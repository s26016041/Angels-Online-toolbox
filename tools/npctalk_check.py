"""跟 NPC 說話的節奏 —— 離線測試（買／修／銀行／活動地圖入口**共用同一支**）。

    py tools\\npctalk_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的是 `supply._engage_npc` / `_approach_npc` 的三條規則，全部來自實機回報：

① **每一輪先問「是不是已經成功了」**（2026-08-27）
   活動地圖入口 NPC 講完話**人就被傳走了**。`_engage_npc` 的重試迴圈原本中間
   不重問，下一輪 `find_npc` 在新地圖當然找不到 → 跑去用地形圖走向「天使學園
   的座標」，角色在新地圖亂走。

② **貼著點就別等 12 秒**（2026-08-27 使用者：「點不到的時候會等很久才橋位置」）
   人已經在 CLICK_RANGE 內時，對話框是一趟伺服器來回的事。12 秒是留給
   「從遠處點、客戶端自己走過去」的。⚠ 人擠人時角色被推著滑動、`is_walking`
   一直是 True，「停住 0.8 秒就放棄」的快路徑永遠不觸發 → 卡滿 12 秒才換站位。

③ **走不動就別磨滿逾時**（同上）
   NPC 旁邊圍滿人時距離根本縮不了，`_approach_npc` 會空轉 20 秒才回去點。
   改成連續 `APPROACH_STALL` 秒沒更靠近就返回，交給互動包＋`_nudge_toward`
   （往 NPC 身上靠／穿過去）處理人牆 —— 那條路本來就是為這個設計的。

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
print("② 貼著點 3 秒沒開就換站位；從遠處點才留 12 秒給客戶端自己走")
supply.find_npc = lambda sc, nid: (0x1000, 0x2000)
supply._click_npc = lambda *a, **k: True
supply._dialog_token = lambda sc: 7
supply._nudge_toward = lambda *a, **k: True
# ⚠ 先把真的那支收起來 —— 第③段要用它本人，不能測到這裡的替身
REAL_APPROACH = supply._approach_npc
supply._approach_npc = lambda *a, **k: None
supply._wait_arrival = lambda *a, **k: True
seen: list[float] = []


AGAIN_SEEN: list = []


def fake_wait_dialog(sc, base, timeout=supply.DIALOG_TIMEOUT, again=None):
    seen.append(timeout)
    AGAIN_SEEN.append(again)
    return False                                 # 一律「沒開」，逼它換站位


supply._wait_dialog = fake_wait_dialog

GAP = [1.0]                                      # 貼身
supply._npc_gap = lambda sc, nid: GAP[0]
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=1,
                   confirm=lambda: False, confirm_timeout=0.1)
check(f"貼身點 → 等 {supply.DIALOG_NEAR_TIMEOUT:.0f} 秒",
      seen == [supply.DIALOG_NEAR_TIMEOUT], f"實得 {seen}")

seen.clear()
GAP[0] = 20.0                                    # 遠處（客戶端要自己走過去）
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
print("③ 人牆卡住時 _approach_npc 不磨滿逾時")
supply._ent_tile_f = lambda sc, e: (50.0, 50.0)
supply._player_tile = lambda sc: (0x3000, (40.0, 40.0))
supply._wait_move_done = lambda sc, **k: CLOCK.sleep(0.9)
GAP[0] = 10.0                                    # 怎麼走都不會更近（被人擋著）
t0 = CLOCK.t
REAL_APPROACH(MOVER, SC, 1, timeout=20.0)
spent = CLOCK.t - t0
check(f"沒進展就回去點（花 {spent:.1f} 秒，不是 20 秒）",
      spent <= supply.APPROACH_STALL + 2.0, f"實得 {spent:.1f} 秒")

CLOCK.t = t0
GAP_SEQ = [10.0, 8.0, 6.0, 4.0, 2.0]             # 走得動 → 不准提早放棄
supply._npc_gap = lambda sc, nid: (GAP_SEQ.pop(0) if GAP_SEQ
                                   else supply.CLICK_RANGE)
REAL_APPROACH(MOVER, SC, 1, timeout=20.0)
check("走得動就照走到位（不是一卡住就放棄）", not GAP_SEQ,
      f"還剩 {GAP_SEQ}")

print()
print("④ 等對話框的期間會一直補點（TryAct：這也是「還沒走到就繼續走」的動力）")
supply._dialog_token = lambda sc: 7              # 一直是基準值 = 對話框沒開
supply.move.pathfinder_this = lambda sc: 0       # 讀不到玩家物件 → 走「沒在走路」那條
hits: list = []
t0 = CLOCK.t
opened = REAL_WAIT(SC, 7, supply.DIALOG_NEAR_TIMEOUT,
                   again=lambda: hits.append(1))
check("沒開就回 False", opened is False)
check(f"3 秒內補點 {supply.DIALOG_NEAR_TIMEOUT / supply.CLICK_REPEAT:.0f} 次上下",
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

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
