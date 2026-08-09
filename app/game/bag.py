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

**陣列索引就是格號**（實測五台，非空格 355 個逐一比對物品自記的 `+0x25`，
全部一致 —— 當初以為有 2 個不一致，那是把 `+0x25` 當 1 byte 讀造成的假象，
見 `ITEM_SLOT`）。

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
#
# ★★ 2026-08-08：下面的偏移**不再是推的** —— 遊戲把物品物件綁給 Lua 用
#   （`ItemData` 那張方法表在 angel.dat `0x849718` 起），逐支反組譯就是欄位表：
#
#     getid        0x5336CA   [this+0x04] 與 [this+0x00]   ← 兩個一組
#     getproto     0x5336ED   [this+0x08]                  種類 ID
#     gettype      0x53370E   [[this+0x58]+0x18]           分類
#     getcount     0x533732   [this+0x27]                  數量
#     geticon      0x533753   [[this+0x58]+0x00]           原型介面
#     getslot      0x533776   movzx eax, **word** [this+0x25]   格號
#     getparam1    0x53379E   [[this+0x58]+0x108]          動態資料1
#     getparam2    0x5337C9   [[this+0x58]+0x10C]          動態資料2
#     getcompose1  0x5337F4   [[this+0x58]+0x110]          一階組合
#     gettimelimit 0x533853   [this+0x2E]                  時限
#     getenergy    0x533874   [this+0xA0]（限分類 0x2E＝46 紙娃娃）
#
ITEM_SERIAL = 0x00
ITEM_STAMP = 0x04
ITEM_TYPE = 0x08            # 種類 ID（itemname 的鍵）
# ★★ 格號是 **u16** 不是 1 byte（`getslot` 是 `movzx eax, word ptr [ecx+0x25]`）。
#   ⚠ 這推翻了舊結論「+0x25 只有一個 byte，格號 > 255 裝不下」——
#     那是當初只讀了 1 byte 造成的假象。✅ 2026-08-08 五台實測 341 件非空格：
#     當 1 byte 對 338 件、**當 u16 對 341 件**（差的 3 件正是 > 255 的裝扮格）。
ITEM_SLOT = 0x25
ITEM_COUNT = 0x27           # 數量（u16）
# ⚠⚠ 耐久要當 **u16** 讀：`gettimelimit` 證明 `+0x2E` 是另一個欄位（時限），
#   當 u32 讀會把時限的低 2 bytes 吃進來 —— 時限道具的耐久就會變成天文數字，
#   `broken`（耐久 0 ＝ 壞了）跟著失靈。實測 341 件目前時限全 0，所以兩種讀法
#   結果一樣；改成 u16 是為了時限道具真的出現時不會安靜算錯。
ITEM_DURA = 0x2C            # 耐久現值（u16）；0 = 沒耐久這回事，或是壞了
ITEM_TIMELIMIT = 0x2E       # ★ 時限；**0 ＝ 沒時限**。非 0 的東西遊戲不讓分解
ITEM_TMPL = 0x58            # 指向這種物品的範本
ITEM_SPAN = 0x5C            # 一次要讀多少 bytes 才涵蓋上面全部
# ★ 能量／經驗值欄（遊戲的 `getenergy`）。紙娃娃裝的是晶化能量、技能經驗球裝的
#   是累積的經驗 —— 同一個欄位，看物品分類決定意義。
#   ⚠ 在 `ITEM_SPAN` 之外，要另外讀（不併進上面那一拍：ITEM_SPAN 是每件物品都會
#   讀的，拉長到 0xA4 會讓貼著區段結尾的物件整批讀取失敗 → 假的「背包空了」）。
ITEM_ENERGY = 0xA0

TMPL_KIND = 0x18            # 分類代號（1:1 對到 item.xml 的「物品類別」，見下）
TMPL_DURA_MAX = 0xDC        # ★ 耐久上限；> 0 ＝ 這是裝備／武器
TMPL_PRICE = 0x104          # 售價；<= 0 = 這東西賣不掉
TMPL_PARAM1 = 0x108         # 動態資料1
# ★★ 動態資料2 ＝ **分解值**（拆成晶能拿幾點）。遊戲拆解介面的判斷就是
#   `getparam2() > 0`，所以不必抄資源包 —— 表就在記憶體裡，改版自動跟上。
#   ✅ 2026-08-08 五台實測 341 件，跟 `item*.xml` 的「動態資料2」**341/341 吻合**。
TMPL_PARAM2 = 0x10C
# 一階組合（融合／合成的組別）。★ 拿來分辨「充能小背包 vs 點裝」很好用：
#   點裝都有組別，充能小背包沒有（說明文字寫「但無法融合」）。
#   程式沒用它判斷，記著是為了看得懂資料。見 memory 的
#   decompose-all-and-doll-slots。
TMPL_COMPOSE1 = 0x110
TMPL_GRADE = 0x130          # ★ 品質（白／藍／橘），見 GRADE_NAMES
TMPL_SPAN = 0x134

