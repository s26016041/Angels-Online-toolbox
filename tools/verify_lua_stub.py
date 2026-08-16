"""離線模擬 lua.py 的原子序列 stub：unicorn 真的執行組出來的位元組。

    py tools\\verify_lua_stub.py        # 不用開遊戲；改 _seq_stub_asm/_seq_pack 後必跑

假 getfield/pcall/pushstring 驗證：呼叫順序與參數、Lua 堆疊上推的 TValue、
結果抄回 P、top 還原、esp 平衡、三條失敗分支。
★ 位元組與 P 版面都用 app.game.lua 的**真**函式（_seq_stub_asm/_seq_pack），
  不另寫一份（trip_sim 假通過的教訓）。
⚠ 需要 unicorn（pip install unicorn；keystone 專案本來就有）。
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keystone                                            # noqa: E402
from unicorn import Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE  # noqa: E402
from unicorn.x86_const import (UC_X86_REG_EAX, UC_X86_REG_ESP)  # noqa: E402

from app.game import lua                                   # noqa: E402

CODE = 0x00500000
FAKE_GETF, FAKE_PCALL, FAKE_PUSHS = 0x00600000, 0x00600100, 0x00600200
SENTINEL = 0x00600F00
P_ADDR, POOL = 0x00700300, 0x00700500
L_ADDR, LSTACK = 0x00800000, 0x00801000
TS_HEAP = 0x00803000
HOST_STK = 0x00910000

ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
ks.syntax = keystone.KS_OPT_SYNTAX_INTEL
STUB, _ = ks.asm(lua._seq_stub_asm(), addr=CODE)
STUB = bytes(STUB)
print(f"stub 組出來 {len(STUB)} bytes（上限 0x600）")
assert len(STUB) <= 0x600

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"　{detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def run(parts, args, do_pcall, scenario, stack_last_off=0x1000,
        pcall_rc=0, pcall_result=("num", 0.0)):
    """跑一次 stub。scenario: {名字: (tt, value)} 決定假 getfield 推什麼。
    回 (eax, P位元組, top最後值, esp最後值, log)。"""
    uc = Uc(UC_ARCH_X86, UC_MODE_32)
    for base, size in ((0x00500000, 0x2000), (0x00600000, 0x1000),
                       (0x00700000, 0x1000), (0x00800000, 0x4000),
                       (0x00900000, 0x11000)):
        uc.mem_map(base, size)
    uc.mem_write(CODE, STUB)
    for fk in (FAKE_GETF, FAKE_PCALL, FAKE_PUSHS, SENTINEL):
        uc.mem_write(fk, b"\xc3")          # ret；副作用在 hook 裡做
    # L：top=+8、stack_last=+0x1C
    uc.mem_write(L_ADDR + 8, struct.pack("<I", LSTACK))
    uc.mem_write(L_ADDR + 0x1C, struct.pack("<I", LSTACK + stack_last_off))

    packed = lua._seq_pack(L_ADDR, parts, args, do_pcall, POOL,
                           FAKE_GETF, FAKE_PCALL, FAKE_PUSHS)
    assert packed is not None
    blob, data = packed
    if blob:
        uc.mem_write(POOL, blob)
    uc.mem_write(P_ADDR, data)

    esp0 = HOST_STK - 8
    uc.mem_write(esp0, struct.pack("<II", SENTINEL, P_ADDR))
    uc.reg_write(UC_X86_REG_ESP, esp0)

    log = {"getf": [], "push": [], "pcall": None, "pcall_stack": None}
    ts_bump = [TS_HEAP]

    def cstr(addr):
        raw = uc.mem_read(addr, 256)
        return bytes(raw).split(b"\0")[0]

    def u32(addr):
        return struct.unpack("<I", uc.mem_read(addr, 4))[0]

    def push_tv(tv):
        top = u32(L_ADDR + 8)
        uc.mem_write(top, tv)
        uc.mem_write(L_ADDR + 8, struct.pack("<I", top + 16))

    def make_ts(payload: bytes) -> int:
        at = ts_bump[0]
        ts_bump[0] += 0x20 + len(payload) + 16
        uc.mem_write(at + 0xC, struct.pack("<I", len(payload)))
        uc.mem_write(at + 0x10, payload + b"\0")
        return at

    def on_code(uc_, addr, _size, _ud):
        esp = uc_.reg_read(UC_X86_REG_ESP)
        if addr == FAKE_GETF:
            L, idx, k = (u32(esp + 4), u32(esp + 8), u32(esp + 12))
            name = cstr(k).decode()
            log["getf"].append((L, idx & 0xFFFFFFFF, name))
            tt, val = scenario[name]
            if tt == 3:
                push_tv(struct.pack("<dii", float(val), 3, 0))
            elif tt == 4:
                push_tv(struct.pack("<Iiii", make_ts(val), 0, 4, 0))
            else:
                push_tv(struct.pack("<iiii", 0, 0, tt, 0))
        elif addr == FAKE_PUSHS:
            L, s = u32(esp + 4), u32(esp + 8)
            log["push"].append((L, cstr(s)))
            push_tv(struct.pack("<Iiii", make_ts(cstr(s)), 0, 4, 0))
        elif addr == FAKE_PCALL:
            L, na, nr, ef = (u32(esp + 4), u32(esp + 8),
                             u32(esp + 12), u32(esp + 16))
            log["pcall"] = (L, na, nr, ef)
            top = u32(L_ADDR + 8)
            fslot = top - (na + 1) * 16
            log["pcall_stack"] = bytes(uc.mem_read(fslot, (na + 1) * 16))
            uc.mem_write(L_ADDR + 8, struct.pack("<I", fslot))  # 收掉 func+args
            kind, val = pcall_result
            if kind == "num":
                push_tv(struct.pack("<dii", float(val), 3, 0))
            else:
                push_tv(struct.pack("<Iiii", make_ts(val), 0, 4, 0))
            uc_.reg_write(UC_X86_REG_EAX, pcall_rc)

    uc.hook_add(UC_HOOK_CODE, on_code, begin=0x00600000, end=0x00600FFF)
    uc.emu_start(CODE, SENTINEL, timeout=2_000_000, count=100_000)
    eax = uc.reg_read(UC_X86_REG_EAX) & 0xFFFFFFFF
    return (eax, bytes(uc.mem_read(P_ADDR, 0x1A0)),
            u32(L_ADDR + 8), uc.reg_read(UC_X86_REG_ESP), log)


G = lua.GLOBALSINDEX

# ── A：完整 call —— 2 層名字、數字/布林/字串參數、字串回傳 ──────────
eax, p, top, esp, log = run(
    ["game", "makeadd"], (7, True, "菇菇"), True,
    {"game": (5, None), "makeadd": (6, None)},
    pcall_result=("str", "回傳值OK".encode("utf-8")))
check("A eax=rc=0", eax == 0)
check("A getfield 順序與 idx", log["getf"] == [
    (L_ADDR, G, "game"), (L_ADDR, 0xFFFFFFFF, "makeadd")], str(log["getf"]))
check("A pushstring 收到 UTF-8", log["push"] == [(L_ADDR, "菇菇".encode("utf-8"))],
      str(log["push"]))
check("A pcall(L,3,1,0)", log["pcall"] == (L_ADDR, 3, 1, 0), str(log["pcall"]))
st = log["pcall_stack"]
check("A 堆疊: 函式在底", struct.unpack_from("<i", st, 8)[0] == 6)
check("A 堆疊: 參數1=數字7", struct.unpack_from("<dii", st, 16)[:2] == (7.0, 3))
check("A 堆疊: 參數2=bool真", struct.unpack_from("<iii", st, 32)[0] == 1
      and struct.unpack_from("<i", st, 40)[0] == 1)
check("A 堆疊: 參數3=字串", struct.unpack_from("<i", st, 56)[0] == 4)
check("A rc_out=0", struct.unpack_from("<I", p, 0x2C)[0] == 0)
check("A 結果 tt=字串", struct.unpack_from("<i", p, 0x38)[0] == 4)
want = "回傳值OK".encode("utf-8")
check("A 結果長度", struct.unpack_from("<I", p, 0x40)[0] == len(want))
check("A 結果內容", p[0x44:0x44 + len(want)] == want)
check("A top 還原", top == LSTACK)
check("A esp 平衡", esp == HOST_STK - 8 + 4)

# ── B：get_global（do_pcall=0）讀數字 ───────────────────────────
eax, p, top, esp, log = run(["WND_MAKE"], (), False, {"WND_MAKE": (3, 1234.5)})
check("B eax=0", eax == 0)
check("B 沒叫 pcall", log["pcall"] is None)
check("B 結果=1234.5", struct.unpack_from("<dii", p, 0x30)[:2] == (1234.5, 3))
check("B top 還原", top == LSTACK)

# ── C：查到的不是函式 ───────────────────────────────────────────
eax, p, top, esp, log = run(["game", "nope"], (1,), True,
                            {"game": (5, None), "nope": (0, None)})
check("C rc=NOT_FUNCTION", eax == 0xFFFFFFFE)
check("C 沒叫 pcall", log["pcall"] is None)
check("C top 還原", top == LSTACK)

# ── D：堆疊快滿要擋 ─────────────────────────────────────────────
eax, p, top, esp, log = run(["f"], tuple(range(8)), True, {"f": (6, None)},
                            stack_last_off=0x30)
check("D rc=NO_ROOM", eax == 0xFFFFFFFD)
check("D 沒叫 pcall", log["pcall"] is None)
check("D top 還原", top == LSTACK)

# ── E：pcall 錯誤 rc=2＋超長錯誤訊息要截斷在 0xB8 ────────────────
long_msg = ("錯" * 150).encode("utf-8")          # 450 bytes
eax, p, top, esp, log = run(["f"], (), True, {"f": (6, None)},
                            pcall_rc=2, pcall_result=("str", long_msg))
check("E eax=rc=2", eax == 2)
check("E 長度截在 0xB8", struct.unpack_from("<I", p, 0x40)[0] == 0xB8)
check("E 內容=前 0xB8 bytes", p[0x44:0x44 + 0xB8] == long_msg[:0xB8])
check("E 沒蓋到參數區(0x100)", p[0xFC:0x100] == b"\0" * 4)
check("E top 還原", top == LSTACK)

print()
print("全部通過 ✅" if not fails else f"❌ {len(fails)} 項失敗：{fails}")
sys.exit(1 if fails else 0)
