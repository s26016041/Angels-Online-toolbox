"""自動掛機分頁：列出附近怪物，選一隻，然後持續送攻擊按鍵。

運作原理（見 app/game/entity.py 的說明）
----------------------------------------
遊戲攻擊時會**自己重讀**「客戶端目前選定的目標」那個欄位，所以只要：
    ① 把目標的實體 ID 寫進狀態物件 +0x2D8
    ② 送一個攻擊按鍵（預設 F2，介面上可改成 F1～F12）給該視窗
角色就會打那隻怪。不必注入會執行的程式碼、不必自己組封包、不必搶視窗焦點
（背景視窗吃得到鍵盤訊息，吃不到滑鼠點擊 —— 所以選目標非走記憶體不可）。

打哪一隻：**離玩家最近的那一隻**。實體串列沒有順序，直接拿第一隻常常是天邊那隻
——先前「有時打得到、有時打不到」就是這個原因。現在怪和玩家都讀得到座標，
排序取最近的即可；而且遊戲的攻擊指令內建自動接近，選最近的只是為了少走路。

介面
----
每個分身一個子分頁（標題就是角色名）。各自可以掃描周圍怪物、選要打哪幾種，
勾「開始掛機」才會開始，取消勾選就停。

三件事各跑各的執行緒，互不干擾（這個拆法是使用者提的，實測有效）
------------------------------------------------------------------
    ScanWorker    背景持續刷新怪物清單（熱區掃描，約 28ms）
    TargetWorker  50 Hz 把目標寫回遊戲，並偵測牠死了沒
    KeyWorker     依設定的頻率狂送技能鍵，什麼都不判斷

為什麼要拆：三件事的需求完全不同 —— 掃描量大但不急、寫入要夠即時、
送鍵只要準時。綁在一起的話任何一邊變慢都會拖到另外兩邊，
症狀就是「殺完一批怪會原地發呆」「施放速度偶爾卡卡的」。
"""
from __future__ import annotations

import ctypes
import math
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from app.core import charname, injector
from app.core import window as win
from app.core.memory import MemoryScanner
from app.core.notifier import Notifier
from app.game import aob, entity, inventory, move, player
from app.tabs.base_tab import BaseTab

SKILL_KEYS = [(f"F{i}", 0x6F + i) for i in range(1, 13)]   # F1=0x70 … F12=0x7B
DEFAULT_KEY = 0x71              # F2
DEFAULT_INTERVAL = 0.05         # 秒；每秒送 20 次技能鍵
WRITE_INTERVAL = 0.02           # 秒；多久重寫一次目標＋檢查它死了沒（50 Hz）
SEND_TIMEOUT_MS = 60            # 送鍵最多等遊戲多久（正常 3.4ms，卡住就放棄這一拍）
TICK_MS = 10                    # UI 心跳
RESCAN_GAP = 0.3                # 沒得打了要多快重掃
# 掛機時多久刷新一次怪物清單。熱區掃描實測約 28ms，所以可以一直刷。
# ★ 必須「一直刷」而不是「沒怪才刷」：跟別人搶怪時，清單一過期就會去打
#   別人已經殺掉的、或錯過剛生出來的那隻。
REFRESH_GAP = 0.5
FULL_EVERY = 30.0               # 多久強制做一次全記憶體掃描當保險
INV_RELOCATE_GAP = 8.0          # 找不到物品陣列表頭時，多久才重試（要跑 AOB 全掃）
STATUS_GAP = 0.2                # 狀態列多久重畫一次（心跳 10ms，不必每拍都畫）
HP_CHECK_GAP = 0.5              # 多久確認一次自己還活著（死了就自動停）
GEAR_CHECK_GAP = 3.0            # 多久看一次武器耐久（掉得很慢，不必常看）
WALK_GAP = 1.5                  # 多久可以重下一次移動指令（走一步要時間，別狂送）
WALK_STOP_SHORT = 3.0           # 走到離怪幾格就好（不要站在牠身上）
HOME_SLACK = 4.0                # 離原點超過幾格才值得走回去
KILL_MEMORY = 60.0              # 打死的實體 ID 記多久（避免又挑到同一具屍體）
NEAR_HEIGHT = 130               # 「周圍怪物」清單高度；使用者要求小一點
STUCK_SECS = 10.0               # 沒掉血、玩家也沒移動這麼久 → 這隻走不過去，換一隻