# ★ 分類代號 46 ＝ 紙娃娃（造型／裝扮）。來源有兩份互相印證：遊戲自己的
#   Lua 常數 `ITEMOBJ_TYPE_DOLL = 46`，以及 2026-08-08 五台實測
#   （344 件物品、23 種分類跟 item.xml 的「物品類別」1:1 零衝突）。
KIND_DOLL = 46

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

# 身上穿的裝備格（掛機「裝備壞掉」看的就是這一段，不含背包）
WORN_FIRST = 0
WORN_LAST = 11

# ★★ 容器後半段的分區。**數字是遊戲自己的 Lua 全域常數**（2026-08-08 倒出來，
#   見 memory 的 lua-readonly-inspect），不是我們數格子推的：
#       CHAR_SLOT_BEGIN=20  CHAR_SLOT_END=69  BAG_SLOT_BEGIN=70
#       DOLL_SLOT_BEGIN=242 DOLL_SLOT_END=249   ← 身上穿著的「裝扮欄」
#   250 是徽章格、251 起是「紙娃娃隨身包」（遊戲裡叫裝扮背包，2 頁×100）。
#   五台分身的背包實拍完全對得上。
DOLL_WORN_FIRST = 242
DOLL_WORN_LAST = 249

GOLD_SLOT = 0              # 第 0 格就是金幣（見 gold()）
GOLD_TYPE = 1              # 金幣的種類 ID

MAX_SLOTS = 4096            # 容器格數的合理上限（實測 743）
                            # ★ 也是「格號的合理上限」：超過就是讀到垃圾，
                            #   不是真的格子（inventory._slot_of 借用）。

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
    time_limit: int = 0     # +0x2E 時限；0 = 沒時限
    decomp_value: int = 0   # 範本 +0x10C 分解值；> 0 = 拆得成晶能

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

    @property
    def is_doll(self) -> bool:
        """是不是紙娃娃（造型／裝扮）。看記憶體分類，不是看名字。"""
        return self.kind == KIND_DOLL

    @property
    def decomposable(self) -> bool:
        """遊戲**自己**認不認這件可以拆成晶能。

        照抄客戶端的判斷（`OnSetPDItem` 的 Lua bytecode，2026-08-08 倒出來）：

            gettype() == ITEMOBJ_TYPE_DOLL   → kind == 46
            getparam2() > 0                  → decomp_value > 0
            gettimelimit() == 0              → 沒時限　★使用者確認：時限不能拆
            slot 不在 DOLL_SLOT_BEGIN~END(242~249，＝身上穿著的裝扮欄)

        ⚠ 這裡**不含**「哪些格算背包」那道關 —— 那是呼叫端的事
          （`items()` 預設就只給一般背包）。
        """
        return (self.is_doll and self.decomp_value > 0
                and self.time_limit == 0
                and not DOLL_WORN_FIRST <= self.slot <= DOLL_WORN_LAST)


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


HEAD_TRIES = 3               # 表頭讀失敗時重讀幾次（見 head）


def read_ptrs(scanner, addr: int, n: int) -> tuple[list[int], bool]:
    """讀 n 個指標，回 (指標, **整段都讀到了嗎**)。讀不到的那格填 0。

    ★★ **全專案讀物品指標陣列只有這一支**（`bag.scan()` 與 `inventory` 那條
      AOB 路都用它）—— 以前兩邊各有一套減半重試，行為還不一樣：
      inventory 那套「成功但被截斷」會安靜地少看後半段，bag 這套則是整袋變空。

    ⚠⚠ 不可以寫成「一次讀 n*4 bytes，失敗就回空」——`_read_bytes` 是
      **全有全無**的（`ReadProcessMemory` 少讀一個 byte 就回 None）。743 格
      ≈ 3KB，陣列搬家搬到貼著記憶體區段結尾時，只有最後一小塊讀不到，
      卻會讓**整個背包看起來是空的** → 「藥水用完」「你沒有券」那類誤報。
      （[[bag-false-empty-guards]] 當年只修了 inventory.py 這條，漏了這裡。）

    所以失敗就對半切，把讀得到的部分撿回來；切到剩一格還失敗才認賠那一格。
    正常情況第一發就成功，不會多花任何時間。
    """
    out = [0] * n
    complete = True
    todo = [(0, n)]
    while todo:
        start, length = todo.pop()
        if length <= 0:
            continue
        raw = scanner._read_bytes(addr + start * 4, length * 4)
        if raw:
            out[start:start + length] = struct.unpack(
                f"<{length}I", bytes(raw))
            continue
        if length == 1:
            complete = False          # 這一格真的讀不到 → 當空格，但標不完整
            continue
        half = length // 2
        todo.append((start, half))
        todo.append((start + half, length - half))
    return out, complete


