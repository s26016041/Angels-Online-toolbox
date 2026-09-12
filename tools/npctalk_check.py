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
import types

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
REAL_TOKEN0 = supply._dialog_token   # ④b 要用它本人（②會換成 lambda sc: 7）
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
PAGE_SEEN: list = []


def fake_wait_dialog(sc, base, timeout=supply.DIALOG_TIMEOUT, again=None,
                     page_base=supply._UNSET_PAGE, **_kw):
    seen.append(timeout)
    AGAIN_SEEN.append(again)
    PAGE_SEEN.append(page_base)
    return False                                 # 一律「沒開」，逼它換站位


supply._wait_dialog = fake_wait_dialog

GAP = [1.0]                                      # 貼身
supply._npc_gap = lambda sc, nid: GAP[0]
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=1,
                   confirm=lambda: False, confirm_timeout=0.1)
check(f"貼身點 → 等 {supply.DIALOG_NEAR_TIMEOUT:.0f} 秒",
      seen == [supply.DIALOG_NEAR_TIMEOUT], f"實得 {seen}")
# ★★ 2026-09-10 使用者實機回報「買水一直上下、前後走」：貼著點**不准**再每
#   CLICK_REPEAT 秒補點 —— 那只會把人一直往 NPC 推，_wait_arrival 的「停穩」
#   永遠過不了 → 判成點不開 → _nudge_toward 穿來穿去。
check("⛔ 貼身點不給「補點」回呼（不然人被 TryAct 一直推著走）",
      AGAIN_SEEN == [None], f"實得 {AGAIN_SEEN}")
check("貼身點也要帶「點之前的對話頁簽章」當第二個訊號",
      PAGE_SEEN and PAGE_SEEN[-1] is not supply._UNSET_PAGE,
      f"實得 {PAGE_SEEN}")

seen.clear()
AGAIN_SEEN.clear()
GAP[0] = 20.0                                    # 遠處（客戶端要自己走過去）
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=1,
                   confirm=lambda: False, confirm_timeout=0.1)
check(f"遠處點 → 留 {supply.DIALOG_TIMEOUT:.0f} 秒",
      seen == [supply.DIALOG_TIMEOUT], f"實得 {seen}")
check("遠處點照舊給「補點」的回呼（官方那個重試迴圈＝走過去的動力）",
      bool(AGAIN_SEEN) and all(callable(x) for x in AGAIN_SEEN),
      f"實得 {AGAIN_SEEN}")

clicked: list = []
supply._click_npc = lambda m, s_, ent: clicked.append(ent) or True
AGAIN_SEEN[-1]()
check("補點前**重新找那隻 NPC**（不吃上一拍的實體位址）",
      clicked == [0x1000], f"實得 {clicked}")
supply._click_npc = lambda *a, **k: True

print()
print("②b 站定了就**動口不動腳**（使用者 2026-09-10：「走到最近然後別動，別一直抖」）")
# ★★★★ 站在講話方框內／已經站上「離 NPC 最近可到的格」→ 不再走、也不 _nudge_toward
#   穿到 NPC 另一側。點不開就原地重點，穿來穿去只會把人晃來晃去（他連續兩次回報）。
REAL_BOX, REAL_PLAYER_TILE = supply._box_status, supply._player_tile
nudges: list = []
walks: list = []
supply._nudge_toward = lambda *a, **k: nudges.append(1) or True
supply._walk_to_npc = lambda *a, **k: walks.append(1) or True
supply._player_tile = lambda sc: (0x3000, (5.5, 5.5))
BOX = {"in_box": True, "free": [(5, 5)], "npc_tile": (6, 5)}
supply._box_status = lambda *a, **k: BOX
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=3,
                   confirm=lambda: False, confirm_timeout=0.1)
check("⛔ 在方框內：一次都不 nudge（不穿到 NPC 另一側）", nudges == [],
      f"實得 {len(nudges)} 次")
check("⛔ 在方框內：一步都不走", walks == [], f"實得 {len(walks)} 次")

nudges.clear(); walks.clear()
BOX = {"in_box": False, "free": [(5, 5)], "npc_tile": (9, 9)}   # 站在最近格上
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=3,
                   confirm=lambda: False, confirm_timeout=0.1)
check("⛔ 已站在最近可到的格：照樣不 nudge、不走",
      nudges == [] and walks == [], f"實得 nudge {len(nudges)}／walk {len(walks)}")

