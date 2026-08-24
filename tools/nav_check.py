"""長距離導航（app/game/navigate.py）離線測試 —— 假地形圖＋假角色，不碰遊戲。

驗的規格：
    ① 「走不到」要分得出兩種（呼叫端處理方式完全不同）
         "grid"    地形圖說根本沒有路 → 真的到不了
         "blocked" 路是通的、只是一直被擋著 → **暫時性失敗**，不准拿來停掛機
    ② 只要真的往前走了（走到下一個轉折點、或離目前這個轉折點又近了），
       重算額度就要**還回去** —— 這是 2026-08-24 使用者回報
       「巡邏點設得到就一定走得到，為什麼會走不到就停」的根因：
       舊版 `_replans` 整趟累加不歸零，長路上被擋三次就被判走不到，
       那張圖只有一個巡邏點時直接停掉掛機。
    ③ 真的原地不動（連續重算都毫無進展）還是要判 stuck —— 修的是誤判，
       不是把煞車拆掉。

用法：py tools\nav_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.game import navigate                      # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✔ " if ok else "  ✘ ") + name + ("" if ok else f"　{extra}"))
    if not ok:
        FAILS.append(name)


# ── 假的時鐘：SEND_GRACE 是真的 0.3 秒，測試不能真的睡 ──────────────────
class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


CLOCK = Clock()
navigate.time = types.SimpleNamespace(monotonic=CLOCK.monotonic)


class FakeGrid:
    """地形圖替身。

    ⚠⚠ **一定要跟真的一樣「從現在的位置重算」**：真的 `Grid.waypoints()` 是拿
      當下座標跑 A*，所以重算之後第一個轉折點永遠在**前方**。第一版這個替身
      不管站在哪都回同一串點，於是重算後 `_route[0]` 落在角色後面、永遠走不到
      —— 那是替身在說謊，測到的東西跟實機無關（[[test-via-button]]）。
    """

    def __init__(self, wp):
        self.wp = wp
        self.asked = 0

    def waypoints(self, start, goal):
        self.asked += 1
        if self.wp is None:
            return None
        ahead = [p for p in self.wp if p[0] > start[0] + 0.5]
        return ahead or None


class FakeMaps:
    """terrain.Cache 替身。⚠ 介面要跟真的一樣（get / why / drop）。"""

    def __init__(self, grid):
        self.grid = grid
        self.why = ""
        self.drops = 0

    def get(self, scanner):
        return self.grid

    def drop(self):
        self.drops += 1


POS = [0.0, 0.0]
WALKING = [False]
navigate.entity = types.SimpleNamespace(
    read_pos=lambda sc, obj: (POS[0], POS[1]),
    is_walking=lambda sc, obj: WALKING[0])

SENT = []
MOVER = types.SimpleNamespace(
    active=True,
    walk_route=lambda sc, obj, x, y, stop_short=0.0, points=None:
        SENT.append((x, y)))


def build(wp):
    """一個導航器 ＋ 它的假地形圖。"""
    grid = FakeGrid(wp)
    maps = FakeMaps(grid)
    return navigate.Navigator(maps), maps


def step(nav, x=None, y=None, gap=0.31):
    """推一拍。`gap` 預設跨過 SEND_GRACE，讓 stall 真的數得到。"""
    if x is not None:
        POS[0], POS[1] = float(x), float(y)
    CLOCK.t += gap
    return nav.step(None, MOVER, object(), 100.0, 0.0)


GOAL_WP = [(10, 0), (20, 0), (30, 0), (40, 0), (100, 0)]

print("① 地形圖說沒有路 → stuck，而且說得出是 \"grid\"")
nav, maps = build(None)
POS[0], POS[1] = 0.0, 0.0
step(nav)
check("第一次不判死（剛傳送完圖跟座標可能還對不起來）", nav.stuck is False)
check("而且會把圖丟掉重讀一次", maps.drops == 1)
step(nav)
check("第二次才判 stuck", nav.stuck is True)
check("理由是 grid（真的到不了）", nav.stuck_reason == "grid",
      f"實得 {nav.stuck_reason!r}")

print("② 完全原地不動 → 還是要判 stuck，理由是 blocked（煞車沒被拆掉）")
nav, maps = build(GOAL_WP)
SENT.clear()
POS[0], POS[1] = 0.0, 0.0
WALKING[0] = False
for _ in range(200):
    step(nav)                       # 位置從頭到尾不動
    if nav.stuck:
        break
check("原地不動最後會判 stuck", nav.stuck is True)
check("理由是 blocked（路被擋住，不是地形圖說沒路）",
      nav.stuck_reason == "blocked", f"實得 {nav.stuck_reason!r}")
check("訊息講得出是「完全沒往前走」",
      "沒往前走" in nav.note, f"實得「{nav.note}」")

print("③ ★ 一路被擋、但每次都有往前走 → **不准**判走不到（2026-08-24 的 bug）")
# 走一趟 100 格的巡邏路：每個轉折點前都先被擋到觸發一次重算，然後脫困往前走。
# 舊版 `_replans` 累加不歸零 → 第 4 次就被判「走不到」→ 只有一個巡邏點時停機。
nav, maps = build(GOAL_WP)
SENT.clear()
POS[0], POS[1] = 0.0, 0.0
WALKING[0] = False
blocked_rounds = 0
for _ in range(4):
    # ① 被擋：位置不動，推到觸發一次重算為止（_stall 滿 → _route=None）
    got_replan = False
    for _ in range(40):
        before = nav._replans
        step(nav)
        if nav.stuck:
            break
        if nav._replans > before:
            got_replan = True
            break
    if nav.stuck:
        break
    blocked_rounds += got_replan
    # ② 脫困：先讓它重算（站著不動一拍），再真的走到它算出來的下一個轉折點
    step(nav)
    if nav._route is None or nav._ri >= len(nav._route):
        break
    pt = nav._route[nav._ri]
    step(nav, pt[0], pt[1])
check("整趟被擋了 4 次（每次都真的觸發過重算）", blocked_rounds == 4,
      f"實得 {blocked_rounds}")
check("★ 但**沒有**被判走不到", nav.stuck is False,
      f"實得 stuck={nav.stuck} reason={nav.stuck_reason!r} note「{nav.note}」")
check("重算額度有被還回去", nav._replans == 0, f"實得 {nav._replans}")

print("④ 走到轉折點就把額度歸零（不必等離目標更近）")
nav, maps = build(GOAL_WP)
POS[0], POS[1] = 0.0, 0.0
step(nav)
nav._replans = navigate.REPLAN_MAX      # 假裝前面已經用掉全部額度
step(nav, 10, 0)                        # 真的走到第一個轉折點
check("走到轉折點 → _replans 歸零", nav._replans == 0, f"實得 {nav._replans}")

print("⑤ 換目標時整組狀態要重來（stuck_reason 也要清）")
nav, maps = build(None)
POS[0], POS[1] = 0.0, 0.0
step(nav)
step(nav)
check("先讓它 stuck", nav.stuck is True and nav.stuck_reason == "grid")
nav.reset()
check("reset 之後 stuck 清掉", nav.stuck is False)
check("reset 之後 stuck_reason 也清掉", nav.stuck_reason == "",
      f"實得 {nav.stuck_reason!r}")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
