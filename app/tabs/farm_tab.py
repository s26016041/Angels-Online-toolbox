"""自動掛機分頁：列出附近怪物，選一隻，然後持續送攻擊按鍵。

運作原理（見 app/game/entity.py 的說明）
----------------------------------------
遊戲攻擊時會**自己重讀**「客戶端目前選定的目標」那個欄位，所以只要：
    ① 把目標的實體 ID 寫進狀態物件 +0x2D8
    ② 送一個攻擊按鍵（預設 F2）給該視窗
角色就會打那隻怪。不必注入會執行的程式碼、不必自己組封包、不必搶視窗焦點
（背景視窗吃得到鍵盤訊息，吃不到滑鼠點擊 —— 所以選目標非走記憶體不可）。

打哪一隻：**離玩家最近的那一隻**。實體串列沒有順序，直接拿第一隻常常是天邊那隻
——先前「有時打得到、有時打不到」就是這個原因。現在怪和玩家都讀得到座標，
排序取最近的即可；而且遊戲的攻擊指令內建自動接近，選最近的只是為了少走路。

介面
----
每個分身一個子分頁（標題就是角色名）。各自可以掃描周圍怪物、選一隻，
勾「開始掛機」才會開始迴圈，取消勾選就停。

掃描要跑全記憶體（約 1～3 秒），所以放在背景執行緒，而且只在按下按鈕或
勾選掛機時才掃 —— 不做無謂的定期重掃。
"""
from __future__ import annotations

import ctypes
import math
import time

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core import charname
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import entity
from app.tabs.base_tab import BaseTab

VK_F2 = 0x71
DEFAULT_INTERVAL = 0.05         # 秒；每秒送 20 次 F2
TICK_MS = 10                    # 迴圈計時器解析度（要比送鍵間隔小才吃得滿）
RESCAN_GAP = 0.3                # 清單裡真的沒得打了，才重掃（一次約 0.5 秒）
STATUS_GAP = 0.2                # 狀態列多久重畫一次（心跳 10ms，不必每拍都畫）
NEAR_HEIGHT = 130               # 「周圍怪物」清單高度；使用者要求小一點
STUCK_SECS = 10.0               # 沒掉血、玩家也沒移動這麼久 → 這隻走不過去，換一隻

# 攻擊半徑（格）。使用者實測：人站在 (154,40) 時打得到 (130,49) 的怪，
# 再遠就打不到 —— √(24²+9²) ≈ 25.6，也就是以角色為圓心的一個圓。
# 半徑內的怪優先打；半徑內沒有時仍會挑最近的（遊戲的攻擊指令會自己走過去），
# 免得站在原地發呆。可在介面上調整。
ATTACK_RADIUS = 26.0


def _send_scan(hwnd: int) -> None:
    """對指定視窗送一次 F2。

    走 app/core/window.py 的共用實作：SendMessageTimeout + 帶掃描碼的 lParam。
    ⚠ 實測這個遊戲**只吃 SendMessage**，PostMessage 完全沒反應（不管 lParam）。
    不碰使用者真正的鍵盤、不搶焦點，可以同時掛多個分身。
    """
    win.send_key(hwnd, VK_F2)


