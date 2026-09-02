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
        self.calls = 0

    def reset(self, goal=None):
        self.goal = goal

    def step(self, _sc, _mv, _pobj, gx, gy):
        self.calls += 1
        self.goal = (gx, gy)
        return "走路中"


class FakeMon:
    """最小的假怪：挑目標／可達過濾／收工判定只用到這幾個欄位。"""

    def __init__(self, x=10.0, y=10.0, eid=1, name="怪", addr=0x3000):
        self.x, self.y, self.eid, self.name, self.addr = x, y, eid, name, addr
        self.dead = False


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

    def set_on(self, v):
        self.on = v

    def stop(self):
        pass

    def wait(self, _ms=0):
        pass


class FakeGrid:
    """假地形：`cells` ＝我這一區走得到的格；`others` ＝別區的可走格。"""

    def __init__(self, cells, others=()):
        self.cells = set(cells)
        self.others = set(others)

    def reachable(self, x, y):
        return set(self.cells) if (x, y) in self.cells else None

    def walkable(self, x, y):
        return (x, y) in self.cells or (x, y) in self.others


class FakeAtk:
    """最小的假「寫目標」執行緒。"""

    def __init__(self):
        self.picked = None
        self.packets = False
        self.engaged = False

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

    def opened(self):
        if self.i < 0:
            self.i = 0

    def page(self, _sc):
        if self.i < 0:
            return dt.talkwnd.Page(is_talk=True, options=(), sig=("殘留",))
        # ⚠ 翻到底之後 i 就不再變 → 簽章固定 ＝ 真實世界「對話結束後那些
        #   全域凍在最後一頁」的行為，執行端就是靠這個判對話走完了。
        j = min(self.i, len(self.pages) - 1) if self.pages else 0
        opts = tuple(self.pages[j] or ()) if self.pages else ()
        return dt.talkwnd.Page(is_talk=not opts, options=opts,
                               sig=(min(self.i, len(self.pages)), opts))

    def close(self, _mv, _sc):
        self.closes += 1
        if not self.i_lock:
            self.i = min(max(self.i, 0) + 1, len(self.pages))
        return True, "送出"


