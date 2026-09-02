r"""對話視窗：把「無異議對話」那一頁**按確定過掉**。

    talkwnd.close_page(mover, scanner)   → (送出去了嗎, 說明)

## 為什麼需要它（使用者 2026-09-02）

> 「比如跟 NPC 講話順序是這樣：無異議對話 → 選項1 → 無異議對話 → 結束」

沒有選項的那一頁**不是**按 talkaction 過的 —— 它有自己的封包。反組譯實證
（2026-09-02，UI 指令表 `messageclose` → 本體 `0x5D48C6`）：

    建包(代號 0x128, 內文 10)
      [內文+0] = word 0x125
      [內文+2] = [ [[世界+8] + 0x2A90] 查出來的物件 + 0x330 ]
      [內文+6] = [ 用參數查到的視窗物件 + 0xB0 ]
    送出

對照 `sell.TALK_FN`（talkaction）是 `建包(0x0B, 3)`、內文只有一個動作碼 ——
**兩件完全不同的事**。所以腳本要能分別記「送第 N 項」與「過掉這一頁」。

## 怎麼叫

⛔ 不自己重建那個封包（要湊兩個執行期物件欄位，改版一動就送垃圾）。
★ 叫遊戲自己那支：`thiscall(世界物件, 視窗代號)`，跟轉盤 `roulettestart`
  同一招（見 `roulette.py`）—— 位址從 **UI 指令表用字串當錨**推出來，
  官方改版位址跟著跑，零寫死。

參數是**視窗代號**：UI 腳本呼叫的是 `messageclose(WND_MESSAGE)`，那個值
就是 Lua 全域 `WND_MESSAGE`（`lua.globals_of` 讀得到）。
⚠⚠ `WND_MESSAGE` 非 0 **不代表對話開著**（[[lua-readonly-inspect]] 實測 5 台
  有 3 台沒在對話也非 0）——所以這支**不判斷開沒開**，由呼叫端決定何時叫；
  沒開著時叫下去，遊戲那支自己會因為查不到東西而不送（`test edi,edi; je`）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import lua, roulette

GAME_MODULE = "angel.dat"
# UI 指令表裡的名字（明文），拿來當錨 —— ⛔ 不寫死函式位址。
CMD_NAME = b"messageclose\x00"
# 指令本體很短（實測 41 bytes 到 ret）；多讀一點無妨。
CMD_BODY = 0x60
CALL_TIMEOUT = 1.0
# 對話視窗的 Lua 全域名（參數就是它的值）。
WND_NAME = "WND_MESSAGE"

_cache: dict = {}


@dataclass(frozen=True)
class Spot:
    """從 UI 指令表推出來的兩個位址。"""

    cmd_fn: int          # messageclose 指令本體（只拿來報告出處）
    world_ptr: int       # [這裡] = 世界物件（thiscall 的 this）
    close_fn: int        # thiscall(世界物件, 視窗代號) = 送 0x128


def locate(scanner) -> Spot | None:
    """推位址；推不出來回 None（＝**大聲停用**，不亂叫別的函式）。"""
    base = scanner.module_base(GAME_MODULE)
    if not base:
        return None
    key = (getattr(scanner, "pid", 0), base)
    if key in _cache:
        return _cache[key]
    spot = None
    span = roulette._module_span(scanner, base)
    buf = roulette._read_image(scanner, base, span)
    if buf:
        i = buf.find(CMD_NAME)
        j = buf.find(struct.pack("<I", base + i)) if i >= 0 else -1
        if j >= 0:
            fn = struct.unpack_from("<I", buf, j + 4)[0]
            if base <= fn < base + span:
                body = buf[fn - base: fn - base + CMD_BODY]
                world = call = None
                # ⚠⚠ 錨要用**骨架**不是「第幾個 call」：這支指令只有 41 bytes，
                #   往後多讀的部分已經是**別的函式**，拿「最後一個 call」會抓到
                #   鄰居的（2026-09-02 第一版就這樣抓到 0x53D6C0）。
                #   骨架是：`mov ecx,[世界全域]` → **緊接著那個 call** 就是本體
                #   （前面那個 call 是「取參數」）；遇到 ret 就停。
                for k in range(len(body) - 6):
                    op = body[k]
                    if body[k:k + 2] == b"\x8b\x0d" and world is None:
                        world = struct.unpack_from("<I", body, k + 2)[0]
                    elif op == 0xE8 and world is not None and call is None:
                        call = fn + k + 5 + struct.unpack_from("<i", body,
                                                               k + 1)[0]
                    elif op == 0xC3 and call is not None:
                        break
                if (world and call and base <= world < base + span
                        and base <= call < base + span):
                    spot = Spot(cmd_fn=fn, world_ptr=world, close_fn=call)
    _cache[key] = spot
    return spot


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else None


def close_page(mover, scanner) -> tuple[bool, str]:
    """把現在這一頁「無異議對話」按掉（送 `messageclose`）。

    回 (叫下去了嗎, 說明)。⚠ 只保證叫下去了 —— 對話有沒有真的翻頁要看畫面
    （製作頁就是給人在旁邊看的），跑的時候靠「下一步做得成」當證據。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    spot = locate(scanner)
    if spot is None:
        return False, "⚠ 找不到 messageclose 的進入點（官方改寫了？）—— 已停用"
    world = _u32(scanner, spot.world_ptr)
    if not world or not 0x10000 < world < 0x7FFF0000:
        return False, "⚠ 讀不到世界物件"
    g = lua.globals_of(scanner, [WND_NAME]) or {}
    wnd = g.get(WND_NAME)
    if not wnd:
        # 讀不到就別亂送 —— 參數錯的話遊戲會拿去查別的視窗。
        return False, f"⚠ 讀不到 {WND_NAME}（對話視窗代號）"
    with mover.lock:
        ok = mover.call_sync(spot.close_fn, int(wnd), ecx=world,
                             timeout=CALL_TIMEOUT) is not None
    return (ok, "已送出「確定」") if ok else (False, "指令槽忙，等下一輪")