# 攻擊半徑（格）。使用者實測：人站在 (154,40) 時打得到 (130,49) 的怪，
# 再遠就打不到 —— √(24²+9²) ≈ 25.6，也就是以角色為圓心的一個圓。
# 半徑內的怪優先打；半徑內沒有時仍會挑最近的（遊戲的攻擊指令會自己走過去），
# 免得站在原地發呆。可在介面上調整。
ATTACK_RADIUS = 26.0


def _send_scan(hwnd: int, vk: int = DEFAULT_KEY) -> None:
    """對指定視窗送一次技能鍵。

    走 app/core/window.py 的共用實作：SendMessageTimeout + 帶掃描碼的 lParam。
    ⚠ 實測這個遊戲**只吃 SendMessage**，PostMessage 完全沒反應（不管 lParam）。
    不碰使用者真正的鍵盤、不搶焦點，可以同時掛多個分身。

    ★ 逾時設短（SEND_TIMEOUT_MS）：SendMessage 會等對方處理完才返回，
      遊戲一卡（讀地圖、掉幀）一次按放最多要吃兩倍逾時。實測正常只要 3.4ms，
      但量到過單拍 353ms —— 那就是使用者說的「偶爾施放速度卡卡的」。
      這裡寧可漏送一次，也不要讓整條節奏停住。
    """
    win.send_key(hwnd, vk, SEND_TIMEOUT_MS)


class _Paced(QThread):
    """固定節奏的背景迴圈。子類別實作 step()。

    ⚠ Windows 的 Sleep() 預設精度只有約 15.6ms —— msleep(50) 可能睡成 62ms，
    節奏會忽快忽慢。timeBeginPeriod(1) 把系統計時器解析度調到 1ms；
    這是行程層級的設定，用完一定要成對還原。
    """

    def __init__(self, interval: float) -> None:
        super().__init__()
        self._interval = interval
        self._running = True

    def set_interval(self, secs: float) -> None:
        self._interval = max(0.005, secs)

    def stop(self) -> None:
        self._running = False

    def step(self) -> None:                      # 子類別實作
        raise NotImplementedError

    def run(self) -> None:
        ctypes.windll.winmm.timeBeginPeriod(1)
        try:
            due = time.perf_counter()
            while self._running:
                self.step()
                # 睡「到下一拍為止」，不是「睡一個間隔」—— 否則週期會變成
                # 間隔 + 這一拍的工作時間，忙的時候還會越漂越多。
                due += self._interval
                left = due - time.perf_counter()
                if left > 0:
                    self.msleep(int(left * 1000) or 1)
                else:
                    due = time.perf_counter()    # 落後太多就重新對時
        finally:
            ctypes.windll.winmm.timeEndPeriod(1)


class KeyWorker(_Paced):
    """**只**負責送技能鍵，什麼都不判斷。

    刻意跟「寫入目標」拆開：送鍵只要準時，而讀寫記憶體偶爾會慢。
    綁在一起的話，任何一次記憶體操作變慢都會拖到送鍵的節奏（使用者感受到的
    「施放速度卡卡的」）。拆開之後這條執行緒永遠只做一件事：到點就送一次。

    ⚠ 送鍵一定是**一次次按放**。曾經想改成「只送 KEYDOWN 模擬按住」，
      方向是錯的 —— 使用者實測這個遊戲按住不放並不會一直放技能。
    """

    def __init__(self, hwnd: int) -> None:
        super().__init__(DEFAULT_INTERVAL)
        self.hwnd = hwnd
        self.vk = DEFAULT_KEY
        self.sent = 0
        self._on = False

    def set_on(self, on: bool) -> None:
        self._on = on

    def step(self) -> None:
        if not self._on:
            return
        try:
            _send_scan(self.hwnd, self.vk)
            self.sent += 1
        except Exception:                      # noqa: BLE001
            pass


