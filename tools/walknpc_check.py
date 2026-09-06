"""`supply._walk_to_npc`（我們自己的走路：站到離 NPC 最近的可走格）—— 離線測試（不碰遊戲、不碰 Qt）。

    py tools\\walknpc_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

規則（2026-09-06 使用者定：「移動的部分都是我們自己處理，對話留給官方」，memory
npc-approach-move-ours-talk-official）：
    ① 目標＝離 NPC 真實座標最近的**可走格、不含 NPC 本格**（貼身＝站旁邊；NPC 常在櫃檯後
       不可到達，那就是最近能站的格）；到達＝離那格中心 ≤ NPC_ARRIVE 就馬上回 True。
       舊版停在 2.9 格斜角（Navigator ARRIVE 3.0）就算到 → 現在要走到貼身格，Navigator 的
       arrive 也要傳 NPC_ARRIVE，不然它 3 格內什麼都不做。已經站在貼身格 → 秒回、不下指令。
    ② 遠一點也一路 step 到貼身格才回；目標不變就不換 Navigator。
    ③ 貼身段（NPC 已串流）被人牆擋住：連續 APPROACH_STALL 秒沒更靠近就回 False，不磨逾時
       （回去切官方 TryAct，點不開再往他身上擠）。
    ④ 遠路段（NPC 沒串流、錨＝表座標）被擋**不**提早放棄（交給 Navigator 重算）；NPC 中途
       串流進來 → 每 2 秒重規劃改錨真座標，同一趟走到貼身格。
    ⑤ Navigator 舉 exhausted（路線走完卻沒進圈）且人停了 → 馬上回 False，不空轉。
    ⑥ _nearest_reachable：只剩 NPC 本格可站才回本格；錨點靠格子邊時外一圈的邊格比這圈
       的角格近 → 要挑到外圈那格。
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

NPC_F = (137.4, 161.6)                # 永夜城銀行小姐艾寶：真座標（浮點格，實測附近）
NPC_T = (137, 161)                    # 她本格（地形圖說可走，實測）
BESIDE = (137, 162)                   # 離她最近、不是本格、同距離裡離出發點近的那格
BESIDE_C = (137.5, 162.5)             # 那格中心（Navigator 的目標）
FALLBACK = (129, 168)                 # .MPC 表座標（不準，離真 NPC 差 10 格）
POS = [138.5, 163.46875]              # 人站的地方（可變）
PF = 0x30DD11D0
NPC_VISIBLE = [True]                  # NPC 串流進來了沒
REGION = {(x, y) for x in range(130, 161) for y in range(155, 171)}   # 一整片可走（表座標 (129,168) 那格不可走，跟實機一樣）


class FakeGrid:
    def reachable(self, cx, cy):
        return set(REGION) if (cx, cy) in REGION else None


class FakeNav:
    """假 Navigator：step 時照 MOVE 把人往目標推（0 ＝ 不動）；EXHAUST 時舉旗不動。"""
    MOVE = 0.0
    EXHAUST = False
    steps = 0
    goals: list = []
    arrives: list = []

    def __init__(self):
        self.stuck = False
        self.exhausted = False
        self.note = ""

    def reset(self, goal=None):
        self.goal = goal
        FakeNav.goals.append(goal)

    def step(self, scanner, mover, player_obj, gx, gy, arrive=navigate.ARRIVE):
        FakeNav.steps += 1
        FakeNav.arrives.append(arrive)
        if FakeNav.EXHAUST:
            self.exhausted = True
            return "最短路只能到這裡"
        if FakeNav.MOVE > 0:
            dx, dy = gx - POS[0], gy - POS[1]
            dd = math.hypot(dx, dy) or 1.0
            k = min(FakeNav.MOVE, dd)
            POS[0] += dx / dd * k
            POS[1] += dy / dd * k
        return "走"


supply.terrain = types.SimpleNamespace(load=lambda sc: (FakeGrid(), None))
supply.navigate = types.SimpleNamespace(Navigator=FakeNav, ARRIVE=navigate.ARRIVE)
supply.entity = types.SimpleNamespace(is_walking=lambda sc, obj: False)
supply._player_tile = lambda sc: (PF, (POS[0], POS[1]))
supply._npc_tile = lambda sc, nid: NPC_F if NPC_VISIBLE[0] else None


def run(timeout=30.0):
    t0 = CLOCK.t
    FakeNav.steps = 0
    FakeNav.goals.clear()
    FakeNav.arrives.clear()
    ok = supply._walk_to_npc(object(), object(), 1890, FALLBACK, timeout)
    return ok, CLOCK.t - t0


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def tile():
    return (math.floor(POS[0]), math.floor(POS[1]))


print("① 站在 2.9 格斜角（舊版就停在這裡）：要走到貼身格才回，而且不踩 NPC 本格")
POS[:] = [138.5, 163.46875]
NPC_VISIBLE[0] = True
FakeNav.MOVE, FakeNav.EXHAUST = 0.5, False
d0 = dist(POS, NPC_F)
ok, dt = run()
check("這個站位真的是「直線 ≤3」的舊到達圈", d0 <= navigate.ARRIVE, f"直線 {d0:.2f}")
check(f"目標＝離 NPC 最近的鄰格 {BESIDE}（不是本格 {NPC_T}）",
      FakeNav.goals == [BESIDE_C], f"實得 {FakeNav.goals}")
check("回 True", ok is True)
check("真的走了（沒有站在原地就說到了）", FakeNav.steps >= 1, f"steps={FakeNav.steps}")
check(f"停在貼身格中心 {supply.NPC_ARRIVE} 格內",
      dist(POS, BESIDE_C) <= supply.NPC_ARRIVE, f"實得 {dist(POS, BESIDE_C):.2f}")
check("沒踩上 NPC 本格", tile() != NPC_T, f"人在 {tile()}")
check(f"Navigator 的 arrive 傳 NPC_ARRIVE（{supply.NPC_ARRIVE}），不是 3 格",
      bool(FakeNav.arrives) and all(a == supply.NPC_ARRIVE for a in FakeNav.arrives),
      f"實得 {set(FakeNav.arrives)}")
check("不磨逾時：<2 秒就回", dt < 2.0, f"花了 {dt:.1f} 秒")

POS[:] = [BESIDE_C[0], BESIDE_C[1]]           # 已經站在貼身格
ok, dt = run()
check("已經站在貼身格 → 秒回 True、不下任何走路指令",
      ok is True and FakeNav.steps == 0 and dt < 0.5, f"ok={ok} steps={FakeNav.steps} dt={dt:.1f}")

print()
print("② 8 格外：一路 step 到貼身格才回，目標不變就不換 Navigator")
POS[:] = [145.0, 161.0]
FakeNav.MOVE, FakeNav.EXHAUST = 1.0, False
ok, dt = run()
check("回 True", ok is True)
check("有真的在走（step 被叫到）", FakeNav.steps >= 5, f"steps={FakeNav.steps}")
check("只用了一個目標（沒有每 2 秒換一個 Navigator）", len(FakeNav.goals) == 1,
      f"實得 {FakeNav.goals}")
check("停在貼身格中心 1 格內", dist(POS, BESIDE_C) <= supply.NPC_ARRIVE,
      f"實得 {dist(POS, BESIDE_C):.2f}")
check("進圈就回（不磨逾時）", dt < 5.0, f"花了 {dt:.1f} 秒")

print()
print("③ 貼身段被人牆擋住（怎麼走都不動）：APPROACH_STALL 秒沒更靠近就回 False，不磨 30 秒")
POS[:] = [141.0, 161.0]
FakeNav.MOVE, FakeNav.EXHAUST = 0.0, False
ok, dt = run()
check("回 False（沒站到）", ok is False)
check("有試著走", FakeNav.steps >= 1, f"steps={FakeNav.steps}")
check(f"花 {supply.APPROACH_STALL} 秒左右就回（不是 30 秒）",
      supply.APPROACH_STALL <= dt <= supply.APPROACH_STALL + 1.0, f"花了 {dt:.1f} 秒")

print()
print("④ 遠路段（NPC 沒串流、錨＝表座標）：被擋不提早放棄；NPC 中途串流進來就改錨真座標")
NPC_VISIBLE[0] = False
POS[:] = [150.0, 165.0]
FakeNav.MOVE = 0.0
ok, dt = run(timeout=8.0)
check("錨＝表座標旁最近的可走格 (130,168)", FakeNav.goals[:1] == [(130.5, 168.5)],
      f"實得 {FakeNav.goals[:2]}")
check("被擋也不提早放棄（磨到逾時交給 Navigator 重算）", ok is False and dt >= 7.5,
      f"ok={ok} 花了 {dt:.1f} 秒")

POS[:] = [150.0, 165.0]
FakeNav.MOVE = 1.0
t_start = CLOCK.t
supply._npc_tile = lambda sc, nid: NPC_F if CLOCK.t >= t_start + 1.0 else None
ok, dt = run()
check("先照表座標走，NPC 串流進來後改錨真座標（目標換成貼身格）",
      len(FakeNav.goals) == 2 and FakeNav.goals[0] == (130.5, 168.5)
      and FakeNav.goals[-1] == BESIDE_C, f"實得 {FakeNav.goals}")
check("同一趟走到貼身格回 True", ok is True and dist(POS, BESIDE_C) <= supply.NPC_ARRIVE,
      f"ok={ok} 離 {dist(POS, BESIDE_C):.2f}")
supply._npc_tile = lambda sc, nid: NPC_F if NPC_VISIBLE[0] else None
NPC_VISIBLE[0] = True

print()
print("⑤ Navigator 舉 exhausted（路線走完卻沒進圈）且人停了：馬上回 False，不空轉")
POS[:] = [141.0, 161.0]
FakeNav.MOVE, FakeNav.EXHAUST = 0.0, True
ok, dt = run()
check("回 False（交給呼叫端切官方）", ok is False)
check("馬上回（<1 秒）", dt < 1.0, f"花了 {dt:.1f} 秒")
FakeNav.EXHAUST = False

print()
print("⑥ _nearest_reachable：本格只在沒別格可站時才挑；外圈更近要挑外圈")
got = supply._nearest_reachable({NPC_T}, NPC_F, (140.0, 160.0), avoid=NPC_T)
check("只剩 NPC 本格可走 → 才回本格", got == NPC_T, f"實得 {got}")
got = supply._nearest_reachable({NPC_T, (136, 161), (137, 162)}, NPC_F,
                                (138.5, 163.5), avoid=NPC_T)
check("有旁邊格就不踩本格；同距離挑離自己近的 (137,162)", got == (137, 162), f"實得 {got}")
got = supply._nearest_reachable({(99, 99), (102, 101)}, (100.99, 100.99))
check("錨點靠格子邊：外一圈的邊格 (102,101) 比第一圈角格 (99,99) 近 → 挑外圈",
      got == (102, 101), f"實得 {got}")
check("reach 空 → None", supply._nearest_reachable(set(), NPC_F) is None)

print()
if FAILS:
    print(f"FAIL {len(FAILS)}：" + "、".join(FAILS))
    sys.exit(1)
print("OK")
