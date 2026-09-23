"""傢俱強化分頁：模擬背包挑一件傢俱、挑一種魔力錘，一直敲到魔力值 ≥ 目標就停。

怎麼用
------
1. 上面選分身 → 「傢俱」那格只列背包裡的傢俱（格子右下角＝目前魔力值），
   滑過看「魔力值：總和(基礎+加成)」。
2. 下面那排是背包裡的魔力錘（右下角＝數量），滑過看擲出的範圍，點一種。
3. 填「魔力值 ≥」，按「開始」—— 每一錘都重讀結果寫進紀錄，到了就停；
   錘子用完、讀不到、格子換人也會停。

⚠ 魔力錘是**重擲**：原本的加成不保留，敲一下可能變差（物品說明原文）。
   目標超過「基礎＋這種錘子的最高值」＝永遠敲不到，按下去直接擋。

背後：`app/game/furniture.py`（基礎值查表 assets/furniture.tsv.gz、加成讀物品 +0xA0、
送出跟強化裝備同一支 USE_ITEM_FN）。
"""
from __future__ import annotations

import html
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
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
    QVBoxLayout,
)

from app import theme
from app.config import config
from app.core import charname, injector, preload, window as win
from app.core.memory import MemoryScanner
from app.game import bag, furniture, itemdesc, locate, move
from app.tabs.base_tab import GROUP_CHORES, BaseTab
from app.tabs.enhance_tab import CELL, Cell, IconGrid

REFRESH_MS = 400
RUN_MS = 50                      # 敲錘中多久看一次結果（使用者 2026-09-23 嫌慢；讀一格只要幾毫秒）
HIST_MAX = 300
TARGET_MAX = 9999

COLOUR_OF = {
    furniture.ROLLED: "#DDDDDD",
    furniture.BLOCKED: "#FFC864",
    furniture.UNKNOWN: "#FFC864",
    furniture.DONE: "#7CFC7C",
}


def _html(lines: list[tuple[str, str]]) -> str:
    parts = [f"<div style='color:{c}'>{html.escape(t)}</div>" for t, c in lines]
    return "<div style='background:#0d1b21; padding:4px'>" + "".join(parts) + "</div>"


def _furn_cells(fs: list[furniture.Furn]) -> list[Cell]:
    return [Cell(key=f.serial, icon_id=f.icon_id, name=f.name, payload=f,
                 badge=str(f.total), badge_colour="#7CFC7C" if f.bonus else "#FFFFFF",
                 tooltip=(lambda f=f: _html([
                     (f.name, "#FFFFFF"),
                     (f"魔力值：{f.total}({f.base}+{f.bonus})", "#7CFC7C"),
                 ])))
            for f in fs]


def _hammer_cells(hs: list[furniture.Hammer]) -> list[Cell]:
    return [Cell(key=h.type_id, icon_id=h.icon_id, name=h.name, payload=h,
                 badge=f"×{h.count}", badge_colour="#FFFFFF",
                 tooltip=(lambda h=h: _html(
                     [(h.name, "#FFFFFF"), (f"加成重擲 {h.lo}～{h.hi}", "#7CD8FF")]
                     + [(t, "#C8C8C8") for t in itemdesc.lines(h.type_id)]
                     + [(f"數量 ×{h.count}", "#DDDDDD")])))
            for h in hs]


