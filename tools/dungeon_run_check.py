"""自動刷副本的狀態機回歸測試（離線，不必開遊戲）。

    py tools\\dungeon_run_check.py

跑的是分頁**自己的**邏輯（`_run_step` / `_finish`），只把「跟遊戲講話」那幾支
換成替身（走路、點物件、送選項）。⚠ 這是 memory `test-via-button` 的教訓：
替身只換 I/O，邏輯要跑真的 —— 換掉整個模組就會測到替身。
"""
from __future__ import annotations

import os
import sys

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
    mons = [object()]
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
