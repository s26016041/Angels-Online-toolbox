"""把遊戲資源包裡的「天使趴趴GO」傳送表抽成小檔，給 app/game/jumpmap.py 用。

    py tools\\build_jumpmap.py [SETTING資料夾]

來源（`SYSTEM_GLOBAL.XML` 裡宣告的）：
    setting/base/JumpMap.xml                 跳地圖編號、場景編號、傳送座標、類別
    setting/big5/string/str_jumpmap.xml      名稱（編號 = 1160000000 + 跳地圖編號）
    setting/big5/string/str_jumpmapclass.xml 類別名（編號 = 1170000000 + 類別編號）

輸出兩個檔：
    assets/jumpmap.tsv        編號 場景 X Y 類別(逗號分隔) 名稱
    assets/jumpmap_class.tsv  類別編號 類別名

⚠ 每個傳送點最多帶三個類別標籤（`類別1`/`類別2`/`類別3`），那**不是嚴格的
  三層樹**：同一個點會同時掛在好幾個類別底下（遊戲的清單就是這樣分頁的）。
  所以這裡把三格合成一個集合，過濾時「任一格命中就算」。
⚠ 遊戲改版增減傳送點要重跑這支。
⚠ 資料夾叫 big5，內容其實是 **UTF-8**。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

NAME_BASE = 1160000000
CLASS_BASE = 1170000000
OUT = Path(__file__).resolve().parents[1] / "assets" / "jumpmap.tsv"
OUT_CLASS = Path(__file__).resolve().parents[1] / "assets" / "jumpmap_class.tsv"
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

    def strings(path: Path, base: int) -> dict[int, str]:
        out: dict[int, str] = {}
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in STR_ROW.finditer(text):
            sid = int(m.group(1))
            if base <= sid < base + 1_000_000:
                d = dict(TEXT.findall(m.group(2)))
                nm = (d.get("文字1", "") + d.get("文字2", "")).strip()
                if nm:
                    out[sid - base] = nm
        return out

    names = strings(find("big5/string/str_jumpmap.xml",
                         "BIG5/STRING/STR_JUMPMAP.XML"), NAME_BASE)
    cls_names = strings(find("big5/string/str_jumpmapclass.xml",
                             "BIG5/STRING/STR_JUMPMAPCLASS.XML"), CLASS_BASE)

    rows = []
    text = find("base/JumpMap.xml", "BASE/JumpMap.xml").read_text(
        encoding="utf-8", errors="replace")
    for m in ROW.finditer(text):
        a = dict(ATTR.findall(m.group(1)))
        if "編號" not in a:
            continue
        jid = int(a["編號"])
        cats = []
        for k in ("類別1", "類別2", "類別3"):
            v = a.get(k, "")
            if v.isdigit() and int(v) and int(v) not in cats:
                cats.append(int(v))
        rows.append((jid, int(a.get("場景編號", 0)),
                     int(a.get("傳送座標X", 0)), int(a.get("傳送座標Y", 0)),
                     ",".join(str(c) for c in cats), names.get(jid, "")))

    # 只留**真的有傳送點掛在底下**的類別 —— 表裡宣告了卻沒人用的類別
    # 放進下拉只會讓使用者選到空清單。
    used = sorted({c for r in rows for c in
                   (int(x) for x in r[4].split(",") if x)})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "".join(f"{j}\t{s}\t{x}\t{y}\t{c}\t{n}\n" for j, s, x, y, c, n in rows),
        encoding="utf-8", newline="\n")
    OUT_CLASS.write_text(
        "".join(f"{c}\t{cls_names.get(c, '')}\n" for c in used),
        encoding="utf-8", newline="\n")
    print(f"共 {len(rows)} 筆 → {OUT}（{OUT.stat().st_size / 1024:.1f} KB）")
    print(f"類別 {len(used)} 個 → {OUT_CLASS}")
    missing = [c for c in used if not cls_names.get(c)]
    if missing:
        print(f"   ⚠ 這幾個類別查不到名字（會顯示成編號）：{missing}")
    for probe in (13, 86, 101, 119):
        hit = next((r for r in rows if r[0] == probe), None)
        print(f"   驗證 {probe} → {hit}")
    print("   類別：" + "、".join(
        f"{c}={cls_names.get(c, '?')}" for c in used))


if __name__ == "__main__":
    main()
