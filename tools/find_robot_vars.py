"""找出「天使精靈設定值」放在記憶體的哪裡，好讓補給判斷不必再呼叫 Lua。

為什麼要找
----------
現在讀一個精靈設定（例如「HP 藥水 1 設成哪個道具」）是這樣做的：

    lua.call(mover, sc, "game.getrobotvar_int", 2121)

那要**請遊戲主執行緒替我們跑 Lua**，一次要三趟跳板往返（取 game 表 → 取欄位
→ pcall），每一趟都要等遊戲的訊息迴圈跑到。讀 10 個設定就是 30 趟，而且整段
是在畫面執行緒上等 —— 慢（零點幾秒的卡頓），而且每一次都要動遊戲的 Lua 堆疊，
是目前風險最高的動作（見 app/game/lua.py 的說明）。

但那些設定**本來就只是記憶體裡的一個陣列**，`getrobotvar_int` 不過是把
DATAID 換算成索引再讀出來而已。只要知道陣列在哪、怎麼算索引，我們就能直接
用 ReadProcessMemory 讀 —— 微秒等級、純讀取、零風險。

這支工具就是去把「陣列在哪、怎麼算」問出來。

怎麼問（純讀記憶體，不注入、不寫入、不掛除錯器）
------------------------------------------------
① 在記憶體裡找 `getrobotvar_int` 這個**字串**（UI 指令表的名字都是明文）。
② 找「哪個 dword 指著那個字串」—— 那就是指令表的一格，**它後面 4 bytes
   就是函式位址**（見記憶 ui-command-table；配對方向別搞反）。
③ 把那個函式的前幾十道指令反組譯出來。我們要找的是這種樣子：

       mov  eax, [ebp+8]              ; 參數 = DATAID
       ...
       mov  eax, [eax*4 + 0x00ABCDEF] ; ← 0x00ABCDEF 就是陣列基底

④ 順便對 `getrobotvar_bool` / `setrobotvar_int` 做同樣的事互相對照。

用法（遊戲已登入進場、精靈的藥水設定已經設好）
----------------------------------------------
    py tools/find_robot_vars.py                 # 只有一個分身就自動找
    py tools/find_robot_vars.py --pid 1234      # 多開時指定
    py tools/find_robot_vars.py --bytes 200     # 想看更長的反組譯

把整段輸出貼回來就行。⚠ 這支**完全不會改到遊戲**，開著掛機跑也不影響。
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import preload                                    # noqa: E402
from app.core.injector import CODE_HI, CODE_LO                  # noqa: E402
from app.core.memory import MemoryScanner                       # noqa: E402

# 要查的指令名字。第一個是主角，其餘拿來對照（同一組 API 通常挨在一起）。
WANTED = [
    "getrobotvar_int",
    "getrobotvar_bool",
    "setrobotvar_int",
    "setrobotvar_bool",
    "setrobotisrun",
]

# 我們實際會用到的設定編號（app/game/robot.py 裡的常數），拿來驗算。
DATAIDS = {
    2121: "HP藥水1 型別", 2131: "HP藥水1 值",
    2122: "HP藥水2 型別", 2132: "HP藥水2 值",
    2123: "HP藥水3 型別", 2133: "HP藥水3 值",
    2124: "MP藥水1 型別", 2134: "MP藥水1 值",
    2125: "MP藥水2 型別", 2135: "MP藥水2 值",
    1502: "裝備損壞回城", 1508: "回城後修理裝備",
}


def _u32(sc, addr):
    raw = sc._read_bytes(addr, 4)
    return struct.unpack("<I", raw)[0] if raw else None


def find_string(sc, text: str) -> list[int]:
    """在記憶體裡找這個 ASCII 字串（含結尾 \\0）的所有位址。"""
    pat = text.encode("ascii") + b"\0"
    out = []
    for base, size in sc._iter_regions(writable_only=False):
        if base > 0xFFFFFFFF:
            continue                       # 32 位元遊戲的東西不會在 4GB 以上
        raw = sc._read_region(base, size)
        if not raw:
            continue
        raw = bytes(raw)          # _read_region 回 memoryview，沒有 .find
        i = raw.find(pat)
        while i >= 0:
            # 前一個 byte 必須不是字母，否則是別的字串的尾巴
            if i == 0 or not chr(raw[i - 1]).isalnum():
                out.append(base + i)
            i = raw.find(pat, i + 1)
    return out


def find_pointers_to(sc, target: int) -> list[int]:
    """找出所有「內容等於 target」的 dword 位址。"""
    want = struct.pack("<I", target)
    out = []
    for base, size in sc._iter_regions(writable_only=False):
        if base > 0xFFFFFFFF:
            continue
        raw = sc._read_region(base, size)
        if not raw:
            continue
        raw = bytes(raw)          # _read_region 回 memoryview，沒有 .find
        i = raw.find(want)
        while i >= 0:
            if (base + i) % 4 == 0:        # 指令表是對齊的
                out.append(base + i)
            i = raw.find(want, i + 1)
    return out


def disasm(sc, addr: int, n: int) -> list[str]:
    try:
        import capstone
    except ImportError:
        return ["（沒裝 capstone，無法反組譯：py -m pip install capstone）"]
    raw = sc._read_bytes(addr, n)
    if not raw:
        return [f"（讀不到 {addr:#010x} 的程式碼）"]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    lines, bases = [], []
    for ins in md.disasm(raw, addr):
        lines.append(f"    {ins.address:08X}  {ins.mnemonic:7} {ins.op_str}")
        # 抓「用索引去查表」的樣子：[reg*4 + 立即值] 或 [reg + 立即值]
        for op in ins.operands:
            if op.type == capstone.x86.X86_OP_MEM and op.mem.disp:
                d = op.mem.disp & 0xFFFFFFFF
                if 0x400000 <= d < 0xC00000 and op.mem.scale in (1, 4):
                    bases.append((ins.address, d, op.mem.scale))
        if ins.mnemonic in ("ret", "retn"):
            break
    return lines, bases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--bytes", type=int, default=120,
                    help="每個函式反組譯幾 bytes（預設 120）")
    args = ap.parse_args()

    wins = preload.windows()
    if not wins:
        print("找不到遊戲視窗 —— 遊戲開著、而且已經登入進場了嗎？")
        return 1
    pid = args.pid or wins[0].pid
    print(f"分身 PID {pid}（{wins[0].title if not args.pid else ''}）")
    print("=" * 70)

    sc = MemoryScanner()
    sc.open(pid)
    try:
        all_bases = []
        for name in WANTED:
            print(f"\n■ {name}")
            hits = find_string(sc, name)
            if not hits:
                print("   找不到這個字串（改版改名了？）")
                continue
            print(f"   字串位址：{', '.join(hex(h) for h in hits)}")

            entries = []
            for h in hits:
                for p in find_pointers_to(sc, h):
                    fn = _u32(sc, p + 4)
                    if fn and CODE_LO <= fn < CODE_HI:
                        entries.append((p, fn))
            if not entries:
                print("   ⚠ 沒找到指令表的那一格（沒有 dword 指著它 + 後面是函式）")
                continue
            for p, fn in entries:
                print(f"   指令表格子 {p:#010x} → 函式 {fn:#010x}")
            fn = entries[0][1]
            lines, bases = disasm(sc, fn, args.bytes)
            print(f"   反組譯 {fn:#010x}：")
            for ln in lines:
                print(ln)
            for at, d, scale in bases:
                tag = "★ 像是陣列基底" if scale == 4 else "  （偏移）"
                print(f"   {tag}：{d:#010x}（在 {at:#010x}，scale={scale}）")
                if scale == 4:
                    all_bases.append(d)

        # --- 用已知的設定編號驗算 ---
        if all_bases:
            print("\n" + "=" * 70)
            print("拿候選基底去讀我們實際要用的那幾個設定（看數字合不合理）：")
            for b in dict.fromkeys(all_bases):
                print(f"\n基底 {b:#010x}")
                for did, what in DATAIDS.items():
                    v = _u32(sc, b + did * 4)
                    print(f"   [{b:#x} + {did}*4] = {v!r:>12}   {what}")
        else:
            print("\n沒有從反組譯裡認出陣列基底 —— 把上面的反組譯貼回來，我來看。")
    finally:
        sc.close()
    print("\n完成。把整段輸出貼回來即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
