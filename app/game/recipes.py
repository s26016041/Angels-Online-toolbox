"""製作配方與公會貢獻品 —— **直接讀遊戲自己載進記憶體的那兩張表**。

★ 這裡沒有寫死任何遊戲資料 ★（CLAUDE.md 資料來源優先序第 1 條）
------------------------------------------------------------
配方、材料、需要的技能等級、哪些東西捐得進公會、一組幾個 —— 全部是遊戲
啟動時就載好的表。改版新增配方會自動跟上，不必重新解包 GAMEDATA/setting。

怎麼查（都是反組譯遊戲自己的查表函式來的）
------------------------------------------
    製作配方  `Make`      [MAKE_TAB] + 配方ID*4      配方ID 1..0x1000
    公會貢獻  `Gcontrib`  [GC_TAB]   + 編號*4        編號   1..0x200
                          [GC_TAB+4] = 筆數（實測 167）
    物品範本  `Item`      [ITEM_TAB] + 物品ID*4      物品ID 1..0x15F90

    學會的配方 = 主物件（gather.WORLD_PTR 那個）+ OFF_LEARNED 起的位元圖，
                 bit 索引就是配方ID（`initmakeclasswnd` 0x58F13D 那行
                 `test [ecx+eax*4+0x58F8], edx`）

配方記錄的欄位（2026-09-22 版；新舊映像各 27 個「查 Make 表」呼叫點逐一對出來的）
--------------------------------------------------------------------------
    +0x00  產物 ID           +0x04 != 0 時產物是**技能**不是物品
    +0x0C ~ +0x1C  生產類別 ×5（烹飪 31…；★9/22 改版從 1 格變 5 格，一個配方可以
                   屬好幾種類別 —— 遊戲自己的比對函式 0x552866 就是「+0x0C 起 5 格
                   逐一 cmp」；後面所有欄位因此整段往後搬 0x10）
    +0x20  需要的技能等級（⚠ 只憑資料對得上 Lv1/Lv5/Lv11 產物，沒有反組譯出處；
           程式裡**沒有任何判斷用到它**，只是顯示／參考）
    +0x38  成功率（實測都是 100；舊版 +0x28，0x5ecca0 讀）
    +0x3C ~ +0x4C  材料物品 ID ×5（舊版 +0x2C；0x59a48d／0x5e4cd1 讀）
    +0x50 ~ +0x60  各材料要幾個 ×5（舊版 +0x40；0x556374／0x5af959 讀）
    +0x64  用到幾種材料（舊版 +0x54；0x59a48d／0x5af959 讀）
    +0x68  3 = 半成品、5 = 成品（舊版 +0x58；0x59a1cd／0x5ea3dd／0x603457 讀；
           跟下面的分類 27 完全一致，當交叉驗證）

    ⚠ 這些是 disp8（1 byte）偏移，`locate` 的 off 類只解得了 4-byte disp32，
      所以**沒辦法 AOB 自動跟**。改版後的防線是 `layout_ok()`：學會的配方裡
      只要有一筆旗標不是 3/5、或材料種類數對不上材料數，就當版面搬了 →
      清單類全部回 None（讀不到）而不是空清單，掛機分頁會大聲警示；
      `tools/verify_offsets.py` 也有一條同樣的檢查給 /_patchCheck 用。
    ⚠ 2026-09-23 踩過：舊偏移讀新版記錄，旗標全 0 → 「沒有半成品配方」→
      一到製作檯就「半成品做完了，一共 0 個」。安靜做錯事的典型。

## 「半成品」不是猜的

`initmakeclasswnd` 把配方分類的依據是**產物那個物品的分類欄**
（物品範本 +0x18），再拿分類去查介面字串：分類 27 被特別對應到字串
編號 0xB6C，而 0xB6C 讀出來就是 **「半成品」**（2026-08-11 實機驗證）。
所以 `ITEM_CLASS_SEMI = 27`，不是從名字或編號規律推的。

## 讀不到 ≠ 沒有

清單類的回傳一律是 **`None` = 這一拍讀不到**（載入中／改版／位址失效），
**空清單 = 真的沒有**。這個專案把「讀不到」講成「沒有」已經復發過六次
（見 memory 的 bag-false-empty-guards），這裡從一開始就分開。

純讀記憶體，不寫入、不注入、不送封包。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import gather

# --- 由 AOB 自動定位（見 app/game/locate.py 的 SIGS）---
MAKE_TAB = 0x0098BCA0      # [這裡] + 配方ID*4 → 配方記錄
GC_TAB = 0x0098FD2C        # [這裡] + 編號*4 → 貢獻品記錄；[這裡+4] = 筆數
ITEM_TAB = 0x0098BC70      # [這裡] + 物品ID*4 → 物品範本
OFF_LEARNED = 0x5900       # 主物件 + 這裡 = 「配方學會了沒」位元圖

# ⚠ 主物件的全域指標不在這裡重寫 —— 跟 gather 是同一個位址，
#   同一個位址不准登記兩次（CLAUDE.md）。

MAKE_MAX = 0x1000          # 配方ID 上限（查表函式自己的 cmp ecx,0xFFF）
GC_MAX = 0x200             # 貢獻編號上限（查表函式自己的 mov eax,0x1FF）
ITEM_MAX = 0x15F90         # 物品ID 上限（查表函式自己的 cmp ecx,0x15F8F）

# 「半成品」的判定值。出處：`initmakeclasswnd` 0x58F1E4 把物品分類 +0x5B0
# 當介面字串編號，27 那個（0x5CB）被特別改成 0xB6C，而 0xB6C 從遊戲的字串表
# 讀出來就是「半成品」（2026-08-11 實機讀到）。不是從編號規律推的。
ITEM_CLASS_SEMI = 27
# 配方記錄自己的 +0x58：實測烹飪 22 個半成品配方全是 3、成品全是 5。
# ⚠ 這個值**沒有**像上面那樣有名字可對，所以只當交叉驗證的第二票，
#   不單獨拿來判定（見 semi_finished）。
STAGE_SEMI = 3

# 配方記錄的欄位（2026-09-22 版；出處見檔頭。⚠ disp8 偏移，AOB 跟不了，
# 靠 layout_ok() 當防線）
R_PRODUCT, R_IS_SKILL, R_CLASS = 0x00, 0x04, 0x0C
CLASS_SLOTS = 5            # +0x0C 起 5 格生產類別（0x552866 的迴圈上限就是 5）
R_NEED_LEVEL, R_RATE = 0x20, 0x38
R_MAT_ID, R_MAT_NUM, R_MAT_KINDS, R_STAGE = 0x3C, 0x50, 0x64, 0x68
R_SIZE = 0x6C              # ⚠ 一筆讀到這裡（涵蓋 R_STAGE +0x68）；欄位出處見檔頭
MAT_SLOTS = 5
STAGE_FULL = 5             # +0x68 的另一個合法值（成品）；旗標只會是 3 或 5

# 貢獻品記錄的欄位
G_ID, G_ITEM, G_GROUP, G_POINTS = 0x00, 0x04, 0x08, 0x0C
G_SIZE = 0x10              # ⚠ 一筆讀到這裡（涵蓋 G_POINTS +0x0C）；欄位出處見檔頭

# ⚠ 出處＝反組譯 initmakeclasswnd：分半成品時讀「物品範本 +0x18」當分類欄（見檔頭）。
OFF_ITEM_CLASS = 0x18      # 物品範本的分類欄

_PTR_LO, _PTR_HI = 0x10000, 0x7FFF0000


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Recipe:
    """一個製作配方。材料是 [(物品ID, 要幾個), …]。"""

    rid: int
    product: int
    product_is_skill: bool
    need_level: int
    mats: tuple[tuple[int, int], ...]
    stage: int             # +0x68：3 半成品／5 成品
    classes: tuple[int, ...] = ()   # +0x0C 起 5 格生產類別（非 0 的那些）

    @property
    def looks_semi(self) -> bool:
        """配方自己那一欄說它是半成品（跟物品分類交叉比對用）。"""
        return self.stage == STAGE_SEMI


@dataclass(frozen=True)
class Contrib:
    """公會貢獻品的一列：一組 group 個 item，可換 points 點貢獻。"""

    cid: int
    item: int
    group: int
    points: int


# ---------------------------------------------------------------------------
def _u32(scanner, addr: int):
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else None


def _ok(p) -> bool:
    return bool(p) and _PTR_LO <= p <= _PTR_HI


def _table(scanner, ptr_addr: int) -> int | None:
    """表指標 → 表本體；值不合理回 None（＝這一拍讀不到，不是空表）。"""
    tab = _u32(scanner, ptr_addr)
    return tab if _ok(tab) else None


def _record(scanner, tab: int, idx: int, size: int) -> bytes | None:
    p = _u32(scanner, tab + idx * 4)
    if not _ok(p):
        return None
    raw = scanner._read_bytes(p, size)
    return bytes(raw) if raw and len(raw) >= size else None


# ---------------------------------------------------------------------------
def recipe(scanner, rid: int) -> Recipe | None:
    """一個配方；編號超出範圍、指標不合理、讀不到都回 None。"""
    if not 1 <= rid < MAKE_MAX:
        return None
    tab = _table(scanner, MAKE_TAB)
    if tab is None:
        return None
    raw = _record(scanner, tab, rid, R_SIZE)
    if raw is None:
        return None
    product = struct.unpack_from("<I", raw, R_PRODUCT)[0]
    if not product:
        return None                        # 空格子（遊戲自己也當沒有）
    kinds = struct.unpack_from("<I", raw, R_MAT_KINDS)[0]
    mats = []
    for k in range(MAT_SLOTS):
        mid = struct.unpack_from("<I", raw, R_MAT_ID + k * 4)[0]
        num = struct.unpack_from("<I", raw, R_MAT_NUM + k * 4)[0]
        if mid and num:
            mats.append((mid, num))
    # ⚠ 材料種類數對不上就當這筆讀壞了 —— 少算一種材料會讓「做得出幾個」
    #   算多，最後卡在做不出來。寧可略過這個配方。
    if kinds and len(mats) != kinds:
        return None
    return Recipe(rid, product,
                  bool(struct.unpack_from("<I", raw, R_IS_SKILL)[0]),
                  struct.unpack_from("<I", raw, R_NEED_LEVEL)[0],
                  tuple(mats),
                  struct.unpack_from("<I", raw, R_STAGE)[0],
                  tuple(c for c in struct.unpack_from(f"<{CLASS_SLOTS}I", raw, R_CLASS)
                        if c))


def layout_ok(scanner) -> bool | None:
    """配方記錄的欄位版面還對不對。True 對／False **搬了**／None 讀不到。

    ★ 這是 disp8 偏移沒有 AOB 可跟的防線（見檔頭）。判法只用「一定成立」的
      不變量：學會的每一筆配方，旗標 +0x68 必須是 3 或 5、材料種類數 +0x64
      必須等於材料格裡非 0 的格數。版面一搬，這兩個欄位讀到的就是別的東西
      （2026-09-23 實錄：旗標全 0）。
    ⚠ 沒學任何配方的角色（戰鬥職）驗不了 → None，不是 False。
    """
    ids = learned_ids(scanner)
    if ids is None or not ids:
        return None
    tab = _table(scanner, MAKE_TAB)
    if tab is None:
        return None
    seen = 0
    for rid in ids:
        raw = _record(scanner, tab, rid, R_SIZE)
        if raw is None or not struct.unpack_from("<I", raw, R_PRODUCT)[0]:
            continue
        seen += 1
        stage = struct.unpack_from("<I", raw, R_STAGE)[0]
        kinds = struct.unpack_from("<I", raw, R_MAT_KINDS)[0]
        n = sum(1 for k in range(MAT_SLOTS)
                if struct.unpack_from("<I", raw, R_MAT_ID + k * 4)[0])
        if stage not in (STAGE_SEMI, STAGE_FULL) or kinds != n:
            return False
    return True if seen else None


def learned_ids(scanner) -> list[int] | None:
    """這個角色學會的配方編號。**讀不到回 None，不是空清單。**"""
    # ⚠ 位元圖掛在**管理員物件本身**（`[gather.WORLD_PTR]`），不是它 +8 的
    #   世界物件 —— `initmakeclasswnd` 0x58F11F 是 `mov eax,[0x9B669C]` 之後
    #   直接拿它當基準。踩過一次：走 +8 讀出來是別的東西，位元圖「學會」
    #   從 73 個變成 605 個，而且每一筆都查得到配方（廚師會出現木工配方），
    #   完全不會失敗、只會安靜地錯。
    obj = _u32(scanner, gather.WORLD_PTR)
    if not _ok(obj):
        return None
    raw = scanner._read_bytes(obj + OFF_LEARNED, MAKE_MAX // 8)
    if not raw or len(raw) < MAKE_MAX // 8:
        return None
    b = bytes(raw)
    return [rid for rid in range(1, MAKE_MAX)
            if (b[rid >> 3] >> (rid & 7)) & 1]


def item_class(scanner, item_id: int) -> int | None:
    """物品的分類（物品範本 +0x18）。讀不到回 None。"""
    if not 1 <= item_id < ITEM_MAX:
        return None
    tab = _table(scanner, ITEM_TAB)
    if tab is None:
        return None
    p = _u32(scanner, tab + item_id * 4)
    if not _ok(p):
        return None
    return _u32(scanner, p + OFF_ITEM_CLASS)


def learned(scanner) -> list[Recipe] | None:
    """學會、而且記錄讀得起來的所有配方。讀不到回 None。"""
    ids = learned_ids(scanner)
    if ids is None:
        return None
    # ★ 版面驗不過＝「讀不到」，不是「沒有」—— 舊偏移讀新版記錄會把每一筆
    #   都讀成垃圾，回空清單就是安靜做錯事（2026-09-23）。
    if layout_ok(scanner) is False:
        return None
    out = []
    for rid in ids:
        r = recipe(scanner, rid)
        if r is not None:
            out.append(r)
    return out


def semi_finished(scanner) -> list[Recipe] | None:
    """學會的配方裡，**產物是半成品**的那些。讀不到回 None。

    判定用兩個各自獨立的欄位交叉比對：
      ① 產物那個物品的分類 == 27（＝介面字串「半成品」，實機驗證過）
      ② 配方自己的 +0x58 == 3
    兩邊都說是才算 —— 只有一邊成立時寧可漏掉，也不要把成品拿去做
    （做成品會把材料吃光、又捐不進公會）。
    """
    rs = learned(scanner)
    if rs is None:
        return None
    out = []
    for r in rs:
        if r.product_is_skill or not r.looks_semi:
            continue
        if item_class(scanner, r.product) == ITEM_CLASS_SEMI:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
def contribs(scanner) -> list[Contrib] | None:
    """公會貢獻品全表。**讀不到回 None，不是空清單。**"""
    tab = _table(scanner, GC_TAB)
    count = _u32(scanner, GC_TAB + 4)
    if tab is None or count is None or not 0 < count <= GC_MAX:
        return None
    out = []
    for cid in range(1, count + 1):
        raw = _record(scanner, tab, cid, G_SIZE)
        if raw is None:
            continue
        gid, item, group, points = struct.unpack_from("<4I", raw, 0)
        # 自我驗證：編號要跟索引對得上、一組不能是 0 個
        if gid != cid or not item or not group:
            continue
        out.append(Contrib(cid, item, group, points))
    return out


def contrib_by_item(scanner) -> dict[int, Contrib] | None:
    """{物品種類ID: 貢獻品} —— 背包裡的東西反查捐不捐得掉。讀不到回 None。"""
    cs = contribs(scanner)
    if cs is None:
        return None
    return {c.item: c for c in cs}


# ---------------------------------------------------------------------------
# 「身上的資源可以做出哪些半成品、各幾個」
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Plan:
    """一個配方目前做得出幾個。"""

    recipe: Recipe
    can_make: int          # 依背包材料算出來的上限
    contrib: Contrib | None    # 產物捐不捐得進公會（None = 不能捐）


def plan(scanner, have: dict[int, int]) -> list[Plan] | None:
    """依背包現況算出「每個半成品做得出幾個」。讀不到回 None。

    have：{物品種類ID: 總數}，**呼叫端要用 `bag.scan()` 的第二個值確認整袋
    真的讀完了才准傳進來** —— 讀了半份背包會把「做得出幾個」算少，
    少做不會壞事，但把「讀不到」當成「沒材料」就會安靜地什麼都不做。

    ⚠ 材料共用：好幾個配方可能吃同一種原料（魚肉與魚鰭都吃魚），這裡算的是
      **各自單獨能做幾個**，加起來會超過實際可做量。要照順序做的話，每做完
      一批就重算一次（背包本來就是唯一的真相）。
    """
    semi = semi_finished(scanner)
    if semi is None:
        return None
    cmap = contrib_by_item(scanner) or {}
    out = []
    for r in semi:
        if not r.mats:
            continue
        can = min(have.get(mid, 0) // num for mid, num in r.mats)
        if can > 0:
            out.append(Plan(r, can, cmap.get(r.product)))
    # 捐得掉的排前面，其次做得多的 —— 使用者的目的是把負重換成貢獻。
    out.sort(key=lambda p: (p.contrib is None, -p.can_make))
    return out


def schedule(scanner, have: dict[int, int]) -> list[tuple[Recipe, int]] | None:
    """照 `plan()` 的順序把材料真的扣掉，算出「各做幾個」。讀不到回 None。

    ⚠ 跟 `plan()` 的差別很重要：`plan()` 的 `can_make` 是每個配方**單獨**算的，
      好幾個配方吃同一種原料時（魚肉與魚鰭都吃魚）加起來會**超過**實際做得
      出來的量。這支是照著要做的順序把材料扣掉，所以總和才是真的 ——
      拿來跟人說「總共要做幾個、還剩多久」只能用這支，用 `plan()` 會灌水。
    """
    ps = plan(scanner, have)
    if ps is None:
        return None
    left = dict(have)
    out: list[tuple[Recipe, int]] = []
    for p in ps:
        n = min((left.get(mid, 0) // num for mid, num in p.recipe.mats),
                default=0)
        if n <= 0:
            continue
        for mid, num in p.recipe.mats:
            left[mid] = left.get(mid, 0) - n * num
        out.append((p.recipe, n))
    return out
