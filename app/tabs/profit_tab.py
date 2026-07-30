"""收益監控分頁：每個分身一張卡片，看掛機效率。

一台一張卡片，除了現況還算**速率**——經驗/時、金幣/時、還要多久升等、每顆技能球
還要多久滿。排版參考使用者提供的競品截圖。

速率是怎麼算的
--------------
按「開始計算」之後才開始累計。每輪快照跟上一輪比，只累加「有增加」的差值：
  * 經驗升等時會歸零再往上跳，所以直接拿頭尾相減會算錯 → 只累加正的差值。
  * 換地圖 / 重連讓位址搬家時，值可能瞬間跳掉 → 同樣只認正差值，且忽略異常大的跳動。
時薪 = 累計增量 / 已經過的時間。樣本太少（不到 30 秒）時不顯示，免得數字亂跳。

掃描與判斷都在 app/game/watcher.py，這裡只負責畫。
全程只讀取記憶體、不寫入、不注入、不掛除錯器（安全）。
"""
from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import config
from app.core import charname, netstat, notify
from app.core import window as win
from app.core.alarm import Alarm, AlarmDialog
from app.core.memory import MemoryScanner
from app.game import items
from app.game.watcher import StatsWorker
from app.tabs.base_tab import BaseTab

TEXT = "#e6e8ef"
TEXT_MUT = "#9aa2b8"
ACCENT = "#6c8cff"
SUCCESS = "#33c17f"
DANGER = "#ff6b6b"
GOLD = "#e0a458"
PANEL = "#262b3b"
BORDER = "#3a4056"

# 經驗球進度條的寬度（px）。條子本身不需要很長，右邊的數值與滿倉預估才是重點。
BAR_WIDTH = 170
# 累計不到這麼久就先不顯示時薪（樣本太少時數字會亂跳得很難看）
MIN_RATE_SECS = 30.0
# 單輪增量超過這個數就當成位址搬家造成的假跳動，不計入
MAX_SANE_STEP = 50_000_000

# ---------------------------------------------------------------------------
# 警報：三種監控
#   球滿   —— 球的數值達到上限。直接比對數值，不用計時猜。
#   死亡   —— HP 掉到 0。要偵測得到，player.read() 必須容許 HP=0（見 player.py）。
#   斷線   —— 遊戲視窗不見了，或那台跟伺服器的 TCP 連線斷了。兩種都算斷線。
#
# ★ 斷線為什麼要看 TCP 而不是看記憶體：實測把一台分身的網路切掉之後，**視窗還在、
#   玩家物件照樣讀得到，只是所有數值凍住**（HP、經驗、球全部不動），解除封鎖後遊戲
#   也不會自己重連。所以「讀不到記憶體」根本不會發生，拿它當斷線訊號等於永不觸發。
#   真正會變的只有 TCP 連線消失，見 app/core/netstat.py。
#
# 每個 (分身, 原因) 只通知一次；情況恢復後會自動重新武裝，下次再發生會再通知。
# ---------------------------------------------------------------------------
R_BALL_FULL = "ball_full"
R_DEATH = "death"
R_DISCONNECT = "disconnect"

# 連線消失多久後才算斷線。這不是給使用者調的 ——「斷多久才算斷」對使用者沒有意義，
# 斷了就是斷了。留這幾秒純粹是為了吸收切換頻道之類的瞬間重連（不同頻道連的遠端埠
# 不一樣，理論上會先關舊連線再開新的），以及查詢本身的抖動。
# ⚠ 「切頻道會不會真的短暫斷線」尚未實測，這個值是保守估計。
DISCONNECT_GRACE_SECS = 15.0
# TCP 連線表的快取秒數：一輪要問五台，沒必要每台都去掃一次整張表。
CONN_CACHE_SECS = 1.5


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def wan(n: float) -> str:
    """把大數字講成人話：1234567 → 「123.5萬」；小數字就原樣。"""
    if abs(n) >= 100_000_000:
        return f"{n / 100_000_000:.2f}億"
    if abs(n) >= 10_000:
        return f"{n / 10_000:.1f}萬"
    return f"{n:,.0f}"


def hms(secs: float) -> str:
    """經過時間 → 01:12:30"""
    s = max(0, int(secs))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def eta(secs: float | None) -> str:
    """預估剩餘 → 「2時58分」/「45分」；算不出來回傳「—」。"""
    if secs is None or secs <= 0 or secs > 86400 * 30:
        return "—"
    s = int(secs)
    if s >= 3600:
        return f"{s // 3600}時{s % 3600 // 60}分"
    if s >= 60:
        return f"{s // 60}分"
    return f"{s}秒"


