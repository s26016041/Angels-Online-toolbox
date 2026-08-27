"""活動轉盤：**叫遊戲自己抽**，不自己編封包。

★★★ 活動限定，收攤時跟 `app/tabs/event_tab.py` 一起刪掉就好。

## 為什麼不送封包

2026-08-27 使用者擷取「抽啤酒節銅轉盤」，整段只有**一包** `代號 0x164`
（內文 7 = u32 + u8）。追下去才發現**那一包不是「請幫我抽」，是動畫轉完之後
才送出去的**：

    roulettestart(n)            ← UI 指令（玩家按下去走這條）
      └ 0x6132F5(this=轉盤物件, n)   ← 檢查 + 開始轉（設一個到期時刻）
    …每一拍…
      0x613718(this=轉盤物件)        ← 到期了才送 0x164
        參數1 = [[轉盤物件]]         ← 實測 0x40（跟擷取一字不差）
        參數2 = [轉盤物件+0x29]      ← 實測 0

**參數兩個都來自轉盤物件的狀態**，不是我們能憑空編的；硬送一包還會跳過
「已經在轉了嗎」「種類對不對」那些閘門。所以正解是**呼叫 `roulettestart`
真正做事的那一支**，讓遊戲自己跑完整套。跟修裝走 `repairall`、
對話走 `talkaction` 同一個方針。

## 位址：從遊戲自己的 UI 指令表推，零寫死

不進 `locate.py` 的 SIGS —— 這是活動限定的東西，收攤要能整包刪掉。
改用**字串當錨**（memory `monster-table-aob-deadend` 那一招）：

    1. 映像裡找 `"roulettestart\0"`
    2. 找指向它的 u32 ＝ UI 指令表那一項 → +4 就是指令函式
    3. 反組譯那 0x40 bytes 抽出三個值：
           8b 0d <全域>   → MGR_PTR
           81 c1 <偏移>   → 轉盤物件在管理器裡的偏移
           e8  <rel32>    → 真正做事的那一支（SPIN_FN）
    ✅ 2026-08-27 實測推出來 = (0x9BD6AC, 0xC7D430, 0x6132F5)，跟手動反組譯一致。
    改版位移自動跟上；官方真的改寫這支的話會**推不出來**（大聲停用，不亂叫）。

## 轉盤物件的欄位（實測五台對照，2026-08-27）

    +0x00  指標 → [這裡] 就是封包參數1（剛抽過的那台讀到 0x40）
    +0x0D  目前開著的轉盤種類；**0xFF ＝ 沒開**
           （只有剛開過轉盤的那台是 1，另外四台都是 0xFF）
    +0x20  ┐ 這一轉的到期時刻；**都是 -1 ＝ 沒有在轉**
    +0x24  ┘（0x613718 送完 0x164 就把兩個寫回 -1）
    +0x29  封包參數2（實測 0）

⚠ 結構偏移屬於「大更新才會壞」那一類（CLAUDE.md 允許寫死），但每個都留了
  出處；讀取端一律做合理性驗證，驗不過就回 None 讓呼叫端停手。
## 快速抽（使用者 2026-08-27 要求「不想等動畫」）

上面那條是「請遊戲按下去」，動畫轉完才送包。**也可以自己送那一包**，兩個參數
都在記憶體裡現讀得到（實測 `[[物件]]`=0x40、`[物件+0x29]`=0，跟擷取一字不差）：

    代號 0x164、內文 7 ＝ u16代號 + u32(參數1) + u8(參數2)

⚠⚠ **只有「完全不叫 `roulettestart`」才安全**：客戶端的計時器
  （`0x613718`）是靠 `[物件+0x20/+0x24]` 的到期時刻決定要不要送 0x164 的，
  沒開始轉的話那兩格是 -1、閘門第一關就 `jl` 出去 → 它不會再送第二包。
  所以**兩條路不能混用**（先叫 roulettestart 再自己送＝同一轉送兩包＝多扣一次）。
⚠ 這條**還沒實機驗過**（送包版面有兩份獨立證據，但「伺服器收不收自己送的」
  沒試過）。呼叫端一定要靠背包對帳確認，沒動靜就停手。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import jumpmap

GAME_MODULE = "angel.dat"
CMD_NAME = b"roulettestart\0"
SCAN_SPAN = 0x400000        # 掃整個映像找字串／指令表項
CMD_BODY = 0x40             # 指令函式前 0x40 bytes 就夠抽出三個值

OFF_PARAM1_PTR = 0x00       # → [這裡] 是封包參數1
OFF_KIND = 0x0D             # 目前開著的轉盤種類；0xFF = 沒開
OFF_DUE_LO = 0x20           # 這一轉的到期時刻（-1 = 沒在轉）
OFF_DUE_HI = 0x24
OFF_PARAM2 = 0x29           # 封包參數2（實測 0）
KIND_NONE = 0xFF

# ★ 出處：0x613718 反組譯 —— `6a 07 / 68 64 01 00 00` ＝內文 7、代號 0x164；
#   `89 41 02`（u32 @+2）、`88 41 06`（u8 @+6）。擷取的長度 22 也對得上
#   （線路長度 = 6 + 向上取整16(7) = 22，見 memory packet-opcode-table）。
DRAW_OPCODE = 0x164
DRAW_BODY = 7
# 相對 mover.scratch()；避開 jumpmap 0x100 / sell 0x140 / supply·exchange 0x180 /
# team 0x1C0 / produce 0x200。
SCRATCH_OFF = 0x240

CALL_TIMEOUT = 1.0
_addr_cache: dict[tuple[int, int], "Spot | None"] = {}


@dataclass(frozen=True)
class Spot:
    """從 UI 指令表推出來的三個位址。"""

    cmd_fn: int          # roulettestart 指令本體（只拿來報告出處）
    mgr_ptr: int         # [這裡] = 管理器
    obj_off: int         # 管理器 + 這個 = 轉盤物件
    spin_fn: int         # thiscall(轉盤物件, 種類) = 開始轉


@dataclass(frozen=True)
class State:
    """轉盤現在的狀態（純讀）。"""

    obj: int
    kind: int            # 目前開著的轉盤種類；KIND_NONE = 沒開
    spinning: bool       # 正在轉（到期時刻還沒到）

    @property
    def open(self) -> bool:
        return self.kind != KIND_NONE


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else None


def locate(scanner) -> Spot | None:
    """從 UI 指令表推出三個位址；推不出來回 None（＝大聲停用，不亂叫）。

    整個映像掃一次約幾十毫秒，結果按 (pid, 模組基底) 快取 —— 改版重開遊戲
    基底不變但內容會變，所以**同一顆 exe 換版時要重開工具箱**才會重推；
    這在活動功能上可以接受（開機時本來就重來一次）。
    """
    base = scanner.module_base(GAME_MODULE)
    if not base:
        return None
    key = (getattr(scanner, "pid", 0), base)
    if key in _addr_cache:
        return _addr_cache[key]
    spot = None
    raw = scanner._read_bytes(base, SCAN_SPAN)
    if raw:
        buf = bytes(raw)
        i = buf.find(CMD_NAME)
        j = buf.find(struct.pack("<I", base + i)) if i >= 0 else -1
        if j >= 0:
            fn = struct.unpack_from("<I", buf, j + 4)[0]
            if base <= fn < base + SCAN_SPAN:
                body = buf[fn - base: fn - base + CMD_BODY]
                mgr = off = call = None
                for k in range(len(body) - 6):
                    if body[k:k + 2] == b"\x8b\x0d" and mgr is None:
                        mgr = struct.unpack_from("<I", body, k + 2)[0]
                    elif body[k:k + 2] == b"\x81\xc1" and off is None:
                        off = struct.unpack_from("<I", body, k + 2)[0]
                    elif body[k] == 0xE8:
                        call = fn + k + 5 + struct.unpack_from("<i", body, k + 1)[0]
                # 三個都要抽到、而且落在合理範圍才算數
                if (mgr and off and call
                        and base <= mgr < base + SCAN_SPAN
                        and base <= call < base + SCAN_SPAN
                        and 0 < off < 0x2000000):
                    spot = Spot(cmd_fn=fn, mgr_ptr=mgr, obj_off=off,
                                spin_fn=call)
    _addr_cache[key] = spot
    return spot


def state(scanner) -> State | None:
    """轉盤現在的狀態；讀不到／位址推不出來回 None。"""
    spot = locate(scanner)
    if spot is None:
        return None
    mgr = _u32(scanner, spot.mgr_ptr)
    if not mgr or not 0x10000 < mgr < 0x7FFF0000:
        return None
    obj = mgr + spot.obj_off
    raw = scanner._read_bytes(obj, 0x2C)
    if not raw or len(raw) < 0x2C:
        return None
    b = bytes(raw)
    kind = b[OFF_KIND]
    lo = struct.unpack_from("<i", b, OFF_DUE_LO)[0]
    hi = struct.unpack_from("<i", b, OFF_DUE_HI)[0]
    return State(obj=obj, kind=kind, spinning=not (lo == -1 and hi == -1))


def spin(mover, scanner) -> tuple[bool, str]:
    """抽一次。回 (送出去了嗎, 說明)。

    ⚠⚠ 交給遊戲的東西**送出前當場重讀重驗**（CLAUDE.md 鐵則）：
      物件位址與種類都是這一拍現讀的，不是呼叫端先前那一拍拿到的。
    ⚠ 只保證「叫下去了」；真的有沒有抽到要靠背包對帳（呼叫端做）。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    spot = locate(scanner)
    if spot is None:
        return False, "⚠ 找不到轉盤的程式進入點（官方改寫了？）—— 已停用"
    st = state(scanner)
    if st is None:
        return False, "⚠ 讀不到轉盤狀態"
    if not st.open:
        return False, "⚠ 轉盤視窗沒開 —— 先在遊戲裡跟啤酒節使者選好要抽哪個轉盤"
    if st.spinning:
        return False, "還在轉，等它轉完"
    # 參數＝遊戲自己記的種類（0x6132F5 會拿它跟 [物件+0x0D] 比對，不符就不動作）
    with mover.lock:
        ok = mover.call_sync(spot.spin_fn, st.kind, ecx=st.obj,
                             timeout=CALL_TIMEOUT) is not None
    return (ok, "已叫下去" if ok else "指令槽忙，等下一輪")


