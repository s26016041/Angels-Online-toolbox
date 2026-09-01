"""副本腳本製作：把一趟副本要做的事，在介面上一步一步「試」出來存成 JSON。

## 為什麼不是錄製（使用者 2026-09-01 定案）

> 「不要錄製問題太多，你不知道我在那點位待那麼久要幹嘛很迷。」

錄影只能**事後猜**使用者做了什麼（點了哪個物件、選了第幾項），猜錯還不會報錯。
這裡反過來：**工具自己送、使用者在旁邊挑** —— 工具送出去的東西它自己知道，
不必猜任何事。

## 對話那一步怎麼做出來

1. 「重新掃描」列出附近**可互動的物件**（`scenery.nearby`，跟製作檯同一族：
   vtable 對得上、採集種類 0、選定 id 合法 —— 純裝飾點了沒反應的會被排掉）。
2. 選一個按「點點看」→ 走 `produce.click()`（＝封包 `0x05`，跟遊戲自己點一樣）。
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

import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
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
                      scene, scenery, sell, supply)
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

    def __init__(self, parent, render, on_pick) -> None:
        super().__init__(parent)
        self.setWindowTitle("副本地圖")
        self.setModal(False)                     # ⚠ 非強制回應：開著也能操作主視窗
        self.resize(1100, 760)
        self._render = render                    # (縮放) → QPixmap
        self._on_pick = on_pick
        v = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("縮放"))
        self.zoom = QSpinBox()
        self.zoom.setRange(1, 12)
        self.zoom.setValue(4)
        self.zoom.valueChanged.connect(self.redraw)
        bar.addWidget(self.zoom)
        b = QPushButton("符合視窗")
        b.setToolTip("自動算一個剛好把整張圖塞進視窗的縮放。")
        b.clicked.connect(self._fit)
        bar.addWidget(b)
        bar.addStretch(1)
        self.info = QLabel("　")
        bar.addWidget(self.info)
        v.addLayout(bar)
        self.canvas = MapCanvas()
        self.canvas.picked.connect(self._picked)
        self.area = QScrollArea()
        self.area.setWidget(self.canvas)
        self.area.setWidgetResizable(False)
        v.addWidget(self.area, 1)

    def _picked(self, x: float, y: float) -> None:
        self._on_pick(x, y)

    def _fit(self) -> None:
        pix = self._render(1)
        if pix.isNull():
            return
        vp = self.area.viewport().size()
        s = min(vp.width() / max(pix.width(), 1),
                vp.height() / max(pix.height(), 1))
        self.zoom.setValue(max(1, int(s)))
        self.redraw(fit_to=s if s < 1 else None)

    def redraw(self, _v=None, fit_to: float | None = None) -> None:
        s = fit_to if fit_to else self.zoom.value()
        pix = self._render(s)
        if not pix.isNull():
            self.canvas.show_map(pix, s)
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
        self._props: list = []            # 上次掃到的可互動物件（附近，挑著點）
        self._props_all: list = []        # 繪製地圖時掃到的全部物件（畫紅點）
        self._pick = None                 # 在地圖上點到的格子
        self._menu: list[int] = []        # 正在試的對話選項路徑
        self._poked = None                # 正在試的那個物件（scenery.Prop）
        self._big = None                  # 放大檢視的獨立視窗
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
            ("另存新檔", "存成新的檔名。", self._save_as),
            ("開啟資料夾", f"腳本都放在 {dungeon.folder()}", self._open_folder),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            fbar.addWidget(b)
        fbar.addStretch(1)
        root.addLayout(fbar)

        # ── 地圖 ＋ 步驟 ────────────────────────────────────
        mid = QHBoxLayout()

        mapbox = QGroupBox("地圖")
        mv = QVBoxLayout(mapbox)
        mh = QHBoxLayout()
        draw = QPushButton("繪製地圖")
        draw.setToolTip("把這台分身**目前所在**的地圖畫出來。\n"
                        "互不相通的房間會用不同顏色，可互動的物件標成紅點。")
        draw.clicked.connect(self._draw)
        mh.addWidget(draw)
        mh.addWidget(QLabel("縮放"))
        self.zoom = QSpinBox()
        self.zoom.setRange(1, 8)
        self.zoom.setValue(2)
        self.zoom.setToolTip("一格畫幾個像素。")
        self.zoom.valueChanged.connect(lambda _v: self._redraw_overlay())
        mh.addWidget(self.zoom)
        big = QPushButton("放大檢視")
        big.setToolTip("把地圖開在一個可以拉大／最大化的獨立視窗，\n"
                       "裡面一樣可以點位置。")
        big.clicked.connect(self._open_big)
        mh.addWidget(big)
        mh.addStretch(1)
        mv.addLayout(mh)

        self.canvas = MapCanvas()
        self.canvas.picked.connect(self._on_pick)
        area = QScrollArea()
        area.setWidget(self.canvas)
        area.setWidgetResizable(False)
        area.setMinimumHeight(320)
        mv.addWidget(area, 1)

        ph = QHBoxLayout()
        self.pick_lbl = QLabel("點一下地圖選位置")
        ph.addWidget(self.pick_lbl)
        ph.addStretch(1)
        self.add_pick = QPushButton("加入點到的位置")
        self.add_pick.setToolTip("把地圖上點到的那一格，加成一個「走到」步驟。")
        self.add_pick.setEnabled(False)
        self.add_pick.clicked.connect(self._add_picked)
        ph.addWidget(self.add_pick)
        b = QPushButton("加入我現在站的位置")
        b.setToolTip("把角色**現在**站的那一格，加成一個「走到」步驟。")
        b.clicked.connect(self._add_here)
        ph.addWidget(b)
        mv.addLayout(ph)
        mid.addWidget(mapbox, 3)

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
        b = QPushButton("加入「清光周圍的怪」")
        b.setToolTip(
            "在這裡**停下來等到完全沒怪**才繼續。\n"
            "（平常本來就會邊走邊清，這一步是要它站住不要往前走 —— \n"
            "  排在對話前面就不會遇到「還有怪物」那種點不動的對話。）")
        b.clicked.connect(lambda: self._add({"do": dungeon.CLEAR}))
        sh2.addWidget(b)
        b = QPushButton("加入「等待」")
        b.setToolTip("單純等幾秒（例如等門開的動畫）。")
        b.clicked.connect(self._add_wait)
        sh2.addWidget(b)
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
        b.setToolTip("送出離開互動（不送的話伺服器會覺得你還在跟它講話）。")
        b.clicked.connect(self._leave)
        th.addWidget(b)
        # ★ 場景標記點（TAG01/TAG02…）畫面上根本看不見，但照樣有互動 id，
        #   一站就掃到 45 個把真正的機關淹掉 —— 預設收起來。
        self.hide_tag = QCheckBox("藏起看不見的標記點")
        self.hide_tag.setChecked(True)
        self.hide_tag.setToolTip(
            "資源包標了「看不見」的物件（TAG01 這種伺服器用的位置標記）不列出來。\n"
            "找不到你要的機關時可以取消勾選，把全部都列出來。")
        self.hide_tag.toggled.connect(lambda _v: self._scan_props())
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
        oh.addWidget(QLabel("選項"))
        for n in range(1, dungeon.MENU_MAX + 1):
            b = QPushButton(str(n))
            b.setFixedWidth(34)
            b.setToolTip(f"送出對話選單的第 {n} 項，並記進這一步的路徑。")
            b.clicked.connect(lambda _=False, k=n: self._send_option(k))
            oh.addWidget(b)
        oh.addStretch(1)
        tv.addLayout(oh)

        ph2 = QHBoxLayout()
        self.menu_lbl = QLabel("已選路徑：（還沒選）")
        ph2.addWidget(self.menu_lbl)
        ph2.addStretch(1)
        b = QPushButton("清掉路徑")
        b.setToolTip("選錯了重來（不會影響已存的步驟）。")
        b.clicked.connect(self._clear_menu)
        ph2.addWidget(b)
        self.save_talk = QPushButton("把這一步存進腳本")
        self.save_talk.setToolTip("存成「對話」步驟：位置＋外觀＋選項路徑。")
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
        for sp in self.findChildren(QSpinBox):
            fit_spin(sp)
        self._reload_files()

    def _open_big(self) -> None:
        if self._grid is None:
            self.status.setText("先按「繪製地圖」")
            return
        if self._big is None:
            self._big = MapWindow(self, self._render, self._on_pick)
        self._big.show()
        self._big.raise_()
        self._big._fit()

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
        self._stamp_map()
        self._refresh_steps()
        self.files.blockSignals(True)
        self.files.setCurrentIndex(0)
        self.files.blockSignals(False)
        self.status.setText(f"新腳本「{name.strip()}」—— 記得按儲存")

    def _stamp_map(self) -> None:
        """把目前這張圖的場景編號與指紋蓋進腳本（開跑前用來比對）。"""
        _pid, sc = self._cur()
        if sc is None:
            return
        try:
            sid = scene.current_id(sc)
            self._script.scene = scene.map_key(sid)
            from app.game import terrain
            grid, _why = terrain.load(sc)
            if grid is not None:
                self._script.map = dungeon.fingerprint(grid)
        except Exception:                                # noqa: BLE001
            pass

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
        name = self._script.name or "副本"
        path, _f = QFileDialog.getSaveFileName(
            self, "另存腳本", str(dungeon.folder() / f"{name}.json"),
            "腳本 (*.json)")
        if not path:
            return
        from pathlib import Path
        self._path = Path(path)
        self._script.name = self._path.stem
        if self._script.scene is None:
            self._stamp_map()
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
        self._redraw_overlay()

    def _add(self, step: dict) -> None:
        ok, why = dungeon.validate(step)
        if not ok:
            self.status.setText(f"⚠ 這一步有問題：{why}")
            return
        self._script.add(step)
        self._refresh_steps()
        self.steps.setCurrentRow(self.steps.count() - 1)
        self.status.setText(f"已加入：{dungeon.describe(step)}")

    def _add_wait(self) -> None:
        secs, ok = QInputDialog.getDouble(self, "等待", "等幾秒？", 3.0,
                                          0.5, 600.0, 1)
        if ok:
            self._add({"do": dungeon.WAIT, "secs": round(secs, 1)})

    def _add_here(self) -> None:
        _pid, sc = self._cur()
        if sc is None:
            self.status.setText("先選一台分身")
            return
        me = self._me(sc)
        if me is None:
            # 讀不到就什麼都不做 —— 存一個 (0,0) 進去比不存危險得多。
            self.status.setText("⚠ 讀不到角色位置，這一步沒有存")
            return
        self._add({"do": dungeon.WALK, "to": [round(me[0]), round(me[1])]})

    def _add_picked(self) -> None:
        if self._pick is None:
            return
        self._add({"do": dungeon.WALK,
                   "to": [int(self._pick[0]), int(self._pick[1])]})

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
        self.status.setText("正在切連通區…")
        QWidget.repaint(self)
        t0 = time.time()
        self._rooms, self._sizes = dungeon.rooms(grid)
        props = scenery.nearby(sc)
        self._props_all = props or []
        self._redraw_overlay()
        sid = scene.current_id(sc)
        self.status.setText(
            f"{scene.scene_name(sid)}　{grid.w}x{grid.h}　"
            f"可走 {sum(sum(r) for r in grid.open)} 格　"
            f"房間 {len(self._sizes)} 間 {self._sizes}　"
            f"可互動物件 {len(self._props_all)} 個"
            f"（{(time.time() - t0) * 1000:.0f} ms）")

    def _base_image(self) -> QImage:
        """1 像素 1 格的底圖（可走格依房間上色）。放大交給 scaled()。"""
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
        return img

    def _redraw_overlay(self) -> None:
        if self._grid is None:
            return
        s = self.zoom.value()
        self.canvas.show_map(self._render(s), s)
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
        # 可互動的物件（紅點）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(220, 80, 80))
        for pr in getattr(self, "_props_all", []):
            p.drawEllipse(int(pr.x * s) - 2, int(pr.y * s) - 2, 5, 5)
        # 已存的步驟
        for i, (x, y) in ((i, xy) for i, xy, _k in self._script.points()):
            kind = self._script.steps[i].get("do")
            col = QColor(90, 170, 255) if kind == dungeon.WALK \
                else QColor(210, 140, 255)
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
        # ⚠ 不可走的格子不給加：走不到的終點會讓執行端一直重試到逾時。
        self.add_pick.setEnabled(walk)
        self.pick_lbl.setText(
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
        self.status.setText(f"已送第 {n} 項 —— 看遊戲畫面有沒有進到下一層")

    def _refresh_menu(self) -> None:
        self.menu_lbl.setText(
            "已選路徑：" + (" → ".join(f"第{n}項" for n in self._menu)
                            if self._menu else "（還沒選）"))

    def _clear_menu(self) -> None:
        self._menu = []
        self._refresh_menu()

    def _leave(self) -> None:
        pid, _sc = self._cur()
        if pid is None:
            return
        mv = self._mover(pid)
        if mv is not None:
            supply.leave_npc(mv)
            self.status.setText("已送出離開互動")

    def _save_talk(self) -> None:
        if self._poked is None:
            self.status.setText("先按「點點看」試一個物件")
            return
        pr = self._poked
        # ⚠⚠ 只存位置與外觀。選定 id 的高 16 位是伺服器每次載入地圖重配的
        #   世代碼，存了下次進場一定失效（scenery.py 檔頭，2026-08-12 實機）。
        self._add({"do": dungeon.INTERACT,
                   "at": [round(pr.x, 1), round(pr.y, 1)],
                   "model": pr.model,
                   "menu": list(self._menu)})
        self._poked = None
        self._menu = []
        self._refresh_menu()
        self.save_talk.setEnabled(False)

    # ------------------------------------------------------------------
    def closeEvent(self, ev) -> None:                    # noqa: N802
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
        super().closeEvent(ev)
