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

★ 五台實測：**沒開過商城視窗的分身也讀得到整張表**（424~425 筆），
  所以掛機中要買東西不必先把商城叫出來。

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
TABLE_PTR = 0x0098BCA8
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


def free_slot(scanner) -> int | None:
    """背包第一個空格（陣列索引）。整袋沒讀完就回 None，不猜。"""
    got = bag.head(scanner)
    if got is None:
        return None
    items, complete = bag.scan(scanner)
    if not complete:
        return None
    used = {it.slot for it in items}
    for s in range(bag.FIRST_SLOT, bag.LAST_SLOT + 1):
        if s not in used:
            return s
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


def buy(mover, scanner, g: Goods, say=None) -> tuple[bool, str]:
    """買一份商品。成功 ＝ **商城倉庫真的多出一筆這個道具**。

    ⚠⚠ 這裡花的是真的點數。重試之所以安全，是因為每次重送前都會先確認
      「商城倉庫還沒多出東西」（見 `_retry`）—— 被伺服器擋掉的購買不扣點。
    """
    if not (mover and mover.active):
        return False, "跳板沒接上"
    if not g.buyable:
        return False, f"商城編號 {g.mall_id} 不在可送範圍（限時／搶購位）"
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

    return actiongate.retry(scanner, _done, _build, say,
                            f"購買「{g.name}」", wait=WAIT_SECS)


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
