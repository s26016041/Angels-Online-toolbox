"""能量晶化分頁。

做三件事：
  1. 顯示還剩多少能量（＝還能按幾次）與各屬性目前累積的點數
  2. 12 個屬性可以勾選
  3. 按「能量晶化」之後讀出這次抽到什麼；**抽到勾選的就自動按一次「我要晶能加倍」**

背後都是 `app/game/energy.py`：呼叫遊戲自己的泛用送包函式
`0x5D3D97(0x38, 1)` / `0x5D3D97(0x39, -1)`，欄位讀狀態物件 +0xB8 起那一段。

為什麼要選分身
--------------
多開時「按一下」一定要指名對誰按。只開一個分身時會自動選好。

⚠ 不做自動連按。晶化要花能量、結果隨機，按幾次是使用者的決定 ——
  「自動加倍」是他勾選之後**針對這一次結果**的反應，不是替他一直按。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.config import config
from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import energy, entity, locate, move
from app.tabs.base_tab import BaseTab

# 按下晶化到結果寫進記憶體要多久。實測 106ms（嵐狐按 3 次都一樣），取 3 倍餘裕。
SETTLE_MS = 300
REFRESH_MS = 500          # 畫面更新間隔（純讀取，很便宜）
COLS = 3                  # 跟遊戲畫面一樣排 3 欄
# 自動晶化的最快間隔。一輪要「晶化 → 等 0.3s → 判斷 → 可能再送加倍」，
# 太密會塞爆指令槽（只有一個槽，前一個還沒做完就會被擋掉）。
MIN_INTERVAL = 0.8
DEFAULT_INTERVAL = 1.5
# 自動晶化的預設次數。刻意**不是「不限」** —— 預設值不該是「花光你全部能量」。
DEFAULT_LIMIT = 10
LOG_COLS = ("時間", "動作", "結果", "實際入帳", "剩餘能量")
LOG_HEIGHT = 150
LOG_MAX = 500             # 只留最近這麼多列，跑一整晚也不會吃掉記憶體


class EnergyTab(BaseTab):
    TAB_TITLE = "能量晶化"
    ORDER = 45

    def build_ui(self) -> None:
        self._movers: dict[int, move.Mover] = {}
        self._scanners: dict[int, MemoryScanner] = {}
        self._state: dict[int, int] = {}          # pid -> 狀態物件
        self._names = energy.FALLBACK_NAMES
        self._boxes: list[QCheckBox] = []
        # 按晶化之前的狀態。要靠它跟按之後比對，才算得出「實際入帳幾點到誰」
        self._pre = None
        # 使用者「想要」的次數（_load 會覆寫）。先給預設值，免得 _clamp_limit
        # 比 _load 早跑到就炸掉。
        self._limit_want = DEFAULT_LIMIT
        # 歷史紀錄：**每個分身各自一份**（pid -> 列)，切分身時整張表換掉
        self._logs: dict[int, list[tuple]] = {}

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "按一下遊戲裡的「能量晶化」或「我要晶能加倍」。跟你手動點那兩顆按鈕"
            "送出的是同一個封包（呼叫遊戲自己的函式，加解密都由客戶端處理）。"))

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        # ⚠ 換分身一定要停掉自動晶化 —— 不然會繼續對「新選的那台」按下去。
        self.who.currentIndexChanged.connect(self._on_who_changed)
        bar.addWidget(self.who)
        refresh = QPushButton("重新整理")
        refresh.setToolTip("重新列出目前開著的遊戲分身。")
        refresh.clicked.connect(self.reload_instances)
        bar.addWidget(refresh)
        bar.addSpacing(16)
        self.energy_lbl = QLabel("能量 —")
        self.energy_lbl.setStyleSheet("font-weight: bold;")
        bar.addWidget(self.energy_lbl)
        bar.addSpacing(16)
        self.cur_lbl = QLabel("目前選中 —")
        bar.addWidget(self.cur_lbl)
        bar.addStretch(1)
        root.addLayout(bar)

        # --- 12 個屬性 -------------------------------------------------
        box = QGroupBox("屬性（勾起來的：抽到就自動按一次「我要晶能加倍」）")
        grid = QGridLayout(box)
        for i in range(energy.ATTR_COUNT):
            cb = QCheckBox(self._names[i])
            cb.setMinimumWidth(190)
            cb.toggled.connect(self._save)
            self._boxes.append(cb)
            grid.addWidget(cb, i // COLS, i % COLS)
        root.addWidget(box)

        row = QHBoxLayout()
        self.auto_cb = QCheckBox("抽到勾選的就自動加倍")
        self.auto_cb.setChecked(True)
        self.auto_cb.setToolTip(
            "按下「能量晶化」之後，等結果寫進記憶體（約 0.1 秒）再判斷。\n"
            "抽到的屬性有勾起來 → 自動送一次「我要晶能加倍」。\n"
            "沒勾任何屬性就等於不會自動加倍。")
        self.auto_cb.toggled.connect(self._save)
        row.addWidget(self.auto_cb)
        row.addSpacing(16)
        self.roll_btn = QPushButton("能量晶化")
        self._roll_tip = (
            "送出一次能量晶化（遊戲裡那顆按鈕）。\n"
            "⚠ 每 1 點能量可進行 1 次，屬性隨機 —— 按幾次由你決定，"
            "程式不會自動連按。")
        self.roll_btn.setToolTip(self._roll_tip)
        self.roll_btn.clicked.connect(self._roll)
        row.addWidget(self.roll_btn)
        self.double_btn = QPushButton("我要晶能加倍")
        self.double_btn.setToolTip("送出一次「我要晶能加倍」（遊戲裡那顆按鈕）。")
        self.double_btn.clicked.connect(self._double)
        row.addWidget(self.double_btn)
        row.addStretch(1)
        root.addLayout(row)

        # --- 自動晶化 ---------------------------------------------------
        arow = QHBoxLayout()
        self.auto_roll_cb = QCheckBox("自動晶化")
        self.auto_roll_cb.setToolTip(
            "照下面的間隔一直按「能量晶化」，抽到勾選的屬性照樣自動加倍。\n"
            "\n"
            "會自動停下來的情況：能量歸零、按到設定的次數、\n"
            "換分身、取消勾選、關掉分頁。")
        self.auto_roll_cb.toggled.connect(self._toggle_auto)
        arow.addWidget(self.auto_roll_cb)
        arow.addWidget(QLabel("每"))
        self.interval = QDoubleSpinBox()
        self.interval.setRange(MIN_INTERVAL, 30.0)
        self.interval.setSingleStep(0.5)
        self.interval.setDecimals(1)
        self.interval.setValue(DEFAULT_INTERVAL)
        # ⚠ 單位文字一律放框外的 QLabel，不要用 setSuffix() —— 那會把文字塞進
        #   輸入框裡（使用者反映「輸入框應該只有數字」）。
        self.interval.setFixedWidth(70)
        self.interval.setToolTip(
            f"兩次晶化之間隔多久。最少 {MIN_INTERVAL} 秒 —— 一輪要「晶化 → "
            "等 0.3 秒讀結果 → 可能再送加倍」，\n太密會塞爆指令槽（只有一個），"
            "送不出去的那次就白按了。")
        self.interval.valueChanged.connect(self._save)
        arow.addWidget(self.interval)
        arow.addWidget(QLabel("秒一次，最多"))
        self.limit = QSpinBox()
        # ★ 下限 **0**：能量是 0 的人就該看到 0，不該被逼著顯示 1。
        #   不允許負數。上限跟著目前能量動態調整（見 _clamp_limit）——
        #   填一個比能量還大的數字沒有意義，直接擋在輸入端。
        #   ⚠ 0 **不是**「不限」（舊版是，那會變成無限狂按）——
        #     0 就是不跑，見 _auto_tick。要用完全部請勾右邊那個。
        self.limit.setRange(0, DEFAULT_LIMIT)
        self.limit.setValue(DEFAULT_LIMIT)
        self.limit.setFixedWidth(70)
        self.limit.setToolTip(
            "按幾次就自動停。\n"
            "⚠ 最大值會自動跟著目前能量走 —— 打不進比能量還大的數字。\n"
            "　 要用完全部能量請勾右邊的「晶化全部能量」。")
        self.limit.valueChanged.connect(self._save)
        arow.addWidget(self.limit)
        arow.addWidget(QLabel("次"))
        self.all_cb = QCheckBox("晶化全部能量")
        self.all_cb.setToolTip(
            "勾起來就**不看上面的次數**，一路按到能量歸零為止。\n"
            "\n"
            "⚠ 能量多的時候這會花掉全部 —— 嵐狐現在有一千多點，"
            "按完就是一千多次。\n"
            "　 真正的開關還是「自動晶化」，那個每次開工具箱都是關的。")
        self.all_cb.toggled.connect(self._on_all_toggled)
        arow.addWidget(self.all_cb)
        self.auto_lbl = QLabel("")
        self.auto_lbl.setStyleSheet("color: #9aa2b8;")
        arow.addWidget(self.auto_lbl)
        arow.addStretch(1)
        root.addLayout(arow)

        # --- 歷史紀錄 ---------------------------------------------------
        # ★ 為什麼要有：晶化的點數是「下一次按的時候才入帳」，加倍成功與否
        #   也只反映在「下次入帳幾點」。光看畫面很難確定加倍到底有沒有生效
        #   —— 使用者原話「讓人心穩一點」。所以這裡把每一次的**實際入帳**
        #   也記下來，看得到 20 點真的進去了。
        self.log = QTableWidget(0, len(LOG_COLS))
        self.log.setHorizontalHeaderLabels(LOG_COLS)
        self.log.setEditTriggers(QTableWidget.NoEditTriggers)
        self.log.setSelectionMode(QTableWidget.NoSelection)
        self.log.verticalHeader().setVisible(False)
        # 隔行換底色比較好讀（使用者要求）。顏色主題已經定好了
        # （app/theme.py 的 alternate-background-color，比底色亮一階）。
        self.log.setAlternatingRowColors(True)
        self.log.setFixedHeight(LOG_HEIGHT)
        hh = self.log.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        root.addWidget(self.log)

        self.status = QLabel("尚未選擇分身")
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)

        self._auto_n = 0
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._auto_tick)

        self._load()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_MS)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self) -> None:
        self.who.blockSignals(True)
        self.who.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
        self._state.clear()
        seen = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:                       # noqa: BLE001
                continue
            try:
                locate.warm(sc)                     # 改版位移自動校正（只做一次）
            except Exception:                       # noqa: BLE001
                pass
            acc = charname.account_from_title(w.title)
            # ⚠ 用預讀的快取，**不要叫 charname.read_character_name()** ——
            #   那是全記憶體掃字串，一台 1.1~1.8 秒，五台就讓分頁卡七、八秒
            #   （使用者回報的「切過去卡一下」）。見 app/core/preload.py。
            nm = preload.name_of(w.pid, sc, acc)
            self._scanners[w.pid] = sc
            self.who.addItem(f"{nm}（{acc}）", w.pid)
        self.who.blockSignals(False)
        if not self._scanners:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
            self._show_log(None)
            return
        # 分身還是同一批（pid 沒變）時歷史要留著，只是重新畫目前這一台的
        pid = self.who.currentData()
        self._show_log(int(pid) if pid is not None else None)
        # 屬性名從遊戲記憶體讀（讀不到會用後備清單）
        sc = next(iter(self._scanners.values()))
        self._names = energy.attr_names(sc)
        for i, cb in enumerate(self._boxes):
            cb.setText(self._names[i])
        self.status.setText(f"找到 {len(self._scanners)} 個分身")
        self._refresh()

    # ------------------------------------------------------------------
    def _cur(self):
        """(pid, scanner, 狀態物件)；拿不到回 (None, None, None)。"""
        pid = self.who.currentData()
        if pid is None:
            return None, None, None
        sc = self._scanners.get(int(pid))
        if sc is None:
            return None, None, None
        # ★ 用預讀好的狀態物件（開程式時跟角色名一起掃出來的）。
        #   自己再找一次要 ~320ms，那就是「第一次切過來還會卡」的原因。
        st = self._state.get(int(pid)) or preload.state_of(int(pid), sc)
        if st:
            self._state[int(pid)] = st
        return int(pid), sc, st

    def _read(self):
        pid, sc, st = self._cur()
        if not st:
            return None
        got = energy.read(sc, st)
        if got is None:                            # 物件搬家了，下次重新定位
            self._state.pop(pid, None)
            preload.forget_state(pid)              # 預讀的那份也作廢
        return got

    def _clamp_limit(self, energy_now: int) -> None:
        """把「最多幾次」的上限釘在目前能量 —— 打不進比能量還大的數字。

        ⚠ 用 blockSignals 包起來：setRange/setValue 會發 valueChanged，
          沒擋掉的話每 0.5 秒的更新都會觸發一次存檔。
        """
        top = max(0, int(energy_now))
        if self.limit.maximum() == top:
            return
        self.limit.blockSignals(True)
        self.limit.setRange(0, top)
        # ⚠ 只調範圍不夠：能量歸零時值會被夾成 0，**能量回來之後不會自己回去**，
        #   使用者就會看到一個永遠是 0 的欄位。所以每次都用「他設定的值」
        #   重新夾一次。`_limit_want` 只有在**使用者自己改**時才更新
        #   （這裡有 blockSignals，不會觸發 _save）。
        self.limit.setValue(min(self._limit_want, top))
        self.limit.blockSignals(False)

    def _on_all_toggled(self, on: bool) -> None:
        """勾了「全部」就把次數欄位鎖起來 —— 不然兩個設定會互相矛盾看不懂。"""
        self.limit.setEnabled(not on)
        self._save()

    def _on_who_changed(self) -> None:
        if self.auto_roll_cb.isChecked():
            self._stop_auto("換了分身")
        # ⚠ 換分身時把「按之前的狀態」丟掉：那是上一台的，拿來算入帳會算到
        #   別人頭上。
        self._pre = None
        pid = self.who.currentData()
        self._show_log(int(pid) if pid is not None else None)
        self._refresh()

    def _set_buttons(self, energy_left: int | None) -> None:
        """能量是 0（或讀不到）就把按鈕變灰 —— 不要讓使用者亂送封包。

        ⚠ 三顆都關掉，包含「我要晶能加倍」：能量 0 的時候實測 +0xBC 也是
          「沒抽過」的狀態，加倍沒有對象可以加。
        """
        can = bool(energy_left)
        why = ("" if can else
               "　（能量是 0，沒得按）" if energy_left == 0 else
               "　（讀不到狀態，先選一個分身）")
        for w in (self.roll_btn, self.double_btn, self.auto_roll_cb,
                  self.all_cb):
            w.setEnabled(can)
        self.interval.setEnabled(can)
        self.limit.setEnabled(can and not self.all_cb.isChecked())
        if not can and self.auto_roll_cb.isChecked():
            self._stop_auto("能量用完了")
        self.roll_btn.setToolTip(self._roll_tip + why)

    def _refresh(self) -> None:
        got = self._read()
        if got is None:
            self.energy_lbl.setText("能量 —")
            self.cur_lbl.setText("目前選中 —")
            self._set_buttons(None)
            return
        self._set_buttons(got.energy)
        self.energy_lbl.setText(f"能量 {got.energy}（還能按 {got.energy} 次）")
        self._clamp_limit(got.energy)
        self.cur_lbl.setText(
            f"目前選中 {got.result_name(self._names)}"
            + (f"　每次 {got.per_roll} 點" if got.per_roll else ""))
        for i, cb in enumerate(self._boxes):
            cb.setText(f"{self._names[i]}　{got.points[i]:,}")

    # ------------------------------------------------------------------
    def _mover(self, pid: int) -> move.Mover | None:
        mv = self._movers.get(pid)
        if mv is not None:
            return mv if mv.active else None
        try:
            mv = move.Mover(pid, injector.process_path(pid))
            mv.start()
        except Exception as exc:                    # noqa: BLE001
            self.status.setText(f"⚠ 無法安裝跳板：{exc}")
            self._movers[pid] = move.Mover(pid, "")
            return None
        self._movers[pid] = mv
        return mv

    def _do(self, what: str, fn) -> bool:
        pid, _sc, _st = self._cur()
        if pid is None:
            self.status.setText("請先選一個分身")
            return False
        mv = self._mover(pid)
        if mv is None:
            return False
        t0 = time.time()
        ok = fn(mv)
        self.status.setText(
            f"{'✔ 已送出' if ok else '⚠ 送不出去（指令槽忙碌，再按一次）'}"
            f"　{self.who.currentText()}　{what}"
            f"　{(time.time() - t0) * 1000:.0f} ms")
        return ok

    def _log(self, action: str, result: str, credit: str = "",
             energy_left="") -> None:
        """記一列到**目前這個分身自己的**歷史。

        ⚠ 每個分身各自一份，不混在一起（使用者要求）—— 混著看根本分不出
          「精準 +20」是誰的。切分身時整張表換成那一台的。
        """
        pid = self.who.currentData()
        if pid is None:
            return
        row = (time.strftime("%H:%M:%S"), action, result, credit,
               str(energy_left))
        rows = self._logs.setdefault(int(pid), [])
        rows.insert(0, row)
        del rows[LOG_MAX:]
        self._show_log(int(pid))

    def _show_log(self, pid: int | None) -> None:
        """把某個分身的歷史畫進表格。pid 為 None 就清空。"""
        rows = self._logs.get(pid, []) if pid is not None else []
        self.log.setRowCount(len(rows))
        for r, cells in enumerate(rows):
            for c, text in enumerate(cells):
                self.log.setItem(r, c, QTableWidgetItem(text))

    def _roll(self) -> bool:
        before = self._read()
        if before is not None and before.energy <= 0:
            self.status.setText("⚠ 能量是 0，按了也不會有事發生")
            return False
        ok = self._do("能量晶化", energy.roll)
        if ok:
            # ★ 記下按之前的狀態：這一次的點數是入帳到「按之前選中的那個屬性」，
            #   所以要靠前後比對才算得出「實際入帳了幾點到誰身上」。
            self._pre = before
            # 結果要等一下才寫進記憶體（實測 106ms），用 singleShot 不卡畫面。
            # ⚠ 把 pid 一起帶著：等這 0.3 秒的期間使用者可能換了分身，
            #   沒帶的話結果會記到別人頭上。
            pid = self.who.currentData()
            QTimer.singleShot(SETTLE_MS, lambda: self._after_roll(pid))
        return ok

    # ------------------------------------------------------------------
    # -- 自動晶化 -------------------------------------------------------
    def _toggle_auto(self, on: bool) -> None:
        self._save()
        if not on:
            self._auto_timer.stop()
            self.auto_lbl.setText("")
            return
        got = self._read()
        if got is None:
            self.auto_roll_cb.setChecked(False)
            self.status.setText("⚠ 讀不到狀態，先選一個分身")
            return
        if got.energy <= 0:
            self.auto_roll_cb.setChecked(False)
            self.status.setText("⚠ 能量是 0，沒得按")
            return
        self._auto_n = 0
        self._auto_timer.start(int(self.interval.value() * 1000))
        self._auto_tick()                       # 立刻按第一次，不要空等一輪

    def _stop_auto(self, why: str) -> None:
        self._auto_timer.stop()
        self.auto_roll_cb.blockSignals(True)
        self.auto_roll_cb.setChecked(False)
        self.auto_roll_cb.blockSignals(False)
        self.auto_lbl.setText(f"　已停止：{why}（共按 {self._auto_n} 次）")

    def _auto_tick(self) -> None:
        """自動晶化的一拍。每一個停止條件都在這裡檢查，漏一個就會停不下來。"""
        if not self.auto_roll_cb.isChecked():
            self._auto_timer.stop()
            return
        got = self._read()
        if got is None:
            self._stop_auto("讀不到狀態（換地圖／重連？）")
            return
        if got.energy <= 0:
            self._stop_auto("能量用完了")
            return
        # 勾了「晶化全部能量」就不看次數，只靠能量歸零收工
        if self.all_cb.isChecked():
            limit = 0
        else:
            limit = int(self.limit.value())
            # ⚠ 0 **不是**「不限」（舊版是，那會變成無限狂按到能量歸零）
            if limit <= 0:
                self._stop_auto("次數設成 0")
                return
            if self._auto_n >= limit:
                self._stop_auto(f"到達設定的 {limit} 次")
                return
        if not self._roll():
            self.auto_lbl.setText("　指令槽忙碌，下一拍再試")
            return
        self._auto_n += 1
        self.auto_lbl.setText(
            f"　自動晶化中：第 {self._auto_n} 次"
            + (f" / {limit}" if limit else
               f"（用完全部，還剩 {got.energy - 1}）"
               if self.all_cb.isChecked() else "")
            + f"　剩餘能量 {got.energy - 1}")

    def _double(self, _checked: bool = False, auto: bool = False) -> bool:
        before = self._read()
        ok = self._do(("自動加倍" if auto else "我要晶能加倍"), energy.double)
        if ok:
            # 加倍成不成功看「下次入帳點數」有沒有變大，也要等它寫進記憶體
            pid = self.who.currentData()
            QTimer.singleShot(
                SETTLE_MS + 300, lambda: self._after_double(before, auto, pid))
        elif auto:
            self._log("加倍", "⚠ 沒送出去（指令槽忙碌）")
        return ok

    def _after_double(self, before, auto: bool, pid=None) -> None:
        if pid is not None and self.who.currentData() != pid:
            return                             # 中途換了分身，這筆不算他的
        got = self._read()
        self._refresh()
        if got is None or before is None:
            self._log("加倍", "讀不到結果")
            return
        if got.per_roll > before.per_roll:
            self._log("加倍", f"★ 成功　下次入帳 {got.per_roll} 點",
                      energy_left=got.energy)
        else:
            self._log("加倍", f"沒中　下次入帳 {got.per_roll} 點",
                      energy_left=got.energy)

    def _after_roll(self, pid=None) -> None:
        if pid is not None and self.who.currentData() != pid:
            return                             # 中途換了分身，這筆不算他的
        got = self._read()
        self._refresh()
        pre = self._pre
        self._pre = None
        if got is None:
            self._log("晶化", "讀不到結果")
            return
        # 這一次入帳到「按之前選中的那個屬性」—— 前後相減才知道實際進了幾點
        credit = ""
        if pre is not None and pre.result is not None:
            delta = got.points[pre.result] - pre.points[pre.result]
            if delta:
                credit = f"{self._names[pre.result]} +{delta}"
        name = got.result_name(self._names)
        self._log("晶化", f"抽到 {name}", credit, got.energy)

        if got.result is None:
            self.status.setText(self.status.text() + "　（讀不到結果，沒有加倍）")
            return
        if not self.auto_cb.isChecked():
            return
        if not self._boxes[got.result].isChecked():
            self.status.setText(f"抽到「{name}」——沒勾，不加倍")
            return
        if not self._double(auto=True):
            self.status.setText(f"抽到「{name}」→ ⚠ 加倍沒送出去，請手動按一次")

    # ------------------------------------------------------------------
    def _key(self, name: str) -> str:
        return f"energy.{name}"

    def _load(self) -> None:
        want = config.get(self._key("watch"), []) or []
        for i, cb in enumerate(self._boxes):
            cb.blockSignals(True)
            cb.setChecked(i in want)
            cb.blockSignals(False)
        self.auto_cb.blockSignals(True)
        self.auto_cb.setChecked(bool(config.get(self._key("auto"), True)))
        self.auto_cb.blockSignals(False)
        for widget, key, default in ((self.interval, "interval",
                                      DEFAULT_INTERVAL),
                                     (self.limit, "limit", DEFAULT_LIMIT)):
            widget.blockSignals(True)
            val = type(widget.value())(config.get(self._key(key), default))
            # ⚠ 舊設定檔可能存著負數、或間隔存著比下限小的值 —— 當成沒設過。
            #   （次數的 0 現在是合法值：能量 0 的人就該看到 0。）
            if val < widget.minimum():
                val = default
            widget.setValue(val)
            widget.blockSignals(False)
        # 使用者「想要」的次數。能量歸零把值夾成 0 之後，能量回來要靠它復原。
        self._limit_want = max(1, int(self.limit.value()))
        self.all_cb.blockSignals(True)
        self.all_cb.setChecked(bool(config.get(self._key("all"), False)))
        self.all_cb.blockSignals(False)
        self.limit.setEnabled(not self.all_cb.isChecked())
        # ★ 「自動晶化」刻意**不記住**：每次開工具箱都是關的。
        #   它會一直花能量，不該因為上次忘了關就自己跑起來。
        #   （「晶化全部能量」有記住，但它只是模式 —— 真正的開關是自動晶化。）

    def _save(self) -> None:
        # 只有使用者自己動過才更新「想要的次數」——
        # _clamp_limit 改值時是 blockSignals 的，不會走到這裡。
        self._limit_want = int(self.limit.value())
        config.set(self._key("watch"),
                   [i for i, cb in enumerate(self._boxes) if cb.isChecked()])
        config.set(self._key("auto"), self.auto_cb.isChecked())
        config.set(self._key("interval"), float(self.interval.value()))
        config.set(self._key("limit"), int(self.limit.value()))
        config.set(self._key("all"), self.all_cb.isChecked())
        config.save()

    def on_close(self) -> None:
        self._auto_timer.stop()
        for mv in self._movers.values():
            try:
                mv.stop()
            except Exception:                       # noqa: BLE001
                pass
        self._movers.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
