"""傳點表（`assets/map_portals.json`）自我檢查＋實機純讀核對。

    py tools\\portal_check.py          # 離線：表載得進來、集會所三個傳點、範圍不會封死窄路
    py tools\\portal_check.py --live   # 另外列出每台分身現在這張圖的傳點與自己的距離

⚠ 純讀，不寫入、不送封包。站到傳點旁邊跑 --live，看列出的傳點位置對不對得上。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import mapfile, mapportal  # noqa: E402


def offline() -> bool:
    ok = True
    print(mapportal.status())
    e = mapportal.entry(147)
    got = sorted(p["to"] for p in e["portals"]) if e else None
    good = got == [140, 146, 148]
    ok &= good
    print(f"{'✔' if good else '✘'} 集會所(147) 傳點目標 {got}")
    # 範圍落在可走格上（格 y 有沒有翻對的旁證：翻錯會大量落在牆裡）
    tot = hit = 0
    for sid_s in mapportal._load():
        g = mapfile.grid_of(int(sid_s))
        if not g:
            continue
        c = mapportal.cells(int(sid_s), g.w, g.h, 0.0)
        tot += len(c)
        hit += sum(1 for q in c if g.walkable(*q))
    rate = hit / tot if tot else 0
    good = rate > 0.8
    ok &= good
    print(f"{'✔' if good else '✘'} 傳點範圍落在可走格 {hit}/{tot}（{rate:.0%}）")
    return ok


def live() -> None:
    from app.core import preload
    from app.core.memory import MemoryScanner
    from app.game import entity, locate, scene, terrain

    for w in preload.windows():
        sc = MemoryScanner()
        try:
            sc.open(w.pid)
            locate.warm(sc)
            sid = scene.current_id(sc, allow_scan=False)
            snap = entity.snapshot(sc)
            me = entity.player_pos(sc, snap[1]) if snap and snap[1] else None
            c = terrain.Cache(avoid_portals=True)
            g = c.get(sc)
            e = mapportal.entry(sid)
            print(f"pid {w.pid} 場景 {sid} 我在 {me} 圖 {g and (g.w, g.h)} "
                  f"蓋了 {c.portal_cells} 格傳點")
            for p in (e or {}).get("portals", []):
                cx = sum(q[0] for q in p["pts"]) / len(p["pts"])
                cy = sum(q[1] for q in p["pts"]) / len(p["pts"])
                d = (((cx - me[0]) ** 2 + (cy - me[1]) ** 2) ** 0.5) if me else None
                print(f"    → 場景 {p['to']:<5} 中心 ({cx:.0f},{cy:.0f})"
                      + (f"  離我 {d:.1f} 格" if d is not None else ""))
        except Exception as exc:                          # noqa: BLE001
            print(f"pid {w.pid}：讀不到（{exc}）")


def main() -> int:
    ok = offline()
    if "--live" in sys.argv:
        live()
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
