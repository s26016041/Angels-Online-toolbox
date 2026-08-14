"""周圍的採集品（礦脈／草叢那類資源）。**純讀記憶體，不寫入、不注入。**

## 採集品不在我們平常掃的實體清單裡

`entity.py` 掃的是 VT_ENTITY 那種物件（怪、NPC、其他玩家）。採集品是**另一種
物件**：2026-08-11 實測，廚狐站在資源旁邊時 `entity.snapshot()` 一個資源都
看不到，但遊戲的生產面板列得出來。

## 怎麼找到的（走遊戲自己的那條路，不是猜的）

反組譯生產面板「重新整理周圍資源列表」按鈕叫的 C 函式
`game.autogatherreloadresource`（Lua game 表 → C 函式 0x597503）：

    esi = [[0x9B669C] + 8]                    ← 世界物件
    走 [esi + 0x2AA8] 這棵樹（迭代器 ++ 是 0x54651E ＝ MSVC 紅黑樹）
        節點 +0x14 = 實體 ID  →  由 ID 查物件
        [物件] 的虛擬函式 [vt+4](0x80000104) 要為真
        [物件+0xC4] 與 [物件+0xC8]（＝座標）都不得為 0
        edi = [物件+0x184]                    ← **採集種類**
            3 → 要技能 0x14　2 → 0x16　4 → 0x15　1 → …
            （拿去跟自己的技能容器 [自己+0x488] 比對，決定採不採得到）

我們照著走同一棵樹，但**不呼叫任何遊戲函式**：
* ID → 物件用遊戲自己那張 ID 表（`move.MGR`，`pathfinder_this` 用的同一張）
* 「是不是採集品」不呼叫虛擬函式，改用**物件開頭的 vtable 等於
  `VT_RESOURCE`**（純讀就能比）＋ 採集種類非 0 ＋ 座標非 0。

實測（廚狐＠神殿遺址）：樹裡 28 筆 = 24 個採集品（小火鱗魚藻 種類 4、
小天使草花叢 種類 3）＋ 2 個玩家 ＋ 自己 ＋ 2 個沒名字、種類 0 的
（那兩個 vtable 一樣但採集種類是 0，遊戲自己的篩選也會把它們排除）。

⚠ 名字／座標的偏移**不在這裡重寫**（CLAUDE.md：同一個位址不准記兩個地方）
  —— 一律引用 `entity` 的常數。差別只有基準：這裡拿到的是**物件起點**，
  `entity` 的偏移是以「起點 +8」為基準（見 entity.py 檔頭），所以要加 `E`。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from app.game import entity, move

# --- 由 AOB 自動定位（見 app/game/locate.py 的 SIGS）---
WORLD_PTR = 0x009B669C     # 世界／場景管理員的全域指標（[+8] 才是世界物件）
VT_RESOURCE = 0x007D87B4   # 採集品物件的 vtable
OFF_TREE = 0x2AA8          # 世界物件裡那棵「場上物件」樹
OFF_GATHER_KIND = 0x184    # 採集品的「採集種類」欄位

# ★ 物件起點 → entity 偏移的基準差（出處：entity.py 檔頭「實體有兩個
#   vtable、差 8」，attack.py 檔頭記的實測老坑同一件事）。
E = 8
_TREE_MAX_DEPTH = 48       # 紅黑樹頂多十幾層；48 是給爛資料的煞車
_MAX_NODES = 4000          # 場上物件再多也不該破這個數
_PTR_LO, _PTR_HI = 0x10000, 0x7FFF0000


@dataclass(frozen=True)
class Resource:
    """一個採集品。座標是格子（跟 entity 同一套單位）。"""

    addr: int          # 物件起點
    eid: int           # 實體 ID
    name: str
    x: float
    y: float
    kind: int          # 採集種類（+0x184；決定要哪個採集技能）

    def dist(self, pos: tuple[float, float]) -> float:
        return ((self.x - pos[0]) ** 2 + (self.y - pos[1]) ** 2) ** 0.5


def _u32(scanner, addr: int):
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else None


def _ok_ptr(v) -> bool:
    return bool(v) and _PTR_LO <= v <= _PTR_HI


def world(scanner) -> int | None:
    """世界物件；讀不到或值不合理回 None（呼叫端就當「這次讀不到」）。"""
    mgr = _u32(scanner, WORLD_PTR)
    if not _ok_ptr(mgr):
        return None
    obj = _u32(scanner, mgr + 8)
    return obj if _ok_ptr(obj) else None


def _walk(scanner, node: int, nil: int, out: list[int], depth: int = 0) -> None:
    """中序走訪紅黑樹，把每個節點的 +0x14（實體 ID）收集起來。

    ⚠ 每個節點**一次讀完 0x18 bytes**：分開讀等於每層三次系統呼叫。
      深度與筆數都有上限 —— 樹被改壞時不能讓我們無限遞迴。
    """
    if depth > _TREE_MAX_DEPTH or not _ok_ptr(node) or node == nil:
        return
    if len(out) >= _MAX_NODES:
        return
    blob = scanner._read_bytes(node, 0x18)
    if not blob:
        return
    b = bytes(blob)
    left, _parent, right = struct.unpack_from("<III", b, 0)
    eid = struct.unpack_from("<I", b, 0x14)[0]
    _walk(scanner, left, nil, out, depth + 1)
    if eid:
        out.append(eid)
    _walk(scanner, right, nil, out, depth + 1)


def entity_ids(scanner) -> list[int]:
    """場上所有物件的實體 ID（含怪、玩家、採集品）。讀不到回空清單。"""
    w = world(scanner)
    if w is None:
        return []
    nil = _u32(scanner, w + OFF_TREE)         # 哨兵節點
    if not _ok_ptr(nil):
        return []
    root = _u32(scanner, nil + 4)             # 哨兵的「父」＝根
    out: list[int] = []
    _walk(scanner, root, nil, out)
    return out


def _obj_of(scanner, eid: int, tbl: int, mx: int) -> int | None:
    """實體 ID → 物件位址（遊戲自己那張表，`pathfinder_this` 用的同一張）。

    ⚠ 一定要拿物件裡回存的 ID 跟查表用的比對 —— 表格是「ID 低 16 位當索引」，
      那一格被別人回收再用時照樣讀得到，不比對就會拿到不相干的物件。
    """
    if (eid & 0xFFFF) > mx:
        return None
    obj = _u32(scanner, tbl + (eid & 0xFFFF) * 4)
    if not _ok_ptr(obj) or _u32(scanner, obj + move.MGR.OBJ_ID) != eid:
        return None
    return obj


def nearby(scanner) -> list[Resource]:
    """周圍所有**採集品**（不含怪、NPC、玩家）。讀不到就回空清單。

    ★ 篩選跟遊戲自己那支一致：vtable 對得上、採集種類非 0、座標非 0。
      種類 0 的那種在遊戲的清單裡也不會出現（實測有 2 個）。
    """
    mgr = _u32(scanner, move.MGR_PTR)
    if not _ok_ptr(mgr):
        return []
    tbl = _u32(scanner, mgr + move.MGR.TBL)
    mx = _u32(scanner, mgr + move.MGR.MAX)
    if not _ok_ptr(tbl) or mx is None:
        return []
    out: list[Resource] = []
    for eid in entity_ids(scanner):
        obj = _obj_of(scanner, eid, tbl, mx)
        if obj is None or _u32(scanner, obj) != VT_RESOURCE:
            continue
        kind = _u32(scanner, obj + OFF_GATHER_KIND)
        if not kind:
            continue                       # 種類 0＝不是可採的（遊戲也排除）
        pos = entity.read_pos(scanner, obj + E)
        if pos is None or (pos[0] == 0 and pos[1] == 0):
            continue                       # 座標 0＝遊戲自己也跳過
        raw = scanner._read_bytes(obj + E + entity.OFF_NAME, entity.NAME_MAX)
        name = ""
        if raw:
            name = bytes(raw).split(b"\x00")[0].decode("utf-8", "replace")
        if not name:
            continue                       # 沒名字的沒辦法給人選，也不該出現
        out.append(Resource(obj, eid, name, pos[0], pos[1], kind))
    return out


def names(scanner) -> list[str]:
    """周圍有哪幾種採集品（去重、排序）—— 給「要採什麼」的清單用。"""
    return sorted({r.name for r in nearby(scanner)})
