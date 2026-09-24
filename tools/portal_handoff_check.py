"""怪在傳點旁 → 我們自己走到離怪 3 格才交棒（2026-09-24 使用者定）離線回歸測試。

    py tools\portal_handoff_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import inspect
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import terrain                          # noqa: E402
from app.tabs import farm_tab                         # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class Page:
    _portal_near = farm_tab.CharFarmPage._portal_near

    def __init__(self, cells):
        self._maps = types.SimpleNamespace(portal_set=frozenset(cells))


print("① _portal_near：怪的 HANDOFF_RANGE 格內有沒有傳點格")
p = Page({(50, 50), (51, 50)})
check("怪離傳點 5 格 → 近", p._portal_near((45.5, 50.5)))
check("怪離傳點 11.5 格 → 近", p._portal_near((39.0, 50.5)))
check("怪離傳點 20 格 → 不近", not p._portal_near((30.5, 50.5)))
check("沒傳點 → 不近", not Page(set())._portal_near((50.5, 50.5)))
check("沒座標 → 不近", not p._portal_near(None))
p2 = Page({(50, 50)})
p2._portal_near((45.5, 50.5))
p2._maps = types.SimpleNamespace(portal_set=frozenset({(200, 200)}))
check("換圖（傳點表換一份）→ 不拿舊答案", not p2._portal_near((45.5, 50.5)))

print("② 距離：停 3 格、開始按快捷鍵也是 3 格附近（不提早交棒）")
hand = farm_tab.PORTAL_HANDOFF_KEEP + 0.6
keep = min(max(hand - (2.0 if hand >= 6.0 else 0.6), farm_tab.move.MIN_GAP), hand - 0.5)
check("走位停在 3.0 格", abs(keep - 3.0) < 1e-9, str(keep))
check("比平常的交棒距離近很多", hand < farm_tab.HANDOFF_RANGE)
src = inspect.getsource(farm_tab.CharFarmPage.tick)
check("tick 的出手範圍／走位／送鍵距離都用 hand_rng（不是寫死 HANDOFF_RANGE）",
      "dist <= hand_rng" in src and "reach_keep = hand_rng" in src
      and "self._keys.reach = hand_rng" in src
      and "HANDOFF_RANGE if handoff" not in src)

print("③ terrain.Cache 留下傳點格")
c = terrain.Cache(avoid_portals=True)
check("預設空集合", c.portal_set == frozenset())

print("④ 傳點半徑 5 格：很貴、拉直線不准切過、不切斷區域（集會所十字路口，離線 .mpc）")
from app.game import mapfile, mapportal               # noqa: E402
g = mapfile.grid_of(147)
if g is None:
    check("集會所地圖讀得到", False, "mapfile 沒有 147")
else:
    hard = mapportal.cells(147, g.w, g.h)
    zone = mapportal.cells(147, g.w, g.h, mapportal.AVOID)
    before = len(g.reachable(125, 85) or ())
    for x, y in hard:
        g.open[y][x] = 0
    g.soft = frozenset(zone - hard)
    pts = [q["pts"] for q in mapportal.entry(147)["portals"] if q["to"] == 148][0]
    hull = mapportal._hull([tuple(q) for q in pts])

    def dmin(path):
        return min(mapportal._dist((x + 0.5, y + 0.5), hull) for x, y in path)

    for a, b in (((125, 85), (150, 77)), ((128, 81), (146, 81)), ((120, 90), (145, 72))):
        r = g.route(a, b)
        check(f"{a}→{b} 穿路口：路線離傳點 ≥ 4.5 格", r is not None and dmin(r) >= 4.5,
              str(r and round(dmin(r), 1)))
        wp = g.waypoints(a, b)
        seg_min = min(
            mapportal._dist((p0[0] + 0.5 + (p1[0] - p0[0]) * k / 20,
                             p0[1] + 0.5 + (p1[1] - p0[1]) * k / 20), hull)
            for p0, p1 in zip(wp, wp[1:]) for k in range(21))
        check("　轉折點之間的直線也離傳點 ≥ 4 格（沒抄近路切過中間）", seg_min >= 4.0,
              str(round(seg_min, 1)))
    check("直線切過路口中間 → clear_line 說不通", not g.clear_line((128, 81), (146, 81)))
    after = len(g.reachable(125, 85) or ())
    check("區域沒被切斷（只少了傳點那幾格）", before - after <= len(hard), f"{before}→{after}")

print("⑤ 掛機不挑傳點 5 格內的怪（_candidates 有這道）")
src5 = inspect.getsource(farm_tab.CharFarmPage._candidates)
check("有 portal_zone 過濾、正在打我的照打", "portal_zone" in src5 and "not hits_me and zone" in src5)

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
