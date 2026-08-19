# -*- coding: utf-8 -*-
"""從 GAMEDATA 抽「補給店販售表」→ assets/supply_shop.json。

    py tools\\build_supply_shop.py

補給店＝shop.xml 裡**唯一賣天使之翼（recall.RECALL_ITEM=1905）**的那家
（目前是編號 35；不寫死，每次抽都現找，找到 0 家或 2 家以上就大聲失敗）。
每個販售物品從 base/item*.xml 抄「重量」「價格」兩欄：

  * 重量 → 掛機的「藥水買到負重 95%」算該買幾顆（supply.run_potion_fill）
  * 價格 → 金幣不夠時封頂，別送一包伺服器整包拒收

為什麼是寫死表（CLAUDE.md 資料來源優先序第 3 級）：NPC 販售清單不在可被動
讀取的記憶體結構（開店時伺服器現送、只存 UI 視窗，見 memory self-supply-buy）；
記憶體的 Item 範本表雖然存在，「重量」欄位的偏移還沒有反組譯出處。
讀取端每輪都拿**實際負重／背包數量**對帳（買了沒進來就大聲停手），
所以表過期只會「買太少＋大聲說」，不會超載或安靜做錯。

改版後重跑本工具，再跑 tools/stamp_tables.py 蓋章（登記於 memory
items-table-maintenance）。
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app.game import itemname, recall    # noqa: E402

SETTING = ROOT / "GAMEDATA" / "setting"
OUT = ROOT / "assets" / "supply_shop.json"
ITEM_FILES = ["item.xml"] + [f"item{i}.xml" for i in range(2, 10)]


def main() -> int:
    shop_xml = io.open(SETTING / "shop.xml", encoding="utf-8").read()

    # 1. 找「唯一賣天使之翼」的商店（＝補給店，藥水雜貨商人開的那家）
    hits = []
    for m in re.finditer(r'<商店 編號="(\d+)">(.*?)</商店>', shop_xml, re.S):
        ids = [int(x) for x in re.findall(r"<item\d+>(\d+)</item\d+>",
                                          m.group(2))]
        if recall.RECALL_ITEM in ids:
            hits.append((int(m.group(1)), ids))
    if len(hits) != 1:
        print(f"⛔ 賣天使之翼({recall.RECALL_ITEM})的商店有 {len(hits)} 家"
              f"（{[s for s, _ in hits]}）——「補給店」認不出來，不出表。")
        return 1
    shop_id, ids = hits[0]

    # 2. item*.xml 抄每個販售物品的 重量 / 價格（屬性缺了就記 0，讀取端會把
    #    重量 0 當「算不了數量」跳過那一組，不會亂買）
    attrs: dict[int, str] = {}
    for fn in ITEM_FILES:
        p = SETTING / "base" / fn
        if not p.exists():
            continue
        for m in re.finditer(r"<道具 ([^>]*?)/?>",
                             io.open(p, encoding="utf-8").read()):
            idm = re.search(r'編號="(\d+)"', m.group(1))
            if idm:
                attrs.setdefault(int(idm.group(1)), m.group(1))

    def num(a: str, key: str) -> int:
        mm = re.search(r'%s="(\d+)"' % key, a)
        return int(mm.group(1)) if mm else 0

    items, missing = {}, []
    for tid in ids:
        a = attrs.get(tid)
        if a is None:
            missing.append(tid)
            continue
        items[str(tid)] = {"w": num(a, "重量"), "p": num(a, "價格")}

    OUT.write_text(json.dumps({
        "shop": shop_id,
        "source": "GAMEDATA/setting/shop.xml + base/item*.xml"
                  "（tools/build_supply_shop.py 自動抽，不手打）",
        "items": items,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"✔ 補給店＝商店 {shop_id}，{len(items)} 樣 → {OUT}")
    for tid in ids:
        it = items.get(str(tid))
        mark = "⚠ item*.xml 查不到" if it is None else \
            f"重量 {it['w']:>3}　價格 {it['p']}"
        print(f"  {tid:>6} {itemname.label(tid):　<14} {mark}")
    if missing:
        print(f"⚠ {len(missing)} 樣在 item*.xml 查不到（沒進表，讀取端會當"
              f"「沒賣」）：{missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
