"""強化裝備分頁：模擬背包挑一件裝備，用強化錘打到指定次數。

怎麼用
------
1. 上面選分身 → 中間的模擬背包只會列**背包裡可以強化的裝備**（身上穿的不列，
   所以不可能誤動你正在穿的東西）。
2. 滑鼠移到圖示上會顯示跟遊戲一樣的說明（名稱／數值／已強化次數／孔與寶石）。
3. 點一下選起來（黃色粗框），選好「強化到 +N」，按「強化錘強化」。

⚠⚠ **一般強化錘失敗 → 裝備直接消失**（使用者 2026-08-28 確認）。
所以按下去就是一路打到目標為止（使用者指定不要每發確認），中途只要
**裝備不見了或退等就立刻停**，下面的紀錄會用紅字寫清楚。

背後
----
* 讀：`app/game/gear.py`（已強化次數 +0x52、孔 +0x51、寶石 +0x3D、進階屬性 +0x0C）
* 送：`app/game/enhance.py` —— 就是「對物品使用物品」那一包（代號 0x2E），
  跟你自己在遊戲裡按確定送的完全一樣。
* 圖：`app/game/itemicon.py`（遊戲自己的圖示，編號從記憶體讀）
"""
from __future__ import annotations

import html
import time

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.core import charname, injector, preload, window as win
from app.core.memory import MemoryScanner
from app.game import bag, enhance, gear, itemicon, itemname, locate, move
from app.tabs.base_tab import BaseTab

# 模擬背包的樣子（照使用者給的 背包.png：深色格子牆＋圖示置中）
CELL = 46
PAD = 3
COLS = 10
CELL_BG = "#123039"
CELL_EDGE = "#2b6a7a"
PICK_EDGE = "#FFD400"          # 選起來的黃色粗框
PICK_WIDTH = 3

REFRESH_MS = 400               # 背包多久對一次帳
RUN_MS = 200                   # 強化狀態機多久跑一拍
HIST_MAX = 300

COLOUR_OF = {
    enhance.SUCCESS: "#7CFC7C",
    enhance.GONE: "#FF5555",
    enhance.DOWNGRADE: "#FF5555",
    enhance.BLOCKED: "#FFC864",
    enhance.UNKNOWN: "#FFC864",
    enhance.DONE: "#7CD8FF",
}


