"""多開交叉指標掃描：找出「在多個分身都讀到正確值」的穩定指標路徑。

原理：在分身A把某值(如HP)所有位址找出來，對每個反查指標路徑，只保留
「在分身B也剛好讀到B的正確值」的路徑。因為要同時對上兩個不同的值，
巧合路徑幾乎不可能存活 → 直接淘汰所有顯示複本，留下 canonical 的那條。

用法（從專案根目錄）：
    py tools/multiscan.py [型別] 52440:2840 50068:2290 [更多分身:值 ...]
    型別可省略(預設 int32)，或填 int32/int16/int64/float/double。
    第一個分身 = 錨點，其餘 = 驗證分身（值越多越準）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win
from app.core.memory import MemoryScanner, VALUE_TYPES

MAX_TARGETS = 400       # 錨點候選最多處理幾個
MAX_LEVEL = 5
MAX_OFFSET = 0x800
MAX_RESULTS = 800       # 每個候選最多反查幾條
STOP_AFTER = 40         # 找到幾條穩定路徑就停


def main() -> int:
    args = sys.argv[1:]
    vt = VALUE_TYPES["int32"]
    if args and args[0] in VALUE_TYPES:
        vt = VALUE_TYPES[args[0]]
        args = args[1:]
    if len(args) < 2:
        print("用法: py tools/multiscan.py [型別] <錨點pid>:<值> <驗證pid>:<值> [...]")
        return 1
    parse = float if vt.is_float else int
    targets = {}
    order = []
    for a in args:
        pid_s, val_s = a.split(":")
        targets[int(pid_s)] = parse(val_s)
        order.append(int(pid_s))
    anchor_pid = order[0]
    validators = order[1:]

    # 開啟所有需要的分身
    scanners = {}
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if w.pid in targets and w.pid not in scanners:
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception as e:
                print(f"PID {w.pid} 開啟失敗: {e}")
                return 1
            scanners[w.pid] = sc
    for pid in targets:
        if pid not in scanners:
            print(f"找不到 PID {pid} 的視窗，請確認它開著。")
            return 1

    A = scanners[anchor_pid]
    print(f"錨點 = {anchor_pid} (值={targets[anchor_pid]})，"
          f"驗證 = {[(p, targets[p]) for p in validators]}", flush=True)

    # 在錨點找出該值的所有位址
    A.first_scan(vt, "exact", targets[anchor_pid], writable_only=True)
    set_a = [addr for addr, _ in A.results(limit=100000)]
    print(f"錨點 {targets[anchor_pid]} → {len(set_a)} 個候選位址", flush=True)

    # 建立錨點指標表
    print("建立指標表中…（約 10 秒）", flush=True)
    t = time.perf_counter()
    A.build_pointer_map()
    allowed = A.game_module_names()
    print(f"  完成，指標槽 {A._ptr_vals.shape[0]:,}，耗時 {time.perf_counter()-t:.1f}s",
          flush=True)

    keepers = []
    seen_paths = set()
    t0 = time.perf_counter()
    for n, addr_a in enumerate(set_a[:MAX_TARGETS]):
        paths = A.pointer_scan(
            addr_a, max_level=MAX_LEVEL, max_offset=MAX_OFFSET,
            allowed_modules=allowed, max_results=MAX_RESULTS,
        )
        for p in paths:
            # 強篩：每個驗證分身都要讀到「它自己的正確值」
            ok = all(scanners[pid].read_path(p, vt) == targets[pid]
                     for pid in validators)
            if ok:
                key = (p.base_offset, tuple(p.offsets))
                if key not in seen_paths:
                    seen_paths.add(key)
                    allv = {pid: scanners[pid].read_path(p, vt) for pid in order}
                    keepers.append((p, allv))
        if (n + 1) % 25 == 0:
            print(f"  已處理 {n+1}/{min(len(set_a),MAX_TARGETS)} 個候選，"
                  f"找到 {len(keepers)} 條，{time.perf_counter()-t0:.0f}s", flush=True)
        if len(keepers) >= STOP_AFTER:
            break

    print("\n" + "=" * 70)
    if keepers:
        keepers.sort(key=lambda k: (k[0].levels, sum(k[0].offsets)))
        print(f"✅ 找到 {len(keepers)} 條跨分身穩定路徑（各分身值都對）：\n")
        for p, allv in keepers[:15]:
            print(f"  {p.expr()}")
            print(f"      各分身值: {allv}")
    else:
        print("❌ 沒找到。可能錨點候選裡不含 canonical 位址，"
              "或路徑比 5 層更深。可調大 MAX_LEVEL 再試。")

    for sc in scanners.values():
        sc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