class AttackWorker(QThread):
    """一個分身的攻擊迴圈，跑在**自己的執行緒**上。

    為什麼不掛在 UI 的 QTimer 上（原本的做法）：
      · Qt 計時器不精準，UI 一忙節奏就漂掉 —— 使用者的感受就是「很卡」。
      · SendMessageTimeout 會等對方處理完才返回（平均 4.6ms，遊戲卡住時
        會用滿 200ms 逾時），放在 UI 執行緒等於拿遊戲的卡頓來凍自己的畫面。
      · 五個分身共用一條也不行：節奏互相排擠，一台卡住其他四台跟著餓死。

    所以一個分身一條執行緒，只做這個循環：
        讀目標 → 判斷死亡 → 寫回目標 → 送一次 F2（按+放）→ 睡到下一拍

    ⚠ 送鍵一定是**一次次按放**。曾經想改成「只送 KEYDOWN 模擬按住」，
      方向是錯的 —— 使用者實測這個遊戲按住不放並不會一直放技能，
      他自己就是狂點鍵盤的。
    """

    died = Signal(int)          # 目標倒了（實體 ID）→ UI 執行緒去挑下一隻
    failed = Signal(str)        # 讀寫記憶體失敗

    def __init__(self, hwnd: int, sc: MemoryScanner) -> None:
        super().__init__()
        self.hwnd = hwnd
        self.sc = sc
        self.hp = 0                 # 最近讀到的目標血量，給 UI 顯示
        self._job: tuple[int, entity.Entity] | None = None
        self._interval = DEFAULT_INTERVAL
        self._wrote = False
        self._running = True

    # -- 以下三個由 UI 執行緒呼叫 --------------------------------------
    def set_interval(self, secs: float) -> None:
        self._interval = max(0.02, secs)

    def attack(self, state: int, ent: entity.Entity) -> None:
        self._wrote = False
        self._job = (state, ent)

    def hold_off(self) -> None:
        """停手。"""
        self._job = None

    # ------------------------------------------------------------------
    def run(self) -> None:
        # ⚠ Windows 的 Sleep() 預設精度只有約 15.6ms —— msleep(50) 可能睡成 62ms，
        # 節奏會忽快忽慢（就是「很卡」的來源之一）。timeBeginPeriod(1) 把系統
        # 計時器解析度調到 1ms；這是行程層級的設定，用完一定要成對還原。
        ctypes.windll.winmm.timeBeginPeriod(1)
        try:
            self._loop()
        finally:
            ctypes.windll.winmm.timeEndPeriod(1)

    def _loop(self) -> None:
        due = time.perf_counter()
        while self._running:
            job = self._job
            if job is None:
                self.msleep(5)
                due = time.perf_counter()
                continue
            state, ent = job
            try:
                # ★ 先讀後判斷、最後才寫 —— 順序不能換。
                # 目標死掉時遊戲會把 +0x2D8 清成 0，那是我們唯一的死亡訊號；
                # 先寫回去就把訊號蓋掉了。
                cur = entity.read_target(self.sc, state)
                self.hp = entity.read_target_hp(self.sc, state)
                if self._wrote and (cur == 0
                                    or not entity.is_alive(self.sc, ent)):
                    self._job = None
                    self._wrote = False
                    self.died.emit(ent.eid)
                    continue

                # ⚠ **每一圈都要重寫兩個欄位**，不能「已經是這隻就跳過」：
                # 遊戲會把 +0x2DC（目標血量%）改回 0，而攻擊前會檢查它 > 0
                # （`cmp [esi+0x2dc],0 / jle 跳過`）。只寫一次的話之後全被跳掉。
                entity.set_target(self.sc, state, ent.eid)
                self._wrote = True
                _send_scan(self.hwnd)
            except Exception as exc:           # noqa: BLE001
                self._job = None
                self.failed.emit(str(exc))
                continue
            # 睡「到下一拍為止」，不是「睡一個間隔」—— 送鍵本身要 3~5ms，
            # 直接睡固定長度的話週期會變成 間隔+送鍵時間，節奏也會隨遊戲卡頓漂移。
            due += self._interval
            left = due - time.perf_counter()
            if left > 0:
                self.msleep(int(left * 1000) or 1)
            else:
                due = time.perf_counter()      # 落後太多就重新對時，不要補償性狂送

    def stop(self) -> None:
        self._running = False
        self._job = None


class ScanWorker(QThread):
    """背景掃描：一次拿齊狀態物件、玩家物件、附近怪物。

    全記憶體掃描一次約 1～3 秒，不能放在 UI 執行緒。
    用一條常駐執行緒處理所有分身的請求，比每次開新執行緒好收尾。
    三種物件走 entity.snapshot() 合併成一遍讀取 —— 掃三遍會慢將近三倍。
    """

    done = Signal(int, object, object, object, str)  # pid, state, 玩家, 怪, err

    def __init__(self) -> None:
        super().__init__()
        self._queue: list[tuple[int, MemoryScanner]] = []
        self._running = True

    def request(self, pid: int, sc: MemoryScanner) -> None:
        if not any(p == pid for p, _ in self._queue):
            self._queue.append((pid, sc))

    def run(self) -> None:
        while self._running:
            if not self._queue:
                self.msleep(80)
                continue
            pid, sc = self._queue.pop(0)
            state = player = mons = None
            err = ""
            try:
                state, player, ents = entity.snapshot(
                    sc, should_stop=lambda: not self._running)
                mons = [e for e in ents if e.is_monster]
                if state is None:
                    err = "找不到狀態物件（掃到 0 個或多個）"
                elif player is None:
                    err = "找不到玩家物件（掃到 0 個或多個）"
            except Exception as exc:               # noqa: BLE001
                err = f"掃描失敗：{exc}"
            if self._running:
                self.done.emit(pid, state, player, mons, err)

    def stop(self) -> None:
        self._running = False


