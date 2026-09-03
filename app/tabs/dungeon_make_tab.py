"""副本腳本製作：把一趟副本要做的事，在介面上一步一步「試」出來存成 JSON。

## 為什麼不是錄製（使用者 2026-09-01 定案）

> 「不要錄製問題太多，你不知道我在那點位待那麼久要幹嘛很迷。」

錄影只能**事後猜**使用者做了什麼（點了哪個物件、選了第幾項），猜錯還不會報錯。
這裡反過來：**工具自己送、使用者在旁邊挑** —— 工具送出去的東西它自己知道，
不必猜任何事。

## 對話那一步怎麼做出來

1. 「重新掃描」列出附近**可互動的物件**（`scenery.nearby`，跟製作檯同一族：
   vtable 對得上、採集種類 0、選定 id 合法 —— 純裝飾點了沒反應的會被排掉）。
2. 選一個按「點點看」→ 走 `produce.click()`（＝**叫遊戲自己那支點**，
   跟滑鼠點一模一樣；不夠近它會用官方尋路自己走過去）。
3. 遊戲把對話框開起來，使用者**看著遊戲畫面**按介面上的「第 N 項」按鈕，
   工具送 `talkaction`（`sell.talk` + `supply.talk_option`）並把 N 記進路徑。
4. 「把這一步存進腳本」→ 存成 `{"do":"interact","at":[x,y],"model":…,"menu":[…]}`。

⚠⚠ **只存位置與外觀，不存選定 id**：那個 id 的高 16 位是伺服器每次載入地圖
  重配的世代碼（`scenery.py` 檔頭，2026-08-12 實機），存了下次進場必定失效。

## 地圖怎麼畫

`terrain.load()` 讀當下這張圖的可走格（實測 420x230 只要 6~8ms），再用
`dungeon.rooms()` 切連通區上色 —— 副本的房間互不相通，一眼就看得出來。
物件、玩家、已存的點位疊在上面。

⚠ 畫法是「先做 1 像素 1 格的小圖再放大」，不是一格一格 fillRect：
  420x230 ＝ 96600 格，逐格畫在 GUI 執行緒上會卡住畫面。
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import (bag, dungeon, entity, locate, lua, mapobj, move, produce,
                      scene, scenery, sell, supply, talkwnd)
from app.tabs.base_tab import BaseTab, fit_spin

# 房間配色（互不相通的連通區各給一色）。第 7 間之後循環用。
ROOM_COLORS = [
    (56, 84, 120), (60, 100, 72), (110, 80, 56), (96, 64, 100),
    (56, 104, 104), (116, 96, 56),
]
WALL_COLOR = (26, 28, 32)

# 掃描附近物件的半徑（格）。太大會把整張圖的物件都列出來，反而難挑。
PROP_RADIUS = 25.0
# 點下去之後等對話框開的上限。⚠ 遊戲會自己走過去才開，所以要放寬一點。
DIALOG_WAIT = 12.0
# ★ 傳點的出口是「盯著看到的」不是算的（使用者 2026-09-02：「人被傳走不會
#   換地圖，有順移就算吧」）：加完傳點那一步就開始每 0.12 秒看一次位置，
#   一跳超過 JUMP_TILES 格就把落點記進那一步。
PORTAL_WATCH_MS = 120
PORTAL_WATCH_SECS = 90.0
# 一次取樣之間跳這麼多格＝順移（走路一拍最多 0.6 格）。
JUMP_TILES = 3.0
# 兩次取樣隔太久就分不出是走的還是傳的 → 不判，只重設基準。
JUMP_MAX_GAP = 0.4


def _fmt(v: float) -> str:
    return f"{v:.0f}"


class MapCanvas(QLabel):
    """地圖畫布。點一下回報**格座標**。"""

    picked = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setText("按「繪製地圖」把目前這張圖畫出來")
        self._scale = 3

    def show_map(self, pix: QPixmap, scale: float) -> None:
        self._scale = max(0.2, float(scale))
        self.setPixmap(pix)
        self.resize(pix.size())

    def mousePressEvent(self, ev) -> None:      # noqa: N802 (Qt 命名)
        if self.pixmap() is None:
            return
        self.picked.emit(ev.position().x() / self._scale,
                         ev.position().y() / self._scale)


class _IconPeek(QLabel):
    """滑鼠移到物件清單某一條時，跳出來顯示那個東西的圖。

    ⚠ 用 `Qt.ToolTip` 視窗旗標而不是 `setToolTip()`：Qt 的提示字串放不進
      本機記憶體裡的圖（要嘛存暫存檔、要嘛自己畫），自己開一個小視窗最單純。
    """

    def __init__(self, parent) -> None:
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            "background:#20242c; color:#d8dde6; border:1px solid #55606f;"
            " padding:6px;")

    def show_for(self, model: int, at) -> None:
        pm = mapobj.pixmap(model)
        name = mapobj.name_of(model)
        if pm is None:
            # 沒有圖就別跳空框 —— 名字清單上已經有了。
            self.hide()
            return
        big = pm.scaled(pm.width() * 2, pm.height() * 2,
                        Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.setPixmap(big)
        self.setToolTip(name)
        self.adjustSize()
        self.move(at.x() + 18, at.y() + 18)
        self.show()


class MapWindow(QDialog):
    """把地圖開在一個**可以放到最大**的獨立視窗裡。

    ★ 為什麼要有它（2026-09-02 使用者回報「我看不到全部，縮放 1 也看不清全貌」）：
      主視窗固定 940 寬，塞得下的地圖區只有約 550px，而副本地圖是 420x230 格
      —— 縮放 1 全圖擠在 420px 裡看不清細節，縮放 3 又超出畫面。
      開成獨立視窗就能拉大／最大化，還多一顆「符合視窗」自動算縮放。
    """

    def __init__(self, tab) -> None:
        super().__init__(tab)
        self.setWindowTitle("副本地圖")
        self.setModal(False)                     # ⚠ 非強制回應：開著也能操作主視窗
        self.resize(1100, 780)
        self._tab = tab
        v = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("縮放"))
        self.zoom = QSpinBox()
        self.zoom.setRange(1, 12)
        self.zoom.setValue(3)
        self.zoom.valueChanged.connect(lambda _v: self.redraw())
        bar.addWidget(self.zoom)
        b = QPushButton("符合視窗")
        b.setToolTip("自動算一個剛好把整張圖塞進視窗的縮放。")
        b.clicked.connect(self._fit)
        bar.addWidget(b)
        # ★ 機關解開會把牆變成路（使用者 2026-09-02）——底圖是快取的，
        #   開了門要按這顆才會重新讀地形、重新切區。
        b = QPushButton("重讀地形")
        b.setToolTip("重新讀一次記憶體裡的地形圖並重新切區。\n"
                     "解開機關把門打開之後按這顆，牆才會變成路。")
        b.clicked.connect(tab._draw)
        bar.addWidget(b)
        self.live_cb = QCheckBox("即時更新")
        self.live_cb.setChecked(True)
        self.live_cb.setToolTip("每半秒重畫一次，角色走到哪裡地圖上就跟著動。")
        bar.addWidget(self.live_cb)
        bar.addStretch(1)
        self.info = QLabel("　")
        bar.addWidget(self.info)
        v.addLayout(bar)

        self.canvas = MapCanvas()
        self.canvas.picked.connect(tab._on_pick)
        self.area = QScrollArea()
        self.area.setWidget(self.canvas)
        self.area.setWidgetResizable(False)
        v.addWidget(self.area, 1)

        # 加點位的按鈕擺在這裡（使用者 2026-09-02：地圖跟加點位都放這個視窗）
        ph = QHBoxLayout()
        self.pick_lbl = QLabel("點一下地圖選位置")
        ph.addWidget(self.pick_lbl)
        ph.addStretch(1)
        self.add_pick = QPushButton("加入點到的位置")
        self.add_pick.setToolTip("把地圖上點到的那一格，加成一個「走到」步驟。")
        self.add_pick.setEnabled(False)
        self.add_pick.clicked.connect(tab._add_picked)
        ph.addWidget(self.add_pick)
        b = QPushButton("加入我現在站的位置")
        b.setToolTip("把角色**現在**站的那一格，加成一個「走到」步驟。")
        b.clicked.connect(tab._add_here)
        ph.addWidget(b)
        # ★ 傳點要單獨一種步驟（使用者 2026-09-02 問：「點位放在傳點上會不會
        #   永遠到不了？」——會）。「走到」的完成條件是站到那一格，但踩上去
        #   人就被搬走了，那一格永遠不會到達。
        #   ⚠ 完成訊號是**順移**不是換地圖（使用者當場更正：吞噬之間的傳點
        #     是同一張圖裡搬位置，場景編號完全不變）。
        self.add_portal = QPushButton("加入「走進傳點」")
        self.add_portal.setToolTip(
            "把點到的那一格當成**傳點**：走過去，看到人被**順移**走才算完成。\n"
            "⚠ 用一般的「走到」放在傳點上會永遠到不了（人被搬走，那一格不會到達）。\n"
            "按下去之後你自己走進傳點，工具盯著看，出口在哪會自動記進這一步。")
        self.add_portal.setEnabled(False)
        self.add_portal.clicked.connect(tab._add_portal)
        ph.addWidget(self.add_portal)
        v.addLayout(ph)

        self.status = QLabel("　")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        # ⚠ 即時更新只重畫**疊圖**（角色、點位、物件），底圖是快取的 ——
        #   底圖 420x230 ＝ 96600 次 setPixel，每半秒重算一次會吃掉一顆核心。
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._live)
        self._timer.start(500)

    def _live(self) -> None:
        if self.isVisible() and self.live_cb.isChecked():
            self.redraw()

    def _fit(self) -> None:
        pix = self._tab._render(1)
        if pix.isNull():
            return
        vp = self.area.viewport().size()
        s = min(vp.width() / max(pix.width(), 1),
                vp.height() / max(pix.height(), 1))
        self.zoom.blockSignals(True)
        self.zoom.setValue(max(1, int(s)))
        self.zoom.blockSignals(False)
        self.redraw(fit_to=s if s < 1 else None)

    def redraw(self, fit_to: float | None = None) -> None:
        s = fit_to if fit_to else self.zoom.value()
        pix = self._tab._render(s)
        if pix.isNull():
            return
        # ⚠ 重畫會換掉 pixmap，捲軸位置預設會被拉回原點 —— 使用者正在看某個
        #   角落時每半秒被彈回左上角是不能用的。所以自己把位置存回去。
        hx = self.area.horizontalScrollBar().value()
        hy = self.area.verticalScrollBar().value()
        self.canvas.show_map(pix, s)
        self.area.horizontalScrollBar().setValue(hx)
        self.area.verticalScrollBar().setValue(hy)
        self.info.setText(f"{pix.width()} x {pix.height()} 像素")


class DungeonMakeTab(BaseTab):
    TAB_TITLE = "副本腳本製作"
    ORDER = 6                       # 排在自動掛機（5）後面

    # ------------------------------------------------------------------
    def build_ui(self) -> None:
        self._scanners: dict[int, MemoryScanner] = {}
        self._movers: dict[int, move.Mover] = {}
        self._script = dungeon.Script()
        self._path = None                 # 目前這份腳本的檔案路徑
        self._grid = None                 # terrain.Grid（繪製時抓的那張）
        self._rooms: dict = {}
        self._sizes: list[int] = []
        self._grid_key = None             # 畫出來那張圖的 map_key
        self._poked_at = None             # 點下去那一刻我站在哪（存成 stand）
        self._props: list = []            # 上次掃到的可互動物件（附近，挑著點）
        self._props_all: list = []        # 繪製地圖時掃到的全部物件（畫紅點）
        self._pick = None                 # 在地圖上點到的格子
        self._menu: list[int] = []        # 正在試的對話選項路徑
        self._poked = None                # 正在試的那個物件（scenery.Prop）
        self._big = None                  # 地圖的獨立視窗
        self._base = None                 # 底圖快取（QImage，只跟地形／房間有關）
        self._poke_base = None            # 點下去之前的對話框代號
        self._poke_until = 0.0

        root = QVBoxLayout(self)

        # ── 分身 ───────────────────────────────────────────
        bar = QHBoxLayout()
        bar.addWidget(QLabel("分身"))
        self.who = QComboBox()
        self.who.setFixedWidth(240)
        self.who.currentIndexChanged.connect(self._on_who_changed)
        bar.addWidget(self.who)
        btn = QPushButton("重新整理")
        btn.setToolTip("重新列出目前開著的遊戲分身。")
        btn.clicked.connect(lambda: self.reload_instances(force_names=True))
        bar.addWidget(btn)
        bar.addStretch(1)
        self.here_lbl = QLabel("－")
        bar.addWidget(self.here_lbl)
        root.addLayout(bar)

        # ── 腳本檔 ─────────────────────────────────────────
        fbar = QHBoxLayout()
        fbar.addWidget(QLabel("腳本"))
        self.files = QComboBox()
        self.files.setFixedWidth(240)
        self.files.currentIndexChanged.connect(self._on_file_changed)
        fbar.addWidget(self.files)
        for text, tip, fn in (
            ("新增", "開一份空白腳本。", self._new_script),
            ("儲存", "存回目前這個檔案。", self._save),
            ("另存新檔", "換一個名字存一份。", self._save_as),
            ("重新蓋章",
             "把這份腳本記的地圖，改成這台分身**現在**所在的那一張。\n"
             "腳本是在別張圖上按「新增」開的（例如先在門口開好才走進副本）\n"
             "就會蓋錯章，自動刷副本會說「不是腳本寫的那張圖」。",
             self._restamp),
            ("開啟資料夾", f"腳本都放在 {dungeon.save_folder()}",
             self._open_folder),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            fbar.addWidget(b)
        fbar.addStretch(1)
        root.addLayout(fbar)

        # ★ 這份腳本是「哪張圖」的 —— 一定要看得見（2026-09-02）：看不見就
        #   會發生「明明站在對的圖上，開跑卻說不是同一張圖」而查不出原因。
        self.stamp_lbl = QLabel("　")
        self.stamp_lbl.setWordWrap(True)
        root.addWidget(self.stamp_lbl)

        # ── 地圖（一顆按鈕 → 獨立視窗，使用者 2026-09-02 指定）───────
        mid = QHBoxLayout()
        draw = QPushButton("繪製地圖")
        draw.setToolTip(
            "把這台分身**目前所在**的地圖畫成一個獨立視窗（可以拉大／最大化）。\n"
            "互不相通的房間用不同顏色，可互動的物件標成紅點，角色是黃色十字。\n"
            "加點位的按鈕也在那個視窗裡；地圖會即時更新。")
        draw.clicked.connect(self._draw)
        mid.addWidget(draw)
        mid.addStretch(1)
        root.addLayout(mid)

        mid = QHBoxLayout()
        stepbox = QGroupBox("步驟（由上往下執行）")
        sv = QVBoxLayout(stepbox)
        self.steps = QListWidget()
        self.steps.setSelectionMode(QAbstractItemView.SingleSelection)
        sv.addWidget(self.steps, 1)
        sh = QHBoxLayout()
        for text, tip, fn in (
            ("↑", "往前挪一步。", lambda: self._move(-1)),
            ("↓", "往後挪一步。", lambda: self._move(1)),
            ("刪除", "刪掉選到的那一步。", self._del),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            sh.addWidget(b)
        sh.addStretch(1)
        sv.addLayout(sh)
        sh2 = QHBoxLayout()
        # ⛔ 這裡**不再有**「加入『清光周圍的怪』」（使用者 2026-09-02：
        #   「為何會有清光周圍怪物按鈕？這事本來在執行跑點位或對話之前就
        #     該這樣，不開出現這個指令」）—— 他是對的：執行端的規矩就是
        #   「走得到的怪還有一隻就先打，一隻都不剩才做下一步」，清怪已經是
        #   **每一步的前提**，再擺一個指令只會讓人以為不加就不會清。
        #   ⚠ 舊腳本裡若有 `clear` 照樣跑得動（只是多一道 3 秒確認）。
        # ★ 使用者 2026-09-02：「需要按一個休息幾秒，不然太快說話可能會出現
        #   無異議對話」——秒數就放旁邊，按一下就加，不必再跳輸入框。
        self.wait_secs = QDoubleSpinBox()
        self.wait_secs.setRange(0.5, 600.0)
        self.wait_secs.setSingleStep(0.5)
        self.wait_secs.setDecimals(1)
        self.wait_secs.setValue(3.0)
        self.wait_secs.setSuffix(" 秒")
        b = QPushButton("加入「休息」")
        b.setToolTip("在這裡停幾秒再做下一步。\n"
                     "例如上一個機關剛講完話，馬上跟下一個講會被拒絕。\n"
                     "⚠ 周圍還有打得到的怪就不會開始數，清光才從頭數。")
        b.clicked.connect(
            lambda: self._add({"do": dungeon.WAIT,
                               "secs": round(self.wait_secs.value(), 1)}))
        sh2.addWidget(b)
        sh2.addWidget(self.wait_secs)
        sv.addLayout(sh2)
        mid.addWidget(stepbox, 2)
        root.addLayout(mid, 1)

        # ── 對話 ───────────────────────────────────────────
        talkbox = QGroupBox("對話（機關／NPC）")
        tv = QVBoxLayout(talkbox)
        th = QHBoxLayout()
        b = QPushButton("重新掃描附近")
        b.setToolTip(f"列出角色 {PROP_RADIUS:.0f} 格內**點得到**的物件。\n"
                     "純裝飾（點了沒反應的）不會列出來。")
        b.clicked.connect(self._scan_props)
        th.addWidget(b)
        self.poke_btn = QPushButton("點點看")
        self.poke_btn.setToolTip(
            "真的去點選到的那個物件，跟你自己用滑鼠點是同一包。\n"
            "點完看遊戲畫面：對話框開起來的話，再按下面的「第 N 項」。")
        self.poke_btn.clicked.connect(self._poke)
        th.addWidget(self.poke_btn)
        b = QPushButton("離開對話")
        b.setToolTip("把對話框關掉**並**送出離開互動。\n"
                     "（不送離開的話伺服器會覺得你還在跟它講話，人會走不動。）")
        b.clicked.connect(self._leave)
        th.addWidget(b)
        # ★ 使用者 2026-09-02：「我們可以讀到傳送點物件，那能不能多一個按鈕
        #   是走去傳送點…走入傳送點一樣記錄在 json 是流程一部分」
        b = QPushButton("這個是傳送點")
        b.setToolTip(
            "把清單裡選到的那個物件記成「走進傳點」的一步（存進腳本）。\n"
            "跑的時候：走過去 → 等人被順移／換圖才算完成；\n"
            "站上去沒反應就每 5 秒對它送一次互動，撐 3 分鐘沒過就停下來通知。\n"
            "按完之後你自己走進去，出口在哪會自動記進這一步。")
        b.clicked.connect(self._add_portal_prop)
        th.addWidget(b)
        # ★ 使用者 2026-09-02：「新增一個『加入進入副本傳送點』，一樣寫在
        #   同一個 json，他會紀錄那個入口傳送點目前在哪個地圖哪個地方」
        b = QPushButton("這是進副本的入口")
        b.setToolTip(
            "把選到的物件記成**進副本的入口傳送點**（整份腳本一個，不是步驟）。\n"
            "會連「入口在哪一張圖」一起記起來。\n"
            "自動刷副本開跑時：人在副本裡就直接跑腳本；\n"
            "人在入口那張圖就先走去撞它，撞不進去每 5 秒補送一次。\n"
            "★ 門口要「點下去→選第 1 項」才進得去的那種：先按「點點看」、\n"
            "　 再按第 N 項，然後按這顆 —— 站位與選項路徑會一起記進去。")
        b.clicked.connect(self._set_entrance)
        th.addWidget(b)
        # ★ 場景標記點（TAG01/TAG02…）畫面上根本看不見，但照樣有互動 id，
        #   一站就掃到 45 個把真正的機關淹掉 —— 預設收起來。
        self.hide_tag = QCheckBox("藏起看不見的標記點")
        self.hide_tag.setChecked(True)
        self.hide_tag.setToolTip(
            "資源包標了「看不見」的物件（TAG01 這種伺服器用的位置標記）不列出來，\n"
            "地圖上也不畫它們的紅點。\n"
            "找不到你要的機關時可以取消勾選，把全部都列出來（標記點會畫成暗灰小點）。")
        # 清單跟地圖上的點用同一個勾選框，兩邊一起跟著變。
        self.hide_tag.toggled.connect(lambda _v: (self._scan_props(),
                                                  self._redraw_overlay()))
        th.addWidget(self.hide_tag)
        th.addStretch(1)
        tv.addLayout(th)

        self.props = QListWidget()
        self.props.setMaximumHeight(110)
        # ★ 滑鼠移到某一條就跳一個小框顯示那個東西長什麼樣（使用者 2026-09-02
        #   要求）。⚠ 要開 setMouseTracking 才會有 itemEntered。
        self.props.setMouseTracking(True)
        self.props.itemEntered.connect(self._preview)
        self.props.viewport().installEventFilter(self)
        self._peek = _IconPeek(self)
        tv.addWidget(self.props)

        oh = QHBoxLayout()
        oh.addWidget(QLabel("對話"))
        # ★ 「無異議對話」那一頁（使用者 2026-09-02：「無異議對話 → 選項1 →
        #   無異議對話 → 結束」）——它送的是 messageclose(0x128)，跟送選項的
        #   talkaction(0x0B) 完全不同，所以要有自己的按鈕、也自己記一格。
        b = QPushButton("過場")
        b.setFixedWidth(46)
        b.setToolTip(
            "這一頁**沒有選項**（只有文字＋確定）→ 按這顆過掉它，往下看下一頁。\n"
            "⚠ 不會記進腳本 —— 跑的時候「沒有選項就自己按到出現選項或結束」\n"
            "　 是自動的，腳本只要記你**選了第幾項**。")
        b.clicked.connect(self._pass_page)
        oh.addWidget(b)
        for n in range(1, dungeon.MENU_MAX + 1):
            b = QPushButton(str(n))
            b.setFixedWidth(34)
            b.setToolTip(f"送出對話選單的第 {n} 項，並記進這一步的路徑。")
            b.clicked.connect(lambda _=False, k=n: self._send_option(k))
            oh.addWidget(b)
        oh.addStretch(1)
        tv.addLayout(oh)

        ph2 = QHBoxLayout()
        self.menu_lbl = QLabel("已選路徑：（先按「點點看」試一個物件）")
        ph2.addWidget(self.menu_lbl)
        ph2.addStretch(1)
        # ⚠ 「選項間隔」欄位已拿掉（使用者 2026-09-03：不給輸入、固定用 dungeon_tab.MENU_GAP）。
        b = QPushButton("清掉路徑")
        b.setToolTip("選錯了重來（不會影響已存的步驟）。")
        b.clicked.connect(self._clear_menu)
        ph2.addWidget(b)
        self.save_talk = QPushButton("把這一步存進腳本")
        self.save_talk.setToolTip(
            "存成「對話」步驟：位置＋外觀＋選項路徑。\n"
            "★ 一個選項都沒按就存 ＝ **純對話**（點一下、等一下、離開），\n"
            "　 整段沒有選項的機關／NPC 就是這樣記。")
        self.save_talk.setEnabled(False)
        self.save_talk.clicked.connect(self._save_talk)
        ph2.addWidget(self.save_talk)
        tv.addLayout(ph2)
        root.addWidget(talkbox)

        self.status = QLabel("　")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._poke_timer = QTimer(self)
        self._poke_timer.timeout.connect(self._poke_check)
        # 傳點監看（加完「走進傳點」才跑，看到順移就停）
        self._pw = None
        self._pw_timer = QTimer(self)
        self._pw_timer.timeout.connect(self._portal_watch)
        for sp in self.findChildren(QSpinBox):
            fit_spin(sp)
        self._reload_files()

    def _say_map(self, text: str) -> None:
        """狀態訊息：地圖視窗開著就寫在它上面，不然寫回分頁。"""
        if self._big is not None and self._big.isVisible():
            self._big.status.setText(text)
        self.status.setText(text)

    # ------------------------------------------------------------------
    # 分身
    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if not self._scanners:
            self.reload_instances()

    def reload_instances(self, force_names: bool = False) -> None:
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
                f"{preload.name_of(w.pid, sc, acc, force=force_names)}"
                f"（{acc}）", w.pid)
        self.who.blockSignals(False)
        if not self._scanners:
            self.status.setText("找不到分身 —— 遊戲開著嗎？")
        self._refresh_here()

    def _on_who_changed(self) -> None:
        # 換分身＝換一台的記憶體，之前掃到的物件與地圖全部作廢。
        # ⚠ 傳點監看也要停：換了一台，「誰在走」就不是同一個人了。
        if self._pw is not None:
            self._pw = None
            self._pw_timer.stop()
            self._say_map("換了分身 → 傳點監看停掉了（要記出口請重加一次）")
        self._props, self._poked, self._menu = [], None, []
        self.props.clear()
        self._clear_menu()
        self._refresh_here()

    def _cur(self):
        pid = self.who.currentData()
        if pid is None:
            return None, None
        return int(pid), self._scanners.get(int(pid))

    def _mover(self, pid: int):
        """拿這台分身的跳板。

        ⚠⚠ 一定要走 `move.acquire()` —— 一個遊戲行程只能有一份跳板，
          自己 new 一個會把掛機分頁那份拆掉（[[mover-per-pid-conflict]]）。
        """
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

    def _me(self, sc) -> tuple[float, float] | None:
        """角色現在站的格子（浮點）。讀不到回 None —— 不要拿 (0,0) 當預設。"""
        ent = bag.player_entity(sc)
        return entity.read_pos(sc, ent + 8) if ent else None

    def _refresh_here(self) -> None:
        _pid, sc = self._cur()
        if sc is None:
            self.here_lbl.setText("－")
            return
        try:
            sid = scene.current_id(sc)
            me = self._me(sc)
        except Exception:                                # noqa: BLE001
            self.here_lbl.setText("－")
            return
        pos = f"　站在 ({_fmt(me[0])}, {_fmt(me[1])})" if me else "　站位讀不到"
        self.here_lbl.setText(f"{scene.scene_name(sid)}（{sid}）{pos}")
        self._refresh_stamp()

    # ------------------------------------------------------------------
    # 腳本檔
    # ------------------------------------------------------------------
    def _reload_files(self, keep: str = "") -> None:
        self.files.blockSignals(True)
        self.files.clear()
        self.files.addItem("（未命名）", None)
        for p in dungeon.list_scripts():
            self.files.addItem(p.stem, str(p))
        if keep:
            i = self.files.findData(keep)
            if i >= 0:
                self.files.setCurrentIndex(i)
        self.files.blockSignals(False)

    def _on_file_changed(self) -> None:
        data = self.files.currentData()
        if not data:
            return
        from pathlib import Path
        sc, why = dungeon.load(Path(data))
        if sc is None:
            # ⚠ 壞掉的腳本不半套載入：少一步就是走到一半沒人接。
            QMessageBox.warning(self, "腳本讀不進來", why)
            self._reload_files()
            return
        self._script, self._path = sc, Path(data)
        self._refresh_steps()
        self.status.setText(
            f"已載入「{sc.name}」共 {len(sc.steps)} 步"
            + (f"（場景 {sc.scene}）" if sc.scene is not None else ""))
        self._redraw_overlay()

    def _new_script(self) -> None:
        name, ok = QInputDialog.getText(self, "新增腳本", "腳本名稱：")
        if not ok or not name.strip():
            return
        self._script = dungeon.Script(name=name.strip())
        self._path = None
        # ⛔ **這裡不蓋地圖章**（2026-09-02 真的踩到）：使用者在「地底廣場」
        #   （場景 71、300x180）按新增，然後才走進「吞噬之間」（76、420x230）
        #   記步驟 —— 章停在 71，自動刷副本開跑前一比就說「不是同一張圖」，
        #   而使用者明明站在對的圖上。改成**第一步存進來的時候**才蓋章
        #   （見 `_add`），章一定跟步驟是同一張圖。
        self._refresh_steps()
        self.files.blockSignals(True)
        self.files.setCurrentIndex(0)
        self.files.blockSignals(False)
        self.status.setText(f"新腳本「{name.strip()}」—— 記得按儲存")

    def _here_key(self) -> tuple[int | None, object]:
        """(現在這張圖的 map_key, terrain.Grid)；讀不到回 (None, None)。"""
        _pid, sc = self._cur()
        if sc is None:
            return None, None
        try:
            from app.game import terrain
            key = scene.map_key(scene.current_id(sc))
            grid, _why = terrain.load(sc)
            return key, grid
        except Exception:                                # noqa: BLE001
            return None, None

    def _stamp_map(self, quiet: bool = False) -> bool:
        """把目前這張圖的場景編號與指紋蓋進腳本（開跑前用來比對）。"""
        key, grid = self._here_key()
        if key is None:
            if not quiet:
                self.status.setText("⚠ 讀不到目前場景，沒有蓋章")
            return False
        self._script.scene = key
        if grid is not None:
            self._script.map = dungeon.fingerprint(grid)
        self._refresh_stamp()
        return True

    def _restamp(self) -> None:
        """「重新蓋章」：把腳本的地圖改成現在這一張（舊腳本蓋錯圖時用）。"""
        key, _grid = self._here_key()
        if key is None:
            QMessageBox.warning(self, "重新蓋章", "讀不到目前場景 —— 先選一台分身。")
            return
        old = self._script.scene
        if old is not None and old != key and self._script.steps:
            if QMessageBox.question(
                    self, "重新蓋章",
                    f"這份腳本原本記的是「{scene.scene_name(old)}」（{old}），"
                    f"現在站的是「{scene.scene_name(key)}」（{key}）。\n"
                    f"裡面已經有 {len(self._script.steps)} 步 —— "
                    f"那些座標是在舊那張圖上點的，換成新圖等於全部作廢。\n"
                    "還是要改嗎？") != QMessageBox.Yes:
                return
        if self._stamp_map():
            self.status.setText(
                f"已重新蓋章：{scene.scene_name(key)}（{key}）—— 記得按儲存")

    def _refresh_stamp(self) -> None:
        """把「這份腳本是哪張圖」寫在介面上 —— 看得到才不會又蓋錯圖。"""
        s = self._script
        if s.scene is None:
            self.stamp_lbl.setText("這份腳本：還沒蓋地圖章（存第一步時自動蓋）")
            return
        fp = s.map or {}
        size = (f"　{fp.get('w')}x{fp.get('h')}　可走 {fp.get('walkable')} 格"
                if fp else "")
        # 走過傳點的腳本會跨好幾張圖 —— 要比的是「記到現在應該在哪一張」。
        want = dungeon.map_at(s)
        if want is None:
            want = dungeon.map_at(s, len(s.steps) - 1)
        here, _g = self._here_key()
        bad = here is not None and want is not None and here != want
        tail = ""
        if want is not None and want != s.scene:
            tail = f"　→ 目前記到「{scene.scene_name(want)}」（{want}）"
        self.stamp_lbl.setText(
            ("⚠ " if bad else "")
            + f"這份腳本：{scene.scene_name(s.scene)}（{s.scene}）{size}{tail}"
            + (f"　—— 但這台分身現在在「{scene.scene_name(here)}」" if bad else "")
            + f"\n{dungeon.describe_entrance(s.entrance)}")

    def _save(self) -> None:
        if self._path is None:
            self._save_as()
            return
        try:
            self._script.save(self._path)
        except Exception as e:                           # noqa: BLE001
            QMessageBox.warning(self, "存不進去", str(e))
            return
        self.status.setText(f"已存 {self._path}")

    def _save_as(self) -> None:
        # ⛔ 不開「另存新檔」的檔案對話框（使用者 2026-09-02 定案）：路徑是
        #   我們決定的（專案 assets/副本），使用者只要給個名字。
        name, ok = QInputDialog.getText(
            self, "另存新檔", "腳本名稱：", text=self._script.name or "副本")
        name = name.strip()
        if not ok or not name:
            return
        path = dungeon.save_folder() / f"{name}.json"
        if path.exists() and QMessageBox.question(
                self, "另存新檔",
                f"「{name}」已經有了，要蓋掉嗎？") != QMessageBox.Yes:
            return
        self._path = path
        self._script.name = name
        self._save()
        self._reload_files(keep=str(self._path))

    def _open_folder(self) -> None:
        import os
        try:
            os.startfile(str(dungeon.folder()))          # noqa: S606
        except Exception:                                # noqa: BLE001
            self.status.setText(f"腳本資料夾：{dungeon.folder()}")

    # ------------------------------------------------------------------
    # 步驟
    # ------------------------------------------------------------------
    def _refresh_steps(self) -> None:
        keep = self.steps.currentRow()
        self.steps.clear()
        for i, s in enumerate(self._script.steps):
            self.steps.addItem(f"{i + 1:>2}. {dungeon.describe(s)}")
        if 0 <= keep < self.steps.count():
            self.steps.setCurrentRow(keep)
        self._refresh_stamp()
        self._redraw_overlay()

    def _add(self, step: dict) -> None:
        ok, why = dungeon.validate(step)
        if not ok:
            self.status.setText(f"⚠ 這一步有問題：{why}")
            return
        # ★★ 地圖章跟著**步驟**走（2026-09-02 使用者回報「明明是同一張地圖
        #   卻說不是」的根因就在這）：第一步存進來時蓋章，之後每一步都確認
        #   還在同一張圖 —— 走出去了還繼續加，等於把兩張圖的座標混在一份
        #   腳本裡，跑起來一定亂走。
        here, _grid = self._here_key()
        # ★ 插在**選到的那一步後面**，不是永遠塞到最後（使用者 2026-09-03：
        #   「點前面步驟加入新流程，要直接加在他下面」）。沒選或選的是最後
        #   一步 → 跟以前一樣接在最後。下面的「上一步」「該在哪張圖」全部
        #   以插入點為準，不是以最後一步為準。
        row = self.steps.currentRow()
        n = len(self._script.steps)
        at = row + 1 if 0 <= row < n - 1 else n
        if self._script.scene is None:
            if not self._stamp_map(quiet=True):
                self._say_map("⚠ 讀不到目前場景，這一步沒有存")
                return
        else:
            steps = self._script.steps
            prev = dungeon.map_at(self._script, at - 1)
            last = steps[at - 1] if at > 0 else None
            # ★ 剛走過傳點：把「傳到哪張圖」記進那一步 —— 是我們**看到**的
            #   （人現在就站在那），不是猜的。這也是腳本唯一准許換圖的時機。
            if (last is not None and last.get("do") == dungeon.PORTAL
                    and last.get("scene") is None
                    and here is not None and here != prev):
                # ⚠ 這是**換圖型**傳點的補網（換圖時座標不見得會跳，順移監看
                #   可能看不到）。位置一樣是當場讀的，不是算的。
                last["scene"] = here
                me = self._me(self._cur()[1])
                if me is not None and not last.get("land"):
                    last["land"] = [round(me[0], 1), round(me[1], 1)]
                self._say_map(f"第 {at} 步的傳點會到"
                              f"「{scene.scene_name(here)}」（{here}）—— 已記住")
            want = dungeon.map_at(self._script, at)
            if want is None:
                want = prev              # 傳點還沒走過 → 應該還在傳點前那張
            if here is not None and want is not None and here != want:
                self._say_map(
                    f"⛔ 這一步沒有存：腳本這時候應該在"
                    f"「{scene.scene_name(want)}」（{want}），你現在在"
                    f"「{scene.scene_name(here)}」（{here}）。\n"
                    "　換圖只能靠「加入『走進傳點』」那一步 ——"
                    "整份重來請按「重新蓋章」。")
                return
        self._script.add(step, at)
        self._refresh_steps()
        self.steps.setCurrentRow(at)
        self._say_map(f"已加入（第 {at + 1} 步）：{dungeon.describe(step)}")
        self._warn_room(at)

    def _warn_room(self, idx: int | None = None) -> None:
        """新加的這一步跟上一步在不在同一區？不同區又沒傳點就**提醒**。

        ★ 副本是好幾塊互不相通的地方拼起來的（使用者 2026-09-02），跨區的
          點位跑起來一定「走不到」——加的當下就講，不要等跑到一半才發現。
        ⚠ 只是**提醒不是擋下**：機關解開會把牆變成路（使用者同日提醒
          「解謎之後會打開又會變成聯通」），現在不通不代表跑的時候不通。
        """
        steps = self._script.steps
        if idx is None or not 0 <= idx < len(steps):
            idx = len(steps) - 1          # 沒給就看最後一步（舊行為）
        if not self._rooms or idx < 1:
            return
        here = steps[idx].get("to") or steps[idx].get("at")
        if not here:
            return
        prev = None
        for st in reversed(steps[:idx]):
            if st.get("do") == dungeon.PORTAL:
                return                     # 中間有傳點：本來就會換區，不比
            prev = st.get("to") or st.get("at")
            if prev:
                break
        if not prev:
            return
        a = self._rooms.get((int(prev[0]), int(prev[1])))
        b = self._rooms.get((int(here[0]), int(here[1])))
        if a is None or b is None or a == b:
            return                         # 有一邊在牆上／碎片區 → 判不出來
        self._say_map(
            f"⚠ 這一步在第 {b} 區，上一步在第 {a} 區 —— 兩區互不相通，"
            "中間沒有「走進傳點」的話跑起來會走不到。"
            "（如果是機關解開才會通的門，那就沒關係，跑的時候會重讀地形。）")

    def _add_here(self) -> None:
        _pid, sc = self._cur()
        if sc is None:
            self._say_map("先選一台分身")
            return
        me = self._me(sc)
        if me is None:
            # 讀不到就什麼都不做 —— 存一個 (0,0) 進去比不存危險得多。
            self._say_map("⚠ 讀不到角色位置，這一步沒有存")
            return
        self._add({"do": dungeon.WALK, "to": [round(me[0]), round(me[1])]})

    def _add_picked(self) -> None:
        if self._pick is None:
            return
        self._add({"do": dungeon.WALK,
                   "to": [int(self._pick[0]), int(self._pick[1])]})

    def _add_portal(self) -> None:
        """把點到的那一格加成「走進傳點」，然後**盯著看**它會把人送到哪。

        出口（`land`）不在這裡填 —— 加完之後使用者自己走進傳點，工具每
        0.12 秒看一次位置，看到順移就把**當場看到的**落點記進這一步。
        ⛔ 不准用算的、也不准猜（傳點對面在哪只有走一次才知道）。
        """
        if self._pick is None:
            return
        n = len(self._script.steps)
        self._add({"do": dungeon.PORTAL,
                   "to": [int(self._pick[0]), int(self._pick[1])]})
        if len(self._script.steps) <= n:
            return                       # 被 _add 擋下來了（換圖了之類）
        self._pw = (n, time.monotonic() + PORTAL_WATCH_SECS, None, 0.0)
        self._pw_timer.start(PORTAL_WATCH_MS)
        self._say_map("走進那個傳點吧 —— 我盯著看它把你送到哪，"
                      "看到就自動記進這一步。")

    def _add_portal_prop(self) -> None:
        """把清單裡選到的物件記成「走進傳點」（使用者 2026-09-02 要的按鈕）。

        跟 `_add_portal`（用地圖上點到的格）差別只在**位置從哪來**：
        這裡連**外觀編號**一起記，跑的時候站上去沒被搬走才有東西可以補送。
        """
        i = self.props.currentRow()
        if not 0 <= i < len(self._props):
            self.status.setText("先在清單裡選一個物件（那個傳送點）")
            return
        pr = self._props[i]
        n = len(self._script.steps)
        self._add({"do": dungeon.PORTAL,
                   "to": [round(pr.x, 1), round(pr.y, 1)],
                   "model": pr.model})
        if len(self._script.steps) <= n:
            return                       # 被 _add 擋下來了（換圖了之類）
        self._pw = (n, time.monotonic() + PORTAL_WATCH_SECS, None, 0.0)
        self._pw_timer.start(PORTAL_WATCH_MS)
        self._say_map(f"記成傳送點：{mapobj.label(pr.model)} —— "
                      "走進去吧，我盯著看它把你送到哪。")

    def _entrance_here(self) -> bool:
        """現在正在跟**已經記好的那個入口**互動嗎？

        條件：入口記過了、這台分身就在入口那張圖、而且剛剛點／撞的那個
        物件就是它（外觀一樣、位置對得上）。
        """
        ent = self._script.entrance or {}
        if not ent:
            return False
        key, _grid = self._here_key()
        if key is None or key != ent.get("scene"):
            return False
        pr = self._poked
        ex, ey = (ent.get("to") or [None, None])[:2]
        if pr is None:
            # 沒點東西（撞上去的那種）→ 看**人是不是就站在入口旁邊**
            _pid, sc = self._cur()
            me = self._me(sc) if sc is not None else None
            if me is None or ex is None:
                return False
            return math.hypot(me[0] - ex, me[1] - ey) <= 5.0
        if ent.get("model") not in (None, pr.model):
            return False
        if ex is None:
            return True
        return math.hypot(pr.x - ex, pr.y - ey) <= 4.0

    def _set_entrance(self) -> None:
        """把選到的物件記成「進副本的入口傳送點」（整份腳本共用一個）。

        ⚠ 這不是步驟，所以**不受**「同一份腳本只能記同一張圖」那道閘限制：
          入口本來就在副本**外面**那張圖上。記的是當下這台分身站的那張圖。
        """
        # ★ 剛「點點看」過就用那一個（連站位、選了第幾項一起記）——
        #   有些副本門口是「點下去→選第 1 項」才進得去（使用者 2026-09-02）。
        pr = self._poked
        if pr is None:
            i = self.props.currentRow()
            if not 0 <= i < len(self._props):
                self.status.setText("先在清單裡選一個物件（那個入口傳送點）")
                return
            pr = self._props[i]
        key, _grid = self._here_key()
        if key is None:
            self.status.setText("⚠ 讀不到目前場景，入口沒有記")
            return
        if self._script.scene is not None and key == self._script.scene:
            QMessageBox.warning(
                self, "入口傳送點",
                f"你現在就站在腳本那張圖「{scene.scene_name(key)}」裡面。\n"
                "入口是**外面**那張圖上的傳送點 —— 先出去再記一次。")
            return
        ent = {"scene": key,
               "to": [round(pr.x, 1), round(pr.y, 1)],
               "model": pr.model}
        if self._menu:
            # 門口要選第幾項（0＝沒有選項那種頁跑的時候會自動過，不必記）
            ent["menu"] = [n for n in self._menu if n]
        # ⚠ 重記一次＝從頭來過，舊的選項路徑不留（不然會疊起來）
        at = getattr(self, "_poked_at", None)
        if at:
            ent["stand"] = [round(at[0], 1), round(at[1], 1)]
        self._script.entrance = ent
        self._menu = []                  # 之後按的選項才是入口的（從頭記）
        self._refresh_menu()
        self._refresh_stamp()
        self.status.setText(
            f"已記入口：{dungeon.describe_entrance(self._script.entrance)}"
            "　—— 現在去撞它、按「第 N 項」，那些選項會自動記進入口")

    def _portal_watch(self) -> None:
        """盯著「剛加的那個傳點」把人送到哪。看到順移才記，看不到就說看不到。"""
        if self._pw is None:
            self._pw_timer.stop()
            return
        i, deadline, prev, prev_t = self._pw
        steps = self._script.steps
        if i >= len(steps) or steps[i].get("do") != dungeon.PORTAL:
            self._pw = None                      # 那一步被刪／搬走了
            self._pw_timer.stop()
            return
        now = time.monotonic()
        if now > deadline:
            self._pw = None
            self._pw_timer.stop()
            self._say_map(
                f"⚠ 等了 {PORTAL_WATCH_SECS:.0f} 秒沒看到你走進傳點 —— "
                f"第 {i + 1} 步的出口沒記到（清單上會標；再按一次就重新盯）")
            return
        _pid, sc = self._cur()
        me = self._me(sc) if sc is not None else None
        if me is None:
            return                               # 讀不到就跳過這一次取樣
        self._pw = (i, deadline, me, now)
        if prev is None or now - prev_t > JUMP_MAX_GAP:
            return                               # 隔太久 → 只重設基準，不判
        if math.hypot(me[0] - prev[0], me[1] - prev[1]) < JUMP_TILES:
            return
        steps[i]["land"] = [round(me[0], 1), round(me[1], 1)]
        steps[i]["scene"] = scene.map_key(scene.current_id(sc))
        self._pw = None
        self._pw_timer.stop()
        self._refresh_steps()
        self._say_map(f"第 {i + 1} 步的傳點出口記起來了："
                      f"({me[0]:.0f}, {me[1]:.0f}) —— 記得按儲存")

    def _move(self, delta: int) -> None:
        i = self.steps.currentRow()
        if i < 0:
            return
        j = self._script.move(i, delta)
        self._refresh_steps()
        self.steps.setCurrentRow(j)

    def _del(self) -> None:
        i = self.steps.currentRow()
        if i < 0:
            return
        self._script.remove(i)
        self._refresh_steps()

    # ------------------------------------------------------------------
    # 地圖
    # ------------------------------------------------------------------
    def _draw(self) -> None:
        _pid, sc = self._cur()
        if sc is None:
            self.status.setText("先選一台分身")
            return
        from app.game import terrain
        grid, why = terrain.load(sc)
        if grid is None:
            # ⚠ 半張地圖比沒有地圖危險：terrain.load 讀不到任何一列就整張失敗。
            self.status.setText(f"⚠ 讀不到地形圖：{why}")
            return
        self._grid = grid
        self._grid_key = scene.map_key(scene.current_id(sc))
        self.status.setText("正在切連通區…")
        QWidget.repaint(self)
        t0 = time.time()
        self._rooms, self._sizes = dungeon.rooms(grid)
        self._base = None                  # 換圖了 → 底圖快取作廢
        props = scenery.nearby(sc)
        self._props_all = props or []
        sid = scene.current_id(sc)
        note = (f"{scene.scene_name(sid)}　{grid.w}x{grid.h}　"
                f"可走 {sum(sum(r) for r in grid.open)} 格　"
                f"房間 {len(self._sizes)} 間 {self._sizes}　"
                f"可互動物件 {len(self._props_all)} 個"
                f"（{(time.time() - t0) * 1000:.0f} ms）")
        if self._big is None:
            self._big = MapWindow(self)
        self._big.show()
        self._big.raise_()
        self._big._fit()
        self._say_map(note)

    def _base_image(self) -> QImage:
        """1 像素 1 格的底圖（可走格依房間上色）。放大交給 scaled()。

        ⚠ **要快取**：420x230 ＝ 96600 次 setPixel，即時更新每半秒重算一次會
          把一顆核心吃滿。底圖只跟地形與房間有關，重畫疊圖時不必重算。
        """
        if self._base is not None:
            return self._base
        g = self._grid
        img = QImage(g.w, g.h, QImage.Format_RGB32)
        img.fill(QColor(*WALL_COLOR))
        for y in range(g.h):
            row = g.open[y]
            for x in range(g.w):
                if not row[x]:
                    continue
                r = self._rooms.get((x, y))
                c = ROOM_COLORS[r % len(ROOM_COLORS)] if r is not None \
                    else (70, 70, 70)
                img.setPixel(x, y, QColor(*c).rgb())
        self._base = img
        return img

    def _redraw_overlay(self) -> None:
        if self._big is not None and self._big.isVisible():
            self._big.redraw()

    def _render(self, s: float) -> QPixmap:
        """把地圖畫成一張圖。`s` ＝ 一格幾個像素（可以小於 1 ＝ 縮小）。"""
        if self._grid is None:
            return QPixmap()
        g = self._grid
        w, h = max(1, int(g.w * s)), max(1, int(g.h * s))
        pix = QPixmap.fromImage(
            self._base_image().scaled(w, h, Qt.IgnoreAspectRatio,
                                      Qt.FastTransformation))
        p = QPainter(pix)
        # 可互動的物件。⚠ 場景標記點（TAG01/TAG02…，SP_ATTRIB_HIDE）畫面上
        #   根本看不見，一張圖上有好幾百個 —— 全畫成紅點會把真正的機關淹掉
        #   （使用者 2026-09-02：「為何繪製地圖那麼多紅點」）。跟下面的清單
        #   用**同一個**勾選框，預設不畫；要看就取消勾選。
        p.setPen(Qt.NoPen)
        hide_tag = self.hide_tag.isChecked()
        for pr in getattr(self, "_props_all", []):
            tag = mapobj.hidden(pr.model)
            if tag:
                if hide_tag:
                    continue
                p.setBrush(QColor(90, 95, 105))      # 標記點：暗灰小點
                p.drawEllipse(int(pr.x * s) - 1, int(pr.y * s) - 1, 3, 3)
                continue
            p.setBrush(QColor(220, 80, 80))
            p.drawEllipse(int(pr.x * s) - 2, int(pr.y * s) - 2, 5, 5)
        # 已存的步驟。⚠ 只畫**這張圖**的：走過傳點的腳本會跨好幾張圖，
        #   把別張圖的座標疊上來只會誤導（同一組座標在兩張圖是不同地方）。
        for i, (x, y) in ((i, xy) for i, xy, _k in self._script.points()
                          if self._grid_key is None
                          or dungeon.map_at(self._script, i) in
                          (None, self._grid_key)):
            kind = self._script.steps[i].get("do")
            if kind == dungeon.WALK:
                col = QColor(90, 170, 255)          # 走到：藍
            elif kind == dungeon.PORTAL:
                col = QColor(120, 235, 140)         # 傳點：綠（會換圖，特別標）
            else:
                col = QColor(210, 140, 255)         # 對話：紫
            p.setPen(QPen(col, 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(int(x * s) - 5, int(y * s) - 5, 11, 11)
            p.drawText(int(x * s) + 7, int(y * s) - 6, str(i + 1))
        # 在地圖上點到的那一格
        if self._pick is not None:
            p.setPen(QPen(QColor(255, 255, 255), 1))
            p.setBrush(Qt.NoBrush)
            px, py = int(self._pick[0] * s), int(self._pick[1] * s)
            p.drawRect(px - 3, py - 3, 7, 7)
        # 角色（黃色十字）
        _pid, sc = self._cur()
        me = self._me(sc) if sc is not None else None
        if me:
            p.setPen(QPen(QColor(255, 220, 60), 2))
            mx, my = int(me[0] * s), int(me[1] * s)
            p.drawLine(mx - 6, my, mx + 6, my)
            p.drawLine(mx, my - 6, mx, my + 6)
        p.end()
        return pix

    def _on_pick(self, x: float, y: float) -> None:
        if self._grid is None:
            return
        gx, gy = int(x), int(y)
        if not (0 <= gx < self._grid.w and 0 <= gy < self._grid.h):
            return
        walk = self._grid.walkable(gx, gy)
        room = self._rooms.get((gx, gy))
        self._pick = (gx, gy)
        if self._big is not None:
            # ⚠ 不可走的格子不給加：走不到的終點會讓執行端一直重試到逾時。
            self._big.add_pick.setEnabled(walk)
            self._big.add_portal.setEnabled(walk)
            self._big.pick_lbl.setText(
                f"點到 ({gx}, {gy})　"
                + (f"房間 {room}" if room is not None else
                   ("可走（碎片區）" if walk else "⚠ 這格不能走")))
        self._redraw_overlay()

    # ------------------------------------------------------------------
    # 對話
    # ------------------------------------------------------------------
    def _scan_props(self) -> None:
        _pid, sc = self._cur()
        if sc is None:
            self.status.setText("先選一台分身")
            return
        me = self._me(sc)
        if me is None:
            self.status.setText("⚠ 讀不到角色位置")
            return
        props = scenery.nearby(sc, me, PROP_RADIUS)
        if props is None:
            # ⚠ 讀不到 ≠ 附近沒有東西（[[bag-false-empty-guards]] 的同一個坑）。
            self.status.setText("⚠ 物件清單讀不到（不是「附近沒有」）")
            return
        shown = [p for p in props
                 if not (self.hide_tag.isChecked() and mapobj.hidden(p.model))]
        hidden_n = len(props) - len(shown)
        props = shown
        self._props = props
        self.props.clear()
        for pr in props:
            # ★ 印名字不只印編號（2026-09-02）：光看「外觀 60049」認不出那是
            #   「惡魔系雕像01」。查不到名字就退回顯示編號（安全退化）。
            self.props.addItem(
                f"{mapobj.label(pr.model)}　({_fmt(pr.x)}, {_fmt(pr.y)})　"
                f"{pr.dist(me):.1f} 格")
        if props:
            self.props.setCurrentRow(0)
        tail = f"（另有 {hidden_n} 個看不見的標記點沒列）" if hidden_n else ""
        self.status.setText(
            f"附近 {PROP_RADIUS:.0f} 格內有 {len(props)} 個點得到的物件{tail}"
            if props else
            f"附近 {PROP_RADIUS:.0f} 格內沒有可列的物件{tail}"
            "　—— 走近一點再掃，或取消勾選把標記點也列出來")

    def _preview(self, item) -> None:
        """滑鼠移到清單某一條 → 顯示那個外觀的圖。"""
        i = self.props.row(item)
        if not 0 <= i < len(self._props):
            return
        from PySide6.QtGui import QCursor
        self._peek.show_for(self._props[i].model, QCursor.pos())

    def eventFilter(self, obj, ev):                      # noqa: N802（Qt 命名）
        # 滑鼠離開清單就把預覽收掉（itemEntered 不會告訴我們「移出去了」）。
        if obj is self.props.viewport() and ev.type() == ev.Type.Leave:
            self._peek.hide()
        return super().eventFilter(obj, ev)

    def _dialog_token(self, sc):
        """對話框的執行期代號。⚠ 非 0 **不代表開著**，只能拿來比「有沒有變」。"""
        try:
            g = lua.globals_of(sc, (supply.DIALOG_WND,))
        except Exception:                                # noqa: BLE001
            return None
        return None if not g else g.get(supply.DIALOG_WND)

    def _poke(self) -> None:
        pid, sc = self._cur()
        if sc is None:
            self.status.setText("先選一台分身")
            return
        i = self.props.currentRow()
        if not 0 <= i < len(self._props):
            self.status.setText("先在清單裡選一個物件")
            return
        mv = self._mover(pid)
        if mv is None:
            return
        prop = self._props[i]
        before = self._dialog_token(sc)
        ok, msg = produce.click(mv, sc, prop)
        if not ok:
            self.status.setText(f"⚠ 點不下去：{msg}")
            return
        self._poked = prop
        # ★ 記下**點下去的當下我站在哪**（使用者 2026-09-02：「跟 NPC 對話會
        #   記錄我在哪個位置對話的，會走到那個位置才對話」）—— 比「靠近那個
        #   物件」可靠得多：物件常常站在不可走的格上，而站位一定站得住
        #   （你當時就站在那裡講到話）。
        self._poked_at = self._me(sc)
        self._menu = []
        self._refresh_menu()
        self.save_talk.setEnabled(True)
        # ⚠⚠ 等對話框**不可以**在 GUI 執行緒裡 sleep 迴圈（2026-09-02 使用者
        #   回報「點點看無效，只會讓我程式當機好幾秒」）—— 那是最多 12 秒的
        #   卡死，畫面不重畫、按鈕按不動，看起來就像當掉。改用計時器輪詢。
        self._poke_base = before
        self._poke_until = time.time() + DIALOG_WAIT
        self.status.setText(f"已點 {mapobj.label(prop.model)}"
                            f" ({_fmt(prop.x)},{_fmt(prop.y)})　等對話框…")
        self._poke_timer.start(100)

    def _poke_check(self) -> None:
        """輪詢對話框開了沒（接在「點點看」後面，不卡畫面）。"""
        _pid, sc = self._cur()
        if sc is None:
            self._poke_timer.stop()
            return
        now = self._dialog_token(sc)
        if now is not None and now != self._poke_base and now != 0:
            self._poke_timer.stop()
            self.status.setText(
                "對話框開了 —— 看遊戲畫面，按下面對應的「第 N 項」")
            return
        if time.time() >= self._poke_until:
            self._poke_timer.stop()
            # ⚠ 「沒看到變化」≠「沒點到」：有些機關是純動作（開門、放火），
            #   根本不會開對話框。所以這裡只陳述事實，不下結論。
            self.status.setText(
                "⚠ 沒看到對話框變化 —— 可能是純動作的機關（開門那種），"
                "也可能沒點到。看一下遊戲畫面：真的開了就直接按「第 N 項」；"
                "什麼都沒發生就把這一步存成沒有選項的對話。")

    def _pass_page(self) -> None:
        """把「無異議對話」那一頁按掉。⚠ **不記進腳本**。

        使用者 2026-09-02：「只要沒選項就幫我對話到結束或出現選項這樣可以嗎」
        —— 可以，執行端自己判斷（`talkwnd.page`：純對話 IS_TALK=1、
        有選項 OPTIONn 非 0），所以製作時按這顆只是**往下翻一頁**給你看，
        腳本裡只留「選了第幾項」。
        """
        pid, sc = self._cur()
        if sc is None:
            return
        mv = self._mover(pid)
        if mv is None:
            return
        ok, why = talkwnd.close_page(mv, sc)
        self.status.setText(
            "已按確定（這一頁不記進腳本，跑的時候會自動過）" if ok
            else f"⚠ 過不掉這一頁（{why}）")

    def _send_option(self, n: int) -> None:
        pid, sc = self._cur()
        if sc is None:
            return
        mv = self._mover(pid)
        if mv is None:
            return
        if not sell.talk(mv, supply.talk_option(n)):
            self.status.setText(f"⚠ 第 {n} 項送不出去（指令槽忙碌）—— 再按一次")
            return
        self._menu.append(n)
        self._refresh_menu()
        self.save_talk.setEnabled(True)
        # ★★ 使用者 2026-09-02：「不行，我按 1 就進副本，無法設定」——
        #   按下選項人就被傳走了，來不及再按「這是進副本的入口」。
        #   所以只要**現在正對著已經記好的入口**，這一項就自動記進入口。
        if self._entrance_here():
            ent = self._script.entrance
            ent.setdefault("menu", []).append(n)
            self._refresh_stamp()
            self.status.setText(
                f"已送第 {n} 項，並記進**入口**的選項路徑"
                f"（{' → '.join('第%d項' % k for k in ent['menu'])}）"
                "　—— 記得按儲存")
            return
        self.status.setText(f"已送第 {n} 項 —— 看遊戲畫面有沒有進到下一頁")

    def _refresh_menu(self) -> None:
        """把「這一步會存成什麼」寫在畫面上。

        ★ 使用者 2026-09-02 問「所以單純過對話按鈕呢」——**不另外加一顆**：
          純對話就是「點點看之後一個選項都不按，直接存」。原本畫面只寫
          「（還沒選）」，看起來像沒做完，所以改成講清楚它會存成純對話，
          存檔鈕的字也跟著變。（⛔ 再開一顆按鈕做同一件事，就是剛拿掉的
          「清光周圍的怪」那種多餘指令。）
        """
        if self._menu:
            path = " → ".join(f"第{n}項" for n in self._menu)
            btn = f"存這一步（選項 {path}）"
        elif self._poked is not None:
            path = "**沒按選項**＝這一段不用選（跑的時候自動按到結束）"
            btn = "存這一步（純對話）"
        else:
            path = "（先按「點點看」試一個物件）"
            btn = "把這一步存進腳本"
        self.menu_lbl.setText("已選路徑：" + path)
        if hasattr(self, "save_talk"):
            self.save_talk.setText(btn)

    def _clear_menu(self) -> None:
        self._menu = []
        self._refresh_menu()

    def _leave(self) -> None:
        """離開對話：**先把框從畫面上收掉**，再送離開互動。

        ⚠ 使用者 2026-09-02 問「那我按鈕的離開對話會關視窗嗎」——本來不會。
          `leave_npc` 是 0x22（告訴伺服器我不跟你講話了），畫面上那個框是
          客戶端 UI 自己收的，要叫 Lua 的 `DestroyMessageWnd`（見 talkwnd）。
          兩件事都做才是真的「離開對話」。
        """
        pid, sc = self._cur()
        if pid is None:
            return
        mv = self._mover(pid)
        if mv is None:
            return
        closed = talkwnd.close_window(mv, sc)
        supply.leave_npc(mv, sc)
        self.status.setText("已關掉對話框並送出離開互動" if closed
                            else "已送出離開互動（⚠ 對話框沒關成，再按一次）")

    def _save_talk(self) -> None:
        if self._poked is None:
            self.status.setText("先按「點點看」試一個物件")
            return
        pr = self._poked
        # ⚠⚠ 只存位置與外觀。選定 id 的高 16 位是伺服器每次載入地圖重配的
        #   世代碼，存了下次進場一定失效（scenery.py 檔頭，2026-08-12 實機）。
        step = {"do": dungeon.INTERACT,
                "at": [round(pr.x, 1), round(pr.y, 1)],
                "model": pr.model,
                "menu": list(self._menu)}
        # ★ 站位：點下去那一刻的位置（讀不到就不寫，跑的時候退回「靠近物件」）
        at = getattr(self, "_poked_at", None)
        if at:
            step["stand"] = [round(at[0], 1), round(at[1], 1)]
        self._add(step)
        self._poked = None
        self._poked_at = None
        self._menu = []
        self._refresh_menu()
        self.save_talk.setEnabled(False)

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        """應用程式關閉前的收尾（⚠ 是 on_close 不是 closeEvent，見 dungeon_tab）。"""
        self._poke_timer.stop()
        self._pw_timer.stop()
        self._pw = None
        if self._big is not None:
            self._big._timer.stop()      # 地圖視窗每半秒重畫，要停掉
            self._big.close()
        # ★ 用 release() 不要 stop()：跳板是同一個 PID 共用的。
        for pid in list(self._movers):
            try:
                move.release(pid, self)
            except Exception:                            # noqa: BLE001
                pass
        self._movers.clear()
        for sc in self._scanners.values():
            sc.close()
        self._scanners.clear()
