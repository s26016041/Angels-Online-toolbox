"""地形圖：直接讀遊戲載在記憶體裡的「哪一格能走」，自己算最短路。

★★★ 為什麼這是對的做法
----------------------
以前走遠路是「問遊戲的尋路 → 算不出來就 360 度找中繼點試」——因為遊戲的
尋路函式一次只算得出約 30 格。那條路在凹地形會來回彈（實測 142 格的路
走了 429 格、49.6 秒）。

其實**整張地圖的可走格就攤在記憶體裡**，讀出來自己算 A* 就好：
實測 300x180 的圖**讀完只要 5ms、A* 算完 12ms**，而且是最短路。

版面（反組譯遊戲自己的「這一格能不能走」0x507F20 得到的）
--------------------------------------------------------
    地圖物件 = [MAP_PTR]
        +0x24  寬（格）
        +0x28  高（格）
        +0x2C  列指標陣列（每列一個指標）
    第 ty 列 = [列指標陣列 + ty*4]
    每一格 7 bytes；`(列[tx*7 + 2] & 3) != 0` 就是**不能走**

原始組語（`ecx` = 地圖物件，參數是格子座標）：
    cmp edx,[ecx+0x24] / jge 失敗      ← x 界限
    cmp esi,[ecx+0x28] / jge 失敗      ← y 界限
    mov eax,[ecx+0x2c]                 ← 列指標陣列
    imul ecx,edx,7                     ← x * 7
    mov eax,[eax+esi*4]                ← 第 y 列
    test byte ptr [eax+ecx+2],3        ← 阻擋旗標
    jne 失敗 / mov al,1

驗證（2026-08-07）
------------------
· 5 台分身、3 張地圖（惡礁峽谷 300x180、人馬崗哨 300x180、靜謐海域
  300x190）全部讀得出來；同一張圖的兩台可走格數完全相同（24264）。
· 跟**遊戲自己的尋路**對答案（60 個隨機點、只算不走）：
  「我們說可走、遊戲說走不到」**一次都沒有**（這個方向是安全的）。
  9 個不一致全是「終點那格本身是牆」——遊戲會回一條盡量靠近的路，
  我們則要求終點可走。所以終點在牆上時要放寬到最近的可走格（nearest_open）。

⚠ 地圖物件會換地圖就搬家，所以快取要綁「物件位址 + 寬高」，一變就重讀。
⚠ 這裡**完全不呼叫遊戲**，純讀記憶體 —— 沒有崩潰風險。
"""
from __future__ import annotations

import heapq
import math
import struct
import zlib

# ⚠ 這個位址會被 locate.warm() 依 AOB 重新定位，不要在別處複製一份。
MAP_PTR = 0x009B5F64

OFF_W = 0x24               # 寬（格）
OFF_H = 0x28               # 高（格）
OFF_ROWS = 0x2C            # 列指標陣列
CELL = 7                   # 每格幾 bytes
FLAG_OFF = 2               # 格子裡的阻擋旗標在第幾個 byte
BLOCK_MASK = 3             # 那個 byte 的哪幾個 bit 代表不能走
MAX_DIM = 4096             # 合理性上限（讀到亂數就當失敗）
# 終點落在牆上時，往外找可走格的半徑。遊戲自己也是走到旁邊。
GOAL_RELAX = 4
# 一段最多幾格。遊戲的走路常式自己會再尋路一次，而它一次只算得出約 30 格
# —— 切短一點保證每段都在它的能力範圍內。
MAX_SEG = 24.0
_SQRT2 = math.sqrt(2.0)


