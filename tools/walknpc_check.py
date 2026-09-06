"""`supply._walk_to_npc` 的到達判定 —— 離線測試（不碰遊戲、不碰 Qt）。

    py tools\\walknpc_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的是 2026-09-06 黑狐實錄的那個坑（memory supply-walk-to-npc-30s-hang）：
    使用者：「銀行／維修前面卡很久，等很久後他沒動就能講到話；藥水商人卻秒說到話」
    根因＝ Navigator 直線 ≤ ARRIVE(3.0) 就當「到了」不再動，而 _walk_to_npc 只認
    曼哈頓 ≤ ARRIVE_TILES(2) → 人停在 2.9 格斜角（曼哈頓 3）站著磨滿 30 秒逾時。
規則：
    ① 直線進到 navigate.ARRIVE 內就要**馬上**回 True（不准等逾時）
    ② 還沒到就一直 step，Navigator 把人帶進圈子就回
    ③ Navigator 舉 exhausted（路線走完卻沒進圈）且人停了 → 夠近回 True、太遠回 False，
       兩種都**馬上**回，不空轉
⚠ 純離線：假時鐘／假地形圖／假 Navigator，**只換 I/O，判斷邏輯跑真的**。
"""
from __future__ import annotations

import math
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.game import navigate, supply                     # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


CLOCK = Clock()
supply.time = CLOCK

NPC = (137, 161)                      # 永夜城銀行小姐艾寶的真實格（實測）
POS = [138.5, 163.46875]              # 人站的地方（可變）
PF = 0x30DD11D0


class FakeGrid:
    def reachable(self, cx, cy):
        return {NPC, (cx, cy), (138, 163), (141, 161), (145, 161), (160, 161)}


class FakeNav:
    """假 Navigator：step 時照 MOVE 把人往目標推（0 ＝ 不動）；EXHAUST 時舉旗不動。"""
    MOVE = 0.0
    EXHAUST = False
    steps = 0

    def __init__(self):
        self.stuck = False
        self.exhausted = False
        self.note = ""

    def reset(self, goal=None):
        self.goal = goal

    def step(self, scanner, mover, player_obj, gx, gy, arrive=navigate.ARRIVE):
        FakeNav.steps += 1
        if FakeNav.EXHAUST:
            self.exhausted = True
            return "最短路只能到這裡"
        if FakeNav.MOVE > 0:
            dx, dy = gx - POS[0], gy - POS[1]
            dd = math.hypot(dx, dy) or 1.0
            POS[0] += dx / dd * FakeNav.MOVE
            POS[1] += dy / dd * FakeNav.MOVE
        return "走"


supply.terrain = types.SimpleNamespace(load=lambda sc: (FakeGrid(), None))
supply.navigate = types.SimpleNamespace(Navigator=FakeNav, ARRIVE=navigate.ARRIVE)
supply.entity = types.SimpleNamespace(is_walking=lambda sc, obj: False)
supply._player_tile = lambda sc: (PF, (POS[0], POS[1]))
supply._npc_tile = lambda sc, nid: NPC


def run(timeout=30.0):
    t0 = CLOCK.t
    FakeNav.steps = 0
    ok = supply._walk_to_npc(object(), object(), 1890, (129, 168), timeout)
    return ok, CLOCK.t - t0


print("① 站在 2.9 格斜角（曼哈頓 3）：Navigator 說到了，我們也要馬上回")
POS[:] = [138.5, 163.46875]
FakeNav.MOVE, FakeNav.EXHAUST = 0.0, False
ok, dt = run()
d_eu = math.hypot(POS[0] - NPC[0], POS[1] - NPC[1])
check("這個站位真的是「直線 ≤3、曼哈頓 >2」的案例", d_eu <= navigate.ARRIVE and
      abs(round(POS[0]) - NPC[0]) + abs(round(POS[1]) - NPC[1]) > supply.ARRIVE_TILES,
      f"直線 {d_eu:.2f}")
check("回 True", ok is True)
check("⛔ 不磨逾時：<1 秒就回（舊寫法要 30 秒）", dt < 1.0, f"花了 {dt:.1f} 秒")

print()
print("② 8 格外：一路 step 到進圈就回")
POS[:] = [145.0, 161.0]
FakeNav.MOVE, FakeNav.EXHAUST = 1.0, False
ok, dt = run()
check("回 True", ok is True)
check("有真的在走（step 被叫到）", FakeNav.steps >= 5, f"steps={FakeNav.steps}")
check("進圈就回（不磨逾時）", dt < 5.0, f"花了 {dt:.1f} 秒")
check("停在 Navigator 的到達圈內", math.hypot(POS[0] - NPC[0], POS[1] - NPC[1]) <= navigate.ARRIVE)

print()
print("③ Navigator 舉 exhausted（路線走完卻沒進圈）且人停了：馬上回，不空轉")
POS[:] = [141.0, 161.0]                       # 4 格：夠近（≤ NEAR_ENOUGH）
FakeNav.MOVE, FakeNav.EXHAUST = 0.0, True
ok, dt = run()
check("夠近 → True", ok is True)
check("馬上回（<2 秒）", dt < 2.0, f"花了 {dt:.1f} 秒")
POS[:] = [160.0, 161.0]                       # 23 格：太遠
ok, dt = run()
check("太遠 → False（不能假裝到了）", ok is False)
check("一樣馬上回", dt < 2.0, f"花了 {dt:.1f} 秒")

print()
if FAILS:
    print(f"FAIL {len(FAILS)}：" + "、".join(FAILS))
    sys.exit(1)
print("OK")
