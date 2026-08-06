"""純讀取：把遊戲 Lua 的全域常數（數字/布林）全部倒出來。

不注入、不呼叫 Lua、不寫入 —— 只 ReadProcessMemory 走全域表的雜湊節點，
開著掛機跑也不影響。遊戲的常數（DATAID_*、WND_*、控制項 id…）都在這裡，
**找設定的代號先跑這支**，不必再去猜或反組譯 C++。

實例：「陣亡時自動復活」的 DATAID 就是這樣找到的 ——
DATAID_AUTO_RESURRECTION_CHECK = 2100、DATAID_AUTO_RESURRECTION_MODE = 2102。

    py tools/dump_lua_globals.py --pid 45724
    py tools/dump_lua_globals.py --pid 45724 --grep RESURRECTION

全量寫進 reports/lua_globals_<pid>.txt，主控台只印過濾結果。
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.memory import MemoryScanner   # noqa: E402
from app.game import locate, lua            # noqa: E402


def u8(sc, a):
    raw = sc._read_bytes(a, 1)
    return raw[0] if raw else None


def u32(sc, a):
    raw = sc._read_bytes(a, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else None


def read_str(sc, ts, limit=256):
    n = u32(sc, ts + lua.OFF_TSTRING_LEN)
    if n is None or not 0 < n < limit:
        return None
    raw = sc._read_bytes(ts + lua.OFF_TSTRING_DATA, n)
    if not raw:
        return None
    try:
        return bytes(raw).decode("ascii")
    except UnicodeDecodeError:
        return None


def globals_table(sc):
    """全域表的 Table*；拿不到回 None。"""
    ctx = u32(sc, lua.CTX_PTR)
    L = u32(sc, ctx + 8) if ctx else None
    if not L:
        return None
    tab = u32(sc, L + lua.OFF_L_GT)
    tt = u32(sc, L + lua.OFF_L_GT + 8)
    return tab if (tt == lua.T_TABLE and tab) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--grep", default="",
                    help="只在主控台印名字含這段（不分大小寫）的；全量都在檔案裡")
    args = ap.parse_args()

    sc = MemoryScanner()
    sc.open(args.pid)
    try:
        locate.warm(sc)                      # 改版位移自動跟上
        tab = globals_table(sc)
        if not tab:
            print("拿不到 Lua 全域表（遊戲還沒進場？）")
            return 1
        lsize = u8(sc, tab + 0x07)           # Table+0x07 = lsizenode
        node = u32(sc, tab + 0x10)           # Table+0x10 = 雜湊節點陣列
        if lsize is None or not node or lsize > 20:
            print(f"表結構不合理（lsizenode={lsize}）")
            return 1
        rows = []
        for i in range(1 << lsize):
            # Node = 32 bytes：值 TValue(16) + 鍵(值 8 + tt 4 + next 4)
            raw = sc._read_bytes(node + i * 32, 32)
            if not raw:
                continue
            if struct.unpack_from("<I", raw, 24)[0] != lua.T_STRING:
                continue
            name = read_str(sc, struct.unpack_from("<I", raw, 16)[0])
            if not name:
                continue
            vtt = struct.unpack_from("<I", raw, 8)[0]
            if vtt == lua.T_NUMBER:
                v = struct.unpack_from("<d", raw, 0)[0]
                if math.isfinite(v):
                    rows.append((name, int(v) if v == int(v) else v))
            elif vtt == lua.T_BOOL:
                rows.append((name, bool(struct.unpack_from("<I", raw, 0)[0])))
        rows.sort()

        out = Path(__file__).resolve().parent.parent / "reports"
        out.mkdir(exist_ok=True)
        out = out / f"lua_globals_{args.pid}.txt"
        with out.open("w", encoding="utf-8") as f:
            for name, v in rows:
                f.write(f"{name} = {v!r}\n")
        print(f"共 {len(rows)} 個數字/布林全域 → {out}")
        if args.grep:
            hits = [r for r in rows if args.grep.upper() in r[0].upper()]
            print(f"名字含「{args.grep}」的 {len(hits)} 個：")
            for name, v in hits:
                print(f"  {name} = {v!r}")
    finally:
        sc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
