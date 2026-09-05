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
## ⛔ 刪掉的路：自己送 0x164「快速抽」（2026-08-27 當天做、當天刪）

一度做過「不等動畫、自己送那一包」（參數兩個都讀得到）。**已整段移除**，兩個原因：

1. **使用者實測：轉盤本來就有冷卻**，抽一次要等 CD —— 省下動畫那幾秒沒有意義。
2. **實機按下去回「建封包排不進去（指令槽忙碌）」**，送不出去。

留這條在這裡當紀錄，免得下次有人又想「送包比較快」再走一遍。
★ 真正該走的是下面的 `spin()`：**已實機驗證**（2026-08-27 黑狐，銅幣 75→65、
  抽到「2026-啤酒節銅幣 x30 (2)」×1，spinning True→False 約 3 秒）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

GAME_MODULE = "angel.dat"
CMD_NAME = b"roulettestart\0"
# ⚠⚠ **「掃多少」不等於「模組多大」**（2026-08-27 第一版就栽在這裡）：
#   一開始寫死掃 4MB，又拿 `base + 4MB` 當「這個值在不在模組裡」的上界 ——
#   結果全域 `0x9BD6AC` 離基底 **5.7MB**、直接被自己的檢查擋掉，五台全部回 None。
#   而且指令字串在 `0x3E2890`，離 4MB 邊界只剩 120KB，改版稍微長一點就整個掃不到。
#   → 長度一律問作業系統要（`SizeOfImage`），FALLBACK 只在問不到時墊底。
FALLBACK_SPAN = 0x800000
CMD_BODY = 0x40             # ⚠ 我們自己的掃描長度：指令函式前 0x40 bytes 就夠抽出三個值（見檔頭「位址」節）

# 轉盤物件的欄位 —— **出處全在檔頭「轉盤物件的欄位」那張表**（2026-08-27 實測
# 五台對照）。這裡各留一句，免得只看這幾行的人以為是猜的。
OFF_PARAM1_PTR = 0x00       # → [這裡] 是封包參數1（實測剛抽過那台讀到 0x40）
OFF_KIND = 0x0D             # 轉盤種類；0xFF=沒開（實測只有剛開過的那台是 1）
OFF_DUE_LO = 0x20           # 到期時刻低位（實測五台全 -1 ＝ 沒在轉）
OFF_DUE_HI = 0x24           # 到期時刻高位（實測同上，0x613718 送完就寫回 -1）
KIND_NONE = 0xFF


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


def _module_span(scanner, base: int) -> int:
    """`angel.dat` 的 `SizeOfImage`；問不到就用 FALLBACK_SPAN 墊底。"""
    try:
        for m in scanner.list_modules():
            if m.name.lower() == GAME_MODULE and m.base == base and m.size:
                return int(m.size)
    except Exception:                                      # noqa: BLE001
        pass
    return FALLBACK_SPAN


# 分段讀的段大小（1MB）。⚠ 不能整塊讀：映像有讀不到的段，一次讀整個會整批失敗；
# 也不能砍半重試 —— 砍半會跳過字串（見上面 _read_image 的說明）。
CHUNK = 0x100000


def _read_image(scanner, base: int, span: int) -> bytes | None:
    """把整個映像讀進來找字串。**分段讀，讀不到的段補零。**

    ⚠⚠ 不可以「一次讀整份、失敗就砍半」：映像裡有沒配置的頁時整份會失敗，
      而砍半一下就從 6MB 掉到 3MB —— 指令字串在 3.9MB 處，直接被跳過
      （2026-08-27 寫這支時真的踩到）。分段讀才不會因為尾巴壞掉就丟掉前面，
      補零也讓**位移保持正確**（位移一歪，抽出來的位址全是垃圾）。
    ⚠ 範圍檢查仍然用 `span`：讀不到那一段，不代表那個位址不在模組裡。
    """
    out = bytearray()
    got = False
    for off in range(0, span, CHUNK):
        n = min(CHUNK, span - off)
        raw = scanner._read_bytes(base + off, n)
        if raw and len(raw) == n:
            out += bytes(raw)
            got = True
        else:
            out += b"\0" * n          # 這一段讀不到 → 補零，後面的位移照樣對
    return bytes(out) if got else None


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
    span = _module_span(scanner, base)
    buf = _read_image(scanner, base, span)
    if buf:
        i = buf.find(CMD_NAME)
        j = buf.find(struct.pack("<I", base + i)) if i >= 0 else -1
        if j >= 0:
            fn = struct.unpack_from("<I", buf, j + 4)[0]
            if base <= fn < base + span:
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
                # ⚠ 上界用**模組真正的長度**，不是我們掃了多少（見 FALLBACK_SPAN
                #   上面那段：全域在 .data，離基底比掃描長度遠得多）。
                if (mgr and off and call
                        and base <= mgr < base + span
                        and base <= call < base + span
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
