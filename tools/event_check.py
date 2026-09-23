"""活動分頁離線測試（自動烤肉）—— offscreen Qt ＋ 假跳板。

    py tools\\event_check.py       （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的規格（2026-09-23 使用者定）：
① 送的是 talkaction **動作碼 2**（擷取 `0x5D952D 參數 (2, …)`）。
② 間隔照設定（毫秒）送，執行中改間隔立即生效。
③ 暫停就停；跳板失效（分身關了）自己停、UI 顯示。
④ 送不出去只記失敗次數，不停。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.game import sell                                  # noqa: E402
from app.tabs import event_tab                             # noqa: E402

FAIL = []


def check(name, ok):
    if not ok:
        FAIL.append(name)
        print("FAIL", name)


class FakeMover:
    def __init__(self):
        self.active = True
        self.codes = []
        self.ok = True


def main():
    sell.TALK_FN = sell.TALK_FN or 0x5DA91E
    real_talk = sell.talk

    def fake_talk(mv, code=0):
        mv.codes.append(code)
        return mv.ok
    sell.talk = fake_talk
    try:
        mv = FakeMover()
        w = event_tab.BbqWorker(mv, 50)
        w.start()
        time.sleep(0.52)
        n = w.sent
        check("① 動作碼 2", mv.codes and set(mv.codes) == {2})
        check(f"② 50ms 約 10 包（實際 {n}）", 7 <= n <= 13)
        w.interval_ms = 200
        time.sleep(0.05)
        a = w.sent
        time.sleep(0.6)
        check(f"② 改 200ms 生效（{w.sent - a}）", 2 <= w.sent - a <= 4)
        w.stop()
        time.sleep(0.25)
        b = w.sent
        time.sleep(0.3)
        check("③ 暫停就停", w.sent == b and not w.alive)

        mv2 = FakeMover()
        mv2.ok = False
        w2 = event_tab.BbqWorker(mv2, 20)
        w2.start()
        time.sleep(0.2)
        check("④ 失敗照送不停", w2.failed >= 5 and w2.alive)
        mv2.active = False
        time.sleep(0.1)
        check("③ 跳板失效自己停", w2.dead and not w2.alive)
        check("最小間隔下限", event_tab.BbqWorker(mv, 1).interval_ms
              == event_tab.MS_MIN)
    finally:
        sell.talk = real_talk

    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    tab = event_tab.EventTab.__new__(event_tab.EventTab)
    check("分頁類別存在", tab is not None)

    if FAIL:
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