class TargetWorker(_Paced):
    """**只**負責「現在該打誰」：持續把目標寫回遊戲，並偵測牠死了沒。

    跑得比送鍵快（預設 50 Hz），所以送鍵那條永遠打得到最新的目標；
    別人搶走怪、或怪倒下時也能在幾十毫秒內發現。
    """

    # ⚠ 用 Signal(object) 不能用 Signal(int)：實體 ID 是**無號** 32 位元
    # （實測有 0x8E8D04DA = 23 億這種值），而 PySide6 的 int 對應 C++ 的
    # 有號 int，emit 超過 21 億的值會丟 OverflowError，死亡偵測就斷了。
    died = Signal(object)       # 目標倒了（實體 ID）→ UI 執行緒去挑下一隻
    failed = Signal(str)        # 讀寫記憶體失敗

    def __init__(self, sc: MemoryScanner) -> None:
        super().__init__(WRITE_INTERVAL)
        self.sc = sc
        self.hp = 0                 # 最近讀到的目標血量，給 UI 顯示
        self._job: tuple[int, entity.Entity] | None = None
        self._wrote = False

    def attack(self, state: int, ent: entity.Entity) -> None:
        self._wrote = False
        self._job = (state, ent)

    def hold_off(self) -> None:
        self._job = None

    def step(self) -> None:
        job = self._job
        if job is None:
            return
        state, ent = job
        try:
            # ★ 先讀後判斷、最後才寫 —— 順序不能換。
            # 目標死掉時遊戲會把 +0x2D8 清成 0，那是我們唯一的死亡訊號；
            # 先寫回去就把訊號蓋掉了。
            cur = entity.read_target(self.sc, state)
            self.hp = entity.read_target_hp(self.sc, state)
            if self._wrote and (cur == 0 or not entity.is_alive(self.sc, ent)):
                self._job = None
                self._wrote = False
                self.died.emit(ent.eid)
                return

            # ⚠ **每一圈都要重寫兩個欄位**，不能「已經是這隻就跳過」：
            # 遊戲會把 +0x2DC（目標血量%）改回 0，而攻擊前會檢查它 > 0
            # （`cmp [esi+0x2dc],0 / jle 跳過`）。只寫一次的話之後全被跳掉。
            entity.set_target(self.sc, state, ent.eid)
            self._wrote = True
        except Exception as exc:               # noqa: BLE001
            self._job = None
            self.failed.emit(str(exc))


@dataclass
class Scan:
    """一次掃描的結果。欄位一直在增加，包成一個物件比一直加訊號參數清楚。"""

    pid: int
    state: int | None = None      # 狀態物件（寫目標用）
    player: int | None = None     # 玩家實體物件（讀自己的座標用）
    stats: int | None = None      # 角色屬性基準（讀 HP 用；見 app/game/player.py）
    inv: int | None = None        # 物品指標陣列表頭（讀武器耐久用）
    mons: list = field(default_factory=list)
    err: str = ""


