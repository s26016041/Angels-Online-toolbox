"""組隊：邀請／同意／退組，以及純讀隊伍狀態。

    team.members(sc)            # 目前隊友（不含自己）
    team.in_party(sc)           # 有隊伍嗎
    team.pending(sc)            # 誰在邀請我（0 = 沒有）
    team.invite(mover, "黑狐", team.SHARE_EVEN)
    team.join(mover, sc)        # 同意（邀請者 id 當場重讀）
    team.leave(mover)           # 退組（隊長、隊員都是這一支）

## 這些東西怎麼來的（2026-08-09，反組譯，不是猜的）

使用者提供六份封包擷取（邀請／同意／退組 × 均分／獨享）。擷取記錄上的
返回位址往前找那道 `call`，得到兩支真正的函式；再從 UI 指令表
（`0x7DD040`，見 [[ui-command-table]]）把遊戲自己的組隊指令逐支反組譯，
動作碼就**寫在遊戲的程式碼裡**，一個都不必猜：

    groupjoin      0x58D2A3   push [ecx+0x338C] / push 1 / call 0x5D5355
    groupkick      0x58D300   push [成員 id]    / push 2 / call 0x5D5355
    grouppromote   0x58D2B9   push [成員 id]    / push 3 / call 0x5D5355
    groupleave     0x58D347   push 0            / push 4 / call 0x5D5355
    groupdisband   0x58D359   push 0            / push 5 / call 0x5D5355
    groupdeny      0x58D36B   push 0            / push 7 / call 0x5D5355
    groupinvite    0x58D26F   push 分配方式 / push 名字 / call 0x5D538A

兩支函式本體（全量在 reports/team_disasm2.txt）：

    0x5D5355(byte 動作, dword 參數)          stdcall（ret 8）
        建封包 代號 0x18、內文 7 → 線材 22   ← 跟擷取到的長度一致
        內文 +2 = 動作、+3 = 參數
    0x5D538A(const char* 角色名, byte 分配方式)  stdcall（ret 8）
        建封包 代號 0x17、內文 36 → 線材 54  ← 跟擷取到的長度一致
        內文 +2 起 32 bytes = 名字、+0x23 = 分配方式（0 均分 / 1 獨享）

★ 兩支都**不需要 this**：呼叫端雖然有 `mov ecx,[0x9B66AC]`，但函式進去第一件
  事就是 `lea ecx,[ebp-0x10]` 把它蓋掉 —— 那是編譯器留下的殘跡。
  所以這裡沒有本專案最怕的「拿猜的 this 去呼叫」崩潰風險。

## 隊伍狀態（純讀）

隊員陣列的位置一樣是從 `grouppromote`／`groupkick` 讀出來的：

    rec = [0x9B66AC] + 0x31A0 + i * 0x62      i = 0..4（遊戲自己 `cmp eax,5`）
    +0x00 成員 id（grouppromote/groupkick 推的就是它）
    +0x08 名字（UTF-8，getcurmembername 的 `add eax,4` 是另一個結構，不是這裡）

⚠ 記錄裡其他欄位**沒有驗證過就不要用**。+0x29 一度看起來像等級，拿
  selfcheck 的真實等級一比就對不上（嵐狐 Lv88 讀成 26、雪狐 Lv87 讀成 95）
  —— 那是「看起來像」的典型陷阱，所以這裡只收 id 與名字。

⚠⚠ **只有 id ≠ 0 的格子算數。** 空格子的名字欄會留著上一次的殘值 ——
  實測某台的 [1][2][3] 都寫著同一個舊名字，改用名字判斷就會看到三個假隊友。

⚠ `[0x9B66AC]` 就是快捷欄那個管理器（`quickbar.MGR_PTR`，已有 AOB 定位）。
  **不要在這裡再寫死一份**：位移後那份會跟上、這份不會，而且不在 SIGS 裡，
  `failed()` 永遠不會提醒（robot.py 檔頭記過同一個坑）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# ⚠ 這兩個值會被 locate.warm() 依 AOB 重新定位，不要在別處複製。
ACTION_FN = 0x005D5355       # f(動作, 參數)：同意／踢人／升隊長／退組／解散／拒絕
INVITE_FN = 0x005D538A       # f(角色名指標, 分配方式)：送出組隊邀請

# 動作碼（全部抄自遊戲自己的 UI 指令，見檔頭）
JOIN, KICK, PROMOTE, LEAVE, DISBAND, DENY = 1, 2, 3, 4, 5, 7

# 分配方式
SHARE_EVEN, SHARE_SOLO = 0, 1

# --- 隊員陣列的版面（反組譯出處見檔頭）---------------------------------
MEMBERS_OFF = 0x31A0
MEMBER_STRIDE = 0x62
MEMBER_MAX = 5               # 遊戲自己 `cmp eax,5 / jge`
M_ID, M_NAME = 0x00, 0x08
NAME_MAX = 0x20              # 邀請封包也是複製 32 bytes（push 0x20）
PENDING_OFF = 0x338C         # 正在邀請我的人的 id（groupjoin 就是推這一格）

SCRATCH_OFF = 0x1C0          # 相對 mover.scratch()；避開 lua 0、jumpmap 0x100、
                             # sell 0x140、exchange 0x180
CALL_TIMEOUT = 1.0


@dataclass(frozen=True)
class Member:
    """一個隊友（不含自己）。只收驗證過的兩個欄位，見檔頭警語。"""

    slot: int
    id: int
    name: str


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else None


def manager(scanner) -> int | None:
    """隊伍／快捷欄共用的那個管理器物件；不合理就回 None。"""
    from app.game import quickbar          # 取最新值（locate.warm 會改寫它）

    mgr = _u32(scanner, quickbar.MGR_PTR)
    if mgr is None or not (0x10000 <= mgr <= 0x7FFF0000):
        return None
    return mgr


def members(scanner) -> list[Member] | None:
    """目前的隊友（**不含自己**）。讀不到回 None、沒有隊伍回空清單。

    ⚠ 「讀不到」與「沒有隊友」是兩件事，不要混成一個空清單 ——
      混掉就會在讀失敗時說「你沒有隊伍」然後去做退組／邀請
      （[[bag-false-empty-guards]] 那個坑犯過三次）。
    """
    mgr = manager(scanner)
    if mgr is None:
        return None
    raw = scanner._read_bytes(mgr + MEMBERS_OFF, MEMBER_STRIDE * MEMBER_MAX)
    if not raw or len(raw) < MEMBER_STRIDE * MEMBER_MAX:
        return None
    raw = bytes(raw)
    out: list[Member] = []
    for i in range(MEMBER_MAX):
        rec = raw[i * MEMBER_STRIDE:(i + 1) * MEMBER_STRIDE]
        mid = struct.unpack_from("<I", rec, M_ID)[0]
        if not mid:
            continue                      # ★ 空格子的名字欄是殘值，一律略過
        name = rec[M_NAME:M_NAME + NAME_MAX].split(b"\x00")[0].decode(
            "utf-8", "replace")
        out.append(Member(i, mid, name))
    return out


def in_party(scanner) -> bool | None:
    """有隊伍嗎；讀不到回 None（呼叫端要當「不知道」，不是「沒有」）。"""
    got = members(scanner)
    return None if got is None else bool(got)


def pending(scanner) -> int | None:
    """正在邀請我的人的 id（0 = 沒有人在邀請）；讀不到回 None。"""
    mgr = manager(scanner)
    return None if mgr is None else _u32(scanner, mgr + PENDING_OFF)


# ---------------------------------------------------------------------------
# 動作（呼叫遊戲自己的函式，跟你在畫面上按那顆按鈕送出的是同一包）
# ---------------------------------------------------------------------------
def _act(mover, action: int, param: int) -> bool:
    if not (mover and mover.active) or not ACTION_FN:
        return False
    with mover.lock:
        return mover.call_sync(ACTION_FN, action, param,
                               timeout=CALL_TIMEOUT) is not None


def leave(mover) -> bool:
    """退組。**隊長與隊員都是這一支**（使用者的兩份擷取都走 groupleave）。"""
    return _act(mover, LEAVE, 0)


def join(mover, scanner) -> tuple[bool, str]:
    """同意組隊邀請。邀請者 id **送出前當場重讀**，沒人邀請就不送。"""
    who = pending(scanner)
    if who is None:
        return False, "讀不到邀請狀態"
    if not who:
        return False, "現在沒有人在邀請"
    if not _act(mover, JOIN, who):
        return False, "排不進指令槽"
    return True, f"已送出同意（邀請者 {who:#x}）"


def invite(mover, name: str, share: int = SHARE_EVEN) -> tuple[bool, str]:
    """邀請某個角色入隊。name 是**角色名**（封包裡帶的就是名字，不是 id）。

    分配方式：SHARE_EVEN(0) 均分制、SHARE_SOLO(1) 獨享制。
    """
    if not (mover and mover.active) or not INVITE_FN:
        return False, "跳板沒裝好或函式沒定位到"
    if share not in (SHARE_EVEN, SHARE_SOLO):
        return False, f"分配方式只能是 0/1（收到 {share}）"
    raw = (name or "").encode("utf-8")
    if not raw:
        return False, "沒有角色名"
    if len(raw) >= NAME_MAX:
        # 遊戲自己是複製固定 32 bytes；塞不下就別送，免得送出被截斷的名字
        return False, f"角色名太長（{len(raw)} bytes，上限 {NAME_MAX - 1}）"
    buf = mover.scratch() + SCRATCH_OFF
    if not buf:
        return False, "沒有暫存區"
    with mover.lock:
        # ⚠ 寫字串與呼叫要在同一個 lock 區間裡：中間被別的功能（lua、賣東西）
        #   插隊寫 scratch 的話，送出去的就是別人的字串。
        if not mover.write(buf, raw + b"\0" * (NAME_MAX + 1 - len(raw))):
            return False, "寫不進暫存區"
        ok = mover.call_sync(INVITE_FN, buf, share,
                             timeout=CALL_TIMEOUT) is not None
    return (ok, "已送出邀請" if ok else "排不進指令槽")
