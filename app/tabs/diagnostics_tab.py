"""診斷分頁。

用來驗證「全背景自動化」的兩個關鍵技術，對天使之戀到底通不通：

  A. 背景擷取：PrintWindow 抓指定視窗畫面，會不會是全黑？
  B. 背景送鍵：PostMessage 送的文字/按鍵，遊戲收不收得到？

操作流程：
  1. 開著遊戲（可多開），按「重新整理」列出視窗。
  2. 用標題關鍵字過濾找出遊戲視窗，選一個。
  3. 按「擷取畫面」→ 會存成 png 並回報是否全黑。
  4. 在遊戲的帳號欄位可輸入狀態下，按「送出測試文字」看遊戲有沒有出現文字。

兩項都通 → 全背景自動化可行；有一項不通 → 需要調整策略。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core import window as win
from app.tabs.base_tab import GROUP_DEV, BaseTab


class DiagnosticsTab(BaseTab):
    TAB_TITLE = "視窗診斷"
    GROUP = GROUP_DEV
    ORDER = 90

    def build_ui(self) -> None:
        root = QVBoxLayout(self)

        root.addWidget(
            QLabel(
                "驗證全背景自動化是否可行：先列出視窗，選中遊戲視窗後，"
                "分別測試「背景擷取」與「背景送鍵」。"
            )
        )

        # 過濾列
        filter_row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("依標題關鍵字過濾（可留空）")
        refresh_btn = QPushButton("重新整理視窗清單")
        refresh_btn.clicked.connect(self.refresh_windows)
        filter_row.addWidget(self.filter_edit)
        filter_row.addWidget(refresh_btn)
        root.addLayout(filter_row)

        # 視窗表格
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["HWND", "PID", "標題", "類別", "尺寸"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        root.addWidget(self.table)

        # 子視窗區（很多程式的輸入由子視窗接收）
        child_btn_row = QHBoxLayout()
        list_child_btn = QPushButton("列出選取視窗的子視窗")
        list_child_btn.clicked.connect(self.refresh_children)
        child_btn_row.addWidget(QLabel("子視窗（送鍵目標優先用這裡選的）："))
        child_btn_row.addWidget(list_child_btn)
        root.addLayout(child_btn_row)

        self.child_table = QTableWidget(0, 4)
        self.child_table.setHorizontalHeaderLabels(
            ["HWND", "標題", "類別", "尺寸"]
        )
        self.child_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.child_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.child_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.child_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        root.addWidget(self.child_table)

        # 測試 A：擷取
        cap_row = QHBoxLayout()
        capture_btn = QPushButton("A. 擷取選取視窗畫面")
        capture_btn.clicked.connect(self.test_capture)
        cap_row.addWidget(capture_btn)
        root.addLayout(cap_row)

        # 測試 B：每 2 秒重複背景送鍵（可邊送邊切換焦點觀察）
        send_row = QHBoxLayout()
        self.test_text = QLineEdit("test123")
        self.start_btn = QPushButton("B. 開始（每2秒送一次）")
        self.start_btn.clicked.connect(self.start_sending)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_sending)
        self.stop_btn.setEnabled(False)
        send_row.addWidget(QLabel("測試文字："))
        send_row.addWidget(self.test_text)
        send_row.addWidget(self.start_btn)
        send_row.addWidget(self.stop_btn)
        root.addLayout(send_row)

        self.status = QLabel("就緒")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)

        self._windows: list[win.WindowInfo] = []
        self._children: list[win.WindowInfo] = []

        # 每 2 秒重複送鍵用的計時器
        self._send_timer = QTimer(self)
        self._send_timer.setInterval(2000)
        self._send_timer.timeout.connect(self._tick)
        self._send_count = 0
        self._send_target_win: win.WindowInfo | None = None

        self.refresh_windows()

    # ------------------------------------------------------------------
    def refresh_windows(self) -> None:
        keyword = self.filter_edit.text().strip() or None
        self._windows = win.enumerate_windows(title_contains=keyword)
        self.table.setRowCount(len(self._windows))
        for row, w in enumerate(self._windows):
            self.table.setItem(row, 0, QTableWidgetItem(str(w.hwnd)))
            self.table.setItem(row, 1, QTableWidgetItem(str(w.pid)))
            self.table.setItem(row, 2, QTableWidgetItem(w.title))
            self.table.setItem(row, 3, QTableWidgetItem(w.class_name))
            self.table.setItem(
                row, 4, QTableWidgetItem(f"{w.width}x{w.height}")
            )
        self.status.setText(f"找到 {len(self._windows)} 個視窗")

    def _selected_window(self) -> win.WindowInfo | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "請先在清單中選一個視窗。")
            return None
        return self._windows[rows[0].row()]

    def refresh_children(self) -> None:
        w = self._selected_window()
        if not w:
            return
        self._children = win.enumerate_child_windows(w.hwnd)
        self.child_table.setRowCount(len(self._children))
        for row, c in enumerate(self._children):
            self.child_table.setItem(row, 0, QTableWidgetItem(str(c.hwnd)))
            self.child_table.setItem(row, 1, QTableWidgetItem(c.title))
            self.child_table.setItem(row, 2, QTableWidgetItem(c.class_name))
            self.child_table.setItem(
                row, 3, QTableWidgetItem(f"{c.width}x{c.height}")
            )
        self.status.setText(
            f"HWND {w.hwnd} 底下有 {len(self._children)} 個子視窗。"
            "可逐一選子視窗測試送鍵，找出真正吃輸入的那個。"
        )

    def _send_target(self) -> win.WindowInfo | None:
        """送鍵目標：優先用子視窗表格選取的，否則用主視窗選取的。"""
        child_rows = self.child_table.selectionModel().selectedRows()
        if child_rows and self._children:
            return self._children[child_rows[0].row()]
        return self._selected_window()

    def test_capture(self) -> None:
        w = self._selected_window()
        if not w:
            return
        try:
            img = win.capture_window(w.hwnd)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "擷取失敗", f"擷取時發生錯誤：\n{exc}")
            return
        if img is None:
            self.status.setText("擷取失敗：視窗尺寸無效")
            return

        out_dir = os.path.join(os.getcwd(), "captures")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"capture_{w.hwnd}_{int(time.time())}.png")
        img.save(path)

        black = win.is_mostly_black(img)
        verdict = (
            "⚠ 幾乎全黑 → PrintWindow 對此視窗無效（可能是 DirectX）"
            if black
            else "✓ 有內容 → 背景擷取可行"
        )
        self.status.setText(f"已存檔：{path}\n判定：{verdict}")

    def start_sending(self) -> None:
        target = self._send_target()
        if not target:
            return
        self._send_target_win = target
        self._send_count = 0
        self._send_timer.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText(
            f"開始每 2 秒送鍵至 {target.class_name} (HWND {target.hwnd})。\n"
            "現在可以試著：先讓遊戲在背景 → 觀察有沒有進字；"
            "再點一下遊戲讓它取得焦點 → 看是否才開始進字（代表需要焦點）。"
        )

    def stop_sending(self) -> None:
        self._send_timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText(f"已停止。總共送了 {self._send_count} 次。")

    def _tick(self) -> None:
        target = self._send_target_win
        if not target:
            self.stop_sending()
            return
        try:
            win.send_text(target.hwnd, self.test_text.text())
            win.send_key(target.hwnd, win.VK_RETURN)
        except Exception as exc:  # noqa: BLE001
            self.stop_sending()
            self.status.setText(f"送鍵失敗，已停止：{exc}")
            return
        self._send_count += 1
        fg = ""
        try:
            import win32gui

            fg_hwnd = win32gui.GetForegroundWindow()
            fg = win32gui.GetWindowText(fg_hwnd)
        except Exception:
            pass
        self.status.setText(
            f"已送第 {self._send_count} 次至 HWND {target.hwnd}"
            f"（目前前景視窗：{fg[:30]!r}）"
        )

    def on_close(self) -> None:
        self._send_timer.stop()
