"""物品種類 ID → 中文名稱。

    itemname.of(1905)          → '天使之翼'
    itemname.label(4837, 7667) → '高效藍藥水(活動) ×7667'

表從哪來
--------
`assets/item_names.tsv.gz`（74127 筆、632KB），是 `tools/build_item_names.py`
從遊戲資源包 `SETTING/BIG5/STRING/STR_ITEM*.XML` 抽出來的。
⚠ **遊戲改版新增物品要重跑那支**，不然新物品只會顯示編號。

⚠ 這推翻了先前記的「物品 ID→名稱找不到」——那時只找過 .pak（真的加密），
  沒去看使用者放進專案的 SETTING 資源包。

★ **只在第一次要用到時才載入**（約 74000 筆、0.1 秒），之後整支程式共用同一份。
  查不到就回空字串 —— 呼叫端自己決定要不要退回顯示編號，絕不假裝知道。
"""
from __future__ import annotations

import gzip

from app.paths import resource

DATA_FILE = "assets/item_names.tsv.gz"

_names: dict[int, str] | None = None


def _load() -> dict[int, str]:
    global _names
    if _names is None:
        out: dict[int, str] = {}
        try:
            with gzip.open(resource(DATA_FILE), "rt", encoding="utf-8") as f:
                for line in f:
                    tid, _, name = line.rstrip("\n").partition("\t")
                    if name:
                        out[int(tid)] = name
        except Exception:                                  # noqa: BLE001
            out = {}          # 檔案缺了就整個功能退化成顯示編號，不要爆掉
        _names = out
    return _names


def of(type_id: int) -> str:
    """種類 ID 的中文名；查不到回空字串。"""
    return _load().get(int(type_id), "")


def label(type_id: int, count: int | None = None) -> str:
    """給人看的標示：有名字就用名字，沒有就退回「種類 12345」。

        label(4837)       → '高效藍藥水(活動)'
        label(4837, 7667) → '高效藍藥水(活動) ×7667'
        label(99999)      → '種類 99999'
    """
    name = of(type_id) or f"種類 {type_id}"
    return f"{name} ×{count}" if count is not None else name
