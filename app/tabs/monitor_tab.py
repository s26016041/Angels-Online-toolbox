"""監控技能經驗球分頁（產品主畫面）。

即時監控每個天使之戀分身正在練的技能經驗球：
  1. 按「開始監控」→ 自動找出所有分身。
  2. 背景用 AOB 特徵掃出每台的技能經驗球候選位址，快執行緒每 ~0.7 秒讀一次值，
     比對前後兩輪、挑出「正在增加」的那顆來跟隨，顯示即時值。
     ★ 位址跑掉會自動跟上 ★——換地圖 / 重連 / 升級會讓遊戲把技能結構搬到別的位址
     （舊位址的值會凍住）。只要球還在增加，就證明位址還是對的、不必重掃；一旦「安靜
     太久」（搬家、斷線、或真的滿了）就重掃特徵補上新位址，效果等同「手動按停止再
     開始」，但是自動的。全掃很貴（一台約 800MB、要 1～2.5 秒），所以只在需要時掃，
     否則會跟 UI 搶 CPU、打字和跳視窗都會頓。
  3. 若跟隨的球「持續 N 秒沒有再增加」（期間重掃也沒有任何在動的球），或「突然
     掃不到特徵」（很可能是斷線 / 遊戲關了，也照樣併入這段倒數）→ 判定經驗球
     滿了 / 遊戲停止/斷線 → 自動停該台、循環發警報聲、跳警告視窗標明帳號；警報聲
     持續到按「停止警報」為止。一定要先偵測到一次增加，才會開始算這段時間。
     N 由畫面上的「無變化多久算滿」欄位決定（預設 300 秒 = 5 分鐘），會存進設定檔。

全程只讀取記憶體、不搶焦點、不掛除錯器（安全）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config import config
from app.core import charname, notify
from app.core import window as win
from app.core.memory import MemoryScanner
from app.game import aob
from app.tabs.base_tab import BaseTab

SIG = aob.SKILL_EXP_BALL
# AOB 特徵較鬆（會命中所有技能結構），上限開大以免漏掉目標；靠「正在增加」選出在練技能。
SCAN_LIMIT = 4096
# 數值持續這麼多秒沒再增加 → 判定經驗球滿了 / 遊戲停止 → 警報。
# 實際採用值由使用者在畫面上設定（存 config：monitor.no_change_secs），這裡只是預設值。
DEFAULT_NO_CHANGE_SECS = 300  # 5 分鐘
# 想打多少就打多少：負數一律夾成 0，離譜大數（超過 1 天）夾成上限。
MIN_NO_CHANGE_SECS = 0
MAX_NO_CHANGE_SECS = 86400
# 快速讀值的間隔（毫秒）：決定畫面值多快更新一次。
READ_INTERVAL_MS = 700
# ScanWorker 多久醒來檢查一次「有誰需要重掃」（檢查本身極便宜）。
SCAN_POLL_SECS = 0.5
# 完全找不到候選位址時的重掃間隔（例如還在讀取畫面）。要積極、但別連環掃。
NO_CANDS_GAP_SECS = 5.0
# 有候選位址、但從沒看過它增加（沒在練功 / 剛進遊戲）→ 隔這麼久才重掃一次。
IDLE_RESCAN_SECS = 30.0
# 安靜的分身重掃間隔的上限：越久沒動就越少重掃（見 ScanWorker._needs_scan）。
MAX_RESCAN_GAP_SECS = 60.0


def _rescan_quiet_secs(no_change_secs: int) -> float:
    """經驗球「安靜」多久就該重掃特徵（可能是搬家了，不是真的滿了）。

    取警報門檻的 1/3、夾在 3～10 秒之間。練功順利時經驗球每幾秒就跳一次，所以這條
    幾乎不會成立 → 背景等於完全不掃、CPU 幾乎歸零；真的搬家 / 斷線時最慢 10 秒內就
    會重掃補上新位址，遠早於警報門檻，不會誤報。
    """
    return max(3.0, min(10.0, no_change_secs / 3))


class ScanWorker(QThread):
    """慢速執行緒：需要時才全記憶體重掃 AOB 特徵，更新每台的候選位址清單。

    「全掃」很貴——一台分身有 ~800MB 可寫記憶體，掃一次要 1～2.5 秒；5 台掃一輪要
    8 秒。以前是「無腦每 3 秒重掃全部」，等於七成時間都在全掃，跟 UI 搶 CPU/GIL，
    打字和跳視窗都會頓。

    其實只要經驗球還在增加，就證明現在的候選位址是對的、根本不用重掃。所以改成
    「按需重掃」：只掃那些「還沒有候選位址」或「安靜太久」（球滿了 / 斷線 / 換地圖
    把技能結構搬走了）的分身。正常練功時背景幾乎不掃 → CPU 幾乎歸零，但換地圖搬家
    後照樣會在 10 秒內重掃補上新位址，使用者體感不變。

    共享的 dict（本執行緒讀寫、UI 執行緒另一端）：
      self._cands  {pid: [候選位址]}      本執行緒寫、ReadWorker 讀
      self._names  {pid: 角色名}          本執行緒寫、UI 讀
      self._health {pid: 上次增加的時間}  UI 執行緒寫、本執行緒讀（判斷誰該重掃）
      self._quiet  [安靜門檻秒數]         UI 執行緒寫（跟著秒數設定走）、本執行緒讀
    """

    def __init__(self, insts, cands: dict, names: dict, health: dict, quiet: list) -> None:
        super().__init__()
        self._insts = insts  # [(pid, title, sc), ...]
        self._cands = cands
        self._names = names
        self._health = health
        self._quiet = quiet
        self._last_scan: dict[int, float] = {}
        self._running = True

    def _needs_scan(self, pid: int, now: float) -> bool:
        since = now - self._last_scan.get(pid, 0.0)   # 距離上次全掃這台多久
        if not self._cands.get(pid):
            return since >= NO_CANDS_GAP_SECS  # 沒候選位址 → 沒東西可讀，得積極找

        last_inc = self._health.get(pid)
        if last_inc is None:
            # 有候選、但從沒看過它增加：可能沒在練功，也可能第一次掃到的是壞資料
            # （例如在讀取畫面掃的）→ 久久重掃一次當保險。
            return since >= IDLE_RESCAN_SECS

        quiet = now - last_inc
        if quiet < self._quiet[0]:
            return False  # 球還在跳 → 現在的位址就是對的 → 完全不用掃（穩態走這條）

        # 安靜了。分不出是「換地圖搬家」（重掃就救得回來）還是「球真的滿了」（重掃也
        # 沒用），所以照掃——但間隔隨著安靜時間拉長。不然球滿了的那台會在整個倒數期間
        # （預設 5 分鐘）被連續全掃，CPU 燒好燒滿，偏偏這時候重掃根本救不了什麼。
        gap = max(self._quiet[0], min(MAX_RESCAN_GAP_SECS, quiet / 2))
        return since >= gap

    def run(self) -> None:
        while self._running:
            for pid, title, sc in self._insts:
                if not self._running:
                    return
                # 角色名解一次就好（存檔路徑不會變）；失敗記 "?" 不重試。
                if pid not in self._names:
                    try:
                        self._names[pid] = charname.read_character_name(
                            sc, charname.account_from_title(title)) or "?"
                    except Exception:
                        self._names[pid] = "?"
                if not self._needs_scan(pid, time.monotonic()):
                    continue
                try:
                    hits = aob.scan(sc, SIG, limit=SCAN_LIMIT,
                                    should_stop=lambda: not self._running)
                except Exception:
                    hits = None
                if not self._running:
                    return  # 中途被喊停 → hits 是不完整的，別拿去覆蓋候選清單
                if hits is not None:
                    self._cands[pid] = hits
                self._last_scan[pid] = time.monotonic()
            # 歇一下再檢查誰需要重掃；分段睡以便能盡快響應停止。
            for _ in range(max(1, int(SCAN_POLL_SECS * 10))):
                if not self._running:
                    return
                self.msleep(100)

    def stop(self) -> None:
        self._running = False


class ReadWorker(QThread):
    """快速執行緒：每 ~0.7 秒讀一次候選位址的現值，回傳 {pid: {位址: 值}} 快照。

    只讀「已知候選位址」的值（很快），所以畫面更新回到即時；不做全掃。搭配 ScanWorker
    保持候選位址最新即可。與 ScanWorker 共用 scanner——兩者都只讀取、不改動 scanner
    內部掃描狀態，ReadProcessMemory 本身可並行，安全。
    """

    snapshot = Signal(object)  # {pid: {addr: value}}

    def __init__(self, insts, cands: dict) -> None:
        super().__init__()
        self._insts = insts
        self._cands = cands
        self._running = True

    def run(self) -> None:
        while self._running:
            snap: dict[int, dict[int, int]] = {}
            for pid, _title, sc in self._insts:
                if not self._running:
                    return
                addrs = self._cands.get(pid, [])
                try:
                    snap[pid] = {a: sc.read_value(a, SIG.vt) for a in addrs}
                except Exception:
                    snap[pid] = {}
            if not self._running:
                return
            self.snapshot.emit(snap)
            self.msleep(READ_INTERVAL_MS)

    def stop(self) -> None:
        self._running = False


def _dur_text(secs: int) -> str:
    """把秒數講成人話：300 → 「5 分鐘」；330 → 「5 分 30 秒」；45 → 「45 秒」。"""
    if secs < 60:
        return f"{secs} 秒"
    m, s = divmod(secs, 60)
    return f"{m} 分鐘" if s == 0 else f"{m} 分 {s} 秒"


ALARM_MP3 = "music/Alarm_music.mp3"


def _resource(rel: str) -> Path:
    """資源檔路徑：開發時相對專案根目錄；打包成 exe 後在 PyInstaller 解壓目錄。"""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / rel
    return Path(__file__).resolve().parents[2] / rel


class BeepThread(QThread):
    """後備方案：mp3 播不出來時，用循環嗶聲，直到 stop()。"""

    def __init__(self) -> None:
        super().__init__()
        self._on = True

    def run(self) -> None:
        try:
            import winsound
        except Exception:
            return
        while self._on:
            try:
                winsound.Beep(1000, 500)
            except Exception:
                self.msleep(500)
            self.msleep(250)

    def stop(self) -> None:
        self._on = False


class Alarm:
    """警報聲：優先『循環播放 music/Alarm_music.mp3』；播不出來則退回嗶聲。

    QMediaPlayer 必須在有事件迴圈的執行緒建立/使用，因此本類別由 UI 主執行緒持有
    與操作（警報是在 UI 執行緒觸發的）。
    """

    def __init__(self, parent=None) -> None:
        self._parent = parent
        self._player = None
        self._audio = None
        self._beep: BeepThread | None = None

    def start(self) -> None:
        if self._player is not None or (self._beep and self._beep.isRunning()):
            return  # 已經在響
        if not self._start_mp3():
            self._beep = BeepThread()
            self._beep.start()

    def _start_mp3(self) -> bool:
        path = _resource(ALARM_MP3)
        if not path.exists():
            return False
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            self._audio = QAudioOutput()
            self._player = QMediaPlayer(self._parent)
            self._player.setAudioOutput(self._audio)
            self._player.setLoops(QMediaPlayer.Loops.Infinite)  # 循環播放到 stop()
            self._player.setSource(QUrl.fromLocalFile(str(path)))
            self._player.play()
            return True
        except Exception:
            self._player = None
            self._audio = None
            return False

    def stop(self) -> None:
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
            self._player = None
            self._audio = None
        if self._beep is not None:
            self._beep.stop()
            self._beep.wait(2000)
            self._beep = None


class AlarmDialog(QDialog):
    """警報視窗：標明哪些帳號經驗球滿了/停止，含「停止警報」按鈕。"""

    def __init__(self, parent, on_stop) -> None:
        super().__init__(parent)
        self.setWindowTitle("⚠ 技能經驗球警報")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(380, 180)
        lay = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setStyleSheet("font-size: 14px;")
        lay.addWidget(self.label)
        lay.addStretch(1)
        btn = QPushButton("停止警報")
        btn.setStyleSheet("font-size: 14px; padding: 8px;")
        btn.clicked.connect(on_stop)
        lay.addWidget(btn)

    def set_accounts(self, accounts, no_change_secs: int) -> None:
        body = "\n".join(f"　• {a}" for a in accounts)
        self.label.setText(
            "以下分身的技能經驗球滿了、或遊戲停止/斷線了\n"
            f"（數值持續 {_dur_text(no_change_secs)} 沒有再增加）：\n\n" + body
            + "\n\n畫面已凍結保留最後資料。處理完記得按『開始監控』重新偵測。"
        )


class MonitorTab(BaseTab):
    TAB_TITLE = "監控技能經驗球"
    ORDER = 5

    def build_ui(self) -> None:
        self._mons: dict[int, dict] = {}
        self._cands: dict[int, list] = {}   # {pid: [addr,...]}，ScanWorker 寫、ReadWorker 讀
        self._names: dict[int, str] = {}    # {pid: 角色名}，ScanWorker 解、UI 讀
        self._health: dict[int, float] = {}  # {pid: 上次增加的時間}，UI 寫、ScanWorker 讀
        self._scan_worker: ScanWorker | None = None
        self._read_worker: ReadWorker | None = None
        self._dying: list[QThread] = []       # 正在收尾的執行緒（見 _teardown）
        self._dying_scanners: list = []       # 等它們停好才能關的 handle
        self._alarm = Alarm(self)
        self._alarm_dialog: AlarmDialog | None = None
        self._alarm_accts: list[str] = []
        # 使用者手動關閉警報的帳號名（用帳號名而非 PID：PID 每次開遊戲會變、帳號名不會）。
        # 存進 config，重開工具 / 重新探索都自動保持關閉。停用只是「不警報」，仍照掃照顯示。
        self._disabled_accounts: set[str] = set(
            config.get("monitor.disabled_accounts", []) or [])

        root = QVBoxLayout(self)
        self.desc = QLabel()
        root.addWidget(self.desc)
        row = QHBoxLayout()
        self.start_btn = QPushButton("開始監控")
        self.start_btn.setProperty("primary", True)  # 主要動作 → 主色（見 app/theme.py）
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.rescan_btn = QPushButton("重新探索分身")
        self.rescan_btn.setEnabled(False)
        self.rescan_btn.clicked.connect(self.rescan)
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.rescan_btn)
        row.addStretch(1)
        root.addLayout(row)

        # 無變化門檻：數值連續這麼多秒沒增加 → 判定滿了/停止（存進設定，下次自動帶回）
        self._no_change_val = self._load_no_change_secs()
        # 單元素 list 當共享盒子：UI 改門檻後，背景 ScanWorker 下一輪就讀到新值。
        self._scan_quiet = [_rescan_quiet_secs(self._no_change_val)]
        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel("無變化多久算滿："))
        self.no_change_edit = QLineEdit(str(self._no_change_val))
        # 純數字輸入框：不放上下箭頭、不限步進，使用者想打多少就打多少。
        # 驗證下限故意開到負數 → 讓「-」打得進去，才有機會在打完時把負數強制成 0；
        # 若下限設 0，Qt 會直接吃掉減號按鍵，反而看不出被修正。
        self.no_change_edit.setValidator(
            QIntValidator(-MAX_NO_CHANGE_SECS, MAX_NO_CHANGE_SECS, self))
        self.no_change_edit.setMaximumWidth(80)
        self.no_change_edit.setToolTip(
            f"經驗球數值連續這麼多秒沒有再增加 → 判定滿了 / 遊戲停止 → 發警報。\n"
            f"預設 {DEFAULT_NO_CHANGE_SECS} 秒（{_dur_text(DEFAULT_NO_CHANGE_SECS)}）；"
            "負數會自動變成 0。監控中改也會立刻生效。"
        )
        self.no_change_edit.editingFinished.connect(self._on_no_change_changed)
        thr_row.addWidget(self.no_change_edit)
        thr_row.addWidget(QLabel("秒"))  # 單位放在框外面
        thr_row.addStretch(1)
        root.addLayout(thr_row)
        self._refresh_desc()

        # 通知方式：音效 or Telegram（選擇與房間 ID 都存進設定，下次自動帶回）
        notify_row = QHBoxLayout()
        notify_row.addWidget(QLabel("通知方式："))
        self.notify_sound_rb = QRadioButton("音效警報")
        self.notify_tg_rb = QRadioButton("Telegram 通知")
        self._notify_grp = QButtonGroup(self)
        self._notify_grp.addButton(self.notify_sound_rb)
        self._notify_grp.addButton(self.notify_tg_rb)
        notify_row.addWidget(self.notify_sound_rb)
        notify_row.addWidget(self.notify_tg_rb)
        notify_row.addWidget(QLabel("群組/房間 ID："))
        self.tg_id_edit = QLineEdit()
        self.tg_id_edit.setPlaceholderText("Telegram 群組/房間 ID")
        self.tg_id_edit.setMaximumWidth(220)
        notify_row.addWidget(self.tg_id_edit)
        notify_row.addStretch(1)
        root.addLayout(notify_row)
        # 從設定載入（先設好再接訊號，避免載入時誤觸存檔）
        self.tg_id_edit.setText(str(config.get("monitor.telegram_id", "")))
        if config.get("monitor.notify_method", "sound") == "telegram":
            self.notify_tg_rb.setChecked(True)
        else:
            self.notify_sound_rb.setChecked(True)
        self.tg_id_edit.setEnabled(self.notify_tg_rb.isChecked())
        self.notify_tg_rb.toggled.connect(self._on_notify_changed)
        self.tg_id_edit.editingFinished.connect(self._save_tg_id)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["監控", "角色", "帳號", "頻道", "PID", "技能經驗球", "狀態"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hdr = self.table.horizontalHeader()
        # 前六欄貼齊內容寬度，「狀態」欄吃掉剩餘空間 → 長狀態字不用手動拉就完整顯示。
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # 監控（勾選 = 會警報）
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)  # 角色
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)  # 帳號
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)  # 頻道
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # PID
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)  # 技能經驗球
        hdr.setSectionResizeMode(6, QHeaderView.Stretch)           # 狀態
        self.table.verticalHeader().setDefaultSectionSize(30)
        root.addWidget(self.table)

        self.status = QLabel("就緒")
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)

    # ------------------------------------------------------------------
    def _find_instances(self):
        insts, seen = [], set()
        for w in win.enumerate_windows(title_contains="Angels Online"):
            if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
                continue
            seen.add(w.pid)
            sc = MemoryScanner()
            try:
                sc.open(w.pid)
            except Exception:
                continue
            insts.append((w.pid, w.hwnd, w.title, sc))
        return insts

    def _stalled_entry(self, pid: int, m: dict) -> dict:
        """一台觸發警報時的顯示名 = 帳號 + 角色名（警告視窗與 Telegram 通知都用它）。"""
        acc = charname.account_from_title(m["title"])
        ch = self._names.get(pid)
        name = f"{acc}（{ch}）" if ch and ch != "?" else acc
        return {"name": name}

    # --- 無變化門檻（存進 config，下次自動帶回）--------------------------------
    @staticmethod
    def _load_no_change_secs() -> int:
        """讀設定檔的門檻秒數；壞值 / 超出範圍都回退成安全值，不讓 UI 崩。"""
        try:
            secs = int(config.get("monitor.no_change_secs", DEFAULT_NO_CHANGE_SECS))
        except (TypeError, ValueError):
            return DEFAULT_NO_CHANGE_SECS
        return max(MIN_NO_CHANGE_SECS, min(MAX_NO_CHANGE_SECS, secs))

    @property
    def _no_change_secs(self) -> int:
        """目前生效的門檻（秒）。用『已確認』的值而非輸入框當下文字——不然使用者
        才打到 300 的第一個 3，倒數就會用 3 秒去比、立刻誤報。"""
        return self._no_change_val

    def _on_no_change_changed(self) -> None:
        """打完（按 Enter / 移開焦點）才採用：空白或亂填就退回上一個好值。"""
        text = self.no_change_edit.text().strip()
        try:
            secs = int(text)
        except ValueError:
            secs = self._no_change_val
        secs = max(MIN_NO_CHANGE_SECS, min(MAX_NO_CHANGE_SECS, secs))
        if str(secs) != text:
            self.no_change_edit.setText(str(secs))  # 把修正後的值寫回框裡
        if secs == self._no_change_val:
            return
        self._no_change_val = secs
        self._scan_quiet[0] = _rescan_quiet_secs(secs)
        config.set("monitor.no_change_secs", secs)
        config.save()
        self._refresh_desc()

    def _refresh_desc(self) -> None:
        dur = _dur_text(self._no_change_secs)
        self.desc.setText(
            "即時監控每個分身正在練的技能經驗球。位址跑掉時會自動重掃跟上，"
            "換地圖 / 重連 / 升級都不會追丟。\n"
            f"某台「持續 {dur}沒再增加」（滿了或停止）→ 警報聲 + 跳視窗提示，並『凍結』"
            "畫面（像暫停）保留最後資料。\n"
            "「監控」欄取消勾選 → 該帳號只顯示數值、不倒數也不警報（設定會記住，下次自動帶回）。\n"
            "⚠ 處理完記得按「開始監控」重新偵測，畫面才會繼續更新。"
        )

    # --- 通知方式設定（存進 config，下次自動帶回）------------------------------
    def _on_notify_changed(self) -> None:
        tg = self.notify_tg_rb.isChecked()
        config.set("monitor.notify_method", "telegram" if tg else "sound")
        config.save()
        self.tg_id_edit.setEnabled(tg)

    def _save_tg_id(self) -> None:
        config.set("monitor.telegram_id", self.tg_id_edit.text().strip())
        config.save()

    # ------------------------------------------------------------------
    # 開始 / 停止 / 重新探索
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._stop_alarm()  # 重新開始前先關掉還在響的警報
        insts = self._find_instances()
        if not insts:
            QMessageBox.information(
                self, "找不到分身",
                "找不到天使之戀分身。請確認遊戲開著且已登入。\n"
                "若遊戲以系統管理員身分執行，請同樣以系統管理員身分執行本工具。",
            )
            return
        self._begin(insts)

    def rescan(self) -> None:
        """重新探索分身（納入新開的視窗 / 移除已關的），並重啟監控。"""
        self._teardown(wait=False)
        self._stop_alarm()
        insts = self._find_instances()
        if not insts:
            self._reset_ui("找不到分身")
            return
        self._begin(insts)

    def _begin(self, insts) -> None:
        self._names = {}   # 重新探索 → 重新解角色名（換角色也能更新）
        self._health = {}  # 重新探索 → 重新判斷誰需要重掃
        self._mons = {
            pid: {
                "hwnd": hwnd,        # 用來即時重讀標題 → 換頻道立刻反映
                "title": title, "sc": sc,
                "prev": {},          # 上一輪快照 {addr: value}
                "tracked": None,     # 目前跟隨（在練）的位址
                "value": None,       # 顯示值
                "last_inc": None,    # 上次偵測到「增加」的時間；None = 還沒動過 → 不倒數
            }
            for pid, hwnd, title, sc in insts
        }
        self._rebuild_table()
        insts = [(pid, m["title"], m["sc"]) for pid, m in self._mons.items()]
        self._cands = {pid: [] for pid, _t, _s in insts}
        # 慢執行緒按需重掃特徵 + 解角色名；快執行緒每 ~0.7s 讀值更新畫面。
        self._scan_worker = ScanWorker(
            insts, self._cands, self._names, self._health, self._scan_quiet)
        self._read_worker = ReadWorker(insts, self._cands)
        self._read_worker.snapshot.connect(self._on_snapshot)
        self._scan_worker.start()
        self._read_worker.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        self.status.setText(f"監控中：{len(self._mons)} 台（背景持續重掃特徵，自動追最新位址）")

    def _rebuild_table(self) -> None:
        self.table.setRowCount(len(self._mons))
        for r, (pid, m) in enumerate(self._mons.items()):
            title = m["title"]
            account = charname.account_from_title(title)
            self.table.setCellWidget(r, 0, self._make_monitor_check(account))
            self.table.setItem(r, 1, QTableWidgetItem(self._names.get(pid) or "…"))
            self.table.setItem(r, 2, QTableWidgetItem(account))
            self.table.setItem(r, 3, QTableWidgetItem(charname.channel_from_title(title) or "—"))
            self.table.setItem(r, 4, QTableWidgetItem(str(pid)))
            self.table.setItem(r, 5, QTableWidgetItem("…"))
            self.table.setItem(r, 6, QTableWidgetItem("定位中…"))

    def _make_monitor_check(self, account: str) -> QWidget:
        """建一列的「監控」勾選框：勾 = 會警報；取消 = 只顯示不警報。置中放進儲存格。

        用 cell widget（而非 checkable item），讓勾選訊號跟每輪的數值/狀態更新完全隔離，
        不會被 _on_snapshot 大量 setItem 誤觸。"""
        cb = QCheckBox()
        cb.setChecked(account not in self._disabled_accounts)
        cb.setToolTip("取消勾選：此帳號不倒數、不警報（仍即時顯示數值）")
        cb.toggled.connect(lambda on, acc=account: self._on_monitor_toggle(acc, on))
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignCenter)
        lay.addWidget(cb)
        return wrap

    def _on_monitor_toggle(self, account: str, monitored: bool) -> None:
        """勾選 = 監控（會警報）；取消 = 加入停用清單。即時生效（_on_snapshot 每輪讀清單），
        並存進 config 讓下次自動帶回。"""
        if monitored:
            self._disabled_accounts.discard(account)
        else:
            self._disabled_accounts.add(account)
        config.set("monitor.disabled_accounts", sorted(self._disabled_accounts))
        config.save()

    # ------------------------------------------------------------------
    # 每輪快照處理（在 UI 執行緒，由背景 worker 的訊號觸發）
    # ------------------------------------------------------------------
    def _on_snapshot(self, snap: dict) -> None:
        now = time.monotonic()
        limit = self._no_change_secs  # 每輪讀一次 → 監控中調整門檻會立刻生效
        stalled = []
        for r, (pid, m) in enumerate(self._mons.items()):
            if r >= self.table.rowCount():
                break

            # 即時重讀視窗標題 → 換頻道 / 換帳號畫面立刻反映（切頻道時遊戲會改標題）。
            # GetWindowText 很便宜，每輪讀一次不影響效能；空字串代表視窗剛關掉，保留舊值。
            fresh = win.get_window_title(m["hwnd"])
            if fresh:
                m["title"] = fresh
            account = charname.account_from_title(m["title"])
            self._update_text(r, 2, account)
            self._update_text(r, 3, charname.channel_from_title(m["title"]) or "—")

            # 角色名由 ScanWorker 背景解出，解到後補上「角色」欄
            nm = self._names.get(pid)
            if nm and nm != "?":
                self._update_text(r, 1, nm)

            cur = snap.get(pid)
            if cur is None:
                continue  # 這輪沒有這台的資料

            prev = m["prev"]
            # 這輪相對上一輪「有增加」的位址們（cur 為空→掃不到特徵→自然沒有任何增加）
            increased = {
                a: v - prev[a]
                for a, v in cur.items()
                if v is not None and prev.get(a) is not None and v > prev[a]
            }
            if increased:
                tracked = m["tracked"]
                if tracked not in increased:
                    # 換一顆在動的（首次鎖定 / 換地圖搬家 / 切技能）→ 改跟增加最多的那顆
                    m["tracked"] = max(increased, key=increased.get)
                m["last_inc"] = now
                # 告訴 ScanWorker「這台還活著、位址是對的」→ 它就不用重掃這台。
                self._health[pid] = now

            # 更新顯示值 = 跟隨那顆的現值
            tr = m["tracked"]
            if tr is not None and cur.get(tr) is not None:
                m["value"] = cur[tr]

            # 已關閉警報的帳號：仍照掃、照顯示數值，但完全跳過倒數 / 警報判定，
            # 也不會被算進「任一台滿了就整體停掉」，不會拖累還在監控的其他分身。
            if account in self._disabled_accounts:
                self._set_row(r, m["value"], "🔕 已關閉警報（僅顯示數值）")
                m["prev"] = cur
                continue

            # 「突然掃不到特徵」通常代表斷線／遊戲關了，等同「沒有球在動」，照樣倒數。
            lost = not cur
            if m["last_inc"] is None:
                # 還沒鎖定過 → 不倒數（先有變動才算 5 分鐘）。
                self._set_row(r, m["value"],
                              "定位中…（掃不到特徵）" if lost else "等待變動以鎖定技能…")
            else:
                quiet = now - m["last_inc"]
                if quiet >= limit:
                    stalled.append(self._stalled_entry(pid, m))
                    self._set_row(r, m["value"],
                                  "⚠ 掃不到特徵（可能斷線）" if lost else "⚠ 已滿/停止")
                elif lost:
                    self._set_row(r, m["value"], f"掃不到特徵…（{int(quiet)}s，可能斷線）")
                else:
                    self._set_row(r, m["value"], f"監控中（{int(quiet)}s 未增加）")
            m["prev"] = cur

        if stalled:
            # 任何一台觸發 → 響警報 + 整個監控停掉（等同按停止），要自己重按「開始監控」。
            self._alarm_and_stop(stalled)

    def _set_row(self, r: int, value, status: str) -> None:
        self.table.setItem(r, 5, QTableWidgetItem(
            "—" if value is None else str(value)))
        self.table.setItem(r, 6, QTableWidgetItem(status))

    def _update_text(self, r: int, c: int, text: str) -> None:
        """僅在文字有變動時才換 item，避免每輪都重建（減少閃爍 / 無謂 CPU）。"""
        item = self.table.item(r, c)
        if item is None or item.text() != text:
            self.table.setItem(r, c, QTableWidgetItem(text))

    # ------------------------------------------------------------------
    # 警報
    # ------------------------------------------------------------------
    def _alarm_and_stop(self, stalled) -> None:
        for s in stalled:
            if s["name"] not in self._alarm_accts:
                self._alarm_accts.append(s["name"])
        # 依使用者選的通知方式：Telegram → 送通知、不放音樂；否則 → 音效警報。
        if self.notify_tg_rb.isChecked():
            note = self._send_telegram(stalled)
        else:
            self._alarm.start()
            note = ""
        # 兩種方式都還是跳警告視窗
        if self._alarm_dialog is None:
            self._alarm_dialog = AlarmDialog(self, self._stop_alarm)
        self._alarm_dialog.set_accounts(self._alarm_accts, self._no_change_secs)
        self._alarm_dialog.show()
        self._alarm_dialog.raise_()
        self._alarm_dialog.activateWindow()
        # 停止背景更新，但『凍結』畫面（保留最後資料，像按暫停）；警報聲繼續響到按「停止警報」。
        # 這裡是 UI 執行緒：絕不能 wait() 等背景掃描結束（一輪全掃要好幾秒 → 警報視窗
        # 一跳出來整個介面就凍住）。改成送出停止訊號就走，背景結束後再自己收 handle。
        self._teardown(wait=False)
        msg = "⚠ 已凍結（觸發警報）— 處理完記得按『開始監控』重新偵測，畫面才會繼續更新"
        self._reset_ui(msg + (f"　｜　{note}" if note else ""), clear_table=False)

    def _send_telegram(self, stalled) -> str:
        """背景送 Telegram 通知（每台一則，name=帳號+角色名）。回傳給狀態列的短訊。"""
        room_id = self.tg_id_edit.text().strip()
        if not room_id:
            return "⚠ 未填 Telegram 群組/房間 ID，通知未送出"
        dur = _dur_text(self._no_change_secs)
        content = f"⚠ 技能經驗球已滿或停止（連續 {dur} 沒再增加），請處理。"
        names = [s["name"] for s in stalled]

        def worker():
            for nm in names:
                ok, info = notify.send_telegram(room_id, nm, content)
                if not ok:
                    sys.stderr.write(f"[telegram] 送出失敗 {nm}: {info}\n")

        threading.Thread(target=worker, daemon=True).start()
        return f"已送出 Telegram 通知（{len(names)} 則）"

    # ------------------------------------------------------------------
    # 生命週期
    # ------------------------------------------------------------------
    def _teardown(self, wait: bool) -> None:
        """停掉背景執行緒，並在它們真的結束後才關掉程序 handle。

        關 handle 一定要等執行緒停好——背景還在 ReadProcessMemory 時把 handle 抽掉會出事。
        但「等」不能發生在 UI 執行緒上（會凍住畫面），所以分兩種：
          wait=True  擋著等（只給關閉程式用，反正要走了）。
          wait=False 立刻返回，改用計時器輪詢；執行緒收乾淨後才關 handle。
        """
        threads = [t for t in (self._read_worker, self._scan_worker) if t is not None]
        if self._read_worker is not None:
            try:
                self._read_worker.snapshot.disconnect(self._on_snapshot)
            except Exception:
                pass
        for t in threads:
            t.stop()
        self._read_worker = None
        self._scan_worker = None
        # 收尾佇列：留著 QThread 參考（別被 GC）與對應的 handle（結束後才能關）。
        self._dying += threads
        self._dying_scanners += [m["sc"] for m in self._mons.values()]

        if wait:
            for t in self._dying:
                t.wait(5000)  # aob.scan 有中止點，正常幾毫秒就收掉
            self._finish_teardown()
            return

        def check() -> None:
            if any(t.isRunning() for t in self._dying):
                QTimer.singleShot(100, check)
                return
            self._finish_teardown()
        QTimer.singleShot(0, check)

    def _finish_teardown(self) -> None:
        """背景執行緒都停好了 → 這時關 handle 才安全。"""
        for sc in self._dying_scanners:
            try:
                sc.close()
            except Exception:
                pass
        self._dying = []
        self._dying_scanners = []


    def _reset_ui(self, status: str, clear_table: bool = True) -> None:
        self._mons = {}
        if clear_table:
            self.table.setRowCount(0)
        # clear_table=False → 保留表格最後資料（凍結/暫停顯示），內部狀態仍重置。
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.rescan_btn.setEnabled(False)
        self.status.setText(status)

    def _stop_alarm(self) -> None:
        self._alarm.stop()
        if self._alarm_dialog:
            self._alarm_dialog.hide()
        self._alarm_accts = []

    def stop(self) -> None:
        self._stop_alarm()
        self._teardown(wait=False)
        self._reset_ui("已停止")

    def on_close(self) -> None:
        self._stop_alarm()
        self._teardown(wait=True)  # 程式要關了 → 擋著等，確保 handle 有收乾淨
