"""「走到 NPC → 講話」的離線測試（不碰遊戲、不碰 Qt）。

    py tools\\npctalk_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的是 `supply._engage_npc` / `_approach_npc` 的規格 —— 2026-09-20 使用者重訂
（他第三次回報「補給的跟藥水商人又在來回踱步」）：

    「就直接走到藥水商人最近，不用管能不能走到，反正角色不動了就發送官方的，
      然後讓官方自己走然後對話；如果 2 秒沒偵測到對話就再發送一次官方的，
      直到 20 秒都不行再停下來。」

規格：
    ① 已經成功（confirm 進場就 True）→ 立刻收工，一步都不走
    ② **走一次**就好：只叫一次 `_approach_npc`；⛔ 舊的那一套（地形圖挑可走格
       `_walk_to_npc`、踩上 NPC／穿到另一側換站位 `_nudge_toward`）整組刪掉，
       這裡連「模組裡還有沒有那些名字」都驗 —— 不准偷偷寫回來
    ③ 沒看到對話 → 每 `TALK_CLICK_GAP`(2 秒) 補送一次官方 TryAct，
       而且每一發都**重新找那隻 NPC**（實體會被回收，位址不能留過夜）
    ④ 看得到 NPC 之後滿 `TALK_GIVE_UP`(20 秒) 還講不到話 → 回 False，
       `talk_failure()` 說得出原因；`run_full_supply` 的訊息帶 `TALK_FAIL`
       （掛機頁據此**通知＋停機**）
    ⑤ 對話開了 → 送選項 → 目標視窗開了 → True
    ⑥ 「讀不到≠沒有」：`_wait_dialog` 說沒看到，但顯示旗標沒有明說 False、
       代號又非 0、人也到位 → 照樣把選項送出去
    ⑦ 旗標**明說** False（畫面上確定沒有對話框）→ 不硬送選項
    ⑧ NPC 還沒串流進來（跨城走一半）**不算**講不到話：繼續走，
       走滿 `WALK_TIMEOUT` 才算失敗
    ⑨ `_approach_npc`：走不走得到都照走（官方尋路 `walk_route`），
       連續 `APPROACH_STALL` 秒沒更靠近就回去讓 TryAct 收尾；
       NPC 還看不到就先往 .MPC 表座標走
⚠ 純離線：假時鐘／假跳板／假實體，**只換 I/O，判斷邏輯跑真的**。
"""
from __future__ import annotations

import math
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
        self.walks: list[tuple] = []

    def call_sync(self, *a, **k):
        return 1

    def walk_route(self, sc, pf, x, y, stop_short=0.0):
        self.walks.append(("route", x, y))
        return 1

    def walk_near(self, sc, pf, x, y, keep):
        self.walks.append(("near", x, y))
        return 1

    def path_to(self, *a, **k):
        return 1


class FakeSC:
    def _read_bytes(self, addr, n):
        return None


CLOCK = Clock()
supply.time = CLOCK                 # ⚠ 假時鐘要 patch 進 supply 的命名空間
MOVER, SC = FakeMover(), FakeSC()

REAL_ENGAGE = supply._engage_npc
REAL_APPROACH = supply._approach_npc
REAL_WAIT_DIALOG = supply._wait_dialog

# 共用替身（各段自己覆蓋需要的）
supply._wait_move_done = lambda *a, **k: None
supply._wait_still = lambda *a, **k: None
supply._talkaction = lambda *a, **k: True
supply._wait_page = lambda *a, **k: True
supply._wait_arrival = lambda *a, **k: True
supply._npc_in_box = lambda sc, nid: True          # 預設：已經站在講話方框內
supply.move = type("M", (), {"pathfinder_this": staticmethod(lambda sc: 0)})()
supply.talkwnd = type("T", (), {
    "page": staticmethod(lambda sc: None),
    "window_visible": staticmethod(lambda sc: None),
})()


