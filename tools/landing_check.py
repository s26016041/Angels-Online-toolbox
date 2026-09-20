"""回城落地後「先開始走、背包邊走邊等」的離線測試 —— `supply._wait_ready()`。

    py tools\\landing_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

★★★★ 2026-09-20 使用者實機回報：「回程後原地發呆超級久，應該要確認回程後直接
  開始走」。真因：天使之翼落地後 `_wait_ready` **乾等**「整袋背包讀得完」
  （最多 `LAND_READY_WAIT` 15 秒）才准往下，期間一發走路指令都沒送。
  可是走去商人根本不需要背包 —— 那個閘擋的是**判斷**
  （[[bag-false-empty-guards]]：讀不到被當成沒有 → 整趟補給放棄），不是腳。

驗的規格：
    ① 人的座標一讀得到就往第一站 NPC 的 .MPC 表座標送走路，⛔ 不等背包；
       走的是**我們自己算的路徑**（地形圖 A*，使用者 2026-09-20 晚定）
    ② 沒在走才補送（每 `HEAD_START_GAP` 秒一發）—— ⛔ 官方正帶著走不插手
    ③ 地形圖說走不到 → 才問官方 `walk_route`；官方也回 0 → `walk_near` 直走
    ④ 背包整袋讀得完 → 回 True（原本的保證**一點都不能少**）
    ⑤ 沒帶 head_to → 完全是舊行為（只等，一步都不走）
    ⑥ 人的座標還讀不到 → ⛔ 不送走路（不對著 NULL 下指令）
    ⑦ 一直讀不到 → 逾時回 False（呼叫端照舊往下，各步驟自己會擋）
⚠ 純離線：假時鐘／假跳板／假背包，**只換 I/O，判斷邏輯跑真的**。
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.game import supply                            # noqa: E402

FAILS: list[str] = []


def ck(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class Clock:
    """假時鐘：sleep 直接把時間往前推（測試秒殺且是決定性的）。"""

    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeMover:
    def __init__(self, route_ok=True):
        self.active = True
        self.route_ok = route_ok
        self.walks: list[tuple] = []

    def walk_route(self, sc, pf, x, y, stop_short=0.0):
        self.walks.append(("route", x, y))
        return 1 if self.route_ok else 0

    def walk_near(self, sc, pf, x, y, keep):
        self.walks.append(("near", x, y))
        return 1


CLOCK = Clock()
supply.time = CLOCK                    # ⚠ 假時鐘要 patch 進 supply 的命名空間
supply._nap = lambda s: CLOCK.sleep(s)
SC = object()
BAG_OK = [False]                       # 背包整袋讀得完了嗎
WALKING = [False]                      # 角色正在走路嗎
HERE = [(0x3000, (5.0, 5.0))]          # _player_tile 回什麼
SAID: list[str] = []

supply.bag = types.SimpleNamespace(scan=lambda sc: ([], BAG_OK[0]))
supply._player_tile = lambda sc: HERE[0]
supply._is_walking = lambda sc: WALKING[0]
supply._reach_goal = lambda sc, here, tx, ty: None   # 地形圖讀不到 → 照原座標走

SHOP = (12345, 170, 90)                # NPC_TABLE 的值：(編號, x, y)

STEPS: list = []


class FakeNav:
    stuck = False

    def step(self, sc, mover, obj, gx, gy, arrive=None):
        STEPS.append((gx, gy))
        return "走"


REAL_NAV = supply.navigate
supply.navigate = types.SimpleNamespace(Navigator=FakeNav)

print("① 背包還沒同步 → 人先走去第一站（⛔ 不是站在原地發呆）")
BAG_OK[0], WALKING[0], HERE[0] = False, False, (0x3000, (5.0, 5.0))
MV = FakeMover()
SAID.clear()
STEPS.clear()
ok = supply._wait_ready(SC, mover=MV, head_to=SHOP[1:], say=SAID.append)
ck("★ 有送走路（⛔ 舊版整整 15 秒一發都沒送）", STEPS != [], str(STEPS[:2]))
ck("★★ 走的是**我們自己算的路徑**（地形圖 A*），目標＝第一站 NPC 的表座標",
   all(s == (170.0, 90.0) for s in STEPS), str(STEPS[:3]))
ck("⛔ A* 走得動時不去問官方尋路、也不亂直走", MV.walks == [],
   str(MV.walks[:3]))
ck("★ 沒在走就補送，間隔 HEAD_START_GAP（不是每一拍狂送）",
   1 <= len(STEPS) <= supply.LAND_READY_WAIT / supply.HEAD_START_GAP + 1,
   f"{len(STEPS)} 發")
ck("★ 等的期間有回報進度（狀態列別看起來像當掉）", SAID != [],
   str(SAID[:1]))
ck("⑦ 背包一直讀不到 → 逾時回 False（呼叫端照舊往下）", ok is False)

print()
print("② 官方／我們正帶著人走 → ⛔ 不插手重送")
BAG_OK[0], WALKING[0] = False, True
MV2 = FakeMover()
STEPS.clear()
supply._wait_ready(SC, mover=MV2, head_to=SHOP[1:])
ck("★ 一發都沒送", STEPS == [] and MV2.walks == [], f"{STEPS} {MV2.walks}")

print()
print("③ 地形圖說走不到 → 才問官方尋路；官方也算不出來 → 直走當最後退路")
BAG_OK[0], WALKING[0] = False, False
FakeNav.stuck = True
MV3 = FakeMover(route_ok=True)
supply._wait_ready(SC, mover=MV3, head_to=SHOP[1:])
ck("★ A* 走不到 → 退官方 walk_route",
   any(w[0] == "route" for w in MV3.walks), str(MV3.walks[:3]))
ck("　官方算得出來就不直走",
   not any(w[0] == "near" for w in MV3.walks), str(MV3.walks[:3]))
MV3b = FakeMover(route_ok=False)
supply._wait_ready(SC, mover=MV3b, head_to=SHOP[1:])
ck("★ 官方也回 0 → walk_near 直走當最後退路",
   any(w[0] == "near" for w in MV3b.walks), str(MV3b.walks[:3]))
FakeNav.stuck = False

print()
print("④ 背包讀得完 → 回 True（原本的保證不變）")
BAG_OK[0] = True
MV4 = FakeMover()
t0 = CLOCK.time()
ok4 = supply._wait_ready(SC, mover=MV4, head_to=SHOP[1:])
ck("★ 回 True", ok4 is True)
ck("★ 立刻回（不再空等）", CLOCK.time() - t0 < 1.0,
   f"{CLOCK.time() - t0:.1f}s")

print()
print("⑤ 沒帶 head_to → 舊行為：只等，一步都不走")
BAG_OK[0] = False
MV5 = FakeMover()
STEPS.clear()
ok5 = supply._wait_ready(SC, mover=MV5)
ck("★ 一步都不走", STEPS == [] and MV5.walks == [], f"{STEPS} {MV5.walks}")
ck("　一樣逾時回 False", ok5 is False)

print()
print("⑥ 人的座標還讀不到 → ⛔ 不對著 NULL 下走路指令")
HERE[0] = (None, None)
MV6 = FakeMover()
STEPS.clear()
supply._wait_ready(SC, mover=MV6, head_to=SHOP[1:])
ck("★ 一步都不走", STEPS == [] and MV6.walks == [], f"{STEPS} {MV6.walks}")
HERE[0] = (0x3000, (5.0, 5.0))

print()
print("⑦ 跳板沒裝好（mover 不能用）→ 不走，也不能炸")
MV7 = FakeMover()
MV7.active = False
STEPS.clear()
ok7 = supply._wait_ready(SC, mover=MV7, head_to=SHOP[1:])
ck("★ 一步都不走", STEPS == [] and MV7.walks == [], f"{STEPS} {MV7.walks}")
ck("　照樣回得了 False", ok7 is False)

print()
print("⑧ 呼叫端：落地那一處真的有把第一站帶進去")
import inspect                                          # noqa: E402

SRC = inspect.getsource(supply._full_supply)
ck("★ 落地後呼叫 _wait_ready 有帶 mover＋head_to",
   "head_to=" in SRC and "mover=mover" in SRC, "")
ck("★ 第一站挑「一定會去的那個」：練技趟＝補給商，其餘＝維修商優先",
   'potion_only else' in SRC and '_ent.get("repair")' in SRC, "")
ck("⛔ 銀行不當起跑方向（有東西要存才去）",
   SRC.index('_ent.get("repair")') < SRC.index('_ent.get("bank")'), "")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