class Session:
    """一台分身的累計狀態（按下「開始計算」後才動）。"""

    def __init__(self) -> None:
        self.started: float | None = None
        self.exp_gain = 0
        self.gold_gain = 0
        self.prev_exp: int | None = None
        self.prev_gold: int | None = None
        self.ball_gain: dict[int, int] = {}     # {球位址: 累計增量}
        self.ball_prev: dict[int, int] = {}
        self.deaths = 0
        self.last_death: float | None = None    # 牆上時鐘（顯示「最近幾點死的」）
        self.prev_hp: int | None = None

    def start(self) -> None:
        self.__init__()
        self.started = time.monotonic()

    @property
    def running(self) -> bool:
        return self.started is not None

    @property
    def elapsed(self) -> float:
        return (time.monotonic() - self.started) if self.started else 0.0

    def _bump(self, prev: int | None, cur: int) -> int:
        """只認「有增加且幅度合理」的差值——升等歸零、位址搬家都不該算進時薪。"""
        if prev is None or cur <= prev:
            return 0
        d = cur - prev
        return d if d <= MAX_SANE_STEP else 0

    def feed(self, stats, balls: list[dict]) -> None:
        if not self.running:
            return
        if stats is not None:
            self.exp_gain += self._bump(self.prev_exp, stats.exp)
            self.gold_gain += self._bump(self.prev_gold, stats.gold)
            self.prev_exp, self.prev_gold = stats.exp, stats.gold
            # 死亡次數：算「活著→死了」的轉折，不是「現在是不是死的」——
            # 後者會在角色躺著的每一輪都加一次。第一次就讀到死的也算一次。
            if stats.hp <= 0 and (self.prev_hp is None or self.prev_hp > 0):
                self.deaths += 1
                self.last_death = time.time()
            self.prev_hp = stats.hp
        for b in balls:
            a = b["addr"]
            self.ball_gain[a] = self.ball_gain.get(a, 0) + \
                self._bump(self.ball_prev.get(a), b["value"])
            self.ball_prev[a] = b["value"]

    def rate(self, gained: int) -> float | None:
        """每小時速率；樣本不足回傳 None。"""
        if not self.running or self.elapsed < MIN_RATE_SECS:
            return None
        return gained / self.elapsed * 3600.0

    def ball_rate(self, addr: int) -> float | None:
        return self.rate(self.ball_gain.get(addr, 0))


