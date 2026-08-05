"""把遊戲資源包的法術表抽成小檔，給 app/game/skills.py 用。

    py tools\\build_skills.py [SETTING資料夾]

來源：`setting/base/magic.xml`（3.5 MB、19312 個法術）
只留自動補 buff 用得到的欄位，壓成幾十 KB。

★ **持續時間的單位是「秒」**（使用者實測：F12 的技能 5424 寫 1200，
  實際持續 20 分鐘 = 1200 秒）。
⚠ 同一列裡的 `前置時間`／`後置時間` 卻是**毫秒**（600 = 施法前搖 0.6 秒），
  同一張表混用兩種單位，不要看到數字就當同一種。

⚠ 遊戲改版調整技能要重跑這支。
"""
from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "assets" / "skills.tsv.gz"
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
        dur = int(float(a.get("持續時間", 0) or 0))
        if dur <= 0:
            continue                        # 沒有持續時間 = 不是 buff，不用留
        # ⚠ 消耗MP 有小數（例如 2845.4），不能直接 int()
        rows.append((int(a["編號"]), dur, a.get("對象", ""),
                     int(float(a.get("射程", 0) or 0)),
                     round(float(a.get("消耗MP", 0) or 0))))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{i}\t{d}\t{t}\t{r}\t{mp}\n" for i, d, t, r, mp in rows)
    OUT.write_bytes(gzip.compress(body.encode("utf-8"), 9))
    print(f"有持續時間的法術 {len(rows)} 個 → {OUT}"
          f"（{OUT.stat().st_size / 1024:.0f} KB）")
    for probe in (163, 261, 253, 254, 5424):
        hit = next((r for r in rows if r[0] == probe), None)
        print(f"   驗證 {probe} → {hit}")


if __name__ == "__main__":
    main()
