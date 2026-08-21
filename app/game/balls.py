"""經驗球：飾品欄裝著的球、背包裡的備球，以及「把備球換上去」。

為什麼不寫死一張球的表
----------------------
球的種類、上限、加成，遊戲**自己就載在記憶體裡**（CLAUDE.md 資料來源第一優先序）：

    是不是球   → 範本 +0x18 分類 ∈ {68 經驗球, 69 技能經驗球, 70 寵物經驗球}
    累積上限   → 範本 +0x10C（`bag.TMPL_PARAM2`；item.xml 的「動態資料2」）
    目前累積   → 物品 +0xA0（`bag.ITEM_ENERGY`，＝遊戲的 `getenergy`）
    在第幾格   → 物品 +0x25（`bag.ITEM_SLOT`，u16，物品自己記的）

出處與交叉驗證（2026-08-21）：
  · 實機（五台）飾品欄的三階技能經驗球，範本 +0x10C 讀到 **120,000**；
    `GAMEDATA/setting/base/item.xml` 的 `<道具 編號="4937" … 動態資料2="120000"/>`
    完全對上。全遊戲 **32 種**球（技能 13 + 角色 13 + 寵物 6）都有這個欄位。
  · 分類代號對照見 `reports/equip_field.txt`（範本 +0x18 → item.xml「物品類別」，
    32476 筆全量比對）：68 經驗球、69 技能經驗球、70 寵物經驗球。
→ 所以改版新增新球種也自動認得，`app/game/items.py` 那張手寫表只留給
  「監控技能經驗球」畫面顯示用，判斷邏輯一律走這裡。

換球怎麼換
----------
遊戲自己的 UI 指令 `changeitemslot`（Lua 綁定 0x59134E）呼叫 `SWAP_FN`：

    SWAP_FN(來源格號, 目標格號)      cdecl，兩個參數都只寫進封包
        push 6 / push 0x12          ; 封包代號 0x12、內文 6 bytes
        [封包+2] = (u16)來源格號
        [封包+4] = (u16)目標格號
        送出(連線, 封包)

參數順序的出處：另一個呼叫點 `0x58CD3E` 是「把物品塞進第一個空格」——
它 `movzx eax, word [物品+0x25]` 當**第一**個參數、找到的空格號當**第二**個，
所以是 (來源, 目標) 不是反過來（0x5B6E0D 那幾處也一致）。

✅ **2026-08-21 實機驗證**（嵐狐，飾品欄左右兩顆三階技能經驗球對調成功）。

⚠ 純封包：客戶端不會自己先動畫面，成不成要看伺服器回不回。所以 `swap()`
  送完會**重讀背包確認飾品欄那格真的換人了**，沒換就回失敗（不假裝成功）。
"""
from __future__ import annotations

import struct
import time

from app.game import actiongate, bag, inventory

# ★ AOB 定位（locate.py balls.SWAP_FN）。定位失敗會被清成 0，`Mover.call()` 擋下。
SWAP_FN = 0x005D23C3

# 呼叫逾時：跟 attack 那邊同一個量級（跳板指令槽只有一個）。
CALL_TIMEOUT = 0.5
# 送出後等伺服器把新的飾品欄推回來的上限（實測換裝是即時的，留裕度）。
SWAP_CONFIRM_SECS = 3.0
# ★★ 兩顆球是**一起換的**，所以會連送兩包 0x12。第一包成功、第二包被伺服器
#   當成「操作太快」丟掉 —— 使用者 2026-08-21 實機回報「只有左飾品換了」。
#   → 走 `actiongate`（跟商城共用同一條隊伍，見那支的檔頭）：
#     第一發只等一下下（前一個動作多半已經隔很久），沒生效的重送才等滿
#     官方的間隔 —— 會走到重送就代表**很可能就是被節流擋掉的**。
SWAP_FIRST_GAP = 1.0
SWAP_GAPS = (SWAP_FIRST_GAP, actiongate.ACTION_GAP, actiongate.ACTION_GAP)

# 飾品欄兩格（借 inventory.py 那一份，不在這裡再寫一次）
ACCESSORY_SLOTS = inventory.SLOT_ACCESSORY


