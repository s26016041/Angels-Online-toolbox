"""天使商城：讀商品表、買、把買到的東西從商城倉庫領進背包。

⚠⚠ **這支會花掉真的商城點數。** 所以每一步都是「送出去 → 回頭驗結果」，
   驗不到就當失敗、大聲講，絕不重試到把點數燒光（呼叫端負責閂門）。

## 商品表在記憶體裡（不抄 GAMEDATA）

    0x552566(商城編號)：
        if (編號 - 1) <= 0x9C3F:  return [ [TABLE_PTR] + 編號*4 ]
        else 報錯回 0

★★ **不可以用 `GAMEDATA/setting/base/mall.xml`** —— 2026-08-21 實機對照發現
   那份本地檔**早就過期**：檔案裡商城編號 2 是「道具 1999、售價 2」，記憶體裡
   實際是「道具 79892、售價 450」；而且整份 448 筆裡**一顆經驗球都沒有**，
   實際商城分類 30 就擺著六筆技能經驗球。商城商品是伺服器下發的，只有記憶體
   那張才是真的（這正是 CLAUDE.md「優先讀記憶體」那條規則的教科書案例）。

⛔⛔ **商品表「要過才有」** —— 2026-08-22 實測五台裡**三台整張表是空的**
   （425 格指標全 0），因為那幾台這次開機後沒人開過商城。
   前一天記的「沒開過商城也讀得到」是取樣運氣好（那幾台早就開過了），**已推翻**。
★ 但不必叫使用者去開商城：`request_data()` 照抄遊戲自己的
  `automallrequestmalldata` 送三包就會灌進來 —— ✅ 2026-08-22 實測
  s26016041 由 0 筆 → **425 筆**。

記錄欄位（實機對照畫面／售價比例確認，2026-08-21）：

    +0x00 道具編號      +0x08 分類內排序    +0x10 一份幾個
    +0x0C 分類（頁籤）  +0x14 售價（點數）

    例：商城編號 363 = 道具 4937「三階技能經驗球」×1，45 點，分類 30
        商城編號 364 = 同一顆 ×2，90 點 —— 售價剛好成比例，欄位對得上。

## 買

`mallbuy` UI 指令 → 0x5D49BE：拿商城視窗選中那筆的編號丟進 `0x5D660F`
換算，再送封包 **0x12B、內文 7**：`u16 代號 + u32 商城編號 + u8 0`。

⚠ `0x5D660F` 只有在編號落在 **0xF3D~0xF4C** 時才會重新對應（那是限時／
  搶購位，同一顆東西會有兩個編號）。那段我們**直接跳過不買** ——
  重算要複製客戶端一整段查表比對邏輯，而正常編號本來就買得到同一顆。

## 領取（商城倉庫 → 背包）

商城倉庫就掛在管理器上，**固定 10 格**、每格 0x37 bytes：

    +0x00 u32 流水號（<= 0 ＝ 這格是空的）
    +0x04 u32 道具編號
    +0x08 u16 數量

  出處：`0x5D38CF`（getmallbagitem 本體）逐格讀這三個欄位；格數上界由
  `0x5D38B1` 的 `cmp eax,9 / ja` 與 `0x5A7EBA` 的 `memset(+0xCEC4, 0, 0x226)`
  兩處互相印證（0x226 = 10 × 0x37）。2026-08-21 嵐狐實機：買了一張
  經驗加倍卡之後第 0 格讀到 (1636596, 2017, 1)，與畫面一致。

領取封包走**跟存倉同一支**（`0x5D2A93`）：代號 **0x2F**、內文 11 ＝
`u8 動作(0x16) + u32 流水號 + u32 目標背包格號`。

  ⚠ 遊戲自己的「右鍵領取」那條路（`getmallbagitem`）第二個參數是**沒初始化
    的堆疊值**（0x592552 只 push 了一個參數給 ret 8 的函式）—— 我們不學它，
    照 `dropmallitem` 那條**版面正確**的路走：明確給一個空背包格號。
  ⚠⚠ 目標格號**只能是遊戲認定「開著」的格**（`bag.usable_slots()`：20~59、
    有擴充通行證才有 60~69、穿著的背包給的 70 起）。送一個鎖住的格號伺服器
    就回「領取商品失敗」—— 2026-09-04 換技能球踩到（角色 40 格滿了挑到 60）。

✅ **2026-08-21 實機驗證（三包全通）**：
   · 領取：嵐狐把商城倉庫裡的經驗加倍卡（流水號 1636596）領進背包成功。
   · 購買：嵐狐跑補球流程買下兩顆三階技能經驗球（商城編號 363，各 45 點），
     商城倉庫清空、背包多出兩顆備球。
   ⚠ 同一趟也暴露了節流問題（連送被擋），修法見 app/game/actiongate.py。
"""
from __future__ import annotations