class ScanWorker(QThread):
    """背景掃描：一次拿齊狀態物件、玩家物件、角色屬性、附近怪物。

    不能放在 UI 執行緒（全掃約 0.4 秒）。用一條常駐執行緒處理所有分身的請求，
    比每次開新執行緒好收尾。三種物件走 entity.snapshot() 合併成一遍讀取。

    ★ **熱區掃描**：實測這三種物件在每個分身裡都只出現在**單一一塊**記憶體
      （佔全部的 0.1%～1.9%）。所以只有第一次做全掃，之後記住命中的區塊，
      往後只掃那一塊。這讓「刷新怪物清單」便宜到可以每秒做一次，於是：
        · 打完一批怪不必停下來等掃描（原本會原地發呆一兩秒）
        · 掃描不再跟攻擊執行緒搶記憶體頻寬（原本 F2 節奏會被拖到）

    ⚠ 熱區會失效（換地圖、重連、堆積重配置），所以：
        · 每 FULL_EVERY 秒強制做一次全掃當保險
        · 熱區掃描只要找不到狀態或玩家物件，立刻退回全掃重新定位
    """

    done = Signal(object)                        # 一個 Scan

    def __init__(self) -> None:
        super().__init__()
        self._queue: list[tuple[int, MemoryScanner]] = []
        self._hot: dict[int, list] = {}          # pid → 上次命中的區塊
        self._full_at: dict[int, float] = {}     # pid → 上次全掃的時間
        self._inv: dict[int, int] = {}           # pid → 物品陣列表頭
        self._inv_at: dict[int, float] = {}      # pid → 上次找表頭的時間
        self._running = True

    def _inventory_head(self, pid: int, sc: MemoryScanner, now: float):
        """物品陣列表頭。有效就直接用 —— 重找要跑 AOB 全掃，很貴。"""
        head = self._inv.get(pid)
        if head and inventory.is_valid(sc, head):
            return head
        if now - self._inv_at.get(pid, -99.0) < INV_RELOCATE_GAP:
            return None                          # 剛找過還是失敗，先別一直重試
        self._inv_at[pid] = now
        hits = aob.scan(sc, aob.SKILL_EXP_BALL, limit=4096,
                        should_stop=lambda: not self._running)
        head = inventory.locate(sc, {a - inventory.ITEM_BALL_OFF for a in hits})
        if head:
            self._inv[pid] = head
        return head

    def request(self, pid: int, sc: MemoryScanner) -> None:
        if not any(p == pid for p, _ in self._queue):
            self._queue.append((pid, sc))

    def forget(self, pid: int) -> None:
        """丟掉熱區快取，下次強制全掃（換地圖／重新定位時用）。"""
        self._hot.pop(pid, None)

    def run(self) -> None:
        while self._running:
            if not self._queue:
                self.msleep(20)
                continue
            pid, sc = self._queue.pop(0)
            out = Scan(pid)
            try:
                now = time.monotonic()
                stats_vt = player.vtable_value(sc)   # 角色屬性物件，順便一起掃
                hot = self._hot.get(pid)
                if now - self._full_at.get(pid, 0.0) >= FULL_EVERY:
                    hot = None                   # 到期了，做一次全掃保險

                def scan(regions):
                    return entity.snapshot(
                        sc, should_stop=lambda: not self._running,
                        regions=regions, extra_vts=(stats_vt,))

                state, pobj, ents, found, extra = scan(hot)
                if hot is not None and (state is None or pobj is None):
                    # 熱區裡找不到本體 → 物件搬家了，立刻退回全掃
                    state, pobj, ents, found, extra = scan(None)
                    hot = None
                # ⚠ 只有全掃的結果可以拿來「重設」熱區。熱區掃描找到的區塊
                # 必然是熱區的子集，拿它覆寫的話熱區只會越縮越小 ——
                # 某一刻剛好沒怪，那塊就被丟掉，之後再也掃不到（直到下次全掃）。
                if hot is None:
                    self._full_at[pid] = now
                    if found:
                        self._hot[pid] = found
                out.state, out.player = state, pobj
                out.stats = player.pick(sc, extra.get(stats_vt, []))
                out.inv = self._inventory_head(pid, sc, now)
                out.mons = [e for e in ents if e.is_monster]
                if state is None:
                    out.err = "找不到狀態物件（掃到 0 個或多個）"
                elif pobj is None:
                    out.err = "找不到玩家物件（掃到 0 個或多個）"
            except Exception as exc:               # noqa: BLE001
                out.err = f"掃描失敗：{exc}"
            if self._running:
                self.done.emit(out)

    def stop(self) -> None:
        self._running = False


