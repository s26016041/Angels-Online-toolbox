"""找「某個結構偏移」在遊戲程式碼裡的使用點，並把該段的特徵位元組印出來。

    py tools\\find_off.py 0x31A0              # 找所有 [reg+0x31A0]
    py tools\\find_off.py 0x31A0 --ctx 0x30   # 每個命中前後多印一點
    py tools\\find_off.py --at 0x5b76c4 0x40  # 直接把某位址那段印成 pattern

為什麼要有它：2026-08-11 改版把「目前選定的目標」從 +0x2D8 搬到 +0x270，
寫舊偏移**不會有任何錯誤**（遊戲只是安靜地什麼都不做），整個掛機廢掉半天。
結論是結構偏移也得跟位址一樣進 `locate.SIGS` 自動定位 —— 而做那件事的
第一步，就是找到「遊戲自己用這個偏移」的那幾行當錨。

⚠ 純讀磁碟上的 angel.dat，不碰遊戲行程。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import capstone
import pefile

GAME = Path(r"D:/AngelsOnline/Angels Online Global/angel.dat")


def load():
    pe = pefile.PE(str(GAME), fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    sec = next(s for s in pe.sections if s.Name.rstrip(b"\0") == b".text")
    return base + sec.VirtualAddress, sec.get_data()


def disasm(txt_va, txt, va, size):
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    off = va - txt_va
    return list(md.disasm(txt[off:off + size], va))


def as_pattern(txt_va, txt, va, size) -> str:
    """那一段的位元組（16 進位，空白分隔）—— 貼進 locate.SIGS 用。"""
    off = va - txt_va
    return " ".join(f"{b:02X}" for b in txt[off:off + size])


def main() -> int:
    txt_va, txt = load()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1

    if args[0] == "--at":
        va = int(args[1], 16)
        size = int(args[2], 16) if len(args) > 2 else 0x30
        print(f"=== {va:#x} 起 {size:#x} bytes ===")
        for i in disasm(txt_va, txt, va, size):
            print(f"  {i.address:#010x}  {i.mnemonic} {i.op_str}")
        print("\npattern:")
        print(f'    "{as_pattern(txt_va, txt, va, size)}"')
        return 0

    want = int(args[0], 16)
    ctx = int(args[args.index("--ctx") + 1], 16) if "--ctx" in args else 0x20
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    needle = want.to_bytes(4, "little")
    hits = []
    for m in re.finditer(re.escape(needle), txt):
        o = m.start()
        for back in range(2, 9):           # 指令開頭往前找
            st = o - back
            if st < 0:
                continue
            try:
                ins = next(md.disasm(txt[st:st + 16], txt_va + st))
            except StopIteration:
                continue
            if ins.size == back + 4 and hex(want) in ins.op_str.replace(
                    "0x", "0x").lower():
                hits.append((txt_va + st, ins))
                break
    print(f"=== [reg+{want:#x}] 的使用點：{len(hits)} 處 ===")
    for va, ins in hits:
        kind = "寫" if (ins.mnemonic == "mov"
                       and ins.op_str.startswith("dword ptr [")) else "讀"
        print(f"  {va:#010x}  ({kind}) {ins.mnemonic} {ins.op_str}")
    if "--ctx" in args:
        for va, _ins in hits:
            print(f"\n--- {va:#x} 前後 ---")
            for i in disasm(txt_va, txt, va - ctx, ctx * 2):
                mark = "  <<<" if i.address == va else ""
                print(f"  {i.address:#010x}  {i.mnemonic} {i.op_str}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