class Ball:
    """一顆經驗球（可能在飾品欄上，也可能躺在背包）。"""

    __slots__ = ("slot", "type_id", "serial", "value", "cap", "kind", "name")

    def __init__(self, slot: int, type_id: int, serial: int,
                 value: int, cap: int, kind: int, name: str) -> None:
        self.slot = slot
        self.type_id = type_id
        self.serial = serial
        self.value = value
        self.cap = cap
        self.kind = kind
        self.name = name

    @property
    def known(self) -> bool:
        """上限讀得到嗎。讀不到就**不准**下「滿了」或「沒滿」的結論。"""
        return self.cap > 0

    @property
    def full(self) -> bool:
        """滿了。上限讀不到一律回 False —— 安全退化成「不換」。"""
        return self.known and self.value >= self.cap

    @property
    def pct(self) -> float | None:
        return None if not self.known else min(100.0, self.value / self.cap * 100.0)

    def __repr__(self) -> str:                       # 診斷用
        return (f"<Ball 格{self.slot} {self.name} "
                f"{self.value}/{self.cap or '?'}>")


def _energy(scanner, ptr: int) -> int | None:
    """讀一顆球的累積值（物品 +0xA0）。讀不到回 None，不回 0 ——
    0 是「空球」的合法值，拿讀取失敗當 0 會把滿球誤判成新球。"""
    raw = scanner._read_bytes(ptr + bag.ITEM_ENERGY, 4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<I", bytes(raw))[0]


def _ptr_of(scanner, slot: int) -> int | None:
    """第 slot 格的物品指標（現讀，不吃快取）。"""
    got = bag.head(scanner)
    if got is None:
        return None
    begin, count = got
    if not 0 <= slot < count:
        return None
    raw = scanner._read_bytes(begin + slot * 4, 4)
    if not raw or len(raw) < 4:
        return None
    p = struct.unpack("<I", bytes(raw))[0]
    return p if 0x10000 < p < 0x7FFF0000 else None


def _serial_of(scanner, slot: int) -> int | None:
    """第 slot 格那件東西的**唯一序號**（物品 +0x00）。讀不到／空格回 None。

    ⚠⚠ **換裝的驗證一定要認序號，不能認陣列指標**（2026-08-21 實機量出來的）：
      換裝時遊戲**不動陣列指標，直接把兩個格子的物品內容互換** ——
          換之前  格8 ptr=0x3be9ce30 serial=76031311
          0.25 秒 格8 ptr=0x3be9ce30 serial=76022663   ← 指標一模一樣
      拿指標當「換好了沒」的判準就永遠驗不到，於是每一次都判失敗、每一次都
      重送 —— 實測同一格連送三包（淨效果剛好換一次，運氣好；偶數次就會
      **換回去**）。這是 [[stale-address-identity-check]]「讀得到≠還是它」
      的同一類坑。
    """
    p = _ptr_of(scanner, slot)
    if not p:
        return None
    raw = scanner._read_bytes(p + bag.ITEM_SERIAL, 4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<I", bytes(raw))[0]


def _to_ball(scanner, item, ptr: int | None = None) -> Ball | None:
    """`bag.Item` → `Ball`；不是球回 None。"""
    if not item.is_ball:
        return None
    if ptr is None:
        ptr = _ptr_of(scanner, item.slot)
    val = _energy(scanner, ptr) if ptr else None
    if val is None:
        return None                       # 讀不到累積值 → 這顆當作沒看到
    return Ball(item.slot, item.type_id, item.serial, val,
                item.ball_cap, item.kind, item.name)


def worn(scanner) -> tuple[list[Ball], bool] | None:
    """(飾品欄兩格上的球, 這兩格是不是都真的讀到了)。整段讀不到回 None。

    ⚠ 回 None 與回 `([], True)` 意義不同：前者是「不知道」（不准動作），
      後者是「確定兩格都沒裝球」。
    """
    got = bag.scan(scanner, bag.WORN_FIRST, bag.WORN_LAST)
    items, complete = got
    if not complete:
        return None
    out = []
    for it in items:
        if it.slot in ACCESSORY_SLOTS and it.is_ball:
            b = _to_ball(scanner, it)
            if b is None:
                return None               # 有球但讀不到值 → 整批不算數
            out.append(b)
    return out, True


def spares(scanner) -> list[Ball] | None:
    """背包裡的備球（**不含**飾品欄）。讀不完整回 None，不回空清單。

    ★ 這是 [[bag-false-empty-guards]] 那條鐵則：「背包沒有備球」是會觸發
      通知的結論，讀不到就不准下這個結論。
    """
    items, complete = bag.scan(scanner)
    if not complete:
        return None
    out = []
    for it in items:
        if not it.is_ball:
            continue
        b = _to_ball(scanner, it)
        if b is not None:
            out.append(b)
    return out


def pick_spare(pool: list[Ball], like: Ball) -> Ball | None:
    """挑一顆要換上去的備球。

    規則（使用者 2026-08-21 定）：
      1. **同一族**（技能球換技能球、角色球換角色球、寵物球換寵物球）——
         換錯族等於掛半天累積到不要的經驗。族看範本分類，不看名字。
      2. 沒滿的才算（`full` 為 True 的跳過；上限讀不到的也跳過，見 `known`）。
      3. 先挑**跟現在同種**的（type_id 一樣）；沒有才挑同族裡上限最大的，
         同上限再挑目前累積最少的。
    """
    ok = [b for b in pool if b.kind == like.kind and b.known and not b.full]
    if not ok:
        return None
    same = [b for b in ok if b.type_id == like.type_id]
    pool2 = same or ok
    return sorted(pool2, key=lambda b: (-b.cap, b.value, b.slot))[0]


def pick_spares(pool: list[Ball], worn_balls: list[Ball]
                ) -> tuple[list[tuple[Ball, Ball]], list[Ball]]:
    """一次幫**每一顆**要換的球各配一顆備球。

    回傳 `(配好的 [(要換下來的, 要換上去的)], 沒配到的那幾顆)`。

    ★ 使用者 2026-08-21 定：飾品欄兩格是**一起換的**，所以配對要一次算完 ——
      同一顆備球不能被兩格重複認領（那會送出兩包搶同一格，第二包必然失敗）。
    """
    left = list(pool)
    pairs: list[tuple[Ball, Ball]] = []
    missing: list[Ball] = []
    for cur in worn_balls:
        got = pick_spare(left, cur)
        if got is None:
            missing.append(cur)
            continue
        pairs.append((cur, got))
        left = [b for b in left if b is not got]
    return pairs, missing


def swap(mover, scanner, src_slot: int, dst_slot: int, say=None
         ) -> tuple[bool, str]:
    """把 `src_slot` 的東西換到 `dst_slot`（＝遊戲的 changeitemslot）。

    ⚠⚠ **送出前當場重讀重驗**（CLAUDE.md 鐵則，`energy.decompose` 是範本）：
      背包會在我們思考的這幾百毫秒內變動（撿到東西、賣掉、補給塞進來），
      拿上一拍讀到的格號送出去就是「安靜地送錯格」。
    ⚠⚠ **重送之前一定要先確認上一發沒生效**（`actiongate.retry` 就是這樣做的）
      —— 換裝是「對調」，對已經換好的再送一次會把它換回去。
    """
    if not (mover and mover.active):
        return False, "跳板沒接上"
    if not SWAP_FN:
        return False, "換裝函式定位失敗（改版？）—— 已停用換球"
    if src_slot == dst_slot:
        return False, "來源與目標是同一格"

    # 「換好了」＝目標格那件東西的**序號換人**（見 `_serial_of`：指標不會變）。
    # 基準只認一次，重送也拿它比。
    before = _serial_of(scanner, dst_slot)
    if before is None:
        return False, f"第 {dst_slot} 格讀不到（還沒進場？）"

    def _done():
        now = _serial_of(scanner, dst_slot)
        if now is None:
            return None                    # 讀不到 → 不下結論
        return now != before

    def _build():
        # ★ 格號每一次都重讀重驗：背包在等待的這幾秒可能已經變動。
        if _ptr_of(scanner, src_slot) is None:
            # 讀不到跟「真的空了」長得一樣 → 當暫時性失敗，下一輪重讀重送。
            return (actiongate.RETRY,
                    f"第 {src_slot} 格讀不到／是空的（背包剛剛變動過）", None)
        with mover.lock:
            if mover.call_sync(SWAP_FN, src_slot, dst_slot,
                               timeout=CALL_TIMEOUT) is None:
                # ★ 指令槽忙碌＝遊戲**根本沒收到**，一定要重送（使用者定）。
                return actiongate.RETRY, "換裝指令排不進去（指令槽忙碌）", None
        return actiongate.SENT, "", lambda: "已換上"

    return actiongate.retry(scanner, _done, _build, say,
                            f"換上第 {dst_slot} 格",
                            wait=SWAP_CONFIRM_SECS, gaps=SWAP_GAPS)
