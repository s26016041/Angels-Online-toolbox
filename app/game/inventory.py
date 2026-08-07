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

定位一次之後每輪只要讀兩個指標，成本趨近於零，而且換裝當下就會反映。
純讀記憶體。
"""
from __future__ import annotations

import struct
from collections import Counter

import numpy as np

# 陣列裡的格子索引
SLOT_ACCESSORY = (8, 9)      # 飾品欄兩格
EQUIP_SLOTS = 12             # 前 12 格是裝備欄，之後是空格與背包（版面說明用）

# 飾品欄的左右對應。實測：使用者把「右邊」那顆卸到背包後，正好是第 9 格變空，
# 第 8 格的球還在；使用者也確認遊戲的飾品欄就是兩格、一左一右。
SLOT_SIDE = {8: "左", 9: "右"}


def slot_side(index: int) -> str:
    """格號 → 「左」/「右」；不是飾品欄就回空字串。"""
    return SLOT_SIDE.get(index, "")

# 物品結構內的偏移
ITEM_TYPE_OFF = 0x08         # 種類 ID
ITEM_SLOT_OFF = 0x25         # ★ **物品自己記著它在第幾格**（單一 byte）
ITEM_COUNT_OFF = 0x27        # 數量（可疊物品，例如藥水 60 個）
ITEM_DURA_OFF = 0x2C         # 耐久度現值；**0 = 壞掉**（使用者確認）
# ⚠ +0x5C **不是**耐久上限。第一次量到五台都是 70 才會這樣猜，
# 但遊戲重開後同一個欄位讀到 793772102 —— 那只是剛好同階武器的某個值。
# 目前**沒有**已知的耐久上限欄位，所以只用「現值 0 = 壞掉」做判斷。
ITEM_DURA_MAX_OFF = 0x5C     # 保留供日後查證，別拿來當上限用
DURA_MAX_SANE = 1000         # 超過這個就當作讀到的不是上限
ITEM_BALL_OFF = 0xA0         # 技能經驗球的值

SLOT_WEAPON = 3              # 武器欄

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
    for p in read_pointers(scanner, head, HEAD_WINDOW):
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
            return head
    return None


def read_pointers(scanner, head: int, count: int = 128) -> list[int]:
    """一次把整條指標陣列讀進來。

    以前是「每格各發一次 ReadProcessMemory」，128 格 × 5 台 × 每 0.7 秒 —— 光這裡
    每輪就上千次系統呼叫。整條一次讀只要 1 次，其餘工作完全一樣。

    要求的範圍只要有一段沒對應到記憶體，ReadProcessMemory 就是整批失敗（不是讀一半），
    所以讀不到時逐次減半重試 —— 表尾剛好貼著區塊邊界時，至少前半段照樣讀得到，
    行為跟以前「逐格讀、讀不到就停」一致。
    """
    n = count
    while n:
        raw = scanner._read_bytes(head, n * 4)
        if raw:
            return list(struct.unpack(f"<{n}I", raw))
        n //= 2
    return []


def read_slot(scanner, head: int, index: int) -> tuple[int, int, int] | None:
    """讀某一格：回傳 (種類 ID, 物品結構位址, 球值)；空格或無效回傳 None。

    球值對非球物品沒有意義，呼叫端要自己用種類 ID 判斷。
    """
    if not head:
        return None                 # 陣列搬家時呼叫端可能還拿著 None
    p = _dword(scanner, head + index * 4)
    if not p:
        return None
    tid = item_type(scanner, p)
    if tid is None:
        return None
    raw = scanner._read_bytes(p + ITEM_BALL_OFF, 4)
    val = struct.unpack("<i", raw)[0] if raw else 0
    return tid, p, val


def scan_slots(scanner, head: int, count: int = 128):
    """走一遍整張表，回傳 [(格號, 種類 ID, 結構位址, 球值), ...]。

    用來數背包裡還有幾顆球。128 格是保守上限。
    """
    out = []
    for i, p in enumerate(read_pointers(scanner, head, count)):
        if not p:
            continue
        tid = item_type(scanner, p)
        if tid is None:
            continue
        raw = scanner._read_bytes(p + ITEM_BALL_OFF, 4)
        out.append((i, tid, p, struct.unpack("<i", raw)[0] if raw else 0))
    return out


HEAD_BACK = 24               # 校正表頭時往前多看幾格
# ★ 走整條陣列時看多少格。⚠⚠ **這個值太小會變成「假的沒有」**：
#   實測格號用到 174（白狐 172、嵐狐 174），原本寫 128 就會把放在後面的
#   藥水判成「用完了」→ 誤觸發回程補給。遊戲本身最大格號 253，取 512 留足餘裕。
#   成本很低：整段用 `read_pointers()` **一次讀進來**，不是一格發一次系統呼叫。
FULL_WINDOW = 512
# ★ 遊戲本身的最大格號是 253，所以「這條陣列我看完了」的最低標準就是
#   覆蓋到表頭後 254 格。低於這個數的讀取結果只能拿來「找東西」，
#   **不能拿來下「沒有／歸零」的結論**（見 _array_ptrs 與 count_by_types）。
SLOT_CEILING = 254


def _item_slot(scanner, ptr: int) -> int | None:
    """這個指標指向的是不是物品？是的話回傳它自己記的格號。"""
    if not (0x01000000 < ptr < 0x40000000):
        return None
    raw = scanner._read_bytes(ptr, ITEM_COUNT_OFF + 1)
    if not raw or len(raw) < ITEM_COUNT_OFF + 1:
        return None
    tid = struct.unpack_from("<I", raw, ITEM_TYPE_OFF)[0]
    if not 0 < tid < MAX_ITEM_ID:
        return None
    slot = raw[ITEM_SLOT_OFF]
    return slot if slot < 200 else None


def _item_row(scanner, ptr: int) -> tuple[int, int, int] | None:
    """一次讀出 (格號, 種類 ID, 數量)；不像物品就回 None。

    ★ 判定條件跟 `_item_slot()` 一模一樣，只是**順便把數量一起帶回來**。
      `_walk()` 以前是先叫 `_item_slot()` 讀 0x28 bytes，再自己重讀 0x29 bytes
      —— 同一塊記憶體讀兩次，每件物品都白花一次系統呼叫。
    """
    if not (0x01000000 < ptr < 0x40000000):
        return None
    raw = scanner._read_bytes(ptr, ITEM_COUNT_OFF + 2)
    if not raw or len(raw) < ITEM_COUNT_OFF + 2:
        return None
    tid = struct.unpack_from("<I", raw, ITEM_TYPE_OFF)[0]
    if not 0 < tid < MAX_ITEM_ID:
        return None
    slot = raw[ITEM_SLOT_OFF]
    if slot >= 200:
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
      裝備檢查光這裡就上千次系統呼叫。整段讀不齊時（表頭貼著區段邊界）
      **原封不動退回舊的逐格讀法**，結果完全一樣。
    """
    if not head:
        return head
    start = head - HEAD_BACK * 4
    total = HEAD_BACK + window
    ptrs = read_pointers(scanner, start, total)
    if len(ptrs) != total:
        # 整段讀不齊 —— 退回逐格讀，跟以前一字不差（含中間有洞的情形）
        votes: Counter = Counter()
        for i in range(-HEAD_BACK, window):
            a = head + i * 4
            p = _dword(scanner, a)
            slot = _item_slot(scanner, p) if p else None
            if slot is not None:
                votes[a - slot * 4] += 1
        return votes.most_common(1)[0][0] if votes else head

    votes = Counter()
    for k, p in enumerate(ptrs):
        if not p:
            continue
        slot = _item_slot(scanner, p)
        if slot is not None:
            votes[start + k * 4 - slot * 4] += 1
    return votes.most_common(1)[0][0] if votes else head