# ---------------------------------------------------------------------------
# 單一角色的卡片
# ---------------------------------------------------------------------------
class CharCard(QFrame):
    def __init__(self, index: int, pid: int, title: str,
                 monitored: bool, on_monitor_toggle) -> None:
        super().__init__()
        self.pid = pid
        self.account = charname.account_from_title(title)
        self.session = Session()
        self._bars: list[tuple[QLabel, QProgressBar, QLabel]] = []

        self.setFrameShape(QFrame.StyledPanel)
        # 一定要用 objectName 限定：QLabel / QProgressBar 都是 QFrame 的子類別，
        # 直接寫 "QFrame { ... }" 會連卡片裡每一個標籤都套上外框，畫面會變成一堆小方塊。
        self.setObjectName("charCard")
        self.setStyleSheet(
            f"QFrame#charCard {{ background: {PANEL}; border: 1px solid {BORDER};"
            f" border-radius: 6px; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        head = QHBoxLayout()
        self.head_lbl = QLabel(f"角色 {index} · PID {pid} · {self.account}")
        self.head_lbl.setStyleSheet(f"color: {TEXT_MUT};")
        # 取消勾選 = 這個帳號不發警報（數值照樣顯示）。用帳號名而不是 PID，
        # 因為 PID 每次開遊戲都會變、帳號不會。
        self.watch_cb = QCheckBox("監控")
        self.watch_cb.setChecked(monitored)
        self.watch_cb.setToolTip("取消勾選：此帳號不發警報（數值仍即時顯示）")
        self.watch_cb.toggled.connect(
            lambda on: on_monitor_toggle(self.account, on))
        self.locate_lbl = QLabel("定位中…")
        self.locate_lbl.setStyleSheet(f"color: {TEXT_MUT};")
        head.addWidget(self.head_lbl)
        head.addSpacing(10)
        head.addWidget(self.watch_cb)
        head.addStretch(1)
        head.addWidget(self.locate_lbl)
        root.addLayout(head)

        # 主要數值列
        top = QHBoxLayout()
        self.name_lbl = QLabel("…")
        self.name_lbl.setStyleSheet(
            f"color: {ACCENT}; font-size: 16px; font-weight: bold;")
        self.level_lbl = QLabel("")
        self.level_lbl.setStyleSheet(f"color: {ACCENT}; font-weight: bold;")
        self.exp_lbl = QLabel("")
        self.exp_lbl.setStyleSheet(f"color: {TEXT}; font-weight: bold;")
        top.addWidget(self.name_lbl)
        top.addSpacing(10)
        top.addWidget(self.level_lbl)
        top.addSpacing(10)
        top.addWidget(self.exp_lbl)
        top.addStretch(1)
        self.chan_lbl = QLabel("")
        self.chan_lbl.setStyleSheet(f"color: {TEXT_MUT};")
        top.addWidget(self.chan_lbl)
        root.addLayout(top)

        self.gold_lbl = QLabel("")
        self.gold_lbl.setStyleSheet(f"color: {GOLD}; font-weight: bold;")
        root.addWidget(self.gold_lbl)

        vit = QHBoxLayout()
        self.hp_lbl = QLabel("")
        self.mp_lbl = QLabel("")
        for w in (self.hp_lbl, self.mp_lbl):
            w.setStyleSheet(f"color: {TEXT};")
        vit.addWidget(self.hp_lbl)
        vit.addSpacing(16)
        vit.addWidget(self.mp_lbl)
        vit.addStretch(1)
        root.addLayout(vit)

        # 計時 / 速率
        btns = QHBoxLayout()
        self.start_btn = QPushButton("開始計算")
        self.start_btn.clicked.connect(self._toggle)
        self.reset_btn = QPushButton("重設")
        self.reset_btn.clicked.connect(self._reset)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.reset_btn)
        btns.addStretch(1)
        root.addLayout(btns)

        # 時薪那一行：時間 / 經驗 / 金幣，最後接死亡次數（紅字，另一個 label 才能上色）
        rate_row = QHBoxLayout()
        rate_row.setSpacing(12)
        self.rate_lbl = QLabel("尚未開始計算")
        self.rate_lbl.setStyleSheet(f"color: {SUCCESS};")
        self.death_lbl = QLabel("")
        rate_row.addWidget(self.rate_lbl)
        rate_row.addWidget(self.death_lbl)
        rate_row.addStretch(1)
        root.addLayout(rate_row)

        # 累計：從按下「開始計算」到現在總共賺了多少
        self.total_lbl = QLabel("")
        self.total_lbl.setStyleSheet(f"color: {TEXT};")
        self.total_lbl.setToolTip(
            "從按下「開始計算」到現在的累計。\n"
            "經驗後面的百分比 = 這段時間賺到的經驗相當於本級所需經驗的幾成。")
        root.addWidget(self.total_lbl)
        self.eta_lbl = QLabel("")
        self.eta_lbl.setStyleSheet(f"color: {TEXT_MUT};")
        root.addWidget(self.eta_lbl)

        # 經驗球區
        self.ball_title = QLabel("● 經驗球")
        self.ball_title.setStyleSheet(f"color: {ACCENT};")
        root.addWidget(self.ball_title)
        self.ball_grid = QGridLayout()
        self.ball_grid.setContentsMargins(4, 0, 4, 0)
        self.ball_grid.setHorizontalSpacing(10)
        root.addLayout(self.ball_grid)
        self.bag_lbl = QLabel("")
        self.bag_lbl.setStyleSheet(f"color: {TEXT_MUT};")
        root.addWidget(self.bag_lbl)

    # -- 按鈕 ----------------------------------------------------------
    def _toggle(self) -> None:
        if self.session.running:
            self.session = Session()
            self.start_btn.setText("開始計算")
            self.rate_lbl.setText("尚未開始計算")
            self.eta_lbl.setText("")
            self.death_lbl.setText("")
            self.total_lbl.setText("")
        else:
            self.session.start()
            self.start_btn.setText("停止計算")

    def _reset(self) -> None:
        if self.session.running:
            self.session.start()
        self.rate_lbl.setText("已重設，重新累計中…")
        self.eta_lbl.setText("")

    # -- 每輪更新 ------------------------------------------------------
    def update_data(self, title: str, stats, ball) -> None:
        self.chan_lbl.setText(charname.channel_from_title(title) or "")
        balls = ball["balls"] if ball else []
        self.session.feed(stats, balls)

        if stats is None:
            self.locate_lbl.setText("定位中…")
            self.locate_lbl.setStyleSheet(f"color: {TEXT_MUT};")
        else:
            if stats.hp <= 0:
                self.locate_lbl.setText("● 死亡")
                self.locate_lbl.setStyleSheet(
                    f"color: {DANGER}; font-weight: bold;")
            else:
                # 用「●」而不是「✓」：勾號在部分中文字型裡沒有字形，會畫成豆腐方塊
                self.locate_lbl.setText("● 已定位")
                self.locate_lbl.setStyleSheet(f"color: {SUCCESS};")
            self.level_lbl.setText(f"Lv{stats.level}")
            # 當前經驗 / 升到下一級所需的經驗（都是絕對值，跟遊戲畫面同一套數字）
            self.exp_lbl.setText(
                f"經驗 {stats.exp:,} / {stats.exp_hi:,}"
                f"（{stats.exp_pct:.2f}%）")
            self.gold_lbl.setText(f"金幣 {stats.gold:,}")
            low = stats.max_hp > 0 and stats.hp / stats.max_hp < 0.3
            self.hp_lbl.setText(f"HP {stats.hp:,}/{stats.max_hp:,}")
            self.hp_lbl.setStyleSheet(
                f"color: {DANGER if low else TEXT};"
                + (" font-weight: bold;" if stats.hp <= 0 else ""))
            self.mp_lbl.setText(f"MP {stats.mp:,}/{stats.max_mp:,}")

        self._update_rates(stats)
        self._update_balls(ball)

    def set_name(self, name: str) -> None:
        self.name_lbl.setText(name)

    def _update_rates(self, stats) -> None:
        s = self.session
        if not s.running:
            return
        exp_h = s.rate(s.exp_gain)
        gold_h = s.rate(s.gold_gain)
        # 注意判 None 而不是判真假：沒在練功時速率是 0，那是有效結果，
        # 該顯示「0/時」讓使用者知道停了，不能跟「樣本還不夠」混為一談。
        parts = [hms(s.elapsed)]
        parts.append(f"經驗 {wan(exp_h)}/時" if exp_h is not None else "經驗 統計中…")
        parts.append(f"金幣 {wan(gold_h)}/時" if gold_h is not None else "金幣 統計中…")
        self.rate_lbl.setText("　".join(parts))

        if s.deaths:
            when = time.strftime("%H:%M:%S", time.localtime(s.last_death)) \
                if s.last_death else ""
            self.death_lbl.setText(
                f"死亡 {s.deaths} 次" + (f"（最近 {when}）" if when else ""))
            self.death_lbl.setStyleSheet(f"color: {DANGER}; font-weight: bold;")
        else:
            self.death_lbl.setText("死亡 0 次")
            self.death_lbl.setStyleSheet(f"color: {TEXT_MUT};")

        # 累計：總共賺了多少。經驗的百分比用「本級所需經驗」當分母 —— 講「賺了
        # 半級」比講「賺了 300 萬」直觀得多。升級會讓分母換成新一級的，屬正常。
        span = (stats.exp_hi - stats.exp_lo) if stats is not None else 0
        exp_txt = f"經驗 +{s.exp_gain:,}"
        if span > 0:
            exp_txt += f"（+{s.exp_gain / span * 100:.1f}%）"
        self.total_lbl.setText(f"累計　{exp_txt}　金幣 +{s.gold_gain:,}")

        if stats is None or not exp_h:
            self.eta_lbl.setText("")
            return
        remain = max(0, stats.exp_hi - stats.exp)
        self.eta_lbl.setText(
            f"升等剩 {wan(remain)} 經驗　預估 {eta(remain / exp_h * 3600)}")

    def _update_balls(self, ball) -> None:
        balls = ball["balls"] if ball else []
        while len(self._bars) < len(balls):      # 需要幾條就長幾條，之後重複使用
            name = QLabel()
            name.setStyleSheet(f"color: {TEXT_MUT};")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            # 固定寬度：條子拉到跟卡片一樣寬會把右邊的數字擠到很遠，讀起來反而吃力
            bar.setFixedSize(BAR_WIDTH, 14)
            bar.setStyleSheet(
                f"QProgressBar {{ background: #1b1f2b; border: 1px solid {BORDER};"
                f" border-radius: 3px; }}"
                f"QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}")
            info = QLabel()
            info.setStyleSheet(f"color: {TEXT};")
            r = len(self._bars)
            self.ball_grid.addWidget(name, r, 0)
            self.ball_grid.addWidget(bar, r, 1)
            self.ball_grid.addWidget(info, r, 2)
            self.ball_grid.setColumnStretch(2, 1)   # 讓右邊的數值欄吃掉剩餘空間
            self._bars.append((name, bar, info))

        for i, (name, bar, info) in enumerate(self._bars):
            show = i < len(balls)
            name.setVisible(show)
            bar.setVisible(show)
            info.setVisible(show)
            if not show:
                continue
            b = balls[i]
            # 飾品欄有左右兩格，標出來才知道要動哪一顆（只有精確路徑拿得到格號）
            side = b.get("side") or ""
            name.setText(f"［{side}］{b['name']}" if side else b["name"])
            if not b["is_ball"]:
                # 對照表沒有、也問不出名字的飾品：只標「非經驗球」，
                # 進度條與百分比都不畫（畫了只會是騙人的數字）。
                bar.setVisible(False)
                info.setText("")
                where = f"飾品欄{side}" if side else "這個位置"
                tip = (f"{where}裝的東西不在對照表裡（種類 ID {b['type_id']}，"
                       f"目前值 {b['value']:,}）。\n"
                       "把這個 ID 補進 app/game/items.py 就能正常顯示名稱與上限。")
                name.setToolTip(tip)
                info.setToolTip(tip)
                continue
            name.setToolTip("")
            info.setToolTip("")
            pct = b["pct"]
            bar.setVisible(pct is not None)      # 沒有上限就沒有進度可畫
            bar.setValue(int(pct) if pct is not None else 0)
            txt = f"{b['value']:,}" + (f" / {b['cap']:,}" if b["cap"] else "")
            if pct is not None:
                txt += f"　{pct:.1f}%"
            rate = self.session.ball_rate(b["addr"])
            if b["cap"] and rate:
                txt += f"　滿約 {eta((b['cap'] - b['value']) / rate * 3600)}"
            info.setText(txt)

        # 球的資料只有一個來源：直接讀物品陣列的飾品欄兩格。沒有「僅供參考」的模式。
        self.ball_title.setText("● 經驗球（飾品欄）")
        self.ball_title.setStyleSheet(f"color: {ACCENT};")

        idle = ball.get("idle") if ball else None
        if idle:
            unfull = sum(1 for tid, v in idle
                         if (bt := items.ball_type(tid)) and v < bt.cap)
            self.bag_lbl.setText(f"背包經驗球 {len(idle)} 顆　未滿 {unfull} 顆")
            self.bag_lbl.setToolTip("飾品欄以外、物品欄裡的球。")
        else:
            self.bag_lbl.setText("")


