"""天使趴趴GO：**填一個編號就傳送**，不必開視窗、不必碰 UI。

    jumpmap.by_scene(7)                  → [Entry(13, '向日葵平原(LV12~23)入口'), …]
    jumpmap.nearest(7, 15, 177, sc)      → 走到 (15,177) **路徑最短**的那個傳送點
    jumpmap.teleport(mover, sc, 13)      → True（1 秒後人就到了）

✅ 實測（黑狐）：千夜魔宮(126) (20.5,23.5) → 送目的地 13 → **+1 秒**
   向日葵平原(7) (15.5,177.5)。落點跟表裡寫的 (15,177) 完全一致。

怎麼送
------
照 `0x5E751B` 尾巴那段原樣重做（那就是遊戲自己送傳送包的程式碼）：

    0x50DF6E(代號 0x151, 內文 6)  ecx = 16 bytes 暫存區 S   → 建封包
    資料 = [S+4]；[資料+2] = 跳地圖編號                      → 填目的地
    0x711130([連線], [S+0xC])                               → 送出

⚠⚠ **不要走 `game.gotojumpmap`**：那支吃的是「清單第幾列」，而那是樹狀清單的
  視覺列號 —— 會隨你切類別、展開、捲動而變。使用者四次實測的列號
  （6/7/9/11）對照 JumpMap.xml，只有一次對得上。
⚠⚠ **更不要去讀那個視窗的清單**：`getlistitemnum` / `getitemtitle` 對它是無效
  操作，實測 4 次有 4 次把客戶端的訊息迴圈弄卡死（要重開遊戲）。
  詳見 [[lua-engine]] 記的「碰資料安全、碰 UI 危險」。

表從哪來
--------
`assets/jumpmap.tsv`（120 筆）與 `assets/jumpmap_class.tsv`（14 個類別），
`tools/build_jumpmap.py` 從遊戲資源包的 `GAMEDATA/setting/base/JumpMap.xml` ＋
`str_jumpmap.xml` ＋ `str_jumpmapclass.xml` 抽出來的。
⚠ 改版增減傳送點要重跑那支工具。

⚠ 類別**不是嚴格的樹**：一個傳送點最多掛三個類別標籤（`類別1/2/3`），
  同一個點會同時出現在好幾個類別底下。所以 `by_class()` 是「任一格命中」。

挑哪個傳送點（2026-09-16 改）
----------------------------
`nearest()` 以前挑**直線距離**最近的，隔一道牆的落點會被選中再繞一大圈
（使用者回報：補給用趴趴GO 回巡邏點、右鍵傳送到巡邏點都會這樣）。現在改成用
地形圖算**真實路徑長度**取最短：人就在那張圖上用記憶體那張地形，要算別張圖
用離線表 `app/game/mapfile.py`（`assets/map_grids.bin.gz`）。
算不出來一律**退回直線最近**，⛔ 不准回 None ——「回程回不去」是出過事的。
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from app.paths import resource

DATA_FILE = "assets/jumpmap.tsv"
CLASS_FILE = "assets/jumpmap_class.tsv"

# ⚠ 這三個值會被 locate.warm() 依 AOB 重新定位，不要在別處複製。
BUILD_FN = 0x0050DF6E        # 建封包(代號, 內文長度)，ecx = 暫存區
SEND_FN = 0x007127A0         # 送出(連線, 封包)
CONN_PTR = 0x009B67D0        # [這裡] = 連線物件

# ★ 出處：反組譯 0x5E751B 尾巴（遊戲自己送傳送包的那段，見檔頭「怎麼送」）。
OPCODE = 0x151               # 傳送封包
# ⚠ 同一段反組譯的版面：內文 6 = 代號(u16) + 跳地圖編號(u32)。
BODY = 6
SCRATCH_OFF = 0x100          # 相對 mover.scratch()；別跟 lua.py 的字串區撞到
CALL_TIMEOUT = 1.0


@dataclass(frozen=True)
class Entry:
    jump_id: int             # 跳地圖編號 —— 送封包用的就是這個
    scene_id: int            # 場景編號
    x: int                   # 落點格子座標
    y: int
    name: str                # 例：'向日葵平原(LV12~23)入口'
    cats: tuple[int, ...] = ()   # 類別編號（最多三個，見檔頭）

    def __str__(self) -> str:
        return self.name or f"跳地圖 {self.jump_id}"


_table: list[Entry] | None = None
_classes: dict[int, str] | None = None


def entries() -> list[Entry]:
    """整張傳送表（第一次用到才載入）。檔案缺了就回空清單。"""
    global _table
    if _table is None:
        out: list[Entry] = []
        try:
            with open(resource(DATA_FILE), encoding="utf-8") as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    # 6 欄是現在的格式；5 欄是還沒有類別那版的舊檔，照樣讀得進來
                    if len(p) == 6:
                        cats = tuple(int(c) for c in p[4].split(",") if c)
                        out.append(Entry(int(p[0]), int(p[1]), int(p[2]),
                                         int(p[3]), p[5], cats))
                    elif len(p) == 5:
                        out.append(Entry(int(p[0]), int(p[1]), int(p[2]),
                                         int(p[3]), p[4]))
        except Exception:                                  # noqa: BLE001
            out = []
        _table = out
    return _table


def classes() -> dict[int, str]:
    """類別編號 → 類別名。檔案缺了就回空字典（呼叫端顯示成編號即可）。"""
    global _classes
    if _classes is None:
        out: dict[int, str] = {}
        try:
            with open(resource(CLASS_FILE), encoding="utf-8") as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    if len(p) == 2 and p[0].isdigit():
                        out[int(p[0])] = p[1]
        except Exception:                                  # noqa: BLE001
            out = {}
        _classes = out
    return _classes


def by_class(class_id: int | None) -> list[Entry]:
    """某個類別底下的傳送點；class_id=None 就是全部。

    ⚠ 「任一格命中」而不是只看第一格 —— 類別不是嚴格的樹（見檔頭）。
    """
    if class_id is None:
        return list(entries())
    return [e for e in entries() if class_id in e.cats]


def by_scene(scene_id: int) -> list[Entry]:
    """某張地圖的所有傳送點（入口／重生點／副本進入點可能有好幾個）。

    ★ 用 map_key 比對，不是編號 —— 表裡**只有本流編號**（120 筆全查過，
      141/241 那些分流編號一筆都沒有）。在分流上掛機的人拿自己的場景
      編號直接查一定落空 →「沒有回○○的傳送點」，補給／死亡回程就把人
      留在城裡。分流地形一樣、落點座標互通（見 scene.map_key）。
    """
    from app.game import scene                        # 避免循環相依
    key = scene.map_key(scene_id)
    return [e for e in entries() if scene.map_key(e.scene_id) == key]


_last_pick = ""                  # 最後一次 nearest() 怎麼挑的（診斷／回歸用）

# ⚠⚠ 算路不便宜（泛洪 33ms ＋ 每個傳送點一次 A*），而**呼叫端有的是每拍都問**
#   （掛機分頁的補給回程／死亡回程 tick 跑在 UI 執行緒上）。同一張圖的地形不會
#   變、目標也是固定的巡邏點，所以答案直接快取起來：第二拍起 O(1)。
#   ⛔ 不准拿掉改成每拍重算 —— 那就是 memory `no-crash-no-lag` 說的
#   「在 UI 執行緒上放慢工作」。key 含場景與目標格，換圖／換目標自然失效。
_PICK_CACHE_SIZE = 8
_pick_cache: dict = {}
_pick_order: list = []


def last_pick() -> str:
    """上一次 `nearest()` 的挑選說明（`tools/mapgrid_check.py` 在驗這個）。"""
    return _last_pick


def clear_pick_cache() -> None:
    """丟掉挑選快取（測試用；節慶換圖那種極端情況也可以手動清）。"""
    _pick_cache.clear()
    _pick_order.clear()


def _grid_for(scene_id: int, scanner=None):
    """算路要用的地形圖：**人就在那張圖上**優先用記憶體那張，否則用離線表。

    ⚠ 記憶體那張是「當下這張圖」的事實（節慶換檔、動態空間都吃得到），所以
      同一張圖一律以它為準；要算**別張圖**才輪到離線表（見 mapfile 檔頭）。
    """
    from app.game import mapfile, scene as scn, terrain     # 避免循環相依

    if scanner is not None:
        try:
            if scn.map_key(scn.current_id(scanner)) == scn.map_key(scene_id):
                grid, _why = terrain.load(scanner)
                if grid is not None:
                    return grid, "記憶體"
        except Exception:
            pass                        # 讀不到就當沒有，往下用離線表
    return mapfile.grid_of(scene_id), "離線表"


def _shortest_walk(scene_id: int, cand: list[Entry], x: float, y: float,
                   scanner=None) -> tuple[Entry | None, str]:
    """候選傳送點裡，**走到 (x,y) 路徑最短**的那個；算不出來回 (None, 原因)。

    ⚠ 先用 `reachable()` 從**目標**泛洪一次，把跟目標不連通的傳送點剔掉，
      再對剩下的算 A*。「走不到的目標最貴」（一次要把整片展開），這道過濾
      就是 memory `terrain-grid` 量過的做法——⛔ 不是給 A* 距離上限，
      那是 `wall-stick-detour-cap` 推翻過的寫法。
    """
    grid, src = _grid_for(scene_id, scanner)
    if grid is None:
        return None, "沒有地形圖"
    goal = grid.nearest_open(int(x), int(y))
    if goal is None:
        return None, f"{src}：目標附近沒有可走格"
    reach = grid.reachable(goal[0], goal[1])
    if not reach:
        return None, f"{src}：目標那格泛洪不出東西"

    best: Entry | None = None
    best_cost = None
    unreachable = 0
    for e in cand:
        start = grid.nearest_open(int(e.x), int(e.y))
        if start is None or start not in reach:
            unreachable += 1
            continue
        path = grid.route((e.x, e.y), (x, y))
        if not path:
            unreachable += 1
            continue
        cost = sum(math.dist(path[i - 1], path[i])
                   for i in range(1, len(path)))
        if best_cost is None or cost < best_cost:
            best, best_cost = e, cost
    if best is None:
        return None, f"{src}：{len(cand)} 個傳送點沒有一個走得到目標"
    return best, (f"{src}：{len(cand)} 個傳送點挑路徑最短的"
                  f"（{best_cost:.0f} 格；走不到的 {unreachable} 個）")


def nearest(scene_id: int, x: float | None = None,
            y: float | None = None, scanner=None) -> Entry | None:
    """那張地圖**走過去最短**的傳送點。

    ★ 一張地圖常有「入口」和「重生點」好幾個落點，位置可以差很遠。傳回**走到
      (x,y) 路徑最短**的那一個，接回自動戰鬥／補給回程時才不用走一大段。
      不給座標就給第一個。

    ★★ 2026-09-16 使用者提的：舊版挑的是**直線距離**最近
      （`(e.x-x)**2+(e.y-y)**2`），隔著一道牆的傳送點會被選中、然後繞一大圈。
      現在改成用地形圖算真實路徑長度；算不出來（沒有那張圖的地形、目標
      在封閉區、傳送點全都走不到）就**退回舊的直線距離**——⛔ 不准回 None，
      那會讓補給／死亡回程整個停掉（回程回不去是 memory 裡記過的事故）。

    scanner: 給了而且**人就在那張圖上**，就用記憶體那張地形（最準）；
      不給或人在別張圖，用離線表 `mapfile`。
    """
    global _last_pick
    cand = by_scene(scene_id)
    if not cand:
        _last_pick = "那張地圖沒有傳送點"
        return None
    if x is None or y is None:
        _last_pick = "沒給座標 → 第一個"
        return cand[0]
    straight = min(cand, key=lambda e: (e.x - x) ** 2 + (e.y - y) ** 2)
    if len(cand) == 1:
        _last_pick = "只有一個傳送點"
        return cand[0]

    from app.game import scene as scn                # 避免循環相依

    key = (scn.map_key(scene_id), int(x), int(y),
           tuple(e.jump_id for e in cand))
    hit = _pick_cache.get(key)
    if hit is not None:
        _last_pick = hit[1] + "（快取）"
        return hit[0]

    best, why = _shortest_walk(scene_id, cand, x, y, scanner)
    if best is None:
        best, why = straight, f"退回直線最近（{why}）"
    else:
        why += "" if best is straight else "，跟直線最近的不同"
    _last_pick = why
    _pick_cache[key] = (best, why)
    _pick_order.append(key)
    while len(_pick_order) > _PICK_CACHE_SIZE:
        _pick_cache.pop(_pick_order.pop(0), None)
    return best


def get(jump_id: int) -> Entry | None:
    return next((e for e in entries() if e.jump_id == jump_id), None)


def search(text: str) -> list[Entry]:
    """用名字找（模糊比對）。"""
    key = text.strip()
    return [e for e in entries() if key in e.name] if key else []


def teleport(mover, scanner, jump_id: int) -> tuple[bool, str]:
    """傳送到某個跳地圖編號。回傳 (成功送出嗎, 說明)。

    ★ 純封包，不碰 UI —— 跟送攻擊、用道具同一個安全等級。
    ⚠ 只保證「封包送出去了」；到不到得看伺服器（等級不足之類會被拒絕）。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    e = get(jump_id)
    if e is None:
        return False, f"傳送表裡沒有編號 {jump_id}"

    def u32(a: int) -> int:
        raw = scanner._read_bytes(a, 4)
        return struct.unpack("<I", bytes(raw))[0] if raw else 0

    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(BUILD_FN, OPCODE, BODY, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return False, "建封包排不進去"
        data = u32(buf + 4)
        if not 0x10000 < data < 0x7FFF0000:
            return False, "封包資料指標不合理"
        mover.write(data + 2, struct.pack("<I", jump_id))
        # ⚠⚠ 連線與封包指標**送出去之前先擋掉 0**。
        #   這支是補給完等待計時（約 2 分鐘）後由計時器觸發的，那個時間點客戶端很可能正在
        #   重連或載地圖 —— 那時 [CONN_PTR] 會是 0，而 SEND_FN 是拿它去算
        #   位址之後才檢查 NULL 的，送 0 進去等於叫遊戲讀一個算出來的爛位址。
        #   ⚠ 只擋 0：第一個參數到底是「連線物件指標」還是「連線編號」還沒
        #     確定（檔頭寫物件、反組譯看起來像索引），沒把握就不要設範圍 ——
        #     猜錯會讓趴趴GO整個失效。
        conn, pkt = u32(CONN_PTR), u32(buf + 0xC)
        if not conn:
            return False, "還沒連上線（連線是 0）—— 可能正在重連"
        if not 0x10000 < pkt < 0x7FFF0000:
            return False, "封包指標不合理"
        if mover.call_sync(SEND_FN, conn, pkt,
                           timeout=CALL_TIMEOUT) is None:
            return False, "送出排不進去"
    return True, f"已送出傳送：{e}"
