"""物品種類 ID → 遊戲的說明文字（提示框裡那幾行原文）。

    itemdesc.of(3105)   → '可鑲嵌於已打孔之裝備上，以加強能力。\\n武器：雷電攻擊 +24\\n…'
    itemdesc.lines(3105) → 一行一項的清單；查不到回空清單

表從哪來
--------
`assets/item_desc.tsv.gz`，是 `tools/build_item_desc.py` 從 `GAMEDATA/setting/big5/string/
str_item*.xml` 的「文字3」抽出來的（跟名稱表 itemname.py 同一批檔、同一套編號規則）。
⚠ **遊戲改版新增物品要重跑那支**，不然新物品沒有說明。
★ 使用者 2026-09-06 要的是遊戲**原文**；素質數字另外由 holes.gem_effects 讀記憶體算，
  兩邊對不上以表為準顯示、另外亮警示（[[table-is-authority]]）。
★ 只在第一次要用到時才載入，之後整支程式共用；查不到回空，絕不假裝知道。
"""
from __future__ import annotations

import gzip

from app.paths import resource

DATA_FILE = "assets/item_desc.tsv.gz"

_descs: dict[int, str] | None = None


def _load() -> dict[int, str]:
    global _descs
    if _descs is None:
        out: dict[int, str] = {}
        try:
            with gzip.open(resource(DATA_FILE), "rt", encoding="utf-8") as f:
                for line in f:
                    tid, _, desc = line.rstrip("\n").partition("\t")
                    if desc:
                        out[int(tid)] = desc.replace("\\n", "\n")
        except Exception:                                  # noqa: BLE001
            out = {}          # 檔案缺了就整個功能退化成沒有說明，不要爆掉
        _descs = out
    return _descs


def of(type_id: int) -> str:
    """種類 ID 的說明原文（多行用 \\n 分隔）；查不到回空字串。"""
    return _load().get(int(type_id), "")


def lines(type_id: int) -> list[str]:
    """說明原文拆成一行一項（空行去掉）；查不到回空清單。"""
    return [ln.strip() for ln in of(type_id).split("\n") if ln.strip()]


def count() -> int:
    """表裡有幾筆（診斷用；0 ＝ 表沒載到）。"""
    return len(_load())
