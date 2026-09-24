"""這張地圖的**傳點範圍**（格子集合）—— 自動戰鬥算路時當牆繞開。

    mapportal.cells(147, 300, 180)   → frozenset({(284, 21), ...})

表是 `tools/build_map_portals.py` 從 `.mpc` 抽的 `assets/map_portals.json`
（格式與出處見那支的檔頭）。**查不到一律回空集合**＝跟以前一樣不繞，
不會拿猜的值做事（安全退化）。

## 為什麼要有（2026-09-24 使用者回報）

掛機用地形圖 A* 自己算路，地形圖只有牆、**沒有傳點** → 追怪／回巡邏點的路線
擦過傳點就被傳去別張圖發呆。

## 範圍怎麼算

⚠ 事件的點怎麼圍成觸發範圍**沒有反組譯確認**（表裡只存原始點）。這裡取
  寬鬆的做法：點數是 3 的倍數就每 3 點一組（6／9 點＝2／3 個三角形共用一個
  事件），否則全部一組；每組取凸包，**凸包內＋離凸包 MARGIN 格內**的格子都算。
  多算只是少走幾格，少算就會踩進去 —— 寧可多算。
⚠ 表的寬高跟記憶體那張圖對不上（改版換圖、節慶換檔）→ 回空集合，不拿別張圖的
  座標硬套。
"""
from __future__ import annotations

import json
import math

from app.paths import resource

DATA_FILE = "assets/map_portals.json"
# 傳點範圍往外多留幾格（格中心到範圍的距離）。見檔頭「範圍怎麼算」。
# ★ 2026-09-24 拿 116 張有傳點的圖泛洪比過：1 格只有場景 4 切掉 65 格死角；
#   2 格會把傳點旁邊的窄路也封掉（場景 23 切掉 311 格、另有 17 張 30~270 格）
#   → 那些地方的怪／巡邏點就變成走不到。取 1 格。
MARGIN = 1.0
# ★ 掛機「盡量別靠近」的半徑（使用者 2026-09-24：「別靠近傳點半徑 5 格」；集會所四季花園
#   傳點在十字路口正中間，怪在對面就穿過中間被傳走）。MARGIN～AVOID 不當牆（當牆的話
#   116 張圖有 35 張會切掉一塊區域），只算很貴（terrain.SOFT_COST）＋不挑這裡的怪。
AVOID = 5.0

_table: dict | None = None
_cache: dict = {}
_fail = ""


def reload() -> None:
    """丟掉已載入的表（build 工具重跑完、測試用）。"""
    global _table, _fail
    _table, _fail = None, ""
    _cache.clear()


def _load() -> dict:
    global _table, _fail
    if _table is None:
        try:
            _table = json.loads(resource(DATA_FILE).read_text("utf-8"))
        except Exception as e:                   # noqa: BLE001 檔案缺了／壞了都當沒有
            _table, _fail = {}, f"讀不到 {DATA_FILE}：{e}"
    return _table


def status() -> str:
    """給診斷看的一行字。"""
    t = _load()
    if not t:
        return f"傳點表：沒有（{_fail or '表是空的'}）"
    return f"傳點表：{len(t)} 個場景、{sum(len(v['portals']) for v in t.values())} 個傳點"


def entry(scene_id: int | None) -> dict | None:
    """表裡那個場景的原始資料；分流先折成本流（同 `mapfile.grid_of`）。"""
    if scene_id is None:
        return None
    t = _load()
    got = t.get(str(scene_id))
    if got is None:
        from app.game import scene                # 避免載入時循環相依
        key = scene.map_key(scene_id)
        got = t.get(str(key)) if key is not None else None
    return got


def _hull(pts):
    """凸包（逆時針）；1~2 點原樣回傳。"""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]


def _seg_dist(p, a, b) -> float:
    ax, ay = b[0] - a[0], b[1] - a[1]
    L = ax * ax + ay * ay
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / L))
    return math.hypot(p[0] - a[0] - t * ax, p[1] - a[1] - t * ay)


def _dist(p, hull) -> float:
    """點到凸包的距離（在裡面＝0）。"""
    if len(hull) >= 3:
        inside = all((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0
                     for a, b in zip(hull, hull[1:] + hull[:1]))
        if inside:
            return 0.0
    if len(hull) == 1:
        return math.hypot(p[0] - hull[0][0], p[1] - hull[0][1])
    return min(_seg_dist(p, a, b) for a, b in zip(hull, hull[1:] + hull[:1]))


def _groups(pts):
    pts = [tuple(q) for q in pts]
    if len(pts) >= 6 and len(pts) % 3 == 0:
        return [pts[i:i + 3] for i in range(0, len(pts), 3)]
    return [pts]


def cells(scene_id: int | None, w: int, h: int,
          margin: float = MARGIN) -> frozenset:
    """那張圖所有傳點範圍（含 margin）的格子；查不到／寬高對不上回空集合。"""
    e = entry(scene_id)
    if not e or e.get("w") != w or e.get("h") != h:
        return frozenset()
    key = (e["file"], w, h, margin)
    got = _cache.get(key)
    if got is not None:
        return got
    out = set()
    for p in e["portals"]:
        for g in _groups(p["pts"]):
            hull = _hull(g)
            xs = [q[0] for q in g]
            ys = [q[1] for q in g]
            for y in range(max(0, int(min(ys) - margin) - 1), min(h, int(max(ys) + margin) + 2)):
                for x in range(max(0, int(min(xs) - margin) - 1), min(w, int(max(xs) + margin) + 2)):
                    if _dist((x + 0.5, y + 0.5), hull) <= margin:
                        out.add((x, y))
    got = _cache[key] = frozenset(out)
    return got
