"""物品種類 ID → 資源包 item.xml 的三個限制旗標（純查表）。

    itemflags.flags(1905)           → 0（天使之翼：三項都沒有）
    itemflags.guild_bankable(tid)   → **表**有沒有擋這一類（⚠ 不是完整答案）
    itemflags.why_not(tid)          → '不可交易' 這種給人看的原因；表沒擋回空字串
    itemflags.icon_of(tid)          → 圖示編號（item.xml「原型介面」；查不到回 0）

表從哪來
--------
`assets/item_flags.tsv.gz`，`tools/build_item_flags.py` 從
`GAMEDATA/setting/base/item*.xml` 抽（每筆道具的 不可存倉庫／不可交易／裝備綁定
三個屬性，第三欄是「原型介面」＝圖示編號，給**不在這台背包**的東西畫圖用——
公會倉庫清單全部分身共用，別台加的東西這台沒有，圖示只能查表）。
⚠ 官方新增道具要重跑那支（跟 item_names 同一份 GAMEDATA）。

規則（使用者 2026-09-06 定、2026-09-16 修正）：公會倉庫**不可交易的不能存**，
加上遊戲本來就有的「不可存倉庫」→ 這兩個任一亮就不列、不送。
表裡沒那筆（新道具還沒重跑 build）→ `flags()` 回 None、`guild_bankable()` 回 False
—— 安全退化＝少存一件，絕不把不確定的東西送進去（CLAUDE.md 第 0 條）。

⚠⚠ **「裝備綁定」旗標不在擋的行列**（2026-09-16 修）。它只說「這類東西會綁定」，
  能不能交易要看**這一件**的剩餘綁定次數（物品實體 +0x38，`bag.Item.tradable`，
  照遊戲 `0x005D91E1` 的判斷抄）。拿旗標一刀切的後果＝華麗駱駝（3 綁、遊戲讓交易）
  被擋在公會倉庫清單外面，使用者 2026-09-16 回報。
  → **能不能存要問 `guildbank.eligible(item)`**（表 ＋ 這一件的狀態兩層），
    ⛔ 不是只問這支。
"""
from __future__ import annotations

import gzip

from app.paths import resource

DATA_FILE = "assets/item_flags.tsv.gz"

NO_BANK = 1      # 不可存倉庫；⚠ 自家位元編碼，非遊戲值；build_item_flags.py 抽 item.xml
NO_TRADE = 2     # 不可交易；⚠ 同上，build_item_flags.py 抽 item.xml
BIND = 4         # 裝備綁定；⚠ 同上，item.xml；⛔ 不擋存倉（見檔頭 2026-09-16 修）

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
    """**這一類**東西有沒有被表擋掉：不可存倉庫／不可交易任一亮就 False。

    ⚠⚠ 2026-09-16 改：**「裝備綁定」不再算擋**。那個旗標只說「這類東西會綁定」，
      不是這一件還能不能交易 —— 真正的條件是物品實體上的**剩餘綁定次數**
      （`bag.Item.tradable`，照遊戲 `0x005D91E1` 的判斷抄）。
      使用者回報「黑狐背包有華麗駱駝，存公會清單卻沒有」就是這裡一刀切造成的：
      那 5 隻都是 3 綁，遊戲讓交易。
      ⛔ 所以呼叫端**不能只問這支**，要 `guildbank.eligible(item)` 兩個條件一起看。
    """
    bits = flags(type_id)
    return bits is not None and not (bits & (NO_BANK | NO_TRADE))


def why_not(type_id: int) -> str:
    """表說不能放的原因（給清單／提示框看）；表沒擋回空字串。

    ⚠ 只講**表**的部分。這一件「綁定次數用完了」不在這裡，見 `bag.Item.tradable`。
    """
    bits = flags(type_id)
    if bits is None:
        return "不在資料表裡（新道具？重跑 py tools\\build_item_flags.py）"
    return "／".join(name for bit, name in _NAMES
                     if bit != BIND and bits & bit)


def count() -> int:
    """表裡有幾筆（診斷用；0 ＝ 表沒載到）。"""
    return len(_load())