def reset(npc_visible=True):
    """把每一段共用的替身回到預設值。"""
    supply._approach_npc = lambda *a, **k: APPROACHES.append(1)
    supply.find_npc = (lambda sc, nid: (0x1000, 0x2000)) if npc_visible \
        else (lambda sc, nid: None)
    supply._click_npc = lambda *a, **k: CLICKS.append(1) or True
    supply._dialog_token = lambda sc: 7
    supply._wait_arrival = lambda *a, **k: True
    supply.talkwnd.window_visible = staticmethod(lambda sc: None)
    supply._talk_clear()
    APPROACHES.clear()
    CLICKS.clear()


APPROACHES: list = []
CLICKS: list = []


def no_dialog(sc, base, timeout=supply.DIALOG_TIMEOUT, *a, **k):
    """對話一直沒開的替身。⚠ 要把假時鐘推到 timeout —— 真的那支是輪詢，
    時間本來就會走；不推的話 `_engage_npc` 的 20 秒預算永遠用不完（空轉）。"""
    CLOCK.sleep(timeout)
    return False

print("① 已經成功了就立刻收工（一步都不走）")
reset()
supply._wait_dialog = lambda *a, **k: True
ok = supply._engage_npc(MOVER, SC, 14897, (170, 90), [10], "",
                        confirm=lambda: True, confirm_timeout=1.0)
check("回 True", ok is True)
check("⛔ 沒有走任何一步", APPROACHES == [], f"走了 {len(APPROACHES)} 次")
check("⛔ 也沒有點下去", CLICKS == [], f"點了 {len(CLICKS)} 次")

print()
print("② 走一次就好；⛔ 舊的「挑可走格／換站位」整組不准回來")
reset()
supply._wait_dialog = lambda *a, **k: True
supply._wnd_open = lambda m, s, n: bool(CLICKS)      # 點過就算開了
supply._engage_npc(MOVER, SC, 1, (170, 90), [10], "WND_NPCSALE")
check("★ 只走一次（進場那一次）", len(APPROACHES) == 1, str(APPROACHES))
for gone in ("_walk_to_npc", "_nudge_toward", "_near_spots", "_box_status",
             "_nearest_reachable", "_reach_around", "NUDGE_STEPS",
             "NEAR_SPOT_R", "NEAR_GIVE_UP"):
    check(f"⛔ supply.{gone} 已經不存在", not hasattr(supply, gone))

print()
print("③ 沒看到對話 → 每 2 秒補送一次官方 TryAct（每發都重找 NPC）")
reset()
GAPS: list = []
AGAIN: list = []


def spy_wait_dialog(sc, base, timeout=supply.DIALOG_TIMEOUT, again=None,
                    give_up_still=None, page_base=None,
                    gap=supply.CLICK_REPEAT):
    GAPS.append((timeout, gap))
    if again is not None:
        AGAIN.append(again())          # 補點一次：驗它真的會重找＋重點
    CLOCK.sleep(timeout)               # ⚠ 真的那支是輪詢，時間會走完 timeout
    return False                       # 一直沒開


FOUND: list = []
supply.find_npc = lambda sc, nid: FOUND.append(nid) or (0x1000, 0x2000)
supply._wait_dialog = spy_wait_dialog
supply._wnd_open = lambda m, s, n: False
ok = supply._engage_npc(MOVER, SC, 1, (170, 90), [10], "WND_NPCSALE")
# ★★★★ 2026-09-20 實機「等超久才走去」→ 補點間隔改成**看狀況**：TryAct 那一發
#   同時是「官方把人走過去」的油門，所以人還在走／還沒進講話方框時要用
#   CLICK_REPEAT(0.35s)，站定且在框內才用 TALK_CLICK_GAP(2s)。
_gaps = [g() if callable(g) else g for _t, g in GAPS]
check("★ 站定且在講話方框內 → 2 秒補一次（使用者要的節奏）",
      all(g == supply.TALK_CLICK_GAP for g in _gaps), str(_gaps))