import struct
import time

from app.game import actiongate, bag, gather, itemname, jumpmap, supply

# ★ 商城商品表的全域指標。AOB 定位（locate.py mall.TABLE_PTR）。
TABLE_PTR = 0x00997394
# 編號上界：0x552566 的 `cmp ecx,0x9C3F`（編號 1..0x9C40）。
MAX_ID = 0x9C40
# 記錄欄位（見檔頭）
G_ITEM, G_ORDER, G_CAT, G_NUM, G_PRICE = 0x00, 0x08, 0x0C, 0x10, 0x14
G_SPAN = 0x18
# ⚠ 這段編號送出前會被客戶端重新對應（0x5D660F），我們不碰。
REMAP_LO, REMAP_HI = 0xF3D, 0xF4C

# 商城倉庫（管理器 + 這裡）。★ 管理器指標借 gather.WORLD_PTR，不再寫一份。
STORAGE_OFF = 0xCEC4
STORAGE_STRIDE = 0x37
STORAGE_SLOTS = 10               # 0x5D38B1 `cmp eax,9`／memset 0x226 兩處印證
ST_SERIAL, ST_ITEM, ST_COUNT = 0x00, 0x04, 0x08

# ★★★ **我的商城點數**（管理器 + 這裡）。2026-08-23 使用者核對畫面確認。
#   位置：緊接在商城倉庫 10 格陣列的正後方（0xCEC4 + 10×0x37 = 0xD0EA → 對齊 0xD0EC）
#   —— 伺服器回商城資料時把「倉庫＋點數」寫進同一塊。
#   怎麼找到的：北極狐是五台裡唯一沒要過商城資料的（商品表空的），
#   拿它跟其他四台比對「只有拿到商城資料的人才有值」的欄位就篩出來了
#   （黑狐 152／白狐 202／嵐狐 182／雪狐 36／北極狐 0，畫面核對全中）。
#   ⛔ 別的路都不通，別再走一遍：客戶端買東西前**不檢查點數**
#     （`mallbuy` 0x5D49BE 直接送 0x12B，「Mile點數不足」是伺服器回的訊息）；
#     Lua 的 `MallBuyCheck` 被 `game.isdef('__MILE_MALL')` 關著、`checkmilemall`
#     沒註冊進 game 表；精靈變數 `AM_INT_CURRENTPOINT`(1602) 只有**開了自動商城**
#     才會填，五台實測全 0。
POINTS_OFF = 0xD0EC
# 合理性上界（驗不過就當讀到垃圾回 None）。
POINTS_MAX = 100_000_000
# ★★ 點數讀到「不夠」要**連續一段時間都這樣**才算數（2026-09-05 使用者實機：
#   經驗球滿了先通知「商城點數不足，目前只有 0 點」，幾秒後又「已從商城買到」——
#   商品表明明載入了、這四個 byte 卻有一小段時間是 0（使用者當時自己開了商城；
#   伺服器回商城資料時這一格被重寫／還沒填回來）。一拍讀到 0 就下「沒錢」的結論
#   ＝拿瞬間值當事實（[[bag-false-empty-guards]] 同一個坑，只是這次是暫時 0 不是讀不到）。
#   → 純讀預檢（`blocked`）：值變了就重新起算，同一個「不夠」的值要撐滿 POINTS_SETTLE
#     秒才擋、才通知；送出前那道（`buy`）：不夠就再盯 POINTS_SETTLE 秒，途中變夠就照買。
#   ⚠ 只是延後幾秒下結論，不是放寬：真的沒錢照樣擋、照樣講差多少。
POINTS_SETTLE = 3.0
# 每台最近一次讀到的點數與「從什麼時候開始一直是這個值」（給 `points_settled`）。
_pt_seen: dict[int, tuple[int, float]] = {}

