"""物品欄：直接讀「哪一格裝了什麼」，不必再靠行為猜。

為什麼需要這個
--------------
原本判斷「哪幾顆球是裝備中」是靠「練功時它會增加」——那只是行為推論，不準：
球一滿就不再增加、剛裝上的球還沒開始漲、記憶體裡的舊副本也會混進來。
玩家快速換飾品時甚至會出現張冠李戴（35000 的球配到 120000 的上限）。

實際上遊戲有一張**物品指標陣列**，裝備欄與背包都在裡面，直接讀就是精確答案。

版面（跨 5 個分身驗證一致）
---------------------------
    陣列表頭 +0x00 起是裝備欄，每格 4 bytes 是一個指標（0 = 該格沒裝東西）
      第 8 格（+0x20）＝ 飾品欄 1
      第 9 格（+0x24）＝ 飾品欄 2
    第 12 格之後是空的裝備格，再往後（實測 +0x50 起）是背包。

    指標指向物品結構：
      結構 +0x08 = 種類 ID（就是 items.py 那張表的鍵）
      結構 +0xA0 = 技能經驗球的累積值（也就是 AOB 特徵找到的那個位址）

    ★★ 物品結構的**欄位表在 `bag.py`**（從遊戲綁給 Lua 的 `ItemData` 方法表
       逐支反組譯出來的），這支只借用、不自己維護一份 —— 兩份各自寫死就會
       各自過期：這裡曾把格號當 1 byte 讀，於是裝扮隨身包第 264 格
       （264 & 0xFF = 8）被當成飾品欄左邊，收益監控畫出「兩個左邊飾品」。

怎麼定位這張表
--------------
用 AOB 找到任一顆球的結構位址後，反向搜「誰精確指著它」——那個位置就是它所在的
格子，再往前掃就到表頭。

⚠ 往前掃很容易多退幾格：空格的內容是 0，而陣列前面的空白也是 0，兩者長得一樣。
多退一格，之後所有格號就整排位移，飾品欄（第 8、9 格）會對到裝備欄的別的東西
——使用者實際遇過「兩顆一樣的球，右邊正常、左邊卻顯示非經驗球」。
所以表頭是這樣定案的：
  1. 刷掉不是物品的候選（走過頭會停在空白上）
  2. 看**誰被別的結構指著**：實測真表頭被指 1～9 次，往前每一格都是 0 次
  3. 最後再確認前 64 格裡有夠多有效物品指標（真表有 90 幾個，巧合命中只有一兩個）
  4. ★ 回傳前再用 `align_head()` 依「物品自己記的格號」（+0x25，**u16**）校正 ——
     前三步在少數機台仍會偏格（實測偏過 5 格與 6 格），偏了的表頭每一格都
     讀得到「像對的東西」，只是整排錯位：飾品欄讀成空格、裝備中的球被當成
     背包裡的。所以**任何拿格號當意義的讀取，一律認物品自記的格號**
     （`_walk()` / `find_by_slot()`），不信陣列索引。

定位一次之後每輪只要幾次批次讀取，成本趨近於零，而且換裝當下就會反映。
純讀記憶體。
"""
from __future__ import annotations

import struct
from collections import Counter

import numpy as np

from app.game import bag

# 陣列裡的格子索引
SLOT_ACCESSORY = (8, 9)      # 飾品欄兩格
EQUIP_SLOTS = 12             # 前 12 格是裝備欄，之後是空格與背包（版面說明用）

# 飾品欄的左右對應。實測：使用者把「右邊」那顆卸到背包後，正好是第 9 格變空，
# 第 8 格的球還在；使用者也確認遊戲的飾品欄就是兩格、一左一右。
SLOT_SIDE = {8: "左", 9: "右"}


def slot_side(index: int) -> str:
    """格號 → 「左」/「右」；不是飾品欄就回空字串。"""
    return SLOT_SIDE.get(index, "")

