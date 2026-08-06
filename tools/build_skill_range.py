"""把每個技能的「射程」抽成小表，給 app/game/skills.py 用。

    py tools\\build_skill_range.py [SETTING資料夾]

為什麼要獨立一張表：`assets/skills.tsv.gz` 只收**有持續時間**的技能（buff），
攻擊技能全都不在裡面 —— 而掛機要知道的正是攻擊技能能打多遠。

★ 射程的單位是「格」。實測（雪狐 82 級、破甲劈擊Ⅳ 射程 1）：
  站 2.0~2.2 格 3 秒打死，站 7 格以上 12 秒 101 次出手零傷害。
  所以歐氏距離的有效值約 = 射程 + 1（斜角相鄰算一格）。

⚠ 遊戲改版調整技能要重跑這支。
"""
from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "assets" / "skill_range.tsv.gz"
ROW = re.compile(r"<法術 ([^>]*)/>")
ATTR = re.compile(r'([\w一-鿿]+)="([^"]*)"')


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    setting = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "SETTING"
    src = None
    for rel in ("base/magic.xml", "BASE/MAGIC.XML"):
        if (setting / rel).is_file():
            src = setting / rel
            break
    if src is None:
        sys.exit("⛔ 找不到 base/magic.xml")

    text = src.read_text(encoding="utf-8", errors="replace")
    rows = []
    for m in ROW.finditer(text):
        a = dict(ATTR.findall(m.group(1)))
        if "編號" not in a:
            continue
        # 對地技能（順移之類）用「範圍」，一般攻擊技能用「射程」
        rng = a.get("射程") or a.get("範圍") or 0
        rows.append((int(a["編號"]), int(float(rng or 0))))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{i}\t{r}\n" for i, r in rows)
    OUT.write_bytes(gzip.compress(body.encode("utf-8"), 9))
    print(f"技能 {len(rows)} 個 → {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    for probe in (737, 734, 743, 257, 261):
        hit = next((r for r in rows if r[0] == probe), None)
        print(f"   驗證 {probe}（{probe:#x}）→ 射程 {hit[1] if hit else '?'}")


if __name__ == "__main__":
    main()
