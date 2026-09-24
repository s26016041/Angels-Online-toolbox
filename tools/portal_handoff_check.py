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

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
