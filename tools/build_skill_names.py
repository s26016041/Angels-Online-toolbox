"""把遊戲資源包的法術名稱抽成小檔，給 app/game/skills.py 的 name_of() 用。

    py tools\\build_skill_names.py [GAMEDATA/setting資料夾]

來源：`setting/big5/string/str_magic.xml` —— 名稱**不在** magic.xml 本體，
在字串表：`表格字串 編號="1180000000+法術編號" 文字1="名稱"`
（跟場景名的 129xxxxxxx、物品名的 STR_ITEM*.XML 同一套規則）。

驗證錨點：257=電擊術Ⅳ（黑狐 F2）、743=幻影刺殺Ⅳ（雪狐 F1）、
5424=單體分身Ⅳ（自動分身那顆 buff，持續 20 分鐘）。

⚠ 遊戲改版新增技能要重跑這支。
"""
from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "assets" / "skill_names.tsv.gz"
ROW = re.compile(r'<表格字串 編號="118(\d{7})" 文字1="([^"]*)"')


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    setting = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "GAMEDATA" / "setting"
    src = None
    for rel in ("big5/string/str_magic.xml", "BIG5/STRING/STR_MAGIC.XML"):
        if (setting / rel).is_file():
            src = setting / rel
            break
    if src is None:
        sys.exit("⛔ 找不到 big5/string/str_magic.xml")

    text = src.read_text(encoding="utf-8", errors="replace")
    rows = [(int(m.group(1)), m.group(2))
            for m in ROW.finditer(text) if m.group(2)]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{i}\t{n}\n" for i, n in rows)
    OUT.write_bytes(gzip.compress(body.encode("utf-8"), 9))
    print(f"法術名稱 {len(rows)} 筆 → {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    got = dict(rows)
    for probe in (257, 743, 5424):
        print(f"   驗證 {probe} → {got.get(probe)}")


if __name__ == "__main__":
    main()
