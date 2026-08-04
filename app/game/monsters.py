"""怪物範本（王／等級／最大HP）：直接讀遊戲自己載進來的那張表。

★ 這裡**沒有寫死任何遊戲資料** ★
------------------------------
`items.py` 的球表、`scene.py` 的地圖名表都是抄進程式的（改版新增就安靜漏掉）。
這一份不是 —— 遊戲啟動時會把 `SETTING/BASE/MONSTER.XML` 整份載進記憶體，
我們只是照著它自己的索引去查。**改版新增怪物、調整數值都會自動跟上**。

怎麼查
------
    [angel.dat + INDEX_PTR_RVA]      → 索引表（照種類 ID 排的指標陣列）
    索引表 + 種類ID * 4              → 該種怪的範本記錄
    記錄 + OFF_FLAGS  的 bit 16      → 是不是「王」
    記錄 + OFF_LEVEL / OFF_MAX_HP    → 等級 / 滿血血量

種類 ID 就是 `Entity.type_id`（掃描時本來就會讀），所以判斷一隻怪是不是王
只要兩次 4-byte 讀取，等於免費。

## 為什麼要分王和小怪

「選中怪物」是**用名字比對**的，而實測有 **20 種怪同名卻一個是王一個不是**
（哥布林幹部 78 / 663(王)、惡臭史萊姆 835 / 836(王)、高熱能妖精 909 /
910(王) / 9235(王)、格鬥野狼 9252(王) / 16184 / 17495 …）。
只靠名字的話，選了小怪版就會連王一起打。要分得開只能用種類 ID。

## 驗證（2026-08-04）

* `OFF_FLAGS` 的每一個 bit 都對得上 MONSTER.XML 的「是/否」欄位，
  19,374 筆記錄全部**吻合度 1.00**（bit16 命中 1,545 筆＝XML 裡 `王="是"`
  的筆數，一筆不差）。
* 拿有 HP 的 5,128 筆逐格比對「王 vs 小怪」，`+0x24` 是唯一**完全不重疊**
  的欄位。`OFF_MAX_HP` 對得上 5,122/5,128、`OFF_LEVEL` 5,845/5,851。
* 現場對照：黑狐的水晶傘蜥蜴（種類 58）讀出「王、Lv17、HP3262」，
  與 XML 完全一致。

⚠ 這幾個位址跟其他寫死的位址一樣，**遊戲改版會位移**（見 memory 的
  game-patch-relocate）。壞掉的症狀是 `info()` 回 None，不會回錯的答案 ——
  `record()` 會檢查指標與數值是否說得通。

純讀記憶體，不寫入、不注入。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

GAME_MODULE = "angel.dat"

# 索引表指標所在（絕對位址 0x0098BC88）。深度 1，解析一次約 0.01 ms。
INDEX_PTR_RVA = 0x58BC88

# 範本記錄的欄位（相對記錄起點）
OFF_ART = 0x00        # 圖號
OFF_LEVEL = 0x08      # 等級
OFF_AI = 0x0C         # AI 編號
OFF_FLAGS = 0x24      # 「是/否」屬性的位元欄位，見 BIT_*
OFF_MAX_HP = 0x28     # 滿血血量
OFF_DEF = 0x38        # 防禦（＝魔防，兩者數值相同所以分不出來）
OFF_ATK = 0x3C        # 平均攻擊（＝魔攻，同上）

# OFF_FLAGS 的位元。全部用 19,374 筆記錄跟 MONSTER.XML 對過，吻合度都是 1.00。
BIT_BOSS = 16             # 王
BIT_NO_MOB_ATTACK = 17    # 不被怪物攻擊
BIT_DEATH_HANDLER = 18    # 死亡處理
BIT_CHASE_FOREVER = 19    # 追目標到死
BIT_EVENT = 21            # 特殊活動
# bit0~15 是各種「免疫○○」、bit20 免疫傷害反噬、bit22 特殊怪傷害調整。

# 記錄裡的合理性檢查：位址失效時用它擋住亂數。
# ⚠⚠ 上限**別憑感覺訂**（踩過）：原本 HP 開 1 億，結果把 214 隻高階王
#   （天使試煉城主那類，HP 1.8~4.4 億）全部判成「查不到」——
#   而那正是最需要認出來的怪。實際 MONSTER.XML 的極值是
#   **HP 10 億、等級 495**，所以這裡照著留餘裕。
#   （同一個坑在 player.py 也有：上限開太鬆會抓到垃圾、開太緊會殺掉真資料。）
_MAX_LEVEL = 5_000
_MAX_HP = 2_000_000_000


@dataclass(frozen=True)
class MonsterInfo:
    """某一「種」怪的固定資料（跟場上那隻是誰無關）。"""

    type_id: int
    level: int
    max_hp: int
    flags: int

    @property
    def boss(self) -> bool:
        return bool(self.flags & (1 << BIT_BOSS))

    @property
    def chase_forever(self) -> bool:
        """追目標到死 —— 跑不掉的那種，挑目標時值得避開。"""
        return bool(self.flags & (1 << BIT_CHASE_FOREVER))

    def __str__(self) -> str:
        # ⚠ 不要用 👑 之類的 emoji：中文字型沒有那個字形，畫面上會變豆腐方塊
        #   （離屏截圖實拍到）。見 memory 的 memory-re-pitfalls 第 11 條。
        return (f"{'【王】' if self.boss else '小怪'} Lv{self.level} "
                f"HP{self.max_hp}")


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<I", bytes(raw[:4]))[0]


def index_base(scanner) -> int | None:
    """索引表位址；讀不到（改版位移、遊戲還沒載完）時回 None。"""
    base = scanner.module_base(GAME_MODULE)
    if not base:
        return None
    idx = _u32(scanner, base + INDEX_PTR_RVA)
    return idx if idx and 0x10000 <= idx < 0xFFFF0000 else None


def record(scanner, type_id: int, idx: int | None = None) -> int | None:
    """某種怪的範本記錄位址。type_id 沒有對應記錄時回 None。"""
    if type_id <= 0 or type_id > 0xFFFFF:
        return None
    idx = index_base(scanner) if idx is None else idx
    if idx is None:
        return None
    p = _u32(scanner, idx + type_id * 4)
    return p if p and 0x10000 <= p < 0xFFFF0000 else None


def info(scanner, type_id: int, idx: int | None = None) -> MonsterInfo | None:
    """查一種怪的固定資料；查不到或數值不合理時回 None（不會回錯的答案）。"""
    rec = record(scanner, type_id, idx)
    if rec is None:
        return None
    raw = scanner._read_bytes(rec, OFF_MAX_HP + 4)
    if not raw or len(raw) < OFF_MAX_HP + 4:
        return None
    b = bytes(raw)
    level = struct.unpack_from("<I", b, OFF_LEVEL)[0]
    flags = struct.unpack_from("<I", b, OFF_FLAGS)[0]
    max_hp = struct.unpack_from("<I", b, OFF_MAX_HP)[0]
    if level > _MAX_LEVEL or max_hp > _MAX_HP:
        return None                     # 位址失效了，寧可說不知道
    return MonsterInfo(type_id, level, max_hp, flags)


def is_boss(scanner, type_id: int, idx: int | None = None) -> bool | None:
    """是不是王。**回 None 代表不知道**（查不到），呼叫端要自己決定怎麼辦。"""
    got = info(scanner, type_id, idx)
    return None if got is None else got.boss
