"""裝備明細：**已強化次數、孔／鑲了什麼寶石、進階屬性、基礎數值**。

    for g in gear.in_bag(scanner):
        print(g.name, g.enhance, f"{len(g.gems_filled)}/{g.holes}")
        for line, colour in gear.tooltip(g):
            print(line)

`bag.Item` 給的是「每件東西都需要的」欄位（名稱／數量／耐久／品質…），
這一支專門處理**裝備才有的那半**，資料同樣是純讀記憶體。

## 偏移出處（2026-08-28 反組譯 angel.dat，全部有實機對照）

| 欄位 | 位置 | 出處 |
|---|---|---|
| 已強化次數 | 物品 `+0x52`（byte） | 提示框 `0x5F181E` → 字串 826「已強化次數 ( %d )」|
| 孔數 | 物品 `+0x51`（byte） | 提示框 `0x5EFA48` → 字串 699「已鑲嵌寶石 ( %d / %d )」|
| 每個孔 | 物品 `+0x3D` 起 5 個 dword | 同上；內容是**寶石的種類 ID**，0 ＝ 空孔 |
| 進階屬性 | 物品 `+0x0C` 起 5 個 dword | 提示框 `0x5F14C9`：高 6 位＝屬性編號、低 26 位＝數值 |

✅ 實機對照 `裝備顯示.png`（奧羅娜的預言）：已強化 10 次、5/5 孔全是紫光符文碎片、
   進階屬性「編號 3 ＝ 34」→ 提示框那行正是「+34 最大MP」。

## 屬性編號 → 名字（`ATTR_NAMES`）是**抽出來的不是猜的**

遊戲畫提示框時，把裝備的加成累加進 `管理器+0xCD7C` 那張 0x148 bytes 的表，
每一行提示框自己讀固定的一格。所以「第幾格 ＝ 哪個屬性」可以從各行的
`(讀第幾格, 取哪個字串, 讀範本哪個欄位)` 三者一起讀出來 ——
`0x5EFF79`（攻防那幾行）與 `0x5EFB91`（元素、速度那幾行）兩支就涵蓋了全部。

⚠ **不要拿 `0x7E7080` 那張 18 項的表當這個編號用** —— 那是「強化結果訊息」
  用的另一組序號（HP/MP/攻/防…），順序不一樣，硬套會安靜地標錯屬性名。

⚠ 這些是結構偏移（CLAUDE.md 允許寫死的那一類，大更新才會壞）；改版後靠
  `/_patchCheck` 的流程重驗 —— 不變量在 `tools/verify_offsets.py` 的
  「裝備明細 gear.OFF_*/TMPL_*」那條（強化次數 0~15、孔 0~5 且孔外必須是 0、
  寶石種類 ID 查得到名字、進階屬性編號認得出來、攻擊下限 ≤ 上限）。
  ⚠⚠ 那條**一定要連身上穿的一起看**：背包裡的裝備幾乎都是 +0／0 孔，
    全 0 的樣本驗不出版面對不對（偏移搬到隔壁照樣讀到 0、照樣印綠燈）。
    ⛔ 2026-08-28 之前這裡寫的是「`tools/gear_check.py` 會逐項對一次」——
      **那支從來沒存在過**，等於掛了一張沒人在驗的保證書。
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from app.game import bag, itemname

# 由 locate.warm() 依 AOB 寫回，⚠ 不要在別處複製這兩個值。
ITEM_TABLE_PTR = 0x0099735C     # [這裡] + 種類ID*4 → 道具範本
JEWEL_TABLE_PTR = 0x009973CC    # [這裡] + 效果編號*4 → 寶石效果表的一列

# --- 物品結構（裝備專屬的那半）-------------------------------------------
OFF_ADV = 0x0C              # 5 個 dword：高 6 位屬性編號、低 26 位數值
ADV_SLOTS = 5
ADV_ID_SHIFT = 26           # 出處：OFF_ADV 那句（高 6 位編號 → 值佔低 26 位）
ADV_VALUE_MASK = 0x3FFFFFF
OFF_GEM = 0x3D              # 5 個 dword：鑲進去的寶石**種類 ID**（0 ＝ 空孔）
GEM_SLOTS = 5
OFF_HOLES = 0x51            # byte：打了幾個孔
OFF_ENHANCE = 0x52          # byte：已強化次數
# ★ 上面全部落在 bag.ITEM_SPAN（0x5C）之內 —— 跟 bag.scan() 同一拍讀得到，
#   不必為了裝備明細多讀一次記憶體。
assert OFF_ENHANCE < bag.ITEM_SPAN

# --- 範本（基礎數值）-----------------------------------------------------
# 拿背包實物對 `setting/base/item*.xml` 找出來的（每一欄都是 N/N 全中）：
TMPL_FLAGS = 0x14           # 旗標；bit 0xC 決定攻擊速度那行印原值還是換算值
                            #   （出處：0x5F04FC 與 0x5EFE26 兩支互補的判斷）
TMPL_LEVEL = bag.TMPL_LEVEL # 物品等級      74/74（★ 定義在 bag.py，這裡只是別名）
TMPL_SKILL_LEVEL = 0x4C     # 技能等限      47/47
TMPL_HP = 0x58              # 最大HP        （提示框 0x5EFF79）
TMPL_MP = 0x60              # 最大MP        （同上，item.xml 全量比對）
TMPL_ATK_MIN = 0x6C         # 攻擊下限　★「平均攻擊 ± 攻擊變數」實測 1722/1808
TMPL_ATK_MAX = 0x70         # 攻擊上限　★ 跟 ATK_MIN 同一輪實測 1722/1808
TMPL_DEF = 0x74             # 防禦          （item.xml 防禦 100%）
TMPL_MATK = 0x78            # 魔攻          （item.xml 魔攻 100%）
TMPL_MDEF = 0x7C            # 魔防          （item.xml 魔防 100%）
TMPL_HIT = 0x80             # 精準          （item.xml 精準 100%）
TMPL_AGI = 0x84             # 靈敏          （同上，item.xml 全量比對）
TMPL_MOVE_SPEED = 0xC4      # 移動速度      （同上，item.xml 全量比對）
TMPL_ATK_SPEED = 0xC8       # 攻擊速度      （item.xml 攻擊速度 100%）
TMPL_RANGE = 0xCC           # 攻擊範圍      7/7
TMPL_WEIGHT = 0xF8          # 重量          117/117
TMPL_LEVEL_REQ = 0x120      # 等級限制      13/13（⚠ 不是 +0x34，那是物品等級）

# 屬性編號 → (名字, 範本基礎值偏移)。範本偏移是 None 代表這個屬性裝備本身
# 沒有基礎欄位，只會以加成形式出現。
ATTR_NAMES: dict[int, str] = {
    1: "最大HP", 3: "最大MP", 5: "SP燈", 7: "最大負重",
    8: "攻擊力", 10: "防禦力", 11: "魔攻", 12: "魔防",
    13: "精準", 14: "靈敏", 15: "移動速度", 16: "攻擊速度",
    18: "雷電攻擊", 19: "火焰攻擊", 20: "寒冰攻擊", 21: "腐蝕攻擊",
    22: "雷電防禦", 23: "火焰防禦", 24: "寒冰防禦", 25: "腐蝕防禦",
    26: "體質抵抗", 27: "心靈抵抗", 40: "重擊機率",
    44: "技能等級", 46: "裝備", 48: "額外格空間",
    57: "減少採集時間", 58: "減少製作時間",
}


# 裝備類的分類代號 → 名字。★ 代號本身是從記憶體讀的（範本 +0x18），這裡只是
# 把它翻成中文；對照關係是 `bag.py` 那份「+0x18 1:1 對到 item.xml 物品類別」
# （2026-08-08 五台實測 23 種分類零衝突）驗過的那一份，**只用於顯示**。
# ⚠ 認不得的代號一律顯示「分類N」，不猜。
KIND_NAMES = {
    2: "頭飾", 3: "衣服", 4: "手套", 5: "鞋子", 6: "飾品", 7: "背包",
    8: "披風", 9: "劍", 10: "刀", 11: "斧", 12: "錘", 13: "槍", 14: "杖",
    15: "弓箭", 16: "彈弓", 17: "盾",
}


# 屬性編號 → 範本裡的欄位偏移。**跟 ATTR_NAMES 是同一組編號**：
# 把寶石效果套用函式 `0x53384C` 的 switch 用每個代號各跑一遍，得到
# 「代號 → 它把範本哪個欄位加進去」，結果跟提示框那邊抽出來的編號完全一致。
# ⚠ 攻擊力是一對（下限、上限）。
ATTR_OFFSET: dict[int, tuple[int, ...]] = {
    1: (0x58,), 3: (0x60,), 5: (0x68,), 7: (0xFC,),
    8: (0x6C, 0x70), 10: (0x74,), 11: (0x78,), 12: (0x7C,),
    13: (0x80,), 14: (0x84,), 15: (0xC4,), 16: (0xC8,),
    18: (0x88,), 19: (0x94,), 20: (0xA0,), 21: (0xAC,),
    22: (0x8C,), 23: (0x98,), 24: (0xA4,), 25: (0xB0,),
    26: (0xB8,), 27: (0xBC,), 40: (0xC0,),
    44: (0xE0,), 57: (0xE8,), 58: (0x13C,),
}

# --- 強化加成（`0x5331B6`）-----------------------------------------------
# 三條分支，差別只在係數與加到哪幾欄；分類走哪一條是遊戲自己的跳表決定的
# （位元組表 `0x532FA7` 之外，強化這支用的是 `0x53344E`）：
#   武器（分類 9~16、63、75）：每級 = ceil(平均攻擊 × 0.06)，攻擊上下限各加
#                              「每級 × 次數」，且**至少等於次數**；
#                              魔攻 = ceil(魔攻 × 0.06) × 次數（沒有保底）
#   防具（分類 2~8、17、76）：0.03，加防禦（有保底）與魔防（沒有）
#   座騎（分類 25）：0.08 的武器版，另外移動速度 += 次數 × 2
# ✅ 對帳：奧羅娜的預言（杖，1722~1808，強化 10）→ ceil(1765×0.06)=106、
#    106×10 = **1060**，跟遊戲提示框的「+1060 點攻擊力」一模一樣。
ENHANCE_WEAPON_KINDS = frozenset({9, 10, 11, 12, 13, 14, 15, 16, 63, 75})
ENHANCE_ARMOR_KINDS = frozenset({2, 3, 4, 5, 6, 7, 8, 17, 76})
ENHANCE_MOUNT_KIND = 25     # 座騎分類 25（出處見上面強化公式那段）
RATE_ARMOR = 0.03           # [0x7D8B48]
RATE_WEAPON = 0.06          # [0x7D8B4C]
RATE_MOUNT = 0.08           # [0x7D8B50]

# 寶石效果表一列的版面：三組效果（+0x00 / +0x24 / +0x48），
# 每組裡面照**裝備分類**挑一欄（＝ jeweleffect.xml 的 影響武器／影響裝備2~8／影響盾）。
# 出處：`0x532CD3` 的跳表（位元組表 0x532FA7、case 表 0x532F83）逐支反組譯。
GEM_GROUPS = (0x00, 0x24, 0x48)
GEM_FIELD_OF_KIND: dict[int, int] = {
    9: 0x00, 10: 0x00, 11: 0x00, 12: 0x00, 13: 0x00, 14: 0x00, 15: 0x00,
    16: 0x00,                                   # 武器
    2: 0x04, 3: 0x08, 4: 0x0C, 5: 0x10,         # 頭飾／衣服／手套／鞋子
    8: 0x14, 6: 0x18, 7: 0x1C, 17: 0x20,        # 披風／飾品／背包／盾
}


def _f32(x: float) -> float:
    """照 x86 的 `mulss` 那樣把中間結果收成單精度 —— 不然 ceil 會差 1。"""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _per_level(value: int, rate: float) -> int:
    return math.ceil(_f32(_f32(float(value)) * _f32(rate)))


def kind_name(kind: int) -> str:
    """裝備分類的中文名；認不得就照實顯示代號。"""
    return KIND_NAMES.get(kind, f"分類{kind}")


def attr_name(attr_id: int) -> str:
    """屬性編號的名字；**認不得就照實顯示編號**（不猜、不套別張表）。"""
    return ATTR_NAMES.get(attr_id, f"屬性{attr_id}")


@dataclass(frozen=True)
class Gear:
    """一件裝備（`bag.Item` ＋ 裝備才有的那半）。"""

    item: bag.Item
    enhance: int                       # 已強化次數
    holes: int                         # 孔數
    gems: tuple[int, ...] = ()         # 每個孔的寶石種類 ID（0 ＝ 空孔）
    advs: tuple[tuple[int, int], ...] = ()   # (屬性編號, 數值)
    base: dict[str, int] = field(default_factory=dict)   # 範本基礎數值

    # --- 轉手常用的 -------------------------------------------------
    @property
    def slot(self) -> int:
        return self.item.slot

    @property
    def serial(self) -> int:
        """★ 驗「還是不是同一件」一律認這個，不能認格號或指標。"""
        return self.item.serial

    @property
    def name(self) -> str:
        return self.item.name

    @property
    def icon_id(self) -> int:
        return self.item.icon_id

    @property
    def gems_filled(self) -> tuple[int, ...]:
        """真的鑲了東西的那幾顆（照抄遊戲的算法：非 0 才算）。"""
        return tuple(g for g in self.gems[:self.holes] if g)


def _read_one(scanner, slot: int, item: bag.Item, blob: bytes) -> Gear:
    enhance = blob[OFF_ENHANCE]
    holes = blob[OFF_HOLES]
    gems = struct.unpack_from(f"<{GEM_SLOTS}I", blob, OFF_GEM)
    advs = []
    for raw in struct.unpack_from(f"<{ADV_SLOTS}I", blob, OFF_ADV):
        if raw:
            advs.append((raw >> ADV_ID_SHIFT, raw & ADV_VALUE_MASK))
    return Gear(item=item, enhance=enhance, holes=holes, gems=gems,
                advs=tuple(advs), base=_base_stats(scanner, blob))


_BASE_FIELDS = (
    ("flags", TMPL_FLAGS),
    ("atk_min", TMPL_ATK_MIN), ("atk_max", TMPL_ATK_MAX),
    ("def", TMPL_DEF), ("matk", TMPL_MATK), ("mdef", TMPL_MDEF),
    ("hit", TMPL_HIT), ("agi", TMPL_AGI), ("hp", TMPL_HP), ("mp", TMPL_MP),
    ("atk_speed", TMPL_ATK_SPEED), ("move_speed", TMPL_MOVE_SPEED),
    ("range", TMPL_RANGE), ("weight", TMPL_WEIGHT),
    ("level", TMPL_LEVEL), ("level_req", TMPL_LEVEL_REQ),
)


def _base_stats(scanner, blob: bytes) -> dict[str, int]:
    tmpl = struct.unpack_from("<I", blob, bag.ITEM_TMPL)[0]
    if not 0x10000 < tmpl < 0x7FFF0000:
        return {}
    raw = scanner._read_bytes(tmpl, TMPL_LEVEL_REQ + 4)
    if not raw:
        return {}
    t = bytes(raw)
    return {name: struct.unpack_from("<i", t, off)[0]
            for name, off in _BASE_FIELDS}


# `read_state()` 的第二個回傳值。★ 這個三態是**安全關鍵**，不是裝飾：
# 「讀不到」跟「那一格真的沒東西」在這條路上長得一模一樣（[[bag-false-empty-guards]]，
# 復發六次），而 `enhance.py` 拿它判定「裝備是不是被強化錘打掉了」——
# 把讀取失敗當成消失，會對著使用者宣告他的裝備沒了（2026-08-28 /_audit 抓到）。
READ_OK = "ok"
READ_UNREADABLE = "unreadable"   # 讀不到：換地圖／遊戲正在搬背包／頁面沒映射
READ_ABSENT = "absent"           # **確定讀到了**，那一格是空的或不是裝備


def read_state(scanner, slot: int) -> tuple["Gear | None", str]:
    """(那一格的裝備, 為什麼沒有)。回 `READ_ABSENT` 才代表「真的不在」。

    ⚠ **每次要動作之前都要重讀一次**（`enhance.py` 就是這樣驗結果的）：
      強化失敗會讓裝備直接消失，先前那一拍讀到的東西可能已經不存在了。
    ⛔ 呼叫端不准把 `None` 一律當「不見了」—— 要先看第二個值。
    """
    got = bag.head(scanner)
    if got is None:
        # 背包表頭讀不到：`bag.head` 自己已經重試過 HEAD_TRIES 次，
        # 還是不行就是換地圖／還沒進場 —— 這時候什麼都不知道。
        return None, READ_UNREADABLE
    begin, count = got
    if not 0 <= slot < count:
        return None, READ_UNREADABLE
    raw = scanner._read_bytes(begin + slot * 4, 4)
    if not raw:
        return None, READ_UNREADABLE
    ptr = struct.unpack_from("<I", bytes(raw), 0)[0]
    if not ptr:
        return None, READ_ABSENT          # 指標讀到了，是 0 ＝ 這格真的空著
    blob = scanner._read_bytes(ptr, bag.ITEM_SPAN)
    if not blob or len(blob) < bag.ITEM_SPAN:
        return None, READ_UNREADABLE
    items, complete = bag.scan(scanner, slot, slot)
    if not items:
        # 掃得完整才敢說「這格沒東西」；掃不完整就是讀不到
        return None, (READ_ABSENT if complete else READ_UNREADABLE)
    if not items[0].is_gear:
        return None, READ_ABSENT          # 讀到了，但那不是裝備
    return _read_one(scanner, slot, items[0], bytes(blob)), READ_OK


def read(scanner, slot: int) -> Gear | None:
    """讀某一格的裝備明細；那一格不是裝備／讀不到都回 None。

    ⚠ 分不出「讀不到」與「不在了」——**要判定東西有沒有消失一律用
      `read_state()`**，別用這支。
    """
    return read_state(scanner, slot)[0]


def in_bag(scanner, first: int = bag.FIRST_SLOT,
           last: int = bag.LAST_SLOT) -> tuple[list[Gear], bool]:
    """(這段格號裡的裝備, 整段是不是真的都讀到了)。

    ⚠ 第二個值跟 `bag.scan()` 一樣重要：讀不到時回的空清單跟「真的沒有裝備」
      長得一模一樣（[[bag-false-empty-guards]]）。
    """
    items, complete = bag.scan(scanner, first, last)
    got = bag.head(scanner)
    if got is None:
        return [], False
    begin, _count = got
    out: list[Gear] = []
    for it in items:
        if not it.is_gear:
            continue
        raw = scanner._read_bytes(begin + it.slot * 4, 4)
        if not raw:
            complete = False
            continue
        ptr = struct.unpack_from("<I", bytes(raw), 0)[0]
        blob = scanner._read_bytes(ptr, bag.ITEM_SPAN) if ptr else None
        if not blob or len(blob) < bag.ITEM_SPAN:
            complete = False
            continue
        b = bytes(blob)
        # ★ 認 serial：指標還在不代表還是同一件（換裝是「指標不動、內容互換」）
        if struct.unpack_from("<I", b, bag.ITEM_SERIAL)[0] != it.serial:
            complete = False
            continue
        out.append(_read_one(scanner, it.slot, it, b))
    return out, complete


# --- 加成（強化 ＋ 進階屬性 ＋ 寶石）-------------------------------------
def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else 0


def _sane(ptr: int) -> bool:
    return 0x10000 < ptr < 0x7FFF0000


def item_template(scanner, type_id: int) -> int:
    """種類 ID → 道具範本；查不到回 0（照抄遊戲 `0x50348A` 的邊界檢查）。"""
    if not 1 <= int(type_id) <= 0x15F8F:
        return 0
    table = _u32(scanner, ITEM_TABLE_PTR)
    if not _sane(table):
        return 0
    tmpl = _u32(scanner, table + int(type_id) * 4)
    return tmpl if _sane(tmpl) else 0


def _jewel_row(scanner, effect_id: int) -> int:
    if not 1 <= int(effect_id) <= 0x3E7:
        return 0
    table = _u32(scanner, JEWEL_TABLE_PTR)
    if not _sane(table):
        return 0
    row = _u32(scanner, table + int(effect_id) * 4)
    return row if _sane(row) else 0


def _no_gem_bonus(g: "Gear") -> dict[int, int]:
    """沒有 scanner 時的退路：只算強化＋進階屬性（寶石那份算不了就不假裝）。"""
    out = dict(enhance_bonus(g))
    for attr_id, val in g.advs:
        out[attr_id] = out.get(attr_id, 0) + val
    return out


def enhance_bonus(g: "Gear") -> dict[int, int]:
    """強化次數帶來的加成 {屬性編號: 數值}（照抄 `0x5331B6`）。"""
    n = g.enhance
    out: dict[int, int] = {}
    if n <= 0 or not g.base:
        return out
    kind = g.item.kind
    b = g.base
    if kind in ENHANCE_WEAPON_KINDS or kind == ENHANCE_MOUNT_KIND:
        rate = RATE_MOUNT if kind == ENHANCE_MOUNT_KIND else RATE_WEAPON
        avg = _f32(float(b.get("atk_min", 0) + b.get("atk_max", 0)) * 0.5)
        out[8] = max(_per_level(avg, rate) * n, n)        # ★ 至少等於次數
        matk = _per_level(b.get("matk", 0), rate) * n
        if matk:
            out[11] = matk
        if kind == ENHANCE_MOUNT_KIND:
            out[15] = n * 2                               # 座騎：移動速度
    elif kind in ENHANCE_ARMOR_KINDS:
        out[10] = max(_per_level(b.get("def", 0), RATE_ARMOR) * n, n)
        mdef = _per_level(b.get("mdef", 0), RATE_ARMOR) * n
        if mdef:
            out[12] = mdef
    return out


def gem_bonus(scanner, g: "Gear") -> dict[int, int]:
    """鑲的寶石帶來的加成 {屬性編號: 數值}（照抄 `0x532CD3` ＋ `0x53384C`）。

    ⚠ 值是**寶石自己的範本欄位**（效果編號只決定「加哪一欄」），所以查不到
      寶石範本就整顆跳過 —— 不猜。
    """
    out: dict[int, int] = {}
    field_off = GEM_FIELD_OF_KIND.get(g.item.kind)
    if field_off is None:
        return out
    for type_id in g.gems[:g.holes]:
        if not type_id:
            break                       # 遊戲自己也是遇到空孔就停
        tmpl = item_template(scanner, type_id)
        if not tmpl:
            continue
        effect_id = _u32(scanner, tmpl + bag.TMPL_PARAM1)
        row = _jewel_row(scanner, effect_id) if effect_id else 0
        if not row:
            continue
        for grp in GEM_GROUPS:
            code = _u32(scanner, row + grp + field_off)
            for off in ATTR_OFFSET.get(code, ()):        # 認不得的代號就跳過
                val = struct.unpack(
                    "<i", bytes(scanner._read_bytes(tmpl + off, 4) or bytes(4)))[0]
                if val:
                    out[code] = out.get(code, 0) + val
                break                  # 攻擊力那對取下限那欄就好（上下限同值）
    return out


def total_bonus(scanner, g: "Gear") -> dict[int, int]:
    """提示框綠字要顯示的加成總和：強化 ＋ 進階屬性 ＋ 寶石。"""
    out = dict(enhance_bonus(g))
    for attr_id, val in g.advs:
        out[attr_id] = out.get(attr_id, 0) + val
    for attr_id, val in gem_bonus(scanner, g).items():
        out[attr_id] = out.get(attr_id, 0) + val
    return out


# --- 提示框文字 ----------------------------------------------------------
GREEN = "#7CFC7C"           # 加成（照遊戲提示框：綠字）
GREY = "#C8C8C8"
RED = "#FF6060"
PURPLE = "#C894FF"          # 已強化次數那行（遊戲用 /c#c894ff）
BLUE = "#9A9CF7"            # 已鑲嵌寶石那行（遊戲用 /c#9a9cf7）
GRADE_COLOUR = {bag.GRADE_NORMAL: "#FFFFFF",
                bag.GRADE_TOP: "#FFCE10",     # 橘金：遊戲調色盤 $3 = RGB(255,206,16)
                bag.GRADE_FINE: "#00F7FF"}    # 青藍


def speed_display(raw: int) -> int:
    """範本的攻擊速度欄 → **提示框上顯示的那個小數字**。

        顯示值 = (範本+0xC8 − 100) ÷ 10 + 5      （整數除法，向零截斷）

    出處：`0x5F051C`（提示框攻擊速度那行）
        `mov eax,[範本+0xC8] / add eax,-0x64 / idiv 10 / lea ecx,[eax+5]`
    ✅ 使用者 2026-08-28 實機核對：奧羅娜的預言 範本值 70 → 遊戲顯示 **2**，
       (70−100)÷10+5 = 2 吻合。
    ⚠ 以前直接把 70 印出來是錯的 —— 那是內部值，不是玩家看到的數字。
    """
    d = int(raw) - 100
    # C 的整數除法是**向零截斷**，Python 的 // 是向下取整，負數會差 1
    q = -(abs(d) // 10) if d < 0 else d // 10
    return q + 5


# ⚠ 出處：攻擊速度那行的閘門（照抄 `0x5F04FC` 與 `0x5EFE26` 兩支互補的判斷）：
#   範本+0x14 的 bit 0xC 有立 → 印**換算後**的值（分類 46 紙娃娃除外）
#   沒立                     → 分類 24 印換算後的值，其餘印範本原值
SPEED_FLAG = 0x0C
SPEED_KIND_RAW = 0x18           # 24
SPEED_KIND_NEVER = 0x2E         # 46 紙娃娃


def _speed_line(lines: list, it, b: dict, add: int) -> None:
    raw = b.get("atk_speed", 0)
    if not raw:
        return
    if b.get("flags", 0) & SPEED_FLAG:
        if it.kind == SPEED_KIND_NEVER:
            return
        shown = speed_display(raw)
        if add:
            lines.append((f"攻擊速度 {speed_display(raw + add)}（{shown}）", GREEN))
        else:
            lines.append((f"攻擊速度 {shown}", GREY))
        return
    if it.kind == SPEED_KIND_RAW:
        lines.append((f"攻擊速度 {speed_display(raw)}", GREY))
        return
    lines.append((f"攻擊速度 {raw + add}"
                  + (f"（{raw} +{add}）" if add else ""),
                  GREEN if add else GREY))


def tooltip(g: Gear, scanner=None) -> list[tuple[str, str]]:
    """提示框要顯示的 [(文字, 顏色)]，順序照遊戲的提示框排。

    綠字加成 ＝ **強化 ＋ 進階屬性 ＋ 寶石**，三份都照遊戲自己的算法算
    （見 `enhance_bonus` / `gem_bonus`）。
    ⚠ 寶石那份要讀寶石的範本，所以得給 `scanner`；沒給就只算強化＋進階屬性。
    """
    it = g.item
    lines: list[tuple[str, str]] = [
        (it.name, GRADE_COLOUR.get(it.grade, "#FFFFFF")),
        (kind_name(it.kind), GREY)]
    bonus = (total_bonus(scanner, g) if scanner is not None
             else _no_gem_bonus(g))
    b = g.base

    def stat(attr_id: int, label: str, base_val: int, suffix: str = "") -> None:
        add = bonus.get(attr_id, 0)
        if not base_val and not add:
            return
        if add and not base_val:
            # 裝備本身沒有這一欄、純粹是加成 → 照遊戲寫成「+34 最大MP」
            lines.append((f"+{add} {label}{suffix}", GREEN))
        elif add:
            lines.append((f"{base_val + add}（{base_val} +{add}）{label}{suffix}",
                          GREEN))
        else:
            lines.append((f"{base_val} {label}{suffix}", GREY))

    if b.get("atk_max"):
        add = bonus.get(8, 0)
        lo, hi = b["atk_min"], b["atk_max"]
        if add:
            lines.append((f"{lo + add}-{hi + add}（{lo}-{hi} +{add}）點攻擊力",
                          GREEN))
        else:
            lines.append((f"{lo}-{hi} 點攻擊力", GREY))
    # ★ 行的順序照遊戲的提示框排（使用者的 裝備顯示.png）：
    #   攻擊力 → 攻擊速度 → 防禦 → 魔攻 → 魔防 → HP → MP → 精準 → 靈敏
    _speed_line(lines, it, b, bonus.get(16, 0))
    stat(10, "點防禦力", b.get("def", 0))
    stat(11, "魔攻", b.get("matk", 0))
    stat(12, "魔防", b.get("mdef", 0))
    stat(1, "最大HP", b.get("hp", 0))
    stat(3, "最大MP", b.get("mp", 0))
    stat(13, "精準", b.get("hit", 0))
    stat(14, "靈敏", b.get("agi", 0))
    # ⚠ 攻擊範圍（範本 +0xCC）讀得到，但使用者 2026-08-28 指定**不要印**。
    lines.append((f"耐久度 {it.dura} / {it.dura_max}",
                  RED if it.broken else GREY))
    if b.get("weight"):
        lines.append((f"重量 {b['weight']}", GREY))
    if b.get("level_req"):
        lines.append((f"需要等級{b['level_req']}以上", GREY))

    # 剩下的加成（上面沒配到基礎欄位的），照實列出來
    shown = {8, 10, 11, 12, 13, 14, 1, 3, 16}
    for aid in sorted(bonus):
        if aid in shown or not bonus[aid]:
            continue
        lines.append((f"+{bonus[aid]} {attr_name(aid)}", GREEN))

    lines.append(("", GREY))
    lines.append((f"已強化次數 ( {g.enhance} )", PURPLE))
    if g.holes:
        lines.append((f"已鑲嵌寶石 ( {len(g.gems_filled)} / {g.holes} )", BLUE))
        for gem in g.gems[:g.holes]:
            lines.append((itemname.of(gem) or f"種類 {gem}" if gem else "（空孔）",
                          BLUE if gem else GREY))
    return lines
