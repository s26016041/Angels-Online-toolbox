"""強化裝備分頁：模擬背包挑一件裝備，用強化錘打到指定次數。

怎麼用
------
1. 上面選分身 → 中間的模擬背包只會列**背包裡可以強化的裝備**（身上穿的不列，
   所以不可能誤動你正在穿的東西）。
2. 滑鼠移到圖示上會顯示跟遊戲一樣的說明（名稱／數值／已強化次數／孔與寶石）。
3. 點一下選起來（黃色粗框），在「強化到 +」那排小方塊點要打到的數字，
   按「強化錘強化」。

「強化到 +N」「打孔到 N 孔」都是小方塊（使用者 2026-09-09 指定，不打字）：
點哪一格就是它，**程式不會自己幫你改**（選到已經強化過的裝備也不動你的選擇），
選的數字比裝備現況還低就按不出東西 —— 下面紀錄會寫「選擇強化等級過低／
選擇孔數過低」。上次選的存在 config（`enhance.target`／`enhance.hole_target`），
重開就是那一格。

⚠⚠ **一般強化錘失敗 → 裝備直接消失**（使用者 2026-08-28 確認）。
所以按下去就是一路打到目標為止（使用者指定不要每發確認），中途只要
**裝備不見了或退等就立刻停**，下面的紀錄會用紅字寫清楚。

自動打孔（2026-09-03）
---------------------
選好裝備 → 設「打孔到 N 孔」與「寶石等限 ≤ L 級」→ 按「自動打孔」：
用一般 N星打孔錘打孔（祝福錘不用；星級要夠這件裝備的等級），有空孔就先拿
等限 ≤ L 的寶石鑲進去（好寶石不動），打到目標孔數為止，**最後一孔留空**。
規則與封包出處見 `app/game/holes.py`。

背後
----
* 讀：`app/game/gear.py`（已強化次數 +0x52、孔 +0x51、寶石 +0x3D、進階屬性 +0x0C）
* 送：`app/game/enhance.py` —— 就是「對物品使用物品」那一包（代號 0x2E），
  跟你自己在遊戲裡按確定送的完全一樣；打孔／鑲嵌也是同一包（`holes.py`）。
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
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from dataclasses import dataclass
from typing import Callable

from app import theme
from app.config import config
from app.core import charname, injector, preload, window as win
from app.core.memory import MemoryScanner
from app.game import (bag, enhance, gear, holes, itemdesc, itemicon, itemname,
                      locate, move)
from app.tabs.base_tab import GROUP_CHORES, BaseTab

# 模擬背包的樣子（照使用者給的 背包.png：深色格子牆＋圖示置中）
CELL = 46
PAD = 3
COLS = 10
CELL_BG = "#123039"
CELL_EDGE = "#2b6a7a"
PICK_EDGE = "#FFD400"          # 選起來的黃色粗框
PICK_WIDTH = 3

REFRESH_MS = 400               # 背包多久對一次帳
RUN_MS = 50                    # 打孔／鑲嵌多久看一次結果（使用者 2026-09-25：跟強化一樣快）
STRIKE_MS = 50                 # 強化錘多久看一次結果（使用者 2026-09-23 嫌慢；讀一格背包只要幾毫秒）
HIST_MAX = 300
# 選裝備時「寶石等限 ≤」自動填成 裝備等級 − 這個數（使用者 2026-09-03 定 15 → 同日改 10），仍可手改
GEM_CAP_BELOW = 10
PICK_CELL = 28                 # 「強化到 +N」「打孔到 N 孔」小方塊的邊長

COLOUR_OF = {
    enhance.SUCCESS: "#7CFC7C",
    enhance.GONE: "#FF5555",
    enhance.DOWNGRADE: "#FF5555",
    enhance.BLOCKED: "#FFC864",
    enhance.UNKNOWN: "#FFC864",
    enhance.DONE: "#7CD8FF",
}


@dataclass
class Cell:
    """模擬背包的一格（裝備背包與寶石背包共用同一種畫法）。"""

    key: int                        # 選取／hover 認這個：裝備＝serial、寶石＝種類 ID
    icon_id: int
    name: str
    payload: object = None          # 選起來要交回去的東西（gear.Gear／holes.Gem）
    badge: str = ""                 # 右下角小字：裝備 "+N"、寶石 "×k"
    badge_colour: str = "#E0B0FF"
    tooltip: Callable[[], str] | None = None   # 滑過才算（裝備的提示要讀範本）


class IconGrid(QWidget):
    """模擬背包：一格一個東西，滑過看說明、點一下選起來。有幾個就畫幾格。

    `multi=True`（使用者 2026-09-26 要的多選）：點一下＝加入／取消，選的順序就是
    處理順序，格子左上角標 1、2、3…。`selected()`／`selected_cell()` 回**最後點的**
    那一件（單選時的用法照舊能用）；整批用 `selected_cells()`。
    """

    picked = Signal(int)                    # 送出被點的 key（0 ＝ 沒選）

    def __init__(self, empty_text: str, parent: QWidget | None = None,
                 multi: bool = False) -> None:
        super().__init__(parent)
        self._cells: list[Cell] = []
        self._key = 0                       # 單選：選的那格；多選：最後點的那格
        self._keys: list[int] = []          # 多選：依點選順序
        self._multi = multi
        self._hover = 0
        self._empty = empty_text
        self.setMouseTracking(True)
        self.setMinimumHeight(CELL + PAD * 2)

    # ------------------------------------------------------------------
    def set_cells(self, cells: list[Cell]) -> None:
        self._cells = cells
        have = {c.key for c in cells}
        if self._multi:
            keep = [k for k in self._keys if k in have]
            if keep != self._keys:          # 選的有些不見了（打掉了／用完了）
                self._keys = keep
                if self._key not in have:
                    self._key = keep[-1] if keep else 0
                self.picked.emit(self._key)
        elif self._key and self._key not in have:
            self._key = 0                   # 選的那個不見了（打掉了／用完了）
            self.picked.emit(0)
        rows = max(1, (len(cells) + COLS - 1) // COLS)
        self.setMinimumHeight(rows * CELL + PAD * 2)
        self.update()

    def select(self, key: int) -> bool:
        """程式端選一格（例如把上次選的寶石找回來）；不在清單裡回 False。"""
        if not any(c.key == key for c in self._cells):
            return False
        self._key = key
        if self._multi and key not in self._keys:
            self._keys.append(key)
        self.picked.emit(key)
        self.update()
        return True

    def select_keys(self, keys) -> None:
        """多選：整批換成這些（照給的順序）；清單裡沒有的略過。"""
        have = {c.key for c in self._cells}
        self._keys = [k for k in keys if k in have]
        self._key = self._keys[-1] if self._keys else 0
        self.picked.emit(self._key)
        self.update()

    def clear_selection(self) -> None:
        self._keys = []
        self._key = 0
        self.picked.emit(0)
        self.update()

    def cells(self) -> list[Cell]:
        return list(self._cells)

    def selected_cell(self) -> Cell | None:
        key = self._key
        if self._multi and key not in self._keys:
            key = self._keys[-1] if self._keys else 0
        for c in self._cells:
            if c.key == key:
                return c
        return None

    def selected(self):
        c = self.selected_cell()
        return c.payload if c is not None else None

    def selected_cells(self) -> list[Cell]:
        """多選：依點選順序的格子；單選：有選就一格。"""
        by = {c.key: c for c in self._cells}
        if not self._multi:
            c = by.get(self._key)
            return [c] if c is not None else []
        return [by[k] for k in self._keys if k in by]

    def _at(self, pos) -> Cell | None:
        col = (pos.x() - PAD) // CELL
        row = (pos.y() - PAD) // CELL
        if col < 0 or col >= COLS or row < 0:
            return None
        idx = row * COLS + col
        return self._cells[idx] if 0 <= idx < len(self._cells) else None

    # ------------------------------------------------------------------
    def paintEvent(self, _ev) -> None:                   # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        if not self._cells:
            # ★ 沒東西就不畫任何格子（使用者 2026-09-03：不要有空的背景框）
            p.setPen(QColor("#888888"))
            p.drawText(QRect(PAD, PAD, COLS * CELL, CELL), Qt.AlignVCenter,
                       self._empty)
            p.end()
            return
        # 有幾個就畫幾格，不補滿整列
        for idx, c in enumerate(self._cells):
            row, col = divmod(idx, COLS)
            x = PAD + col * CELL
            y = PAD + row * CELL
            cell = QRect(x, y, CELL - 2, CELL - 2)
            p.fillRect(cell, QColor(CELL_BG))
            p.setPen(QPen(QColor(CELL_EDGE), 1))
            p.drawRect(cell)
            pm = itemicon.pixmap(c.icon_id)
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
                p.drawText(cell, Qt.AlignCenter, c.name[:2])
            if c.badge:
                # 右下角小字。先用黑底描一遍再畫，
                # 不然疊在亮色圖示上會看不見（背包.png 的數量也是這樣描邊）。
                box = cell.adjusted(0, 0, -3, -2)
                p.setPen(QColor(0, 0, 0, 200))
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    p.drawText(box.translated(dx, dy),
                               Qt.AlignRight | Qt.AlignBottom, c.badge)
                p.setPen(QColor(c.badge_colour))
                p.drawText(box, Qt.AlignRight | Qt.AlignBottom, c.badge)
            if self._multi:
                if c.key in self._keys:
                    p.setPen(QPen(QColor(PICK_EDGE), PICK_WIDTH))
                    p.drawRect(cell.adjusted(1, 1, -1, -1))
                    # 左上角順序號碼（黑底描邊，同右下角數字）
                    num = str(self._keys.index(c.key) + 1)
                    box = cell.adjusted(4, 2, 0, 0)
                    p.setPen(QColor(0, 0, 0, 220))
                    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        p.drawText(box.translated(dx, dy),
                                   Qt.AlignLeft | Qt.AlignTop, num)
                    p.setPen(QColor(PICK_EDGE))
                    p.drawText(box, Qt.AlignLeft | Qt.AlignTop, num)
            elif c.key == self._key:
                p.setPen(QPen(QColor(PICK_EDGE), PICK_WIDTH))
                p.drawRect(cell.adjusted(1, 1, -1, -1))
        p.end()

    def mousePressEvent(self, ev) -> None:               # noqa: N802
        c = self._at(ev.position().toPoint())
        if self._multi:
            if c is None:
                return                      # 點空白不動選取（多選不小心清掉很痛）
            if c.key in self._keys:
                self._keys.remove(c.key)
                self._key = self._keys[-1] if self._keys else 0
            else:
                self._keys.append(c.key)
                self._key = c.key
            self.picked.emit(c.key)
            self.update()
            return
        self._key = c.key if c else 0
        self.picked.emit(self._key)
        self.update()

    def mouseMoveEvent(self, ev) -> None:                # noqa: N802
        c = self._at(ev.position().toPoint())
        if c is None:
            QToolTip.hideText()
            self._hover = 0
            return
        # ⚠ 只有換格子才重畫提示 —— 每次滑鼠移動都 showText 會讓提示一直閃。
        if c.key != self._hover:
            self._hover = c.key
            text = c.tooltip() if c.tooltip else html.escape(c.name)
            QToolTip.showText(ev.globalPosition().toPoint(), text, self)

    def sizeHint(self) -> QSize:                         # noqa: N802
        rows = max(1, (len(self._cells) + COLS - 1) // COLS)
        return QSize(COLS * CELL + PAD * 2, rows * CELL + PAD * 2)


def _gear_cells(gears: list[gear.Gear], scanner) -> list[Cell]:
    return [Cell(key=g.serial, icon_id=g.icon_id, name=g.name, payload=g,
                 badge=f"+{g.enhance}" if g.enhance else "",
                 tooltip=(lambda g=g: _tooltip_html(g, scanner)))
            for g in gears]


def _gem_cells(gems: list[holes.Gem], scanner=None) -> list[Cell]:
    """寶石背包：**同一種寶石合成一格**（各堆數量加總），等限低的排前面。
    `scanner`：滑過時讀「這顆加什麼」（範本＋效果表，見 holes.gem_effects）；沒給就不印那幾行。"""
    by_type: dict[int, list[holes.Gem]] = {}
    for gm in gems:
        by_type.setdefault(gm.type_id, []).append(gm)
    out: list[Cell] = []
    for type_id, stacks in by_type.items():
        first = stacks[0]
        n = sum(s.count for s in stacks)
        out.append(Cell(key=type_id, icon_id=first.icon_id, name=first.name,
                        payload=first, badge=f"×{n}", badge_colour="#FFFFFF",
                        tooltip=(lambda gm=first, n=n: _gem_tooltip_html(gm, n, scanner))))
    out.sort(key=lambda c: (c.payload.min_level, c.key))
    return out


def _gem_tooltip_html(gm: holes.Gem, n: int, scanner=None) -> str:
    """寶石說明（使用者 2026-09-06）：**GAMEDATA 的原文是最權威、不會錯**（itemdesc，
    資源包文字3：可鑲嵌於…／武器：…／防具：…／盾牌：…／裝備等限），有表就印表、
    ⛔ 不拿記憶體去對它、不加警示；表沒那顆（改版後還沒重跑 build_item_desc）才退回印
    記憶體算的加成（holes.gem_effects，滑過才讀，讀不到就不印）。
    ⛔ 「可鑲 N～M 級」那行使用者說不要（原文已有裝備等限）。"""
    lines = [(gm.name, "#FFFFFF")]
    text = itemdesc.lines(gm.type_id)
    if text:
        lines += [(t, "#C8C8C8") for t in text]
    else:
        effects = []
        if scanner is not None:
            try:
                effects = holes.gem_effects(scanner, gm.type_id)
            except Exception:                            # noqa: BLE001
                effects = []
        if effects:
            lines.append(("加什麼（說明表沒有這顆，讀記憶體）：", "#C8C8C8"))
            for label, attr, val in effects:
                lines.append((f"{label}：{attr} {val:+d}" if val else f"{label}：{attr}", "#7CFC7C"))
        lines.append((f"裝備等限：{gm.min_level}級", "#7CD8FF"))
    lines.append((f"數量 ×{n}", "#DDDDDD"))
    parts = [f"<div style='color:{colour}'>{html.escape(text)}</div>"
             for text, colour in lines]
    return ("<div style='background:#0d1b21; padding:4px'>"
            + "".join(parts) + "</div>")


def _tooltip_html(g: gear.Gear, scanner=None) -> str:
    """把 `gear.tooltip()` 那幾行變成提示框 HTML（顏色照遊戲）。"""
    parts = []
    for text, colour in gear.tooltip(g, scanner):
        if not text:
            parts.append("<div style='height:6px'></div>")
            continue
        parts.append(f"<div style='color:{colour}'>{html.escape(text)}</div>")
    return ("<div style='background:#0d1b21; padding:4px'>"
            + "".join(parts) + "</div>")


class NumberPicker(QWidget):
    """1~N 的小方塊，點哪一格就是要打到哪個數字（取代原本的數字輸入框）。

    使用者 2026-09-09 指定三件事，這個元件就是為了守住它們：
      · 不打字，直接點方塊；
      · **程式不准自己幫他改選的值**（選了強化過的裝備也不動）；
      · 記住上次選的，下次開起來就是那一格（存 config，`changed` 由呼叫端接）。
    """

    changed = Signal(int)

    def __init__(self, hi: int, value: int, parent=None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self._btns: list[QPushButton] = []
        for n in range(1, hi + 1):
            b = QPushButton(str(n))
            b.setCheckable(True)
            b.setFixedSize(PICK_CELL, PICK_CELL)
            b.setStyleSheet("padding: 0px;")     # ⛔ 只改內距，色碼一律走 theme
            b.clicked.connect(lambda _c=False, k=n: self.setValue(k))
            lay.addWidget(b)
            self._btns.append(b)
        self._value = 0
        self.setValue(value)

    def value(self) -> int:
        return self._value

    def setValue(self, n: int) -> None:
        n = max(1, min(int(n), len(self._btns)))
        self._value = n
        for i, b in enumerate(self._btns, 1):
            # 再點同一格會被 Qt 取消勾選 → 這裡一律重設，選中的那格永遠亮著
            b.setChecked(i == n)
        self.changed.emit(n)


class EnhanceTab(BaseTab):
    TAB_TITLE = "強化裝備"
    GROUP = GROUP_CHORES
    ORDER = 49                       # 排在活動（48）後面

    # ------------------------------------------------------------------
    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._sig: tuple | None = None
        self._run: enhance.Run | holes.Run | None = None
        # ★ 多選整批（使用者 2026-09-26）：照點選順序一件一件做。
        #   {"kind": "enhance"/"holes", "queue": [serial...], "target", "tally": {…},
        #    "cur": 目前這件名字, "last": 目前這件最後一個事件種類}
        self._batch: dict | None = None

        root = QVBoxLayout(self)
        # ⛔ 最上面那段使用說明使用者 2026-09-06 說不要（「那堆說明文字不要寫」）；
        #   規則都在檔頭 docstring 與各按鈕的 tooltip 裡。

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
        self.hole_lbl = QLabel("打孔錘 —　寶石 —")
        self.hole_lbl.setStyleSheet("font-weight: bold;")
        self.hole_lbl.setToolTip(
            "背包裡能用的一般打孔錘（祝福錘不算）與查得到等限的寶石。")
        bar.addWidget(self.hole_lbl)
        root.addLayout(bar)

        box = QGroupBox("模擬背包（可強化的裝備）")
        box_lay = QVBoxLayout(box)
        self.grid = IconGrid("背包裡沒有可強化的裝備", multi=True)
        self.grid.picked.connect(self._on_picked)
        area = QScrollArea()
        area.setWidget(self.grid)
        area.setWidgetResizable(True)
        area.setMinimumHeight(CELL * 3 + 12)
        box_lay.addWidget(area)
        sel_row = QHBoxLayout()
        self.all_btn = QPushButton("全選")
        self.all_btn.setToolTip("選起所有還沒到「強化到 +N」或「打孔到 N 孔」的裝備。")
        self.all_btn.clicked.connect(self._on_select_all)
        sel_row.addWidget(self.all_btn)
        self.none_btn = QPushButton("清除")
        self.none_btn.clicked.connect(self.grid.clear_selection)
        sel_row.addWidget(self.none_btn)
        self.sel_lbl = QLabel("點一下選、再點取消；可以選好幾件，照數字順序做")
        self.sel_lbl.setStyleSheet(f"color: {theme.TEXT_MUT};")
        sel_row.addWidget(self.sel_lbl)
        sel_row.addStretch(1)
        box_lay.addLayout(sel_row)
        root.addWidget(box)

        gem_box = QGroupBox("寶石背包（點一顆 ＝ 打孔時只鑲這一種）")
        gem_lay = QVBoxLayout(gem_box)
        self.gem_grid = IconGrid("背包裡沒有寶石")
        self.gem_grid.picked.connect(self._on_gem_picked)
        gem_area = QScrollArea()
        gem_area.setWidget(self.gem_grid)
        gem_area.setWidgetResizable(True)
        gem_area.setMinimumHeight(CELL + 12)
        gem_area.setMaximumHeight(CELL * 2 + 12)
        gem_lay.addWidget(gem_area)
        root.addWidget(gem_box)
        self._gem_sig: tuple | None = None
        # 上次選的寶石種類（config），寶石背包一列出來就幫他選回去
        self._want_gem = int(config.get("enhance.gem_type", 0) or 0)

        # ⚠ 使用者 2026-09-09 指定：這一排不要「已選：…」、不要「強化到 +」
        #   字樣，也不要滑鼠提示 —— 選了哪一件看格子的黃框就知道。
        act = QHBoxLayout()
        act.addStretch(1)
        self.target = NumberPicker(
            enhance.MAX_LEVEL, int(config.get("enhance.target", 1) or 1))
        self.target.changed.connect(self._on_target)
        act.addWidget(self.target)
        self.go_btn = QPushButton("強化錘強化")
        self.go_btn.clicked.connect(self._on_go)
        act.addWidget(self.go_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        act.addWidget(self.stop_btn)
        root.addLayout(act)

        act2 = QHBoxLayout()
        act2.addStretch(1)
        act2.addWidget(QLabel("打孔到"))
        self.hole_target = NumberPicker(
            holes.MAX_HOLES,
            int(config.get("enhance.hole_target", holes.MAX_HOLES)))
        self.hole_target.setToolTip(
            f"要打到幾個孔（遊戲上限 {holes.MAX_HOLES}）。到了就停，最後一孔留空。\n"
            "點一下就是它，程式不會自己幫你改；下次開起來預設是上次選的。")
        self.hole_target.changed.connect(self._on_hole_target)
        act2.addWidget(self.hole_target)
        act2.addWidget(QLabel("孔"))
        act2.addSpacing(12)
        # 寶石怎麼挑 —— 二選一（使用者 2026-09-03）：等限以下 vs 指定一種
        self.gem_cap_rb = QRadioButton("寶石等限 ≤")
        self.gem_cap_rb.setToolTip(
            "只拿「裝備等限」不超過右邊數字的寶石去鑲（當墊子用），好寶石不會動。")
        act2.addWidget(self.gem_cap_rb)
        self.gem_cap = QSpinBox()
        self.gem_cap.setRange(0, holes.LEVEL_SANE)
        self.gem_cap.setSingleStep(10)
        self.gem_cap.setFixedWidth(60)
        self.gem_cap.setToolTip(
            f"選裝備時自動填「裝備等級 − {GEM_CAP_BELOW}」，要改自己打。")
        act2.addWidget(self.gem_cap)
        act2.addWidget(QLabel("級"))
        act2.addSpacing(8)
        self.gem_pick_rb = QRadioButton("用選的寶石：")
        self.gem_pick_rb.setToolTip("只鑲下面寶石背包裡點起來的那一種；用完就停。")
        act2.addWidget(self.gem_pick_rb)
        self.gem_pick_lbl = QLabel("（還沒選）")
        act2.addWidget(self.gem_pick_lbl)
        if config.get("enhance.gem_mode", "cap") == "pick":
            self.gem_pick_rb.setChecked(True)
        else:
            self.gem_cap_rb.setChecked(True)
        self.gem_cap_rb.toggled.connect(self._on_gem_mode)
        act2.addSpacing(8)
        self._cap_serial = 0             # 上次自動填等限時選的是哪件
        self.hole_btn = QPushButton("自動打孔")
        self.hole_btn.setToolTip(
            "用一般 N星打孔錘打孔，有空孔先鑲寶石，直到孔數到目標（最後一孔留空）。\n"
            "⚠ 打孔失敗裝備會毀損，一旦毀損或驗不出結果就立刻停。\n"
            "祝福打孔錘不會用；錘子挑「星級夠這件裝備等級」裡最低的那種。")
        self.hole_btn.clicked.connect(self._on_go_holes)
        act2.addWidget(self.hole_btn)
        root.addLayout(act2)

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
            self.grid.set_cells([])           # ⚠ 46f441c 改名漏了這一處：遊戲沒開時整頁炸 AttributeError
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
        gears, _complete = gear.in_bag(sc)
        # 一次掃描同時數強化錘、打孔錘、寶石（別各掃一遍）
        items, scanned = bag.scan(sc)
        n_hammer = sum(it.count for it in items
                       if it.type_id == enhance.HAMMER_TYPE)
        if n_hammer or scanned:
            self.hammer_lbl.setText(f"強化錘 {n_hammer} 個")
        else:
            self.hammer_lbl.setText("強化錘 —")
        hs, _ = holes.hammers(sc, items, scanned)
        gs, _ = holes.gems(sc, items, scanned)
        if hs or gs or scanned:
            by_star: dict[int, int] = {}
            for h in hs:
                by_star[h.star] = by_star.get(h.star, 0) + h.count
            stars = "、".join(f"{s}星×{n}" for s, n in sorted(by_star.items()))
            self.hole_lbl.setText(
                f"打孔錘 {stars or '0'}　寶石 {sum(g.count for g in gs)} 顆")
        else:
            self.hole_lbl.setText("打孔錘 —　寶石 —")
        sig = (pid, tuple((g.serial, g.enhance, g.slot, g.holes, g.gems_filled)
                          for g in gears))
        if sig != self._sig:
            self._sig = sig
            self.grid.set_cells(_gear_cells(gears, sc))
            self._on_picked(self.grid.selected().serial
                            if self.grid.selected() else 0)
        gem_sig = (pid, tuple(sorted((g.type_id, g.slot, g.count) for g in gs)))
        if gem_sig != self._gem_sig:
            self._gem_sig = gem_sig
            self.gem_grid.set_cells(_gem_cells(gs, sc))
            # 上次選的那種寶石一出現就選回去（只做一次；他點別顆就以他的為準）
            if self._want_gem and self.gem_grid.selected() is None:
                if self.gem_grid.select(self._want_gem):
                    self._want_gem = 0
        self._update_buttons()

    def _on_picked(self, _serial: int) -> None:
        g = self.grid.selected()
        if g is not None:
            # ⛔ 這裡以前會把「強化到 +N」「打孔到 N 孔」往上頂到裝備目前的次數＋1
            #   —— 使用者 2026-09-09 明令**不准再偷改他選的數字**。選太低就在
            #   按下去的時候用紀錄告訴他（見 `_on_go` / `_on_go_holes`）。
            # ★ 只在「換了一件」時自動填等限 —— 打孔中孔數變了也會走到這裡，
            #   每拍都填會把使用者手改的值洗掉
            lvl = g.base.get("level")
            if lvl is not None and g.serial != self._cap_serial:
                self._cap_serial = g.serial
                self.gem_cap.setValue(max(0, int(lvl) - GEM_CAP_BELOW))
        self._update_buttons()

    def _update_buttons(self) -> None:
        running = self._batch is not None or (
            self._run is not None and not self._run.done)
        gs = [c.payload for c in self.grid.selected_cells()]
        ok = (bool(gs) and not running
              and any(g.enhance < enhance.MAX_LEVEL for g in gs))
        self.go_btn.setEnabled(bool(ok))
        gem_ok = (self.gem_cap_rb.isChecked()
                  or self.gem_grid.selected() is not None)
        self.hole_btn.setEnabled(bool(gs and not running and gem_ok
                                      and any(g.holes < holes.MAX_HOLES for g in gs)))
        self.gem_cap.setEnabled(self.gem_cap_rb.isChecked())
        self.stop_btn.setEnabled(running)
        self.who.setEnabled(not running)
        self.all_btn.setEnabled(not running)
        self.none_btn.setEnabled(not running)
        if gs:
            self.sel_lbl.setText(f"已選 {len(gs)} 件（照格子上的數字順序做）")
        else:
            self.sel_lbl.setText("點一下選、再點取消；可以選好幾件，照數字順序做")

    def _on_select_all(self) -> None:
        """全選：還沒到強化目標、或還沒到打孔目標的裝備（背包順序）。"""
        et, ht = self.target.value(), self.hole_target.value()
        keys = [c.key for c in self.grid.cells()
                if (c.payload.enhance < min(et, enhance.MAX_LEVEL)
                    or c.payload.holes < min(ht, holes.MAX_HOLES))]
        self.grid.select_keys(keys)
        self._update_buttons()

    def _on_gem_picked(self, key: int) -> None:
        c = self.gem_grid.selected_cell()
        if c is None:
            self.gem_pick_lbl.setText("（還沒選）")
        else:
            self.gem_pick_lbl.setText(c.name)
            self.gem_pick_rb.setChecked(True)   # 點了寶石就是要用它
            self._want_gem = 0
            config.set("enhance.gem_type", int(key))
            config.save()
        self._update_buttons()

    def _on_target(self, n: int) -> None:
        config.set("enhance.target", int(n))
        config.save()                      # ★ set() 不寫檔，要接 save()

    def _on_hole_target(self, n: int) -> None:
        config.set("enhance.hole_target", int(n))
        config.save()

    def _on_gem_mode(self, _checked: bool) -> None:
        config.set("enhance.gem_mode",
                   "cap" if self.gem_cap_rb.isChecked() else "pick")
        config.save()
        self._update_buttons()

    # ------------------------------------------------------------------
    def _log(self, text: str, colour: str = "#DDDDDD") -> None:
        item = QListWidgetItem(f"{time.strftime('%H:%M:%S')}　{text}")
        item.setForeground(QColor(colour))
        self.hist.insertItem(0, item)          # ★ 追加式清單用插列，不要重畫整表
        while self.hist.count() > HIST_MAX:
            self.hist.takeItem(self.hist.count() - 1)

    def _on_go(self) -> None:
        self._begin_batch("enhance")

    def _on_go_holes(self) -> None:
        self._begin_batch("holes")

    def _begin_batch(self, kind: str) -> None:
        """照點選順序整批做。⚠ 只記 serial —— 輪到那件時才重讀它在哪一格、現況多少
        （背包會在中間變動，CLAUDE.md 鐵則：交給遊戲的格號送出前當場重讀）。"""
        pid, sc = self._cur()
        cells = self.grid.selected_cells()
        if sc is None or not cells:
            return
        if self._mover(pid) is None:
            return
        gem_type, how = None, ""
        if kind == "holes":
            if self.gem_pick_rb.isChecked():
                c = self.gem_grid.selected_cell()
                if c is None:
                    self.status.setText("選了「用選的寶石」但還沒點寶石")
                    return
                gem_type = int(c.key)
                how = f"只鑲 {c.name}"
            else:
                how = f"寶石等限 ≤ {self.gem_cap.value()} 級"
        target = (self.target.value() if kind == "enhance"
                  else self.hole_target.value())
        self._batch = {"kind": kind, "queue": [c.key for c in cells],
                       "target": target, "gem_type": gem_type,
                       "cap": self.gem_cap.value(), "total": len(cells),
                       "tally": {"ok": 0, "gone": 0, "down": 0, "skip": 0},
                       "cur": "", "last": None}
        what = f"強化到 +{target}" if kind == "enhance" else f"打孔到 {target} 孔（{how}）"
        self._log(f"整批開始：{len(cells)} 件，{what}", "#7CD8FF")
        self._next_in_batch()
        self._update_buttons()

    def _next_in_batch(self) -> None:
        """輪到下一件：重讀它的現況 → 已達標／不見了就跳過 → 開一個 Run。"""
        b = self._batch
        pid, sc = self._cur()
        mv = self._movers.get(pid)
        while b is not None and b["queue"]:
            serial = b["queue"].pop(0)
            n = b["total"] - len(b["queue"])
            gears, complete = gear.in_bag(sc) if sc is not None else ([], False)
            g = next((x for x in gears if x.serial == serial), None)
            if g is None:
                if not complete:
                    b["queue"].insert(0, serial)      # 這一拍背包讀不完整 → 等下一拍
                    QTimer.singleShot(300, self._next_in_batch)
                    return
                b["tally"]["skip"] += 1
                self._log(f"[{n}/{b['total']}] 那件不在背包了，跳過", theme.WARN)
                continue
            t = b["target"]
            if b["kind"] == "enhance":
                if g.enhance >= min(t, enhance.MAX_LEVEL):
                    b["tally"]["skip"] += 1
                    self._log(f"[{n}/{b['total']}] {g.name} 已經 +{g.enhance}，跳過")
                    continue
                self._run = enhance.Run(sc, mv, g.slot, g.serial, t, g.name)
                self._log(f"[{n}/{b['total']}] 開始：{g.name} +{g.enhance} → +{t}", "#7CD8FF")
                self.status.setText(f"強化中… [{n}/{b['total']}] {g.name} → +{t}")
                self._run_timer.start(STRIKE_MS)
            else:
                if g.holes >= min(t, holes.MAX_HOLES):
                    b["tally"]["skip"] += 1
                    self._log(f"[{n}/{b['total']}] {g.name} 已經 {g.holes} 孔，跳過")
                    continue
                cap = b["cap"]
                lvl = g.base.get("level")
                if b["total"] > 1 and lvl is not None:
                    # ★ 整批時等限取「你填的」跟「這件等級 − GEM_CAP_BELOW」較低的那個：
                    #   只會更保守（拿更差的寶石墊），不會動到好寶石。
                    cap = min(cap, max(0, int(lvl) - GEM_CAP_BELOW))
                self._run = holes.Run(sc, mv, g.slot, g.serial, t, cap, g.name,
                                      gem_type=b["gem_type"])
                gem = "指定寶石" if b["gem_type"] else f"寶石等限 ≤ {cap}"
                self._log(f"[{n}/{b['total']}] 開始打孔：{g.name} {g.holes} 孔 → {t} 孔（{gem}）",
                          "#7CD8FF")
                self.status.setText(f"打孔中… [{n}/{b['total']}] {g.name} → {t} 孔")
                self._run_timer.start(RUN_MS)
            b["cur"], b["last"] = g.name, None
            return
        self._end_batch("")

    def _end_batch(self, why: str) -> None:
        b = self._batch
        self._batch = None
        self._run = None
        self._run_timer.stop()
        self.status.setText("　")
        self._sig = None                      # 逼下一拍重畫（東西可能不見了）
        if b is not None:
            t = b["tally"]
            left = len(b["queue"])
            parts = [f"成功 {t['ok']}"]
            if t["gone"]:
                parts.append(f"毀損 {t['gone']}")
            if t["down"]:
                parts.append(f"退等 {t['down']}")
            if t["skip"]:
                parts.append(f"跳過 {t['skip']}")
            if left:
                parts.append(f"沒做 {left}")
            bad = bool(why) or t["gone"] or t["down"]
            self._log(f"整批結束：共 {b['total']} 件，" + "、".join(parts)
                      + (f"（{why}）" if why else ""),
                      "#FFC864" if bad else "#7CFC7C")
        self._update_buttons()

    def _on_stop(self) -> None:
        if self._batch is not None or (self._run is not None and not self._run.done):
            self._log("手動停止", "#FFC864")
        if self._batch is not None:
            self._end_batch("手動停止")
            return
        self._run = None
        self._run_timer.stop()
        self.status.setText("　")
        self._update_buttons()

    def _run_tick(self) -> None:
        run = self._run
        if run is None:
            self._run_timer.stop()
            return
        b = self._batch
        for ev in run.tick():
            self._log(ev.text, COLOUR_OF.get(ev.kind, "#DDDDDD"))
            if b is not None:
                b["last"] = ev.kind
        if not run.done:
            return
        self._run_timer.stop()
        self._run = None
        self._sig = None
        if b is None:
            self.status.setText("　")
            self._update_buttons()
            return
        # ★ 使用者 2026-09-26 定：失敗毀損／退等 → 繼續下一件；
        #   錘子／寶石用完、驗不出結果、格子換人 → 整批停。
        last = b["last"]
        if last == enhance.DONE:
            b["tally"]["ok"] += 1
        elif last == enhance.GONE:
            b["tally"]["gone"] += 1
        elif last == enhance.DOWNGRADE:
            b["tally"]["down"] += 1
        else:
            self._end_batch(f"{b['cur']} 停在「{last}」，整批停下")
            return
        self._next_in_batch()

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
