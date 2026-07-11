"""把設定檔裡存的所有座標，同時套到每個天使之戀分身上，
找出「在所有分身都解得出來」的穩定路徑（多開 = 免費的穩定度測試）。

用法（從專案根目錄）：
    py tools/filter_saved.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import config
from app.core import window as win
from app.core.memory import MemoryScanner, PointerPath, VALUE_TYPES


def main() -> int:
    saved = config.get("memory.saved", []) or []
    if not saved:
        print("設定檔裡沒有存座標。")
        return 1

    # 開啟所有分身
    insts: list[tuple[int, str, MemoryScanner]] = []
    seen: set[int] = set()
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if w.pid in seen:
            continue
        seen.add(w.pid)
        sc = MemoryScanner()
        try:
            sc.open(w.pid)
        except Exception:
            continue
        insts.append((w.pid, w.title, sc))

    if not insts:
        print("找不到任何天使之戀分身。")
        return 1

    pids = [pid for pid, _, _ in insts]
    print(f"存座標 {len(saved)} 條，分身 {len(insts)} 個：{pids}\n")

    rows = []
    for e in saved:
        path = PointerPath.from_dict(e["path"])
        vt = VALUE_TYPES.get(e.get("vt", "int32"), VALUE_TYPES["int32"])
        vals = [sc.read_path(path, vt) for _, _, sc in insts]
        n_ok = sum(1 for v in vals if v is not None)
        rows.append((n_ok, e["name"], path, vals))

    rows.sort(key=lambda r: -r[0])

    print(f"{'通過數':<6}{'名稱':<16}{'各分身的值'}")
    print("-" * 70)
    for n_ok, name, path, vals in rows:
        flag = "★全通" if n_ok == len(insts) else f"{n_ok}/{len(insts)}"
        vals_str = "  ".join("斷" if v is None else str(v) for v in vals)
        print(f"{flag:<7}{name:<16}{vals_str}")

    winners = [r for r in rows if r[0] == len(insts)]
    print("\n" + "=" * 70)
    if winners:
        print(f"✅ 有 {len(winners)} 條在「全部分身」都解得出來 → 這些才是穩定候選：")
        for _, name, path, vals in winners:
            print(f"   {name}: {path.expr()}")
        print("\n下一步：確認這些條的值等於各分身畫面上的技能經驗球，挑一條填進 signatures.py。")
    else:
        print("❌ 沒有任何一條在全部分身都通 → 這 50 條都是巧合路徑。")
        print("   建議：加大『最大層數』(5~6) 或用不同數值重掃，再存全部候選重測。")

    for _, _, sc in insts:
        sc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
