"""對帳身分驗證離線測試——重演 2026-08-15 斷線事故（不碰遊戲、不碰記憶體）。

事故：斷網後所有分身退回登入畫面（pid 不變），一鍵登入把帳號填進
「第一台空著的視窗」→ 帳號跟視窗的配對洗牌；舊版對帳只認 pid，
結果黑狐的分頁拿著黑狐的設定指揮雪狐的角色。

這支用 offscreen Qt＋假遊戲層驗 ClientWatchMixin 的修法：
    斷線那拍不亂動、換帳號整頁重建、名字快取作廢重解、意向自動接回、
    同帳號重登（自動登入固定進第 1 隻）也要重解角色名。

用法：py tools\\watch_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import (QApplication, QCheckBox,  # noqa: E402
                               QTabWidget, QWidget)

from app.core import charname, preload                   # noqa: E402
from app.tabs import base_tab                            # noqa: E402

# --- 假遊戲層 ----------------------------------------------------------
# pid → (標題裡的帳號；登入畫面是 ""，角色名)。改這張表＝改「遊戲現況」。
REALITY: dict[int, tuple[str, str]] = {}


def _title(pid: int) -> str:
    acct = REALITY[pid][0]
    return (f"Angels Online Global - {acct}(雅典娜-3)" if acct
            else "Angels Online Global")


def fake_windows():
    return [types.SimpleNamespace(pid=pid, hwnd=pid * 10, title=_title(pid))
            for pid in sorted(REALITY)]


def fake_name_of(pid, scanner=None, account="", force=False):
    """骨架照抄 preload.name_of：快取優先；「掃記憶體」改成查 REALITY。"""
    got = preload._names.get(pid)
    if got and not (force and scanner is not None):
        return got
    if scanner is not None:
        got = REALITY.get(pid, ("", ""))[1] or account
        preload._names[pid] = got
        return got
    return account


class InlineThread:
    """背景讀名字改成同步跑，測試才是決定性的。"""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


# ⚠ 假物件要 patch 進「用到它的模組」的命名空間（base_tab／本檔）
preload.windows = fake_windows
preload.name_of = fake_name_of
base_tab.threading = types.SimpleNamespace(Thread=InlineThread)


class FakeTab(base_tab.ClientWatchMixin, QWidget):
    """最小子類別：只帶 mixin 合約要求的東西（_pages / tabs / 兩個掛鉤）。"""

    def __init__(self):
        super().__init__()
        self._watch_init()
        self.tabs = QTabWidget(self)
        self._pages: dict[int, QWidget] = {}
        self.rebuilt: list[int] = []

    def _client_new(self, w):
        acct = charname.account_from_title(w.title)
        nm = preload.name_of(w.pid, account=acct)     # 跟 farm/produce 同款：只查快取
        page = QWidget()
        page.account, page.char_name = acct, nm
        page.sc = object()                            # 只被 _nm_worker 轉手
        page.run_cb = QCheckBox(page)
        page.run_cb.clicked.connect(lambda _on, p=page: self._note_intent(p))
        self._pages[w.pid] = page
        self.tabs.addTab(page, nm or acct or str(w.pid))
        self.rebuilt.append(w.pid)
        self._maybe_resume(page)

    def _client_gone(self, pid):
        page = self._pages.pop(pid)
        page.run_cb.setChecked(False)                 # 程式放勾：不准動到意向
        i = self.tabs.indexOf(page)
        if i >= 0:
            self.tabs.removeTab(i)


fails: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def main() -> int:
    QApplication.instance() or QApplication([])
    preload._names.clear()
    preload._states.clear()
    tab = FakeTab()

    # 第 0 幕：兩台在線（黑狐／雪狐）＋一台開在登入畫面
    REALITY[1] = ("acctA", "黑狐")
    REALITY[2] = ("acctB", "雪狐")
    REALITY[3] = ("", "")
    preload._names[1] = "黑狐"                        # 模擬開機預讀
    preload._names[2] = "雪狐"
    tab._watch_tick()
    check("三台都建了頁", set(tab._pages) == {1, 2, 3})
    check("登入畫面那台帳號是空字串（不是整串標題）",
          tab._pages[3].account == "")
    check("pid1 掛黑狐、pid2 掛雪狐",
          (tab._pages[1].char_name, tab._pages[2].char_name) == ("黑狐", "雪狐"))

    # 使用者親手勾兩台開始掛機（模擬親手點要用 click() 不是 setChecked）
    tab._pages[1].run_cb.click()
    tab._pages[2].run_cb.click()
    check("意向記下 acctA/acctB", tab._intent == {"acctA": True, "acctB": True})

    # 第 1 幕：斷網——全部退回登入畫面（pid 不變）
    REALITY[1] = ("", "")
    REALITY[2] = ("", "")
    tab.rebuilt.clear()
    tab._watch_tick()
    check("斷線那拍不重建任何頁", tab.rebuilt == [])
    check("斷線那拍不動勾勾", tab._pages[1].run_cb.isChecked())
    check("記下待驗身分", {1, 2} <= tab._relog)

    # 第 2 幕：一鍵登入洗牌——帳號互換（事故重演）
    REALITY[1] = ("acctB", "雪狐")
    REALITY[2] = ("acctA", "黑狐")
    tab._watch_tick()
    check("兩頁都重建", set(tab.rebuilt) == {1, 2})
    check("pid1 換掛 acctB、pid2 換掛 acctA",
          (tab._pages[1].account, tab._pages[2].account) == ("acctB", "acctA"))
    tab._watch_tick()                                  # 收背景解出的名字
    check("角色名跟著換（不是撿快取裡上一個人的）",
          (tab._pages[1].char_name, tab._pages[2].char_name) == ("雪狐", "黑狐"))
    check("分頁標籤跟著換",
          tab.tabs.tabText(tab.tabs.indexOf(tab._pages[1])) == "雪狐")
    check("意向自動接回（兩台勾勾補上）",
          tab._pages[1].run_cb.isChecked() and tab._pages[2].run_cb.isChecked())

    # 第 3 幕：同帳號重登、但這次登進的是另一隻角色
    REALITY[1] = ("", "")
    tab._watch_tick()                                  # 斷線
    REALITY[1] = ("acctB", "北極狐")
    tab.rebuilt.clear()
    tab._watch_tick()                                  # 同帳號回來：不重建、名字重解
    check("同帳號重登不重建（掛機不被打斷）", tab.rebuilt == [])
    tab._watch_tick()                                  # 收名字
    check("角色名重解成北極狐", tab._pages[1].char_name == "北極狐")

    # 第 4 幕：關一台 → 收走、狀態不殘留
    del REALITY[3]
    tab._watch_tick()
    check("關掉的收走", 3 not in tab._pages and 3 not in tab._relog)

    if fails:
        print(f"共 {len(fails)} 項 FAIL")
        return 1
    print("OK 全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