def find_by_slot(scanner, head: int, slot: int,
                 window: int = HEAD_WINDOW) -> int | None:
    """找出「自己說它在第 slot 格」的物品，回傳物件位址；沒有回傳 None。

    ★ 為什麼不直接用 `read_pointers(head)[slot]`：
      表頭有可能挑偏（實測五台裡有一台偏了 5 格，結果拿飾品當武器讀）。
      這裡先用 align_head() 依格號校正，再認格號取值，完全不受表頭影響。
      驗證：飾品確實落在 8、9 格，跟既有的 SLOT_ACCESSORY 完全吻合。
    """
    base = align_head(scanner, head, window)
    if not base:
        return None                 # 陣列搬家時呼叫端可能還拿著 None
    ptrs = read_pointers(scanner, base, window)
    if len(ptrs) != window:
        ptrs = None                 # 讀不齊就逐格讀（跟以前一樣）
    for i in range(window):
        p = ptrs[i] if ptrs is not None else _dword(scanner, base + i * 4)
        if p and _item_slot(scanner, p) == slot:
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


def _array_ptrs(scanner, base: int, window: int = FULL_WINDOW
                ) -> tuple[list[int], int]:
    """讀整條指標陣列，回傳 (指標清單, 覆蓋到表頭後第幾格)。

    ⚠⚠ **表頭前面那 24 格可能整段沒有映射**（陣列剛好貼著記憶體區段的開頭）。
      ReadProcessMemory 對這種範圍是**整批失敗**，不是讀一半 ——
      而 `read_pointers` 的減半重試救不了「開頭就讀不到」，因為起點沒變。
      結果是整條陣列讀出來是空的：藥水全變 0、天使之翼也變 0
      → 「水沒了」+「沒有回程道具」→ 掛機被停掉。
      使用者實際遇到（黑狐，表頭 0x3ac35008，往前 96 bytes 就是未映射）。
      所以讀不到就退一步，從表頭本身開始讀。
    ⚠⚠ 反過來**尾端貼著區段結尾**時，減半重試會「成功但被截斷」——
      只剩前面一小段、後面的格子整批看不見，而且沒有任何錯誤。
      所以把「覆蓋到第幾格」一起回報：不足 SLOT_CEILING 就代表這次
      沒看完整條，「沒數到」不能當「沒有」（見 count_by_types）。
      這正是當年 FULL_WINDOW=128 太小 → 假的沒有 那個 bug 的動態翻版，
      陣列搬家搬到貼區段尾端的位置就會發作。
    """
    start = base - HEAD_BACK * 4
    ptrs = read_pointers(scanner, start, HEAD_BACK + window)
    covered = len(ptrs) - HEAD_BACK
    if len(ptrs) < HEAD_BACK + window and covered < SLOT_CEILING:
        # 讀不齊而且覆蓋不夠 → 從表頭本身重讀一次，哪邊看得遠用哪邊
        alt = read_pointers(scanner, base, window)
        if len(alt) > covered:
            return alt, len(alt)
    return ptrs, covered