nudges.clear(); walks.clear()
BOX = {"in_box": False, "free": [(20, 20)], "npc_tile": (21, 20)}  # 還沒到位
# ⚠ 只有**真的走不到**（_walk_to_npc 回 False：人牆／算不出路）才照舊走 nudge 階梯；
#   走到了就舉 arrived 閂，之後怎麼樣都不再動腳（見下一個案例）。
supply._walk_to_npc = lambda *a, **k: walks.append(1) or False
supply._wait_arrival = lambda *a, **k: False     # 人沒到位 → fails 才會加
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=2,
                   confirm=lambda: False, confirm_timeout=0.1)
check("★ 走不到的時候照舊：先走過去，點不開才 nudge",
      walks and nudges, f"實得 walk {len(walks)}／nudge {len(nudges)}")
supply._wait_arrival = lambda *a, **k: True
nudges.clear(); walks.clear()
approaches: list = []
supply._approach_npc = lambda *a, **k: approaches.append(1)
supply._wait_arrival = lambda *a, **k: False     # 逼 fails 累加（舊版就會開始 nudge）
STATE = {"box": {"in_box": False, "free": [(20, 20)], "npc_tile": (21, 20)}}
supply._box_status = lambda *a, **k: STATE["box"]


def walk_then_lose(*_a, **_k):
    """走到了 → 但之後地形圖讀不到（換圖瞬間／官方 TryAct 把人推離那格）。"""
    walks.append(1)
    STATE["box"] = None
    return True


supply._walk_to_npc = walk_then_lose
supply._engage_npc(MOVER, SC, 1, (0, 0), [10], "", tries=4,
                   confirm=lambda: False, confirm_timeout=0.1)
# ★★★★ 使用者 2026-09-11：「走到能走到的 NPC 最近，然後用官方的對話自己把最後那段
#   小小路走完並對話」——「到過了」是一次性的閂，⛔ 不准因為下一輪判斷變了又走一次
#   （那就是他看到的「商人旁邊走出去又回去」）。
check("★ 走到過就不再動腳：只走一次，之後不 nudge、也不 approach",
      walks == [1] and nudges == [] and approaches == [],
      f"實得 walk {len(walks)}／nudge {len(nudges)}／approach {len(approaches)}")
supply._wait_arrival = lambda *a, **k: True
supply._approach_npc = lambda *a, **k: None
supply._box_status, supply._player_tile = REAL_BOX, REAL_PLAYER_TILE
supply._nudge_toward = lambda *a, **k: True

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
print("④b 對話框開了但 WND_MESSAGE 的值沒變（同一個視窗重開＝同一個位址）")
# ★★★ 2026-09-10：這正是「買水一直上下、前後走」的源頭 —— 代號邊沿看不到 →
#   判成「沒開」→ _nudge_toward 踩上 NPC／穿到另一側換站位重點。
#   → 加第二個訊號：**點之前 vs 現在的對話頁簽章**（新的一頁來了就會變）。
REAL_TALKWND = supply.talkwnd
PAGE = [types.SimpleNamespace(sig=("舊頁",), wnd=7)]
VIS = [None]                       # 「視窗顯示中」旗標：None＝不知道（退回舊訊號）
supply.talkwnd = types.SimpleNamespace(page=lambda sc: PAGE[0],
                                       window_visible=lambda sc: VIS[0])
CLOCK.t = t0
opened = REAL_WAIT(SC, 7, supply.DIALOG_NEAR_TIMEOUT, page_base=("舊頁",))
check("頁面沒變 → 照舊回 False", opened is False)

PAGE[0] = types.SimpleNamespace(sig=("新的一頁",), wnd=7)   # 代號一樣，內容換了
CLOCK.t = t0
opened = REAL_WAIT(SC, 7, supply.DIALOG_NEAR_TIMEOUT, page_base=("舊頁",))
check("★ 新的一頁來了 → 認定對話開了（代號沒變也算）", opened is True)

CLOCK.t = t0
opened = REAL_WAIT(SC, 7, supply.DIALOG_NEAR_TIMEOUT)      # 沒傳 page_base
check("沒傳頁面基準 ＝ 完全是舊行為（只看代號）", opened is False)

PAGE[0] = None                                             # 頁面讀不到
CLOCK.t = t0
opened = REAL_WAIT(SC, 7, supply.DIALOG_NEAR_TIMEOUT, page_base=("舊頁",))
check("頁面讀不到 → 退回只看代號，不亂判", opened is False)

