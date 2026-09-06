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
talkwnd.locate = lambda _sc: None      # ①② 不走防呆包裝（③ 再換成有 Spot 的）

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

print("④ 防呆包裝（unicorn 模擬）：視窗不在 → 不叫送包本體、回 0；在 → 叫、回 1")
import keystone                                            # noqa: E402
from unicorn import Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE  # noqa: E402
from unicorn.x86_const import (UC_X86_REG_EAX, UC_X86_REG_ECX,   # noqa: E402
                               UC_X86_REG_ESI, UC_X86_REG_ESP)

GUARD, LOOKUP, CLOSE, WORLD, STACK = 0x10000, 0x20000, 0x30000, 0x40000, 0x50000
MGR, WND = 0x41000, 0x1234


def emulate(found: bool):
    ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
    ks.syntax = keystone.KS_OPT_SYNTAX_INTEL
    guard, _ = ks.asm(talkwnd._guard_asm(LOOKUP, CLOSE), addr=GUARD)
    # 假 GetWindowById：驗 ecx==管理器、[esp+4]==代號，回物件或 0；ret 4（__thiscall）
    lookup, _ = ks.asm(f"mov eax, {0x30000000 if found else 0:#x}; ret 0x4", addr=LOOKUP)
    close, _ = ks.asm("mov eax, 0x77; mov esi, 0; ret 0x4", addr=CLOSE)   # 故意弄髒 esi 看包裝保不保
    uc = Uc(UC_ARCH_X86, UC_MODE_32)
    for a in (GUARD, LOOKUP, CLOSE, WORLD, STACK):
        uc.mem_map(a, 0x1000)
    uc.mem_write(GUARD, bytes(guard))
    uc.mem_write(LOOKUP, bytes(lookup))
    uc.mem_write(CLOSE, bytes(close))
    uc.mem_write(WORLD + 0xC, MGR.to_bytes(4, "little"))
    seen = {"lookup": [], "close": []}

    def hook(u, addr, size, _ud):
        if addr == LOOKUP:
            seen["lookup"].append((u.reg_read(UC_X86_REG_ECX),
                                   int.from_bytes(u.mem_read(u.reg_read(UC_X86_REG_ESP) + 4, 4), "little")))
        if addr == CLOSE:
            seen["close"].append((u.reg_read(UC_X86_REG_ECX),
                                  int.from_bytes(u.mem_read(u.reg_read(UC_X86_REG_ESP) + 4, 4), "little")))
    uc.hook_add(UC_HOOK_CODE, hook)
    esp0 = STACK + 0x800
    uc.mem_write(esp0 + 4, WND.to_bytes(4, "little"))          # 參數
    uc.mem_write(esp0, (0xDEAD0000).to_bytes(4, "little"))      # 回傳位址（跑到這裡就停）
    uc.reg_write(UC_X86_REG_ESP, esp0)
    uc.reg_write(UC_X86_REG_ECX, WORLD)
    uc.reg_write(UC_X86_REG_ESI, 0x5E5E5E5E)
    uc.emu_start(GUARD, 0xDEAD0000, timeout=200000, count=200)
    return (uc.reg_read(UC_X86_REG_EAX), uc.reg_read(UC_X86_REG_ESP) - esp0,
            uc.reg_read(UC_X86_REG_ESI), seen)


eax, dsp, esi, seen = emulate(found=False)
check("視窗不在：查了一次、沒叫送包本體", seen["lookup"] == [(MGR, WND)] and seen["close"] == [], str(seen))
check("　回 0", eax == 0, f"eax={eax:#x}")
check("　堆疊平衡（ret 4 吃掉參數）、esi 還原", dsp == 8 and esi == 0x5E5E5E5E, f"dsp={dsp} esi={esi:#x}")
eax, dsp, esi, seen = emulate(found=True)
check("視窗在：查一次、送包本體叫一次（世界, 代號）",
      seen["lookup"] == [(MGR, WND)] and seen["close"] == [(WORLD, WND)], str(seen))
check("　回 1", eax == 1, f"eax={eax:#x}")
check("　堆疊平衡、esi 還原（本體弄髒了也保得住）", dsp == 8 and esi == 0x5E5E5E5E, f"dsp={dsp} esi={esi:#x}")

print("⑤ 從送包本體的骨架抄 GetWindowById 位址（磁碟上的 angel.dat）")
GAME = r"D:\AngelsOnline\Angels Online Global\angel.dat"
if os.path.exists(GAME):
    import struct
    data = open(GAME, "rb").read()
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    base = struct.unpack_from("<I", data, e_lfanew + 24 + 28)[0]
    secs = []
    for i in range(nsec):
        o = e_lfanew + 24 + opt_size + i * 40
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, o + 8)
        secs.append((va, max(vsize, rsize), raw))

    def off(va):
        rva = va - base
        for sva, sz, raw in secs:
            if sva <= rva < sva + sz:
                return raw + (rva - sva)
        return None

    class DiskSC:
        def _read_bytes(self, addr, n):
            o = off(addr)
            return data[o:o + n] if o is not None else None

        def module_base(self, _name):
            return base

    talkwnd.roulette._module_span = lambda _sc, _b: 0x800000
    spot = talkwnd.Spot(cmd_fn=0x5D48C6, world_ptr=0x9F3400, close_fn=0x5D494D)
    got = talkwnd._find_lookup_fn(DiskSC(), spot)
    check("抄到的就是反組譯看到的 GetWindowById 0x5037F3", got == 0x5037F3, f"got={got and hex(got)}")
    bad = talkwnd.Spot(cmd_fn=0x5D48C6, world_ptr=0x9F3400, close_fn=0x5D4918)   # 別的函式：骨架對不上
    check("骨架對不上 → None（包裝停用、退回 Lua 確定鈕）", talkwnd._find_lookup_fn(DiskSC(), bad) is None)
else:
    print("  （沒有 angel.dat，跳過）")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