class BagGrid(QWidget):
    """模擬背包：一格一件裝備，滑過看說明、點一下選起來。"""

    picked = Signal(int)                    # 送出被選中的 serial

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._gears: list[gear.Gear] = []
        self._serial = 0
        self._hover = 0
        self.setMouseTracking(True)
        self.setMinimumHeight(CELL * 3)

    # ------------------------------------------------------------------
    def set_gears(self, gears: list[gear.Gear]) -> None:
        self._gears = gears
        if self._serial and all(g.serial != self._serial for g in gears):
            self._serial = 0                # 選的那件不見了（強化失敗）
            self.picked.emit(0)
        rows = max(1, (len(gears) + COLS - 1) // COLS)
        self.setMinimumHeight(rows * CELL + PAD * 2)
        self.update()

    def selected(self) -> gear.Gear | None:
        for g in self._gears:
            if g.serial == self._serial:
                return g
        return None

    def _at(self, pos) -> gear.Gear | None:
        col = (pos.x() - PAD) // CELL
        row = (pos.y() - PAD) // CELL
        if col < 0 or col >= COLS or row < 0:
            return None
        idx = row * COLS + col
        return self._gears[idx] if 0 <= idx < len(self._gears) else None

    # ------------------------------------------------------------------
    def paintEvent(self, _ev) -> None:                   # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        rows = max(1, (len(self._gears) + COLS - 1) // COLS)
        for row in range(rows):
            for col in range(COLS):
                x = PAD + col * CELL
                y = PAD + row * CELL
                cell = QRect(x, y, CELL - 2, CELL - 2)
                p.fillRect(cell, QColor(CELL_BG))
                p.setPen(QPen(QColor(CELL_EDGE), 1))
                p.drawRect(cell)
                idx = row * COLS + col
                if idx >= len(self._gears):
                    continue
                g = self._gears[idx]
                pm = itemicon.pixmap(g.icon_id)
                if pm is not None and not pm.isNull():
                    w = min(pm.width(), CELL - 8)
                    h = min(pm.height(), CELL - 8)
                    p.drawPixmap(x + (CELL - 2 - w) // 2,
                                 y + (CELL - 2 - h) // 2,
                                 pm.scaled(w, h, Qt.KeepAspectRatio,
                                           Qt.SmoothTransformation))
                else:
                    # 沒有圖就顯示名字前兩個字 —— 安全退化，不抓別張圖頂替
                    p.setPen(QColor("#DDDDDD"))
                    p.drawText(cell, Qt.AlignCenter, g.name[:2])
                if g.enhance:
                    # 右下角標「+N」。先用黑底描一遍再畫紫字，
                    # 不然疊在亮色圖示上會看不見（背包.png 的數量也是這樣描邊）。
                    box = cell.adjusted(0, 0, -3, -2)
                    p.setPen(QColor(0, 0, 0, 200))
                    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        p.drawText(box.translated(dx, dy),
                                   Qt.AlignRight | Qt.AlignBottom,
                                   f"+{g.enhance}")
                    p.setPen(QColor("#E0B0FF"))
                    p.drawText(box, Qt.AlignRight | Qt.AlignBottom,
                               f"+{g.enhance}")
                if g.serial == self._serial:
                    p.setPen(QPen(QColor(PICK_EDGE), PICK_WIDTH))
                    p.drawRect(cell.adjusted(1, 1, -1, -1))
        p.end()

    def mousePressEvent(self, ev) -> None:               # noqa: N802
        g = self._at(ev.position().toPoint())
        self._serial = g.serial if g else 0
        self.picked.emit(self._serial)
        self.update()

    def mouseMoveEvent(self, ev) -> None:                # noqa: N802
        g = self._at(ev.position().toPoint())
        if g is None:
            QToolTip.hideText()
            self._hover = 0
            return
        # ⚠ 只有換格子才重畫提示 —— 每次滑鼠移動都 showText 會讓提示一直閃。
        if g.serial != self._hover:
            self._hover = g.serial
            QToolTip.showText(ev.globalPosition().toPoint(),
                              _tooltip_html(g), self)

    def sizeHint(self) -> QSize:                         # noqa: N802
        rows = max(1, (len(self._gears) + COLS - 1) // COLS)
        return QSize(COLS * CELL + PAD * 2, rows * CELL + PAD * 2)


def _tooltip_html(g: gear.Gear) -> str:
    """把 `gear.tooltip()` 那幾行變成提示框 HTML（顏色照遊戲）。"""
    parts = []
    for text, colour in gear.tooltip(g):
        if not text:
            parts.append("<div style='height:6px'></div>")
            continue
        parts.append(f"<div style='color:{colour}'>{html.escape(text)}</div>")
    return ("<div style='background:#0d1b21; padding:4px'>"
            + "".join(parts) + "</div>")


class EnhanceTab(BaseTab):
    TAB_TITLE = "強化裝備"
    ORDER = 49                       # 排在活動（48）後面

    # ------------------------------------------------------------------
    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._sig: tuple | None = None
        self._run: enhance.Run | None = None

        root = QVBoxLayout(self)

        hint = QLabel(
            "只列「背包裡」可以強化的裝備（身上穿的不會出現）。滑鼠移上去看說明，"
            "點一下選起來，選好要強化到幾次再按下面的按鈕。"
            "⚠ 一般強化錘失敗會讓裝備直接消失，按下去就會一路打到目標為止。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        self.who.currentIndexChanged.connect(self._on_who_changed)
        bar.addWidget(self.who)
        reload_btn = QPushButton("重新整理")
        reload_btn.setToolTip("重新列出目前開著的遊戲分身。")
        reload_btn.clicked.connect(lambda: self.reload_instances(True))
        bar.addWidget(reload_btn)
        bar.addStretch(1)
        self.hammer_lbl = QLabel("強化錘 —")
        self.hammer_lbl.setStyleSheet("font-weight: bold;")
        bar.addWidget(self.hammer_lbl)
        root.addLayout(bar)

        box = QGroupBox("模擬背包（可強化的裝備）")
        box_lay = QVBoxLayout(box)
        self.grid = BagGrid()
        self.grid.picked.connect(self._on_picked)
        area = QScrollArea()
        area.setWidget(self.grid)
        area.setWidgetResizable(True)
        area.setMinimumHeight(CELL * 3 + 12)
        box_lay.addWidget(area)
        root.addWidget(box)

        act = QHBoxLayout()
        self.pick_lbl = QLabel("還沒選裝備")
        act.addWidget(self.pick_lbl)
        act.addStretch(1)
        act.addWidget(QLabel("強化到 +"))
        self.target = QSpinBox()
        self.target.setRange(1, enhance.MAX_LEVEL)
        self.target.setFixedWidth(64)
        act.addWidget(self.target)
        self.go_btn = QPushButton("強化錘強化")
        self.go_btn.setToolTip(
            "對選起來的裝備一直使用強化錘，直到強化次數到達目標。\n"
            "⚠ 失敗時裝備會消失，一旦消失或退等就會立刻停下來。")
        self.go_btn.clicked.connect(self._on_go)
        act.addWidget(self.go_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        act.addWidget(self.stop_btn)
        root.addLayout(act)

        self.status = QLabel("　")
        root.addWidget(self.status)

        hist_box = QGroupBox("紀錄")
        hist_lay = QVBoxLayout(hist_box)
        self.hist = QListWidget()
        self.hist.setMinimumHeight(120)
        hist_lay.addWidget(self.hist)
        root.addWidget(hist_box, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_MS)
        self._run_timer = QTimer(self)
        self._run_timer.timeout.connect(self._run_tick)

        self._update_buttons()

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self, force_names: bool = False) -> None:
        self._on_stop()
        self.who.blockSignals(True)
        self.who.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
        seen = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:                            # noqa: BLE001
                continue
            try:
                locate.warm(sc)
            except Exception:                            # noqa: BLE001
                pass
            acc = charname.account_from_title(w.title)
            self._scanners[w.pid] = sc
            self.who.addItem(
                f"{preload.name_of(w.pid, sc, acc, force=force_names)}（{acc}）",
                w.pid)
        self.who.blockSignals(False)
        self._sig = None
        if not self._scanners:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
            self.grid.set_gears([])
            return
        self.status.setText(f"找到 {len(self._scanners)} 個分身")
        self._refresh()

    def _cur(self):
        pid = self.who.currentData()
        return pid, self._scanners.get(pid) if pid else None

    def _on_who_changed(self) -> None:
        self._on_stop()
        self._sig = None
        self._refresh()

    def _mover(self, pid: int) -> move.Mover | None:
        """拿這台分身的跳板。⚠ 一定要走 move.acquire()，不要自己 new。"""
        mv = self._movers.get(pid)
        if mv is not None and mv.active:
            return mv
        try:
            mv = move.acquire(pid, injector.process_path(pid), self)
        except Exception as exc:                         # noqa: BLE001
            self._movers.pop(pid, None)
            self.status.setText(f"⚠ 無法安裝跳板：{exc}")
            return None
        self._movers[pid] = mv
        return mv

    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        pid, sc = self._cur()
        if sc is None:
            return
        gears, complete = gear.in_bag(sc)
        got = enhance.find_hammer(sc)
        if got:
            self.hammer_lbl.setText(f"強化錘 {got[1]} 個")
        else:
            self.hammer_lbl.setText("強化錘 0 個" if complete else "強化錘 —")
        sig = (pid, tuple((g.serial, g.enhance, g.slot) for g in gears))
        if sig != self._sig:
            self._sig = sig
            self.grid.set_gears(gears)
            self._on_picked(self.grid.selected().serial
                            if self.grid.selected() else 0)
        self._update_buttons()

    def _on_picked(self, _serial: int) -> None:
        g = self.grid.selected()
        if g is None:
            self.pick_lbl.setText("還沒選裝備")
        else:
            self.pick_lbl.setText(
                f"已選：{g.name}（目前 +{g.enhance}）")
            low = min(g.enhance + 1, enhance.MAX_LEVEL)
            self.target.setMinimum(low)
            if self.target.value() < low:
                self.target.setValue(low)
        self._update_buttons()

    def _update_buttons(self) -> None:
        running = self._run is not None and not self._run.done
        g = self.grid.selected()
        ok = (g is not None and not running
              and g.enhance < enhance.MAX_LEVEL)
        self.go_btn.setEnabled(bool(ok))
        self.stop_btn.setEnabled(running)
        self.who.setEnabled(not running)

    # ------------------------------------------------------------------
    def _log(self, text: str, colour: str = "#DDDDDD") -> None:
        item = QListWidgetItem(f"{time.strftime('%H:%M:%S')}　{text}")
        item.setForeground(QColor(colour))
        self.hist.insertItem(0, item)          # ★ 追加式清單用插列，不要重畫整表
        while self.hist.count() > HIST_MAX:
            self.hist.takeItem(self.hist.count() - 1)

    def _on_go(self) -> None:
        pid, sc = self._cur()
        g = self.grid.selected()
        if sc is None or g is None:
            return
        mv = self._mover(pid)
        if mv is None:
            return
        target = self.target.value()
        if target <= g.enhance:
            self.status.setText("目標比目前的次數還低 —— 不用打")
            return
        self._run = enhance.Run(sc, mv, g.slot, g.serial, target, g.name)
        self._log(f"開始：{g.name} +{g.enhance} → +{target}", "#7CD8FF")
        self.status.setText(f"強化中… {g.name} → +{target}")
        self._run_timer.start(RUN_MS)
        self._update_buttons()

    def _on_stop(self) -> None:
        if self._run is not None and not self._run.done:
            self._log("手動停止", "#FFC864")
        self._run = None
        self._run_timer.stop()
        self.status.setText("　")
        self._update_buttons()

    def _run_tick(self) -> None:
        run = self._run
        if run is None:
            self._run_timer.stop()
            return
        for ev in run.tick():
            self._log(ev.text, COLOUR_OF.get(ev.kind, "#DDDDDD"))
        if run.done:
            self._run_timer.stop()
            self._run = None
            self.status.setText("　")
            self._sig = None                 # 逼下一拍重畫（東西可能不見了）
            self._update_buttons()

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        self._run_timer.stop()
        self._timer.stop()
        for mv in self._movers.values():
            try:
                mv.release()
            except Exception:                            # noqa: BLE001
                pass
        self._movers.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