# ★★★★ 2026-09-12：有了「視窗真的顯示中」的硬旗標（視窗物件 +0xB4，
#   talkwnd.window_visible）就不必再等代號／簽章變 —— 使用者問「對話框不是
#   馬上就會出現嗎，為何要等」，答案是以前**看不出來**，只能把逾時磨滿。
PAGE[0] = types.SimpleNamespace(sig=("舊頁",), wnd=7)      # 代號、頁面都沒變
VIS[0] = True
CLOCK.t = t0
opened = REAL_WAIT(SC, 7, supply.DIALOG_NEAR_TIMEOUT, page_base=("舊頁",))
check("★ 旗標說顯示中 → 立刻認定開了（代號沒變、頁面也沒變）", opened is True)
check("　而且幾乎沒花時間（⛔ 不再磨滿逾時）", CLOCK.t - t0 < 0.3,
      f"花了 {CLOCK.t - t0:.2f} 秒")
VIS[0] = False                     # 旗標說沒顯示 → 照舊看另外兩個訊號
CLOCK.t = t0
opened = REAL_WAIT(SC, 7, supply.DIALOG_NEAR_TIMEOUT, page_base=("舊頁",))
check("旗標說沒顯示 → 照舊回 False", opened is False)
VIS[0] = None                      # 讀不到（改版抄不到偏移）→ 完全是舊行為
PAGE[0] = types.SimpleNamespace(sig=("新的一頁",), wnd=7)
CLOCK.t = t0
opened = REAL_WAIT(SC, 7, supply.DIALOG_NEAR_TIMEOUT, page_base=("舊頁",))
check("旗標讀不到 → 退回頁面簽章，不亂判", opened is True)

# ★★★★ 2026-09-12 實機（雪狐 → 暴走穗海農場，兩趟都重現）的真兇：旗標說
#   「畫面上沒有對話框」，但代號跟基準比起來不一樣（一邊有符號、一邊無符號
#   ＝同一個代號的兩種寫法）→ 舊寫法宣告「對話開了」→ 對著沒開的對話送掉三個
#   選項 → 磨滿 20 秒確認逾時 → 再點一次才成。33 秒裡 20 秒是這樣白等的。
VIS[0] = False
PAGE[0] = types.SimpleNamespace(sig=("新的一頁",), wnd=2582610512)
CLOCK.t = t0
opened = REAL_WAIT(SC, -1712356784, supply.DIALOG_NEAR_TIMEOUT,
                   page_base=("舊頁",))
check("★★★ 旗標說沒顯示 → 代號變了／頁面變了**都不算開**（實機那 20 秒的根因）",
      opened is False, f"回了 {opened}")
VIS[0] = None                      # 問不到旗標 → 那才是真的「驗不了」，照舊採信
CLOCK.t = t0
opened = REAL_WAIT(SC, -1712356784, supply.DIALOG_NEAR_TIMEOUT,
                   page_base=("舊頁",))
check("　旗標問不到 → 照舊採信軟訊號（⛔ 不要因為修 bug 把退路也砍了）",
      opened is True, f"回了 {opened}")
supply.talkwnd = REAL_TALKWND
REAL_GLOBALS0 = supply.lua.globals_of
supply.lua.globals_of = lambda sc, names: {supply.DIALOG_WND: -1712356784}
check("★ _dialog_token 回**無符號**（跟 talkwnd.Page.wnd 同一種表示法才比得起來）",
      REAL_TOKEN0(SC) == 2582610512, str(REAL_TOKEN0(SC)))
supply.lua.globals_of = REAL_GLOBALS0
supply.talkwnd = types.SimpleNamespace(page=lambda sc: PAGE[0],
                                       window_visible=lambda sc: VIS[0])

print()
print("④c 選項之間：看到下一頁就送，⛔ 不睡滿 TALK_GAP（使用者 2026-09-12）")
PAGE[0] = types.SimpleNamespace(sig=("第一頁",), wnd=7)
CLOCK.t = t0


def _turn_page(_sc):
    # 第二次問的時候就換頁（＝伺服器回了下一頁）
    if CLOCK.t - t0 >= supply.TALK_STEP_FLOOR + 0.05:
        return types.SimpleNamespace(sig=("第二頁",), wnd=7)
    return PAGE[0]


supply.talkwnd = types.SimpleNamespace(page=_turn_page,
                                       window_visible=lambda sc: None)
got = supply._wait_page(SC, ("第一頁",), supply.TALK_GAP)
check("換頁了 → True", got is True)
check(f"　而且沒睡滿 {supply.TALK_GAP} 秒", CLOCK.t - t0 < supply.TALK_GAP,
      f"花了 {CLOCK.t - t0:.2f} 秒")
check(f"　但有留最小間隔 {supply.TALK_STEP_FLOOR} 秒（太快送伺服器不吃）",
      CLOCK.t - t0 >= supply.TALK_STEP_FLOOR, f"花了 {CLOCK.t - t0:.2f} 秒")
CLOCK.t = t0
supply.talkwnd = types.SimpleNamespace(page=lambda sc: PAGE[0],
                                       window_visible=lambda sc: None)
