"""`supply._walk_to_npc` 的到達判定 —— 離線測試（不碰遊戲、不碰 Qt）。

    py tools\\walknpc_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的是 2026-09-06 黑狐三段實錄的坑（memory supply-walk-to-npc-30s-hang、nav-blocked-detour）：
    上午：Navigator 直線 ≤ ARRIVE(3.0) 就當「到了」，_walk_to_npc 只認曼哈頓 ≤2 → 站著磨 30 秒。
    晚上：改「站上目標格」又太緊 —— 商人本人站在可走格時目標＝他本格、伺服器不給站 → 磨 30 秒；
          而棕櫚基地銀行唯一能講話的格被玩家「倉用4」站著 → 磨 4 輪 51~72 秒才放棄。
規則（現行，使用者 2026-09-06 定「直接走到離 NPC 最近可到的格」）：
    ① 人已站在離 NPC 最近可到的格 → **馬上**回 True，一步都不走
    ② 還沒 → 目標＝離 NPC 最近、可走、我這區走得到的格，**站上去**才回（不用講話方框當停止線）
    ③ Navigator 舉 exhausted 且人停了 → 夠近回 True、太遠回 False，兩種都馬上回
    ④ NPC 周圍沒有可到格 → 退回「離 NPC 最近的可走格」照走
    ⑤ 最後一步走不進去（目標格被人站著、Navigator 說 stuck）→ 離目標 ≤2 格就算到，不磨 30 秒
    ⚠ 人牆偵測（被別人站著就放棄）使用者定刪掉，沒有這條規格。
⚠ 純離線：假時鐘／假地形圖／假 Navigator／假實體，**只換 I/O，判斷邏輯跑真的**。
"""
from __future__ import annotations

import math
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.game import navigate, supply                     # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✔ " if ok else "  ✘ ") + name + ("" if ok else f"　{extra}"))
    if not ok:
        FAILS.append(name)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


CLOCK = Clock()
supply.time = CLOCK

NPC = (137, 161)                      # 永夜城銀行小姐（地形圖說她那格可走）
NPC_ID = 1890
POS = [138.5, 163.46875]              # 人站的地方（可變）
PF = 0x30DD11D0
REACH = set()                         # 假地形圖「我這區走得到」的格（各測試自己設）


class FakeGrid:
    def reachable(self, cx, cy):
        return set(REACH) | {(cx, cy)}


class FakeNav:
    """假 Navigator：step 時照 MOVE 把人往目標推（0 ＝ 不動），進 `arrive` 內就不動；
    EXHAUST 時舉旗不動。記下最近一次收到的 arrive 跟目標。"""
    MOVE = 0.0
    EXHAUST = False
    steps = 0
    last_arrive = None
    last_goal = None

    def __init__(self):
        self.stuck = False
        self.exhausted = False
        self.note = ""

    def reset(self, goal=None):
        self.goal = goal

    def step(self, scanner, mover, player_obj, gx, gy, arrive=navigate.ARRIVE):
        FakeNav.steps += 1
        FakeNav.last_arrive = arrive
        FakeNav.last_goal = (gx, gy)
        if FakeNav.EXHAUST:
            self.exhausted = True
            return "最短路只能到這裡"
        dx, dy = gx - POS[0], gy - POS[1]
        dd = math.hypot(dx, dy)
        if dd <= arrive:
            return "到了"
        if FakeNav.MOVE > 0:
            # 跟真的一樣：送出去的走路指令是走到目標點**本身**（遊戲會把人走到格中心停），
            # 所以最後一段直接落在目標上，不會停在半格外。
            if dd <= FakeNav.MOVE + arrive:
                POS[0], POS[1] = gx, gy
            else:
                POS[0] += dx / dd * FakeNav.MOVE
                POS[1] += dy / dd * FakeNav.MOVE
        return "走"


supply.terrain = types.SimpleNamespace(load=lambda sc: (FakeGrid(), None))
supply.navigate = types.SimpleNamespace(Navigator=FakeNav, ARRIVE=navigate.ARRIVE)
supply.entity = types.SimpleNamespace(is_walking=lambda sc, obj: False)
supply.move = types.SimpleNamespace(pathfinder_this=lambda sc: PF)
supply._player_tile = lambda sc: (PF, (POS[0], POS[1]))
supply._npc_tile = lambda sc, nid: NPC
supply.find_npc = lambda sc, nid: (0x1000, None)                 # NPC 一直看得到
supply._ent_tile_f = lambda sc, e: (NPC[0] + 0.5, NPC[1] + 0.5)
supply._act_size = lambda sc, e: 1


