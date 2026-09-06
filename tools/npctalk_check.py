"""跟 NPC 說話的節奏 —— 離線測試（買／修／銀行／活動地圖入口**共用同一支**）。

    py tools\\npctalk_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的是 `supply._engage_npc` / `_wait_dialog` / `_walk_to_npc` 的規則，全部來自實機回報：

① **每一輪先問「是不是已經成功了」**（2026-08-27）
   活動地圖入口 NPC 講完話**人就被傳走了**。`_engage_npc` 的重試迴圈原本中間
   不重問，下一輪 `find_npc` 在新地圖當然找不到 → 跑去用地形圖走向「天使學園
   的座標」，角色在新地圖亂走。

② **貼著點就別等 12 秒**（2026-08-27 使用者：「點不到的時候會等很久才橋位置」）
   人已經在 CLICK_RANGE 內時，對話框是一趟伺服器來回的事。12 秒是留給
   「從遠處點、官方自己走過去」的。⚠ 人擠人時角色被推著滑動、`is_walking`
   一直是 True，「停住 0.8 秒就放棄」的快路徑永遠不觸發 → 卡滿 12 秒才換站位。

③ **移動全部我們自己處理、講話交給官方**（2026-09-06 使用者定，memory
   npc-approach-move-ours-talk-official）：點之前先用我們的走路 `_walk_to_npc` 站到離
   NPC 最近的可走格，⛔ 不再叫遊戲尋路走近（舊 `_approach_npc` 已刪）；切官方之後
   官方那支沒到位會自己走一步，人牆裡走不動＝官方流程卡住的地方 → 等對話框時
   「連續 APPROACH_STALL 秒沒更靠近」就 give_up，回去換站位再切官方，不磨滿 12 秒。
   （貼身段我們自己走被擋的 stall 規則在 tools/walknpc_check.py）

⚠ 純離線：假的 mover／scanner／時鐘，不碰遊戲。**只換 I/O，判斷邏輯跑真的**。
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.game import supply                              # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class Clock:
    """假時鐘：sleep 直接把時間往前推，測試才是決定性的、而且秒殺。"""

    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeMover:
    def __init__(self):
        self.active = True
        self.lock = threading.Lock()
        self.walks = 0

    def call_sync(self, *a, **k):
        return 1

    def walk_route(self, *a, **k):
        self.walks += 1
        return 1

    def walk_near(self, *a, **k):
        self.walks += 1
        return 1

    def path_to(self, *a, **k):
        return 1


class FakeSC:
    def _read_bytes(self, addr, n):
        return None


CLOCK = Clock()
supply.time = CLOCK                 # ⚠ 假時鐘要 patch 進 supply 的命名空間
MOVER, SC = FakeMover(), FakeSC()

REAL_WAIT = supply._wait_dialog     # ④ 要用它本人（②會換成替身）
REAL_WALK = supply._walk_to_npc     # ⑤ 要用它本人（①會換成替身）

print("① 人被傳走之後不會在新地圖亂走（每一輪先問 confirm）")
walked = []
supply.find_npc = lambda sc, nid: None          # 新地圖上當然找不到那隻 NPC
supply._walk_to_npc = lambda *a, **k: walked.append(1)
asked = []


def confirm_after_first():
    asked.append(1)
    return len(asked) > 1                       # 進場那次還沒成功，之後成功


ok = supply._engage_npc(MOVER, SC, 14897, (170, 90), [13, 10, 10], "",
                        tries=4, confirm=confirm_after_first,
                        confirm_timeout=1.0)
check("成功之後立刻收工", ok is True)
check("⛔ 沒有在新地圖用地形圖亂走", walked == [], f"走了 {len(walked)} 次")

print()
print("② 貼著點 3 秒沒開就換站位；從遠處點才留 12 秒給官方自己走")
supply.find_npc = lambda sc, nid: (0x1000, 0x2000)
supply._click_npc = lambda *a, **k: True
supply._dialog_token = lambda sc: 7
supply._nudge_toward = lambda *a, **k: True
supply._wait_arrival = lambda *a, **k: True
seen: list[float] = []


AGAIN_SEEN: list = []
GIVEUP_SEEN: list = []


def fake_wait_dialog(sc, base, timeout=supply.DIALOG_TIMEOUT, again=None,
                     give_up=None):
    seen.append(timeout)
    AGAIN_SEEN.append(again)
    GIVEUP_SEEN.append(give_up)
    return False                                 # 一律「沒開」，逼它換站位


supply._wait_dialog = fake_wait_dialog

GAP = [1.0]                                      # 貼身
supply._npc_gap = lambda sc, nid: GAP[0]
walked.clear()
MOVER.walks = 0
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=1,
                   confirm=lambda: False, confirm_timeout=0.1)
check(f"貼身點 → 等 {supply.DIALOG_NEAR_TIMEOUT:.0f} 秒",
      seen == [supply.DIALOG_NEAR_TIMEOUT], f"實得 {seen}")

seen.clear()
GAP[0] = 20.0                                    # 遠處（官方自己走過去）
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=1,
                   confirm=lambda: False, confirm_timeout=0.1)
check(f"遠處點 → 留 {supply.DIALOG_TIMEOUT:.0f} 秒",
      seen == [supply.DIALOG_TIMEOUT], f"實得 {seen}")
check("等對話框時有給「補點」的回呼（官方那個重試迴圈）",
      bool(AGAIN_SEEN) and all(callable(x) for x in AGAIN_SEEN),
      f"實得 {AGAIN_SEEN}")

clicked: list = []
supply._click_npc = lambda m, s_, ent: clicked.append(ent) or True
AGAIN_SEEN[-1]()
check("補點前**重新找那隻 NPC**（不吃上一拍的實體位址）",
      clicked == [0x1000], f"實得 {clicked}")
supply._click_npc = lambda *a, **k: True

print()
print("③ 移動全部我們自己處理：點之前先 _walk_to_npc，⛔ 不叫遊戲尋路；官方沒更靠近就 give_up")
check("點之前每輪都先用我們的走路（_walk_to_npc 被叫到，貼身/遠處各一次）",
      len(walked) == 2, f"實得 {len(walked)} 次")
check("⛔ 點之前沒有叫遊戲尋路走近（walk_route/walk_near 零次）",
      MOVER.walks == 0, f"實得 {MOVER.walks} 次")
check("等對話框時有給 give_up 回呼",
      bool(GIVEUP_SEEN) and all(callable(x) for x in GIVEUP_SEEN),
      f"實得 {GIVEUP_SEEN}")

blocked = GIVEUP_SEEN[-1]                        # 遠處那輪的 give_up（跑真的 _blocked）
GAP[0] = 20.0
t0 = CLOCK.t
first = blocked()
CLOCK.sleep(supply.APPROACH_STALL - 0.5)
mid = blocked()
CLOCK.sleep(1.0)
late = blocked()
check(f"距離不變 → 撐過 {supply.APPROACH_STALL} 秒才 give_up（之前不放棄）",
      first is False and mid is False and late is True,
      f"實得 {first}/{mid}/{late}")

# 重新 engage 一次拿一份**新的** give_up（上面那份的進度已經 give_up 過了）
supply._npc_gap = lambda sc, nid: 20.0
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=1,
                   confirm=lambda: False, confirm_timeout=0.1)
blocked = GIVEUP_SEEN[-1]
GAP_SEQ = [20.0, 17.0, 14.0, 11.0, 8.0]          # 官方真的在往前走
supply._npc_gap = lambda sc, nid: GAP_SEQ.pop(0) if GAP_SEQ else 8.0
results = []
for _ in range(5):
    results.append(blocked())
    CLOCK.sleep(supply.APPROACH_STALL - 0.5)
check("每拍都更靠近 → 一直不 give_up（官方走得動就讓它走）",
      all(r is False for r in results), f"實得 {results}")

print()
print("④ 等對話框的期間會一直補點（官方的講話重試）；give_up 一回 True 就馬上放棄")
supply._dialog_token = lambda sc: 7              # 一直是基準值 = 對話框沒開
supply.move.pathfinder_this = lambda sc: 0       # 讀不到玩家物件 → 走「沒在走路」那條
hits: list = []
t0 = CLOCK.t
opened = REAL_WAIT(SC, 7, supply.DIALOG_NEAR_TIMEOUT,
                   again=lambda: hits.append(1))
check("沒開就回 False", opened is False)
check(f"{supply.DIALOG_NEAR_TIMEOUT} 秒內補點 {supply.DIALOG_NEAR_TIMEOUT / supply.CLICK_REPEAT:.0f} 次上下",
      2 <= len(hits) <= 12, f"實得 {len(hits)} 次")
check("⛔ 有補點就不准用「停住 0.8 秒就放棄」早退",
      CLOCK.t - t0 >= supply.DIALOG_NEAR_TIMEOUT,
      f"只花了 {CLOCK.t - t0:.1f} 秒")

CLOCK.t = t0
hits.clear()
opened = REAL_WAIT(SC, 7, supply.DIALOG_TIMEOUT)  # 沒給 again ＝ 舊行為
check("沒給補點回呼時，站著不動 0.8 秒就早退（舊行為不變）",
      opened is False and CLOCK.t - t0 < supply.DIALOG_TIMEOUT,
      f"花了 {CLOCK.t - t0:.1f} 秒")

CLOCK.t = t0
hits.clear()
opened = REAL_WAIT(SC, 7, supply.DIALOG_TIMEOUT, again=lambda: hits.append(1),
                   give_up=lambda: True)
check("give_up 回 True → 馬上回 False（不磨 12 秒）",
      opened is False and CLOCK.t - t0 < 1.0, f"花了 {CLOCK.t - t0:.1f} 秒")

print()
print("⑤ 走去 NPC：人站在地形圖標成不可走的格上（復活剛落地）")
# ★★ 2026-09-06 黑狐實錄：死在副本 → 復活回永夜城 → 補給，人站的格地形圖說不可走 →
#   reachable 回 None → 舊寫法退回 .MPC 表座標 (129,168)，那格是櫃檯後 2 格孤島 →
#   尋路永遠算不出 → 4 輪 × 30 秒原地不動 → 「倉庫開不起來（停在離銀行約 ? 格）」。


class FakeGrid:
    def __init__(self, region, island):
        self.region, self.island = set(region), set(island)

    def reachable(self, x, y):
        if (x, y) in self.region:
            return set(self.region)
        if (x, y) in self.island:
            return set(self.island)
        return None


class FakeNav:
    goals: list = []

    def __init__(self):
        self.stuck = False
        self.exhausted = False          # 真的 Navigator 有這個旗（_walk_to_npc 會看）

    def reset(self, goal):
        FakeNav.goals.append(goal)

    def step(self, *a, **k):
        pass


grid = FakeGrid({(213, 54), (213, 53), (212, 52), (129, 166)}, {(129, 168), (128, 168)})
supply.terrain.load = lambda sc: (grid, None)
supply.navigate.Navigator = FakeNav
supply._npc_tile = lambda sc, nid: None                  # 銀行 NPC 還沒串流進來
supply._player_tile = lambda sc: (0x3000, (213.5, 53.5))  # round → (214,54)：不可走
REAL_WALK(MOVER, SC, 1890, (129, 168), timeout=0.5)
check("站的格不可走 → 問旁邊一圈取最大區，目標＝離銀行最近的可走格 (129,166) 的中心",
      bool(FakeNav.goals) and FakeNav.goals[0] == (129.5, 166.5), f"實得 {FakeNav.goals[:2]}")
check("全程沒把孤島的表座標 (129,168) 當目標",
      all(g != (129.5, 168.5) for g in FakeNav.goals), str(FakeNav.goals[:3]))
FakeNav.goals.clear()
supply._player_tile = lambda sc: (0x3000, (300.0, 300.0))  # 旁邊一圈也全不可走
REAL_WALK(MOVER, SC, 1890, (129, 168), timeout=0.5)
check("整圈都問不到（地形圖跟人對不上）→ 才硬走表座標（沒有上一個目標可沿用）",
      bool(FakeNav.goals) and FakeNav.goals[0] == (129.5, 168.5), f"實得 {FakeNav.goals[:2]}")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