def draw_args(scanner) -> tuple[int, int] | None:
    """封包 0x164 的兩個參數 `(參數1, 參數2)`，**現讀**；讀不到／不合理回 None。

    出處（0x613718 反組譯，實測值與擷取一字不差）：
        參數1 = `[[轉盤物件]]`      ← `mov eax,[esi]` / `push [eax]`
        參數2 = `[轉盤物件+0x29]`   ← `movzx eax, byte [esi+0x29]`
    """
    st = state(scanner)
    if st is None or not st.open:
        return None
    inner = _u32(scanner, st.obj + OFF_PARAM1_PTR)
    if inner is None or not 0x10000 < inner < 0x7FFF0000:
        return None                       # 視窗剛開還沒填好 → 不要拿垃圾去送
    p1 = _u32(scanner, inner)
    raw = scanner._read_bytes(st.obj + OFF_PARAM2, 1)
    if p1 is None or not raw:
        return None
    return p1, bytes(raw)[0]


def draw(mover, scanner) -> tuple[bool, str]:
    """**快速抽**：自己送 0x164，不等客戶端的動畫。回 (送出去了嗎, 說明)。

    ⚠⚠ **絕對不要跟 `spin()` 混用**：`spin()` 會讓客戶端起一個計時器，時間到
      它自己也送一包 —— 同一轉送兩包＝多扣一次。要嘛全走這支，要嘛全走 spin()。
    ⚠ 只保證「送出去了」；有沒有抽到要靠**背包對帳**（呼叫端做）。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if not (jumpmap.BUILD_FN and jumpmap.SEND_FN):
        return False, "送包位址還沒定位（改版？先跑 patch_doctor）"
    st = state(scanner)
    if st is None:
        return False, "⚠ 讀不到轉盤狀態"
    if not st.open:
        return False, "⚠ 轉盤視窗沒開 —— 先在遊戲裡跟啤酒節使者選好要抽哪個轉盤"
    if st.spinning:
        # 客戶端正在轉（有人按了遊戲自己的按鈕）→ 它待會會自己送一包，
        # 這時再送就是兩包。讓開。
        return False, "客戶端正在轉，這一輪讓開"
    args = draw_args(scanner)
    if args is None:
        return False, "⚠ 讀不到封包參數（轉盤資料還沒填好？）"
    p1, p2 = args
    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(jumpmap.BUILD_FN, DRAW_OPCODE, DRAW_BODY, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return False, "建封包排不進去（指令槽忙碌）"
        data = _u32(scanner, buf + 4)
        if data is None or not 0x10000 < data < 0x7FFF0000:
            return False, "封包資料指標不合理"
        # data+0 代號由建構函式寫；我們填 +2 參數1(u32)、+6 參數2(u8)。
        if not mover.write(data + 2, struct.pack("<IB", p1, p2)):
            return False, "寫封包內容失敗"
        conn = _u32(scanner, jumpmap.CONN_PTR)
        pkt = _u32(scanner, buf + 0xC)
        if not conn:
            return False, "還沒連上線 —— 可能正在重連"
        if pkt is None or not 0x10000 < pkt < 0x7FFF0000:
            return False, "封包指標不合理"
        if mover.call_sync(jumpmap.SEND_FN, conn, pkt,
                           timeout=CALL_TIMEOUT) is None:
            return False, "送出排不進去（指令槽忙碌）"
    return True, f"已送出（參數 {p1:#x}, {p2}）"