def _walk(scanner, head: int, window: int = FULL_WINDOW):
    """走整條物品陣列，逐一吐出 (格號, 種類ID, 數量, 物件位址)。

    ★ 指標**一次批次讀進來**（`read_pointers`），所以掃 512 格跟掃 128 格
      的成本差不多 —— 沒有理由為了省時間去縮範圍。
    ⚠ 只認「物品自己記得住格號」的（`_item_slot`），過濾掉陣列外圍的殘留指標。
    ⚠ 讀取被截斷時這裡**照樣吐出**讀得到的那段（拿來找東西沒問題）——
      要下「沒有／歸零」結論的請改用 `count_by_types()`，它會回報完整性。
    """
    if not head:
        return
    base = align_head(scanner, head)
    if not base:
        return
    ptrs, _covered = _array_ptrs(scanner, base, window)
    for p in ptrs:
        if not p:
            continue
        got = _item_row(scanner, p)      # 格號＋種類＋數量一次讀完
        if got is None:
            continue
        slot, tid, cnt = got
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
      加總出來的 0 可能只是「後面那段沒看到」。實測藥水疊到第 174 格、
      遊戲最大格號 253，覆蓋不到 SLOT_CEILING 格就不能說東西不在。
    ★ 有數到的部分（>0）照樣可信 —— 截斷只會少看，不會多看。
    """
    total = dict.fromkeys(type_ids, 0)
    if not head:
        return total, False
    base = align_head(scanner, head)
    if not base:
        return total, False
    ptrs, covered = _array_ptrs(scanner, base)
    for p in ptrs:
        if not p:
            continue
        got = _item_row(scanner, p)
        if got is not None and got[1] in total:
            total[got[1]] += got[2]
    return total, covered >= SLOT_CEILING


def durability(scanner, head: int) -> tuple[int, int] | None:
    """武器的 (耐久現值, 上限)；找不到武器回傳 None。**現值 0 = 壞掉。**

    上限回傳 0 代表「不知道」——`+0x5C` 曾經看起來像上限（五台都是 70），
    但重開遊戲後讀到 793772102，證實不是。判斷只用現值。
    """
    ptr = find_by_slot(scanner, head, SLOT_WEAPON)
    if not ptr:
        return None
    raw = scanner._read_bytes(ptr, ITEM_DURA_MAX_OFF + 4)
    if not raw or len(raw) < ITEM_DURA_MAX_OFF + 4:
        return None
    cur = struct.unpack_from("<i", raw, ITEM_DURA_OFF)[0]
    mx = struct.unpack_from("<i", raw, ITEM_DURA_MAX_OFF)[0]
    if not 0 < mx <= DURA_MAX_SANE or mx < cur:
        mx = 0                      # 讀到的顯然不是上限，就別顯示
    return cur, mx


def is_valid(scanner, head: int) -> bool:
    """表頭還有效嗎？（陣列會搬家：換地圖、重連、物品增減都可能重新配置）

    ⚠ 這裡**必須用完整的門檻**（64 格裡要有 12 個有效物品），不能為了省讀取改成
    「前幾格有東西就算數」。實測過的反例：黑狐的陣列從 0x365A31D0 搬到 0x37EC6BA8
    之後，舊位址那塊記憶體還讀得到，而且**還殘留兩個像物品的指標**（第 2 格、第 9 格）
    —— 寬鬆的檢查會通過，程式就一直讀那份死掉的副本，畫面變成
    「左邊整格不見、右邊非經驗球」。嚴格門檻對同一塊記憶體回傳 False，正確。

    成本本來就不高：整條指標一次讀進來，數到 12 個就提早收工。
    """
    return bool(head) and _looks_like_inventory(scanner, head)
