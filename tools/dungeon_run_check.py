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


class FakeMaps:
    """假地形快取：記 drop() 被叫幾次（Cache 有 __slots__ 不能改方法）。"""

    def __init__(self, grid=None):
        self.grid = grid
        self.drops = 0

    def drop(self):
        self.drops += 1

    def get(self, _sc):
        return self.grid


class FakeProp:
    def __init__(self, x, y, model, oid=0x13920001):
        self.x, self.y, self.model, self.oid = x, y, model, oid

    def dist(self, p):
        return ((self.x - p[0]) ** 2 + (self.y - p[1]) ** 2) ** 0.5


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
    tab.run_cb.blockSignals(True)
    tab.run_cb.setChecked(True)
    tab.run_cb.blockSignals(False)
    dt.scenery.nearby = lambda _sc, around=None, r=0: [
        p for p in props
        if around is None or p.dist(around) <= r]
    dt.produce.click = lambda *a, **k: (True, "點了")
    tab.sent = []
    dt.sell.talk = lambda _mv, code: (tab.sent.append(code), True)[1]
    tab.left = []
    dt.supply.leave_npc = lambda *a, **k: tab.left.append(1)
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

    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    tab._nav.stuck, tab._nav.stuck_reason = True, "grid"
    run(tab, 0.3)
    ck("★ 地形圖說走不到 → 大聲停下（不無聲耗著）",
       not tab.run_cb.isChecked(), tab.status.text())
    ck("　訊息講得出是哪一步", "第 1 步" in tab.status.text(),
       tab.status.text())

    print("\n等待步驟")
    tab = make_tab([{"do": "wait", "secs": 1.0}, {"do": "clear"}])
    run(tab, 0.5)
    ck("時間沒到不前進", tab._i == 0)
    run(tab, 0.8)
    ck("時間到 → 前進", tab._i == 1)

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
    run(tab, dt.MENU_GAP * 2 + 0.5)
    from app.game import supply as _sup
    want = [_sup.talk_option(1), _sup.talk_option(2)]
    ck("選項照順序送出（第1項→第2項）", tab.sent == want,
       f"送出 {tab.sent}，應該是 {want}")
    ck("送完 → 前進下一步", tab._i == 1)
    ck("★ 送完會送「離開互動」（不送伺服器會以為還在講話）", tab.left == [1])

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

    tab = make_tab([step], pos=(20.0, 20.0),
                   props=[FakeProp(20.1, 20.2, 60999)])   # 外觀對不上
    run(tab, 0.3)
    ck("★ 找不到對應外觀 → 大聲停下（⛔ 絕不就近點一個）",
       not tab.run_cb.isChecked(), tab.status.text())
    ck("　訊息有講外觀編號", "60307" in tab.status.text(), tab.status.text())

    tab = make_tab([step], pos=(20.0, 20.0), props=[])
    dt.scenery.nearby = lambda *a, **k: None              # 讀不到
    run(tab, 0.5)
    ck("★ 物件清單讀不到 ≠ 沒有 → 繼續重試，不停機也不亂點",
       tab.run_cb.isChecked() and not tab._clicked, tab.status.text())

    print("\n卡住保護")
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    run(tab, dt.STEP_TIMEOUT + 1)
    ck(f"★ 一步卡超過 {dt.STEP_TIMEOUT:.0f} 秒 → 停下來",
       not tab.run_cb.isChecked(), tab.status.text())

    print("\n收工判定（使用者定：腳本跑完 ＋ 周圍沒怪）")
    mons = [FakeMon()]
    tab = make_tab([{"do": "wait", "secs": 0.2}], mons=mons)
    run(tab, 1.0)
    ck("腳本跑完了", tab._i >= 1)
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
    tab._pos = [50.2, 50.2]
    run(tab, 0.3)
    ck("★ 就算站上去了也還是不算完成", tab._i == 0, str(tab._i))
    ck("　一直往傳點走", tab._nav.goal == (50, 50), str(tab._nav.goal))
    tab._jumped = True
    run(tab, 0.1)
    ck("★ 順移了 → 這一步完成", tab._i == 1)

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

    # 看門狗：一直在打卻半隻都沒殺掉 → 大聲停下（沒有黑名單就靠它兜底）
    tab = make_tab([{"do": "walk", "to": [50, 50]}])
    tab._keys, tab._atk = FakeKeys(), FakeAtk()
    tab._live_monsters = lambda: [FakeMon(eid=5)]
    tab._nokill_t = dt.NO_PROGRESS + 1
    tab._fight((10.0, 10.0), TICK)
    ck(f"★ 打了 {dt.NO_PROGRESS:.0f} 秒一隻都沒殺掉 → 大聲停下",
       not tab.run_cb.isChecked(), tab.status.text())

    # 地形圖要定期重讀（機關開門會就地改掉可走格）
    tab = make_tab([{"do": "clear"}])
    tab._maps = FakeMaps()
    tab._grid_t = 0.0
    tab._refresh_grid((10.0, 10.0), TICK)
    ck("★ 時間到就重讀地形（門開了才跟得上）", tab._maps.drops == 1)
    tab._refresh_grid((10.0, 10.0), TICK)
    ck("　沒到時間不重讀（不要每拍讀一整張圖）", tab._maps.drops == 1)
    ck("　讀不到圖 → 可達集合是 None ＝不過濾", tab._reach is None)

    # 門開了：同一區的格數變多 → 可達集合要跟著變（不然解完謎還說走不到）
    tab = make_tab([{"do": "clear"}])
    tab._maps = FakeMaps(FakeGrid({(10, 10), (11, 10)}))
    tab._grid_t = 0.0
    tab._refresh_grid((10.0, 10.0), TICK)
    ck("關著的時候只有 2 格", tab._reach_n == 2, str(tab._reach_n))
    tab._maps.grid = FakeGrid({(10, 10), (11, 10), (12, 10), (13, 10)})
    tab._grid_t = 0.0
    tab._refresh_grid((10.0, 10.0), TICK)
    ck("★ 門開了 → 重讀之後可達區真的變大（不必重開分頁）",
       tab._reach_n == 4, str(tab._reach_n))
    ck("　而且會講出來", "地形變了" in tab.status.text(), tab.status.text())

    print("\n不認得的動作")
    tab = make_tab([{"do": "walk", "to": [1, 1]}])
    tab._script.steps[0] = {"do": "fly"}                  # 繞過 validate 硬塞
    run(tab, 0.3)
    ck("★ 不認得的動作 → 停下來，不當作沒事跳過",
       not tab.run_cb.isChecked(), tab.status.text())

    print(f"\n通過 {PASS}　失敗 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
