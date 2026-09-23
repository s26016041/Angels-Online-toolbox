"""從資源包抽「傢俱魔力值」表 → assets/furniture.tsv.gz。

    py tools\build_furniture.py          # 專案根目錄那份 GAMEDATA
    py tools\build_furniture.py --check  # 只比對，不寫檔（改版體檢用）

兩種列（第一欄是列種）：
    F<TAB>物品種類ID<TAB>基礎魔力值<TAB>傢俱編號
        item*.xml 裡 `物品類別="傢俱"` 的道具；`動態資料1` ＝ furniture.xml 的傢俱編號，
        基礎魔力值 ＝ 那一列的 `擺設分數`。
    H<TAB>物品種類ID<TAB>最低<TAB>最高
        item*.xml 裡 `物品類別="魔力骰"` 的道具（傢俱魔力錘）；`動態資料1`／`動態資料2`
        ＝ 重擲加成值的下限／上限（跟物品說明「必定給予 1 以上，最高至 35」逐一對過）。

出處（2026-09-23 反組譯提示框 `0x5F34CE`）：
    * 範本 +0x18 == 0x3A（傢俱）才印魔力值那行；
    * `0x51AE20(範本 +0x108 動態資料1)` 拿傢俱列，列 +8 ＝ 基礎值 ——
      當場讀 [0x9D70B0]+編號*4 的列 +8：1→65、5→46、19→18、190→164、2609→700，
      跟 furniture.xml 的擺設分數全部一致；
    * 字串 2537「魔力值：%d(%d+%d)」＝ 基礎＋物品 +0xA0（u16 加成）。
furniture.xml 沒有那一列（或沒擺設分數）的傢俱**不寫進表**——讀取端就不列它（安全退化）。

改版：官方新增傢俱／魔力錘要重跑，見 .claude/commands/_patchCheck.md 第 7 步與
memory items-table-maintenance。
"""
from __future__ import annotations

import gzip
import io
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "GAMEDATA" / "setting" / "base"
OUT = ROOT / "assets" / "furniture.tsv.gz"
ITEM_FILES = ["item.xml"] + [f"item{i}.xml" for i in range(2, 10)]


def _attr(a: str, key: str) -> str | None:
    m = re.search(r'%s="([^"]*)"' % key, a)
    return m.group(1) if m else None


def extract() -> list[tuple]:
    scores: dict[int, int] = {}
    text = io.open(BASE / "furniture.xml", encoding="utf-8").read()
    for m in re.finditer(r"<furniture ([^>]*?)/?>", text):
        fid, score = _attr(m.group(1), "編號"), _attr(m.group(1), "擺設分數")
        if fid and score and fid.isdigit() and score.isdigit():
            scores.setdefault(int(fid), int(score))
    rows: list[tuple] = []
    seen: set[int] = set()
    for fn in ITEM_FILES:
        p = BASE / fn
        if not p.exists():
            continue
        for m in re.finditer(r"<道具 ([^>]*?)/?>", io.open(p, encoding="utf-8").read()):
            a = m.group(1)
            tid = _attr(a, "編號")
            if not tid or not tid.isdigit() or int(tid) in seen:
                continue                     # 同編號後面的檔不蓋前面的（跟 build_item_flags 同規矩）
            seen.add(int(tid))
            kind = _attr(a, "物品類別")
            d1, d2 = _attr(a, "動態資料1"), _attr(a, "動態資料2")
            if kind == "傢俱" and d1 and d1.isdigit() and int(d1) in scores:
                rows.append(("F", int(tid), scores[int(d1)], int(d1)))
            elif kind == "魔力骰" and d1 and d2 and d1.isdigit() and d2.isdigit():
                rows.append(("H", int(tid), int(d1), int(d2)))
    return sorted(rows)


def load_existing() -> list[tuple]:
    if not OUT.exists():
        return []
    out = []
    with gzip.open(OUT, "rt", encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) == 4:
                out.append((p[0], int(p[1]), int(p[2]), int(p[3])))
    return sorted(out)


def main() -> int:
    check_only = "--check" in sys.argv
    if not (BASE / "furniture.xml").exists():
        print(f"⛔ 找不到 {BASE / 'furniture.xml'} —— 先把 GAMEDATA 解包放到專案根目錄")
        return 2
    rows = extract()
    n_f = sum(1 for r in rows if r[0] == "F")
    n_h = sum(1 for r in rows if r[0] == "H")
    if not n_f or not n_h:
        print(f"⛔ 傢俱 {n_f} 筆、魔力錘 {n_h} 筆 —— 格式變了？不出表")
        return 1
    print(f"傢俱 {n_f} 筆、魔力錘 {n_h} 種")
    old = load_existing()
    if old:
        added, gone = set(rows) - set(old), set(old) - set(rows)
        print(f"跟現有表比：新增／變動 {len(added)}、消失／舊值 {len(gone)}")
        if check_only:
            return 0 if not (added or gone) else 1
    elif check_only:
        print("現有表不存在")
        return 1
    with gzip.open(OUT, "wt", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write("\t".join(map(str, r)) + "\n")
    print(f"✔ 寫出 {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