class Grid:
    """一張地圖的可走格。`rows[y]` 是一列的 bytes，用 bit 判斷。"""

    __slots__ = ("w", "h", "obj", "open")

    def __init__(self, w: int, h: int, obj: int, open_rows: list[bytearray]):
        self.w, self.h, self.obj, self.open = w, h, obj, open_rows

    def walkable(self, x: int, y: int) -> bool:
        return (0 <= x < self.w and 0 <= y < self.h
                and bool(self.open[y][x]))

    def clear_line(self, a, b) -> bool:
        """兩格之間直線是不是全程可走（含不許鑽對角縫）。"""
        x0, y0 = a
        x1, y1 = b
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        x, y = x0, y0
        while (x, y) != (x1, y1):
            if not self.walkable(x, y):
                return False
            e2 = err * 2
            stepped_x = stepped_y = False
            if e2 > -dy:
                err -= dy
                x += sx
                stepped_x = True
            if e2 < dx:
                err += dx
                y += sy
                stepped_y = True
            # 斜著跨過去時，兩個「肩膀」都要能走，不然是鑽牆縫
            if stepped_x and stepped_y:
                if not (self.walkable(x - sx, y) and self.walkable(x, y - sy)):
                    return False
        return self.walkable(x1, y1)

    def nearest_open(self, gx: int, gy: int, radius: int = GOAL_RELAX):
        """終點落在牆上時找最近的可走格；找不到回 None。"""
        if self.walkable(gx, gy):
            return gx, gy
        for r in range(1, radius + 1):
            best = None
            for dx in range(-r, r + 1):
                ys = (-r, r) if abs(dx) != r else range(-r, r + 1)
                for dy in ys:
                    x, y = gx + dx, gy + dy
                    if self.walkable(x, y):
                        d = dx * dx + dy * dy
                        if best is None or d < best[0]:
                            best = (d, x, y)
            if best:
                return best[1], best[2]
        return None

    # -- 最短路 -------------------------------------------------------
    def route(self, start, goal, relax: int = GOAL_RELAX):
        """A* 最短路，回傳每一格的座標；走不到回 None。

        ⚠ 起點也可能落在牆上（角色站在只有一格寬的縫裡、或剛傳送完），
          所以起點同樣要放寬 —— 不然明明走得動卻回「走不到」。
        """
        s = self.nearest_open(int(start[0]), int(start[1]), relax)
        g = self.nearest_open(int(goal[0]), int(goal[1]), relax)
        if s is None or g is None:
            return None
        sx, sy = s
        gx, gy = g
        if (sx, sy) == (gx, gy):
            return [(sx, sy)]

        def hf(x, y):
            dx, dy = abs(x - gx), abs(y - gy)
            return (dx + dy) + (_SQRT2 - 2.0) * (dx if dx < dy else dy)

        openq = [(hf(sx, sy), 0.0, sx, sy)]
        came: dict = {}
        best = {(sx, sy): 0.0}
        walk = self.walkable
        push, pop = heapq.heappush, heapq.heappop
        while openq:
            _f, g0, x, y = pop(openq)
            if x == gx and y == gy:
                path = [(x, y)]
                while (x, y) in came:
                    x, y = came[(x, y)]
                    path.append((x, y))
                path.reverse()
                return path
            if g0 > best.get((x, y), 1e18):
                continue
            for dx, dy, c in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0),
                              (0, -1, 1.0), (1, 1, _SQRT2), (1, -1, _SQRT2),
                              (-1, 1, _SQRT2), (-1, -1, _SQRT2)):
                nx, ny = x + dx, y + dy
                if not walk(nx, ny):
                    continue
                # 不許從兩面牆的對角縫鑽過去（遊戲也不讓）
                if dx and dy and not (walk(nx, y) and walk(x, ny)):
                    continue
                ng = g0 + c
                if ng < best.get((nx, ny), 1e18):
                    best[(nx, ny)] = ng
                    came[(nx, ny)] = (x, y)
                    push(openq, (ng + hf(nx, ny), ng, nx, ny))
        return None

    def waypoints(self, start, goal, relax: int = GOAL_RELAX):
        """最短路 → **轉折點**（每段之間直線可通、且不超過 MAX_SEG 格）。

        直接把 A* 的每一格都當路徑點沒有意義（一段 180 格就是 180 個點）；
        拉直之後通常只剩幾個點，每一段交給遊戲的走路常式一次走完。
        """
        path = self.route(start, goal, relax)
        if not path:
            return None
        out = []
        anchor = 0
        i = 1
        while i < len(path):
            seg = math.dist(path[anchor], path[i])
            if seg > MAX_SEG or not self.clear_line(path[anchor], path[i]):
                out.append(path[i - 1])
                anchor = i - 1
            i += 1
        out.append(path[-1])
        # 去掉「原地」那種零長度段
        return [p for k, p in enumerate(out)
                if k == 0 or math.dist(p, out[k - 1]) > 0.5]


