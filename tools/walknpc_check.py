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
    def reachable(self, cx, cy, avoid=None):
        return (set(REACH) | {(cx, cy)}) - set(avoid or ())


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
        self._avoid = set()

    def retarget(self, goal):
        keep = set(getattr(self, "_avoid", ()))
        self.reset(goal)
        self._avoid = keep

    def seed_avoid(self, cells):
        self._avoid |= set(cells)

    @property
    def avoid(self):
        return frozenset(getattr(self, "_avoid", ()))

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

print("⑥ ★★★ 2026-09-18：**現在站著人的格排到後面**（人多時別挑站不上去的那格）")
# `_near_spots` 的純函式測試：同樣的地形、同樣的可到區，只差 busy 集合。
_G = types.SimpleNamespace(walkable=lambda x, y: True)
_NPC = (100, 100)
_HERE = (100.5, 96.5)


def _spots(busy):
    """繞過 _reach_around（要真地形）：直接餵一個「什麼都走得到」的可到區。"""
    reach = {(_NPC[0] + dx, _NPC[1] + dy)
             for dx in range(-3, 4) for dy in range(-3, 4)}
    o_reach = supply._reach_around
    supply._reach_around = lambda g, here, avoid=None: reach
    try:
        return supply._near_spots(None, _G, _HERE, 0, _NPC, None, busy)
    finally:
        supply._reach_around = o_reach


base = _spots(None)
check("沒給 busy → 順序跟以前一模一樣（最近的在前）",
      base[0] in {(99, 100), (101, 100), (100, 99), (100, 101)}, base[:4])

busy = {(99, 100), (101, 100), (100, 99), (100, 101)}      # 四個正交鄰格全站了人
got = _spots(busy)
check("四個最近格都有人 → 第一名換成沒人的那一圈",
      got[0] not in busy, got[:4])
check("⛔ 有人的格**沒有被剔除**（只是排後面；2026-09-06 刪掉的『有人就放棄』別回來）",
      all(c in got for c in busy), [c for c in busy if c not in got])
check("有人的格全部排在沒人的後面",
      max(got.index(c) for c in busy) > min(
          got.index(c) for c in got if c not in busy))

allbusy = {c for c in base}
same = _spots(allbusy)
check("全部都有人 → 順序退回跟沒給 busy 一樣（不會亂跳）", same == base)

print("⑦ _busy_tiles：讀不到一律回空集合（安全退化，不擋走路）")


class _Boom:
    """假 scanner：碰它就炸（_busy_tiles 只從 entity.snapshot 拿資料，不該碰它）。"""

    _pid = 1

    def _read_bytes(self, *a, **k):
        raise OSError("讀不到")


class _E:
    def __init__(self, addr, x, y):
        self.addr, self.x, self.y = addr, x, y


NPC2 = (100, 100)
o_entity, o_bag = supply.entity, supply.bag


def _with(snap, me):
    """換掉 supply 用到的 entity.snapshot 與 bag.player_entity，跑一次再換回來。"""
    supply.entity = types.SimpleNamespace(snapshot=snap)
    supply.bag = types.SimpleNamespace(player_entity=lambda s: me)
    try:
        return supply._busy_tiles(_Boom(), NPC2)
    finally:
        supply.entity, supply.bag = o_entity, o_bag


def _boom_snap(*a, **k):
    raise OSError("掃不動")


check("snapshot 爆了 → 回空集合，不丟例外", _with(_boom_snap, 0) == set())
check("一個實體都掃不到 → 回空集合（熱區過期，下次全掃）",
      _with(lambda *a, **k: (None, None, [], None, {}), 0) == set())

two = lambda *a, **k: (None, None,                      # noqa: E731
                       [_E(0x1000, 100.5, 101.5), _E(0x2000, 100.5, 130.5)],
                       None, {})
got = _with(two, 0x2000)
check("只算半徑內的（30 格外的那個不算）", got == {(100, 101)}, got)
check("⛔ 自己站的那格不算「有人」", _with(two, 0x1000) == set())

print()
if FAILS:
    print(f"FAIL {len(FAILS)}：" + "、".join(FAILS))
    sys.exit(1)
print("OK")
