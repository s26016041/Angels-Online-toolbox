"""背包：直接讀遊戲自己的物品容器。

    for it in bag.items(scanner):
        print(it.slot, it.name, it.count, it.price)

跟 `inventory.py` 有什麼不一樣
------------------------------
`inventory.py` 是用「經驗球 AOB → 反查誰指著它 → 往前找表頭」推出來的，
所以有兩個先天限制：**身上沒有球就定位不到**，而且表頭實測會偏 5~6 格
（要再 `align_head()` 校正）。

這一支是照**遊戲自己取背包的那條路**走的，沒有猜的成分：

    [quickbar.MGR_PTR] + 0x08          → 場景管理器
    管理器 + 0x2A90                    → 我的實體 ID
    管理器 + 0x2A74                    → 實體表（+0x2AA4 是上限，實測 4096）
    實體表[ID & 0xFFFF]                → 我的實體（驗 +0xBC == ID 才算數）
    實體 + 0x2FC                       → 物品容器 std::vector
        +0x04 = 頭、+0x08 = 尾，每格 4 bytes 一個物品指標（0 = 空格）

**陣列索引就是格號**（實測五台，非空格 355 個裡只有 2 個不一致 —— 那 2 個是
格號 > 255 的倉庫格，物品自記的 `+0x25` 只有一個 byte 裝不下）。

怎麼反推出來的
--------------
賣東西的視窗重建清單時是這樣列你的東西的（`0x60235D` 那段）：

    push 0x14 / call 0x508B9A      ; 取第 0x14 格 —— 0x508B9A 就是
    ...                            ;   `ecx += 0x2FC; jmp 取vector第n格`
    mov eax, [item + 0x58]         ; 物品的「範本」
    cmp dword [eax + 0x104], 0     ; 範本+0x104 = 售價
    jle 下一格                     ; ★ **售價 <= 0 的東西遊戲自己就不列**
    ...
    cmp [ebp-0x1c], 0xA9 / jle 迴圈 ; 只跑 0x14 ~ 0xA9 這段格號

所以「哪些格算背包」「哪些東西賣得掉」都不是我們定的，是照抄遊戲的判斷。

純讀記憶體，不寫入、不注入。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import itemname, quickbar

# ★ 跟 `quickbar.MGR_PTR` 是同一個全域（快捷欄表也掛在它底下），所以直接借用
#   ——locate.warm() 只要定位那一個，改版位移這裡就跟著對。
#   ⚠ 不要在這裡另外寫一份特徵：同一個位址兩份特徵會各自失效，很難查。
OFF_SCENE_MGR = 0x08        # [MGR_PTR] + 這裡 = 場景管理器
OFF_MY_ID = 0x2A90          # 管理器裡存的「我的實體 ID」
OFF_ENT_TABLE = 0x2A74      # 實體表（用 ID 低 16 位當索引）
OFF_ENT_CAP = 0x2AA4        # 實體表上限
OFF_ENT_ID = 0xBC           # 實體自己記的 ID —— 用來確認沒抓錯人
OFF_CONTAINER = 0x2FC       # 實體 + 這裡 = 物品容器 vector

# --- 物品結構 ------------------------------------------------------------
# ★ +0x00 / +0x04 是**賣出封包要填的那兩個欄位**，不要改名也不要拆開用。
#   實測：同一批進背包的四疊藥水 +0x04 完全相同、+0x00 依序 +1，
#   而 +0x04 換算成 Unix 時間剛好落在最近 —— 所以是（唯一序號, 取得時間）。
ITEM_SERIAL = 0x00
ITEM_STAMP = 0x04
ITEM_TYPE = 0x08            # 種類 ID（itemname 的鍵）
ITEM_SLOT = 0x25            # 物品自己記的格號（1 byte，> 255 就裝不下）
ITEM_COUNT = 0x27           # 數量（u16）
ITEM_DURA = 0x2C            # 耐久現值；0 = 沒耐久這回事，或是壞了
ITEM_TMPL = 0x58            # 指向這種物品的範本
ITEM_SPAN = 0x5C            # 一次要讀多少 bytes 才涵蓋上面全部

TMPL_KIND = 0x18            # 分類代號（1:1 對到 item.xml 的「物品類別」，見下）
TMPL_DURA_MAX = 0xDC        # ★ 耐久上限；> 0 ＝ 這是裝備／武器
TMPL_PRICE = 0x104          # 售價；<= 0 = 這東西賣不掉
TMPL_GRADE = 0x130          # ★ 品質（白／藍／橘），見 GRADE_NAMES
TMPL_SPAN = 0x134

# ★★ 「這是不是裝備／武器」＝ 範本 +0xDC（耐久上限）> 0。
# ✅ 拿 `setting/base/item*.xml` 的「耐久」欄整張對帳：**32476 筆 100.00% 吻合**
#   （其他候選欄位最高只有 78%）。
# ⚠ **不可以用物品自己的 +0x2C（耐久現值）來判斷**：壞掉的裝備現值是 0，
#   跟藥水分不出來 —— 而壞掉的白裝正是最該賣掉的東西。
# 實測旁證：技能經驗球(分類 69)、座騎(25)、扭蛋(33)、藥水(0) 的上限都是 0，
#          頭飾 60、杖 70、背包 40 —— 該進的進、該擋的擋。
# 分類代號（+0x18）1:1 對到 item.xml 的物品類別，裝備／武器落在：
#   2 頭飾 3 衣服 4 手套 5 鞋子 6 飾品 7 背包 8 披風
#   9 劍 10 刀 11 斧 12 錘 13 槍 14 杖 15 弓箭 16 彈弓 17 盾
# （這裡沒用分類判斷，記著是為了看得懂數字。）
# ⚠ 遊戲算單價其實有兩條分支（`0x602391` / `0x602457`）：
#   `0x5533CC` 只有在**分類代號是 26 或 27、而且 +0x14 的 bit 28 有立**時才回真，
#   那條會把單價乘上「數量 ÷ [0x7D9008 + 分類*4]」（寵物飼料那類）。
#   其餘全部走 `單價 = 範本+0x104` 這條，也就是這裡實作的這條。
#   實測五台分身**賣得掉的東西沒有一件落在特例分支**（分類是 0/2/3/4/5/13/17/33），
#   所以沒做那條 —— 真的遇到時單價會偏高，不影響賣不賣得掉。

# 背包的格號範圍，照抄遊戲賣東西視窗的迴圈（0x602357 / 0x6024EB）。
# 0~11 是身上穿的、12~19 是空的裝備格 —— **都不在這個範圍內，所以不會被賣掉**。
FIRST_SLOT = 0x14
LAST_SLOT = 0xA9

GOLD_SLOT = 0              # 第 0 格就是金幣（見 gold()）
GOLD_TYPE = 1              # 金幣的種類 ID

_MAX_SLOTS = 4096           # 容器格數的合理上限（實測 743）

# ★★ 品質（普通白／優質藍／頂級橘）—— **不是靠名字認的**。
#
# 遊戲畫提示框時是這樣挑名字顏色的（`0x5F358A`，物品名稱那一行）：
#
#     mov eax,[物品+0x58]        ; 範本
#     mov eax,[範本+0x130]       ; ← 就是這個欄位
#     1 → "/c#888888%s/c*"       灰
#     2 → "/c$3%s/c*"            **橘金**　┐ `$N` 是調色盤，`0x51CD4F` 查
#     3 → "/c#00F7FF%s/c*"       **青藍**　│ `[0x8494D8 + 字元*4]`，存的是
#     其他(含 0) → "%s"          不上色＝白 ┘ RGB565：$3 = 0xFE62 = RGB(255,206,16)
#
# ✅ 拿 `setting/base/item*.xml` 的 `顏色=` 欄整張對帳，**32476 筆 100% 吻合**：
#     顏色（無）→ 0 (23738 筆)　顏色白 → 0 (457)　顏色紅 → 0 (1)
#     顏色黃   → 2 ( 5440 筆)　顏色藍 → 3 (2840)
#   四大裝備類（衣服／頭飾／手套／鞋子）只出現「（無）／藍／黃」三種，
#   所以裝備的三階就是 0 / 3 / 2，沒有第四種。
# ⚠ 記憶體分不出「顏色=白」與「沒有顏色欄」（都是 0）—— 但那 457 筆白色沒有
#   一件是裝備類，對「賣裝備」不影響。
# ⚠ 值 1（灰）在遊戲載進來的表裡一次都沒出現過，所以沒給它名字。
GRADE_NORMAL = 0            # 普通（白）
GRADE_TOP = 2               # 頂級（橘／資料表寫「黃」）
GRADE_FINE = 3              # 優質（藍）
GRADE_NAMES = {GRADE_NORMAL: "普通", GRADE_TOP: "頂級", GRADE_FINE: "優質"}


@dataclass(frozen=True)
class Item:
    """背包裡的一格東西。"""

    slot: int               # 格號（＝陣列索引）
    serial: int             # +0x00 唯一序號　┐ 賣出封包填這兩個
    stamp: int              # +0x04 取得時間　┘
    type_id: int
    count: int
    dura: int
    kind: int               # 範本的分類代號
    price: int              # 單價；<= 0 = 賣不掉
    grade: int              # 品質，見 GRADE_NAMES
    dura_max: int           # 耐久上限；> 0 = 這是裝備／武器

    @property
    def name(self) -> str:
        return itemname.of(self.type_id) or f"物品 {self.type_id}"

    @property
    def grade_name(self) -> str:
        """'普通' / '優質' / '頂級'；認不得的值就照實顯示編號，不猜。"""
        return GRADE_NAMES.get(self.grade, f"品質{self.grade}")

    @property
    def sellable(self) -> bool:
        return self.price > 0

    @property
    def is_gear(self) -> bool:
        """是不是裝備／武器。看**耐久上限**，不是現值 —— 壞掉的裝備也算。"""
        return self.dura_max > 0

    @property
    def broken(self) -> bool:
        """裝備但耐久歸零（＝壞了）。"""
        return self.is_gear and self.dura <= 0


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else 0


def player_entity(scanner) -> int | None:
    """我的實體物件；認不出來就回 None（絕不回一個「大概是」的位址）。"""
    mgr = _u32(scanner, quickbar.MGR_PTR)
    if not mgr:
        return None
    scene = _u32(scanner, mgr + OFF_SCENE_MGR)
    if not scene:
        return None
    my_id = _u32(scanner, scene + OFF_MY_ID)
    table = _u32(scanner, scene + OFF_ENT_TABLE)
    cap = _u32(scanner, scene + OFF_ENT_CAP)
    if not my_id or not table or not 0 < cap <= 0x10000:
        return None
    idx = my_id & 0xFFFF
    if idx >= cap:
        return None
    ent = _u32(scanner, table + idx * 4)
    # ⚠ 這道驗證不能省：還沒進場／正在換地圖時表裡是舊資料或空的，
    #   照樣讀得到一個「像位址」的值。遊戲自己也是比對這個欄位才認帳。
    if not ent or _u32(scanner, ent + OFF_ENT_ID) != my_id:
        return None
    return ent


def head(scanner) -> tuple[int, int] | None:
    """物品容器的 (表頭, 格數)；還沒進場之類的情況回 None。"""
    ent = player_entity(scanner)
    if ent is None:
        return None
    begin = _u32(scanner, ent + OFF_CONTAINER + 4)
    end = _u32(scanner, ent + OFF_CONTAINER + 8)
    if not begin or end < begin:
        return None
    count = (end - begin) // 4
    if not 0 < count <= _MAX_SLOTS:
        return None
    return begin, count


def items(scanner, first: int = FIRST_SLOT,
          last: int = LAST_SLOT) -> list[Item]:
    """背包裡的東西（預設只含遊戲認定的背包格 0x14~0xA9）。

    讀不到就回空清單 —— 呼叫端看到「一件都沒有」比看到半份資料安全。
    """
    got = head(scanner)
    if got is None:
        return []
    begin, count = got
    lo, hi = max(first, 0), min(last, count - 1)
    if lo > hi:
        return []
    raw = scanner._read_bytes(begin + lo * 4, (hi - lo + 1) * 4)
    if not raw:
        return []
    ptrs = struct.unpack(f"<{hi - lo + 1}I", bytes(raw))

    tmpl_cache: dict[int, tuple[int, int, int]] = {}
    out: list[Item] = []
    for offset, ptr in enumerate(ptrs):
        if not ptr:
            continue
        blob = scanner._read_bytes(ptr, ITEM_SPAN)
        if not blob:
            continue
        b = bytes(blob)
        serial, stamp, type_id = struct.unpack_from("<III", b, ITEM_SERIAL)
        count_ = struct.unpack_from("<H", b, ITEM_COUNT)[0]
        dura = struct.unpack_from("<I", b, ITEM_DURA)[0]
        tmpl = struct.unpack_from("<I", b, ITEM_TMPL)[0]
        kind, price, grade, dmax = tmpl_cache.get(tmpl, (0, 0, 0, 0))
        if tmpl and tmpl not in tmpl_cache:
            traw = scanner._read_bytes(tmpl, TMPL_SPAN)
            if traw:
                tb = bytes(traw)
                kind = struct.unpack_from("<I", tb, TMPL_KIND)[0]
                price = struct.unpack_from("<i", tb, TMPL_PRICE)[0]
                grade = struct.unpack_from("<I", tb, TMPL_GRADE)[0]
                dmax = struct.unpack_from("<I", tb, TMPL_DURA_MAX)[0]
            tmpl_cache[tmpl] = (kind, price, grade, dmax)
        out.append(Item(slot=lo + offset, serial=serial, stamp=stamp,
                        type_id=type_id, count=count_, dura=dura,
                        kind=kind, price=price, grade=grade, dura_max=dmax))
    return out


def gold(scanner) -> int | None:
    """身上的金幣；讀不到回 None。

    ★ 金幣就是**背包第 0 格的那個物品**（種類 1），數量欄 `+0x27` 當 u32 讀 ——
      這是遊戲自己的做法：`0x508C24` = `ecx += 0x2FC; 取第 0 格; return [它+0x27]`。
      五台分身跟 `player.read().gold` 逐一對照，完全一致。
    ⚠ 用這條而不是 `player.locate()`：那支要全記憶體掃描（每台 0.4~1 秒），
      這條只要兩次讀取，而且背包本來就已經定位好了。
    """
    got = head(scanner)
    if got is None:
        return None
    begin, count = got
    if count < 1:
        return None
    raw = scanner._read_bytes(begin, 4)
    if not raw:
        return None
    ptr = struct.unpack("<I", bytes(raw))[0]
    if not ptr:
        return 0
    blob = scanner._read_bytes(ptr, ITEM_SPAN)
    if not blob:
        return None
    b = bytes(blob)
    if struct.unpack_from("<I", b, ITEM_TYPE)[0] != GOLD_TYPE:
        return None                      # 第 0 格不是金幣 → 版面變了，不猜
    return struct.unpack_from("<I", b, ITEM_COUNT)[0]