got = supply._wait_page(SC, ("第一頁",), supply.TALK_GAP)
check("一直沒換頁 → False，而且等滿上限（跟以前一樣照送）", got is False
      and CLOCK.t - t0 >= supply.TALK_GAP - 0.1, f"花了 {CLOCK.t - t0:.2f} 秒")
CLOCK.t = t0
supply.talkwnd = types.SimpleNamespace(page=lambda sc: None,
                                       window_visible=lambda sc: None)
got = supply._wait_page(SC, ("第一頁",), supply.TALK_GAP)
check("頁面讀不到 → False（⛔ 不當成換頁了）", got is False)

print()
print("④d 視窗開了沒：代號非 0 **不算**開著（WND_NPCSALE 關掉不歸零）")
supply.talkwnd = types.SimpleNamespace(
    page=lambda sc: None, window_visible=lambda sc: None,
    id_visible=lambda sc, wnd: VIS[0])
REAL_GLOBALS = supply.lua.globals_of
supply.lua.globals_of = lambda sc, names: {n: 0x1234 for n in names}
VIS[0] = True
check("代號非 0 ＋ 旗標說顯示中 → 開著",
      supply._wnd_open(None, SC, "WND_NPCSALE") is True)
VIS[0] = False
check("★ 代號非 0 但旗標說沒顯示 → **沒開**（就是「假成功」那個坑）",
      supply._wnd_open(None, SC, "WND_NPCSALE") is False)
VIS[0] = None
check("旗標讀不到 → 沿用舊判斷（代號非 0 就算開）",
      supply._wnd_open(None, SC, "WND_NPCSALE") is True)
supply.lua.globals_of = lambda sc, names: {}
check("代號是 0／讀不到 → 沒開",
      supply._wnd_open(None, SC, "WND_NPCSALE") is False)
supply.lua.globals_of = REAL_GLOBALS
supply.talkwnd = REAL_TALKWND

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

    def step(self, *a, **k):
        pass


grid = FakeGrid({(213, 54), (213, 53), (212, 52), (129, 166)}, {(129, 168), (128, 168)})
supply.terrain.load = lambda sc: (grid, None)
supply.navigate.Navigator = FakeNav
supply._npc_tile = lambda sc, nid: None                  # 銀行 NPC 還沒串流進來
supply._player_tile = lambda sc: (0x3000, (213.5, 53.5))  # round → (214,54)：不可走
REAL_WALK(MOVER, SC, 1890, (129, 168), timeout=0.5)
check("站的格不可走 → 問旁邊一圈取最大區，目標＝離銀行最近的可走格 (129,166)",
      bool(FakeNav.goals) and FakeNav.goals[0] == (129.5, 166.5), f"實得 {FakeNav.goals[:2]}")
check("全程沒把孤島的表座標 (129,168) 當目標",
      all(g != (129.5, 168.5) for g in FakeNav.goals), str(FakeNav.goals[:3]))
FakeNav.goals.clear()
supply._player_tile = lambda sc: (0x3000, (300.0, 300.0))  # 旁邊一圈也全不可走
REAL_WALK(MOVER, SC, 1890, (129, 168), timeout=0.5)
check("整圈都問不到（地形圖跟人對不上）→ 才硬走表座標（沒有上一個目標可沿用）",
      bool(FakeNav.goals) and FakeNav.goals[0] == (129.5, 168.5), f"實得 {FakeNav.goals[:2]}")

print()
print("⑥ 走去 NPC：站位**選定就不換**（使用者 2026-09-10「別一直抖動」）")
# ★★★ 可走區是從「人現在站的格」泛洪算的 → 人一動，「離 NPC 最近可到的格」就可能
#   跳到另一邊；舊寫法每 ~2 秒重挑一次 → 目標在兩格之間跳 → 人跟著來回走。
SPOTS = [(10, 10), (30, 30), (10, 10), (30, 30)]


def fake_box(*_a, **_k):
    s = SPOTS.pop(0) if len(SPOTS) > 1 else SPOTS[0]
    return {"in_box": False, "free": [s], "npc_tile": (11, 10)}


supply._box_status = fake_box
supply.find_npc = lambda sc, nid: (0x1000, 0x2000)
supply._npc_tile = lambda sc, nid: (11.5, 10.5)
supply._player_tile = lambda sc: (0x3000, (5.5, 5.5))
FakeNav.goals.clear()
REAL_WALK(MOVER, SC, 1, (0, 0), timeout=12.0)
check("★ 目標只挑一次（後面就算算出別的格也不換）",
      FakeNav.goals == [(10.5, 10.5)], f"實得 {FakeNav.goals}")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