def run(timeout=30.0):
    t0 = CLOCK.t
    FakeNav.steps = 0
    FakeNav.last_arrive = FakeNav.last_goal = None
    ok = supply._walk_to_npc(object(), object(), NPC_ID, (129, 168), timeout)
    return ok, CLOCK.t - t0


def in_box():
    return supply._in_talk_box((int(POS[0]), int(POS[1])), 1, NPC, 1)


BOX_TILES = {(139, 161), (138, 163), (135, 159)}     # 框內可走的格

print("① 人已站在離 NPC 最近可到的格 (139,161)：馬上回 True，一步不走")
POS[:] = [139.5, 161.46875]
REACH.clear(); REACH.update(BOX_TILES)
FakeNav.MOVE, FakeNav.EXHAUST = 1.0, False
ok, dt = run()
check("回 True", ok is True)
check("★ 一步都沒走（step 沒被叫到）", FakeNav.steps == 0, f"steps={FakeNav.steps}")
check("⛔ 不磨逾時：<1 秒就回", dt < 1.0, f"花了 {dt:.1f} 秒")

print()
print("② 8 格外：目標＝離 NPC 最近可到的格 (139,161)，站上去才回")
POS[:] = [145.0, 161.0]
FakeNav.MOVE, FakeNav.EXHAUST = 1.0, False
ok, dt = run()
check("回 True", ok is True)
check("★ 目標是離 NPC 最近可到格的中心 (139.5,161.5)", FakeNav.last_goal == (139.5, 161.5),
      f"實得 {FakeNav.last_goal}")
check("★ Navigator 收到 arrive=NPC_ARRIVE", FakeNav.last_arrive == supply.NPC_ARRIVE,
      f"實得 {FakeNav.last_arrive}")
check("有真的在走（step 被叫到）", FakeNav.steps >= 3, f"steps={FakeNav.steps}")
check("★ 站上那格才回（不是進方框就停）", (int(POS[0]), int(POS[1])) == (139, 161), f"停在 {POS}")
check("不磨逾時", dt < 8.0, f"花了 {dt:.1f} 秒")

print()
print("③ Navigator 舉 exhausted（路線走完卻沒進框）且人停了：馬上回，不空轉")
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
print("④ 框內沒有任何可走格（地形圖跟遊戲對不上）→ 退回離 NPC 最近的可走格照走")
POS[:] = [150.0, 161.0]
REACH.clear(); REACH.update({(143, 161), (160, 161)})     # 框外才有可走格（(143,161) Δx=6）
FakeNav.MOVE, FakeNav.EXHAUST = 1.0, False
ok, dt = run()
check("回 True（站上最近可走格）", ok is True)
check("目標是 (143,161) 的中心", FakeNav.last_goal == (143.5, 161.5), f"實得 {FakeNav.last_goal}")
check("停在目標格上", (int(POS[0]), int(POS[1])) == (143, 161), f"停在 {POS}")

print()
print("⑤ ★ 最後一步走不進去（目標格被人站著）：Navigator 說 stuck、離目標 ≤2 格 → 算到，不磨 30 秒")
POS[:] = [140.5, 161.46875]                   # 目標 (139,161) 的隔壁格
REACH.clear(); REACH.update(BOX_TILES)
FakeNav.MOVE, FakeNav.EXHAUST = 0.0, False
_orig_step = FakeNav.step
def _stuck_step(self, *a, **k):
    r = _orig_step(self, *a, **k)
    self.stuck = True                          # 走不動 → 導航器舉 stuck
    return r
FakeNav.step = _stuck_step
ok, dt = run()
FakeNav.step = _orig_step
check("回 True（旁邊這格就是實際最近可到的格）", ok is True)
check("⛔ 馬上回（<3 秒，舊寫法磨 30 秒）", dt < 3.0, f"花了 {dt:.1f} 秒")

print()
if FAILS:
    print(f"FAIL {len(FAILS)}：" + "、".join(FAILS))
    sys.exit(1)
print("OK")