# ---------------------------------------------------------------------------
class ProfitTab(BaseTab):
    TAB_TITLE = "收益監控"
    ORDER = 4   # 產品主畫面，排最前面

    def build_ui(self) -> None:
        self._mons: dict[int, dict] = {}
        self._cards: dict[int, CharCard] = {}
        self._names: dict[int, str] = {}
        self._worker: StatsWorker | None = None
        self._dying: list[QThread] = []
        self._dying_scanners: list = []
        # 警報狀態
        self._alarm = Alarm(self)
        self._dialog: AlarmDialog | None = None
        self._fired: dict[int, set[str]] = {}    # {pid: 目前成立的警報原因}
        self._pending: list[tuple[str, str]] = []  # 累積要顯示的 [(顯示名, 說明)]
        self._lost_since: dict[int, float] = {}   # {pid: 從何時開始沒有連線}
        self._conn_cache: tuple[float, set] = (0.0, set())
        self._disabled: set[str] = set(
            config.get("profit.disabled_accounts", []) or [])

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.start_btn = QPushButton("開始監控")
        self.start_btn.setProperty("primary", True)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.rescan_btn = QPushButton("重新整理實例")
        self.rescan_btn.setEnabled(False)
        self.rescan_btn.clicked.connect(self.rescan)
        self.found_lbl = QLabel("尚未偵測")
        self.found_lbl.setStyleSheet(f"color: {TEXT_MUT};")
        bar.addWidget(self.start_btn)
        bar.addWidget(self.stop_btn)
        bar.addWidget(self.rescan_btn)
        bar.addSpacing(12)
        bar.addWidget(self.found_lbl)
        bar.addStretch(1)
        root.addLayout(bar)

        hint = QLabel(
            "每台一張卡片。按各卡的「開始計算」後開始統計經驗/時、金幣/時、升等預估，"
            "以及每顆技能球還要多久滿。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {TEXT_MUT};")
        root.addWidget(hint)

        self._build_notify_ui(root)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        self.cards_box = QVBoxLayout(holder)
        self.cards_box.setContentsMargins(0, 0, 0, 0)
        self.cards_box.setSpacing(10)
        self.cards_box.addStretch(1)
        self.scroll.setWidget(holder)
        root.addWidget(self.scroll, 1)

        self.status = QLabel("就緒")
        self.status.setStyleSheet(f"color: {TEXT_MUT};")
        root.addWidget(self.status)

    # ------------------------------------------------------------------
    # 通知設定（原本在「監控技能經驗球」分頁，搬過來並加上死亡／斷線監控）
    # ------------------------------------------------------------------
    def _build_notify_ui(self, root) -> None:
        # 監控項目
        watch = QHBoxLayout()
        watch.addWidget(QLabel("監控項目："))
        self.cb_ball = QCheckBox("技能球滿")
        self.cb_ball.setToolTip(
            "球的數值達到上限就通知（直接比對數值，不用計時猜）。\n"
            "⚠ 上限不在對照表裡的球沒辦法判斷滿不滿，不會通知。")
        self.cb_death = QCheckBox("死亡監控")
        self.cb_death.setToolTip("角色 HP 掉到 0 就通知。復活後再死會再通知一次。")
        self.cb_disc = QCheckBox("斷線監控")
        self.cb_disc.setToolTip(
            "兩種都算斷線：\n"
            "  • 遊戲視窗消失（被關閉或崩潰）→ 立刻通知\n"
            f"  • 跟伺服器的 TCP 連線斷掉 → 約 {int(DISCONNECT_GRACE_SECS)} 秒後通知\n\n"
            "斷線時視窗還在、記憶體也照樣讀得到，只是數值全部凍住，\n"
            "所以只能靠連線狀態判斷（實測過，見 app/core/netstat.py）。")
        for cb, key in ((self.cb_ball, "ball"), (self.cb_death, "death"),
                        (self.cb_disc, "disconnect")):
            cb.setChecked(bool(config.get(f"profit.watch_{key}", True)))
            cb.toggled.connect(lambda on, k=key: self._save("watch_" + k, on))
            watch.addWidget(cb)
        watch.addStretch(1)
        root.addLayout(watch)

        # 通知方式
        row = QHBoxLayout()
        row.addWidget(QLabel("通知方式："))
        self.rb_sound = QRadioButton("音效警報")
        self.rb_tg = QRadioButton("Telegram 通知")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_sound)
        grp.addButton(self.rb_tg)
        row.addWidget(self.rb_sound)
        row.addWidget(self.rb_tg)
        row.addWidget(QLabel("群組/房間 ID："))
        self.tg_id = QLineEdit()
        self.tg_id.setPlaceholderText("Telegram 群組/房間 ID")
        self.tg_id.setMaximumWidth(200)
        row.addWidget(self.tg_id)
        self.test_btn = QPushButton("測試通知")
        self.test_btn.clicked.connect(self._test_notify)
        row.addWidget(self.test_btn)
        row.addStretch(1)
        root.addLayout(row)

        # 先載入設定再接訊號，避免載入時誤觸存檔
        self.tg_id.setText(str(config.get("profit.telegram_id", "")))
        if config.get("profit.notify_method", "sound") == "telegram":
            self.rb_tg.setChecked(True)
        else:
            self.rb_sound.setChecked(True)
        self.tg_id.setEnabled(self.rb_tg.isChecked())
        self.rb_tg.toggled.connect(self._on_method_changed)
        self.tg_id.editingFinished.connect(
            lambda: self._save("telegram_id", self.tg_id.text().strip()))

    def _save(self, key: str, value) -> None:
        config.set(f"profit.{key}", value)
        config.save()

    def _on_method_changed(self, tg: bool) -> None:
        self.tg_id.setEnabled(tg)
        self._save("notify_method", "telegram" if tg else "sound")

    def _on_monitor_toggle(self, account: str, on: bool) -> None:
        if on:
            self._disabled.discard(account)
        else:
            self._disabled.add(account)
        self._save("disabled_accounts", sorted(self._disabled))

    def _test_notify(self) -> None:
        """讓使用者確認通知管道真的通，不必等到真的出事才發現沒設定好。"""
        if self.rb_tg.isChecked():
            room = self.tg_id.text().strip()
            if not room:
                QMessageBox.warning(self, "測試通知", "請先填入 Telegram 群組/房間 ID。")
                return
            self._send_telegram_async(
                [("測試", "這是一則測試通知，收到代表通知管道正常。")])
            self.status.setText("測試通知已送出（Telegram）")
        else:
            self._alarm.start()
            self._show_dialog([("測試", "這是一則測試警報，按「停止警報」即可關閉。")])
            self.status.setText("測試警報已觸發（音效）")

    # ------------------------------------------------------------------
    # 警報判定
    # ------------------------------------------------------------------
    def _connected(self, pid: int) -> bool:
        """這台還連著伺服器嗎？整張 TCP 表快取一下，一輪五台只查一次。"""
        now = time.monotonic()
        ts, pids = self._conn_cache
        if now - ts > CONN_CACHE_SECS:
            pids = netstat.established_pids()
            self._conn_cache = (now, pids)
        # 查不到任何連線通常代表 API 出錯，這時當作「還連著」——
        # 寧可漏報，也不要因為查詢失敗就對每一台狂發假斷線警報。
        return pid in pids if pids else True

    def _reasons(self, pid: int, m: dict, stats, ball) -> dict[str, str]:
        """回傳這台目前成立的警報原因 {原因: 說明文字}。

        說明文字會原封不動送進 Telegram 與警報視窗，所以要寫成人看得懂的句子。
        """
        out: dict[str, str] = {}
        now = time.monotonic()

        # -- 斷線：視窗不見了，或跟伺服器的連線斷了 --
        if not self._connected(pid):
            self._lost_since.setdefault(pid, now)
        else:
            self._lost_since.pop(pid, None)
        if self.cb_disc.isChecked():
            if not m.get("alive", True):
                out[R_DISCONNECT] = "遊戲視窗已消失（遊戲被關閉或崩潰）。"
            elif pid in self._lost_since:
                gone = now - self._lost_since[pid]
                if gone >= DISCONNECT_GRACE_SECS:
                    out[R_DISCONNECT] = (
                        "與伺服器的連線已中斷。遊戲視窗還在、畫面數值停在斷線那一刻，"
                        "但不會自己重連，要手動重登。")

        # -- 死亡：HP 掉到 0 就算一次死亡 --
        # 不要求先看到「活著」的那一幀 —— 剛接上就已經躺著的那台一樣要通知。
        # 復活（HP > 0）後這個原因會從 _fired 消失，下次再死會再通知一次。
        if self.cb_death.isChecked() and stats is not None and stats.hp <= 0:
            out[R_DEATH] = (
                f"角色死亡：HP 歸零（Lv{stats.level}，HP 0/{stats.max_hp:,}）。")

        # -- 技能球滿：直接比對數值，不用計時猜 --
        # ball 只會是「飾品欄兩格的實際內容」，所以不必再擔心誤把背包裡早就滿了的
        # 球當成裝備中的（那是舊推論路徑才有的問題）。
        if self.cb_ball.isChecked() and ball:
            full = [b for b in ball["balls"]
                    if b["is_ball"] and b["cap"] and b["value"] >= b["cap"]]
            if full:
                b = full[0]
                out[R_BALL_FULL] = (
                    f"技能經驗球已滿：{b['name']} {b['value']:,}/{b['cap']:,}"
                    f"（共 {len(full)} 顆滿了），請更換或收球。")
        return out

    def _check_alarms(self, pid: int, m: dict, stats, ball) -> None:
        card = self._cards.get(pid)
        if card is None or card.account in self._disabled:
            self._fired.pop(pid, None)
            return
        cur = self._reasons(pid, m, stats, ball)
        prev = self._fired.get(pid, set())
        # 只對「這次才成立」的原因發通知；情況恢復後會從 _fired 消失 → 自動重新武裝
        fresh = [r for r in cur if r not in prev]
        self._fired[pid] = set(cur)
        if not fresh:
            return
        who = f"{card.account}（{card.name_lbl.text()}）"
        events = [(who, cur[r]) for r in fresh]
        self._pending += events
        if self.rb_tg.isChecked():
            self._send_telegram_async(events)
            self.status.setText(f"已送出 Telegram 通知（{len(events)} 則）")
        else:
            self._alarm.start()
        self._show_dialog(self._pending)

    def _show_dialog(self, events) -> None:
        if self._dialog is None:
            self._dialog = AlarmDialog(self, self._stop_alarm, "⚠ 收益監控警報")
        body = "\n".join(f"　• {who} — {msg}" for who, msg in events)
        self._dialog.set_text(
            "以下分身觸發了警報：\n\n" + body
            + "\n\n按「停止警報」關閉聲音並清除這份清單；監控會繼續執行。"
        )
        self._dialog.popup()

    def _stop_alarm(self) -> None:
        self._alarm.stop()
        if self._dialog:
            self._dialog.hide()
        self._pending = []

    def _send_telegram_async(self, events) -> None:
        """背景送 Telegram（每則一次呼叫，name=帳號＋角色名，content=事件說明）。"""
        room = self.tg_id.text().strip()
        if not room:
            self.status.setText("⚠ 未填 Telegram 群組/房間 ID，通知未送出")
            return
        payload = list(events)

        def worker():
            for who, msg in payload:
                ok, info = notify.send_telegram(room, who, "⚠ " + msg)
                if not ok:
                    sys.stderr.write(f"[telegram] 送出失敗 {who}: {info}\n")

        threading.Thread(target=worker, daemon=True).start()

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

    def start(self) -> None:
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
        self._teardown(wait=False)
        insts = self._find_instances()
        if not insts:
            self._reset_ui("找不到分身")
            return
        self._begin(insts)

    def _begin(self, insts) -> None:
        self._clear_cards()
        self._names = {}
        self._mons = {pid: {"hwnd": hwnd, "title": title, "sc": sc}
                      for pid, hwnd, title, sc in insts}
        self._fired = {}
        self._lost_since = {}
        for i, (pid, m) in enumerate(self._mons.items(), start=1):
            acct = charname.account_from_title(m["title"])
            card = CharCard(i, pid, m["title"], acct not in self._disabled,
                            self._on_monitor_toggle)
            self._cards[pid] = card
            self.cards_box.insertWidget(self.cards_box.count() - 1, card)

        self._worker = StatsWorker(
            [(pid, m["hwnd"], m["title"], m["sc"]) for pid, m in self._mons.items()],
            self._names)
        self._worker.snapshot.connect(self._on_snapshot)
        self._worker.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        self.found_lbl.setText(f"偵測到 {len(self._mons)} 個遊戲視窗")
        self.status.setText(f"監控中：{len(self._mons)} 台")

    def _clear_cards(self) -> None:
        for card in self._cards.values():
            self.cards_box.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards = {}

    # ------------------------------------------------------------------
    def _on_snapshot(self, pid: int, stats, ball) -> None:
        card = self._cards.get(pid)
        if card is None:
            return
        m = self._mons[pid]
        # 標題讀不到 = 視窗沒了（遊戲被關掉／崩潰）→ 斷線監控要用這個訊號
        fresh = win.get_window_title(m["hwnd"])
        m["alive"] = bool(fresh)
        if fresh:
            m["title"] = fresh
        # 名字解不出來時（新角色還沒產生存檔檔案）先用帳號頂著，別讓那一格一直空白。
        # 背景會定期重試，存檔一出現就會自動換成真的角色名。
        nm = self._names.get(pid)
        card.set_name(nm if nm and nm != "?"
                      else charname.account_from_title(m["title"]))
        card.update_data(m["title"], stats, ball)
        self._check_alarms(pid, m, stats, ball)

    # ------------------------------------------------------------------
    # 生命週期（與角色總覽相同的收尾流程：先停執行緒，確認停好才關 handle）
    # ------------------------------------------------------------------
    def _teardown(self, wait: bool) -> None:
        threads = [t for t in (self._worker,) if t is not None]
        if self._worker is not None:
            try:
                self._worker.snapshot.disconnect(self._on_snapshot)
            except Exception:
                pass
        for t in threads:
            t.stop()
        self._worker = None
        self._dying += threads
        self._dying_scanners += [m["sc"] for m in self._mons.values()]

        if wait:
            for t in self._dying:
                t.wait(5000)
            self._finish_teardown()
            return

        def check() -> None:
            if any(t.isRunning() for t in self._dying):
                QTimer.singleShot(100, check)
                return
            self._finish_teardown()
        QTimer.singleShot(0, check)

    def _finish_teardown(self) -> None:
        for sc in self._dying_scanners:
            try:
                sc.close()
            except Exception:
                pass
        self._dying = []
        self._dying_scanners = []

    def _reset_ui(self, status: str) -> None:
        self._mons = {}
        self._clear_cards()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.rescan_btn.setEnabled(False)
        self.found_lbl.setText("尚未偵測")
        self.status.setText(status)

    def stop(self) -> None:
        self._stop_alarm()
        self._teardown(wait=False)
        self._reset_ui("已停止")

    def on_close(self) -> None:
        self._stop_alarm()
        self._teardown(wait=True)