# 物品結構內的偏移 —— ★★ **一律借 `bag.py` 那一份，這裡不留第二份。**
#   兩份各自寫死就會各自過期，而且不會有人警告：這支原本把格號當 **1 byte** 讀
#   （`bag.py` 早就從遊戲自己的 `getslot`＝`movzx eax, word [this+0x25]` 確認是
#   u16），於是裝扮隨身包第 264 格（264 & 0xFF = 8）被當成飾品欄左邊 ——
#   使用者實機遇到「雪狐變成兩個左邊飾品」（水之聖光城，種類 4944，
#   2026-08-08 五台實測只有雪狐中招）。同一類錯還會把第 259 格讀成武器欄。
ITEM_TYPE_OFF = bag.ITEM_TYPE      # 種類 ID
ITEM_SLOT_OFF = bag.ITEM_SLOT      # ★ 物品自己記著它在第幾格（**u16**）
ITEM_COUNT_OFF = bag.ITEM_COUNT    # 數量（可疊物品，例如藥水 60 個）
ITEM_BALL_OFF = bag.ITEM_ENERGY    # 技能經驗球的值（＝遊戲的 getenergy）
# 格號的合理上限：超過就是讀到垃圾，不是真的格子。★ 不可以再用「< 200」那種
# 只裝得下 1 byte 的門檻 —— 裝扮隨身包本來就排到 251 以後。
SLOT_SANE_MAX = bag.MAX_SLOTS
# 一次讀多少 bytes 才涵蓋（種類 ID, 格號 u16, 數量 u16）
ROW_SPAN = bag.ITEM_COUNT + 2

# 判定表頭用：前這麼多格裡至少要有這麼多個有效物品指標
HEAD_WINDOW = 64
HEAD_MIN_ITEMS = 12
MAX_ITEM_ID = 200_000


