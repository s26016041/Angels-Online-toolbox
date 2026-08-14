"""場上「點得到的製作站台」——製作檯就是其中一個。純讀，不寫入、不注入。

## 為什麼要有這個模組

製作面板不是自己開得起來的：伺服器要先知道「你在某個檯子前面」。遊戲自己
的做法就是**點一下檯子**，送出封包 `0x05(選定id, 動作碼 0)`
（`attack.THIRD_FN`）。所以我們要拿到「檯子那個東西的選定 id」。

## 製作檯到底是什麼物件（2026-08-12 傍晚實機攔包定案）

⚠⚠ **舊結論作廢**：以前以為製作檯是 vtable `0x7D8140`（scenery）的 model 685
佈景物件。**實測那是純裝飾，點了不開面板**（`[0x9B6664]` 有更新＝伺服器收到，
但 `WND_MAKE` 一直 0）。使用者手動開面板時攔到，真正送出去的是那個**互動物
id**（藏在站台物件 +0x1D0），不是佈景自己的 `0x0a` id。⚠ 那 id 的高 16 位是
伺服器每次載入地圖重配的**世代碼**（廚狐 scene26＝`0x1392xxxx`、工匠狐 scene29＝
`0x4E80xxxx`），**不是固定類型標記、別寫死**（寫死 0x13 → 工匠狐找不到站台）。

真正的製作檯：**跟採集品同一個 vtable `0x7D87B4`（`gather.VT_RESOURCE`）、
採集種類 `kind(+0x184)==0`（所以 `gather.nearby` 會把它跳掉、scenery 舊碼又只
收 0x7D8140，兩邊都漏了它），而它要送給伺服器的選定 id 藏在物件 `+0x1D0`**。

* `+0x1D0` 的出處＝遊戲自己的自動生產函式：`0x556DC9` 先拿物件 `+0xB4`(外觀)
  跟站台外觀表比對命中，再 `0x556F9C: push dword [eax+0x1D0]` 把這個 id 交出去。
  純裝飾（0x7D8140）的 +0x1D0 是垃圾。→ **這一格才是「點它」要用的 id。**
* ✅實機兩次：`THIRD_FN(物件+0x1D0, 0)` → `WND_MAKE` 由 0 變非 0（0.25 秒）。
  拿佈景自己的 `0x0a` oid 去 select ✗ 不開。

## 哪一個才是「烹飪」檯？

站台身上沒有欄位直接寫「烹飪台」。多半只有一種站台在腳邊；由近而遠一個一個
select，面板開起來（`WND_MAKE` 0→非0）的那個就是它，記住位置＋外觀下次直接用
（呼叫端 `produce_tab` 負責）。選定 id 每次載入地圖會重配（高 16 位是世代碼），
所以**只存位置不存 id**，每趟到現場再讀當下的 `+0x1D0`。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import entity, gather, move

# 站台物件的 vtable ＝ 採集品那一族（gather.VT_RESOURCE，已由 AOB 定位）。
# 站台跟採集品的差別：站台 kind==0（採集品 kind!=0）。
# 選定 id 的偏移。出處：反組譯自動生產 `0x556F9C  push dword [eax+0x1D0]`
# （屬「大更新才會壞」的結構偏移，CLAUDE.md 允許寫死＋留出處；改版靠
#  game-patch-relocate 重驗。⚠ /_audit 會把它列為裸偏移，之後補 AOB 登記）。
OFF_SELECT_ID = 0x1D0
# 外觀編號。**只拿來當「同一種東西」的指紋**（記住上次點中的檯子長什麼樣）。
OFF_MODEL = 0xB4
# ★ 物件起點 → entity 偏移的基準差（出處：entity.py 檔頭「實體有兩個
#   vtable、差 8」）。gather.py 走同一棵樹拿到的也是物件起點，同一個常數。
E = 8
_PTR_LO, _PTR_HI = 0x10000, 0x7FFF0000


@dataclass(frozen=True)
class Prop:
    """一個可點的製作站台。座標是格子（跟 entity 同一套單位）。"""

    addr: int          # 物件起點（still_there 用它重驗）
    oid: int           # ★ **要送給伺服器的選定 id**（＝物件 +0x1D0，不可跨地圖沿用）
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


def _ok_select_id(v) -> bool:
    """站台的選定 id 看起來合理嗎（＝世代碼與索引都非 0）。

    id 的版面是 `世代碼(高16) << 16 | 索引(低16)`，兩半都該非 0。
    ⚠⚠ **高位元組不是固定的類型標記**——它是伺服器每次載入地圖重配的世代碼：
      廚狐那張圖（scene 26）站台是 `0x1392xxxx`，工匠狐那張（scene 29）是
      `0x4E80xxxx`。**千萬不要寫死某個高位元組**（2026-08-12 踩過：寫死 0x13
      → 工匠狐整個找不到站台）。純裝飾（vtable 0x7D8140）的 +0x1D0 是垃圾，
      但那一族靠 `nearby()` 的 vtable 條件就排掉了，這裡只要擋掉 0／半截值。
    """
    return bool(v) and (v >> 16) != 0 and (v & 0xFFFF) != 0


# 一個物件要看的欄位：vtable(+0)、外觀(+0xB4)、選定id(+0x1D0)、
# 採集種類(+0x184)、座標(+0xC4/+0xC8＝ entity 基準 +8 再 +0xBC/0xC0)。
# 一次整塊讀完（分開讀等於每個物件多次系統呼叫；場上有一兩千個）。
_SPAN = OFF_SELECT_ID + 4


def nearby(scanner, around: tuple[float, float] | None = None,
           radius: float = 12.0) -> list[Prop] | None:
    """`around` 附近的製作站台，由近到遠。**讀不到回 None，不是空清單。**

    ⚠ 每一筆都自我驗證：vtable 對得上、oid 低 16 位等於表格索引（殘留就跳過）、
      採集種類==0（排掉真採集品）、選定 id 世代碼與索引都非 0、座標讀得出。
    """
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
    out: list[Prop] = []
    for i, obj in enumerate(slots):
        if not _ok_ptr(obj):
            continue
        blob = scanner._read_bytes(obj, _SPAN)
        if not blob or len(blob) < _SPAN:
            continue
        b = bytes(blob)
        if struct.unpack_from("<I", b, 0)[0] != gather.VT_RESOURCE:
            continue
        oid = struct.unpack_from("<I", b, move.MGR.OBJ_ID)[0]
        if (oid & 0xFFFF) != i:
            continue                       # 世代碼／索引對不上＝殘留，不要用
        if struct.unpack_from("<I", b, gather.OFF_GATHER_KIND)[0] != 0:
            continue                       # 採集種類非 0 ＝真採集品，不是站台
        select_id = struct.unpack_from("<I", b, OFF_SELECT_ID)[0]
        if not _ok_select_id(select_id):
            continue                       # 沒有合法互動 id ＝純裝飾，點了不開面板
        vx, vy = struct.unpack_from("<II", b, E + entity.OFF_POS_X)
        x = (vx >> 16) / entity.TILE_UNITS
        y = (vy >> 16) / entity.TILE_UNITS
        if x == 0 and y == 0:
            continue
        if around is not None:
            d = ((x - around[0]) ** 2 + (y - around[1]) ** 2) ** 0.5
            if d > radius:
                continue
        out.append(Prop(obj, select_id, x, y,
                        struct.unpack_from("<I", b, OFF_MODEL)[0]))
    if around is not None:
        out.sort(key=lambda p: p.dist(around))
    return out


def still_there(scanner, prop: Prop) -> bool:
    """這個站台**現在**還在、選定 id 還一樣嗎？（送出封包前當場重驗用）

    ⚠ CLAUDE.md 的鐵則：交給遊戲的 id 送出前要當場重讀重驗。物件可能已被回收
      （換地圖、走出視野），那個位址會被別的東西佔走。用物件位址重讀，確認
      vtable 仍是站台族、且 +0x1D0 仍等於當初那個選定 id。
    """
    if not prop.oid or not _ok_ptr(prop.addr):
        return False
    if _u32(scanner, prop.addr) != gather.VT_RESOURCE:
        return False
    return _u32(scanner, prop.addr + OFF_SELECT_ID) == prop.oid
