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
# ⚠ ①~④ 會把這幾支換成假的（寫死位址）—— ⑤ 之後要對真檔案，先留一份真的。
_REAL = {n: getattr(talkwnd, n)
         for n in ('locate', '_wnd_object', '_u32')}
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

print("⑤ 對真的 angel.dat 抄位址／偏移（⛔ 測試裡也不准寫死，改版要自己跟上）")
GAME = r"D:\AngelsOnline\Angels Online Global\angel.dat"
if os.path.exists(GAME):
    import struct

    data = open(GAME, "rb").read()
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    base = struct.unpack_from("<I", data, e_lfanew + 24 + 28)[0]
    span = 0
    img = bytearray(0x1000000)
    for i in range(nsec):
        o = e_lfanew + 24 + opt_size + i * 40
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, o + 8)
        img[va:va + rsize] = data[raw:raw + rsize]
        span = max(span, va + max(vsize, rsize))
    img = bytes(img[:span])

    class DiskSC:
        """把磁碟上的 angel.dat 攤成「載入後的樣子」（VA 連續），純讀。"""

        pid = 0

        def _read_bytes(self, addr, n):
            i = addr - base
            return img[i:i + n] if 0 <= i and i + n <= len(img) else None

        def module_base(self, _name):
            return base

    talkwnd.roulette._module_span = lambda _sc, _b: len(img)
    # ⚠ 前面幾段用的是假 scanner（pid 0、同一個 base）——它們的結果還躺在
    #   `_cache` 裡，不清掉這一段就會拿假位址去對真檔案（會紅得莫名其妙）。
    talkwnd._cache.clear()
    talkwnd._vis_cache.clear()
    for _n, _f in _REAL.items():          # 把 ①~④ 換掉的還原
        setattr(talkwnd, _n, _f)
    sc = DiskSC()
    # ★ 位址一律自己從 UI 指令表推（跟產品同一條路），⛔ 不寫死 ——
    #   9/8 改版就把 messageclose 本體從 0x5D48C6 搬到 0x5914E0，
    #   舊版測試寫死那個數字，改版後整項變紅（跟功能無關的假警報）。
    spot = talkwnd.locate(sc)
    check("messageclose：從指令表推得出本體＋世界全域", spot is not None
          and base < spot.close_fn < base + len(img), str(spot))
    lookup = talkwnd._find_lookup_fn(sc, spot) if spot else None
    check("　從送包本體的骨架抄得到 GetWindowById",
          bool(lookup) and base < lookup < base + len(img),
          f"got={lookup and hex(lookup)}")
    fspot = talkwnd.find_spot(sc)
    check("ismessageend：推得出本體＋視窗管理器全域", fspot is not None
          and base < fspot.world_ptr < base + len(img), str(fspot))
    if spot:
        bad = talkwnd.Spot(cmd_fn=spot.cmd_fn, world_ptr=spot.world_ptr,
                           close_fn=spot.cmd_fn)   # 指令本體：沒有那個骨架
        check("骨架對不上 → None（包裝停用、退回 Lua 確定鈕）",
              talkwnd._find_lookup_fn(sc, bad) is None)

    print("⑥ 「視窗真的顯示中」的旗標偏移（從 window.isvisible 骨架抄）")
    talkwnd._vis_cache.clear()
    off = talkwnd._vis_off(sc)
    check("抄得到偏移", off is not None, f"got={off}")
    check("　落在合理範圍（結構偏移）", off is not None
          and talkwnd._VIS_MIN <= off <= talkwnd._VIS_MAX, f"off={off}")
    # 這一版反組譯看到的是 +0xB4（window.show → SetVisible 寫的同一個 byte）。
    # ⚠ 改版偏移會變 —— 所以**不是**斷言等於 0xB4，只印出來給改版體檢看。
    print(f"    （這一版抄到 {off:#x}；2026-09-12 反組譯看到的是 0xb4）"
          if off else "    （抄不到）")
    vspot = talkwnd._locate_cmd(sc, talkwnd.VIS_CMD, "_vis")
    # ★★ 交叉驗證：isvisible 本體裡查視窗那支，必須跟 messageclose 骨架抄到的
    #   是**同一個** GetWindowById（兩支不同指令指到同一支＝抄對了）。
    check("isvisible 用的查視窗函式＝messageclose 骨架抄到的同一支",
          bool(vspot) and vspot.close_fn == lookup,
          f"isvisible={vspot and hex(vspot.close_fn)} "
          f"messageclose={lookup and hex(lookup)}")
    check("isvisible 的視窗管理器全域＝ismessageend 那個",
          bool(vspot) and bool(fspot) and vspot.world_ptr == fspot.world_ptr,
          f"{vspot} vs {fspot}")

    print("⑦ id_visible／window_visible 的三態（用假記憶體疊在真檔案上）")

    class VisSC(DiskSC):
        """真檔案 ＋ 假的管理器／視窗物件，驗三態（True／False／None）。"""

        MGR = 0x20000000
        OBJ = 0x21000000

        def __init__(self, obj_at_slot=True, wnd_id=0x1234, vis=1,
                     mgr_ok=True, slot_readable=True):
            self.wnd_id, self.vis = wnd_id, vis
            self.obj_at_slot, self.mgr_ok = obj_at_slot, mgr_ok
            self.slot_readable = slot_readable

        def _read_bytes(self, addr, n):
            off_vis = off
            slot = self.MGR + (self.wnd_id & talkwnd.WND_SLOT_MASK) * 4 \
                + talkwnd.WND_TABLE_OFF
            if addr == fspot.world_ptr and n == 4:
                return struct.pack("<I", self.MGR if self.mgr_ok else 0)
            if addr == slot and n == 4:
                if not self.slot_readable:
                    return None
                return struct.pack("<I", self.OBJ if self.obj_at_slot else 0)
            if addr == self.OBJ + talkwnd.WND_ID_OFF and n == 4:
                return struct.pack("<I", self.wnd_id)
            if addr == self.OBJ + off_vis and n == 1:
                return bytes([self.vis])
            return DiskSC._read_bytes(self, addr, n)

    check("顯示中 → True", talkwnd.id_visible(VisSC(vis=1), 0x1234) is True)
    check("旗標 0（殘留的視窗物件）→ False",
          talkwnd.id_visible(VisSC(vis=0), 0x1234) is False)
    check("管理器那一格沒物件 → False",
          talkwnd.id_visible(VisSC(obj_at_slot=False), 0x1234) is False)
    check("代號 0 → False", talkwnd.id_visible(VisSC(), 0) is False)
    check("管理器讀不到 → None（⛔ 不是「沒顯示」）",
          talkwnd.id_visible(VisSC(mgr_ok=False), 0x1234) is None)
    check("那一格讀不到 → None（讀不到 ≠ 沒有）",
          talkwnd.id_visible(VisSC(slot_readable=False), 0x1234) is None)
    check("window_visible：讀 WND_MESSAGE 再看旗標 → True",
          talkwnd.window_visible(VisSC(vis=1)) is True)
    check("window_visible：旗標 0（殘留）→ False",
          talkwnd.window_visible(VisSC(vis=0)) is False)
    talkwnd._vis_cache.clear()
    talkwnd._cache.clear()

    class NoVisSC(DiskSC):
        """抄不到偏移（改版把 isvisible 改寫了）→ 一律 None，不准亂猜。"""

        def _read_bytes(self, addr, n):
            got = DiskSC._read_bytes(self, addr, n)
            if got and vspot and addr == vspot.cmd_fn:
                return bytes(n)                      # 本體變成一片 0：骨架對不上
            return got

    check("抄不到偏移 → None（大聲不知道）",
          talkwnd.id_visible(NoVisSC(), 0x1234) is None)
    talkwnd._vis_cache.clear()
    talkwnd._cache.clear()
else:
    print("  （沒有 angel.dat，跳過 ⑤⑥⑦）")
print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