class CharFarmPage(QWidget):
    """單一分身的掛機介面。"""

    def __init__(self, pid: int, hwnd: int, title: str, sc: MemoryScanner,
                 on_scan, tgt: TargetWorker, keys: KeyWorker,
                 notifier: Notifier | None = None,
                 account: str = "", char_name: str = "") -> None:
        super().__init__()
        self.pid = pid
        self.hwnd = hwnd
        self.title = title
        self.sc = sc
        self.account = account
        self.char_name = char_name
        self._notifier = notifier
        # 三件事各自跑自己的執行緒，互不干擾：
        #   掃描更新清單（ScanWorker，全分身共用）
        #   寫入目標＋偵測死亡（TargetWorker，50 Hz）
        #   狂送技能鍵（KeyWorker，使用者設定的頻率）
        # 本頁只負責「挑要打誰」跟畫面。
        self._atk = tgt
        self._keys = keys
        tgt.died.connect(self._on_died)
        tgt.failed.connect(lambda msg: self._stop_with(f"⚠ 記憶體存取失敗：{msg}"))
        self.state: int | None = None
        self.player: int | None = None           # 玩家物件位址（拿來讀自己的座標）
        self.stats: int | None = None            # 角色屬性基準（拿來讀 HP）
        self.inv: int | None = None              # 物品陣列表頭（拿來讀武器耐久）
        self.mons: list[entity.Entity] = []
        self._on_scan = on_scan
        self._cur: entity.Entity | None = None   # 正在打的那隻
        self._kills = 0
        self._waiting = False      # 正在等重新掃描的結果
        self._since_scan = 0.0     # 距離上次自動重掃過了多久
        self._stuck = 0.0          # 打不到也走不到的時間（卡住偵測）
        self._show = 0.0           # 距離上次重畫狀態列過了多久
        self._hp_t = 0.0           # 距離上次檢查自己的 HP 過了多久
        self._hp = -1              # 最近讀到的 HP（給狀態列用）
        self._gear_t = 0.0         # 距離上次檢查武器耐久過了多久
        self._dura = (-1, -1)      # 最近讀到的 (耐久, 上限)
        self._mover: move.Mover | None = None
        self._walk_t = 0.0         # 距離上次下移動指令過了多久
        self._home: tuple[float, float] | None = None   # 原點
        self._last_hp = -1
        self._last_pos: tuple[float, float] | None = None
        # 已經打死（或判定走不過去）的實體 ID → 記下來的時間。
        # 怪死掉後物件不會馬上被回收，is_alive() 可能還是 true —— 沒這層擋著，
        # 換下一隻時會又挑到同一具屍體。
        # ⚠ 不能在每次重掃時清空：現在每秒都在刷新清單，一清就等於沒擋。
        #   改成保留 KILL_MEMORY 秒後自動淘汰（實體 ID 久了才可能被重用）。
        self._killed: dict[int, float] = {}

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.scan_btn = QPushButton("掃描周圍怪物")
        self.scan_btn.clicked.connect(lambda: self._on_scan(self.pid))
        bar.addWidget(self.scan_btn)
        bar.addSpacing(12)
        bar.addWidget(QLabel("技能鍵"))
        self.key_box = QComboBox()
        for label, vk in SKILL_KEYS:
            self.key_box.addItem(label, vk)
        self.key_box.setCurrentIndex(
            next(i for i, (_, vk) in enumerate(SKILL_KEYS) if vk == DEFAULT_KEY))
        self.key_box.setFixedWidth(70)
        self.key_box.setToolTip("要一直按的攻擊／技能鍵。")
        self.key_box.currentIndexChanged.connect(
            lambda i: setattr(self._keys, "vk", self.key_box.itemData(i)))
        bar.addWidget(self.key_box)
        bar.addSpacing(12)
        bar.addWidget(QLabel("每隔"))
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.02, 5.0)
        self.interval.setSingleStep(0.01)
        self.interval.setDecimals(2)
        self.interval.setValue(DEFAULT_INTERVAL)
        self.interval.setSuffix(" 秒按一次")
        self.interval.setFixedWidth(130)
        self.interval.valueChanged.connect(self._keys.set_interval)
        self._keys.set_interval(DEFAULT_INTERVAL)
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
            "勾選後開始：把「選中怪物」裡離自己最近的一隻寫進遊戲的目前目標，"
            "並持續送技能鍵。\n打死會自動接下一隻。取消勾選立刻停止。")
        self.run_cb.toggled.connect(self._on_toggle)
        bar.addWidget(self.run_cb)
        bar.addStretch(1)
        root.addLayout(bar)

        # 移動列：走過去遠處的怪、以及回原點
        mbar = QHBoxLayout()
        self.move_cb = QCheckBox("自動走過去")
        self.move_cb.setChecked(True)
        self.move_cb.setToolTip(
            "半徑內沒有選中的怪時，主動走到最近那隻旁邊。\n"
            "⚠ 這項功能需要在遊戲行程裡執行一小段程式碼（掛 PeekMessageA），\n"
            "　 才能呼叫遊戲自己的移動函式 —— 純讀寫記憶體做不到移動。")
        mbar.addWidget(self.move_cb)
        mbar.addSpacing(12)
        self.home_btn = QPushButton("設為原點")
        self.home_btn.setToolTip("把角色目前的位置記成原點。")
        self.home_btn.clicked.connect(self._set_home)
        mbar.addWidget(self.home_btn)
        self.home_lbl = QLabel("原點：未設定")
        self.home_lbl.setStyleSheet("color: #9aa2b8;")
        mbar.addWidget(self.home_lbl)
        mbar.addSpacing(12)
        self.back_cb = QCheckBox("沒怪時回原點")
        self.back_cb.setToolTip(
            "附近沒有選中的怪時走回原點，避免一路追怪越跑越遠。")
        mbar.addWidget(self.back_cb)
        mbar.addStretch(1)
        root.addLayout(mbar)

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
    def _set_home(self) -> None:
        p = self.my_pos()
        if p is None:
            self.status.setText("讀不到位置，請先按「掃描周圍怪物」")
            return
        self._home = p
        self.home_lbl.setText(f"原點：({p[0]:.0f}, {p[1]:.0f})")

    def _ensure_mover(self) -> bool:
        """需要移動時才安裝 hook —— 沒用到就不要在遊戲裡放程式碼。"""
        if self._mover is not None and self._mover.active:
            return True
        if self._mover is not None:
            return False                       # 之前裝失敗過，別一直重試
        try:
            mv = move.Mover(self.pid, injector.process_path(self.pid))
            mv.start()
            self._mover = mv
            return True
        except Exception as exc:               # noqa: BLE001
            self._mover = move.Mover(self.pid, "")   # 佔位，代表試過了
            self.status.setText(f"⚠ 無法啟用移動：{exc}（掛機其他功能不受影響）")
            return False

    def _walk_toward(self, gx: float, gy: float, me, keep: float) -> None:
        """往 (gx,gy) 走，但在距離 keep 格處停下。有冷卻，不會狂送。"""
        if not self._ensure_mover():
            return
        d = math.hypot(gx - me[0], gy - me[1])
        if d <= keep:
            return
        r = (d - keep) / d                     # 只走到剩 keep 格的位置
        self._mover.walk_to(self.sc, self.player,
                            me[0] + (gx - me[0]) * r,
                            me[1] + (gy - me[1]) * r)
        self._walk_t = 0.0

    def my_pos(self) -> tuple[float, float] | None:
        """玩家目前的格子座標（每次都重讀，因為角色會走動）。"""
        if self.player is None:
            return None
        return entity.read_pos(self.sc, self.player)

    def apply_scan(self, s: Scan) -> None:
        self.state = s.state
        self.player = s.player
        self.stats = s.stats
        self.inv = s.inv
        self.mons = s.mons or []
        err = s.err
        # 只列中文名字（去重、不顯示數量、不顯示任何 ID）。
        # 掛機時每秒都在刷新，內容沒變就別重建清單 —— 不然使用者的選取會一直被清掉。
        seen = []
        for m in self.mons:
            if m.name not in seen:
                seen.append(m.name)
        if seen != [self.near.item(i).text() for i in range(self.near.count())]:
            self.near.clear()
            self.near.addItems(seen)
        self.scan_btn.setEnabled(True)
        self._waiting = False

        if err:
            self.status.setText(f"⚠ {err}")
            return

        # 掛機中且正在等下一隻 → 自動挑名字在清單裡、離自己最近的接上去
        if self.run_cb.isChecked():
            if self._cur is None and not self._pick_next():
                self.status.setText(
                    f"附近沒有選中的怪了（已擊殺 {self._kills} 隻）→ 等新的出現…")
            return          # 掛機中的狀態列由 tick() 負責，別蓋掉
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
        now = time.monotonic()
        for eid, t in list(self._killed.items()):     # 淘汰太舊的死亡記錄
            if now - t > KILL_MEMORY:
                del self._killed[eid]
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
        self._atk.attack(self.state, self._cur)   # 寫入執行緒：開始鎖定這隻
        self._keys.set_on(True)                   # 送鍵執行緒：開始狂按
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
        self._killed[eid] = time.monotonic()   # 免得又挑到同一具還沒回收的屍體
        self._cur = None
        if not self.run_cb.isChecked():
            self._keys.set_on(False)
            return
        if not self._pick_next():
            self._keys.set_on(False)              # 沒目標就別空按
            self._since_scan = RESCAN_GAP         # 清單空了，才排重掃
            self.status.setText(
                f"「{m.name if m else ''}」倒了（累計 {self._kills} 隻）→ 重新掃描…")

    def _on_toggle(self, on: bool) -> None:
        if not on:
            self._keys.set_on(False)
            self._atk.hold_off()
            self._cur = None
            # 若是被 _stop_with() 停的（例如角色死亡），它會在這之後蓋上原因
            self.status.setText(f"已停止（本次擊殺 {self._kills} 隻）")
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

        寫目標與送鍵各自在 TargetWorker / KeyWorker 的執行緒上跑，節奏不受 UI
        影響 —— 原本整個迴圈掛在這裡，UI 一忙節奏就漂掉，感受就是「很卡」。
        """
        if not self.run_cb.isChecked() or self.state is None:
            return

        # ★ 角色死了就自動停：不然會對著空氣一直送技能鍵。
        # HP 走 app/game/player.py（跨 5 台驗證過的定位）。
        # ⚠ 它的 read() 刻意不做數值檢查，HP 歸零照樣讀得到 —— 早期版本把
        #   「HP > 0」寫進合理性檢查，結果角色一死就回 None，死亡永遠測不到。
        self._hp_t += dt
        if self._hp_t >= HP_CHECK_GAP:
            self._hp_t = 0.0
            if self.stats:
                st = player.read(self.sc, self.stats)
                if st is not None:
                    self._hp = st.hp
                    if st.hp <= 0:
                        self._stop_with(
                            f"☠ 角色死亡 → 已自動停止掛機"
                            f"（本次擊殺 {self._kills} 隻）")
                        return
                else:
                    self.stats = None       # 物件搬家了，等下次掃描重新定位

        # ★ 掛機時持續在背景刷新清單，不要等到沒怪可打才去掃。
        # 掃描已經降到只掃熱區（很便宜），所以可以每秒刷一次；
        # 這樣打完一批怪也有現成的名單可接，不會原地發呆等掃描。
        self._since_scan += dt
        gap = RESCAN_GAP if self._cur is None else REFRESH_GAP
        if self._since_scan >= gap and not self._waiting:
            self._since_scan = 0.0
            self._waiting = True
            self._on_scan(self.pid)
        # ★ 武器壞了（耐久 0）就停下來並通知 —— 壞掉的武器打不動怪，
        # 繼續掛只是白費時間。耐久掉得很慢，幾秒看一次就夠。
        self._gear_t += dt
        if self._gear_t >= GEAR_CHECK_GAP and self.inv:
            self._gear_t = 0.0
            d = inventory.durability(self.sc, self.inv)
            if d is not None:
                self._dura = d
                if d[0] <= 0:
                    self._stop_with(f"🔧 武器已損壞（耐久 0）→ 已自動停止掛機"
                                    f"（本次擊殺 {self._kills} 隻）")
                    self.notify("武器已損壞（耐久 0），掛機已自動停止。")
                    return

        self._walk_t += dt
        if self._cur is None:
            # 沒怪可打時走回原點，免得一路追怪越跑越遠
            me = self.my_pos()
            if (self.back_cb.isChecked() and self._home and me
                    and self._walk_t >= WALK_GAP
                    and math.hypot(me[0] - self._home[0],
                                   me[1] - self._home[1]) > HOME_SLACK):
                self._walk_toward(self._home[0], self._home[1], me, 0.0)
                self.status.setText(
                    f"附近沒有選中的怪 → 走回原點"
                    f"({self._home[0]:.0f},{self._home[1]:.0f})…")
            return

        m = self._cur
        hp = self._atk.hp
        me = self.my_pos()

        # ★ 半徑外的怪就自己走過去（遊戲的攻擊指令雖然也會接近，但只能走向怪、
        # 而且到攻擊範圍就停；主動走比較好控制，也才能配合回原點）。
        if (self.move_cb.isChecked() and me and self._walk_t >= WALK_GAP):
            mp = entity.read_pos(self.sc, m.addr)
            if mp and math.hypot(mp[0] - me[0],
                                 mp[1] - me[1]) > self.radius.value():
                self._walk_toward(mp[0], mp[1], me, WALK_STOP_SHORT)

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
            self._killed[m.eid] = time.monotonic()   # 走不過去，暫時別再挑它
            self._atk.hold_off()
            self._cur = None
            if not self._pick_next():
                self._keys.set_on(False)
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
            f"掛機中：{m.name}　距離 {d:.1f} 格　目標血量 {hp}%"
            + (f"　我的 HP {self._hp:,}" if self._hp >= 0 else "")
            + (f"　武器耐久 {self._dura[0]}/{self._dura[1]}"
               if self._dura[0] >= 0 else "")
            + f"　累計擊殺 {self._kills}")

    def notify(self, msg: str) -> None:
        """送警報通知。設定沿用收益監控那一份（使用者只要設定一次）。"""
        if self._notifier is None:
            return
        who = f"{self.account}（{self.char_name}）"
        note = self._notifier.fire(who, msg)
        self.status.setText(self.status.text() + f"　[{note}]")

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
        self._keys: list[_Paced] = []         # 每個分身：寫入執行緒 + 送鍵執行緒
        # 通知設定沿用收益監控那一份，使用者只要在那邊設定一次
        self._notifier = Notifier(self, "⚠ 自動掛機警報")
        self._notifier.failed.connect(
            lambda msg: self.found.setText(msg))

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
            "會挑**離自己最近**的一隻持續送技能鍵；打死之後立刻接下一隻，"
            "不必手動再選。清單在背景持續更新（跟人搶怪也不會拿到過期資料）。\n"
            "取消勾選才停。不搶視窗焦點、不占用你的鍵盤滑鼠，可以同時掛多個分身。")
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
        # 掃描讓路給攻擊：掃描是大量記憶體讀取，攻擊只要準時。
        self._worker.start(QThread.LowPriority)
        for pid, hwnd, title, sc in insts:
            tgt, keys = TargetWorker(sc), KeyWorker(hwnd)
            tgt.start(QThread.HighPriority)
            keys.start(QThread.HighPriority)
            self._keys += [tgt, keys]
            acct = charname.account_from_title(title)
            nm = charname.read_character_name(sc, acct) or acct
            page = CharFarmPage(pid, hwnd, title, sc, self._request_scan,
                                tgt, keys, self._notifier, acct, nm)
            self._pages[pid] = page
            self.tabs.addTab(page, nm)
        self.found.setText(f"偵測到 {len(insts)} 個分身")

    def _request_scan(self, pid: int) -> None:
        page = self._pages.get(pid)
        if page is None or self._worker is None:
            return
        # 掛機中每秒都會刷新，這時不要動按鈕與狀態列（會一直閃）
        if not page.run_cb.isChecked():
            page.scan_btn.setEnabled(False)
            page.status.setText("掃描中…（全記憶體掃描，約 0.5 秒）")
        self._worker.request(pid, page.sc)

    def _on_scan_done(self, s: Scan) -> None:
        page = self._pages.get(s.pid)
        if page is not None:
            page.apply_scan(s)

    def _tick(self) -> None:
        dt = TICK_MS / 1000.0
        for page in self._pages.values():
            page.tick(dt)

    # ------------------------------------------------------------------
    def _teardown(self) -> None:
        for page in self._pages.values():
            page.run_cb.setChecked(False)
            # ⚠ 一定要拆掉移動 hook —— 不還原 IAT 就等於在遊戲裡留了一段跳板
            if page._mover is not None:
                page._mover.stop()
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