# ★ 出處：反組譯 mallbuy 本體 0x5D49BE 的 `push 7 / push 0x12B`
#   （建包 0x50E1C2 的兩個參數＝內文長度、封包代號）。
BUY_OPCODE = 0x12B
# ⚠ 同一段反組譯的版面：內文 7 = 代號(u16) + 商城編號(u32) + 0(u8)。
BUY_BODY = 7
# 領取：走**存倉那支封包**（0x5D2A93：代號 0x2F、內文 11），只差動作碼。
# ★ 代號與長度借 supply.py 那一份，不在這裡再寫一次（同一個常數兩處寫死，
#   改版後只會有一邊跟上）。0x11 存入 / 0x16 從商城倉庫領取。
TAKE_OPCODE = supply.DEPOSIT_OPCODE
TAKE_BODY = supply.DEPOSIT_BODY
# ★ 出處：反組譯 0x5D39CD（getmallbagitem 本體的收尾）
#   `push [ebp+0xc] / push [商城倉庫該筆+0x00] / push 0x16 / call 0x5D2A93`
#   —— 那個 0x16 就是 0x2F 封包的動作碼（0x11 是存入倉庫，見 supply.py）。
TAKE_ACTION = 0x16

# ★★★ 「跟伺服器要商城資料」＝ UI 指令 `automallrequestmalldata`（0x595678）
#   照抄它送的三包（反組譯：它完全沒碰 Lua 狀態，就是三個送包）：
#       封包 0x12D，內文 2（只有代號）
#       0x5D2ACC(0x14, 0) → 封包 0x16、內文 7 ＝ u8 動作 + u32 0
#       0x5D2ACC(0x15, 0) → 同上，動作 0x15
#   ⚠⚠ **商品表是要過才有**：2026-08-22 實測五台裡三台的整張表是空的
#   （425 格指標全 0），因為那幾台這次開機後沒人開過商城。
#   （前一天「沒開過商城也讀得到」的結論是取樣運氣好——那幾台早就開過了。）
REQ_OPCODE = 0x12D
# ⚠ 同一段反組譯的版面：`push 2 / push 0x12D` —— 內文 2 ＝ 只有代號，沒有內容。
REQ_BODY = 2
# ★ 出處：反組譯 0x5D2ACC（`push 7 / push 0x16`）—— 0x16 那一族的送包函式。
REQ_SUB_OPCODE = 0x16
# ⚠ 同一段反組譯的版面：內文 7 = 代號(u16) + 動作(u8) + 參數(u32)。
REQ_SUB_BODY = 7
# ★ 出處：0x595678 依序 `0x5D2ACC(0x14, 0)`、`0x5D2ACC(0x15, 0)`。
REQ_SUB_ACTIONS = (0x14, 0x15)
# 已經有表時，重要一次之後等伺服器回填多久（驗不出「換新了沒」）。
REFRESH_SETTLE = 1.5
# 這麼短的時間內不重複要（補兩顆球會連叫兩次，沒必要）。
REFRESH_GAP = 30.0

# 每台上一次跟伺服器要商城資料的時刻。
_refreshed: dict[int, float] = {}

SCRATCH_OFF = 0x40               # 跟 supply.py 借同一塊暫存區的用法
CALL_TIMEOUT = 0.5
# 買完等伺服器把東西塞進商城倉庫、領完等它進背包的上限。
WAIT_SECS = 8.0
POLL = 0.25

# ★★★ 官方的動作節流（兩次動作要隔 5 秒以上）**整套在 app/game/actiongate.py**
#   —— 那裡有反組譯出處，而且計時表是**跨模組共用**的：伺服器對帳號算，
#   商城購買／領取／換裝全部同一條隊伍，各留一份就等於沒排隊。
#   ⚠ 這裡刻意**不留別名**：要用就直接 `actiongate.ACTION_GAP`，
#     免得將來有人以為這是另一個可以獨立調的旋鈕。


class Goods:
    """商城的一筆商品。"""

    __slots__ = ("mall_id", "type_id", "count", "price", "cat", "order")

    def __init__(self, mall_id, type_id, count, price, cat, order):
        self.mall_id = mall_id
        self.type_id = type_id
        self.count = count
        self.price = price
        self.cat = cat
        self.order = order

    @property
    def name(self) -> str:
        return itemname.of(self.type_id) or f"物品 {self.type_id}"

    @property
    def buyable(self) -> bool:
        """我們敢不敢送這一筆。重對應區跳過（見檔頭）。"""
        return (self.count > 0 and self.price > 0
                and not REMAP_LO <= self.mall_id <= REMAP_HI)

    def __repr__(self) -> str:
        return (f"<Goods {self.mall_id} {self.name}×{self.count} "
                f"{self.price}點>")


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) == 4 else 0