def head(scanner) -> tuple[int, int] | None:
    """物品容器的 (表頭, 格數)；還沒進場之類的情況回 None。

    ★ 讀失敗會重讀 HEAD_TRIES 次：這條路上有 6 次 4-byte 讀取，中間只要有
      一拍撞上遊戲正在搬東西（換地圖、實體表重建）就會失敗，而失敗的代價是
      呼叫端看到「背包空的」。重讀幾乎一定救得回來，成本只有幾十微秒。
      ⚠ 重讀救不回來的是**真的沒進場**（登入畫面／選角／讀取中）——那本來
      就沒有背包可讀，只能由呼叫端說清楚，不能假裝讀到。
    """
    for _ in range(HEAD_TRIES):
        got = _head_once(scanner)
        if got is not None:
            return got
    return None


def _head_once(scanner) -> tuple[int, int] | None:
    ent = player_entity(scanner)
    if ent is None:
        return None
    begin = _u32(scanner, ent + OFF_CONTAINER + 4)
    end = _u32(scanner, ent + OFF_CONTAINER + 8)
    if not begin or end < begin:
        return None
    count = (end - begin) // 4
    if not 0 < count <= MAX_SLOTS:
        return None
    return begin, count


def items(scanner, first: int = FIRST_SLOT,
          last: int = LAST_SLOT) -> list[Item]:
    """背包裡的東西（預設只含遊戲認定的背包格 0x14~0xA9）。

    讀不到就回空清單 —— 呼叫端看到「一件都沒有」比看到半份資料安全。
    ⚠ 要下「沒有／用完」這種結論請改用 `scan()`，見那支的說明。
    """
    return scan(scanner, first, last)[0]


def scan(scanner, first: int = FIRST_SLOT,
         last: int = LAST_SLOT) -> tuple[list[Item], bool]:
    """(這段格號裡的東西, **整段是不是真的都讀到了**)。

    ⚠⚠ 要下「沒有／用完／歸零」這種結論**一定要用這支並看第二個值**：
      `items()` 讀不到容器時回的空清單，跟「真的一件都沒有」長得一模一樣，
      當成「沒有」就是安靜地做錯事。這是 [[bag-false-empty-guards]]
      （藥水誤報用完）的同一個坑 —— 2026-08-08 又在每日兌換那顆按鈕上重演
      （「讀不到背包」被報成「你沒有獎勵券」）。
      第二值 False 的情形：還沒進場／換地圖中、位址定位失敗、容器搬家搬到
      區段邊界讀不動、某件物品的物件讀不到。
    ★ 有數到的東西照樣可信 —— 讀不到只會少看，不會多看。
    """
    got = head(scanner)
    if got is None:
        return [], False
    begin, count = got
    lo, hi = max(first, 0), min(last, count - 1)
    if lo > hi:
        # 容器比 first 還短 —— 這段本來就不存在，不是讀不到
        return [], True
    # ★ 讀不到的格子只會壞那一格，不會讓整個背包變空（見 read_ptrs）
    ptrs, complete = read_ptrs(scanner, begin + lo * 4, hi - lo + 1)

    tmpl_cache: dict[int, tuple[int, int, int, int, int]] = {}
    out: list[Item] = []
    for offset, ptr in enumerate(ptrs):
        if not ptr:
            continue
        blob = scanner._read_bytes(ptr, ITEM_SPAN)
        if not blob:
            # 物件剛好在這一拍被回收／搬走 —— 再讀一次多半就有了
            blob = scanner._read_bytes(ptr, ITEM_SPAN)
        if not blob:
            complete = False       # 有格子但讀不到內容 → 這段不完整
            continue
        b = bytes(blob)
        serial, stamp, type_id = struct.unpack_from("<III", b, ITEM_SERIAL)
        count_ = struct.unpack_from("<H", b, ITEM_COUNT)[0]
        # ⚠ u16 不是 u32 —— +0x2E 是時限，見 ITEM_DURA 的說明。
        dura = struct.unpack_from("<H", b, ITEM_DURA)[0]
        tlimit = struct.unpack_from("<I", b, ITEM_TIMELIMIT)[0]
        tmpl = struct.unpack_from("<I", b, ITEM_TMPL)[0]
        kind, price, grade, dmax, param2 = tmpl_cache.get(
            tmpl, (0, 0, 0, 0, 0))
        if tmpl and tmpl not in tmpl_cache:
            traw = scanner._read_bytes(tmpl, TMPL_SPAN)
            if traw:
                tb = bytes(traw)
                kind = struct.unpack_from("<I", tb, TMPL_KIND)[0]
                price = struct.unpack_from("<i", tb, TMPL_PRICE)[0]
                grade = struct.unpack_from("<I", tb, TMPL_GRADE)[0]
                dmax = struct.unpack_from("<I", tb, TMPL_DURA_MAX)[0]
                param2 = struct.unpack_from("<i", tb, TMPL_PARAM2)[0]
            tmpl_cache[tmpl] = (kind, price, grade, dmax, param2)
        out.append(Item(slot=lo + offset, serial=serial, stamp=stamp,
                        type_id=type_id, count=count_, dura=dura,
                        kind=kind, price=price, grade=grade, dura_max=dmax,
                        time_limit=tlimit, decomp_value=param2))
    return out, complete