supply._npc_in_box = lambda sc, nid: False        # 還沒進框（在走過去）
_gaps2 = [g() if callable(g) else g for _t, g in GAPS]
check("★★ 還沒到位 → 回到 0.35 秒踩油門（⛔ 不准一路 2 秒，那會「等超久才走去」）",
      all(g == supply.CLICK_REPEAT for g in _gaps2), str(_gaps2))
supply._npc_in_box = lambda sc, nid: True
check("★ 等待上限就是剩下的預算（≤ TALK_GIVE_UP）",
      GAPS and GAPS[0][0] <= supply.TALK_GIVE_UP, str(GAPS))
check("★ 補點會重新找 NPC 再點", AGAIN and all(AGAIN), str(AGAIN))
check("　一輪至少找過兩次 NPC（挑目標一次＋補點一次）", len(FOUND) >= 2,
      str(len(FOUND)))

print()
print("④ 20 秒都講不到話 → 回 False、說得出原因、訊息帶 TALK_FAIL")
check("★ 回 False", ok is False)
check("★ talk_failure() 有字", supply.talk_failure() != "",
      repr(supply.talk_failure()))
check("　原因講得出「20 秒都沒開對話」", "沒開對話" in supply.talk_failure(),
      supply.talk_failure())
took = None


def fake_full(*_a, **_k):
    supply._talk_failed("20 秒都沒開對話")
    return False, "開交易失敗（靠不夠近或對話碼不對）"


REAL_FULL = supply._full_supply
supply._full_supply = fake_full
ok2, msg2 = supply.run_full_supply(MOVER, SC)
supply._full_supply = REAL_FULL
check("★ run_full_supply 的訊息帶 TALK_FAIL", supply.TALK_FAIL in msg2, msg2)
check("　原本那一步的說明也留著", "開交易失敗" in msg2, msg2)
check("　整趟成功就不報（免得吵他）", (lambda: (
    supply._talk_clear(), True)[1])())

print()
print("④b 對話開得了、選項也送了，卻一直沒換到目標視窗 → 一樣停，但訊息講實話")
reset()
supply._wait_dialog = lambda *a, **k: CLOCK.sleep(0.5) or True
supply._talkaction = lambda *a, **k: True
supply._wnd_open = lambda m, s, n: False
ok = supply._engage_npc(MOVER, SC, 1, (170, 90), [10], "WND_NPCSALE",
                        tries=3)
check("★ 回 False", ok is False)
check("★ 原因不是「沒開對話」（那是另一回事）",
      "沒開對話" not in supply.talk_failure(), supply.talk_failure())
check("　說得出是沒換到哪個視窗", "WND_NPCSALE" in supply.talk_failure(),
      supply.talk_failure())

print()
print("⑤ 對話開了 → 送選項 → 目標視窗開了 → True")
reset()
supply._wait_dialog = lambda *a, **k: True
SENT: list = []
supply._talkaction = lambda m, s, code: SENT.append(code) or True
supply._wnd_open = lambda m, s, n: len(SENT) >= 2
ok = supply._engage_npc(MOVER, SC, 1, (170, 90),
                        [supply.TALK_BANK_USE, supply.TALK_BANK_SELF],
                        supply.BANK_WND)
check("★ 回 True", ok is True)
check("★ 兩個選項照順序送出",
      SENT == [supply.TALK_BANK_USE, supply.TALK_BANK_SELF], str(SENT))
supply._talkaction = lambda *a, **k: True

print()
print("⑥ 「讀不到≠沒有」：驗不了但旗標沒說沒開 → 照樣送選項")
reset()
supply._wait_dialog = no_dialog                   # 看不到邊沿
supply.talkwnd.window_visible = staticmethod(lambda sc: None)   # 問不到
SENT2: list = []
supply._talkaction = lambda m, s, code: SENT2.append(code) or True
supply._wnd_open = lambda m, s, n: bool(SENT2)
ok = supply._engage_npc(MOVER, SC, 1, (170, 90), [10], "WND_NPCSALE")
check("★ 照樣把選項送出去（成沒成交給視窗檢查）", SENT2 == [10], str(SENT2))
check("　最後回 True", ok is True)