class CharFarmPage(QWidget):
    """單一分身的掛機介面。"""

    def __init__(self, pid: int, hwnd: int, title: str,
                 sc: MemoryScanner, on_scan, atk: AttackWorker) -> None:
        super().__init__()
        self.pid = pid
        self.hwnd = hwnd
        self.title = title
        self.sc = sc
        # 攻擊迴圈（寫目標 + 送 F2）整個在這條執行緒上跑，本頁只負責挑目標。
        self._atk = atk
        atk.died.connect(self._on_died)
        atk.failed.connect(lambda msg: self._stop_with(f"⚠ 記憶體存取失敗：{msg}"))
        self.state: int | None = None
        self.player: int | None = None           # 玩家物件位址（拿來讀自己的座標）
        self.mons: list[entity.Entity] = []
        self._on_scan = on_scan
        self._cur: entity.Entity | None = None   # 正在打的那隻
        self._kills = 0
        self._waiting = False      # 正在等重新掃描的結果
        self._since_scan = 0.0     # 距離上次自動重掃過了多久
        self._stuck = 0.0          # 打不到也走不到的時間（卡住偵測）
        self._show = 0.0           # 距離上次重畫狀態列過了多久
        self._last_hp = -1
        self._last_pos: tuple[float, float] | None = None
        # 這輪掃描裡已經打死（或判定走不過去）的實體 ID。
        # 怪死掉後物件不會馬上被回收，is_alive() 可能還是 true —— 沒這層擋著，
        # 換下一隻時會又挑到同一具屍體。每次重掃後清空（新的掃描結果才算數）。
        self._killed: set[int] = set()

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.scan_btn = QPushButton("掃描周圍怪物")
        self.scan_btn.clicked.connect(lambda: self._on_scan(self.pid))
        bar.addWidget(self.scan_btn)
        bar.addSpacing(12)
        bar.addWidget(QLabel("每隔"))
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.02, 5.0)
        self.interval.setSingleStep(0.01)
        self.interval.setDecimals(2)
        self.interval.setValue(DEFAULT_INTERVAL)
        self.interval.setSuffix(" 秒按一次 F2")
        self.interval.setFixedWidth(150)
        self.interval.valueChanged.connect(self._atk.set_interval)
        self._atk.set_interval(DEFAULT_INTERVAL)
        bar.addWidget(self.interval)
        bar.addSpacing(12)
        bar.addWidget(QLabel("攻擊半徑"))
        self.radius = QDoubleSpinBox()
        self.radius.setRange(1.0, 200.0)
        self.radius.setSingleStep(1.0)
        self.radius.setDecimals(1)
        self.radius.setValue(ATTACK_RADIUS)
        self.radius.setSuffix(" 格")
        self.radius.setFixedWidth(90)
        self.radius.setToolTip(
            "以角色為圓心、這個半徑內的怪優先打（實測約 25.6 格）。\n"
            "半徑內沒有選中的怪時，仍會挑最近的一隻 —— 遊戲的攻擊指令\n"
            "會讓角色自己走過去，只是要多花點時間。")
        bar.addWidget(self.radius)
        bar.addSpacing(12)
        self.run_cb = QCheckBox("開始掛機")
        self.run_cb.setToolTip(
            "勾選後開始迴圈：把「選中怪物」那一種寫進遊戲的目前目標，並持續送 F2。\n"
            "打死會自動接同名的下一隻。取消勾選立刻停止。")
        self.run_cb.toggled.connect(self._on_toggle)
        bar.addWidget(self.run_cb)
        bar.addStretch(1)
        root.addLayout(bar)

        # 兩個小區塊：左邊是要打哪些怪（可多選、可手動輸入），右邊是附近有什麼。
        # 一律只顯示中文名字 —— 比對也是用名字，所以手動打字才會通。
        panes = QHBoxLayout()

        left = QGroupBox("選中怪物")
        left.setFixedWidth(190)
        lv = QVBoxLayout(left)
        self.picked = QListWidget()
        self.picked.setFixedHeight(NEAR_HEIGHT)
        self.picked.setSelectionMode(QListWidget.ExtendedSelection)
        lv.addWidget(self.picked)
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("手動輸入後按 Enter")
        self.manual.returnPressed.connect(self._add_manual)
        lv.addWidget(self.manual)
        panes.addWidget(left)

        # 中間的刪除鈕：把「選中怪物」裡選起來的移除
        mid = QVBoxLayout()
        mid.addStretch(1)
        # 用 ASCII 的 X，不要用 ✕ —— 部分中文字型沒有那個字形，會變成豆腐。
        # ⚠ 還要覆寫 padding：主題給 QPushButton 的是 `padding: 6px 14px`，
        #   左右各 14px 就吃掉 28px，小按鈕會完全看不到文字（踩過）。
        self.del_btn = QPushButton("X")
        self.del_btn.setFixedSize(32, 32)
        self.del_btn.setStyleSheet("padding: 0;")
        self.del_btn.setToolTip("把「選中怪物」裡選起來的項目刪掉")
        self.del_btn.clicked.connect(self._remove_picked)
        mid.addWidget(self.del_btn)
        mid.addStretch(1)
        panes.addLayout(mid)

        right = QGroupBox("周圍怪物")
        rv = QVBoxLayout(right)
        self.near = QListWidget()
        self.near.setFixedHeight(NEAR_HEIGHT)
        self.near.setSelectionMode(QListWidget.ExtendedSelection)
        self.near.itemClicked.connect(lambda it: self._add_name(it.text()))
        rv.addWidget(self.near)
        panes.addWidget(right, 1)
        root.addLayout(panes)

        self.status = QLabel("尚未掃描")
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)
        root.addStretch(1)

    # ------------------------------------------------------------------
    # -- 選中怪物清單 --------------------------------------------------
    def wanted(self) -> list[str]:
        return [self.picked.item(i).text() for i in range(self.picked.count())]

    def _add_name(self, name: str) -> None:
        name = name.strip()
        if name and name not in self.wanted():
            self.picked.addItem(name)

    def _add_manual(self) -> None:
        self._add_name(self.manual.text())
        self.manual.clear()

    def _remove_picked(self) -> None:
        for it in self.picked.selectedItems():
            self.picked.takeItem(self.picked.row(it))

    # ------------------------------------------------------------------
    def my_pos(self) -> tuple[float, float] | None:
        """玩家目前的格子座標（每次都重讀，因為角色會走動）。"""
        if self.player is None:
            return None
        return entity.read_pos(self.sc, self.player)

    def apply_scan(self, state, player, mons, err: str) -> None:
        self.state = state
        self.player = player
        self.mons = mons or []
        self._killed.clear()       # 新的掃描結果才算數；殘留的屍體靠死亡偵測擋
        # 只列中文名字（去重、不顯示數量、不顯示任何 ID）
        self.near.clear()
        seen = []
        for m in self.mons:
            if m.name not in seen:
                seen.append(m.name)
        self.near.addItems(seen)
        self.scan_btn.setEnabled(True)
        self._waiting = False

        if err:
            self.status.setText(f"⚠ {err}")
            return

        # 掛機中且正在等下一隻 → 自動挑名字在清單裡、離自己最近的接上去
        if self.run_cb.isChecked() and self._cur is None:
            if not self._pick_next():
                self.status.setText(
                    f"附近沒有選中的怪了（已擊殺 {self._kills} 隻）→ 等新的出現…")
            return
        self.status.setText(f"找到 {len(self.mons)} 隻，"
                            f"{self.near.count()} 種")

    def _pick_next(self) -> bool:
        """挑一隻名字在「選中怪物」裡、**離自己最近**的接著打；挑不到回傳 False。

        用名字比對而不是種類 ID，因為使用者要能手動輸入怪物名稱。

        ★ 一定要按距離排序。實體串列是靠掃 vtable 得到的，順序等同記憶體位址，
          直接拿第一隻常常是地圖另一頭那隻 —— 這就是先前「有時打得到、
          有時打不到」的原因。現在怪和玩家都讀得到座標，排序即可，
          不需要一隻一隻試。

        ★ 這裡**不重掃記憶體**，直接用上一次掃描的清單。一張圖上通常有十幾隻，
          打死一隻就重掃等於白等 2 秒（1.5 秒間隔 + 0.5 秒掃描），
          而換下一隻其實只要挑清單裡還活著的即可 —— 這是換怪速度的關鍵。
          清單裡真的沒得打了，才由 tick() 去排重掃。
        """
        want = self.wanted()
        me = self.my_pos()
        pool = []
        for m in self.mons:
            if m.name not in want or m.eid in self._killed:
                continue
            if not entity.is_alive(self.sc, m):
                continue
            # 座標要當場重讀：怪會走、角色也在走，掃描時記的早就過期了
            p = entity.read_pos(self.sc, m.addr)
            pool.append((math.hypot(p[0] - me[0], p[1] - me[1])
                         if p and me else float("inf"), m))
        pool.sort(key=lambda t: t[0])
        if not pool:
            return False
        # 半徑內的優先；半徑內沒有就挑最近的（遊戲會自己走過去，不要站在原地）
        r = self.radius.value()
        inside = [t for t in pool if t[0] <= r]
        d, self._cur = (inside or pool)[0]
        self._stuck = 0.0
        self._last_hp = -1
        self._last_pos = me
        self._atk.attack(self.state, self._cur)     # 交給攻擊執行緒，立刻開打
        self.status.setText(
            f"鎖定「{self._cur.name}」　距離 {d:.1f} 格"
            + ("" if inside else "（半徑外，會自己走過去）")
            + f"　累計擊殺 {self._kills}")
        return True

    def _on_died(self, eid: int) -> None:
        """攻擊執行緒回報目標倒了 —— 立刻從既有清單接下一隻。

        不重掃記憶體：重掃要 0.5 秒還要排隊，每殺一隻就等一次會非常卡。
        清單裡真的沒得打了，才由 tick() 去排重掃。
        """
        m = self._cur
        self._kills += 1
        self._killed.add(eid)     # 物件可能還沒被回收，記下來免得又打同一具屍體
        self._cur = None
        if not self.run_cb.isChecked():
            return
        if not self._pick_next():
            self._since_scan = RESCAN_GAP         # 清單空了，才排重掃
            self.status.setText(
                f"「{m.name if m else ''}」倒了（累計 {self._kills} 隻）→ 重新掃描…")

    def _on_toggle(self, on: bool) -> None:
        if not on:
            self._atk.hold_off()
            self.status.setText(f"已停止（本次擊殺 {self._kills} 隻）")
            self._cur = None
            return
        if self.state is None or self.player is None:
            self.run_cb.setChecked(False)
            QMessageBox.information(self, "還不能開始",
                                    "請先按「掃描周圍怪物」。")
            return
        want = self.wanted()
        if not want:
            self.run_cb.setChecked(False)
            QMessageBox.information(
                self, "還不能開始",
                "「選中怪物」是空的。點右邊的名字加進來，或自己打字後按 Enter。")
            return
        self._kills = 0
        self._killed.clear()
        self._since_scan = RESCAN_GAP      # 清單裡挑不到的話，立刻重掃
        self._cur = None
        self.status.setText("掛機中：只打「" + "、".join(want) + "」")

    def tick(self, dt: float) -> None:
        """UI 側的心跳：只做「挑目標、卡住偵測、更新狀態列」。

        真正的攻擊迴圈（寫目標 + 送 F2）在 AttackWorker 自己的執行緒上跑，
        節奏不受 UI 影響 —— 原本整個迴圈掛在這裡，UI 一忙節奏就漂掉，
        使用者的感受就是「很卡」。
        """
        if not self.run_cb.isChecked() or self.state is None:
            return
        self._since_scan += dt
        if self._waiting:                   # 正在等重新掃描的結果
            return
        if self._cur is None:
            # 清單裡挑得到就直接接上（不必等重掃）；真的沒了才排重掃
            if self._pick_next():
                return
            if self._since_scan >= RESCAN_GAP:
                self._since_scan = 0.0
                self._waiting = True
                self._on_scan(self.pid)
            return

        m = self._cur
        hp = self._atk.hp
        me = self.my_pos()

        # 卡住偵測（次要保險，不是主要機制）：目標已經是最近的一隻，
        # 正常情況下不是打得到就是角色正在走過去。若血量不掉、玩家座標也不動，
        # 代表這隻走不過去（隔著地形之類），換一隻。
        moving = (me is not None and self._last_pos is not None
                  and (abs(me[0] - self._last_pos[0]) > 0.2
                       or abs(me[1] - self._last_pos[1]) > 0.2))
        if moving or (0 < hp < self._last_hp) or self._last_hp < 0:
            self._stuck = 0.0
        else:
            self._stuck += dt
        self._last_pos = me
        self._last_hp = hp
        if self._stuck >= STUCK_SECS:
            self._killed.add(m.eid)   # 這隻走不過去，這輪別再挑它
            self._atk.hold_off()
            self._cur = None
            if not self._pick_next():
                self._since_scan = RESCAN_GAP
            self.status.setText(
                f"「{m.name}」{STUCK_SECS:.0f} 秒沒進展（走不過去？）→ 換一隻")
            return

        # 狀態列不必每一拍都重畫（心跳 10ms，重畫太頻繁反而拖慢 UI）
        self._show += dt
        if self._show < STATUS_GAP:
            return
        self._show = 0.0
        # 距離要用當下的座標算：怪會走、角色也在走，掃描時的座標早就過期了
        mp = entity.read_pos(self.sc, m.addr)
        d = (math.hypot(mp[0] - me[0], mp[1] - me[1])
             if mp and me else float("nan"))
        self.status.setText(
            f"掛機中：{m.name}　距離 {d:.1f} 格　血量 {hp}%"
            f"　累計擊殺 {self._kills}")

    def _stop_with(self, msg: str) -> None:
        self.run_cb.setChecked(False)
        self.status.setText(msg)