class FurnitureTab(BaseTab):
    TAB_TITLE = "傢俱強化"
    GROUP = GROUP_CHORES
    ORDER = 49.5                     # 緊接在強化裝備（49）後面

    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._sig: tuple | None = None
        self._hsig: tuple | None = None
        self._run: furniture.Run | None = None
        self._want_hammer = int(config.get("furniture.hammer", 0) or 0)

        root = QVBoxLayout(self)

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
        root.addLayout(bar)

        box = QGroupBox("傢俱（右下角＝目前魔力值）")
        box_lay = QVBoxLayout(box)
        self.grid = IconGrid("背包裡沒有傢俱")
        self.grid.picked.connect(lambda _k: self._update_buttons())
        area = QScrollArea()
        area.setWidget(self.grid)
        area.setWidgetResizable(True)
        area.setMinimumHeight(CELL * 3 + 12)
        box_lay.addWidget(area)
        root.addWidget(box)

        hbox = QGroupBox("傢俱魔力錘（點一種）")
        hlay = QVBoxLayout(hbox)
        self.hgrid = IconGrid("背包裡沒有傢俱魔力錘")
        self.hgrid.picked.connect(self._on_hammer_picked)
        harea = QScrollArea()
        harea.setWidget(self.hgrid)
        harea.setWidgetResizable(True)
        harea.setMinimumHeight(CELL + 12)
        harea.setMaximumHeight(CELL * 2 + 12)
        hlay.addWidget(harea)
        root.addWidget(hbox)

        act = QHBoxLayout()
        act.addStretch(1)
        act.addWidget(QLabel("魔力值 ≥"))
        self.target = QSpinBox()
        self.target.setRange(1, TARGET_MAX)
        self.target.setFixedWidth(80)
        self.target.setValue(int(config.get("furniture.target", 300) or 300))
        self.target.setToolTip("魔力值（基礎＋加成）到這個數字就停。")
        self.target.valueChanged.connect(self._on_target)
        act.addWidget(self.target)
        self.go_btn = QPushButton("開始")
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
        self._sig = self._hsig = None
        if not self._scanners:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
            self.grid.set_cells([])
            self.hgrid.set_cells([])
            return
        self.status.setText(f"找到 {len(self._scanners)} 個分身")
        self._refresh()

    def _cur(self):
        pid = self.who.currentData()
        return pid, self._scanners.get(pid) if pid else None

    def _on_who_changed(self) -> None:
        self._on_stop()
        self._sig = self._hsig = None
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
        items, scanned = bag.scan(sc)             # 一次掃描，傢俱與錘子共用
        fs, _ = furniture.in_bag(sc, items, scanned)
        hs, _ = furniture.hammers(sc, items, scanned)
        sig = (pid, tuple((f.serial, f.slot, f.bonus) for f in fs))
        if sig != self._sig:
            self._sig = sig
            self.grid.set_cells(_furn_cells(fs))
        hsig = (pid, tuple((h.type_id, h.count) for h in hs))
        if hsig != self._hsig:
            self._hsig = hsig
            self.hgrid.set_cells(_hammer_cells(hs))
            # 上次選的那種錘子一出現就選回去（只做一次；他點別種就以他的為準）
            if self._want_hammer and self.hgrid.selected() is None:
                if self.hgrid.select(self._want_hammer):
                    self._want_hammer = 0
        self._update_buttons()

    def _on_hammer_picked(self, key: int) -> None:
        if key:
            self._want_hammer = 0
            config.set("furniture.hammer", int(key))
            config.save()
        self._update_buttons()

    def _on_target(self, n: int) -> None:
        config.set("furniture.target", int(n))
        config.save()                      # ★ set() 不寫檔，要接 save()

    def _update_buttons(self) -> None:
        running = self._run is not None and not self._run.done
        ok = (self.grid.selected() is not None and self.hgrid.selected() is not None
              and not running)
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

    def _warn(self, msg: str) -> None:
        self._log(msg, theme.WARN)
        self.status.setText(msg)

    def _on_go(self) -> None:
        pid, sc = self._cur()
        f = self.grid.selected()
        h = self.hgrid.selected()
        if sc is None or f is None or h is None:
            return
        target = self.target.value()
        if f.total >= target:
            self._warn(f"{f.name} 魔力值已經 {f.total}（≥ {target}），不用敲")
            return
        if f.base + h.hi < target:
            self._warn(f"{h.name} 最高只到 +{h.hi}，{f.name} 最多 {f.base + h.hi}，"
                       f"到不了 {target}")
            return
        mv = self._mover(pid)
        if mv is None:
            return
        self._run = furniture.Run(sc, mv, f.slot, f.serial, h.type_id, target, f.name)
        self._log(f"開始：{f.name} 魔力值 {f.total}（{f.base}+{f.bonus}）→ ≥ {target}，"
                  f"用 {h.name}（{h.lo}～{h.hi}，剩 {h.count}）", "#7CD8FF")
        self.status.setText(f"敲錘中… {f.name} → 魔力值 ≥ {target}")
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
            self._sig = self._hsig = None
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
