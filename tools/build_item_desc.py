"""把遊戲資源包裡的**物品說明文字**抽成一個小檔，給 app/game/itemdesc.py 用。

    py tools\\build_item_desc.py [GAMEDATA/setting資料夾]

為什麼要這張表（2026-09-06 使用者：「寶石裡面加什麼素質跟敘述都沒寫」「這感覺是 GAMEDATA 會有的」）
----------------------------------------------------------------------------
說明文字在 `GAMEDATA/setting/big5/string/str_item*.xml` 的「文字3」，跟名稱同一批
（`build_item_names.py` 只抽文字1＋2）。寶石的說明長這樣：
    可鑲嵌於已打孔之裝備上，以加強能力。
    武器：雷電攻擊 +24
    防具：靈敏 +10
    盾牌：雷電防禦 +5
    裝備等限：80級
素質那幾行**執行時另外從記憶體算得出來**（holes.gem_effects），這張表是為了顯示遊戲
**原文**（13 顆寶石的說明跟標準句不一樣、其他道具的說明也在）。兩邊對不上以表為準顯示、
另外亮警示（[[table-is-authority]]）。
⚠ **遊戲改版新增物品要重跑這支**，不然新物品沒有說明（介面退回只印記憶體算的那幾行）。
⚠ 資料夾名字叫 BIG5，但檔案內容其實是 **UTF-8**。

編號規則（跟名稱表同一套）
--------------------------
    字串編號 = 1140000000 + 物品種類ID；說明 = 文字3；換行在 XML 裡是 &#xA;
輸出 `assets/item_desc.tsv.gz`：種類ID <TAB> 說明（換行寫成 \\n）
"""
from __future__ import annotations

import gzip
import html
import re
import sys
from pathlib import Path

BASE = 1140000000
LIMIT = 1149999999
ROW = re.compile(r'<表格字串 編號="(\d+)"([^>]*)/>')
ATTR = re.compile(r'(文字3)="([^"]*)"')
OUT = Path(__file__).resolve().parents[1] / "assets" / "item_desc.tsv.gz"


def build(setting_dir: Path) -> dict[int, str]:
    files = sorted((setting_dir / "big5" / "string").glob("str_item*.xml"))
    if not files:
        files = sorted((setting_dir / "BIG5" / "STRING").glob("STR_ITEM*.XML"))
    if not files:
        sys.exit(f"⛔ 找不到 {setting_dir}\\big5\\string\\str_item*.xml")
    descs: dict[int, str] = {}
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        n = 0
        for m in ROW.finditer(text):
            sid = int(m.group(1))
            if not BASE <= sid <= LIMIT:
                continue
            parts = dict(ATTR.findall(m.group(2)))
            desc = html.unescape(parts.get("文字3", "")).replace("\r", "").strip()
            if desc:
                descs[sid - BASE] = desc.replace("\t", " ")
                n += 1
        print(f"  {p.name:<18} {n} 筆")
    return descs


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    setting = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "GAMEDATA" / "setting"
    descs = build(setting)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}\t{v.replace(chr(10), chr(92) + 'n')}\n" for k, v in sorted(descs.items()))
    with gzip.open(OUT, "wt", encoding="utf-8", newline="\n") as f:
        f.write(body)
    print(f"\n共 {len(descs)} 筆 → {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    for probe in (3105, 1905, 1202):
        print(f"   驗證 {probe} → {descs.get(probe, '（沒有）')[:60]!r}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")          # type: ignore[attr-defined]
    except Exception:                                      # noqa: BLE001
        pass
    main()
