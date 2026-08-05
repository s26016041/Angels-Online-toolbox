"""把遊戲資源包裡的物品名稱抽成一個小檔，給 app/game/itemname.py 用。

    py tools\\build_item_names.py [SETTING資料夾]

為什麼要先抽出來
----------------
名稱在 `SETTING/BIG5/STRING/STR_ITEM*.XML`，七個檔加起來 **14MB** ——
直接打包進 exe 太肥、每次啟動再解析也慢。這支把它壓成一個 gzip 的 TSV
（約 300KB），放在 `assets/`，spec 已經整包收 assets 了。

⚠ **遊戲改版新增物品時要重跑這支**，不然新物品會顯示成編號。
⚠ 資料夾名字叫 BIG5，但檔案內容其實是 **UTF-8**（照著檔名用 big5 解會全部變亂碼）。

編號規則
--------
    字串編號 = 1140000000 + 物品種類ID
    名稱 = 文字1 + 文字2（文字3 是說明，不要）
例：`<表格字串 編號="1140004837" 文字1="高效" 文字2="藍藥水(活動)" 文字3="…"/>`
    → 4837 = 高效藍藥水(活動)
"""
from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

BASE = 1140000000
LIMIT = 1149999999
ROW = re.compile(r'<表格字串 編號="(\d+)"([^>]*)/>')
ATTR = re.compile(r'(文字[12])="([^"]*)"')
OUT = Path(__file__).resolve().parents[1] / "assets" / "item_names.tsv.gz"


def build(setting_dir: Path) -> dict[int, str]:
    files = sorted((setting_dir / "BIG5" / "STRING").glob("STR_ITEM*.XML"))
    if not files:
        sys.exit(f"⛔ 找不到 {setting_dir}\\BIG5\\STRING\\STR_ITEM*.XML")
    names: dict[int, str] = {}
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        n = 0
        for m in ROW.finditer(text):
            sid = int(m.group(1))
            if not BASE <= sid <= LIMIT:
                continue
            parts = dict(ATTR.findall(m.group(2)))
            name = (parts.get("文字1", "") + parts.get("文字2", "")).strip()
            if name:
                names[sid - BASE] = name
                n += 1
        print(f"  {p.name:<18} {n} 筆")
    return names


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    setting = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "SETTING"
    names = build(setting)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}\t{v}\n" for k, v in sorted(names.items()))
    with gzip.open(OUT, "wt", encoding="utf-8", newline="\n") as f:
        f.write(body)
    print(f"\n共 {len(names)} 筆 → {OUT}"
          f"（{OUT.stat().st_size / 1024:.0f} KB）")
    for probe in (1905, 4836, 4837, 25832):
        print(f"   驗證 {probe} → {names.get(probe, '（沒有）')}")


if __name__ == "__main__":
    main()