class FarmTab(BaseTab):
    TAB_TITLE = "自動掛機"
    ORDER = 5

    def build_ui(self) -> None:
        self._pages: dict[int, CharFarmPage] = {}
        self._scanners: list[MemoryScanner] = []
        self._worker: ScanWorker | None = None
        self._keys: list[AttackWorker] = []   # 每個分身一條攻擊執行緒

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.rescan_btn = QPushButton("重新偵測分身")
        self.rescan_btn.clicked.connect(self.reload_instances)
        bar.addWidget(self.rescan_btn)
        self.found = QLabel("尚未偵測")
        self.found.setStyleSheet("color: #9aa2b8;")
        bar.addWidget(self.found)
        bar.addStretch(1)
        root.addLayout(bar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        hint = QLabel(
            "① 按「掃描周圍怪物」→ ② 點右邊的名字加進「選中怪物」"
            "（可加多種，也可自己打字後按 Enter，選起來按 X 可刪除）"
            "→ ③ 勾「開始掛機」。\n"
            "會挑**離自己最近**的一隻持續送 F2；打死之後自動重掃、接著打下一隻，"
            "不必手動再選。取消勾選才停。不搶視窗焦點，可以同時掛多個分身。")
        hint.setStyleSheet("color: #9aa2b8;")
        root.addWidget(hint)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._pages:
            self.reload_instances()

    def reload_instances(self) -> None:
        self._teardown()
        insts = []
        seen = set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:
                continue
            self._scanners.append(sc)
            insts.append((w.pid, w.hwnd, w.title, sc))
        if not insts:
            self.found.setText("找不到分身")
            return
        self._worker = ScanWorker()
        self._worker.done.connect(self._on_scan_done)
        self._worker.start()
        for pid, hwnd, title, sc in insts:
            atk = AttackWorker(hwnd, sc)
            atk.start()
            self._keys.append(atk)
            page = CharFarmPage(pid, hwnd, title, sc, self._request_scan, atk)
            self._pages[pid] = page
            acct = charname.account_from_title(title)
            nm = charname.read_character_name(sc, acct) or acct
            self.tabs.addTab(page, nm)
        self.found.setText(f"偵測到 {len(insts)} 個分身")

    def _request_scan(self, pid: int) -> None:
        page = self._pages.get(pid)
        if page is None or self._worker is None:
            return
        page.scan_btn.setEnabled(False)
        page.status.setText("掃描中…（全記憶體掃描，約 1～3 秒）")
        self._worker.request(pid, page.sc)

    def _on_scan_done(self, pid: int, state, player, mons, err: str) -> None:
        page = self._pages.get(pid)
        if page is not None:
            page.apply_scan(state, player, mons, err)

    def _tick(self) -> None:
        dt = TICK_MS / 1000.0
        for page in self._pages.values():
            page.tick(dt)

    # ------------------------------------------------------------------
    def _teardown(self) -> None:
        for page in self._pages.values():
            page.run_cb.setChecked(False)
        for th in ([self._worker] if self._worker else []) + self._keys:
            th.stop()
            th.wait(5000)
        self._worker = None
        self._keys = []
        self.tabs.clear()
        self._pages = {}
        for sc in self._scanners:
            try:
                sc.close()
            except Exception:
                pass
        self._scanners = []

    def on_close(self) -> None:
        self._timer.stop()
        self._teardown()
