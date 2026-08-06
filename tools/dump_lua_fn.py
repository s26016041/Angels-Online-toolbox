"""純讀取：倒出一個 Lua 函式的常數表與 bytecode（Lua 5.1）。

遊戲的 UI 邏輯全在 Lua 裡、腳本檔又是加密的 .pak —— 但**載入後的函式物件
就在記憶體裡**，常數表＋反組譯足以看懂一支小函式在做什麼。不注入、不寫入。

實例：「復活方式」下拉選單存什麼值就是這樣確認的 ——
`ResurrectionSelected` 的 bytecode 是
`settitle(getstring(1955+列號))` → `setrobotvar_int(2102, 列號)`，
所以 2102 的值＝下拉列號（1955 靈魂、1956 回城、1957 原地 → 0/1/2）。

    py tools/dump_lua_fn.py --pid 45724 ResurrectionSelected --code
    py tools/dump_lua_fn.py --pid 45724 OnClickAssistCheckButton
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.memory import MemoryScanner            # noqa: E402
from app.game import locate, lua                     # noqa: E402
from tools.dump_lua_globals import globals_table, u8, u32   # noqa: E402

# Lua 5.1（32-bit）的 Proto 版面
P_K, P_CODE, P_SUBP = 0x08, 0x0C, 0x10
P_SIZEK, P_SIZECODE, P_SIZEP = 0x28, 0x2C, 0x34
P_LINEDEF = 0x3C
# LClosure：+6 isC、+0x10 Proto*
CL_ISC, CL_PROTO = 0x06, 0x10

OPS = ("MOVE LOADK LOADBOOL LOADNIL GETUPVAL GETGLOBAL GETTABLE SETGLOBAL "
       "SETUPVAL SETTABLE NEWTABLE SELF ADD SUB MUL DIV MOD POW UNM NOT LEN "
       "CONCAT JMP EQ LT LE TEST TESTSET CALL TAILCALL RETURN FORLOOP "
       "FORPREP TFORLOOP SETLIST CLOSE CLOSURE VARARG").split()


def i32(sc, a):
    raw = sc._read_bytes(a, 4)
    return struct.unpack("<i", bytes(raw))[0] if raw else None


def read_str(sc, ts, limit=256):
    """跟 dump_lua_globals 的不同：字串常數可能是中文（big5）。"""
    n = u32(sc, ts + lua.OFF_TSTRING_LEN)
    if n is None or not 0 <= n < limit:
        return None
    raw = sc._read_bytes(ts + lua.OFF_TSTRING_DATA, n)
    return bytes(raw).decode("big5", errors="replace") if raw else None


def find_function(sc, tab, want: str):
    """全域表裡找這個名字的 Lua closure；不是 Lua 函式回 None。"""
    lsize = u8(sc, tab + 0x07)
    node = u32(sc, tab + 0x10)
    if lsize is None or not node:
        return None
    for i in range(1 << lsize):
        raw = sc._read_bytes(node + i * 32, 32)
        if not raw or struct.unpack_from("<I", raw, 24)[0] != lua.T_STRING:
            continue
        if read_str(sc, struct.unpack_from("<I", raw, 16)[0]) != want:
            continue
        if struct.unpack_from("<I", raw, 8)[0] != lua.T_FUNCTION:
            return None
        cl = struct.unpack_from("<I", raw, 0)[0]
        if u8(sc, cl + CL_ISC):
            return None                       # C 函式沒有 Proto
        return u32(sc, cl + CL_PROTO)
    return None


def const_str(sc, k, i):
    raw = sc._read_bytes(k + i * 16, 16)
    if not raw:
        return "?"
    tt = struct.unpack_from("<I", raw, 8)[0]
    if tt == lua.T_NUMBER:
        v = struct.unpack_from("<d", raw, 0)[0]
        return str(int(v) if v == int(v) else v)
    if tt == lua.T_STRING:
        return repr(read_str(sc, struct.unpack_from("<I", raw, 0)[0]))
    if tt == lua.T_BOOL:
        return str(bool(struct.unpack_from("<I", raw, 0)[0]))
    return f"<tt{tt}>"


def dump_consts(sc, p, depth=0):
    pad = "  " * depth
    nk = i32(sc, p + P_SIZEK) or 0
    k = u32(sc, p + P_K)
    print(f"{pad}proto @{p:#x}（第 {i32(sc, p + P_LINEDEF)} 行起，{nk} 個常數）")
    if k and 0 < nk < 500:
        for i in range(nk):
            print(f"{pad}  [{i}] {const_str(sc, k, i)}")
    nsub = i32(sc, p + P_SIZEP) or 0
    subs = u32(sc, p + P_SUBP)
    if subs and 0 < nsub < 50:
        for i in range(nsub):
            sp = u32(sc, subs + i * 4)
            if sp:
                dump_consts(sc, sp, depth + 1)


def rk(sc, k, x):
    return f"K({const_str(sc, k, x - 256)})" if x >= 256 else f"R{x}"


def dump_code(sc, p):
    k = u32(sc, p + P_K)
    code = u32(sc, p + P_CODE)
    n = i32(sc, p + P_SIZECODE) or 0
    print(f"  {n} 道指令：")
    for i in range(min(n, 2000)):
        ins = u32(sc, code + i * 4)
        if ins is None:
            break
        op = ins & 0x3F
        a = (ins >> 6) & 0xFF
        c = (ins >> 14) & 0x1FF
        b = (ins >> 23) & 0x1FF
        bx = ins >> 14
        name = OPS[op] if op < len(OPS) else f"OP{op}"
        if name in ("LOADK", "GETGLOBAL", "SETGLOBAL"):
            print(f"  {i:4}  {name:<10} R{a} {const_str(sc, k, bx)}")
        elif name == "JMP":
            print(f"  {i:4}  {name:<10} → {i + 1 + (bx - 131071)}")
        elif name in ("GETTABLE", "SETTABLE", "SELF", "ADD", "SUB", "MUL",
                      "DIV", "MOD", "POW", "EQ", "LT", "LE"):
            print(f"  {i:4}  {name:<10} R{a} {rk(sc, k, b)} {rk(sc, k, c)}")
        else:
            print(f"  {i:4}  {name:<10} A={a} B={b} C={c}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("names", nargs="+", help="Lua 全域函式的名字")
    ap.add_argument("--code", action="store_true", help="連 bytecode 一起反組譯")
    args = ap.parse_args()

    sc = MemoryScanner()
    sc.open(args.pid)
    try:
        locate.warm(sc)
        tab = globals_table(sc)
        if not tab:
            print("拿不到 Lua 全域表（遊戲還沒進場？）")
            return 1
        for name in args.names:
            print(f"\n■ {name}")
            p = find_function(sc, tab, name)
            if not p:
                print("  找不到、不是函式、或是 C 函式（沒有常數表）")
                continue
            dump_consts(sc, p, 1)
            if args.code:
                dump_code(sc, p)
    finally:
        sc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
