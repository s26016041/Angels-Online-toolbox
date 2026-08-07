"""用多開自動生成 AOB 特徵碼：比對兩個分身「HP 周圍的位元組」，
相同的當結構(固定)、不同的當萬用字元(??)，做出一段能定位 HP 的特徵。

純讀，不附加偵錯器、不改遊戲記憶體。

用法（從專案根目錄）：
    py tools/build_signature.py float 52440:2840 50068:2290
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win
from app.core.memory import MemoryScanner, VALUE_TYPES

PRE = 0x50   # HP 前面取多少位元組
POST = 0x50  # HP 後面取多少位元組


def read_ctx(sc, addr):
    return sc._read_bytes(addr - PRE, PRE + POST)


def masked_search(sc, sig, mask, anchor_off, anchor_len):
    """在可寫入區段找出符合 masked 特徵的位址（回傳每個匹配的「HP 位址」）。"""
    anchor = bytes(sig[anchor_off:anchor_off + anchor_len])
    hits = []
    for base, size in sc._iter_regions(writable_only=True):
        raw = sc._read_region(base, size)
        if not raw:
            continue
        raw = bytes(raw)          # _read_region 回 memoryview，沒有 .find
        s = 0
        while True:
            i = raw.find(anchor, s)
            if i < 0:
                break
            start = i - anchor_off
            if start >= 0 and start + len(sig) <= len(raw):
                seg = raw[start:start + len(sig)]
                if all(m == 0 or seg[k] == sig[k] for k, m in enumerate(mask)):
                    hits.append(base + start + PRE)  # HP 位址
            s = i + 1
    return hits


def main() -> int:
    args = sys.argv[1:]
    vt = VALUE_TYPES["int32"]
    if args and args[0] in VALUE_TYPES:
        vt = VALUE_TYPES[args[0]]
        args = args[1:]
    parse = float if vt.is_float else int
    order, want = [], {}
    for a in args:
        p, v = a.split(":")
        order.append(int(p)); want[int(p)] = parse(v)
    A_pid, B_pid = order[0], order[1]

    scs = {}
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if w.pid in want and w.pid not in scs:
            sc = MemoryScanner(); sc.open(w.pid); scs[w.pid] = sc
    A, B = scs[A_pid], scs[B_pid]

    A.first_scan(vt, "exact", want[A_pid], writable_only=True)
    addrs_a = [a for a, _ in A.results(100000)]
    B.first_scan(vt, "exact", want[B_pid], writable_only=True)
    addrs_b = [a for a, _ in B.results(100000)]
    print(f"A({A_pid}) {want[A_pid]} → {len(addrs_a)} 個；B({B_pid}) {want[B_pid]} → {len(addrs_b)} 個",
          flush=True)

    # 找「周圍結構最像」的一對(=最穩定的那個複本)
    best = None
    for aa in addrs_a:
        ca = read_ctx(A, aa)
        if ca is None:
            continue
        for bb in addrs_b:
            cb = read_ctx(B, bb)
            if cb is None:
                continue
            # 結構分數：相同且非零、且不在 HP 4 bytes 區的位元組數
            score = sum(
                1 for k in range(len(ca))
                if ca[k] == cb[k] and ca[k] != 0 and not (PRE <= k < PRE + vt.size)
            )
            if best is None or score > best[0]:
                best = (score, aa, bb, ca, cb)

    score, aa, bb, ca, cb = best
    print(f"最佳配對: A@0x{aa:X}  B@0x{bb:X}  結構相同位元組數={score}", flush=True)

    # 生成特徵：相同→固定值；不同或HP區→萬用字元(mask 0)
    sig = bytearray(len(ca))
    mask = bytearray(len(ca))
    for k in range(len(ca)):
        if PRE <= k < PRE + vt.size:  # HP 本身 → 萬用
            mask[k] = 0
        elif ca[k] == cb[k]:
            sig[k] = ca[k]; mask[k] = 1
        else:
            mask[k] = 0

    # 找最長的連續固定位元組當搜尋錨點
    best_run = (0, 0)
    run_start = None
    for k in range(len(mask) + 1):
        if k < len(mask) and mask[k]:
            if run_start is None:
                run_start = k
        else:
            if run_start is not None:
                if k - run_start > best_run[1]:
                    best_run = (run_start, k - run_start)
                run_start = None
    anchor_off, anchor_len = best_run
    fixed = sum(mask)
    print(f"特徵長度 {len(sig)} bytes，固定 {fixed} 個，"
          f"最長連續固定={anchor_len}(位於 offset {anchor_off})", flush=True)

    if anchor_len < 6:
        print("⚠ 連續固定位元組太少，特徵可能不夠獨特/穩定。")

    # 驗證：在 A 和 B 各掃這個特徵，是否只定位到少數(=HP)位址
    hits_a = masked_search(A, sig, mask, anchor_off, anchor_len)
    hits_b = masked_search(B, sig, mask, anchor_off, anchor_len)
    va = [A.read_value(h, vt) for h in hits_a]
    vb = [B.read_value(h, vt) for h in hits_b]
    print(f"\n用特徵掃描：A 命中 {len(hits_a)} 處，讀到值={va}", flush=True)
    print(f"           B 命中 {len(hits_b)} 處，讀到值={vb}", flush=True)

    ok_a = want[A_pid] in va
    ok_b = want[B_pid] in vb
    def sig_str():
        return " ".join("??" if mask[k] == 0 else f"{sig[k]:02X}" for k in range(len(sig)))
    print("\n" + "=" * 60)
    if ok_a and ok_b and len(hits_a) <= 4 and len(hits_b) <= 4:
        print("✅ 特徵有效！兩個分身都用它定位到正確 HP。")
        print(f"   特徵→HP 偏移 = +0x{PRE:X}")
        print(f"   AOB 特徵：\n   {sig_str()}")
        print("\n→ 每次啟動：掃這段特徵 + 0x%X = HP 位址（純讀，免指標路徑）。" % PRE)
    else:
        print("❌ 特徵不夠獨特或不穩(A_ok=%s B_ok=%s)。可調大 PRE/POST 或換 HP 複本再試。"
              % (ok_a, ok_b))

    for sc in scs.values():
        sc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