def _dword(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<I", raw)[0]


def item_type(scanner, ptr: int) -> int | None:
    """這個指標指到的像不像一個物品結構？是的話回傳種類 ID。"""
    if not (0x10000 < ptr < 0x7FFF0000):
        return None
    raw = scanner._read_bytes(ptr + ITEM_TYPE_OFF, 4)
    if not raw:
        return None
    tid = struct.unpack("<i", raw)[0]
    return tid if 0 < tid < MAX_ITEM_ID else None


def _head_candidates(scanner, slot: int) -> list[int]:
    """從某一格往前掃，把沿路每個「可能是表頭」的位址都收集起來。

    ⚠ 不能只取最前面那個：**空格（0）也算陣列的一部分**，所以往前掃會一路走過
    陣列前面的零，多退幾格。多退一格就會讓所有格號位移，飾品欄（第 8、9 格）
    就對到裝備欄的別的東西 —— 使用者實際遇過「左邊顯示非經驗球、右邊正常」。
    所以收集候選之後要再用「表頭本身必須放著一個物品」＋「有指標指著它」挑。
    """
    out = []
    at = slot
    while at > 0x10000 and slot - at < 0x1000:
        p = _dword(scanner, at - 4)
        if p is None or (p != 0 and item_type(scanner, p) is None):
            break
        at -= 4
        out.append(at)
    return out


def _solid(scanner, cands: list[int]) -> list[int]:
    """候選裡「那一格真的放著物品」的那些。

    走過頭時會停在陣列前面的空白上，那些格子是 0（不是物品），先刷掉。
    """
    return [a for a in cands
            if item_type(scanner, _dword(scanner, a) or 0) is not None]


def _vote_for_head(scanner, cands: list[int], near: int) -> dict[int, int]:
    """數每個候選各被幾個指標指著 —— 真正的表頭會被別的結構持有，陣列中間不會。

    實測五個分身：真表頭被指 1～9 次，往前每一格都是 **0 次**，判別力乾淨。

    成本控制：候選全擠在 4KB 內，所以先用範圍比較刷掉 99.99%，再逐一比對；
    而且先掃「陣列自己所在的那塊」，那裡通常就找得到持有者，找到就收工，
    找不到才把整個程序讀完。
    """
    lo, hi = min(cands), max(cands)
    votes = {a: 0 for a in cands}
    regions = list(scanner._iter_regions(writable_only=True))
    # 把陣列所在的那塊排到最前面
    regions.sort(key=lambda r: not (r[0] <= near < r[0] + r[1]))
    for base, size in regions:
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        arr = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")
        idx = np.flatnonzero((arr >= lo) & (arr <= hi))
        for v in arr[idx] if idx.size else ():
            if int(v) in votes:
                votes[int(v)] += 1
        if any(votes.values()):
            break                       # 已經有人被指名，不必再讀下去
    return votes


def _pick_head(scanner, slot: int) -> int | None:
    """從某一格回推真正的表頭；這一格不在物品陣列裡就回 None。

    順序是刻意的：
      1. 刷掉不是物品的候選（走過頭會停在陣列前面的空白上）
      2. 先用最前面那個當草稿，確認「這裡真的是物品陣列」——不是的話直接放棄，
         省下第 3 步的掃描（反查會找到不少指向同一件物品、但不在陣列裡的指標）
      3. 再用「誰被指著」把表頭釘到正確那一格
    """
    solid = _solid(scanner, _head_candidates(scanner, slot))
    if not solid:
        return None
    rough = min(solid)
    if not _looks_like_inventory(scanner, rough):
        return None
    if len(solid) == 1:
        return rough
    votes = _vote_for_head(scanner, solid, near=slot)
    best = max(votes, key=lambda a: (votes[a], -a))
    return best if votes[best] else rough


def _looks_like_inventory(scanner, head: int) -> bool:
    """真的物品陣列前 64 格會塞滿有效指標；巧合命中的只有零星一兩個。"""
    n = 0
    for p in bag.read_ptrs(scanner, head, HEAD_WINDOW)[0]:
        if p and item_type(scanner, p) is not None:
            n += 1
            if n >= HEAD_MIN_ITEMS:
                return True
    return False


def locate(scanner, item_structs) -> int | None:
    """用已知的物品結構位址反向找出物品陣列表頭；找不到回傳 None。

    item_structs: 可疊代的物品結構起點（技能球的話 = AOB 命中位址 - ITEM_BALL_OFF）。
    需要全記憶體掃一次（跟 AOB 掃描同一個等級的成本），所以只在還沒定位、
    或既有表頭失效時才呼叫。

    ★ 回傳前用 `align_head()` 依物品自記格號校正 —— `_pick_head()` 的投票在
      少數機台會挑偏 5～6 格（見模組說明第 4 點與 recall.py 的實錄）。
    """
    targets = np.array(sorted(set(item_structs)), dtype="<u4")
    if targets.size == 0:
        return None
    lo, hi = targets[0], targets[-1]
    slots: list[int] = []
    for base, size in scanner._iter_regions(writable_only=True):
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        arr = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")
        # 先用範圍刷掉絕大多數，再比對；等價於 np.isin，但不隨目標數變慢
        idx = np.flatnonzero((arr >= lo) & (arr <= hi))
        if idx.size:
            slots.extend(base + int(i) * 4 for i in idx[np.isin(arr[idx], targets)])
    for at in sorted(slots):
        head = _pick_head(scanner, at)
        if head is not None:
            return align_head(scanner, head)
    return None


# ⛔ 這裡以前有一支自己的 `read_pointers()`：讀不到就把長度**減半重試**，
#   結果是「成功但被截斷」—— 尾端貼著區段結尾時後面的格子整批看不見，
#   而且開頭讀不到的話（起點沒變）連減半都救不了。
#   2026-08-08 全部改用 `bag.read_ptrs()`：對半切之後**兩半都試**，
#   所以只有真的讀不到的那一格會變 0，其餘照收，並回報整段完不完整。
#   ★ 全專案讀指標陣列只剩那一支，兩邊行為不會再各走各的。


def ball_value(scanner, ptr: int) -> int:
    """物品結構 → 技能經驗球的累積值。非球物品讀出來沒有意義，呼叫端自己判斷。

    ⚠ 這裡刻意**只收物品結構位址、不收格號** —— 以前有 `read_slot(head, 索引)`
      這種拿陣列索引讀格子的路徑，表頭一偏格就整排錯位（裝備中的球被讀成
      背包裡的）。要按格號拿東西一律走 `_walk()` / `find_by_slot()`。
    """
    raw = scanner._read_bytes(ptr + ITEM_BALL_OFF, 4)
    return struct.unpack("<i", raw)[0] if raw else 0


HEAD_BACK = 24               # 校正表頭時往前多看幾格（只有 align_head 用）
# ★ 走整條陣列時看多少格。⚠⚠ **這個值太小會變成「假的沒有」**：
#   實測格號用到 174（白狐 172、嵐狐 174），原本寫 128 就會把放在後面的
#   藥水判成「用完了」→ 誤觸發回程補給。
#   ⛔ 舊註解寫「遊戲本身最大格號 253」是錯的：容器實測**五台都是 743 格**
#     （`bag.head()`），而且 251 起的裝扮隨身包真的用到 264（雪狐的水之聖光城）。
#     所以窗口開到蓋得住整個容器，寧可多讀 1KB。
#   成本很低：整段用 `read_pointers()` **一次讀進來**，不是一格發一次系統呼叫。
FULL_WINDOW = 768
# ★ 「這條陣列我看完了」的最低標準。**不是陣列長度**，是「可疊消耗品可能待的
#   最後一格」：背包／角色格都在 254 以內（實測藥水最遠 174），251 起的裝扮
#   隨身包只放紙娃娃，不會有藥水或回程道具。覆蓋不到這個數的讀取結果只能拿來
#   「找東西」，**不能拿來下「沒有／歸零」的結論**（見 count_by_types）。
SLOT_CEILING = 254


def _slot_of(raw) -> int | None:
    """物品前 ROW_SPAN bytes → 它自己記的格號（**u16**，見 ITEM_SLOT_OFF）。"""
    slot = struct.unpack_from("<H", raw, ITEM_SLOT_OFF)[0]
    return slot if slot < SLOT_SANE_MAX else None


def _item_slot(scanner, ptr: int) -> int | None:
    """這個指標指向的是不是物品？是的話回傳它自己記的格號。"""
    if not (0x01000000 < ptr < 0x40000000):
        return None
    raw = scanner._read_bytes(ptr, ROW_SPAN)
    if not raw or len(raw) < ROW_SPAN:
        return None
    tid = struct.unpack_from("<I", raw, ITEM_TYPE_OFF)[0]
    if not 0 < tid < MAX_ITEM_ID:
        return None
    return _slot_of(raw)


def _item_row(scanner, ptr: int) -> tuple[int, int, int] | None:
    """一次讀出 (格號, 種類 ID, 數量)；不像物品就回 None。

    ★ 判定條件跟 `_item_slot()` 一模一樣，只是**順便把數量一起帶回來**。
      `_walk()` 以前是先叫 `_item_slot()` 讀一次，再自己重讀一次
      —— 同一塊記憶體讀兩次，每件物品都白花一次系統呼叫。
    """
    if not (0x01000000 < ptr < 0x40000000):
        return None
    raw = scanner._read_bytes(ptr, ROW_SPAN)
    if not raw or len(raw) < ROW_SPAN:
        return None
    tid = struct.unpack_from("<I", raw, ITEM_TYPE_OFF)[0]
    if not 0 < tid < MAX_ITEM_ID:
        return None
    slot = _slot_of(raw)
    if slot is None:
        return None
    return slot, tid, struct.unpack_from("<H", raw, ITEM_COUNT_OFF)[0]


def align_head(scanner, head: int, window: int = HEAD_WINDOW) -> int:
    """用「物品自己記的格號」校正表頭位置。

    ★ 每個物品物件的 +0x25 就是它在第幾格。所以對每個看起來像物品的指標，
      「這個指標的位址 - 格號*4」都會指向同一個表頭 —— 取最多票的就是答案。
      這比靠數量門檻猜可靠得多：實測五台裡有一台原本偏了 5 格
      （往後偏，所以連往後掃都找不到第 3 格的武器）。

    找不到任何物品時回傳原值，不做無謂的更動。

    ★ 指標**一次批次讀進來**：以前是 88 格各發一次 ReadProcessMemory，
      而這支被 `_walk()`／`find_by_slot()` 每次呼叫都會用到，掛機每 3 秒的
      裝備檢查光這裡就上千次系統呼叫。
      ⚠ 表頭貼著區段邊界時讀不到的**只有那幾格**（`bag.read_ptrs` 會對半切
        把讀得到的撿回來，讀不到的填 0），投票照樣進行 —— 以前那條「整段
        讀不齊就退回逐格讀」的備援因此不必存在了（那正是它在做的事）。
    """
    if not head:
        return head
    start = head - HEAD_BACK * 4
    total = HEAD_BACK + window
    ptrs, _complete = bag.read_ptrs(scanner, start, total)

    votes: Counter = Counter()
    for k, p in enumerate(ptrs):
        if not p:
            continue
        slot = _item_slot(scanner, p)
        if slot is not None:
            votes[start + k * 4 - slot * 4] += 1
    return votes.most_common(1)[0][0] if votes else head


def find_by_slot(scanner, head: int, slot: int) -> int | None:
    """找出「自己說它在第 slot 格」的物品，回傳物件位址；沒有回傳 None。

    ★ 為什麼不直接用 `read_pointers(head)[slot]`：
      表頭有可能挑偏（實測五台裡有一台偏了 5 格，結果拿飾品當武器讀）。
      走 `_walk()` 就是先校正表頭、再認物品自記的格號，完全不受表頭影響。
      驗證：飾品確實落在 8、9 格，跟既有的 SLOT_ACCESSORY 完全吻合。
    ⚠ 以前這裡自己走一遍陣列，而且只看前 64 格 —— 放在第 174 格的東西
      永遠找不到（＝假的沒有）。現在直接借 `_walk()`，範圍就是整個容器。
    """
    for s, _tid, _cnt, p in _walk(scanner, head):
        if s == slot:
            return p
    return None


def find_by_type(scanner, head: int, type_id: int
                 ) -> tuple[int, int, int] | None:
    """挑**一格**這個種類的東西來用；回傳 (格號, 物件位址, 那一格的數量)。

    ⚠⚠ 第三個值是**那一格**的數量，不是總數 —— 可疊物品會散在好幾格。
      要總數請用 `count_by_type()`（見那裡的黑狐 7667 個實例）。
    這支的用途是「挑一格來用掉」，例如回程道具要傳格號給遊戲函式。

    ★★ 格號取自**物品自己的 `+0x25`**，不是陣列索引 —— `locate()` 找到的表頭
      有機會偏格（實測偏過 5 格與 6 格），拿索引去打封包就會用到別的東西。
      實際踩過：回程道具被讀成「第 48 格的 3966」，其實 3966 在第 54 格，
      真正的回程道具是 1905。要打封包的格號一律走這裡。

    ⚠ 表頭是 None 就直接回 None —— 陣列會搬家（換地圖、撿東西都會），
      呼叫端拿到的可能是上一輪的值。
    """
    for slot, tid, cnt, p in _walk(scanner, head):
        if tid == type_id:
            return slot, p, cnt
    return None


def _resolve(scanner, head: int) -> tuple[int, int | None] | None:
    """把呼叫端給的表頭變成「這一拍真的可以走的 (表頭, 格數)」；不可信就回 None。

    ★★ 跟**遊戲自己的容器表頭**對帳：`bag.head()` 走的是遊戲取背包那條路
      （實體 +0x2FC 的 vector，+0x04 頭 +0x08 尾），所以表頭與格數都是遊戲記的。
      ✅ 2026-08-08 五台實測：AOB 反查 + `align_head()` 的結果跟它**完全一致**。

    ★★ 2026-08-08 改：問得到遊戲的容器就**直接用它**，不再拿呼叫端的表頭
      去比對、比不過就整個放棄。理由是專案鐵則的第一條 —— 遊戲自己載進記憶體
      的資料優先於我們推出來的：`bag.head()` 是這一拍現讀的（實體 +0x2FC 的
      vector，頭與格數都是遊戲記的），呼叫端那個表頭則可能是好幾秒前定位的。
      兩邊不一致＝呼叫端的過期了，這時候正確答案是**用新的**，
      不是「什麼都不做」（舊行為：陣列一搬家就整段空窗，藥水／回程道具
      判斷全部放棄，看起來就是「明明讀得到卻說找不到」）。
      ✅ 五台實測兩邊本來就完全一致（見 align_head 的對帳）。

    ⚠ 問不到容器（還沒進場、換地圖中、MGR_PTR 定位失敗）才退回呼叫端的表頭
      ＋保守窗口 —— 這是 AOB 那條獨立的路，兩條同時斷才會真的讀不到。
    """
    try:
        got = bag.head(scanner)
    except Exception:                                        # noqa: BLE001
        got = None
    if got is not None:
        return got                   # (表頭, 格數) 都是遊戲自己說的
    base = align_head(scanner, head) if head else 0
    return (base, None) if base else None


def _array_ptrs(scanner, base: int, total: int | None,
                window: int = FULL_WINDOW) -> tuple[list[int], bool]:
    """讀整條指標陣列（**從表頭本身開始**），回傳 (指標清單, 整條看完了嗎)。

    ⚠⚠ 以前是從「表頭前 24 格」開始讀，那 24 格**不屬於這條陣列**，兩種病都出過：

      · 表頭剛好貼著記憶體區段的開頭時，前面那 24 格沒有映射，
        ReadProcessMemory 對這種範圍是**整批失敗**（不是讀一半），而當年那套
        減半重試救不了「開頭就讀不到」（起點沒變）。
        結果整條陣列讀出來是空的：藥水全變 0、天使之翼也變 0 →
        「水沒了」＋「沒有回程道具」→ 掛機被停掉（黑狐，表頭 0x3ac35008，
        往前 96 bytes 就是未映射）。
      · 反過來，那 24 格**讀得到的時候更麻煩**：裡面可能躺著看起來完全正常的
        殘留物品指標。2026-08-08 實測北極狐第 -23 格穩定放著一個
        「1 級腐敗肉片」，自稱第 21 格 —— 撈進來就是憑空多一件：
        飾品欄可能多一顆（使用者看到的「兩個左邊飾品」就是同一類症狀）、
        藥水可能多一疊、`find_by_type` 可能把回程道具的格號挑到那一件身上
        （送錯格＝安靜地做錯事，本專案一律當 bug）。

      表頭前面本來就沒有東西該讀，所以直接從表頭開始，兩種病一起絕根。

    ⚠⚠ 陣列的**尾端**同樣不能猜：讀過尾端就會撈到堆積裡的垃圾。實測黑狐容器
      743 格，窗口開到 768 就多撈出 3 筆長得很正常的東西（見習手斧、粗糙的小餅乾，
      都自稱**第 0 格**＝金幣格，其中一筆還重複兩次）。所以格數是**遊戲自己說的**
      （`total`，由 `_resolve()` 問來），有就照它收手。

    ⚠⚠ 尾端貼著區段結尾時，讀不到的那幾格會是 0 而不是報錯（`bag.read_ptrs`
      已經把讀得到的都撿回來了）。所以第二個回傳值是「整條看完了嗎」：
      沒看完的話「沒數到」不能當「沒有」（見 count_by_types）。
      這正是當年 FULL_WINDOW=128 太小 → 假的沒有 那個 bug 的動態翻版。
    """
    n = window if total is None else min(window, total)
    ptrs, complete = bag.read_ptrs(scanner, base, n)
    # 「看完」要兩個條件同時成立：① 每一格都真的讀到了（complete）
    # ② 範圍蓋得夠遠 —— 問到格數就照遊戲說的整條，問不到就退回「可疊消耗品
    #    待得住的最後一格」這個保守標準（見 SLOT_CEILING）。
    return ptrs, complete and n >= (total if total is not None
                                    else SLOT_CEILING)


def _walk(scanner, head: int, window: int = FULL_WINDOW):
    """走整條物品陣列，逐一吐出 (格號, 種類ID, 數量, 物件位址)。

    ★ 指標**一次批次讀進來**（`read_pointers`），所以走完整條 743 格跟只看
      128 格的成本差不多 —— 沒有理由為了省時間去縮範圍。
    ⚠ 範圍是**表頭本身到遊戲說的容器尾端**（見 `_array_ptrs`）：往前一格、
      往後一格都不是這條陣列的，那裡的殘留指標會變成憑空多出來的物品。
    ⚠ 只認「物品自己記得住格號」的（`_item_slot`），過濾掉不是物品的殘留指標。
    ⚠ 讀取被截斷時這裡**照樣吐出**讀得到的那段（拿來找東西沒問題）——
      要下「沒有／歸零」結論的請改用 `count_by_types()`，它會回報完整性。
    ⚠ 表頭跟遊戲的容器對不上時**什麼都不吐**（見 `_resolve()`）。
    """
    got = _resolve(scanner, head)
    if got is None:
        return
    base, total = got
    ptrs, _complete = _array_ptrs(scanner, base, total, window)
    for p in ptrs:
        if not p:
            continue
        row = _item_row(scanner, p)      # 格號＋種類＋數量一次讀完
        if row is None:
            continue
        slot, tid, cnt = row
        yield slot, tid, cnt, p


def count_by_type(scanner, head: int, type_id: int) -> int:
    """某個種類**全部加起來**有幾個。

    ⚠⚠ **數量一定要用這支，不要拿 `find_by_type()` 的第三個值。**
      可疊物品會**散在好幾格**：實測黑狐的補魔藥水（種類 4837）分在
      第 51/52/53/55/58/70/75/76 格 —— 667 + 1000×7 = 7667，
      而 `find_by_type()` 只會回第一格的 667。使用者一眼就看出數字不對。
    """
    return sum(cnt for _s, tid, cnt, _p in _walk(scanner, head)
               if tid == type_id)


def count_by_types(scanner, head: int, type_ids
                   ) -> tuple[dict[int, int], bool]:
    """好幾種一起數總數，回傳 ({種類: 總數}, 整條走完了嗎)。

    ⚠⚠ 要下「歸零／沒有」結論的一定要用這支、**而且要看第二個值** ——
      `_walk()` 對截斷（陣列尾端貼著區段結尾，見 _array_ptrs）不吭聲，
      加總出來的 0 可能只是「後面那段沒看到」。實測藥水疊到第 174 格，
      沒把整條看完就不能說東西不在。
    ★ 有數到的部分（>0）照樣可信 —— 截斷只會少看，不會多看。
    """
    total = dict.fromkeys(type_ids, 0)
    got = _resolve(scanner, head)
    if got is None:
        return total, False           # 表頭失效／對不上 → 不准下「沒有」的結論
    base, slots = got
    ptrs, complete = _array_ptrs(scanner, base, slots)
    for p in ptrs:
        if not p:
            continue
        got = _item_row(scanner, p)
        if got is not None and got[1] in total:
            total[got[1]] += got[2]
    return total, complete


# ⛔ 這裡以前有一支 `durability()`（讀武器欄 +0x2C 的耐久、+0x5C 當上限）。
#   已經刪掉：沒有任何呼叫端，而且它讀的兩個欄位都被 `bag.py` 推翻了 ——
#   耐久現值是 **u16**（+0x2E 是時限），耐久上限在**範本** +0xDC（拿 item*.xml
#   對帳 32476 筆 100% 吻合）。要判斷裝備壞掉請用 `bag.worn_broken()`。


def is_valid(scanner, head: int) -> bool:
    """表頭還有效嗎？（陣列會搬家：換地圖、重連、物品增減都可能重新配置）

    ⚠ 這裡**必須用完整的門檻**（64 格裡要有 12 個有效物品），不能為了省讀取改成
    「前幾格有東西就算數」。實測過的反例：黑狐的陣列從 0x365A31D0 搬到 0x37EC6BA8
    之後，舊位址那塊記憶體還讀得到，而且**還殘留兩個像物品的指標**（第 2 格、第 9 格）
    —— 寬鬆的檢查會通過，程式就一直讀那份死掉的副本，畫面變成
    「左邊整格不見、右邊非經驗球」。嚴格門檻對同一塊記憶體回傳 False，正確。

    成本本來就不高：整條指標一次讀進來，數到 12 個就提早收工。

    ★★ 2026-08-08：**遊戲自己的容器讀得到時直接算有效** —— 這支是拿來判斷
      「等一下走陣列走得成嗎」，而 `_walk()` 在那種情況下走的根本是遊戲的容器
      （見 `_resolve`），跟呼叫端這個表頭新不新無關。以前不分這一層，
      呼叫端的表頭一過期就算「無效」→ 掛機那邊的「藥水歸零／有沒有回程道具」
      直接放棄判斷（安全但不做事）。現在讀得到就照走，找得到就是找得到。
    """
    try:
        if bag.head(scanner) is not None:
            return True
    except Exception:                                        # noqa: BLE001
        pass
    return bool(head) and _looks_like_inventory(scanner, head)
