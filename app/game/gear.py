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
  `/_patchCheck` 的流程重驗，`tools/gear_check.py` 會把上面每一條逐項對一次。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from app.game import bag, itemname

# --- 物品結構（裝備專屬的那半）-------------------------------------------
OFF_ADV = 0x0C              # 5 個 dword：高 6 位屬性編號、低 26 位數值
ADV_SLOTS = 5
ADV_ID_SHIFT = 26
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
TMPL_LEVEL = 0x34           # 物品等級      74/74
TMPL_SKILL_LEVEL = 0x4C     # 技能等限      47/47
TMPL_HP = 0x58              # 最大HP        （提示框 0x5EFF79）
TMPL_MP = 0x60              # 最大MP
TMPL_ATK_MIN = 0x6C         # 攻擊下限　★「平均攻擊 ± 攻擊變數」實測 1722/1808
TMPL_ATK_MAX = 0x70         # 攻擊上限
TMPL_DEF = 0x74             # 防禦          （item.xml 防禦 100%）
TMPL_MATK = 0x78            # 魔攻          （item.xml 魔攻 100%）
TMPL_MDEF = 0x7C            # 魔防          （item.xml 魔防 100%）
TMPL_HIT = 0x80             # 精準          （item.xml 精準 100%）
TMPL_AGI = 0x84             # 靈敏
TMPL_MOVE_SPEED = 0xC4      # 移動速度
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


def read(scanner, slot: int) -> Gear | None:
    """讀某一格的裝備明細；那一格不是裝備／讀不到就回 None。

    ⚠ **每次要動作之前都要重讀一次**（`enhance.py` 就是這樣驗結果的）：
      強化失敗會讓裝備直接消失，先前那一拍讀到的東西可能已經不存在了。
    """
    got = bag.head(scanner)
    if got is None:
        return None
    begin, count = got
    if not 0 <= slot < count:
        return None
    ptr = struct.unpack_from(
        "<I", bytes(scanner._read_bytes(begin + slot * 4, 4) or b"\0\0\0\0"), 0)[0]
    if not ptr:
        return None
    blob = scanner._read_bytes(ptr, bag.ITEM_SPAN)
    if not blob or len(blob) < bag.ITEM_SPAN:
        return None
    items = bag.scan(scanner, slot, slot)[0]
    if not items or not items[0].is_gear:
        return None
    return _read_one(scanner, slot, items[0], bytes(blob))


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


# --- 提示框文字 ----------------------------------------------------------
GREEN = "#7CFC7C"           # 加成（照遊戲提示框：綠字）
GREY = "#C8C8C8"
RED = "#FF6060"
PURPLE = "#C894FF"          # 已強化次數那行（遊戲用 /c#c894ff）
BLUE = "#9A9CF7"            # 已鑲嵌寶石那行（遊戲用 /c#9a9cf7）
GRADE_COLOUR = {bag.GRADE_NORMAL: "#FFFFFF",
                bag.GRADE_TOP: "#FFCE10",     # 橘金：遊戲調色盤 $3 = RGB(255,206,16)
                bag.GRADE_FINE: "#00F7FF"}    # 青藍


def tooltip(g: Gear) -> list[tuple[str, str]]:
    """提示框要顯示的 [(文字, 顏色)]，順序照遊戲的提示框排。

    ⚠ 綠字加成只加**這件裝備自己的進階屬性**；遊戲的提示框還會把寶石效果
      算進去（`裝備顯示.png` 的 +1060 攻擊就有寶石的份）—— 寶石那份的算法
      還沒解，所以這裡**不假裝算得出來**：寶石單獨列出名字，不混進總和。
    """
    it = g.item
    lines: list[tuple[str, str]] = [
        (it.name, GRADE_COLOUR.get(it.grade, "#FFFFFF")),
        (kind_name(it.kind), GREY)]
    bonus = {aid: val for aid, val in g.advs}
    b = g.base

    def stat(attr_id: int, label: str, base_val: int, suffix: str = "") -> None:
        add = bonus.get(attr_id, 0)
        if not base_val and not add:
            return
        if add:
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
    stat(10, "點防禦力", b.get("def", 0))
    stat(11, "魔攻", b.get("matk", 0))
    stat(12, "魔防", b.get("mdef", 0))
    stat(13, "精準", b.get("hit", 0))
    stat(14, "靈敏", b.get("agi", 0))
    stat(1, "最大HP", b.get("hp", 0))
    stat(3, "最大MP", b.get("mp", 0))
    if b.get("atk_speed"):
        lines.append((f"攻擊速度 {b['atk_speed']}", GREY))
    lines.append((f"耐久度 {it.dura} / {it.dura_max}",
                  RED if it.broken else GREY))
    if b.get("weight"):
        lines.append((f"重量 {b['weight']}", GREY))
    if b.get("level_req"):
        lines.append((f"需要等級{b['level_req']}以上", GREY))

    # 剩下的進階屬性（上面沒配到基礎欄位的），照實列出來
    shown = {8, 10, 11, 12, 13, 14, 1, 3}
    for aid, val in g.advs:
        if aid in shown:
            continue
        lines.append((f"+{val} {attr_name(aid)}", GREEN))

    lines.append(("", GREY))
    lines.append((f"已強化次數 ( {g.enhance} )", PURPLE))
    if g.holes:
        lines.append((f"已鑲嵌寶石 ( {len(g.gems_filled)} / {g.holes} )", BLUE))
        for gem in g.gems[:g.holes]:
            lines.append((itemname.of(gem) or f"種類 {gem}" if gem else "（空孔）",
                          BLUE if gem else GREY))
    return lines
