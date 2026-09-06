"""talkwnd_check — 對話視窗「按確定」的防呆回歸（離線、秒殺）。

    py tools\talkwnd_check.py

驗的是 2026-09-06 黑狐 11:49 遊戲崩潰的根因（error.log EIP=0x5D498A、ESI=0）：
遊戲的 messageclose 送包本體 `0x5D494D` 對「代號查不到視窗物件」**沒有防呆**
（只守玩家物件那個 `test edi,edi`，視窗物件 esi 直接 `mov eax,[esi+0xB0]`），
`WND_MESSAGE` 殘留舊代號、物件已被遊戲收掉（傳點順移那一瞬間）時叫下去就是讀 NULL 崩潰。
→ `talkwnd.close_page` 送之前一律用 `_wnd_object` 當場驗物件還在；查不到就不送。
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.game import talkwnd                             # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class FakeMover:
    active = True
    lock = threading.Lock()

    def __init__(self):
        self.calls = []

    def call_sync(self, fn, *a, **k):
        self.calls.append((fn, a))
        return 1


lua_calls: list = []
talkwnd.lua.globals_of = lambda _sc, _names: {"WND_MESSAGE": 0x1234}
talkwnd.lua.call = lambda _mv, _sc, fn, *a: (lua_calls.append((fn, a)) or (True, None))
SC = object()

print("① 代號殘留、視窗物件已不在 → 不送（送了遊戲會當）")
talkwnd._wnd_object = lambda _sc: None
mv = FakeMover()
ok, why = talkwnd.close_page(mv, SC)
check("close_page 回 False、說得出原因", not ok and "不送" in why, f"{ok} {why}")
check("Lua OnMessageClose 一次都沒叫", lua_calls == [], str(lua_calls))
check("C 函式退化路也沒叫", mv.calls == [], str(mv.calls))

print("② 視窗物件在 → 照按遊戲的確定鈕")
talkwnd._wnd_object = lambda _sc: 0x30000000
ok, why = talkwnd.close_page(mv, SC)
check("close_page 回 True", ok, f"{ok} {why}")
check("叫的是 OnMessageClose(代號)", lua_calls == [("OnMessageClose", (0x1234,))], str(lua_calls))

print("③ Lua 叫不動 → 退化路（C 函式）送之前一樣驗物件")
lua_calls.clear()
talkwnd.lua.call = lambda _mv, _sc, fn, *a: (False, None)
talkwnd.locate = lambda _sc: talkwnd.Spot(cmd_fn=0x5D48C6, world_ptr=0x9F0000, close_fn=0x5D494D)
talkwnd._u32 = lambda _sc, addr: 0x30001000
talkwnd._wnd_object = lambda _sc: None
mv = FakeMover()
ok, why = talkwnd.close_page(mv, SC)
check("物件不在 → 退化路也不送", not ok and mv.calls == [] and "不送" in why, f"{ok} {why} {mv.calls}")
talkwnd._wnd_object = lambda _sc: 0x30000000
talkwnd.close_window = lambda _mv, _sc: True
ok, why = talkwnd.close_page(mv, SC)
check("物件在 → 退化路送 close_fn(代號)", ok and mv.calls == [(0x5D494D, (0x1234,))],
      f"{ok} {why} {mv.calls}")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
