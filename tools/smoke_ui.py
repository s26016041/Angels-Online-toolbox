"""介面冒煙測試：把每個分頁**真的建出來**，抓 import 漏掉／名字打錯這類錯。

    py tools\\smoke_ui.py

## 為什麼需要這支

`py_compile` 只檢查語法，**抓不到 `NameError`** —— 2026-08-07 就這樣把
「farm_tab 用了 terrain 卻沒 import」送到使用者手上：程式跑得起來，
但一切到自動掛機分頁就整頁炸掉（`NameError: name 'terrain' is not defined`）。
那一行在建構式裡，只有真的把分頁生出來才會執行到。

所以這支做的事就是：離屏開一個 Qt、把每個分頁 `建構 → on_show() → on_close()`
跑一遍。**不需要遊戲開著**（偵測不到分身就是空清單，照樣走完建構流程）。

⚠ 純離線：不寫遊戲記憶體、不注入、不送封包。沒開遊戲也能跑。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# 一定要在 import Qt 之前設：沒有這個會真的開視窗（CI／遠端就掛了）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from app.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    bad: list[tuple[str, str]] = []

    try:
        win = MainWindow()
    except Exception:                                  # noqa: BLE001
        print("✘ 主視窗建不起來：")
        traceback.print_exc()
        return 1

    tabs = win.tabs if hasattr(win, "tabs") else None
    if tabs is None:
        print("✘ 找不到主視窗的分頁容器（tabs），這支測試要跟著改")
        return 1

    n = tabs.count()
    print(f"共 {n} 個分頁\n")
    for i in range(n):
        name = tabs.tabText(i)
        page = tabs.widget(i)
        try:
            # ★★ **直接呼叫 on_show()**，不要只用 setCurrentIndex()。
            #   分頁是在 on_show() 裡才真的建出來的，而它是掛在 Qt 訊號上的
            #   —— 訊號槽裡丟出來的例外，PySide6 只會把 traceback **印出來**，
            #   不會往外傳。靠切分頁的話，畫面上錯得一塌糊塗，這支測試卻
            #   一路 ✔ 到底（2026-08-07 第一版就是這樣，差點又放過同一個 bug）。
            if hasattr(page, "on_show"):
                page.on_show()
            tabs.setCurrentIndex(i)
            app.processEvents()
            print(f"✔ {name}")
        except Exception as exc:                       # noqa: BLE001
            bad.append((name, traceback.format_exc()))
            print(f"✘ {name}：{type(exc).__name__}: {exc}")

    # 收尾：每個分頁的 on_close 也要能跑（會停執行緒、還原 hook）
    for i in range(n):
        page = tabs.widget(i)
        if hasattr(page, "on_close"):
            try:
                page.on_close()
            except Exception as exc:                   # noqa: BLE001
                bad.append((tabs.tabText(i) + " on_close",
                            traceback.format_exc()))
                print(f"✘ {tabs.tabText(i)} on_close：{exc}")

    if bad:
        print(f"\n{len(bad)} 個分頁有問題：\n")
        for name, tb in bad:
            print(f"--- {name} ---\n{tb}")
        return 1
    print("\n全部分頁都建得起來。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