def _u32(scanner, addr: int):
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else None


def load(scanner) -> Grid | None:
    """讀出目前這張地圖的可走格；讀不到回 None（呼叫端要有退路）。

    實測 300x180 只要 5ms —— 一列一次讀取，180 次系統呼叫。
    """
    obj = _u32(scanner, MAP_PTR)
    if not obj or not 0x10000 < obj < 0x7FFF0000:
        return None
    w = _u32(scanner, obj + OFF_W)
    h = _u32(scanner, obj + OFF_H)
    rows = _u32(scanner, obj + OFF_ROWS)
    if not w or not h or not rows:
        return None
    if w > MAX_DIM or h > MAX_DIM or not 0x10000 < rows < 0x7FFF0000:
        return None
    raw = scanner._read_bytes(rows, h * 4)
    if not raw or len(raw) < h * 4:
        return None
    ptrs = struct.unpack(f"<{h}I", bytes(raw))
    open_rows: list[bytearray] = []
    for y in range(h):
        line = scanner._read_bytes(ptrs[y], w * CELL) if ptrs[y] else None
        if not line or len(line) < w * CELL:
            # 整列讀不到 → 當成不能走（保守），但不要讓整張圖失敗
            open_rows.append(bytearray(w))
            continue
        b = bytes(line)
        open_rows.append(bytearray(
            0 if (b[x * CELL + FLAG_OFF] & BLOCK_MASK) else 1
            for x in range(w)))
    return Grid(w, h, obj, open_rows)


class Cache:
    """一個角色一份。地圖沒換就重用，換了自動重讀。

    ⚠⚠ 「換了沒」**不能只看地圖物件的位址**。實測兩張完全不同的地圖
      （惡礁峽谷 96、人馬崗哨 123）**寬高都是 300x180**，物件大小一樣 ——
      換圖時配置器很可能把同一塊記憶體發回來，位址與寬高就都一樣了。
      那樣快取會拿**上一張圖的牆**去算這張圖的路：明明走得過去卻說到不了，
      或往牆裡走。頻繁換地圖的人一定會踩到。
    ★ 所以身分＝(物件位址, 寬, 高, **場景編號**, 列指標陣列的檢查碼)：
      · 場景編號是遊戲自己的地圖身分，最直接
      · 列指標陣列是每張圖各自配置的，當作場景編號讀不到時的保險
      成本是幾次小讀取（列陣列 h*4 ≈ 720 bytes），而且只在要規劃路線時問。
    """

    __slots__ = ("_grid", "_key")

    def __init__(self) -> None:
        self._grid: Grid | None = None
        self._key = None

    @staticmethod
    def _identity(scanner):
        """(物件, 寬, 高, 場景編號, 列指標檢查碼)；讀不到回 None。"""
        obj = _u32(scanner, MAP_PTR)
        if not obj or not 0x10000 < obj < 0x7FFF0000:
            return None
        w = _u32(scanner, obj + OFF_W)
        h = _u32(scanner, obj + OFF_H)
        rows = _u32(scanner, obj + OFF_ROWS)
        if not w or not h or not rows or w > MAX_DIM or h > MAX_DIM:
            return None
        raw = scanner._read_bytes(rows, h * 4)
        if not raw:
            return None
        from app.game import scene              # 這裡才 import：避免循環相依
        try:
            sid = scene.current_id(scanner, allow_scan=False)
        except Exception:                        # noqa: BLE001
            sid = None
        return obj, w, h, sid, zlib.crc32(bytes(raw))

    def get(self, scanner) -> Grid | None:
        key = self._identity(scanner)
        if key is None:
            self._grid, self._key = None, None
            return None
        if self._grid is not None and self._key == key:
            return self._grid
        self._grid = load(scanner)
        self._key = key if self._grid is not None else None
        return self._grid

    def drop(self) -> None:
        self._grid, self._key = None, None
