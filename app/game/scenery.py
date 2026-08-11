"""場上「點得到的佈景物件」——**製作檯就是其中一個**。純讀，不寫入、不注入。

## 為什麼要有這個模組

製作面板不是自己開得起來的：伺服器要先知道「你在某個檯子前面」。遊戲自己
的做法就是**點一下檯子**，送出封包 `0x05(實體ID, 動作碼 0)`
（`attack.THIRD_FN`，反組譯 0x559FE0：`push 8 / push 5`、
`mov [封包+2],實體ID`、`mov [0x9B6664],實體ID`、`mov [封包+6],動作碼`）。

所以我們要拿到「檯子那個東西的實體 ID」。

## 它在哪裡（2026-08-12 查清楚的）

* **不在** `entity.snapshot()` 掃的實體清單（那是怪／NPC／玩家）。
* **不在** `gather.nearby()` 那棵 `[世界+0x2AA8]` 樹（那是採集品）。
* **在** 遊戲自己的 ID→物件表（`move.MGR`，`pathfinder_this` 用的同一張），
  vtable ＝ `VT_SCENERY`。一張城鎮圖裡有一兩千個（桌椅、石頭、招牌…）。

### ⚠⚠ 舊紀錄說「查不到物件」是**查錯地圖**

memory 的 `craft-donate-storage-packets` 記著「`[0x9B6664]` 的 ID 拿去
`move.MGR` 查不到物件」。2026-08-12 重查：那個 ID 的**低 16 位就是表格索引**
（實測 40 個佈景物件，只有 `+0xBC` 這個欄位低 16 位永遠等於索引），
高 16 位是伺服器配的世代碼，**每次載入地圖都會重配**。當時人已經換到別張圖，
拿舊 ID 去比對當然對不上 —— 不是查錯表，是那個 ID 過期了。
→ 所以 ID **絕對不能跨地圖存起來用**（存位置可以，存 ID 不行）。

## 哪一個才是製作檯？**客戶端根本不知道**

場上物件身上沒有任何欄位說「我是烹飪台」（2026-08-11 查過整個版面、
`setting` 資源包裡也沒有「烹飪台」這個名字）。製作面板開起來之後，
客戶端才從**面板自己**身上讀生產類別（`initmakeclasswnd` 0x58F0A7 的
`mov bl,[視窗+0xB0]`，1→27 2→28 3→29 5→31 6→26）——那是視窗的欄位，
不是檯子的。

→ 所以「哪個是檯子」只能**點點看**：點下去伺服器把製作面板開起來
（Lua 全域 `WND_MAKE` 從 0 變成非 0）就是它。點到桌子椅子不會有事，
那本來就是玩家一天到晚在做的動作。點中的那一個由 `produce_tab` 記起來
（位置＋外觀編號），下次直接用。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import entity, move

# --- 由 AOB 自動定位（見 app/game/locate.py 的 SIGS）---
VT_SCENERY = 0x007D8140    # 佈景／可點物件的 vtable

# 物件起點 → entity 偏移的基準差。出處：entity.py 檔頭 —— 同一個物件有兩個
# vtable（VT_ENTITY2 在起點、VT_ENTITY 在 +8），entity 的偏移一律以 +8 為基準。
# gather.py 走同一棵樹拿到的也是物件起點，用的是同一個常數（實測驗證過）。
E = 8
# 外觀編號。**只拿來當「同一種東西」的指紋**（記住上次點中的檯子長什麼樣），
# 任何判斷都不靠它 —— 它是什麼意思我們沒有反組譯出處，只知道同一種佈景相同。
OFF_MODEL = 0xB4
_PTR_LO, _PTR_HI = 0x10000, 0x7FFF0000


@dataclass(frozen=True)
class Prop:
    """一個佈景物件。座標是格子（跟 entity 同一套單位）。"""

    addr: int          # 物件起點
    oid: int           # 實體 ID —— **送 0x05 用的就是它**（不可跨地圖沿用）
    x: float
    y: float
    model: int         # 外觀編號（只當指紋）

    def dist(self, pos: tuple[float, float]) -> float:
        return ((self.x - pos[0]) ** 2 + (self.y - pos[1]) ** 2) ** 0.5


def _u32(scanner, addr: int):
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else None


def _ok_ptr(v) -> bool:
    return bool(v) and _PTR_LO <= v <= _PTR_HI


# 一個物件要看的欄位：vtable(+0)、外觀(+0xB4)、ID(+0xBC)、座標(+0xC4/+0xC8)
# —— 一次整塊讀完（分開讀等於每個物件四次系統呼叫；場上有一兩千個）。
_SPAN = E + entity.OFF_POS_Y + 4


def nearby(scanner, around: tuple[float, float] | None = None,
           radius: float = 12.0) -> list[Prop] | None:
    """`around` 附近的佈景物件，由近到遠。**讀不到回 None，不是空清單。**

    ⚠ 每一筆都自我驗證：vtable 對得上、**ID 的低 16 位等於表格索引**
      （那一格被回收再用時就對不上 → 直接跳過，不會拿到不相干的東西）、
      座標讀得出來。驗不過的一律不列 —— 送一個過期 ID 給伺服器，
      最好的情況是它不理我們，最壞是點到別的東西。
    """
    mgr = _u32(scanner, move.MGR_PTR)
    if not _ok_ptr(mgr):
        return None
    tbl = _u32(scanner, mgr + move.MGR.TBL)
    mx = _u32(scanner, mgr + move.MGR.MAX)
    if not _ok_ptr(tbl) or mx is None or not 0 < mx <= 0x10000:
        return None
    # 指標陣列一次讀完（4096 格 = 16KB，一次系統呼叫）
    raw = scanner._read_bytes(tbl, (mx + 1) * 4)
    if not raw or len(raw) < (mx + 1) * 4:
        return None
    slots = struct.unpack_from(f"<{mx + 1}I", bytes(raw), 0)
    out: list[Prop] = []
    for i, obj in enumerate(slots):
        if not _ok_ptr(obj):
            continue
        blob = scanner._read_bytes(obj, _SPAN)
        if not blob or len(blob) < _SPAN:
            continue
        b = bytes(blob)
        if struct.unpack_from("<I", b, 0)[0] != VT_SCENERY:
            continue
        oid = struct.unpack_from("<I", b, move.MGR.OBJ_ID)[0]
        if (oid & 0xFFFF) != i:
            continue                       # 世代碼／索引對不上＝殘留，不要用
        vx, vy = struct.unpack_from("<II", b, E + entity.OFF_POS_X)
        x = (vx >> 16) / entity.TILE_UNITS
        y = (vy >> 16) / entity.TILE_UNITS
        if x == 0 and y == 0:
            continue
        if around is not None:
            d = ((x - around[0]) ** 2 + (y - around[1]) ** 2) ** 0.5
            if d > radius:
                continue
        out.append(Prop(obj, oid, x, y,
                        struct.unpack_from("<I", b, OFF_MODEL)[0]))
    if around is not None:
        out.sort(key=lambda p: p.dist(around))
    return out


def still_there(scanner, prop: Prop) -> bool:
    """這個 ID **現在**還指著同一個東西嗎？（送出封包前當場重驗用）

    ⚠ CLAUDE.md 的鐵則：交給遊戲的 ID 送出前要當場重讀重驗 —— 上一拍讀到的
      物件可能已經被回收（換地圖、走出視野），那一格會被別的東西佔走。
    """
    if not prop.oid:
        return False
    mgr = _u32(scanner, move.MGR_PTR)
    if not _ok_ptr(mgr):
        return False
    tbl = _u32(scanner, mgr + move.MGR.TBL)
    mx = _u32(scanner, mgr + move.MGR.MAX)
    idx = prop.oid & 0xFFFF
    if not _ok_ptr(tbl) or mx is None or idx > mx:
        return False
    obj = _u32(scanner, tbl + idx * 4)
    if not _ok_ptr(obj) or _u32(scanner, obj) != VT_SCENERY:
        return False
    return _u32(scanner, obj + move.MGR.OBJ_ID) == prop.oid