def goods(scanner) -> list[Goods] | None:
    """整張商城商品表。讀不到回 **None**（不是空清單）。"""
    base = _u32(scanner, TABLE_PTR)
    if not 0x10000 < base < 0x7FFF0000:
        return None
    blob = scanner._read_bytes(base + 4, MAX_ID * 4)     # arr[0] = 編號 1
    if not blob or len(blob) < MAX_ID * 4:
        return None
    ptrs = struct.unpack(f"<{MAX_ID}I", bytes(blob))
    out: list[Goods] = []
    for i, p in enumerate(ptrs):
        if not 0x10000 < p < 0x7FFF0000:
            continue
        rec = scanner._read_bytes(p, G_SPAN)
        if not rec or len(rec) < G_SPAN:
            continue
        b = bytes(rec)
        tid = struct.unpack_from("<i", b, G_ITEM)[0]
        if tid <= 0:
            continue
        out.append(Goods(
            i + 1, tid,
            struct.unpack_from("<i", b, G_NUM)[0],
            struct.unpack_from("<i", b, G_PRICE)[0],
            struct.unpack_from("<i", b, G_CAT)[0],
            struct.unpack_from("<i", b, G_ORDER)[0]))
    return out or None


def cheapest(scanner, type_id: int) -> Goods | None:
    """商城裡買這種道具**最省的一份**：先挑單價低的，同單價挑買得少的。

    ⚠ 找不到回 None ＝ 商城沒賣這個 → 呼叫端要安全退化（通知，不要亂買）。
    """
    all_g = goods(scanner)
    if all_g is None:
        return None
    same = [g for g in all_g if g.type_id == type_id and g.buyable]
    if not same:
        return None
    return sorted(same, key=lambda g: (g.price / g.count, g.count,
                                       g.mall_id))[0]


def storage(scanner) -> list[tuple[int, int, int]] | None:
    """商城倉庫：[(流水號, 道具編號, 數量)]。讀不到回 **None**。"""
    mgr = _u32(scanner, gather.WORLD_PTR)
    if not 0x10000 < mgr < 0x7FFF0000:
        return None
    blob = scanner._read_bytes(mgr + STORAGE_OFF,
                               STORAGE_STRIDE * STORAGE_SLOTS)
    if not blob or len(blob) < STORAGE_STRIDE * STORAGE_SLOTS:
        return None
    b = bytes(blob)
    out = []
    for i in range(STORAGE_SLOTS):
        o = i * STORAGE_STRIDE
        serial = struct.unpack_from("<i", b, o + ST_SERIAL)[0]
        if serial <= 0:                       # 0x5D38FF：<= 0 ＝ 這格是空的
            continue
        out.append((serial,
                    struct.unpack_from("<I", b, o + ST_ITEM)[0],
                    struct.unpack_from("<H", b, o + ST_COUNT)[0]))
    return out


def _free_slots(scanner) -> list[int] | None:
    """背包**放得進東西**的空格（陣列索引）。整袋沒讀完就回 None，不猜。

    ⚠⚠ 只從 `bag.usable_slots()` 挑 —— 那是遊戲自己「領商城倉庫」本體算
      空格的範圍（20~59 ＋ 有通行證才開的 60~69 ＋ 穿著的背包給的 70 起）。
      舊版拿 `FIRST_SLOT~LAST_SLOT`（賣東西視窗的範圍）當可用格，角色那
      40 格一滿就挑到鎖住的第 60 格 → 伺服器回「領取商品失敗」
      （使用者 2026-09-04 換技能球實錄）。
    """
    got = bag.head(scanner)
    if got is None:
        return None
    items, complete = bag.scan(scanner)
    if not complete:
        return None
    usable = bag.usable_slots(scanner, got)
    if usable is None:
        return None
    used = {it.slot for it in items}
    return [s for s in usable if s not in used]


def free_slot(scanner) -> int | None:
    """背包第一個空格（陣列索引）。整袋沒讀完就回 None，不猜。"""
    got = _free_slots(scanner)
    return got[0] if got else None


def free_count(scanner) -> int | None:
    """背包還有幾個空格。整袋沒讀完就回 **None**（不是 0）。"""
    got = _free_slots(scanner)
    return None if got is None else len(got)


