"""小屋：讀「自己的房子放在哪」、找場上某人的房子、送「進入房子」。

    house.own(sc)                 → Own('棕櫚基地', 184, 81)；沒展示/沒看過 → None
    house.find(sc, '雪狐')        → Lodge(addr, oid, x, y, locked)；視野裡沒有 → None
    house.enter(mover, sc, lodge) → (True, '已送出')

## 自己的房子（2026-09-25 實機：雪狐 棕櫚基地(184,81)）

放在狀態物件 `[quickbar.MGR_PTR]` 裡（⚠ 不是 move.MGR_PTR 那個物件管理器）：
    +MAP_OFF  UTF-8 地圖名（0x20 bytes，0 結尾）
    +XY_OFF   u16 x、+XY_OFF+2 u16 y（格）
出處（反組譯）：
  * 寫入 `0x5C2BDC`：場上生出「屋主＝我」的房子物件時，把**當下地圖名**抄進
    +MAP_OFF、房子座標 ÷ 格寬寫進 +XY_OFF/+XY_OFF+2。
  * 清除 `0x5C2C3E`：收回房子的封包 → 地圖名清空、`mov [esi+XY_OFF], 0`。
  * 房屋資訊視窗 `0x601209` 讀 +MAP_OFF、`0x601314` 讀 +XY_OFF 顯示。
⚠ 所以它是「我**看過**我的房子在哪」—— 重登後還沒走到房子旁邊前可能是空的
  （未實機驗證）。空的一律回 None（＝不知道），不是「沒有房子」。

## 場上的房子物件

跟其他場景物件同一張表（`[move.MGR_PTR]+MGR.TBL`，同 scenery），vtable ＝ `VT_HOUSE`。
  +0x1D0  送給伺服器的選定 id（`0x590689 push [esi+0x1D0]` → ENTER_FN）
  +0x20C  屋主名（UTF-8，0x21 bytes；建構子 `0x546A74 push 0x21 / lea [esi+0x20C]`，
          `0x546F49 lea eax,[ecx+0x20C]` 取屋主名）
  +0x22D  bit0 ＝ 設了密碼（`0x546F50`；建構子清 0）
座標跟 scenery 同一套（物件起點 +E 之後是 entity 版面）。

## 進入房子（使用者擷取 `封包/進入房子.txt` 第 1 包）

UI 指令 `0x5905D8`（點房子選「進入」）→ 查物件 → 有密碼且不是自己的就開密碼框，
否則 `0x5E49BD(選定id, 密碼字串)`（ecx=[MGR]，函式本身不用 ecx）：
建 0x13A 封包、[+2]=選定id、[+6]=密碼（空字串就照抄空字串）送出。
✅ 擷取的參數 0x664D0007 ＝ 雪狐房子物件 +0x1D0（當場對過）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import entity, move, quickbar

# ★ 下面四個由 locate.warm() 用 AOB 寫回（見 locate.SIGS 的 house 段）。
ENTER_FN = 0x005E49BD     # 進入房子(選定id, 密碼字串指標)
VT_HOUSE = 0x008078E0     # 房子物件的 vtable
MAP_OFF = 0x57F6          # [MGR]+這裡：自己房子所在地圖名（UTF-8）
XY_OFF = 0x5818           # [MGR]+這裡：u16 x, u16 y

MAP_LEN = 0x20            # 0x5C2BD8 push 0x20（抄地圖名的上限）
OFF_SELECT_ID = 0x1D0
OFF_OWNER = 0x20C
OWNER_LEN = 0x21          # 建構子 push 0x21
OFF_LOCK = 0x22D
E = 8                     # 物件起點 → entity 版面（同 scenery.E）
SPAN = OFF_LOCK + 1
SCRATCH_OFF = 0x300       # 空密碼字串放 mover.scratch() 這裡（別人用到 0x2xx 為止，見 grep SCRATCH_OFF）
CALL_TIMEOUT = 1.0
_LO, _HI = 0x10000, 0x7FFF0000


@dataclass(frozen=True)
class Own:
    map_name: str
    x: int
    y: int

    def __str__(self) -> str:
        return f"{self.map_name}({self.x},{self.y})"


@dataclass(frozen=True)
class Lodge:
    addr: int       # 物件起點（送出前 still_there 重驗）
    oid: int        # 選定 id（+0x1D0）
    owner: str
    x: float
    y: float
    locked: bool


def _u32(sc, a: int) -> int | None:
    raw = sc._read_bytes(a, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else None


def _ok(p) -> bool:
    return p is not None and _LO < p < _HI


def _cstr(b: bytes) -> str | None:
    s = b.split(b"\0", 1)[0]
    try:
        return s.decode("utf-8")
    except UnicodeDecodeError:
        return None


def own(sc) -> Own | None:
    """自己的房子在哪；沒展示／還沒看過／讀不到／不合理 → None。"""
    mgr = _u32(sc, quickbar.MGR_PTR)
    if not _ok(mgr) or not MAP_OFF or not XY_OFF:
        return None
    raw = sc._read_bytes(mgr + MAP_OFF, MAP_LEN)
    xy = sc._read_bytes(mgr + XY_OFF, 4)
    if not raw or not xy or len(xy) < 4:
        return None
    name = _cstr(bytes(raw))
    x, y = struct.unpack("<HH", bytes(xy))
    if not name or not (0 < x < 4096 and 0 < y < 4096):
        return None
    return Own(name, x, y)


def _read(sc, obj: int, idx: int | None = None) -> Lodge | None:
    b = sc._read_bytes(obj, SPAN)
    if not b or len(b) < SPAN:
        return None
    b = bytes(b)
    if struct.unpack_from("<I", b, 0)[0] != VT_HOUSE:
        return None
    if idx is not None and \
            (struct.unpack_from("<I", b, move.MGR.OBJ_ID)[0] & 0xFFFF) != idx:
        return None                        # 殘留（世代碼對不上）
    oid = struct.unpack_from("<I", b, OFF_SELECT_ID)[0]
    if not oid or not (oid & 0xFFFF):
        return None
    owner = _cstr(b[OFF_OWNER:OFF_OWNER + OWNER_LEN])
    if not owner:
        return None
    vx, vy = struct.unpack_from("<II", b, E + entity.OFF_POS_X)
    x, y = (vx >> 16) / entity.TILE_UNITS, (vy >> 16) / entity.TILE_UNITS
    if x == 0 and y == 0:
        return None
    return Lodge(obj, oid, owner, x, y, bool(b[OFF_LOCK] & 1))


def nearby(sc) -> list[Lodge] | None:
    """視野裡所有房子；讀不到表回 None（不是空清單）。"""
    if not VT_HOUSE:
        return None
    mgr = _u32(sc, move.MGR_PTR)
    if not _ok(mgr):
        return None
    tbl = _u32(sc, mgr + move.MGR.TBL)
    mx = _u32(sc, mgr + move.MGR.MAX)
    if not _ok(tbl) or mx is None or not 0 < mx <= 0x10000:
        return None
    raw = sc._read_bytes(tbl, (mx + 1) * 4)
    if not raw or len(raw) < (mx + 1) * 4:
        return None
    out = []
    for i, obj in enumerate(struct.unpack_from(f"<{mx + 1}I", bytes(raw))):
        if _ok(obj):
            h = _read(sc, obj, i)
            if h is not None:
                out.append(h)
    return out


def find(sc, owner: str) -> Lodge | None:
    """視野裡屋主叫 owner 的房子；沒有或讀不到 → None。"""
    for h in nearby(sc) or ():
        if h.owner == owner:
            return h
    return None


def still_there(sc, lodge: Lodge) -> bool:
    """送出前重驗：同一個位址還是同一棟（vtable、選定 id、屋主都對得上）。"""
    h = _read(sc, lodge.addr)
    return h is not None and h.oid == lodge.oid and h.owner == lodge.owner


def enter(mover, sc, lodge: Lodge) -> tuple[bool, str]:
    """送「進入房子」。只保證送出；進沒進去看場景有沒有變。"""
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if not ENTER_FN:
        return False, "進入房子的函式定位失敗（改版？）"
    if lodge.locked:
        return False, f"{lodge.owner}的小屋有設密碼"
    if not still_there(sc, lodge):
        return False, "房子物件已經不在（換圖／收回？）"
    mgr = _u32(sc, quickbar.MGR_PTR)
    if not _ok(mgr):
        return False, "讀不到狀態物件"
    with mover.lock:
        pw = mover.scratch() + SCRATCH_OFF
        if not mover.write(pw, b"\0" * 4):
            return False, "寫不進暫存區"
        if mover.call_sync(ENTER_FN, lodge.oid, pw, ecx=mgr,
                           timeout=CALL_TIMEOUT) is None:
            return False, "指令槽忙"
    return True, "已送出進入房子"


def scene_of(map_name: str) -> int | None:
    """地圖名 → 場景編號（查 scene.SCENE_NAMES，那是 GAMEDATA 抽的表）。

    ⚠ 同名的地圖不只一張（天使學園之類）或表裡沒有 → None，不猜。
    """
    from app.game import scene                       # 避免模組載入期循環相依
    ids = [k for k, v in scene.SCENE_NAMES.items() if v == map_name]
    return ids[0] if len(ids) == 1 else None


def inside(scene_id: int | None) -> bool:
    """人在不在某間房子裡（場景表裡叫「房屋」的那張，實測 500＋分流序號）。"""
    from app.game import scene
    base = scene.base_id(scene_id)
    return base is not None and scene.SCENE_NAMES.get(base) == "房屋"
