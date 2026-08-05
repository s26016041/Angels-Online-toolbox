"""把遊戲資源包裡的「天使趴趴GO」傳送表抽成小檔，給 app/game/jumpmap.py 用。

    py tools\\build_jumpmap.py [SETTING資料夾]

來源（`SYSTEM_GLOBAL.XML` 裡宣告的）：
    setting/base/JumpMap.xml            跳地圖編號、場景編號、傳送座標、類別
    setting/big5/string/str_jumpmap.xml 名稱（編號 = 1160000000 + 跳地圖編號）

⚠ 遊戲改版增減傳送點要重跑這支。
⚠ 資料夾叫 big5，內容其實是 **UTF-8**。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

NAME_BASE = 1160000000
OUT = Path(__file__).resolve().parents[1] / "assets" / "jumpmap.tsv"
ROW = re.compile(r"<跳地圖 ([^>]*)/>")
ATTR = re.compile(r'(\w+)="([^"]*)"')
STR_ROW = re.compile(r'<表格字串 編號="(\d+)"([^>]*)/>')
TEXT = re.compile(r'(文字\d)="([^"]*)"')


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    setting = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "SETTING"

    def find(*rel: str) -> Path:
        for r in rel:                       # 資料夾大小寫在不同版本不一致
            p = setting / r
            if p.is_file():
                return p
        sys.exit(f"⛔ 找不到 {rel[0]}")

    names: dict[int, str] = {}
    text = find("big5/string/str_jumpmap.xml",
                "BIG5/STRING/STR_JUMPMAP.XML").read_text(
                    encoding="utf-8", errors="replace")
    for m in STR_ROW.finditer(text):
        sid = int(m.group(1))
        if NAME_BASE <= sid < NAME_BASE + 1_000_000:
            d = dict(TEXT.findall(m.group(2)))
            nm = (d.get("文字1", "") + d.get("文字2", "")).strip()
            if nm:
                names[sid - NAME_BASE] = nm

    rows = []
    text = find("base/JumpMap.xml", "BASE/JumpMap.xml").read_text(
        encoding="utf-8", errors="replace")
    for m in ROW.finditer(text):
        a = dict(ATTR.findall(m.group(1)))
        if "編號" not in a:
            continue
        jid = int(a["編號"])
        rows.append((jid, int(a.get("場景編號", 0)),
                     int(a.get("傳送座標X", 0)), int(a.get("傳送座標Y", 0)),
                     names.get(jid, "")))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "".join(f"{j}\t{s}\t{x}\t{y}\t{n}\n" for j, s, x, y, n in rows),
        encoding="utf-8", newline="\n")
    print(f"共 {len(rows)} 筆 → {OUT}（{OUT.stat().st_size / 1024:.1f} KB）")
    for probe in (13, 86, 101, 119):
        hit = next((r for r in rows if r[0] == probe), None)
        print(f"   驗證 {probe} → {hit}")


if __name__ == "__main__":
    main()