def points(scanner) -> int | None:
    """我現在有多少商城點數；**不知道回 None**（≠ 0 點）。

    ⚠⚠ **商品表沒載入時一律回 None** —— 那代表這台這次開機還沒跟伺服器要過
      商城資料，那格根本沒被填過（北極狐實測就是 0）。把「沒要過」講成
      「沒點數」就是拿讀不到當結論（[[bag-false-empty-guards]] 的同一個坑）。
      要先 `request_data()` 把資料要下來，這一格才有意義。
    """
    if not loaded(scanner):
        return None
    mgr = _u32(scanner, gather.WORLD_PTR)
    if not mgr:
        return None
    raw = scanner._read_bytes(mgr + POINTS_OFF, 4)
    if not raw or len(raw) != 4:
        return None
    val = struct.unpack("<I", bytes(raw))[0]
    return val if 0 <= val <= POINTS_MAX else None


def points_settled(scanner) -> tuple[int | None, float]:
    """`points()` 加上「這個值已經連續讀到幾秒」：回 `(點數, 秒)`。

    值變了（或讀不到）就重新起算 —— 呼叫端拿秒數跟 `POINTS_SETTLE` 比，
    沒撐滿就別下「不夠」的結論（見 POINTS_SETTLE 的說明）。讀不到回 `(None, 0.0)`。
    """
    pid = getattr(scanner, "pid", 0) or 0
    val = points(scanner)
    now = time.monotonic()
    if val is None:
        _pt_seen.pop(pid, None)
        return None, 0.0
    seen = _pt_seen.get(pid)
    if seen is None or seen[0] != val:
        _pt_seen[pid] = (val, now)
        return val, 0.0
    return val, now - seen[1]


def confirmed_short(scanner, need: int) -> int | None:
    """送出前用：點數不夠**而且盯了 POINTS_SETTLE 秒都還不夠**才回那個數；
    夠／讀不到回 None（讀不到不是「沒錢」的證據）。

    ⚠ 會 sleep（最多 POINTS_SETTLE 秒），只准在背景執行緒叫。
    """
    end = time.monotonic() + POINTS_SETTLE
    while True:
        have = points(scanner)
        if have is None or have >= need:
            return None
        if time.monotonic() >= end:
            return have
        time.sleep(POLL)


def loaded(scanner) -> bool:
    """商品表**有沒有資料**（不是「讀不到」，是「這台還沒跟伺服器要過」）。"""
    return goods(scanner) is not None


def request_data(mover, scanner, say=None, force: bool = False
                 ) -> tuple[bool, str]:
    """跟伺服器要商城資料，把商品表灌進記憶體。

    ★ 照抄遊戲自己的 `automallrequestmalldata`（見上面常數的出處）——
      三包純送包，不碰 Lua、沒有 this，所以我們自己建包送就等價。

    · 表是空的（`force=False` 的一般情況）→ 成功＝**商品表真的有東西了**。
    · `force=True`（**每次要花錢之前**）→ 表已經有東西時驗不出「有沒有換新」，
      所以送完只等一小段讓伺服器回填就算數。
      ⚠⚠ 為什麼還是要送：商城會改（限時／搶購位換人、調價）。拿幾小時前的
        舊表去買，**商城編號可能已經指向別的東西** —— 那就是花真錢買錯東西，
        比報錯還糟。一次購買才多花幾秒，這個保險很便宜。
    ⚠ 太密集就不重送（`REFRESH_GAP`）：補兩顆球會連叫兩次，沒必要。
    """
    if not (mover and mover.active):
        return False, "跳板沒接上"
    pid = getattr(scanner, "pid", 0) or 0
    had = goods(scanner)
    if force and had and time.monotonic() - _refreshed.get(pid, -999.0) < REFRESH_GAP:
        return True, "商城資料剛剛才更新過"
    if say:
        say("跟伺服器要最新的商城資料…" if had else "跟伺服器要商城資料…")
    actiongate.gate(scanner, say=say)
    st, why = _send(mover, scanner, REQ_OPCODE, REQ_BODY, b"")
    if st != actiongate.SENT:
        return False, f"要商城資料失敗：{why}"
    for act in REQ_SUB_ACTIONS:
        st, why = _send(mover, scanner, REQ_SUB_OPCODE, REQ_SUB_BODY,
                        struct.pack("<BI", act, 0))
        if st != actiongate.SENT:
            return False, f"要商城資料失敗：{why}"
    _refreshed[pid] = time.monotonic()
    if had:
        # 已經有表 → 驗不出「換新了沒」，等伺服器回填一下就好。
        time.sleep(REFRESH_SETTLE)
        g = goods(scanner)
        return True, f"商城資料已更新（{len(g) if g else 0} 筆商品）"
    end = time.monotonic() + WAIT_SECS
    while time.monotonic() < end:
        time.sleep(POLL)
        g = goods(scanner)
        if g:
            return True, f"商城資料已載入（{len(g)} 筆商品）"
    return False, "送出了但商城商品表還是空的"


