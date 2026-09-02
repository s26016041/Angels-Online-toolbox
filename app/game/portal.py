r"""場景觸發物件（副本傳點／機關／地板）—— **踩上去遊戲自己會送封包 0x0D**。

## 我們什麼都不用送（2026-09-02 從使用者「進副本」的擷取解出來）

那份擷取只有 6 包：心跳 0x0F ×2、移動 0x04、**0x0D**、動作 0x07、點實體 0x05。
其中 `0x0D` 就是「進副本」那一下，而且**不是我們送的**——是遊戲客戶端每一拍
自己對每個場景物件跑這支（`0x546A13`，__thiscall，this ＝那個物件）：

    ax = word[物件+0x1FE]                  ; 伺服器發的物件旗標
    if ax == 0: return
    if [物件+0x204] > 0: [物件+0x204]--; return          ; 節流倒數，這期間不送
    if !(ax & 0x185): return                             ; 不是會觸發的東西
    p = 0x580CFF(物件)                     ; 「站在我這一格上的是誰」→ 玩家物件
    if !p: [物件+0x208] = 0; return                      ; 沒人站上來 → 去重欄歸零
    if [物件+0x208] == [p+0xBC]: return    ; ★ 同一個玩家，已經送過了，不重送
    [物件+0x208] = [p+0xBC]
    碼 = ([p+0x1D4] bit27) ? (ax & 0x100 ? 9 : ax & 0x8000 ? 8 : 不送)
                           : (ax & 4     ? 3 : ax & 1      ? 1 : 不送)
    0x5D94A4([p+0x1D0], [物件+0x1D0], 碼)  ; 建包代號 0x0D、內文 11，送出

★ 實測對帳（擷取當下那台還開著）：擷取到的第一個參數 `0x14890089` **就等於**
  那台 `pathfinder_this()+0x1D0`；第二個 `0x5E1400D3` 在物件表裡找得到 ——
  地底廣場（71）(252, 24) 外觀 60001「測試STATIC」，正是吞噬之間的入口。

## ★★★ 由此得到的兩條規矩

1. **傳點不用送封包**：把人走到那一格上就好（`dungeon.py` 的 `portal` 步驟
   本來就是這樣設計的，這份擷取是它的實證）。
2. **重試一定要先離開再回來**：去重欄 `+0x208` 記的是「上一個踩上來的玩家」，
   站在傳點上不動**永遠不會再送第二次**；只有「沒有人站在上面」那一拍才會
   歸零。所以踩了沒反應時，原地重試是白費工，要退開幾格再走回來。
   —— `armed()` 就是把這件事變成**讀得到的硬訊號**（不必靠等幾秒猜）。

## 認得出「這是觸發物件」的方法（零寫死 vtable）

那支每拍檢查是**虛擬函式**，掛在四個類別的 vtable 第 3 槽（+0x0C）上：
CNetObject / **CStaticObject** / CHouseObject / CFarmObject（RTTI 讀出來的）。
槽裡放的是一支 6 道指令的跳板 `push esi / mov esi,ecx / call … / mov ecx,esi /
pop esi / jmp 0x546A13`。所以判斷一個物件會不會觸發，只要：

    [物件+0] → vtable → [vtable+0x0C] → 那支跳板 → 尾巴的 jmp 目標 == TRIGGER_FN

`TRIGGER_FN` 由 AOB 定位（`locate.SIGS` 的 `portal.TRIGGER_FN`），vtable 一個
都不用寫死，四個類別也自動全含。⚠ 定位失敗（改版把那支重寫）→ 一律回 None，
呼叫端顯示「認不出傳點」**大聲停用**，不會拿沒驗過的旗標亂判。

⚠ 這裡的欄位偏移（+0x1FE/+0x204/+0x208/+0x1D0）屬 CLAUDE.md 允許寫死的
  「結構偏移」，出處就是上面那段反組譯；大改版搬家時 `TRIGGER_FN` 那段
  AOB 會先失敗（同一支函式被重寫），功能會先停用而不是安靜算錯。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import entity, mapobj, move

# ── 位址（AOB 自動定位，見 locate.SIGS）────────────────────────────
# 每一拍檢查「有沒有人踩上來」的那支（0x546A13）。定位失敗會被清成 0。
TRIGGER_FN = 0x00546A13
# 建包（代號 0x0D、內文 11）＋送出：`SEND_FN(玩家+0x1D0, 物件+0x1D0, u8 碼)`。
# ＝遊戲踩上去時自己叫的那一支，我們叫它就等於「踩了一下」。
SEND_FN = 0x005D94A4

# ── 結構偏移（出處＝檔頭那段反組譯）───────────────────────────────
OFF_FLAGS = 0x1FE          # u16 伺服器發的物件旗標，決定送不送、送哪個碼
OFF_FLAGS2 = 0x200         # u16 旁邊那個（進場封包同時填的，用途未確認）
OFF_THROTTLE = 0x204       # i32 節流倒數：>0 的期間一律不送
OFF_LAST = 0x208           # u32 ★去重欄：上一個踩上來的玩家（＝玩家 +0xBC）
OFF_SELECT_ID = 0x1D0      # u32 封包裡代表這個物件的 id（跟 scenery 同一格）
OFF_MODEL = 0xB4           # u32 外觀編號（查 mapobj 拿名字）
# vtable 第 3 槽 ＝ 那支每拍檢查的跳板。
VT_SLOT = 0x0C
# 跳板的長度與尾巴 jmp 的位置：`56 8B F1 E8 rel32 8B CE 5E E9 rel32`
THUNK_LEN = 16
THUNK_JMP_AT = 11
# 旗標要中這幾個位元才會送（0x546A3B `test eax, 0x185`）。
TRIGGER_MASK = 0x185
# 物件起點 → entity 偏移的基準差（跟 scenery.py / gather.py 同一個常數）。
E = 8
# 一次要讀到哪裡（最遠的欄位是 +0x208）。
_SPAN = OFF_LAST + 4
_PTR_LO, _PTR_HI = 0x10000, 0x7FFF0000


@dataclass(frozen=True)
class Trigger:
    """一個「踩上去會有事發生」的場景物件。座標是格子。"""

    addr: int          # 物件位址（送出前重驗用）
    oid: int           # 物件表的 id（低 16 位＝表格索引，＝+0xBC）
    select_id: int     # ★ 封包 0x0D 裡代表這個物件的 id（＝+0x1D0）
    x: float
    y: float
    model: int         # 外觀編號
    flags: int         # +0x1FE
    last: int          # +0x208 上一個踩上來的玩家
    throttle: int      # +0x204 節流倒數

    @property
    def code(self) -> int | None:
        """一般狀態下踩上去會送的動作碼；這個旗標不會送就回 None。"""
        return code_for(self.flags)

    @property
    def name(self) -> str:
        return mapobj.name_of(self.model) or f"外觀 {self.model}"

    def dist(self, pos) -> float:
        return ((self.x - pos[0]) ** 2 + (self.y - pos[1]) ** 2) ** 0.5


def code_for(flags: int, special: bool = False) -> int | None:
    """旗標 → 會送出去的動作碼（送不出去回 None）。

    `special` ＝ 玩家 `+0x1D4` 的 bit27（騎乘／變身之類，我們讀不懂它的意思，
    但遊戲會走另一組碼）。照反組譯原樣搬過來，不加自己的解釋。
    """
    if special:
        if flags & 0x100:
            return 9
        if flags & 0x8000:
            return 8
        return None
    if flags & 0x4:
        return 3
    if flags & 0x1:
        return 1
    return None


def _u32(scanner, addr: int):
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else None


def _ok_ptr(v) -> bool:
    return bool(v) and _PTR_LO <= v <= _PTR_HI


def class_triggers(scanner, vt: int) -> bool:
    """這個 vtable 的類別有沒有「每拍檢查有沒有人踩上來」那支？

    ⚠ `TRIGGER_FN` 沒定位到（改版重寫）就一律回 False —— 認不出來要停用，
      不可以退回「看旗標猜」（別的類別 +0x1FE 根本不是旗標，那樣會亂報一堆）。
    """
    if not TRIGGER_FN or not _ok_ptr(vt):
        return False
    thunk = _u32(scanner, vt + VT_SLOT)
    if not _ok_ptr(thunk):
        return False
    raw = scanner._read_bytes(thunk, THUNK_LEN)
    if not raw or len(raw) < THUNK_LEN:
        return False
    b = bytes(raw)
    if b[THUNK_JMP_AT] != 0xE9:            # 尾巴不是 jmp rel32 ＝不是那支跳板
        return False
    rel = struct.unpack_from("<i", b, THUNK_JMP_AT + 1)[0]
    return thunk + THUNK_LEN + rel == TRIGGER_FN


def nearby(scanner, around=None, radius: float | None = None
           ) -> list[Trigger] | None:
    """場上所有觸發物件（給了 `around` 就只留半徑內的，由近到遠）。

    **讀不到回 None，不是空清單**（[[bag-false-empty-guards]] 的同一條規矩：
    「這裡沒有傳點」跟「我讀不到」對使用者是完全不同的兩句話）。
    """
    if not TRIGGER_FN:
        return None                        # 定位失敗＝認不出來，大聲停用
    mgr = _u32(scanner, move.MGR_PTR)
    if not _ok_ptr(mgr):
        return None
    tbl = _u32(scanner, mgr + move.MGR.TBL)
    mx = _u32(scanner, mgr + move.MGR.MAX)
    if not _ok_ptr(tbl) or mx is None or not 0 < mx <= 0x10000:
        return None
    raw = scanner._read_bytes(tbl, (mx + 1) * 4)
    if not raw or len(raw) < (mx + 1) * 4:
        return None
    slots = struct.unpack_from(f"<{mx + 1}I", bytes(raw), 0)
    known: dict[int, bool] = {}            # vtable → 是不是觸發物件那一族
    out: list[Trigger] = []
    for i, obj in enumerate(slots):
        if not _ok_ptr(obj):
            continue
        blob = scanner._read_bytes(obj, _SPAN)
        if not blob or len(blob) < _SPAN:
            continue
        b = bytes(blob)
        vt = struct.unpack_from("<I", b, 0)[0]
        ok = known.get(vt)
        if ok is None:
            ok = known[vt] = class_triggers(scanner, vt)
        if not ok:
            continue
        oid = struct.unpack_from("<I", b, move.MGR.OBJ_ID)[0]
        if (oid & 0xFFFF) != i:
            continue                       # 表格殘留，不是現在這個東西
        flags = struct.unpack_from("<H", b, OFF_FLAGS)[0]
        if not flags or not (flags & TRIGGER_MASK):
            continue                       # 遊戲自己也會在這裡走人
        vx, vy = struct.unpack_from("<II", b, E + entity.OFF_POS_X)
        x = (vx >> 16) / entity.TILE_UNITS
        y = (vy >> 16) / entity.TILE_UNITS
        if x == 0 and y == 0:
            continue
        if around is not None and radius is not None:
            if ((x - around[0]) ** 2 + (y - around[1]) ** 2) ** 0.5 > radius:
                continue
        out.append(Trigger(
            obj, oid, struct.unpack_from("<I", b, OFF_SELECT_ID)[0], x, y,
            struct.unpack_from("<I", b, OFF_MODEL)[0], flags,
            struct.unpack_from("<I", b, OFF_LAST)[0],
            struct.unpack_from("<i", b, OFF_THROTTLE)[0]))
    if around is not None:
        out.sort(key=lambda t: t.dist(around))
    return out


def still_there(scanner, trig: Trigger) -> bool:
    """這個物件**現在**還在原地、還是同一個嗎（換地圖就會被回收再利用）。"""
    if trig is None or not _ok_ptr(trig.addr):
        return False
    if _u32(scanner, trig.addr + move.MGR.OBJ_ID) != trig.oid:
        return False
    return class_triggers(scanner, _u32(scanner, trig.addr) or 0)


def read(scanner, trig: Trigger) -> Trigger | None:
    """重讀同一個物件的即時欄位（旗標／節流／去重欄）。認不出來回 None。"""
    if not still_there(scanner, trig):
        return None
    blob = scanner._read_bytes(trig.addr, _SPAN)
    if not blob or len(blob) < _SPAN:
        return None
    b = bytes(blob)
    vx, vy = struct.unpack_from("<II", b, E + entity.OFF_POS_X)
    return Trigger(
        trig.addr, trig.oid, struct.unpack_from("<I", b, OFF_SELECT_ID)[0],
        (vx >> 16) / entity.TILE_UNITS, (vy >> 16) / entity.TILE_UNITS,
        struct.unpack_from("<I", b, OFF_MODEL)[0],
        struct.unpack_from("<H", b, OFF_FLAGS)[0],
        struct.unpack_from("<I", b, OFF_LAST)[0],
        struct.unpack_from("<i", b, OFF_THROTTLE)[0])


def armed(scanner, trig: Trigger, player_obj: int) -> bool | None:
    """**現在**踩上去會不會送？讀不到回 None（＝不知道，別據此下結論）。

    False 的意思是「去重欄已經記著我了」——站在上面不動不會有第二次，
    要重試就得先走開讓它歸零（檔頭那條規矩）。

    ⚠ `player_obj` 要傳 **`move.pathfinder_this()` / `bag.player_entity()`
      那一種**（+0xBC 是網路 eid 的那個），不是 `entity.snapshot()` 掃到的
      實體本體（那個是它 +8）—— 見 [[entity-coordinates]]「差 8 bytes」。
    """
    now = read(scanner, trig)
    me = _u32(scanner, player_obj + move.MGR.OBJ_ID) if player_obj else None
    if now is None or not me:
        return None
    return now.last != me


def enter(mover, scanner, trig: Trigger, player_obj: int,
          special: bool = False) -> tuple[bool, str]:
    """對這個觸發物件**送一次** 0x0D —— 等於「在那一格上踩了一下」。

    叫的是遊戲自己那一支（`SEND_FN`），參數版面完全照它原本的用法，
    所以送出去的位元組跟真的踩上去一模一樣。

    ⚠⚠ **送出前當場重讀重驗**（CLAUDE.md 鐵則）：物件會被回收、
      `+0x1D0` 每次載入地圖都會重配，用上一拍掃到的值送＝送到別的東西身上。
    ⚠ 這一支**只送一次、不自己重試**（使用者 2026-09-02 指定：「單純按鈕
      按一下發一次」）—— 要再送就再按一次。
    回 (成功排進去了嗎, 給人看的說明)。
    """
    if not SEND_FN:
        return False, "⛔ 送包函式沒定位到（改版了？）—— 停用，不亂叫"
    now = read(scanner, trig)
    if now is None:
        return False, "⛔ 那個物件已經不在了（換圖／被回收）—— 重新掃描"
    if not now.select_id:
        return False, "⛔ 這個物件沒有可用的 id（+0x1D0 是 0）—— 不送"
    code = code_for(now.flags, special)
    if code is None:
        return False, (f"⛔ 旗標 {now.flags:#06x} 算出來「不會送」"
                       "（遊戲自己踩上去也不會送）—— 不送")
    me = _u32(scanner, player_obj + move.MGR.OBJ_ID) if player_obj else None
    my_id = _u32(scanner, player_obj + OFF_SELECT_ID) if player_obj else None
    if not me or not my_id:
        return False, "⛔ 讀不到自己的實體（載圖中？）—— 不送"
    if not mover.call(SEND_FN, my_id, now.select_id, code):
        # ★ 排不進去 ≠ 送出去了（[[confirm-and-resend]]）——講清楚，不當成成功。
        return False, "⚠ 指令槽忙碌，這一發沒送出去 —— 再按一次"
    return True, (f"已送出 0x0D(我 {my_id:#010x}, 物件 {now.select_id:#010x}, "
                  f"碼 {code})")
