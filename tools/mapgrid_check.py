"""離線地形表 ＋「傳送點挑路徑最短」的回歸測試。

    py tools\\mapgrid_check.py            離線測試（不碰遊戲）
    py tools\\mapgrid_check.py --live     再加上實機逐格對帳（遊戲要開著）

驗的規格
--------
① `assets/map_grids.bin.gz` 載得進來、場景數對得上 `stage.xml`。
② `jumpmap.nearest()` 挑的是**走過去最短**，不是直線最近 ——
   2026-09-16 使用者回報：「補給用趴趴GO 回巡邏點、右鍵傳送到巡邏點，
   有些地圖有多個傳送點，要按路徑選最短的那個。」
   隔著一道牆的傳送點直線很近，走過去卻要繞一大圈。
③ **算不出來一律退回直線最近，⛔ 不准回 None** —— 回程回不去是出過事的
   （memory `jump-back-channel-fix`）。沒有地形圖、目標在封閉區、傳送點
   全都走不到，三種都要退化而不是停掉。
④ 分流編號（高 16 位是分流序號）查得到本流那張圖。
⑤ `--live`：離線表 vs `terrain.load()` 讀到的記憶體地形**逐格**比對，
   不同格數必須是 0（2026-09-16 五台 191,000 格驗過）。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.game import jumpmap, mapfile                        # noqa: E402
from app.game.terrain import Grid                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def check(name, ok, extra=""):
    print(("  ✔ " if ok else "  ✘ ") + name + ("" if ok else f"　{extra}"))
    if not ok:
        FAILS.append(name)


# ── 假地形：x=5 是一道牆，只有最底下 y=19 有缺口 ──────────────────────
#   目標 (2,5)。傳送點 A(8,5) 直線只有 6 格，但要繞到 y=19 再回來；
#   傳送點 B(2,15) 直線 10 格，一路直通。→ 正解是 B。
W = H = 20


def walled_grid() -> Grid:
    rows = [bytearray([1] * W) for _ in range(H)]
    for y in range(H - 1):                 # 留 y=19 當缺口
        rows[y][5] = 0
    return Grid(W, H, 0, rows)


def sealed_grid() -> Grid:
    """整道牆封死：目標那側跟傳送點完全不連通。"""
    rows = [bytearray([1] * W) for _ in range(H)]
    for y in range(H):
        rows[y][5] = 0
    return Grid(W, H, 0, rows)


E_A = jumpmap.Entry(101, 999, 8, 5, "A_直線近但要繞")
E_B = jumpmap.Entry(102, 999, 2, 15, "B_直線遠但直通")
E_C = jumpmap.Entry(103, 999, 8, 15, "C_跟A同一側")


def with_fakes(grid, cand=(E_A, E_B)):
    """暫時換掉 by_scene / _grid_for，回一個還原用的函式。

    ⚠ 每個案例都要清挑選快取 —— 同一個 (場景,目標,傳送點) 在不同的假地形上
      答案不一樣，不清會拿到上一個案例的結果。
    """
    jumpmap.clear_pick_cache()
    old_by, old_grid = jumpmap.by_scene, jumpmap._grid_for
    jumpmap.by_scene = lambda sid: list(cand)
    jumpmap._grid_for = lambda sid, sc=None: (grid, "測試")

    def restore():
        jumpmap.by_scene = old_by
        jumpmap._grid_for = old_grid
        jumpmap.clear_pick_cache()
    return restore


def offline_tests() -> None:
    print("① 離線地形表")
    mapfile.reload()
    st = mapfile.status()
    print("   " + st)
    check("表載得進來", mapfile.has(126), st)
    g = mapfile.grid_of(126)
    check("千夜魔宮(126) 380x250", g is not None and (g.w, g.h) == (380, 250),
          f"讀到 {(g.w, g.h) if g else None}")
    stage = (ROOT / "GAMEDATA" / "setting" / "base" / "stage.xml")
    if stage.exists():
        want = len(set(re.findall(r'<場景 編號="(\d+)"[^>]*?地圖檔="map\\',
                                  stage.read_text("utf-8"))))
        check(f"場景數對得上 stage.xml（{want}）",
              len(mapfile._scenes) == want, f"表裡 {len(mapfile._scenes)}")
    else:
        print("   （沒有 GAMEDATA，跳過場景數比對）")

    print("④ 分流折回本流")
    # 暴走穗海農場分流：高 16 位 = 分流序號（見 scene.map_key）
    check("分流編號查得到地圖", mapfile.has((1 << 16) | 441) or not mapfile.has(441),
          "分流查不到但本流查得到 → map_key 沒生效")

    print("② 挑路徑最短，不是直線最近")
    restore = with_fakes(walled_grid())
    try:
        got = jumpmap.nearest(999, 2, 5)
        check("隔一道牆的 A 不該被選中（正解 B）", got is E_B,
              f"挑到 {got}　{jumpmap.last_pick()}")
        straight = min((E_A, E_B), key=lambda e: (e.x - 2) ** 2 + (e.y - 5) ** 2)
        check("（前提）直線最近的確實是 A", straight is E_A)
    finally:
        restore()

    print("③ 算不出來要退回直線最近，不准回 None")
    # 牆封死：目標在左側，兩個傳送點都在右側 → 一個都走不到
    restore = with_fakes(sealed_grid(), cand=(E_A, E_C))
    try:
        got = jumpmap.nearest(999, 2, 5)
        check("傳送點全都走不到 → 退回直線最近 A", got is E_A,
              f"挑到 {got}　{jumpmap.last_pick()}")
    finally:
        restore()

    # 同一道封死的牆，但 B 在目標那一側 → 要挑走得到的 B（不是直線近的 A）
    restore = with_fakes(sealed_grid())
    try:
        got = jumpmap.nearest(999, 2, 5)
        check("直線最近的那個走不到 → 挑走得到的 B", got is E_B,
              f"挑到 {got}　{jumpmap.last_pick()}")
    finally:
        restore()

    restore = with_fakes(None)               # 完全沒有地形圖
    try:
        got = jumpmap.nearest(999, 2, 5)
        check("沒有地形圖 → 退回直線最近 A", got is E_A,
              f"挑到 {got}　{jumpmap.last_pick()}")
    finally:
        restore()

    restore = with_fakes(walled_grid(), cand=(E_A,))
    try:
        check("只有一個傳送點就直接給它", jumpmap.nearest(999, 2, 5) is E_A)
        check("沒給座標給第一個", jumpmap.nearest(999) is E_A)
    finally:
        restore()

    restore = with_fakes(walled_grid(), cand=())
    try:
        check("那張圖沒有傳送點 → None", jumpmap.nearest(999, 2, 5) is None)
    finally:
        restore()

    print("⑥ 同一題不准每拍重算（掛機 tick 在 UI 執行緒上）")
    restore = with_fakes(walled_grid())
    try:
        calls = [0]
        real = jumpmap._shortest_walk

        def counted(*a, **kw):
            calls[0] += 1
            return real(*a, **kw)
        jumpmap._shortest_walk = counted
        try:
            for _ in range(5):
                jumpmap.nearest(999, 2, 5)
            check("問 5 次只真的算 1 次", calls[0] == 1, f"算了 {calls[0]} 次")
            check("快取命中也回同一個答案",
                  jumpmap.nearest(999, 2, 5) is E_B, jumpmap.last_pick())
            jumpmap.nearest(999, 18, 18)        # 換目標要重算
            check("換目標要重算", calls[0] == 2, f"算了 {calls[0]} 次")
        finally:
            jumpmap._shortest_walk = real
    finally:
        restore()


def live_tests() -> None:
    print("⑤ 實機逐格對帳（純讀，不碰遊戲）")
    from app.core.memory import MemoryScanner
    from app.core import window as win
    from app.game import terrain, scene, locate

    seen = 0
    for wnd in win.enumerate_windows(title_contains="Angels Online"):
        sc = MemoryScanner()
        try:
            sc.open(wnd.pid)
        except Exception as e:
            print(f"   PID {wnd.pid} 開不起來：{e}")
            continue
        locate.warm(sc)
        sid = scene.current_id(sc)
        live, why = terrain.load(sc)
        if live is None:
            print(f"   PID {wnd.pid} 記憶體地形讀不到（{why}）—— 跳過")
            continue
        off = mapfile.grid_of(sid)
        if off is None:
            check(f"PID {wnd.pid} 場景{sid} 離線表有這張", False, "表裡沒有")
            continue
        if (off.w, off.h) != (live.w, live.h):
            check(f"PID {wnd.pid} 場景{sid} 寬高相同", False,
                  f"離線 {off.w}x{off.h} vs 記憶體 {live.w}x{live.h}")
            continue
        diff = sum(1 for y in range(live.h) for x in range(live.w)
                   if off.open[y][x] != live.open[y][x])
        check(f"PID {wnd.pid} 場景{sid}（{scene.scene_name(sid)}）"
              f"{live.w}x{live.h} 逐格相同", diff == 0, f"不同 {diff} 格")
        seen += 1
    if not seen:
        print("   ⚠ 一台都沒對到（遊戲沒開、或都停在登入頁）")


def main() -> int:
    offline_tests()
    if "--live" in sys.argv:
        live_tests()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}：" + "、".join(FAILS))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