# ★ 「錢不夠」的標記 —— 呼叫端用 `short_of_points()` 認它（要通知使用者）。
SHORT_TAG = "商城點數不足"


def short_of_points(msg: str) -> bool:
    """這個訊息是不是「點數不夠」（呼叫端據此通知，見 `SHORT_TAG`）。"""
    return SHORT_TAG in (msg or "")


def quote(scanner, need: list) -> tuple[int, int, str]:
    """這批缺的球要花多少點：回 `(要花的點數, 真的要買幾份, 說明)`。

    ⚠⚠ **一次換兩顆就是兩份**（使用者 2026-08-23 提醒：缺兩顆＝90 點，
      不是一顆的 45）—— 逐項查各自的 `cheapest`，不要拿第一顆的價錢乘。
    ★ 商城倉庫裡已經有現成的那幾顆**不算錢**（`restock` 會直接領）。
    說明非空 ＝ 算不出來（商城沒賣／表沒載入），呼叫端自己決定要不要擋。
    """
    st = storage(scanner)
    have = [tid for _s, tid, _n in st] if st is not None else []
    total = 0
    count = 0
    for cur in need:
        tid = getattr(cur, "type_id", None)
        if tid is None:
            return 0, 0, "缺球清單看不懂"
        if tid in have:
            have.remove(tid)                     # 這顆領就好，不必買
            continue
        g = cheapest(scanner, tid)
        if g is None:
            return total, count, f"商城查不到「{getattr(cur, 'name', tid)}」"
        total += g.price
        count += 1
    return total, count, ""


def blocked(scanner, need: list) -> str | None:
    """**動手之前**先看有沒有一眼就知道會失敗的事；沒問題回 None。

    ★ 2026-08-22 使用者問「會檢查嗎，說不定商城會指令滿或吃掉指令」——
      「吃掉指令」那半靠 `actiongate.retry`（送出去、驗結果、沒生效補送）；
      這一支管的是**另一半**：倉庫滿／背包沒空格／商城沒賣／**點數不夠**，
      這些純讀就看得出來，不必先白跑三十秒（節流一次要等 6 秒）才失敗。
    ⚠ 讀不到一律回 None（＝不擋）：讀不到不等於有問題，真的動手時每一步
      還是會各自驗一次。
    """
    n = len(need)
    first = getattr(need[0], "type_id", 0) if need else 0
    st = storage(scanner)
    if st is not None:
        have = sum(1 for _s, tid, _n in st if tid == first)
        # 倉庫已經有現成的就不必買，滿不滿都無所謂（restock 會直接領）
        if not have and len(st) >= STORAGE_SLOTS:
            return f"商城倉庫滿了（{STORAGE_SLOTS} 格），請先去領取"
    free = free_count(scanner)
    if free is not None and free < n:
        return f"背包只剩 {free} 格空位，補 {n} 顆放不下"
    # ⚠⚠ **「查不到」要先分清楚是「表空的」還是「真的沒賣」** ——
    #   2026-08-22 實測：沒開過商城的分身整張表是空的（指標全 0），
    #   舊版會把它講成「商城沒有賣這種球」＝拿讀不到當結論
    #   （[[bag-false-empty-guards]] 那條鐵則的同一個坑）。
    if not loaded(scanner):
        # ★ 表還沒載入**不算擋** —— `restock()` 會先 `request_data()` 把它要下來。
        #   ⚠ 這時更**不准**去問 `cheapest()`／`points()`：表是空的，問了一定
        #   回 None／0，然後就會被講成「商城沒有賣這種球」「沒點數」。
        return None
    total, count, why = quote(scanner, need)
    if why:
        return why
    # ★★★ 點數不夠 —— 這是**唯一不會自己好**的擋法（要儲值），所以訊息要
    #   講清楚差多少，呼叫端看到 `short_of_points()` 會通知使用者。
    #   ⚠ `points()` 回 None ＝ 不知道（不是 0 點）→ 不擋，讓後面照常試。
    #   ⚠ 剛讀到「不夠」不算數：同一個值要撐滿 POINTS_SETTLE 秒（見那條說明）——
    #     這段期間回一句**不帶 SHORT_TAG** 的擋法（不動手、也不通知）。
    have_pt, age = points_settled(scanner)
    if have_pt is not None and count and have_pt < total:
        if age < POINTS_SETTLE:
            return (f"商城點數讀到 {have_pt} 點（要 {total} 點）"
                    f"—— 再確認一次才算不夠")
        return (f"{SHORT_TAG}：補 {count} 顆要 {total} 點，"
                f"目前只有 {have_pt} 點（差 {total - have_pt} 點）")
    return None


