"""物品種類 ID → 資源包 item.xml 的三個限制旗標（純查表）。

    itemflags.flags(1905)           → 0（天使之翼：三項都沒有）
    itemflags.guild_bankable(tid)   → 能不能放進公會（社團）倉庫
    itemflags.why_not(tid)          → '不可交易' 這種給人看的原因；能放回空字串
    itemflags.icon_of(tid)          → 圖示編號（item.xml「原型介面」；查不到回 0）

表從哪來
--------
`assets/item_flags.tsv.gz`，`tools/build_item_flags.py` 從
`GAMEDATA/setting/base/item*.xml` 抽（每筆道具的 不可存倉庫／不可交易／裝備綁定
三個屬性，第三欄是「原型介面」＝圖示編號，給**不在這台背包**的東西畫圖用——
公會倉庫清單全部分身共用，別台加的東西這台沒有，圖示只能查表）。
⚠ 官方新增道具要重跑那支（跟 item_names 同一份 GAMEDATA）。

規則（使用者 2026-09-06 定）：公會倉庫**綁定／不可交易的也不能存**，加上遊戲本來
就有的「不可存倉庫」→ 三個旗標任一亮就不列、不送。
表裡沒那筆（新道具還沒重跑 build）→ `flags()` 回 None、`guild_bankable()` 回 False
—— 安全退化＝少存一件，絕不把不確定的東西送進去（CLAUDE.md 第 0 條）。
"""
from __future__ import annotations

import gzip

from app.paths import resource

DATA_FILE = "assets/item_flags.tsv.gz"

NO_BANK = 1      # 不可存倉庫
NO_TRADE = 2     # 不可交易
BIND = 4         # 裝備綁定

_NAMES = ((NO_BANK, "不可存倉庫"), (NO_TRADE, "不可交易"), (BIND, "裝備綁定"))

_table: dict[int, tuple[int, int]] | None = None     # 種類 → (旗標位元, 圖示編號)


def _load() -> dict[int, tuple[int, int]]:
    global _table
    if _table is None:
        out: dict[int, tuple[int, int]] = {}
        try:
            with gzip.open(resource(DATA_FILE), "rt", encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 2 and parts[0] and parts[1]:
                        out[int(parts[0])] = (int(parts[1]),
                                              int(parts[2]) if len(parts) >= 3 and parts[2] else 0)
        except Exception:                                  # noqa: BLE001
            out = {}          # 表缺了＝什麼都不能存（安全退化），不要爆掉
        _table = out
    return _table


def flags(type_id: int) -> int | None:
    """旗標位元；表裡沒這筆回 None（⚠ None ≠ 0：沒查到不是沒限制）。"""
    got = _load().get(int(type_id))
    return None if got is None else got[0]


def icon_of(type_id: int) -> int:
    """圖示編號（item.xml「原型介面」）；查不到回 0（呼叫端就不畫圖，不拿別張頂替）。"""
    got = _load().get(int(type_id))
    return 0 if got is None else got[1]


def guild_bankable(type_id: int) -> bool:
    """能不能放進公會倉庫：查得到而且三個旗標都沒亮才 True。"""
    return flags(type_id) == 0


def why_not(type_id: int) -> str:
    """不能放的原因（給清單／提示框看）；能放回空字串。"""
    bits = flags(type_id)
    if bits is None:
        return "不在資料表裡（新道具？重跑 py tools\\build_item_flags.py）"
    return "／".join(name for bit, name in _NAMES if bits & bit)


def count() -> int:
    """表裡有幾筆（診斷用；0 ＝ 表沒載到）。"""
    return len(_load())