def wire(tab, fake):
    dt.talkwnd.page = fake.page
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
    tab._my_pos = lambda: tuple(tab._pos)
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

    # ★ 每一步自己的選項間隔（使用者：太快說話會出現無異議對話）
    slow = {"do": "interact", "at": [20, 20], "model": 60307,
            "menu": [1, 2], "gap": 5.0}
    tab = make_tab([slow], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60307)])
    run(tab, 0.3)
    ck("點下去了", tab._clicked)
    run(tab, 2.0)
    ck("★ 間隔 5 秒時，2 秒還不送選項", tab.sent == [], str(tab.sent))
    run(tab, 4.0)
    ck("★ 過了 5 秒才送第一個", len(tab.sent) == 1, str(tab.sent))

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
    tab._jumped = True
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
    fake.close = lambda _mv, _sc: (True, "送出")     # 按了也不翻頁＝對話沒了
    dt.talkwnd.close_page = fake.close
    run(tab, 8.0)
    ck("★ 對話走完了選項卻還沒送到 → 停下來",
       not tab.run_cb.isChecked(), tab.status.text())

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
    tab._jumped = True
    run(tab, 0.1)
    ck("★ 傳到腳本記的出口 → 過", tab._i == 1, tab.status.text())
    tab = make_tab([{"do": "portal", "to": [50, 50], "land": [200, 40]}])
    tab._pos = [12.0, 300.0]
    tab._jumped = True
    run(tab, 0.1)
    ck("★ 傳到別的地方 → 大聲停下（不拿別處的座標繼續跑）",
       not tab.run_cb.isChecked(), tab.status.text())

    # 順移偵測本身：速度分得出「走的」跟「傳的」
    tab = make_tab([{"do": "clear"}])
    tab._pos_prev, tab._pos_t = (10.0, 10.0), time.monotonic()
    ck("★ 走路一拍動 0.5 格 → 不是順移",
       not tab._check_jump((10.5, 10.0)))
    tab._pos_prev, tab._pos_t = (10.0, 10.0), time.monotonic()
    ck("★ 一拍跳 40 格 → 是順移", tab._check_jump((50.0, 10.0)))
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
    far = FakeMon(x=300.0, y=200.0, eid=2, name="很遠的隔壁區")
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
    tab._keys = FakeKeys()
    tab._live_monsters = lambda: [FakeMon(x=300.0, y=200.0, eid=9)]
    ck("★ 只剩打不到的怪 → 不算在打怪，腳本照跑",
       not tab._fight((10.0, 10.0), TICK))
    run(tab, 0.3)
    ck("　腳本真的動了（尋路往點位走）", tab._nav.goal == (50, 50),
       str(tab._nav.goal))

    # ⛔ 使用者明令不要黑名單：換一隻就好，一直問它走不走得到
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    m = FakeMon(x=11.0, y=10.0, eid=7, name="打不到的")
    tab._live_monsters = lambda: [m]
    tab._cur = m
    tab._grid_t = 5.0
    tab._give_up("測試")
    ck("★★ 放棄之後**不記黑名單**：只剩它一隻就照樣再問一次（門可能開了）",
       [x.eid for x in tab._targets()] == [7]
       and not hasattr(tab, "_skip"))
    ck("　放棄會立刻重問一次地形（門開了才跟得上）", tab._grid_t == 0.0)
    other = FakeMon(x=12.0, y=10.0, eid=8, name="另一隻")
    tab._live_monsters = lambda: [m, other]
    tab._keys, tab._atk = FakeKeys(), FakeAtk()
    dt.entity.read_pos = lambda _sc, _addr: None    # 讀不到位置就放掉，夠測挑選
    tab._fight((10.0, 10.0), TICK)
    ck("★ 有別隻的時候先挑別隻（不是黑名單，只是先換一隻）",
       tab._atk.picked is other,
       tab._atk.picked.name if tab._atk.picked else "沒挑")

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
    run(tab, dt.CLICK_RETRY * 4 + 1.0)
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
    print("\n從第幾步開始 ＋ 設定記在使用者那邊")
    steps5 = [{"do": "walk", "to": [i, i]} for i in range(1, 6)]
    tab = make_tab(steps5)
    tab.start_box.clear()
    for i, st in enumerate(steps5):
        tab.start_box.addItem(f"{i + 1}", i)
    tab.start_box.setCurrentIndex(2)          # 從第 3 步開始
    tab._reset_run()
    tab._i = max(0, min(tab.start_box.currentIndex(), len(steps5) - 1))
    run(tab, 0.3)
    ck("★★ 選了從第 3 步開始 → 真的從第 3 步跑",
       tab._nav.goal == (3, 3), str(tab._nav.goal))

    from app.config import config as _cfg
    tab._account = lambda: "測試帳號"
    tab._loading = False
    tab._save_settings()
    ck("★ 設定寫進 config（起始步驟）",
       _cfg.get("dungeon.測試帳號.start") == 2,
       str(_cfg.get("dungeon.測試帳號.start")))
    ck("　技能鍵也存了", isinstance(_cfg.get("dungeon.測試帳號.vks"), list))
    ck("　記得上次是哪一台分身",
       _cfg.get("dungeon.last_account") == "測試帳號")
    # ⚠ 這裡要擋訊號：直接動下拉會觸發 `_save_settings`，把剛存的 2 蓋成 0
    #   （真的操作介面時本來就該存，所以是測試要配合，不是程式的問題）。
    tab.start_box.blockSignals(True)
    tab.start_box.setCurrentIndex(0)
    tab.start_box.blockSignals(False)
    # ⚠ `_load_settings` 會先照「目前選的腳本檔」重建下拉；測試裡沒有真的檔，
    #   所以把重建換掉 —— 要驗的是「有沒有把索引讀回來」。
    tab._refresh_start_box = lambda: None
    tab._load_settings()
    ck("★★ 重讀設定 → 起始步驟回到第 3 步",
       tab.start_box.currentIndex() == 2,
       str(tab.start_box.currentIndex()))

    print("\n製作頁：入口的選項自動記進去")
    from app.tabs.dungeon_make_tab import DungeonMakeTab
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


    print(f"\n通過 {PASS}　失敗 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