print()
print("⑦ 旗標**明說** False（畫面上確定沒對話框）→ ⛔ 不硬送選項")
reset()
supply._wait_dialog = no_dialog
supply.talkwnd.window_visible = staticmethod(lambda sc: False)
SENT3: list = []
supply._talkaction = lambda m, s, code: SENT3.append(code) or True
supply._wnd_open = lambda m, s, n: False
ok = supply._engage_npc(MOVER, SC, 1, (170, 90), [10], "WND_NPCSALE")
check("★ 一個選項都沒送", SENT3 == [], str(SENT3))
check("　回 False 並記一筆講不到話", ok is False and supply.talk_failure() != "")
supply._talkaction = lambda *a, **k: True

print()
print("⑧ NPC 還沒串流 → 繼續走，⚠ 那段**不算**講不到話")
reset(npc_visible=False)
supply._wait_dialog = no_dialog
supply._wnd_open = lambda m, s, n: False


def walk_and_tick(*_a, **_k):
    APPROACHES.append(1)
    CLOCK.sleep(10.0)                 # 每走一段花 10 秒


supply._approach_npc = walk_and_tick
ok = supply._engage_npc(MOVER, SC, 1, (170, 90), [10], "WND_NPCSALE")
check("★ 一直走到 WALK_TIMEOUT 才放棄",
      len(APPROACHES) >= supply.WALK_TIMEOUT / 10.0, str(len(APPROACHES)))
check("★ ⛔ 一次都沒點（看不到他哪來的 eid）", CLICKS == [], str(CLICKS))
check("　原因是「看不到他」不是「沒開對話」",
      "看不到他" in supply.talk_failure(), supply.talk_failure())

print()
print("⑨ _approach_npc：走不走得到都照走，卡住就回去讓 TryAct 收尾")
supply._approach_npc = REAL_APPROACH
supply._wait_move_done = lambda *a, **k: CLOCK.sleep(0.5)
HERE = [(5.5, 5.5)]
supply._player_tile = lambda sc: (0x3000, HERE[0])
supply._ent_tile_f = lambda sc, ent: (20.5, 5.5)
supply.find_npc = lambda sc, nid: (0x1000, 0x2000)
MOVER.walks.clear()
REAL_APPROACH(MOVER, SC, 1, (170, 90))
check("★ 卡住（位置一直沒變）→ APPROACH_STALL 秒內就回來，不磨滿",
      0 < len(MOVER.walks) <= supply.APPROACH_STALL / 0.5 + 2,
      str(len(MOVER.walks)))
check("★ 走的是**官方尋路**對 NPC 本人（⛔ 不是挑出來的可走格）",
      all(w[1:] == (20.5, 5.5) for w in MOVER.walks), str(MOVER.walks[:3]))

MOVER.walks.clear()
HERE[0] = (19.0, 5.5)                 # 已經在 CLICK_RANGE 內
REAL_APPROACH(MOVER, SC, 1, (170, 90))
check("★ 已經夠近就一步都不走", MOVER.walks == [], str(MOVER.walks))

MOVER.walks.clear()
HERE[0] = (5.5, 5.5)
supply.find_npc = lambda sc, nid: None            # 還沒串流
REAL_APPROACH(MOVER, SC, 1, (170, 90))
check("★ 看不到他 → 改走 .MPC 表座標（把人帶進串流範圍）",
      bool(MOVER.walks) and all(w[1:] == (170.0, 90.0) for w in MOVER.walks),
      str(MOVER.walks[:3]))

MOVER.walks.clear()
supply._player_tile = lambda sc: (None, None)     # 讀不到自己
REAL_APPROACH(MOVER, SC, 1, (170, 90))
check("　讀不到玩家座標就安全退出（不亂走）", MOVER.walks == [])

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