def _send(mover, scanner, opcode: int, body: int,
          payload: bytes) -> tuple[str, str]:
    """建包 → 填內文（從 +2 開始）→ 送出。跟 supply.deposit_slot 同一套。

    ⚠⚠ 回的是 `actiongate` 的**三態字串**（`SENT`／`RETRY`／`STOP`），
      **不是 bool**。這裡曾經回 True/False，`actiongate.retry` 拿它跟 `SENT`
      比就永遠不相等 → 每一發都被當成「沒送出去」而重送 —— 2026-08-21 實機
      因此**多買了一顆球**（多花 45 點）。錢的路徑上，型別錯就是花錯錢。
    ★ 「指令槽忙碌」那種**遊戲根本沒收到**的一律 `RETRY`（使用者定：要重送）；
      只有「沒救」（位址沒定位）才 `STOP`。
    """
    if not (jumpmap.BUILD_FN and jumpmap.SEND_FN):
        return actiongate.STOP, "送包位址還沒定位（改版？先跑 patch_doctor）"
    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(jumpmap.BUILD_FN, opcode, body, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return actiongate.RETRY, "建封包排不進去（指令槽忙碌）"
        data = _u32(scanner, buf + 4)
        if not 0x10000 < data < 0x7FFF0000:
            return actiongate.RETRY, "封包資料指標不合理（下一輪重讀）"
        if not mover.write(data + 2, payload):
            return actiongate.RETRY, "寫封包內容失敗"
        conn = _u32(scanner, jumpmap.CONN_PTR)
        pkt = _u32(scanner, buf + 0xC)
        if not conn:
            return actiongate.RETRY, "還沒連上線 —— 可能正在重連"
        if not 0x10000 < pkt < 0x7FFF0000:
            return actiongate.RETRY, "封包指標不合理"
        if mover.call_sync(jumpmap.SEND_FN, conn, pkt,
                           timeout=CALL_TIMEOUT) is None:
            return actiongate.RETRY, "送出排不進去（指令槽忙碌）"
    return actiongate.SENT, ""


# ★★ 「送出去了、伺服器就是不受理」的標記 —— 呼叫端用 `rejected()` 認它。
#   這種失敗**不會自己好**（點數不足要去儲值、下架／限購要等官方），
#   跟「跳板沒接上」「倉庫讀不到」那種等一下就好的暫時性失敗必須分開：
#   前者要停手＋通知，後者才排冷卻重試。
#   ⚠ 2026-08-23 使用者回報「商城點數不夠會卡在那邊」＝ 舊版全部當暫時性，
#     每 10 分鐘白跑一輪（每輪三次購買重試、各等滿 6 秒節流），永遠補不到、
#     而且只通知第一次 → 看起來就是卡住。
REJECT_TAG = "伺服器一直不受理"


def rejected(msg: str) -> bool:
    """這個失敗訊息是不是「送出去了但伺服器不受理」（見 `REJECT_TAG`）。"""
    return REJECT_TAG in (msg or "")


def buy(mover, scanner, g: Goods, say=None) -> tuple[bool, str]:
    """買一份商品。成功 ＝ **商城倉庫真的多出一筆這個道具**。

    ⚠⚠ 這裡花的是真的點數。重試之所以安全，是因為每次重送前都會先確認
      「商城倉庫還沒多出東西」（見 `_retry`）—— 被伺服器擋掉的購買不扣點。
    """
    if not (mover and mover.active):
        return False, "跳板沒接上"
    if not g.buyable:
        return False, f"商城編號 {g.mall_id} 不在可送範圍（限時／搶購位）"
    # ★★ 送出**前當場重讀**點數：預檢是幾秒前算的，而且補第二顆時第一顆
    #   已經扣過錢了 —— 不夠就別送，免得白送三發（每發等滿 6 秒節流）再失敗。
    #   ⚠ `points()` 回 None ＝ 不知道 → 照送（讀不到不是「沒錢」的證據）。
    #   ⚠ 剛要過商城資料那一格可能暫時是 0 → 不夠就再盯 POINTS_SETTLE 秒
    #     （`confirmed_short`），途中變夠就照買。
    have_pt = confirmed_short(scanner, g.price)
    if have_pt is not None:
        return False, (f"{SHORT_TAG}：買「{g.name}」要 {g.price} 點，"
                       f"目前只有 {have_pt} 點")
    before = storage(scanner)
    if before is None:
        return False, "商城倉庫讀不到 —— 這時候不買"
    if len(before) >= STORAGE_SLOTS:
        return False, f"商城倉庫滿了（{STORAGE_SLOTS} 格），請先領取"
    seen = {s for s, _, _ in before}
    got: list = []

    def _done():
        now = storage(scanner)
        if now is None:
            return None                          # 讀不到 → 不下結論
        new = [r for r in now if r[0] not in seen and r[1] == g.type_id]
        if new:
            got[:] = new
            return True
        return False

    def _build():
        st, why = _send(mover, scanner, BUY_OPCODE, BUY_BODY,
                        struct.pack("<IB", g.mall_id & 0xFFFFFFFF, 0))
        return st, why, lambda: (f"已買到「{g.name}」×"
                                 f"{got[0][2] if got else g.count}"
                                 f"（{g.price} 點）")

    ok, msg = actiongate.retry(scanner, _done, _build, say,
                               f"購買「{g.name}」", wait=WAIT_SECS)
    if not ok and "沒有生效" in msg:
        # ⚠ 這裡跟 `actiongate.retry` 的訊息形狀耦合（它的 `last` 只有兩種：
        #   「沒送出去」＝指令槽忙／被節流，「沒有生效」＝送出去了但結果
        #   沒出現）。**全專案只有這一處認這個字**，改那邊要一起改。
        if storage(scanner) is None:
            # 驗不了 ≠ 被拒收（[[bag-false-empty-guards]] 的同一條鐵則）
            return False, f"買「{g.name}」結果驗不了：商城倉庫讀不到"
        # 倉庫讀得到、就是沒多出這一筆 → 伺服器把這幾發全擋了。
        # 最常見的原因就是**商城點數不足**（客戶端不預檢，見檔頭）。
        return False, (f"買「{g.name}」（{g.price} 點）{REJECT_TAG}"
                       f"：送了 {actiongate.TRIES} 次都沒進商城倉庫"
                       f" —— 商城點數不足？（也可能那顆下架／限購）")
    return ok, msg


def take(mover, scanner, serial: int, type_id: int, say=None
         ) -> tuple[bool, str]:
    """把商城倉庫某一筆領進背包。成功 ＝ **那一筆從商城倉庫消失**。

    ⚠ 目標背包格號**每一次送之前都當場重找**（背包會在這幾秒內變動）。
    """
    if not (mover and mover.active):
        return False, "跳板沒接上"
    name = itemname.of(type_id) or f"物品 {type_id}"

    def _done():
        now = storage(scanner)
        if now is None:
            return None
        return not any(s == serial for s, _, _ in now)

    def _build():
        slot = free_slot(scanner)                # ★ 現找，不用上一發那個
        if slot is None:
            # 「沒空格」與「整袋讀不到」長得一樣，兩種都可能下一輪就好了
            # —— 當暫時性失敗重送，不要直接判死。
            return actiongate.RETRY, "背包沒有空格（或整袋讀不到）", None
        st, why = _send(mover, scanner, TAKE_OPCODE, TAKE_BODY,
                        struct.pack("<BII", TAKE_ACTION,
                                    serial & 0xFFFFFFFF, slot & 0xFFFFFFFF))
        return st, why, lambda: f"已把「{name}」領進背包"

    return actiongate.retry(scanner, _done, _build, say,
                            f"領取「{name}」", wait=WAIT_SECS)
