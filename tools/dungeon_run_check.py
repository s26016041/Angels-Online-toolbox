"""自動刷副本的狀態機回歸測試（離線，不必開遊戲）。

    py tools\\dungeon_run_check.py

跑的是分頁**自己的**邏輯（`_run_step` / `_finish`），只把「跟遊戲講話」那幾支
換成替身（走路、點物件、送選項）。⚠ 這是 memory `test-via-button` 的教訓：
替身只換 I/O，邏輯要跑真的 —— 換掉整個模組就會測到替身。
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication                # noqa: E402

_app = QApplication.instance() or QApplication([])

from app.game import dungeon                              # noqa: E402
from app.tabs import dungeon_tab as dt                    # noqa: E402

PASS = FAIL = 0
TICK = dt.TICK_MS / 1000.0


def ck(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}　{note}")


class FakeNav:
    """假尋路：只記「被叫去哪裡」，永遠不會 stuck。"""

    def __init__(self):
        self.goal = None
        self.stuck = False
        self.stuck_reason = ""
        self.exhausted = False        # 最短路走完人卻還離目標 >3 格（見 navigate）
        self.calls = 0

    def reset(self, goal=None):
        self.goal = goal

    def step(self, _sc, _mv, _pobj, gx, gy, arrive=None):
        self.calls += 1
        self.goal = (gx, gy)
        self.arrive = arrive
        return "走路中"


class FakeMon:
    """最小的假怪：挑目標／可達過濾／收工判定只用到這幾個欄位。"""

    def __init__(self, x=10.0, y=10.0, eid=1, name="怪", addr=0x3000):
        self.x, self.y, self.eid, self.name, self.addr = x, y, eid, name, addr
        self.dead = False
        self.hp_zero = False


class FakeNotifier:
    """假通知器：只記「送了什麼」，不響、不跳視窗。"""

    def __init__(self):
        self.fired = []

    def fire(self, who, msg):
        self.fired.append((who, msg))
        return "假通知"


class FakeKeys:
    """最小的假出手執行緒：沒怪時 _fight 會叫 set_on(False)。"""

    def __init__(self):
        self.on = None
        self.eid = None
        self.player = None
        self.pos = None
        self.handoff = False
        self.reach = 0.0
        self.client_walk = False
        self.mode = dt.MODE_PACKET
        self.packets = False
        self.skill = 0
        self.mover = None
        self.selected = False
        self.min_range = 3.0
        self.open_wait = 0.0
        self.open_note = ""

    def in_range_of_any(self, dist):
        # 跟真的一樣：歐氏距離 ≈ 射程 + 1，上限 12
        return dist is None or dist <= min(12.0, float(self.min_range) + 1.0)

    def set_on(self, v):
        self.on = v

    def stop(self):
        pass

    def wait(self, _ms=0):
        pass


class FakeMover:
    """假跳板：只記「叫我走去哪、留幾格、給了哪些點」。"""

    def __init__(self):
        self.active = True
        self.near = []          # walk_near 的 (x, y, keep)
        self.routes = []        # walk_route 的 (x, y, stop_short, points)

    def walk_near(self, _sc, _p, x, y, keep):
        self.near.append((x, y, keep))
        return True

    def walk_route(self, _sc, _p, x, y, stop_short=0.0, wait=0.12, points=None):
        self.routes.append((x, y, stop_short, list(points or [])))
        return len(points or []) or 1


class FakeGrid:
    """假地形：`cells` ＝我這一區走得到的格；`others` ＝別區的可走格。"""

    def __init__(self, cells, others=()):
        self.cells = set(cells)
        self.others = set(others)

    def reachable(self, x, y):
        return set(self.cells) if (x, y) in self.cells else None

    def walkable(self, x, y):
        return (x, y) in self.cells or (x, y) in self.others

    def line_free(self, a, b):
        return not getattr(self, "wall_between", False)

    def route(self, start, goal, relax=4, max_cost=None):
        return [(int(start[0]), int(start[1])), (int(goal[0]), int(goal[1]))]


class FakeAtk:
    """最小的假「寫目標」執行緒。"""

    def __init__(self):
        self.picked = None
        self.packets = False
        self.engaged = False
        self.hp = 0                 # 目標血量（真的那支是 TargetWorker 讀回來的）

    def attack(self, _state, mon):
        self.picked = mon

    def hold_off(self):
        pass

    def stop(self):
        pass

    def wait(self, _ms=0):
        pass


class FakeScan:
    """假掃描執行緒：只記 force_full 被叫幾次（真的那條是 QThread）。"""

    def __init__(self):
        self.fulls = 0

    def request(self, *_a):
        pass

    def force_full(self, _pid):
        self.fulls += 1

    def stop(self):
        pass

    def wait(self, _ms=0):
        pass


class FakeMaps:
    """假地形快取：記 drop() 被叫幾次（Cache 有 __slots__ 不能改方法）。"""

    def __init__(self, grid=None):
        self.grid = grid
        self.drops = 0

    def drop(self):
        self.drops += 1

    def get(self, _sc):
        return self.grid


class FakeTrig:
    """假觸發物件（傳點）：portal.nearby / portal.enter 用得到的欄位。"""

    def __init__(self, x, y, model, oid=0x13920001):
        self.x, self.y, self.model, self.oid = x, y, model, oid
        self.addr = 0x4000
        self.select_id = 0x5E140000

    def dist(self, p):
        return ((self.x - p[0]) ** 2 + (self.y - p[1]) ** 2) ** 0.5


class FakeProp:
    def __init__(self, x, y, model, oid=0x13920001):
        self.x, self.y, self.model, self.oid = x, y, model, oid

    def dist(self, p):
        return ((self.x - p[0]) ** 2 + (self.y - p[1]) ** 2) ** 0.5


class FakeTalk:
    """假對話：照一串頁面依序回，送了動作才翻到下一頁。"""

    def __init__(self, pages):
        self.pages = list(pages)      # 每頁 = None(結束) 或 選項 tuple
        # ⚠ -1 ＝「還沒點，讀到的是上一次的殘留」——真實世界就是這樣
        #   （那些 Lua 全域關掉對話還留著），執行端靠「簽章有沒有變」
        #   判斷點到了沒。
        self.i = -1
        self.closes = 0
        self.i_lock = False           # True ＝ 送了動作也不換頁（伺服器沒回）
        self.unknown = False          # True ＝ 問不到有沒有視窗（叫不動）
        self.ended = True             # 按確定那一頁是不是最後一頁（message_ended）

    def opened(self):
        if self.i < 0:
            self.i = 0

    def window_open(self, _mv, _sc):
        """有沒有對話視窗 —— 真實世界那支硬訊號（不知道回 None）。

        ⚠ 沒點之前／翻完之後都是**沒有視窗**，但 `page()` 照樣讀得到殘留值，
          這就是要模擬的重點。
        """
        if self.unknown:
            return None
        return 0 <= self.i < len(self.pages)

    def page(self, _sc):
        if self.i < 0:
            return dt.talkwnd.Page(is_talk=True, options=(), sig=("殘留",))
        # ⚠ 翻到底之後 i 就不再變 → 簽章固定 ＝ 真實世界「對話結束後那些
        #   全域凍在最後一頁」的行為，執行端就是靠這個判對話走完了。
        j = min(self.i, len(self.pages) - 1) if self.pages else 0
        opts = tuple(self.pages[j] or ()) if self.pages else ()
        return dt.talkwnd.Page(is_talk=not opts, options=opts,
                               sig=(min(self.i, len(self.pages)), opts))

    def close_gone(self, _mv, _sc):
        """按確定 → 整段對話直接結束（**視窗消失**）。

        ⚠ 那些 Lua 全域凍在最後一頁不會被清掉 —— 真實世界就是這樣，
          所以只有「有沒有視窗」問得出真相。
        """
        self.closes += 1
        self.i = len(self.pages)
        return True, "送出"

    def close(self, _mv, _sc):
        self.closes += 1
        if not self.i_lock:
            self.i = min(max(self.i, 0) + 1, len(self.pages))
        return True, "送出"


def wire(tab, fake):
    dt.talkwnd.page = fake.page
    dt.talkwnd.window_open = fake.window_open
    dt.talkwnd.message_ended = lambda _sc: fake.ended
    dt.talkwnd.close_page = fake.close
    dt.talkwnd.close_window = lambda *_a: True
    real = dt.sell.talk

    def talk(mv, code):
        if not fake.i_lock:
            fake.i = min(fake.i + 1, len(fake.pages))
        return real(mv, code)
    dt.sell.talk = talk

    def click(*_a, **_k):            # 點下去 → 對話才開起來
        fake.opened()
        return True, "點了"
    dt.produce.click = click


def make_tab(steps, pos=(10.0, 10.0), props=(), mons=()):
    """建一個分頁，內部狀態直接擺好，跳過「選分身／裝跳板」那一段。"""
    tab = dt.DungeonTab()
    tab._script = dungeon.Script(name="t", steps=list(steps))
    tab._sc = object()
    tab._pid = 1
    tab._mover = object()
    tab._state = 0x1000
    tab._player = 0x2000
    tab._reset_run()
    tab._nav = FakeNav()
    tab._pos = list(pos)
    tab._me = tuple(pos)
    tab._my_pos = lambda: tuple(tab._pos)
    dt.entity.read_live_hp = lambda _sc, m: (True, "", (m.x, m.y), -1)
    tab._notifier = FakeNotifier()          # ⚠ 真的會響警報＋跳視窗
    dt.player.locate_fast = lambda _sc: None   # 死亡判定的基準：測試裡自己塞
    tab._live_monsters = lambda: list(mons)
    tab._refresh_steps = lambda: None
    # ⚠ 掃描是真的 QThread：測試裡換成假的（也順便收掉，不然一堆殘留執行緒）
    tab._scan.stop()
    tab._scan.wait(500)
    tab._scan = FakeScan()
    tab.run_cb.blockSignals(True)
    tab.run_cb.setChecked(True)
    tab.run_cb.blockSignals(False)
    dt.scenery.nearby = lambda _sc, around=None, r=0: [
        p for p in props
        if around is None or p.dist(around) <= r]
    dt.produce.click = lambda *a, **k: (True, "點了")
    # ⚠ 傳點那條會叫 portal（讀記憶體）——測試裡換成假的
    tab.portal_sent = []
    dt.portal.nearby = lambda _sc, around=None, r=0: [
        t for t in getattr(tab, "trigs", [])
        if around is None or t.dist(around) <= r]
    dt.portal.enter = lambda _mv, _sc, t, _pf: (
        tab.portal_sent.append(t.model), (True, "已送出 0x0D"))[1]
    dt.move.pathfinder_this = lambda _sc: 0x2000
    tab.sent = []
    dt.sell.talk = lambda _mv, code: (tab.sent.append(code), True)[1]
    tab.left = []
    dt.supply.leave_npc = lambda *a, **k: tab.left.append(1)
    # ⚠ 對話那條路現在會問 talkwnd（讀 Lua）——測試一律換成假的，
    #   預設「每一頁都有選項 1~3」，要別的行為的測試自己再 wire 一次。
    wire(tab, FakeTalk([(1, 2, 3), (1, 2, 3), (), ()]))
    return tab


def run(tab, secs: float) -> None:
    for _ in range(int(secs / TICK)):
        if not tab.run_cb.isChecked():
            return
        # 外圈（補給／飛回入口／組隊）有事就先跑外圈（跟真的 _tick 一樣）
        if tab._cycle_tick(TICK):
            continue
        if tab._i >= len(tab._script.steps):
            tab._finish(TICK)
            continue
        tab._run_step(tab._my_pos(), TICK)


def main() -> int:
    print("走位步驟")
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    run(tab, 0.5)
    ck("還沒到 → 一直叫尋路", tab._i == 0 and tab._nav.calls > 0)
    ck("尋路目標就是腳本點位", tab._nav.goal == (50, 50), str(tab._nav.goal))
    tab._pos = [50.5, 50.5]
    run(tab, 0.2)
    ck("走到了 → 前進下一步", tab._i == 1)

    # ★★ 「現在沒有路」⛔ **永遠不會因此停機**（使用者 2026-09-02：
    #   「不會有『幾秒沒到就壞掉』，那個拔掉」）——重讀地形一直試。
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    tab._nav.stuck, tab._nav.stuck_reason = True, "grid"
    run(tab, 1.0)
    ck("★★ 說走不到 → 重讀地形繼續試，不停機",
       tab.run_cb.isChecked() and "繼續試" in tab.status.text(),
       tab.status.text())
    ck("　會一直重讀地形（門一開就走）", tab._grid_t == 0.0)
    run(tab, 120.0)
    ck("★★ 兩分鐘之後**照樣還在跑**（⛔ 沒有逾時這種東西）",
       tab.run_cb.isChecked(), tab.status.text())
    ck("　狀態列講得出等多久了", "分鐘" in tab.status.text(), tab.status.text())
    # 門開了（不再 stuck）→ 等待計時要歸零
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    tab._nav.stuck, tab._nav.stuck_reason = True, "grid"
    run(tab, 5.0)
    tab._nav.stuck = False
    run(tab, 0.2)
    ck("★ 門開了（尋路不再說沒路）→ 等待計時歸零", tab._unreach_t == 0.0,
       str(tab._unreach_t))

    print("\n等待步驟")
    tab = make_tab([{"do": "wait", "secs": 1.0}, {"do": "clear"}])
    run(tab, 0.5)
    ck("時間沒到不前進", tab._i == 0)
    run(tab, 0.8)
    ck("時間到 → 前進", tab._i == 1)

    # ★ 使用者 2026-09-02：「休息要確認周圍沒有可以打到的怪物才能進入休息」
    tab = make_tab([{"do": "wait", "secs": 1.0}, {"do": "clear"}])
    tab._live_monsters = lambda: [FakeMon(x=11.0, y=10.0, eid=1)]
    run(tab, 2.0)
    ck("★★ 周圍還有走得到的怪 → 不進入休息，也不倒數",
       tab._i == 0 and tab._wait_left == 0.0, tab.status.text())
    ck("　訊息講得出原因", "先別休息" in tab.status.text(), tab.status.text())
    tab._live_monsters = lambda: []
    run(tab, 0.5)
    ck("　清光了才開始數", tab._i == 0 and tab._wait_left > 0)
    run(tab, 0.8)
    ck("　數完才前進", tab._i == 1)

    # 休息數到一半冒出怪 → 從頭數（不是接著數）
    tab = make_tab([{"do": "wait", "secs": 2.0}, {"do": "clear"}])
    tab._keys, tab._atk = FakeKeys(), FakeAtk()
    run(tab, 1.0)
    ck("休息數到一半", 0 < tab._wait_left < 2.0, str(tab._wait_left))
    tab._live_monsters = lambda: [FakeMon(x=11.0, y=10.0, eid=2)]
    dt.entity.read_pos = lambda _sc, _addr: (11.0, 10.0)
    tab._fight((10.0, 10.0), TICK)
    ck("★ 打起來 → 正在數的休息作廢（等清乾淨從頭數）",
       tab._wait_left == 0.0, str(tab._wait_left))

    print("\n清怪步驟")
    tab = make_tab([{"do": "clear"}])
    run(tab, dt.CLEAR_SETTLE - 0.5)
    ck("沉澱時間沒滿不算清完", tab._i == 0)
    run(tab, 1.0)
    ck(f"★ 連續 {dt.CLEAR_SETTLE:.0f} 秒沒怪才算清完", tab._i == 1)

    print("\n對話步驟")
    step = {"do": "interact", "at": [20, 20], "model": 60307, "menu": [1, 2]}
    tab = make_tab([step], pos=(10.0, 10.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    run(tab, 0.3)
    ck("太遠 → 先走過去，不亂點", tab._nav.goal == (20, 20) and not tab._clicked)
    tab._pos = [21.0, 20.0]
    run(tab, 0.2)
    ck("走近了 → 點下去", tab._clicked)
    run(tab, dt.MENU_GAP * 20 + 1.0)
    from app.game import supply as _sup
    want = [_sup.talk_option(1), _sup.talk_option(2)]
    ck("選項照順序送出（第1項→第2項）", tab.sent == want,
       f"送出 {tab.sent}，應該是 {want}")
    ck("送完 → 前進下一步", tab._i == 1)
    ck("★ 送完會送「離開互動」（不送伺服器會以為還在講話）",
       len(tab.left) >= 1, str(tab.left))

    # ★ 2026-09-03 使用者：「把間隔直接刪掉、預設 0.5、不跟使用者說」——
    #   腳本裡舊的 "gap" 一律忽略，節奏固定 dungeon_tab.MENU_GAP。
    slow = {"do": "interact", "at": [20, 20], "model": 60307,
            "menu": [1, 2], "gap": 5.0}
    tab = make_tab([slow], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    run(tab, 0.3)
    ck("點下去了", tab._clicked)
    run(tab, dt.MENU_GAP * 2 + 0.3)
    ck(f"★ 腳本寫 gap=5 也**忽略**：{dt.MENU_GAP} 秒節奏就送第一項",
       len(tab.sent) >= 1, str(tab.sent))
    ck(f"　MENU_GAP 固定 0.5（使用者定）", dt.MENU_GAP == 0.5, str(dt.MENU_GAP))

    # ★★ 使用者 2026-09-02 定案：**不比外觀** —— 機關被啟動過外觀會換
    #   （實測 60335 廢棄機器人2 → 60301 門開關火，同一格同一個東西）。
    tab = make_tab([step], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60999)])   # 外觀跟腳本不同
    run(tab, 0.3)
    ck("★★ 外觀跟腳本記的不一樣 → 照樣點得到（只認位置）", tab._clicked,
       tab.status.text())
    # ⚠ 但看不見的場景標記點（TAG）要排掉 —— 點它們沒有意義
    tab = make_tab([step], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60005)])   # TAG01（HIDE）
    run(tab, 0.3)
    ck("★ 只掃到看不見的標記點（TAG）→ 大聲停下，⛔ 不點它",
       not tab.run_cb.isChecked() and not tab._clicked, tab.status.text())

    tab = make_tab([step], pos=(20.0, 20.0), props=[])
    dt.scenery.nearby = lambda *a, **k: None              # 讀不到
    run(tab, 0.5)
    ck("★ 物件清單讀不到 ≠ 沒有 → 繼續重試，不停機也不亂點",
       tab.run_cb.isChecked() and not tab._clicked, tab.status.text())

    # ★★ 使用者 2026-09-02：「不會有『幾秒沒到就壞掉』，那個拔掉」
    print("\n卡住：一直試，沒有逾時這種東西")
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    run(tab, 200.0)
    ck("★★ 一步卡了 200 秒 → **照樣還在跑**（沒有逾時）",
       tab.run_cb.isChecked(), tab.status.text())
    ck("　也沒有跳通知這種東西（_warn 已經整支拿掉）",
       not hasattr(tab, "_warn"))

    print("\n收工判定（使用者定：腳本跑完 ＋ 周圍沒怪）")
    # ⚠ 先在沒怪的情況下把腳本跑完（有怪的話「休息」根本不會開始數，
    #   見上面「休息要確認周圍沒有可以打到的怪物」那一條）。
    tab = make_tab([{"do": "wait", "secs": 0.2}])
    run(tab, 1.0)
    ck("腳本跑完了", tab._i >= 1)
    tab._live_monsters = lambda: [FakeMon()]
    run(tab, dt.CLEAR_SETTLE + 1)
    ck("★ 還有怪 → 不算結束", tab.run_cb.isChecked() and not tab._done)
    tab._live_monsters = lambda: []
    run(tab, dt.CLEAR_SETTLE + 0.5)
    ck("★ 怪清光又沉澱夠久 → 這一趟結束", tab._done,
       tab.status.text())
    ck("　收工會停手", not tab.run_cb.isChecked())

    # ★★ 使用者 2026-09-02：「點位放在傳點上會不會永遠到不了？」→ 會，
    #   所以傳點是獨立一種步驟；而且完成訊號是**順移**不是換地圖
    #   （「人被傳走不會換地圖，有順移就算吧，有時候傳點之間也很短」）。
    print("\n傳點步驟")
    tab = make_tab([{"do": "portal", "to": [50, 50]}, {"do": "clear"}])
    run(tab, 0.5)
    ck("★ 站到傳點上**不算**完成（會被搬走，那一格不會到達）", tab._i == 0)
    ck("　還沒到就一直往傳點走", tab._nav.goal == (50, 50), str(tab._nav.goal))
    tab._pos = [50.2, 50.2]
    run(tab, 0.3)
    ck("★ 就算站上去了也還是不算完成", tab._i == 0, str(tab._i))
    tab._jumped = (50.2, 50.2)          # 從傳點上跳走（跳之前站的位置）
    run(tab, 0.1)
    ck("★ 順移了 → 這一步完成", tab._i == 1)

    # ★★★ 走對話（使用者 2026-09-02：「只要沒選項就幫我對話到結束或出現
    #   選項」）：腳本只記選項，沒有選項的頁自己按確定過掉。
    from app.game import supply as _sup2
    # 無異議 → 選項(1,2) → 無異議 → 結束；腳本只記 [1]
    fake = FakeTalk([(), (1, 2), (), ()])
    talk_step = {"do": "interact", "at": [20, 20], "model": 60307,
                 "menu": [1], "gap": 0.2}
    tab = make_tab([talk_step], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    run(tab, 8.0)
    ck("★★ 沒有選項的頁自己按確定過掉", fake.closes >= 2, str(fake.closes))
    ck("★★ 有選項才照腳本送（腳本只記了第 1 項）",
       tab.sent == [_sup2.talk_option(1)], str(tab.sent))
    ck("　對話走完 → 送離開互動、前進", tab.left and tab._i == 1,
       f"{tab.left} 步{tab._i}")

    # 整段都沒有選項 → 一路按到底
    fake = FakeTalk([(), (), ()])
    pure = {"do": "interact", "at": [20, 20], "model": 60307, "menu": [],
            "gap": 0.2}
    tab = make_tab([pure], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    run(tab, 8.0)
    ck("★ 純對話：一路按確定到結束", fake.closes >= 2 and tab.sent == [],
       f"按{fake.closes} 送{tab.sent}")
    ck("　然後才離開互動", tab.left and tab._i == 1, f"{tab.left} 步{tab._i}")

    # 舊腳本記了 0（過場）→ 忽略，不會多按
    fake = FakeTalk([(), (1, 2), ()])
    oldstyle = {"do": "interact", "at": [20, 20], "model": 60307,
                "menu": [0, 1, 0], "gap": 0.2}
    tab = make_tab([oldstyle], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    run(tab, 8.0)
    ck("★ 舊腳本的 0（過場）忽略掉，選項照送不重複",
       tab.sent == [_sup2.talk_option(1)], str(tab.sent))

    # ⛔ 跳出選項但腳本沒記 → 絕不亂選
    fake = FakeTalk([(1, 2, 3)])
    tab = make_tab([pure], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    run(tab, 2.0)
    ck("★★ 跳出選項但腳本沒說要選哪一項 → 大聲停下（⛔ 絕不亂選）",
       not tab.run_cb.isChecked() and tab.sent == [], tab.status.text())

    # ⛔ 腳本要選的項目這一頁沒有 → 停下
    fake = FakeTalk([(1, 2)])
    want3 = {"do": "interact", "at": [20, 20], "model": 60307, "menu": [3],
             "gap": 0.2}
    tab = make_tab([want3], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    run(tab, 2.0)
    ck("★ 腳本要第 3 項但這一頁只有 1、2 → 停下來",
       not tab.run_cb.isChecked(), tab.status.text())

    # 對話提早結束、選項還沒送完 → 停下
    fake = FakeTalk([()])
    tab = make_tab([talk_step], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    dt.talkwnd.close_page = fake.close_gone   # 按了確定 → 視窗直接不見
    run(tab, 8.0)
    ck("★ 對話走完了選項卻還沒送到 → 停下來",
       not tab.run_cb.isChecked(), tab.status.text())

    # ★★★ 「到底有沒有對話視窗」＝硬訊號（使用者 2026-09-02：
    #   「對話後關視窗太慢了，不知道在等啥，請要明確知道有沒有視窗」）
    # ★★★ 2026-09-03 確定＝messageclose＋destroy 之後：**不是最後一頁**時視窗會
    #   先不見、下一頁稍後才重建 —— 這段空窗不可以當「對話走完」（會誤停）。
    print("\n按完確定視窗暫時不見（不是最後一頁）→ 要等下一頁，不能誤判走完")
    talk_step2 = {"do": "interact", "at": [20, 20], "model": 60307,
                  "menu": [1], "gap": 0.2}
    fake = FakeTalk([(), (1,)])                 # 過場頁 → 有選項的頁
    fake.ended = False                          # 過場頁不是最後一頁
    tab = make_tab([talk_step2], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    gone = {"n": 0}

    def close_then_reopen(_mv, _sc):
        """按確定 → 視窗先不見（destroy），過幾拍伺服器才送下一頁。"""
        fake.closes += 1
        fake.i = -2                             # -2 ＝ 視窗不見、但下一頁還沒來
        gone["n"] = 0
        return True, "送出"
    real_wo = fake.window_open

    def wo(_mv, _sc):
        if fake.i == -2:
            gone["n"] += 1
            if gone["n"] >= 3:                  # 「一趟來回」之後下一頁到了
                fake.i = 1
            return False
        return real_wo(_mv, _sc)
    dt.talkwnd.close_page = close_then_reopen
    dt.talkwnd.window_open = wo
    run(tab, dt.MENU_GAP * 12)
    ck("★ 視窗暫時不見時**沒有**誤判成走完（分頁沒停）", tab.run_cb.isChecked(),
       tab.status.text())
    ck("★ 下一頁到了照樣把第 1 項送到", 10 in tab.sent, f"送出 {tab.sent}")

    print("\n有沒有對話視窗（硬訊號）")
    fake = FakeTalk([()])                       # 一頁沒有選項的對話
    one = {"do": "interact", "at": [20, 20], "model": 60307, "menu": [],
           "gap": 0.2}
    tab = make_tab([one, {"do": "wait", "secs": 9}], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    run(tab, 0.2)
    ck("　點下去了", tab._clicked and fake.i == 0)
    run(tab, dt.MENU_GAP + 0.2)
    ck("★★ 視窗確定開著 → **馬上**按確定（不必等它「穩定」）",
       fake.closes >= 1, f"按了{fake.closes}次")
    run(tab, dt.MENU_GAP + 0.2)
    ck("★★★ 視窗不見了 → **立刻**收工，不用等 TALK_SETTLE",
       tab._i == 1, f"還在第{tab._i + 1}步：{tab.status.text()}")
    ck("　收工前會送「離開互動」", len(tab.left) >= 1, str(tab.left))

    # ⛔ 視窗還在 ＝ 對話**還沒**結束（就算那些全域一直沒變）
    fake = FakeTalk([(), ()])
    fake.i_lock = True                          # 按了確定伺服器也不翻頁
    tab = make_tab([one, {"do": "wait", "secs": 9}], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    run(tab, dt.TALK_SETTLE * 0.2 + dt.CLOSE_RETRY + 1.0)
    ck("⛔ 視窗還在 → 不可以當成「對話結束」", tab._i == 0,
       f"跑到第{tab._i + 1}步")
    ck("★ 沒反應會補送確定（不是乾等）", fake.closes >= 2,
       f"按了{fake.closes}次")

    # ⚠ 問不到有沒有視窗（叫不動／讀不到）→ 退回舊的「簽章有沒有變」判斷
    fake = FakeTalk([(1,), ()])
    fake.unknown = True
    tab = make_tab([{"do": "interact", "at": [20, 20], "model": 60307,
                     "menu": [1], "gap": 0.2},
                    {"do": "wait", "secs": 9}], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    run(tab, 8.0)
    ck("⚠ 問不到視窗狀態 → 照舊走得完（不是壞掉）",
       tab.sent == [_sup2.talk_option(1)] and tab._i == 1,
       f"送出{tab.sent} 第{tab._i + 1}步 {tab.status.text()}")

    # ★★ 傳點站上去沒被搬走 → 每 PORTAL_POKE 秒補送一次，撐 PORTAL_TIMEOUT
    #   （使用者 2026-09-02：「在傳送點每 5 秒送一次，3 分鐘就結束跳通知」）
    tab = make_tab([{"do": "portal", "to": [50, 50], "model": 60123}],
                   pos=(50.0, 50.0))
    tab.trigs = [FakeTrig(50.0, 50.0, 60123)]
    poked = tab.portal_sent
    run(tab, 0.3)
    ck("★ 站在傳點上 → 立刻打第一發 0x0D", poked == [60123], str(poked))
    run(tab, dt.PORTAL_POKE - 1.0)
    ck(f"　{dt.PORTAL_POKE:.0f} 秒還沒到不重送（⛔ 不是每拍狂送）",
       len(poked) == 1, str(poked))
    run(tab, 1.5)
    ck(f"★ 過了 {dt.PORTAL_POKE:.0f} 秒才送第二次", len(poked) == 2, str(poked))
    tab._step_t = 600.0                    # 站了十分鐘
    run(tab, 0.2)
    ck("★★ 傳點站了十分鐘 → 一樣**不停機**（沒有逾時、沒有通知）",
       tab.run_cb.isChecked(), tab.status.text())
    # 那一格附近掃不到傳點 → 只回報，⛔ 不亂送
    tab = make_tab([{"do": "portal", "to": [50, 50], "model": 60123}],
                   pos=(50.0, 50.0))
    tab.trigs = [FakeTrig(56.0, 50.0, 69999)]      # 離記的位置 6 格（超出容忍）
    run(tab, 2.0)
    ck("★ 那一格附近掃不到傳點 → ⛔ 不亂送", tab.portal_sent == [],
       str(tab.portal_sent))
    tab = make_tab([{"do": "portal", "to": [50, 50], "model": 60123}],
                   pos=(50.0, 50.0))
    tab.trigs = [FakeTrig(50.2, 50.1, 69999)]      # 同一格、外觀變了
    run(tab, 0.3)
    ck("★★ 傳點外觀變了但位置對得上 → 照樣打得到（只認位置）",
       tab.portal_sent == [69999], str(tab.portal_sent))

    # 出口對不對得上（腳本記了 land 就要驗）
    tab = make_tab([{"do": "portal", "to": [50, 50], "land": [200, 40]}])
    tab._pos = [201.0, 41.0]
    tab._jumped = (50.0, 50.5)
    run(tab, 0.1)
    ck("★ 傳到腳本記的出口 → 過", tab._i == 1, tab.status.text())
    tab = make_tab([{"do": "portal", "to": [50, 50], "land": [200, 40]}])
    tab._pos = [12.0, 300.0]
    tab._jumped = (50.0, 50.5)
    run(tab, 0.1)
    ck("★ 傳到別的地方 → 大聲停下（不拿別處的座標繼續跑）",
       not tab.run_cb.isChecked(), tab.status.text())

    # ★★ 2026-09-05 使用者實機（無限塔第 41／52 步）：人還在 10 多格外走過去，
    #   伺服器把位置拉回幾格（一拍跳 ≥3 格）→ 舊版當成「傳點把人送到別的地方」
    #   大聲停下，其實根本沒踩到傳點。→ 跳之前不在傳點上＝不算傳送，繼續走。
    tab = make_tab([{"do": "portal", "to": [141.3, 277.6],
                     "land": [47.5, 278.5]}], pos=(128.0, 270.0))
    tab._jumped = (134.0, 270.0)              # 跳之前離傳點 10 格
    run(tab, 0.1)
    ck("★★ 不在傳點上就跳了（伺服器拉回）→ 不停、不算完成",
       tab.run_cb.isChecked() and tab._i == 0, tab.status.text())
    ck("　照樣往傳點走", tab._nav.goal == (141.3, 277.6), str(tab._nav.goal))
    ck("　狀態列講得出「不算傳送」", "不算傳送" in tab.status.text(),
       tab.status.text())
    tab = make_tab([{"do": "portal", "to": [50, 50]}], pos=(30.0, 30.0))
    tab._jumped = (36.0, 30.0)
    run(tab, 0.1)
    ck("★ 沒記出口的舊腳本：不在傳點上跳了一樣不算",
       tab._i == 0 and tab.run_cb.isChecked(), tab.status.text())
    tab = make_tab([{"do": "portal", "to": [50, 50], "land": [200, 40]}],
                   pos=(201.0, 41.0))
    tab._jumped = (43.0, 50.0)                # 離傳點 7 格（比 PORTAL_FROM 遠）
    run(tab, 0.1)
    ck("★ 跳之前離傳點 7 格但落在記的出口 → 照樣算過（觸發範圍比記的寬）",
       tab._i == 1, tab.status.text())
    tab = make_tab([{"do": "portal", "to": [50, 50]}], pos=(80.0, 50.0))
    tab._jumped = (52.0, 51.0)                # 站在傳點上跳走、沒記出口
    run(tab, 0.1)
    ck("★ 從傳點上跳走、腳本沒記出口 → 算過", tab._i == 1, tab.status.text())

    # ★★ 2026-09-05 無限塔第 54 步「剩 3.5 格　走完這條路線 → 重算收尾」原地不動：
    #   目標那格地形圖說不可走、最短路的終點被放寬到 3 格外、人站在那裡
    #   → 尋路器舉 exhausted → 剩下那段要**直走**（walk_exact），不是站著等重算。
    tab = make_tab([{"do": "walk", "to": [44, 263]}], pos=(47.5, 264.0))
    walked_x = []
    tab._mover = type("M", (), {
        "walk_exact": lambda _s, _sc, _p, x, y: walked_x.append((x, y)),
    })()
    dt.entity.is_walking = lambda _sc, _p: False
    tab._nav.exhausted = True
    run(tab, 0.3)
    ck("★★ 尋路器說最短路只能到這（exhausted）→ 直走到點位（walk_exact）",
       bool(walked_x) and walked_x[0] == (44, 263), str(walked_x))
    ck("　沒被當成走不到（不進 _blocked）", "沒有路" not in tab.status.text(),
       tab.status.text())
    ck("　還在跑、步驟沒跳", tab.run_cb.isChecked() and tab._i == 0)

    # 順移偵測本身：速度分得出「走的」跟「傳的」
    tab = make_tab([{"do": "clear"}])
    tab._pos_prev, tab._pos_t = (10.0, 10.0), time.monotonic()
    ck("★ 走路一拍動 0.5 格 → 不是順移",
       not tab._check_jump((10.5, 10.0)))
    tab._pos_prev, tab._pos_t = (10.0, 10.0), time.monotonic()
    ck("★ 一拍跳 40 格 → 是順移，而且回的是跳之前站的位置",
       tab._check_jump((50.0, 10.0)) == (10.0, 10.0))
    tab._pos_prev, tab._pos_t = (10.0, 10.0), time.monotonic() - 5.0
    ck("★ 兩次取樣隔 5 秒 → 不判（可能是走過去的），只重設基準",
       not tab._check_jump((50.0, 10.0)))
    tab._pos_prev = None
    ck("★ 第一拍沒有基準 → 不判", not tab._check_jump((50.0, 10.0)))

    # ★★ 使用者 2026-09-02：「副本很容易掃到另一個地區的怪物無法到達」
    #   「到點位不要跟自動戰鬥互卡，在殺怪物就不要跑點位，都沒怪到點位才算到」
    print("\n打得到的怪才算數（副本是好幾塊互不相通的地方拼起來的）")
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    # 我這一區＝x 8~10；牆在 x=11；隔壁區＝x 12~14（薄牆，只隔一格）
    mine = {(x, 10) for x in (8, 9, 10)}
    nextdoor = {(x, 10) for x in (12, 13, 14)}
    tab._reach = set(mine)
    tab._grid = FakeGrid(mine, nextdoor)
    near = FakeMon(x=9.0, y=10.0, eid=1, name="同一區")
    far = FakeMon(x=35.0, y=10.0, eid=2, name="很遠的隔壁區")
    # ★★ 使用者 2026-09-02 特別點名的坑：隔一牆但距離只有 2 格
    wall = FakeMon(x=12.0, y=10.0, eid=3, name="隔一牆只有2格")
    tab._live_monsters = lambda: [near, far, wall]
    got = [m.name for m in tab._targets()]
    ck("★★ 隔一牆但只有 2 格的怪 → 排除（⛔ 不可以再看它旁邊那一圈）",
       got == ["同一區"], str(got))
    onwall = FakeMon(x=11.0, y=10.0, eid=4, name="站在牆上")
    tab._live_monsters = lambda: [onwall]
    ck("　怪站在不可走格上（貼牆／門框）→ 看旁邊一圈，還是打得到",
       [m.name for m in tab._targets()] == ["站在牆上"])
    tab._reach, tab._grid = None, None                   # 讀不到地形圖
    tab._live_monsters = lambda: [near, far, wall]
    ck("★ 讀不到地形圖 → 不過濾（安全退化，寧可多打也不要不出手）",
       len(tab._targets()) == 3)

    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    tab._reach = {(10, 10)}
    tab._keys, tab._atk = FakeKeys(), FakeAtk()
    tab._live_monsters = lambda: [FakeMon(x=300.0, y=200.0, eid=9)]
    ck("★ 只剩打不到的怪 → 不算在打怪，腳本照跑",
       not tab._fight((10.0, 10.0), TICK))
    run(tab, 0.3)
    ck("　腳本真的動了（尋路往點位走）", tab._nav.goal == (50, 50),
       str(tab._nav.goal))

    # ⛔⛔ 使用者兩次明令（9/2、9/4）：不要黑名單、不要冷卻 —— 放棄就換一隻，
    #   沒別隻就再問同一隻。
    print("\n⛔ 沒有黑名單、沒有冷卻：放棄就換一隻，沒別隻就再問同一隻（2026-09-04）")
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    m = FakeMon(x=11.0, y=10.0, eid=7, name="打不到的")
    tab._live_monsters = lambda: [m]
    tab._keys, tab._atk = FakeKeys(), FakeAtk()
    tab._cur = m
    tab._grid_t = 5.0
    tab._give_up("測試")
    ck("★★ 放棄之後只剩它一隻 → **馬上再挑它**（門可能開了；⛔ 沒有冷卻）",
       tab._cur is m and tab._atk.picked is m and tab._last_gave_up == 7,
       f"cur={tab._cur} last={tab._last_gave_up}")
    ck("　放棄會立刻重問一次地形（門開了才跟得上）", tab._grid_t == 0.0)
    ck("　沒有任何冷卻／黑名單欄位", not hasattr(tab, "_gave_up")
       and not hasattr(tab, "_toofar") and not hasattr(tab, "_killed"))
    other = FakeMon(x=12.0, y=10.0, eid=8, name="另一隻")
    tab._live_monsters = lambda: [m, other]
    tab._cur = m
    tab._give_up("測試2")
    ck("★ 有別隻的時候先挑別隻（不是黑名單，只是先換一隻）",
       tab._cur is other and tab._atk.picked is other,
       tab._atk.picked.name if tab._atk.picked else "沒挑")

    # ★★★ 使用者 2026-09-04：「超過 30 格要跳過」
    print("\n超過 30 格的怪整個不看（2026-09-04）")
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    far = FakeMon(x=45.0, y=10.0, eid=91, name="直線35格")
    ok_m = FakeMon(x=14.0, y=10.0, eid=93, name="正常")
    tab._live_monsters = lambda: [far, ok_m]
    tab._reach = None
    ck(f"★ 直線超過 {dt.MAX_CHASE:.0f} 格 → 不在目標裡",
       [m.eid for m in tab._targets()] == [93], str([m.eid for m in tab._targets()]))
    ck("　狀態列的盤點講得出「幾隻超過 30 格」",
       f"1 隻超過 {dt.MAX_CHASE:.0f} 格" in tab._mon_note(), tab._mon_note())
    tab._keys, tab._atk = FakeKeys(), FakeAtk()
    tab._live_monsters = lambda: [far]
    ck("★ 只剩超過 30 格的 → 不追、跑腳本", not tab._fight((10.0, 10.0), TICK))
    ck("　訊息講得出原因", "超過 30 格" in tab.status.text(), tab.status.text())

    # ★★★ 掛機那套：「近」＝我們自己 A* 算的路徑長度，不是直線
    print("\n挑目標＝路徑最短（掛機頁的複本）")

    class FakeGridPath:                        # ⚠ 別叫 FakeGrid：main() 後面有同名的
        costs = {13: 40.0, 18: 8.0}            # 目標 x → 路徑長度（其餘＝直線）

        def clear_line(self, a, b):
            return True

        def waypoints(self, a, b, relax=4, max_cost=None):
            return [b]

        def route(self, start, goal, relax=4, max_cost=None):
            n = self.costs.get(int(goal[0]), abs(goal[0] - start[0]))
            if max_cost is not None and n > max_cost:
                return None
            return [(start[0] + i, start[1]) for i in range(int(n) + 1)]

    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    around = FakeMon(x=13.0, y=10.0, eid=92, name="隔牆繞40格")   # 直線最近、但要繞
    ok_m = FakeMon(x=18.0, y=10.0, eid=93, name="正常")
    tab._live_monsters = lambda: [around, ok_m]
    tab._reach = None
    tab._grid = FakeGridPath()
    tab._keys, tab._atk, tab._mover = FakeKeys(), FakeAtk(), FakeMover()
    dt.entity.read_pos = lambda _sc, _addr: None
    tab._fight((10.0, 10.0), TICK)
    ck("★★ 直線 3 格但要繞 40 格 vs 直線 8 格路徑 8 格 → 挑後者",
       tab._atk.picked is ok_m, tab._atk.picked and tab._atk.picked.name)
    closer = FakeMon(x=11.0, y=10.0, eid=94, name="真的更近")
    tab._live_monsters = lambda: [around, ok_m, closer]
    dt.entity.read_pos = lambda _sc, _addr: (18.0, 10.0)
    tab._fight((10.0, 10.0), TICK)
    ck("★ 趕路途中冒出路徑明顯更短的 → 改打牠", tab._cur is closer,
       tab._cur and tab._cur.name)
    tab._hurt = True
    nearest = FakeMon(x=10.5, y=10.0, eid=95, name="更更近")
    tab._live_monsters = lambda: [around, ok_m, closer, nearest]
    dt.entity.read_pos = lambda _sc, _addr: (11.0, 10.0)
    tab._switch_t = 0.0
    tab._fight((10.0, 10.0), TICK)
    ck("　⚠ 打傷過的絕不換", tab._cur is closer, tab._cur and tab._cur.name)
    dt.entity.read_pos = lambda _sc, _addr: None

    print("\n怪跟我之間有障礙物 → 沿繞路點貼臉、邊走邊打（掛機頁的複本）")

    class FakeGridWall:
        def clear_line(self, a, b):
            return False

        def waypoints(self, a, b, relax=4, max_cost=None):
            return [(12, 12), (int(b[0]), int(b[1]))]

        def route(self, start, goal, relax=4, max_cost=None):
            return [start, goal]

    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    m = FakeMon(x=14.0, y=10.0, eid=62, name="牆後面")
    tab._live_monsters = lambda: [m]
    tab._keys, tab._atk, tab._mover = FakeKeys(), FakeAtk(), FakeMover()
    tab._keys.min_range = 12                              # 遠程：4 格本來打得到
    tab._reach, tab._grid = None, FakeGridWall()
    dt.entity.read_pos = lambda _sc, _addr: (14.0, 10.0)
    for _ in range(6):
        tab._fight((10.0, 10.0), TICK)
    ck("★★ 隔著地形 → 走 A* 繞路點、停留距離壓到 2 格（貼臉）",
       tab._mover.routes and tab._mover.routes[-1][2] == dt.MELEE_RANGE
       and len(tab._mover.routes[-1][3]) == 2,
       str(tab._mover.routes))
    ck("★★ 邊走邊打（隔地形不擋攻擊，打不打得到只有怪的血知道）", tab._keys.on is True)
    ck("　狀態列講得出「隔著地形」", "隔著地形" in tab.status.text(), tab.status.text())

    class FakeGridOpen:
        def clear_line(self, a, b):
            return True

        def waypoints(self, a, b, relax=4, max_cost=None):
            return [b]

        def route(self, start, goal, relax=4, max_cost=None):
            return [start, goal]

    # 直線沒牆、只差最後那一兩格 → walk_near 直走，不尋路（近戰 2.5 格那個坑）
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    m = FakeMon(x=12.5, y=10.0, eid=61, name="站在2.5格的弓手")
    tab._live_monsters = lambda: [m]
    tab._keys, tab._atk, tab._mover = FakeKeys(), FakeAtk(), FakeMover()
    tab._keys.min_range = 1                               # 近戰：打得到 2 格
    tab._reach, tab._grid = None, FakeGridOpen()
    dt.entity.read_pos = lambda _sc, _addr: (12.5, 10.0)
    for _ in range(6):
        tab._fight((10.0, 10.0), TICK)
    ck("★★ 近戰離怪 2.5 格（直線沒牆）→ walk_near 直走過去，不尋路",
       tab._mover.near and not tab._mover.routes and tab._keys.on is False,
       f"near={tab._mover.near} routes={tab._mover.routes} on={tab._keys.on}")
    ck("　停在比攻擊距離短一點的地方（走進去才打得到）",
       tab._mover.near and tab._mover.near[-1][2] < 2.0, str(tab._mover.near))
    dt.entity.read_pos = lambda _sc, _addr: (11.4, 10.0)
    tab._fight((10.0, 10.0), TICK)
    ck("　走進射程 → 出手", tab._keys.on is True)

    # 真訊號兜底：站在射程內 3 秒零傷害 → 貼身繞打（邊走邊打）、掉血解除
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    m = FakeMon(x=16.0, y=10.0, eid=63, name="欄杆後面")
    tab._live_monsters = lambda: [m]
    tab._keys, tab._atk, tab._mover = FakeKeys(), FakeAtk(), FakeMover()
    tab._keys.min_range = 12
    tab._keys.selected = True
    tab._atk.hp = 100
    tab._reach, tab._grid = None, FakeGridOpen()
    dt.entity.read_pos = lambda _sc, _addr: (16.0, 10.0)
    for _ in range(int(2.0 / TICK)):
        tab._fight((10.0, 10.0), TICK)
    ck("站 2 秒零傷害 → 還在出手（沒到門檻）",
       tab._keys.on is True and not tab._push_in and not tab._mover.routes)
    for _ in range(int(1.5 / TICK)):
        tab._fight((10.0, 10.0), TICK)
    ck(f"★★ 站 {dt.PUSH_IN_SECS:.0f} 秒血一滴不掉 → 貼身繞打（停 {dt.MELEE_RANGE:g} 格）",
       tab._push_in and tab._mover.routes and tab._mover.routes[-1][2] == dt.MELEE_RANGE,
       f"push={tab._push_in} routes={tab._mover.routes}")
    ck("　邊走邊打、⛔ 沒有換怪", tab._keys.on is True and tab._cur is m)
    ck("　原因看得到", "零傷害" in tab.status.text(), tab.status.text())
    tab._atk.hp = 90                                      # 傷害進來了
    tab._fight((10.0, 10.0), TICK)
    ck("★ 一掉血就解除貼身、記成打傷過", not tab._push_in and tab._hurt)

    # 沒進展 15 秒 → 換一隻；只剩它就再挑它（⛔ 沒有冷卻）
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    m = FakeMon(x=16.0, y=10.0, eid=64, name="打不中的")
    tab._live_monsters = lambda: [m]
    tab._keys, tab._atk, tab._mover = FakeKeys(), FakeAtk(), FakeMover()
    tab._keys.min_range = 12
    tab._keys.selected = True
    tab._atk.hp = 100
    tab._reach, tab._grid = None, FakeGridOpen()
    dt.entity.read_pos = lambda _sc, _addr: (16.0, 10.0)
    for _ in range(int((dt.STUCK_ENGAGED + 0.5) / TICK)):
        tab._fight((10.0, 10.0), TICK)
    ck(f"★ 交戰中 {dt.STUCK_ENGAGED:.0f} 秒沒進展 → 換一隻；只剩它 → 馬上再挑它",
       tab._last_gave_up == 64 and tab._cur is m and tab._stuck < 1.0,
       f"last={tab._last_gave_up} cur={tab._cur} stuck={tab._stuck}")
    ck("　原因看得到", "沒進展" in tab.status.text(), tab.status.text())
    other = FakeMon(x=20.0, y=10.0, eid=65, name="另一隻")
    tab._live_monsters = lambda: [m, other]
    tab._last_gave_up = None
    for _ in range(int((dt.STUCK_ENGAGED + 0.5) / TICK)):
        tab._fight((10.0, 10.0), TICK)
    ck("★ 有別隻 → 換成別隻", tab._cur is other, tab._cur and tab._cur.name)

    # 地形圖連續算不出路（怪走進走不到的角落）→ 換一隻；沒打傷過才算
    class FakeGridFlaky(FakeGridOpen):
        nopath = False

        def clear_line(self, a, b):
            return not self.nopath

        def waypoints(self, a, b, relax=4, max_cost=None):
            return None if self.nopath else [b]

    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    m = FakeMon(x=18.0, y=10.0, eid=66, name="走進角落的")
    tab._live_monsters = lambda: [m]
    tab._keys, tab._atk, tab._mover = FakeKeys(), FakeAtk(), FakeMover()
    tab._keys.min_range = 3
    g = FakeGridFlaky()
    tab._reach, tab._grid = None, g
    dt.entity.read_pos = lambda _sc, _addr: (18.0, 10.0)
    tab._fight((10.0, 10.0), TICK)
    g.nopath = True
    for _ in range(int(1.0 / TICK)):
        tab._fight((10.0, 10.0), TICK)
    ck(f"★ 尋路連續 {dt.UNREACH_HITS} 次算不出 → 換一隻（沒別隻就再問它）",
       tab._last_gave_up == 66 and "走不到" in tab.status.text(), tab.status.text())

    # 目標從掃描消失、物件也沒了 → 兩拍後放掉（一拍漏掃不算）
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    m = FakeMon(x=12.0, y=10.0, eid=67, name="消失的")
    tab._live_monsters = lambda: [m]
    tab._keys, tab._atk, tab._mover = FakeKeys(), FakeAtk(), FakeMover()
    tab._reach, tab._grid = None, FakeGridOpen()
    dt.entity.read_pos = lambda _sc, _addr: (12.0, 10.0)
    tab._fight((10.0, 10.0), TICK)
    tab._live_monsters = lambda: []
    dt.entity.read_live_hp = lambda _sc, e: (False, "", None, -1)
    tab._fight((10.0, 10.0), TICK)
    ck("掃描少一拍 → 還不放（可能只是漏掃）", tab._cur is m)
    tab._fight((10.0, 10.0), TICK)
    ck("★ 連續兩拍不在、物件也沒了 → 放掉", tab._cur is None)
    dt.entity.read_live_hp = lambda _sc, e: (True, "", (e.x, e.y), -1)
    dt.entity.read_pos = lambda _sc, _addr: None

    # ★★ 血量歸零＝打死了（使用者 2026-09-05：副本裡的柱子死掉屍體會留一段時間、
    #   動畫狀態不變 'Dead'，只看狀態會一直對屍體出手）。血量 −1＝沒交戰，當活的。
    print("血量歸零＝打死了")
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    m = FakeMon(x=12.0, y=10.0, eid=68, name="柱子")
    tab._live_monsters = lambda: [m]
    tab._keys, tab._atk, tab._mover = FakeKeys(), FakeAtk(), FakeMover()
    tab._reach, tab._grid = None, FakeGridOpen()
    dt.entity.read_pos = lambda _sc, _addr: (12.0, 10.0)
    tab._fight((10.0, 10.0), TICK)
    ck("鎖定柱子", tab._cur is m)
    dt.entity.read_live_hp = lambda _sc, e: (True, "Wait", (e.x, e.y), 35)
    tab._fight((10.0, 10.0), TICK)
    ck("　血量 35%、狀態 Wait → 照打", tab._cur is m)
    dt.entity.read_live_hp = lambda _sc, e: (True, "Wait", (e.x, e.y), 0)
    tab._fight((10.0, 10.0), TICK)
    ck("★ 血量 0、狀態還是 Wait（屍體殘留）→ 立刻放掉",
       tab._cur is None and "血量歸零" in tab.status.text(), tab.status.text())
    tab._fight((10.0, 10.0), TICK)
    ck("★ 屍體不會再被挑到（血量 0 的候選直接跳過）", tab._cur is None)
    dt.entity.read_live_hp = lambda _sc, e: (True, "", (e.x, e.y), -1)
    tab._fight((10.0, 10.0), TICK)
    ck("　血量 −1（沒交戰）→ 當活的、照挑", tab._cur is m)
    dt.entity.read_pos = lambda _sc, _addr: None
    # 掃描快照那一層（Entity.hp_zero）也要濾
    E = dt.entity.Entity
    tab._last = type("S", (), {})()
    tab._last.mons = [
        E(0x3000, 1, 5, "活的", 12.0, 10.0, kind=4, state="Wait", hp=-1),
        E(0x3100, 2, 5, "柱子屍體", 12.0, 10.0, kind=4, state="Wait", hp=0),
        E(0x3200, 3, 5, "一般屍體", 12.0, 10.0, kind=4, state="Dead", hp=0),
        E(0x3300, 4, 5, "沒讀到血", 12.0, 10.0, kind=4, state="", hp=-1)]
    live = [x.eid for x in dt.DungeonTab._live_monsters(tab)]
    ck("★ 掃描快照：血量 0 與 'Dead' 都當屍體濾掉、−1 留著", live == [1, 4], str(live))

    # ★★ 通知（使用者 2026-09-05：「死掉或出問題也要通知，跟自動掛機一樣」）
    print("通知")
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    fake = tab._notifier
    tab._started = True
    tab._stop("⛔ 地圖變了，停下來")
    ck("★ 開跑後因為出問題停機 → 通知", len(fake.fired) == 1 and "地圖變了" in fake.fired[0][1],
       str(fake.fired))
    ck("　通知後 run_cb 是關的、狀態列有寫", not tab.run_cb.isChecked()
       and "地圖變了" in tab.status.text())
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    fake = tab._notifier
    tab._started = True
    tab._stop("已停止")
    ck("　使用者自己按停 → 不通知", fake.fired == [])
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    fake = tab._notifier
    tab._stop("⚠ 腳本讀不進來：壞掉")           # _started 還是 False
    ck("　開跑前的檢查沒過 → 不通知（人就在電腦前）", fake.fired == [])
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    fake = tab._notifier
    tab._started = True
    tab.notify_cb.blockSignals(True)          # ⚠ 別讓測試寫進使用者的 config
    tab.notify_cb.setChecked(False)
    tab.notify_cb.blockSignals(False)
    tab._stop("⛔ 出事了")
    ck("　關掉「啟用通知」→ 不通知，但照樣停", fake.fired == [] and not tab.run_cb.isChecked())
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    fake = tab._notifier
    tab._started = True
    tab._stop("✔ 這一趟結束")
    ck("　正常跑完 → 不通知", fake.fired == [])

    # 角色死亡：HP ≤ 0 連續 DEATH_HITS 次 → 停機＋通知
    class St:
        def __init__(self, hp):
            self.hp = hp
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    fake = tab._notifier
    tab._started = True
    tab._stats = 0x5000
    dt.player.read = lambda _sc, _base: St(120)
    for _ in range(int(2.0 / TICK)):
        tab._check_death(TICK)
    ck("活著（HP 120）→ 不停", tab.run_cb.isChecked() and fake.fired == [])
    dt.player.read = lambda _sc, _base: None
    for _ in range(int(2.0 / TICK)):
        tab._check_death(TICK)
    ck("　讀不到（物件搬家）→ 不算死、基準丟掉重找", tab.run_cb.isChecked()
       and fake.fired == [] and tab._stats is None)
    tab._stats = 0x5000
    dt.player.read = lambda _sc, _base: St(0)
    tab._check_death(dt.DEATH_POLL)
    ck("　HP 0 只讀到一次 → 還不算", tab.run_cb.isChecked())
    dead = False
    for _ in range(int(2.0 / TICK)):
        dead = tab._check_death(TICK) or dead
    ck("★ HP 0 連續兩次 → 停機＋通知「角色死亡」", dead and not tab.run_cb.isChecked()
       and len(fake.fired) == 1 and "死亡" in fake.fired[0][1], str(fake.fired))
    dt.player.read = lambda _sc, _base: None

    # ⚠⚠ 交給 KeyWorker 的玩家位址不可以 +8（2026-09-02「完全不打怪物」的
    #   第二個病灶）：KeyWorker 拿它讀「我離目標多遠」，讀成 (0,0) 就每一招
    #   都判超出射程 → 完全不出手。
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    tab._keys, tab._atk = FakeKeys(), FakeAtk()
    tab._player = 0x2000
    tab._live_monsters = lambda: [FakeMon(x=12.0, y=10.0, eid=1)]
    dt.entity.read_pos = lambda _sc, _addr: (12.0, 10.0)
    tab._fight((10.0, 10.0), TICK)
    ck("★★ 給 KeyWorker 的玩家位址＝實體本體，⛔ 不可以再 +8",
       tab._keys.player == 0x2000, hex(tab._keys.player or 0))

    # ⛔ 看門狗那一組拿掉了（使用者 2026-09-02：不要「幾秒沒到就壞掉」）

    # 地形圖要定期重讀（機關開門會就地改掉可走格）
    tab = make_tab([{"do": "clear"}])
    tab._maps = FakeMaps()
    tab._grid_t = 0.0
    tab._refresh_grid((10.0, 10.0), TICK)
    ck("★ 時間到就重讀地形（門開了才跟得上）", tab._maps.drops == 1)
    tab._refresh_grid((10.0, 10.0), TICK)
    ck("　沒到時間不重讀（不要每拍讀一整張圖）", tab._maps.drops == 1)
    ck("　讀不到圖 → 可達集合是 None ＝不過濾", tab._reach is None)

    # ★★ 錨點落在碎片區 → 不可以拿它去把全部的怪判掉（「完全不理怪物」的
    #   第三個病灶）：站在不可走格上時要挑**最大**的鄰居區，而且太小就不篩。
    big = {(x, 10) for x in range(20, 60)}       # 40 格的正經區域
    tiny = {(5, 5), (5, 6), (6, 5)}              # 3 格碎片
    tab = make_tab([{"do": "clear"}])
    grid = FakeGrid(big | tiny)

    def _reach(x, y):                            # 兩塊各自獨立的連通區
        if (x, y) in big:
            return set(big)
        if (x, y) in tiny:
            return set(tiny)
        return None
    grid.reachable = _reach
    tab._maps = FakeMaps(grid)
    tab._grid_t = 0.0
    tab._refresh_grid((6.0, 6.0), TICK)          # 站在 (6,6)：不可走，鄰居有碎片
    ck("★ 錨在碎片區上 → 不篩選（⛔ 不可以把整張圖的怪都判成走不到）",
       tab._reach is None, str(tab._reach_n))
    ck("　而且會講出來", "看起來不對" in tab.status.text(), tab.status.text())
    tab._grid_t = 0.0
    tab._refresh_grid((20.5, 10.5), TICK)        # 站在大區裡
    ck("　站在正經區域裡就照常篩", tab._reach_n == len(big), str(tab._reach_n))

    # 站在不可走格、旁邊同時有碎片和大區 → 要挑大的那一塊
    tab = make_tab([{"do": "clear"}])
    cells = {(10, 10), (10, 11)} | {(12, y) for y in range(5, 40)}
    grid = FakeGrid(cells)
    small, large = {(10, 10), (10, 11)}, {(12, y) for y in range(5, 40)}

    def _reach2(x, y):
        if (x, y) in small:
            return set(small)
        if (x, y) in large:
            return set(large)
        return None
    grid.reachable = _reach2
    tab._maps = FakeMaps(grid)
    tab._grid_t = 0.0
    tab._refresh_grid((11.0, 10.0), TICK)        # (11,10) 不可走，兩邊都是鄰居
    ck("★ 站在牆上時挑**最大**的鄰居區，不是第一個問到的",
       tab._reach_n == len(large), str(tab._reach_n))

    # 走到點位但周圍還有走得到的怪 → 不算到（使用者明訂的規矩）
    tab = make_tab([{"do": "walk", "to": [50, 50]}, {"do": "clear"}],
                   pos=(50.0, 50.0))
    tab._live_monsters = lambda: [FakeMon(x=51.0, y=50.0, eid=1)]
    run(tab, 0.3)
    ck("★★ 站上點位了但周圍還有走得到的怪 → 還不算到",
       tab._i == 0, tab.status.text())
    tab._live_monsters = lambda: []
    run(tab, 0.2)
    ck("　怪清光了才算到", tab._i == 1)

    # 門開了：同一區的格數變多 → 可達集合要跟著變（不然解完謎還說走不到）
    tab = make_tab([{"do": "clear"}])
    shut = {(x, 10) for x in range(10, 40)}          # 門關著：30 格
    open_ = shut | {(x, 11) for x in range(10, 40)}  # 門開了：60 格
    tab._maps = FakeMaps(FakeGrid(shut))
    tab._grid_t = 0.0
    tab._refresh_grid((10.0, 10.0), TICK)
    ck("關著的時候 30 格", tab._reach_n == 30, str(tab._reach_n))
    tab._maps.grid = FakeGrid(open_)
    tab._grid_t = 0.0
    tab._refresh_grid((10.0, 10.0), TICK)
    ck("★ 門開了 → 重讀之後可達區真的變大（不必重開分頁）",
       tab._reach_n == 60, str(tab._reach_n))
    ck("　而且會講出來", "地形變了" in tab.status.text(), tab.status.text())

    # ★★ 入口傳送點（使用者 2026-09-02：「在副本裡面會直接執行 json；
    #   如果不在就會去撞副本傳點，撞了沒效就每 5 秒送一次直到成功」）
    print("\n進副本（入口傳送點）")
    ent = {"scene": 71, "to": [10, 20], "model": 60001}
    ok, why = dungeon.validate_entrance(ent)
    ck("入口格式合法", ok, why)
    ck("沒設入口也合法（只在副本裡跑）",
       dungeon.validate_entrance({})[0])
    ck("★ 少了 scene → 擋下",
       not dungeon.validate_entrance({"to": [1, 2]})[0])
    ck("★ 少了 to → 擋下", not dungeon.validate_entrance({"scene": 71})[0])
    ck("清單上看得出是入口", "入口" in dungeon.describe_entrance(ent))

    # ★ 人在別張圖（天使學園之類）→ 先用趴趴GO飛到入口那張圖
    #   （使用者 2026-09-02：「我在天使學園開自動刷副本會用趴趴GO飛過去嗎」）
    flown = []
    tab = make_tab([{"do": "clear"}])
    tab._script.scene = 76
    tab._script.entrance = ent
    tab._phase = "fly"
    tab._fly = dt.jumpmap.Entry(jump_id=73, scene_id=71, x=244, y=20,
                                name="地底廣場副本進入點")
    dt.jumpmap.teleport = lambda _mv, _sc, jid: (flown.append(jid),
                                                 (True, "送出"))[1]
    tab._go_fly(TICK)
    ck("★ 在別張圖 → 送趴趴GO", flown == [73], str(flown))
    for _ in range(int((dt.FLY_RESEND - 1.0) / TICK)):
        tab._go_fly(TICK)
    ck(f"　{dt.FLY_RESEND:.0f} 秒內不重送", len(flown) == 1, str(flown))
    for _ in range(int(1.5 / TICK)):
        tab._go_fly(TICK)
    ck("★ 沒到就再送一次（無限重試、不通知）", len(flown) == 2, str(flown))
    ck("　還勾著（⛔ 不因為飛不過去就停）", tab.run_cb.isChecked())

    tab = make_tab([{"do": "clear"}], pos=(50.0, 50.0))
    tab.trigs = [FakeTrig(10.0, 20.0, 60001)]
    poked = tab.portal_sent
    tab._script.scene = 76
    tab._script.entrance = ent
    tab._phase = "enter"
    tab._go_entrance((50.0, 50.0), TICK)
    ck("★ 還在外面 → 先走去入口", tab._nav.goal == (10, 20), str(tab._nav.goal))
    ck("　還沒到就不亂送", poked == [], str(poked))
    tab._go_entrance((10.0, 20.0), TICK)
    ck("★ 站到入口上 → 立刻打第一發 0x0D", poked == [60001], str(poked))
    for _ in range(int((dt.PORTAL_POKE - 1.0) / TICK)):
        tab._go_entrance((10.0, 20.0), TICK)
    ck(f"　{dt.PORTAL_POKE:.0f} 秒沒到不重送", len(poked) == 1, str(poked))
    for _ in range(int(1.5 / TICK)):
        tab._go_entrance((10.0, 20.0), TICK)
    ck(f"★ 過了 {dt.PORTAL_POKE:.0f} 秒才送第二次", len(poked) == 2, str(poked))
    # ⛔⛔ 使用者 2026-09-02 明令：「無限嘗試不需要通知」
    fired = []
    tab._warn = lambda msg: fired.append(msg)
    tab._enter_t = 900.0                          # 撞了 15 分鐘
    poked.clear()
    for _ in range(int((dt.PORTAL_POKE + 0.5) / TICK)):
        tab._go_entrance((10.0, 20.0), TICK)
    ck("★★ 撞不進去**無限重試**：15 分鐘後照樣在勾著、照樣打封包",
       tab.run_cb.isChecked() and poked == [60001],
       f"勾著={tab.run_cb.isChecked()} 送={poked}")
    ck("★★ 而且**不通知**（這是暫時性失敗，出口是使用者自己取消勾選）",
       fired == [], str(fired))
    ck("　狀態列會講已經試多久", "分鐘" in tab.status.text(),
       tab.status.text())
    # 走不到入口也不停（人牆／門），只重讀地形繼續試
    tab._nav.stuck, tab._nav.stuck_reason = True, "grid"
    tab._go_entrance((50.0, 50.0), TICK)
    ck("★ 算不出路也不停，重讀地形再試",
       tab.run_cb.isChecked() and tab._grid_t == 0.0, tab.status.text())
    tab._nav.stuck = False
    dt.produce.click = lambda *a, **k: (True, "點了")

    # ★★ 使用者 2026-09-02：「進入副本前不要自動打怪」＋「進入副本完全不
    #   打怪一直往點位走，請優先打怪再往點位走」
    # ⚠⚠ 使用者 2026-09-02 當場回報的兩個 bug，各留一項照妖鏡：
    #   ① 有選項的那一頁 `MESSAGE_IS_TALK` 也可能是 1（實機讀到 is_talk=True
    #      而且 options=(1,2)）→ 判「有沒有選項」只准看 OPTIONn。
    #   ② 送了選項之後畫面還沒換頁，⛔ 不可以當成「對話結束」。
    print("\n對話的兩個誤判")
    pg = dt.talkwnd.Page(is_talk=True, options=(1, 2), sig=(1,))
    ck("★★ is_talk=1 但有選項 → 照樣算「有選項」（⛔ 不可以按確定過掉）",
       pg.has_options and not pg.is_plain)
    ck("　真的沒有選項才算純對話",
       dt.talkwnd.Page(is_talk=False, options=(), sig=(2,)).is_plain)

    slow_reply = FakeTalk([(1, 2), (1, 2), ()])   # 送了選項也不馬上換頁
    step2 = {"do": "interact", "at": [20, 20], "model": 60307,
             "menu": [1, 2], "gap": 0.2}
    tab = make_tab([step2], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, slow_reply)
    slow_reply.i_lock = True                     # 點得開，但之後都不換頁
    run(tab, 2.0)                     # 兩秒都沒換頁
    ck("★★ 送了選項但對話還沒回 → **不可以**當成「對話結束」而停掉",
       tab.run_cb.isChecked(), tab.status.text())
    ck("　而且不會重複送同一項", len(tab.sent) == 1, str(tab.sent))
    run(tab, 60.0)
    ck("★★ 等了一分鐘也**不會停**（⛔ 沒有「幾秒沒到就壞掉」）",
       tab.run_cb.isChecked(), tab.status.text())
    ck("　還是只送過一次", len(tab.sent) == 1, str(tab.sent))

    # ★★ 使用者 2026-09-02：「最後一個石頭雕像他點不到」→ 點一次就不管是
    #   不對的（遊戲是自己走過去才開對話，路上被打斷那一下就落空）。
    never = FakeTalk([(1,)])
    never.opened = lambda: None            # 怎麼點都開不起來
    tab = make_tab([{"do": "interact", "at": [20, 20], "model": 60307,
                     "menu": [1], "gap": 0.2}], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, never)
    clicks = []
    dt.produce.click = lambda *_a, **_k: (clicks.append(1), (True, "點了"))[1]
    moved3 = []
    tab._mover = type("M", (), {
        "walk_near": lambda _s, _sc, _p, x, y, k: moved3.append(("near", k)),
        "walk_exact": lambda _s, _sc, _p, x, y: moved3.append(("exact", 0.0)),
    })()
    dt.entity.is_walking = lambda _sc, _p: False
    run(tab, (dt.CLICK_RETRY + dt.MENU_GAP * 2) * 5 + 1.0)
    ck(f"★★ 點了 {dt.CLICK_RETRY:.0f} 秒沒反應 → **再點一次**（不是點一次就不管）",
       len(clicks) >= 2, str(len(clicks)))
    ck("　而且還在跑（上限交給 STEP_TIMEOUT 大聲停）", tab.run_cb.isChecked())
    # ★★ 使用者 2026-09-02：「如果點了沒反應要調整位置往對話物件靠上去」
    ck("★★ 重點之前會**往物件靠上去**（站著硬點沒用）", bool(moved3),
       str(moved3))
    keeps = [k for _w, k in moved3]
    ck("　一次比一次近", keeps == sorted(keeps, reverse=True), str(keeps))
    ck("　最後直接穿過去（留 0 格）", 0.0 in keeps, str(keeps))

    # 收尾要把對話框從畫面上收掉（不然人會帶著框到處跑）
    closed = []
    dt.talkwnd.close_window = lambda *_a: (closed.append(1), True)[1]
    fake = FakeTalk([(1,), ()])
    tab = make_tab([{"do": "interact", "at": [20, 20], "model": 60307,
                     "menu": [1], "gap": 0.2}], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    wire(tab, fake)
    dt.talkwnd.close_window = lambda *_a: (closed.append(1), True)[1]
    run(tab, 8.0)
    ck("★★ 對話收尾會把視窗關掉（⛔ 不要帶著對話框到處跑）",
       bool(closed), str(closed))

    # ★★ 使用者 2026-09-02：「他沒走到我標的對話點才說話，而是在上個點位
    #   一直打對話」——對話點的容忍半徑本來 3.0（＝navigate.ARRIVE），
    #   站在 2.4 格外就開點了。收緊到 TALK_NEAR，最後一段自己走。
    walked = []
    tab = make_tab([{"do": "interact", "at": [20, 20], "model": 60307,
                     "menu": [1], "gap": 0.2}], pos=(17.8, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    tab._mover = type("M", (), {
        "walk_near": lambda _s, _sc, _p, x, y, k: walked.append((x, y, k)),
        "walk_exact": lambda _s, _sc, _p, x, y: walked.append((x, y, 0)),
    })()
    dt.entity.is_walking = lambda _sc, _p: False
    run(tab, 0.5)
    ck("★★ 離對話點 2.2 格 → **還要再走過去**，不是就地開點",
       not tab._clicked and walked, f"點了={tab._clicked} 走={walked}")
    ck("　最後一段用 walk_near 留幾格（機關常站在不可走格上）",
       bool(walked) and walked[0][2] == dt.TALK_KEEP, str(walked))
    tab._pos = [19.0, 20.0]
    run(tab, 0.4)
    ck("　真的靠近了才點下去", tab._clicked)

    # ★★ 使用者 2026-09-02：「跟 NPC 對話會記錄我在哪個位置對話的，
    #   會走到那個位置才對話」——有 stand 就以站位為準。
    walked2 = []
    st = {"do": "interact", "at": [20, 20], "model": 60307,
          "stand": [24, 20], "menu": [1], "gap": 0.2}
    ok, why = dungeon.validate(st)
    ck("★ 帶站位的對話步驟合法", ok, why)
    ck("　清單上看得到站位", "站位" in dungeon.describe(st),
       dungeon.describe(st))
    tab = make_tab([st], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    run(tab, 0.4)
    ck("★★ 就算已經站在物件旁邊，也要先走到**記下來的站位**",
       not tab._clicked and tab._nav.goal == (24, 20),
       f"點了={tab._clicked} 走去={tab._nav.goal}")
    tab._pos = [24.0, 20.0]
    run(tab, 0.4)
    ck("　到站位了才點下去", tab._clicked)

    # 兩支走路分開：走到那一格 vs 走到旁邊留幾格
    tab = make_tab([{"do": "clear"}])
    moved = []
    tab._mover = type("M", (), {
        "walk_near": lambda _s, _sc, _p, x, y, k: moved.append(("near", x, y, k)),
        "walk_exact": lambda _s, _sc, _p, x, y: moved.append(("exact", x, y)),
    })()
    dt.entity.is_walking = lambda _sc, _p: False
    tab._walk_onto(5, 6)
    tab._walk_beside(7, 8, 1.2)
    ck("★ _walk_onto 走到那一格本身（不留距離）",
       moved[0] == ("exact", 5, 6), str(moved))
    ck("★ _walk_beside 才留距離", moved[1] == ("near", 7, 8, 1.2),
       str(moved))

    # ★★ 使用者 2026-09-02：「設定的時候點得到、跑腳本卻不行、滑鼠點也可以」
    #   —— 差別在製作頁點完沒有人再叫它走路。點 0x05 之後**遊戲會自己
    #   走過去**才開對話，這期間插手（重點／重下走路）就會把那趟打斷。
    walking = {"v": True}
    dt.entity.is_walking = lambda _sc, _p: walking["v"]
    clicks2 = []
    tab = make_tab([{"do": "interact", "at": [20, 20], "model": 60307,
                     "menu": [1], "gap": 0.2}], pos=(20.5, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    never2 = FakeTalk([(1,)])
    never2.opened = lambda: None
    wire(tab, never2)
    dt.produce.click = lambda *_a, **_k: (clicks2.append(1),
                                          (True, "點了"))[1]
    run(tab, 0.5)
    ck("★★ 還在走路 → **先站穩再點**（走著點人沒到，對話開不起來）",
       clicks2 == [], str(clicks2))
    # ⚠⚠ 但不能無限等：人擠人／被推時 `is_walking` 會恆為 True
    #   （[[self-supply-buy]] 的老坑），那樣就永遠不點了 → 等過 STILL_WAIT
    #   就照點。
    run(tab, dt.STILL_WAIT + 0.3)
    ck(f"★★ 一直在動超過 {dt.STILL_WAIT:.1f} 秒 → **不等了，照樣點**",
       len(clicks2) >= 1, str(clicks2))
    walking["v"] = False
    clicks2.clear()
    tab = make_tab([{"do": "interact", "at": [20, 20], "model": 60307,
                     "menu": [1], "gap": 0.2}], pos=(20.5, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    never3 = FakeTalk([(1,)])
    never3.opened = lambda: None
    wire(tab, never3)
    dt.produce.click = lambda *_a, **_k: (clicks2.append(1),
                                          (True, "點了"))[1]
    run(tab, 0.3)
    ck("　站著不動的話馬上就點", len(clicks2) == 1, str(clicks2))
    # 點完遊戲自己在走過去 → 只要還在靠近就不要插手重點
    #   ⚠ 要像真的走路那樣**每一拍都更近一點**（使用者要求重試收到 1 秒，
    #     用「1.5 秒才動一格」的假走法會被判成停住不動）。
    d = 4.0
    for _ in range(30):
        d -= 0.12
        tab._pos = [20.1 + d, 20.2]
        run(tab, TICK)
    ck("★★ 遊戲正在走過去（一直更靠近）→ **不插手重點**",
       len(clicks2) == 1, str(clicks2))
    run(tab, dt.CLICK_RETRY + 0.5)               # 停在原地不動了
    ck("　真的停住不動才重點", len(clicks2) >= 2, str(clicks2))
    dt.entity.is_walking = lambda _sc, _p: False

    # ★★ 使用者 2026-09-02：「有一個副本門口進去還要選第一個選項才能進去」
    ent2 = {"scene": 71, "to": [10, 20], "model": 60001, "menu": [1],
            "stand": [11, 20], "gap": 0.2}
    ok, why = dungeon.validate_entrance(ent2)
    ck("★ 對話式入口格式合法", ok, why)
    ck("　清單上看得出要選第幾項",
       "第1項" in dungeon.describe_entrance(ent2),
       dungeon.describe_entrance(ent2))
    ck("　沒 menu 的照舊寫「踩上去就傳」",
       "踩上去" in dungeon.describe_entrance(ent))
    ck("★ menu 值不合法 → 擋下",
       not dungeon.validate_entrance(
           {"scene": 71, "to": [1, 2], "menu": [99]})[0])

    # ★★ 使用者 2026-09-02 更正：「要去撞他自己會產生對話，所以點點看沒用」
    #   → 入口是**撞（0x0D）出對話**，再選第 N 項；⛔ 不是用點的。
    tab = make_tab([{"do": "clear"}], pos=(10.0, 20.0))
    tab.trigs = [FakeTrig(10.0, 20.0, 60001)]
    tab._script.scene = 76
    tab._script.entrance = ent2
    tab._phase = "enter"
    dt.entity.is_walking = lambda _sc, _p: False
    pages = {"i": 0}                      # 0＝沒對話；撞了才變 1（有選項）
    dt.talkwnd.page = lambda _sc: dt.talkwnd.Page(
        is_talk=pages["i"] == 0, options=((1, 2) if pages["i"] else ()),
        sig=(pages["i"],))
    dt.talkwnd.window_open = lambda _mv, _sc: pages["i"] > 0
    real_enter = dt.portal.enter
    dt.portal.enter = lambda mv, sc, t, pf: (pages.__setitem__("i", 1),
                                             real_enter(mv, sc, t, pf))[1]
    for _ in range(int(3.0 / TICK)):      # ⚠ 入口那條要直接叫 _go_entrance
        if not tab.run_cb.isChecked():
            break
        tab._go_entrance(tab._my_pos(), TICK)
    from app.game import supply as _sup3
    ck("★★ 撞入口（打 0x0D）", tab.portal_sent == [60001], str(tab.portal_sent))
    ck("★★ 撞出對話之後才送第 1 項（⛔ 不是用點的）",
       tab.sent == [_sup3.talk_option(1)], str(tab.sent))
    ck("　還沒進去 → 不前進步驟、階段還是 enter",
       tab._i == 0 and tab._phase == "enter", f"步{tab._i} {tab._phase}")
    dt.portal.enter = real_enter

    # ★★ 使用者 2026-09-02 實遇：「他就在門口一直打封包，打封包了也沒按 1」
    #   —— 走到那裡時對話**已經開著**（撞過一次／上一輪留的），舊碼把那一頁
    #   當基準，之後永遠「沒變」就只剩打封包。⛔ 不可以只看「簽章變了」。
    tab = make_tab([{"do": "clear"}], pos=(10.0, 20.0))
    tab.trigs = [FakeTrig(10.0, 20.0, 60001)]
    tab._script.scene = 76
    tab._script.entrance = ent2
    tab._phase = "enter"
    dt.talkwnd.page = lambda _sc: dt.talkwnd.Page(   # 一開始就開著、而且不變
        is_talk=True, options=(1, 2), sig=("一直是這一頁",))
    dt.talkwnd.window_open = lambda _mv, _sc: True
    for _ in range(int(1.0 / TICK)):
        tab._go_entrance(tab._my_pos(), TICK)
    ck("★★ 一到門口對話就已經開著（簽章從頭到尾沒變）→ **照樣送第 1 項**",
       tab.sent == [_sup3.talk_option(1)], str(tab.sent))
    ck("　同一頁不會一直重送", tab.sent == [_sup3.talk_option(1)],
       str(tab.sent))

    print("\n打怪的時機")
    tab = make_tab([{"do": "clear"}])
    tab._keys, tab._atk = FakeKeys(), FakeAtk()
    tab._maps = FakeMaps()                      # 沒有地形圖 → 不篩選怪
    tab._script.scene = 76
    tab._script.entrance = {"scene": 71, "to": [10, 20], "model": 60001}
    tab._phase = "enter"
    # ⚠ _reset_run() 會把 _state/_player 清成 None，所以要在 make_tab 之後補
    tab._state, tab._player = 0x1000, 0x2000
    tab._live_monsters = lambda: [FakeMon(x=11.0, y=10.0, eid=1)]
    dt.entity.read_pos = lambda _sc, _addr: (11.0, 10.0)
    dt.scene.current_id = lambda _sc, allow_scan=True: 71
    dt.scene.map_key = lambda v: v
    dt.scene.scene_name = lambda v: f"圖{v}"
    tab._map_key = 71
    tab._tick()
    ck("★★ 還在去副本的路上 → **不打怪**，直接趕路",
       tab._atk.picked is None and tab._keys.on is False,
       f"挑了{tab._atk.picked} on={tab._keys.on}")
    tab._phase = "run"
    tab._map_key = 71
    tab._script.scene = 71
    tab._tick()
    ck("★ 進到副本裡（run）→ 才開始打怪", tab._atk.picked is not None,
       tab.status.text())

    # 換圖／傳點之後要強制全掃（不然熱區還是舊圖那塊，會整塊漏掉怪）
    tab = make_tab([{"do": "clear"}])
    tab._script.scene = 76
    tab._script.entrance = {"scene": 71, "to": [10, 20]}
    tab._phase = "enter"
    tab._map_key = 71
    tab._maps = FakeMaps()
    tab._keys, tab._atk = FakeKeys(), FakeAtk()
    dt.scene.current_id = lambda _sc, allow_scan=True: 76
    dt.scene.map_key = lambda v: v
    dt.dungeon.check_map = lambda *a, **k: (True, "")
    tab._check_map_change()
    ck("★★ 進副本的那一刻要求全掃（不然 30 秒內看不到新圖的怪）",
       tab._scan.fulls == 1 and tab._phase == "run",
       f"全掃{tab._scan.fulls} phase={tab._phase}")

    print("\n不認得的動作")
    tab = make_tab([{"do": "walk", "to": [1, 1]}])
    tab._script.steps[0] = {"do": "fly"}                  # 繞過 validate 硬塞
    run(tab, 0.3)
    ck("★ 不認得的動作 → 停下來，不當作沒事跳過",
       not tab.run_cb.isChecked(), tab.status.text())

    # ★★ 製作頁：使用者 2026-09-02「按 1 就進副本，無法設定」——
    #   按了選項人就被傳走，所以選項要在**按下的當下**就記進入口。
    # ★★ 使用者 2026-09-02：「改一下自動刷副本，我可以選擇從哪開始」＋
    #   「自動刷副本的設定也都要記錄在使用者那邊」
    # ★ 2026-09-05 改：「從第幾步開始」是一個**開關**——沒打開下拉是灰的、一律從
    #   第 1 步；開關與選了第幾步都**不記錄**（預設關）。
    print("\n從第幾步開始（開關，不記錄）＋ 設定記在使用者那邊")
    steps5 = [{"do": "walk", "to": [i, i]} for i in range(1, 6)]
    tab = make_tab(steps5)
    tab.start_box.clear()
    for i, st in enumerate(steps5):
        tab.start_box.addItem(f"{i + 1}", i)
    tab.start_box.setCurrentIndex(2)          # 下拉選第 3 步，但開關沒開
    ck("★ 開關預設關、下拉是灰的",
       not tab.start_cb.isChecked() and not tab.start_box.isEnabled())
    ck("★ 開關沒開 → 就算下拉選了第 3 步也從第 1 步",
       tab._round_plan(5)[0] == 0, str(tab._round_plan(5)))
    tab.start_cb.setChecked(True)
    ck("★ 開關打開 → 下拉亮起來、真的從第 3 步",
       tab.start_box.isEnabled() and tab._round_plan(5)[0] == 2,
       str(tab._round_plan(5)))
    tab._reset_run()
    tab._i = tab._round_plan(len(steps5))[0]
    run(tab, 0.3)
    ck("★★ 選了從第 3 步開始 → 真的從第 3 步跑",
       tab._nav.goal == (3, 3), str(tab._nav.goal))

    from app.config import config as _cfg
    tab._account = lambda: "測試帳號"
    tab._loading = False
    _cfg.set("dungeon.測試帳號.start", 4)     # 舊版留下的值，新版不准再讀它
    tab._save_settings()
    ck("★ 「從第幾步」不存進 config（舊值原封不動）",
       _cfg.get("dungeon.測試帳號.start") == 4,
       str(_cfg.get("dungeon.測試帳號.start")))
    ck("　技能鍵有存", isinstance(_cfg.get("dungeon.測試帳號.vks"), list))
    ck("　記得上次是哪一台分身",
       _cfg.get("dungeon.last_account") == "測試帳號")
    # ⚠ `_load_settings` 會先照「目前選的腳本檔」重建下拉；測試裡沒有真的檔，
    #   所以把重建換掉 —— 要驗的是「重讀設定會把開關關掉、不套用舊的 start」。
    tab._refresh_start_box = lambda: None
    tab._load_settings()
    ck("★★ 重讀設定 → 開關回到關、下拉灰掉、不套用舊值",
       not tab.start_cb.isChecked() and not tab.start_box.isEnabled()
       and tab._round_plan(5)[0] == 0,
       f"cb={tab.start_cb.isChecked()} idx={tab.start_box.currentIndex()}")

    # ★★ 使用者 2026-09-05：「多一個選項是循環打副本，打勾就會一直跑，不然只打一場；
    #   勾了循環就算從中間開始也會繼續跑下一場 —— 從中間開始只有我開的那場，後面從頭」
    print("\n循環打副本勾選框 ＋ 「從第幾步」只管第一場")
    tab.party_box.setCurrentIndex(tab.party_box.findData("bind"))
    tab.start_cb.setChecked(True)             # 開關打開、下拉還在第 3 步
    tab.loop_cb.setChecked(True)
    ck("★★ 勾循環、從第 3 步開始 → 這一場從第 3 步、要循環、組隊照選",
       tab._round_plan(5) == (2, True, "bind"), str(tab._round_plan(5)))
    tab.loop_cb.setChecked(False)
    ck("★★ 沒勾循環 → 只打一場：不循環、不組隊（起始步驟照舊）",
       tab._round_plan(5) == (2, False, "none"), str(tab._round_plan(5)))
    tab.start_box.blockSignals(True)
    tab.start_box.setCurrentIndex(0)
    tab.start_box.blockSignals(False)
    tab.loop_cb.setChecked(True)
    ck("　勾循環、從第 1 步 → 跟以前一樣（循環＋組隊）",
       tab._round_plan(5) == (0, True, "bind"), str(tab._round_plan(5)))
    tab._save_settings()
    ck("★ 循環勾選存進 config", _cfg.get("dungeon.測試帳號.loop") is True,
       str(_cfg.get("dungeon.測試帳號.loop")))
    tab.loop_cb.setChecked(False)              # 會觸發存檔 → False
    tab.loop_cb.blockSignals(True)
    tab.loop_cb.setChecked(True)               # 畫面先弄髒，看讀回來有沒有蓋掉
    tab.loop_cb.blockSignals(False)
    tab._load_settings()
    ck("★★ 重讀設定 → 循環勾選讀回來（沒勾）", not tab.loop_cb.isChecked())
    tab.loop_cb.setChecked(True)

    print("\n製作頁：入口的選項自動記進去")
    from app.tabs.dungeon_make_tab import DungeonMakeTab
    # ★ 製作頁的傳點出口監看（2026-09-05 稽核）：跟執行端同一條規矩 ——
    #   跳之前不在傳點上（伺服器拉回）→ 不記出口、繼續盯；從傳點上跳走才記。
    from app.tabs import dungeon_make_tab as dmt
    mk2 = DungeonMakeTab()
    mk2._script = dungeon.Script(name="t", steps=[{"do": "portal",
                                                    "to": [141.3, 277.6]}])
    mk2._cur = lambda: (1, object())
    pos2 = [128.0, 270.0]
    mk2._me = lambda _sc: tuple(pos2)
    dmt.scene.current_id = lambda _sc, **_k: 110
    mk2._pw = (0, time.monotonic() + 60.0, (134.0, 270.0), time.monotonic())
    mk2._portal_watch()
    ck("★ 製作頁：跳之前離傳點 10 格（拉回）→ 不記出口、繼續盯",
       "land" not in mk2._script.steps[0] and mk2._pw is not None,
       str(mk2._script.steps[0]))
    pos2[:] = [47.5, 278.5]
    mk2._pw = (0, time.monotonic() + 60.0, (141.0, 277.0), time.monotonic())
    mk2._portal_watch()
    ck("★ 製作頁：從傳點上跳走 → 記出口",
       mk2._script.steps[0].get("land") == [47.5, 278.5], str(mk2._script.steps[0]))
    mk = DungeonMakeTab()
    mk._script = dungeon.Script(name="t")
    mk._script.entrance = {"scene": 71, "to": [10.0, 20.0], "model": 60001}
    mk._here_key = lambda: (71, None)
    mk._cur = lambda: (1, object())
    mk._me = lambda _sc: (10.5, 20.0)
    mk._mover = lambda _pid: object()
    mk._refresh_stamp = lambda: None
    dt.sell.talk = lambda *_a, **_k: True
    mk._poked = None
    mk._send_option(1)
    ck("★★ 站在入口旁按第 1 項 → **自動記進入口**",
       mk._script.entrance.get("menu") == [1], str(mk._script.entrance))
    mk._send_option(2)
    ck("　再按一項就接在後面", mk._script.entrance["menu"] == [1, 2],
       str(mk._script.entrance["menu"]))
    mk._me = lambda _sc: (80.0, 90.0)
    mk._send_option(3)
    ck("★ 離入口很遠時按的選項 ⛔ 不會亂記進入口",
       mk._script.entrance["menu"] == [1, 2],
       str(mk._script.entrance["menu"]))
    mk._here_key = lambda: (76, None)
    mk._me = lambda _sc: (10.5, 20.0)
    mk._send_option(4)
    ck("★ 不在入口那張圖 ⛔ 也不會記",
       mk._script.entrance["menu"] == [1, 2],
       str(mk._script.entrance["menu"]))
    mk.on_close()

    # =====================================================================
    # ★★★ 全自動循環（使用者 2026-09-03）：刷完 → 補給 → 飛回入口 → 退組再組隊 → 循環
    # =====================================================================
    print("\n全自動循環：刷完 → 補給 → 飛回入口 → 組隊")

    class FakeEntry:
        name, jump_id = "入口旁", 7

    class FakeSc:
        _is_partner = True

    def loop_tab(party="bind", start=0):
        tab = make_tab([{"do": "walk", "to": [10, 10]}], pos=(10.5, 10.0))
        tab._script.scene = 98
        tab._script.entrance = {"scene": 90, "to": [294.2, 14.7],
                                "model": 60001, "menu": [1]}
        tab._loop = start == 0
        tab._party = party if tab._loop else "none"
        tab._i = start
        tab._pid = 1
        tab._ppid, tab._psc, tab._pmover = 2, FakeSc(), object()
        tab._partner_name = "小黑"
        tab._targets = lambda: []
        tab._drop_target = lambda: None
        return tab

    world = {"here": 98, "mine": [], "his": [], "left": [], "invited": [],
             "joined": 0, "flown": []}

    class M:
        def __init__(self, n):
            self.name = n
    dt.team.members = lambda sc: list(world["his"] if sc is not None and
                                      getattr(sc, "_is_partner", False)
                                      else world["mine"])
    dt.team.leave = lambda mv: (world["left"].append(id(mv)), True)[1]
    dt.team.invite = lambda mv, name, share: (world["invited"].append((name, share)),
                                              (True, ""))[1]
    dt.team.join = lambda mv, sc: (world.__setitem__("joined", world["joined"] + 1),
                                   (True, ""))[1]
    dt.scene.current_id = lambda sc, **k: world["here"]
    dt.jumpmap.nearest = lambda scene_id, x, y: FakeEntry()
    dt.jumpmap.teleport = lambda mv, sc, jid: (world["flown"].append(jid),
                                               (True, "送出"))[1]

    # ① 單輪（從第幾步有選）：跑完就停，不補給
    tab = loop_tab("bind", start=0)
    tab._loop = False
    tab._pos = [10.5, 10.0]
    run(tab, 0.3)                       # 第 1 步走到了 → _i=1 → _finish
    run(tab, dt.CLEAR_SETTLE + 0.5)
    ck("單輪：跑完就停（不進補給）", not tab.run_cb.isChecked()
       and tab._cycle == "go", f"{tab._cycle} {tab.status.text()}")

    # ①b ★★ 使用者 2026-09-05：勾了循環、從中間開始 → 這一場照樣跑完就補給，
    #   下一趟從第 1 步（「從中間開始只有我開的那場，後面還是要從頭」）。
    tab = make_tab([{"do": "walk", "to": [10, 10]}, {"do": "walk", "to": [12, 12]}],
                   pos=(12.5, 12.0))
    tab._i = 1                          # 從第 2 步（最後一步）開始
    tab._loop, tab._party = True, "none"
    tab._targets = lambda: []
    tab._drop_target = lambda: None
    started2 = []
    tab._start_supply_trip = lambda: (started2.append(1),
                                      setattr(tab, "_i", 0),   # 真的那支會歸零
                                      setattr(tab, "_cycle", "supply"))[1]
    run(tab, 0.3)
    run(tab, dt.CLEAR_SETTLE + 0.5)
    ck("★★ 勾循環＋從中間開始：跑完不停機、進補給", tab.run_cb.isChecked()
       and started2 and tab._cycle == "supply", f"{tab._cycle} {tab.status.text()}")
    ck("　下一趟從第 1 步", tab._i == 0, str(tab._i))
    ck("　趟數有算", tab._rounds == 1, str(tab._rounds))

    # ② 循環：跑完 → 進「補給」段（不停機），補給完 → 飛回入口 → 組隊 → 開跑
    tab = loop_tab("bind")
    started = []
    tab._start_supply_trip = lambda: (started.append(1),
                                      setattr(tab, "_i", 0),   # 真的那支會歸零
                                      setattr(tab, "_cycle", "supply"))[1]
    tab._pos = [10.5, 10.0]
    run(tab, 0.3)
    run(tab, dt.CLEAR_SETTLE + 0.5)
    ck("★ 循環：跑完不停機，改去補給", tab.run_cb.isChecked() and started
       and tab._cycle == "supply", f"{tab._cycle} {tab.status.text()}")
    # 補給回來了：人在城裡（別張圖）→ 要飛
    world["here"] = 26
    tab._supply_result = (True, "都夠了")
    run(tab, 0.3)
    ck("★ 補給完 → 人在別張圖 → 趴趴GO飛回入口那張圖", tab._cycle == "back"
       and tab._phase == "fly" and world["flown"], f"{tab._cycle}/{tab._phase}")
    # 落地入口那張圖 → 組隊段：先退組（兩隻都要清空）
    world["here"] = 90
    world["mine"], world["his"] = [M("小黑")], [M("黑狐")]
    run(tab, 0.3)
    ck("★ 落地 → 進組隊段，先退組（兩隻都送）", tab._cycle == "team"
       and tab._team_sub == "leave" and len(world["left"]) >= 2,
       f"{tab._cycle}/{tab._team_sub} left={len(world['left'])}")
    world["mine"], world["his"] = [], []
    run(tab, 0.3)
    ck("　名單清空 → 換成邀請", tab._team_sub == "invite", tab._team_sub)
    run(tab, 0.5)
    ck("★ 刷副本這隻當隊長邀請綁定分身、**均分**、分身按同意",
       world["invited"] and world["invited"][0] == ("小黑", dt.team.SHARE_EVEN)
       and world["joined"] >= 1, f"{world['invited']} joined={world['joined']}")
    world["mine"] = [M("小黑")]
    run(tab, 0.6)
    ck("★ 分身出現在名單 → 組隊完成 → 回到「去撞入口」", tab._cycle == "go"
       and tab._phase == "enter", f"{tab._cycle}/{tab._phase}")

    # ③ 遊戲自動組隊：退組 → 等名單有人 → 開跑
    tab = loop_tab("auto")
    tab._ppid = tab._psc = tab._pmover = None
    world.update(mine=[M("路人")], his=[], left=[], invited=[], joined=0, here=90)
    tab._phase = "enter"
    tab._team_begin()
    run(tab, 0.3)
    ck("自動組隊：先退組", tab._team_sub == "leave" and world["left"], tab._team_sub)
    world["mine"] = []
    run(tab, 0.3)
    ck("　清空後改成等遊戲配隊（不邀請任何人）", tab._team_sub == "wait"
       and not world["invited"], f"{tab._team_sub} {world['invited']}")
    run(tab, 1.0)
    ck("　沒隊伍就一直等（不停機）", tab.run_cb.isChecked() and tab._cycle == "team")
    world["mine"] = [M("路人甲"), M("路人乙")]
    run(tab, 0.3)
    ck("★ 名單出現人 → 開跑", tab._cycle == "go", tab._cycle)

    print(f"\n通過 {PASS}　失敗 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
