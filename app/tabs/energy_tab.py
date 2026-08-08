"""能量晶化分頁。

做四件事：
  1. 顯示還剩多少能量（＝還能按幾次）與各屬性目前累積的點數
  2. 12 個屬性可以勾選
  3. 按「能量晶化」之後讀出這次抽到什麼；**抽到勾選的就自動按一次「我要晶能加倍」**
  4. 自動分解小背包：每 3 秒把背包裡的「充能-小背包(20)/(30)」一次全拆成
     晶能（一拍上限 energy.MAX_PER_TICK 顆，剩的下一拍接著），
     **拆完不停**、一直盯到使用者按「暫停」為止，
     **只認這兩種**（energy.DECOMP_ITEMS 白名單＋送包前當場重驗每一格）
  5. 自動分解全部（紅字，比較危險）：同一套流程，但條件換成「遊戲自己認
     可以拆的」—— **不寫死名單，三個欄位全部讀記憶體**
     （`bag.Item.decomposable`：分類 46 紙娃娃、分解值 > 0、沒時限、
     不在身上的裝扮欄）。改版自動跟上。
     ★ 只動**一般背包**（格 0x14~0xA9）—— 玩家的裝扮收在遊戲自己分區的
       「紙娃娃隨身包」（格 251 起），碰不到。範圍那道關在 energy._slot_ok，
       不是分頁自己守的。
     ⚠ 開跑前會列出即將被拆的東西讓使用者確認（點裝掉在一般背包也會被拆）。

背後都是 `app/game/energy.py`：呼叫遊戲自己的泛用送包函式
`0x5D3D97(0x38, 1)` / `0x5D3D97(0x39, -1)`，欄位讀狀態物件 +0xB8 起那一段。

⚠ 晶化資料是「用到才同步」：剛上線那段記憶體全 0，要等客戶端送過
  `0x5D3D97(0x3F, 0)`（＝遊戲裡打開晶能視窗）伺服器才會把數字給下來。
  「同步資料」按鈕就是替使用者送這一包。

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
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app import theme
from app.config import config
from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import bag, energy, itemname, locate, move
# fit_spin：數字框寬度照最大值算 —— 寫死的話上下箭頭會把數字擠掉
# （使用者回報過「框框被砍到一半」）。
from app.tabs.base_tab import BaseTab, fit_spin

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
# 自動分解小背包的節奏：使用者定的「每三秒拆一顆」。
DECOMP_MS = 3000
# ⚠⚠ 狀態物件定位失敗後，隔多久才准再全掃一次。
#   沒有這道節流的話：`_read()` 讀失敗會把快取丟掉，下一次 0.5 秒的更新就又
#   叫一次 `entity.locate_state()`（**全記憶體掃描，0.4~1 秒，而且是在 GUI
#   執行緒上**）。角色在登入畫面、換地圖、重連時定位本來就會失敗，於是
#   「每 0.5 秒卡 0.5 秒以上」——畫面等於整個凍住。跟 watcher 的
#   RELOCATE_GAP_SECS 用同一個值。
STATE_RELOCATE_GAP = 5.0


class EnergyTab(BaseTab):
    TAB_TITLE = "能量晶化"
    ORDER = 45

    def build_ui(self) -> None:
        self._movers: dict[int, move.Mover] = {}
        self._scanners: dict[int, MemoryScanner] = {}
        # ⛔ 這裡以前另外存一份 `self._state`（pid -> 狀態物件）——**已刪除**。
        #    那是「同一個位址在第二個地方再存一次」：preload 那份會驗身分、
        #    這份不會，物件搬家之後這份就把過期位址交出去（畫面上的能量／
        #    屬性／點數整片垃圾值）。狀態物件現在只有 preload 一份，
        #    而且每次取用都當場驗（見 preload.state_of）。
        # pid -> 上次「全掃找狀態物件」的時間（見 STATE_RELOCATE_GAP）
        self._state_try: dict[int, float] = {}
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
        # ⚠ 說明文字要開自動換行，不然視窗窄的時候會被切掉半句。
        hint = QLabel(
            "按一下遊戲裡的「能量晶化」或「我要晶能加倍」。跟你手動點那兩顆按鈕"
            "送出的是同一個封包（呼叫遊戲自己的函式，加解密都由客戶端處理）。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        # ⚠ 換分身一定要停掉自動晶化 —— 不然會繼續對「新選的那台」按下去。
        self.who.currentIndexChanged.connect(self._on_who_changed)
        bar.addWidget(self.who)
        refresh = QPushButton("重新整理")
        refresh.setToolTip("重新列出目前開著的遊戲分身。")
        # ★ 按鈕要 force_names=True：同一台登出換角色 pid 不變，
        #   不強制重掃的話下拉選單永遠是舊角色名。
        refresh.clicked.connect(
            lambda: self.reload_instances(force_names=True))
        bar.addWidget(refresh)
        sync = QPushButton("同步資料")
        sync.setToolTip(
            "跟伺服器要一次晶化資料（＝遊戲裡打開晶能視窗時送的那包）。\n"
            "剛上線時伺服器還沒把資料同步下來，這裡讀到的全是 0 ——\n"
            "按這顆就不用進遊戲開視窗，約半秒後數字自己出現。\n"
            "⚠ 遊戲裡可能會跳出晶能視窗，直接關掉即可。")
        sync.clicked.connect(self._sync)
        bar.addWidget(sync)
        bar.addSpacing(16)
        self.energy_lbl = QLabel("能量 —")
        self.energy_lbl.setStyleSheet("font-weight: bold;")
        bar.addWidget(self.energy_lbl)
        bar.addSpacing(16)
        self.cur_lbl = QLabel("目前選中 —")
        bar.addWidget(self.cur_lbl)
        bar.addStretch(1)
        root.addLayout(bar)

        # --- 自動分解小背包（使用者指定：放在分身列與屬性之間） ---------
        drow = QHBoxLayout()
        self.decomp_btn = QPushButton("自動分解小背包")
        self.decomp_btn.setToolTip(
            "每 3 秒把背包裡的「充能-小背包(20)/(30)」**一次全部**拆解成晶能\n"
            "（＝遊戲分解分頁那顆「拆解」鈕）。\n"
            "★ 拆完**不會停**，繼續盯著背包 —— 掛機掉出新的下一拍就拆掉，\n"
            "　 要停請按旁邊的「暫停」。\n"
            f"一拍最多 {energy.MAX_PER_TICK} 顆，更多的下一拍接著拆。\n"
            "⚠⚠ **只認這兩種**，其他東西一律不碰 —— 送包前還會當場重讀\n"
            "　 背包逐格再驗一次。\n"
            "⚠ 進行中請不要手動搬動背包物品。")
        self.decomp_btn.clicked.connect(lambda: self._start_decomp(False))
        drow.addWidget(self.decomp_btn)
        # ⚠ 紅字（使用者指定）：這顆會把一般背包裡**所有**可分解的東西拆掉，
        #   拆掉就回不來了，跟只認兩種編號的那顆不是同一個風險等級。
        self.decomp_all_btn = QPushButton("自動分解全部")
        # ⚠ 一定要連 :disabled 一起寫：只寫 color 的話，widget 樣式的優先度會
        #   蓋掉主題的 QPushButton:disabled，跑起來按鈕變灰了字還是紅的。
        #   底色／邊框沿用主題（這裡只改文字顏色，兩張樣式表是疊加的）。
        self.decomp_all_btn.setStyleSheet(
            f"QPushButton {{ color: {theme.DANGER}; font-weight: bold; }}"
            "QPushButton:disabled { color: #6b7288; }")
        self.decomp_all_btn.setToolTip(
            "⚠⚠ 把**一般背包裡所有遊戲允許分解的東西**每 3 秒一次全部拆成晶能。\n"
            "　 條件照抄遊戲自己的判斷、而且是**現場讀記憶體**：\n"
            "　 分類是紙娃娃、分解值 > 0、沒有時限（限時道具遊戲不讓拆）。\n"
            "\n"
            "★ 只動一般背包（遊戲分區的「背包的slot」）。你的裝扮收在\n"
            "　 「紙娃娃隨身包」那一區，這顆碰不到，穿在身上的也碰不到。\n"
            "⚠⚠ 但**點裝／造型只要放在一般背包裡就會被拆掉**（水藍搖滾裝、\n"
            "　 水之補師 那類都在名單內，分解值只有 1~12 點）。\n"
            "　 開跑前請先確認一般背包裡沒有捨不得的裝扮。\n"
            "\n"
            "★ 拆完**不會停**，繼續盯著背包，要停請按旁邊的「暫停」。\n"
            f"一拍最多 {energy.MAX_PER_TICK} 件，更多的下一拍接著拆。")
        self.decomp_all_btn.clicked.connect(lambda: self._start_decomp(True))
        drow.addWidget(self.decomp_all_btn)
        self.pause_btn = QPushButton("暫停")
        self.pause_btn.setToolTip(
            "停下自動分解 —— 這是唯一的出口（不然它會一直盯著背包拆下去）。\n"
            "已經送出去的那幾顆不會收回。")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(lambda: self._stop_decomp("手動暫停"))
        drow.addWidget(self.pause_btn)
        self.decomp_lbl = QLabel("")
        self.decomp_lbl.setStyleSheet("color: #9aa2b8;")
        drow.addWidget(self.decomp_lbl)
        drow.addStretch(1)
        root.addLayout(drow)

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
        fit_spin(self.interval)
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
        fit_spin(self.limit)
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

        # 自動分解的狀態：送出去還沒從背包消失的 [(序號, 種類ID, 分解值), …]＋累計
        self._decomp_sent: list[tuple[int, int, int]] = []
        self._decomp_n = 0
        self._decomp_gain = 0
        # 這一輪是「全部」還是「只有小背包」。⚠ 一定要記在狀態裡，不能每拍
        # 去問按鈕 —— 兩顆鈕跑的是同一個計時器與同一份 _decomp_sent。
        self._decomp_all = False
        # 送出去以後撐了幾拍還沒消失（序號 → 拍數）。只拿來**提醒**，
        # 不改重試行為（暫時性失敗照規矩無限重試，出口是「暫停」）。
        self._decomp_rounds: dict[int, int] = {}
        self._decomp_timer = QTimer(self)
        self._decomp_timer.timeout.connect(self._decomp_tick)

        self._load()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_MS)

    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self, force_names: bool = False) -> None:
        # force_names：只有按「重新整理」才 True，on_show 自動載入走快取。
        self.who.blockSignals(True)
        self.who.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
        self._state_try.clear()      # 重新整理＝使用者要求重試，節流也一併歸零
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
            nm = preload.name_of(w.pid, sc, acc, force=force_names)
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
        pid = int(pid)
        # ⚠ allow_scan=False：只查快取、絕對不掃描，但**會先驗那份快取現在
        #   還是不是狀態物件**（一次 4-byte 讀取，微秒級）。
        #   以前這裡是 `self._state.get(pid) or preload.state_of(pid)` ——
        #   兩份都是沒驗過的舊位址，物件搬家之後照樣交出去，畫面上的能量／
        #   屬性／點數就整片是垃圾值，而且不會自己重新定位（見 energy.read）。
        st = preload.state_of(pid, sc, allow_scan=False)
        if not st:
            # 真的沒有 → 才需要全掃，而且要節流（見 STATE_RELOCATE_GAP）。
            now = time.monotonic()
            if now - self._state_try.get(pid, 0.0) >= STATE_RELOCATE_GAP:
                self._state_try[pid] = now
                st = preload.state_of(pid, sc)
        return pid, sc, st

    def _read(self):
        pid, sc, st = self._cur()
        if not st:
            return None
        got = energy.read(sc, st)
        if got is None:                            # 物件搬家了，下次重新定位
            preload.forget_state(pid)
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
        # ⚠ 自動分解也一樣：不停的話會拿「新選那台」的背包格號繼續送。
        if self._decomp_timer.isActive():
            self._stop_decomp("換了分身")
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
        # ★ 「全部是 0」幾乎一定是還沒同步（同步過的人 per_roll 至少是 10）——
        #   晶化資料是「用到才同步」，剛上線時伺服器根本還沒給。提示他按同步，
        #   不要讓他以為能量真的是 0。
        if not got.energy and not got.per_roll and not any(got.points):
            self.energy_lbl.setText("能量 0（還沒同步？按「同步資料」）")
        else:
            self.energy_lbl.setText(
                f"能量 {got.energy}（還能按 {got.energy} 次）")
        self._clamp_limit(got.energy)
        self.cur_lbl.setText(
            f"目前選中 {got.result_name(self._names)}"
            + (f"　每次 {got.per_roll} 點" if got.per_roll else ""))
        for i, cb in enumerate(self._boxes):
            cb.setText(f"{self._names[i]}　{got.points[i]:,}")

    # ------------------------------------------------------------------
    def _mover(self, pid: int) -> move.Mover | None:
        """拿這台分身的跳板。

        ⚠⚠ 一定要走 `move.acquire()`，**不要自己 new 一個 Mover** ——
          同一個遊戲行程只能有一份跳板。以前這裡自己裝一份，掛機分頁正在跑
          的時候按一下晶化，就會把掛機那份拆掉（它之後每個指令都「排不進去」
          而且不會自己好）。細節見 `move.acquire()` 的說明。
        """
        mv = self._movers.get(pid)
        if mv is not None and mv.active:
            return mv
        try:
            mv = move.acquire(pid, injector.process_path(pid), self)
        except Exception as exc:                    # noqa: BLE001
            # ⚠ 失敗**不要記進 _movers**：以前記一個 active=False 的空殼進去，
            #   上面那個 `if mv is not None` 就永遠成立 → 這台分身在關掉分頁
            #   之前再也裝不上跳板，使用者按幾次都沒反應（連「重新整理」
            #   也救不回來，因為那不清 _movers）。
            self._movers.pop(pid, None)
            self.status.setText(f"⚠ 無法安裝跳板：{exc}")
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

    def _sync(self) -> bool:
        """跟伺服器要一次晶化資料。回包後由每 0.5 秒的更新自己撿到。"""
        return self._do("同步資料", energy.sync)

    # ------------------------------------------------------------------
    # -- 自動分解（小背包／全部共用同一套流程）---------------------------
    def _confirm_all(self, found: list) -> bool:
        """「分解全部」開跑前的確認：把即將拆掉的東西逐項列出來。按取消回 False。

        ⚠ 只列**這一刻**背包裡的；開跑後掉出來的新東西照樣會被拆（這顆鈕
          本來就是「拆完不停」），所以文字要講清楚，不要讓人以為只拆這幾件。
        """
        rows: dict[str, list[int]] = {}
        for it in found:
            key = f"{itemname.label(it.type_id)}（{it.decomp_value} 點）"
            rows.setdefault(key, []).append(it.slot)
        lines = [f"　• {name}　×{len(s)}　（格 "
                 + "、".join(str(x) for x in sorted(s)) + "）"
                 for name, s in sorted(rows.items())]
        body = ("即將把一般背包裡**這些東西**拆成晶能：\n\n" + "\n".join(lines)
                if lines else
                "一般背包裡目前沒有可拆的東西。")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("自動分解全部")
        box.setText(body)
        box.setInformativeText(
            "⚠ 拆掉就**回不來了**。點裝／造型只要放在一般背包裡也會被拆掉"
            "（分解值通常只有 1~12 點）。\n"
            "★ 穿在身上的、以及收在「紙娃娃隨身包」的裝扮不會被碰。\n"
            "★ 拆完不會停，會一直盯著背包 —— 之後掉出來的新東西也會被拆，"
            "要停請按「暫停」。")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)   # 手滑按 Enter 不會開拆
        box.button(QMessageBox.Yes).setText("確定，開始拆")
        box.button(QMessageBox.Cancel).setText("取消")
        return box.exec() == QMessageBox.Yes

    def _start_decomp(self, all_items: bool = False) -> None:
        # ★ 背包裡現在沒有也照樣開跑（使用者定的：跑到按「暫停」為止）——
        #   掛機掉出來的新小背包會被下一拍撿到。
        _pid, sc, _st = self._cur()
        if sc is None:
            self.status.setText("請先選一個分身")
            return
        self._decomp_all = bool(all_items)
        what = "件可分解的東西" if self._decomp_all else "顆"
        found = energy.decomposable(sc, self._decomp_all)
        n = len(found)
        # ⚠⚠ 「全部」開跑前先把**即將被拆掉的東西逐項列出來讓他確認**。
        #   理由不是理論上的：實測（2026-08-08，五台分身）嵐狐的一般背包
        #   第 23 格就躺著一件「水之補師」（點裝，分解值只有 1）——
        #   「我的裝扮都收在紙娃娃隨身包」這個前提**實際上不成立**。
        #   拆掉不能還原，所以這裡多問一句；只拆小背包的那顆鈕不問（名單
        #   寫死兩個編號，沒有誤拆的空間）。
        if self._decomp_all and not self._confirm_all(found):
            return
        self._decomp_sent = []
        self._decomp_rounds = {}
        self._decomp_n = 0
        self._decomp_gain = 0
        self.decomp_btn.setEnabled(False)
        self.decomp_all_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        head = "⚠ 分解全部：" if self._decomp_all else "　"
        self.decomp_lbl.setText(
            f"{head}背包裡有 {n} {what}，開拆" if n else
            f"{head}背包裡目前沒有可拆的，開始盯著")
        self._decomp_timer.start(DECOMP_MS)
        self._decomp_tick()                    # 立刻拆第一輪，不空等一拍

    def _stop_decomp(self, why: str) -> None:
        self._decomp_timer.stop()
        self.decomp_btn.setEnabled(True)
        self.decomp_all_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.decomp_lbl.setText(
            f"　已停止：{why}"
            f"（共拆 {self._decomp_n} 件、＋{self._decomp_gain} 晶能）")

    def _decomp_tick(self) -> None:
        """自動分解的一拍：先對帳上一拍送的，再把背包裡的**一次全送**。

        ★ **拆完不停**：背包空了就繼續每 3 秒看一次，等新的小背包掉出來
          （使用者要的是「跑到我按暫停為止」）。唯一的出口是「暫停」鈕 ——
          換分身、關分頁另外算。
        ★ 拆成功的唯一訊號＝**那顆的序號從背包消失**（背包對帳當真相，
          不信送出成功）。還沒消失的下一拍跟著重送，不設上限
          （使用者定的規矩，見 transient-failure-auto-retry）。
        ★ 一拍最多送 energy.MAX_PER_TICK 顆（護著跳板的鎖），超過的
          下一拍接著拆 —— 幾十顆以內就是「一拍全拆」。
        """
        pid, sc, _st = self._cur()
        if sc is None:
            self._stop_decomp("讀不到分身")
            return
        if bag.head(sc) is None:
            # 換地圖／還沒進場，背包暫時讀不到 —— 不停，等它回來。
            self.decomp_lbl.setText("　讀不到背包（換地圖中？），下一拍再試")
            return
        present = {it.serial for it in bag.items(sc)}
        # 送過的：序號不見了＝伺服器收走＝拆成功，晶能入帳；還在的留著追蹤
        # ★ 分解值在**送出的當下**就記起來（(序號, 種類ID, 分解值)）——
        #   等它從背包消失才回頭查就查不到了（東西已經不在，範本讀不到）。
        still: list[tuple[int, int, int]] = []
        for serial, tid, gain in self._decomp_sent:
            if serial in present:
                still.append((serial, tid, gain))
                self._decomp_rounds[serial] = (
                    self._decomp_rounds.get(serial, 0) + 1)
                continue
            self._decomp_rounds.pop(serial, None)
            self._decomp_n += 1
            self._decomp_gain += gain
            got = self._read()
            self._log("分解", itemname.label(tid), f"晶能 +{gain}",
                      got.energy if got else "")
        # ⚠ 列清單跟送包用**同一條規則**（energy.decomposable ↔ decompose_batch
        #   都走 _slot_ok）：格號範圍、可拆條件、時限一次到位。
        matches = energy.decomposable(sc, self._decomp_all)
        # 送出去撐很多拍還在的：遊戲收了包卻沒動作（伺服器擋掉？）。
        # 只提醒，不改重試（規矩見 memory 的 transient-failure-auto-retry）。
        stuck = sum(1 for s, _, _ in still
                    if self._decomp_rounds.get(s, 0) >= 5)
        stuck_note = (f"　⚠ 有 {stuck} 件送了很多次還在背包裡" if stuck else "")
        if not matches:
            # ★ 拆完**不收工**（使用者要的是「跑到我按暫停為止」）——
            #   繼續每 3 秒看一次，掛機掉出來的新的下一拍就會被拆掉。
            self._decomp_sent = still
            note = stuck_note
            if not still:
                # 一件都沒中時要講得出原因（例如全被時限擋掉），
                # 不准安靜地不做事。
                why = energy.blocked_note(sc, self._decomp_all)
                note = f"　{why}" if why else ""
            self.decomp_lbl.setText(
                f"　等新的可分解物品中：已拆 {self._decomp_n} 件"
                f"（＋{self._decomp_gain} 晶能）" + note)
            return
        mv = self._mover(pid)
        if mv is None:
            self._decomp_sent = still
            return                             # status 已寫原因，下一拍再試
        sent_slots, whole = energy.decompose_batch(
            mv, sc, [it.slot for it in matches], self._decomp_all)
        # 這一拍真的送出去的，補進追蹤名單（送過但還在背包的不重複記）
        tracked = {s for s, _, _ in still}
        sent_set = set(sent_slots)
        for it in matches:
            if it.slot in sent_set and it.serial not in tracked:
                still.append((it.serial, it.type_id,
                              energy.gain_of(it, self._decomp_all)))
        self._decomp_sent = still
        if not sent_slots:
            self.decomp_lbl.setText("　⚠ 一包都沒排進去（指令槽忙碌），"
                                    "下一拍再試")
            return
        note = stuck_note
        if len(sent_slots) < len(matches):
            note += (f"　（這一拍先拆 {len(sent_slots)} 件，剩的下一拍接著）"
                     if whole else "　⚠ 指令槽忙碌，只送出一部分，下一拍補")
        head = "⚠ 分解全部中" if self._decomp_all else "自動分解中"
        self.decomp_lbl.setText(
            f"　{head}：這一拍送 {len(sent_slots)} 件、"
            f"已入帳 {self._decomp_n} 件（＋{self._decomp_gain} 晶能）" + note)

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
        self._decomp_timer.stop()
        # ⚠ 更新用的計時器也要停：下面會把 scanner 全部關掉，計時器還在跑的話
        #   會拿已經關閉的控制碼去讀記憶體。
        self._timer.stop()
        # ★ 用 release() 不要直接 stop()：跳板是同一個 PID 共用的，
        #   掛機分頁可能還在用（見 move.acquire）。
        for pid in list(self._movers):
            try:
                move.release(pid, self)
            except Exception:                       # noqa: BLE001
                pass
        self._movers.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