def worn_broken(scanner) -> list[Item] | None:
    """身上穿的裝備裡**壞掉的那些**（耐久 0）；讀不到容器回 **None**。

    ★ 「裝備壞掉」看全身（0~11 格），不是只看武器（使用者 2026-08-07 要求）。
      背包裡的東西不算 —— 壞的白裝躺在背包裡本來就正常。
    ★ 是不是裝備看**範本的耐久上限**（`is_gear`），藥水那種現值 0 的
      不會被誤判（見 TMPL_DURA_MAX 那段的對帳）。
    ⚠ **None 跟空清單是兩回事**：None = 還沒進場／換地圖中，什麼都別做；
      [] = 讀得到而且沒有一件壞。呼叫端拿 None 去觸發停機就是誤殺。
    """
    if head(scanner) is None:
        return None
    return [it for it in items(scanner, WORN_FIRST, WORN_LAST) if it.broken]


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


def synced(scanner) -> bool:
    """伺服器真的把背包內容推過來了嗎？——要下「歸零／沒有」結論前先問這句。

    ⚠⚠ 這支專治一種**既有防護全部擋不住**的誤報：換頻道／傳送會斷線重連，
      重連之後容器 vector 會**先配置好格數、物品再由伺服器一件件推過來**。
      那段空窗裡容器讀得到、格數正常、每一格都讀得成功，只是全都是空的
      —— 走一趟的結果跟「東西真的用完了」一模一樣。
      `complete`（讀取完整性）與 `is_valid`（表頭有效性）都會通過，因為
      讀取本來就成功、表頭本來就有效。使用者回報「換頻道有時會跳藥水用完」
      就是撞上這個窗口（見 [[bag-false-empty-guards]]）。

    ★ 判準用**遊戲自己的取金幣路徑**，不是計時也不是數量門檻：背包第 0 格
      永遠放著金幣物品（種類 1），身無分文的角色那一格也在（數量 0）——
      見 `gold()` 引的反組譯 `0x508C24`。所以第 0 格是空指標＝「容器配好了、
      東西還沒到」，不是「什麼都沒有」。
    ⚠ **不可以改寫成 `gold(scanner) is not None`**：那支對「第 0 格是空指標」
      回的是 0（身無分文），正好就是這裡要擋掉的狀態。

    ★ 順帶擋掉第二條誤報路徑：這裡只認 `head()`（遊戲自己的容器）。
      `inventory._resolve()` 在問不到容器時會退回 AOB 找到的舊表頭，而換頻道
      後舊背包副本還躺在堆積裡、可能已被部分回收 —— 走那份死副本同樣會數出
      「全部歸零」。要下結論就得問得到遊戲的容器，問不到就不下結論。

    ⚠ 只有要**下結論**時才需要問。平常找東西、數東西都不必：數得到就是有，
      這道檢查只會讓「沒數到」變成「不知道」。
    """
    got = head(scanner)
    if got is None:
        return False                     # 還沒進場／換地圖中／定位失敗
    begin, count = got
    if count < 1:
        return False
    raw = scanner._read_bytes(begin, 4)
    if not raw:
        return False
    ptr = struct.unpack("<I", bytes(raw))[0]
    if not ptr:
        return False                     # ★ 容器配好了，東西還沒推過來
    blob = scanner._read_bytes(ptr + ITEM_TYPE, 4)
    if not blob:
        return False
    return struct.unpack("<I", bytes(blob))[0] == GOLD_TYPE
