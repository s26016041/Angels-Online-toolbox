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
    QButtonGroup,
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
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import config
from app.core import charname, injector
from app.core import window as win
from app.core.memory import MemoryScanner
from app.core.notifier import Notifier
from app.game import aob, attack, entity, inventory, move, player
from app.tabs.base_tab import BaseTab

SKILL_KEYS = [(f"F{i}", 0x6F + i) for i in range(1, 13)]   # F1=0x70 … F12=0x7B
DEFAULT_KEY = 0x71              # F2
DEFAULT_INTERVAL = 0.05         # 秒；每秒送 20 次技能鍵
WRITE_INTERVAL = 0.02           # 秒；多久重寫一次目標＋檢查它死了沒（50 Hz）
SEND_TIMEOUT_MS = 60            # 送鍵最多等遊戲多久（正常 3.4ms，卡住就放棄這一拍）
TICK_MS = 10                    # UI 心跳
RESCAN_GAP = 0.3                # 沒得打了要多快重掃
IDLE_SCAN_GAP = 1.5             # 沒在掛機時也持續刷新「周圍怪物」的間隔
# 掛機時多久刷新一次怪物清單。熱區掃描實測約 28ms，所以可以一直刷。
# ★ 必須「一直刷」而不是「沒怪才刷」：跟別人搶怪時，清單一過期就會去打
#   別人已經殺掉的、或錯過剛生出來的那隻。
# 掛機中、已經有目標時的刷新間隔。熱區掃描只要約 28ms，所以可以很密。
# ⚠ 刷新變快**不會讓屍體消失** —— 牠們是真的還在遊戲的實體清單裡。
#   加快只對「新出現的怪」有幫助。
REFRESH_GAP = 0.3
FULL_EVERY = 30.0               # 多久強制做一次全記憶體掃描當保險
INV_RELOCATE_GAP = 8.0          # 找不到物品陣列表頭時，多久才重試（要跑 AOB 全掃）
HP_CHECK_GAP = 0.5              # 多久確認一次自己還活著（死了就自動停）
GEAR_CHECK_GAP = 3.0            # 多久看一次武器耐久（掉得很慢，不必常看）
# 多久可以重下一次移動指令。★ 單次指令只走得到約 15 格（見 app/game/move.py
# 的 MAX_HOP），長距離是靠這裡定期重下、一段一段接力走完的，所以不能太久。
# ★ 下一次移動指令最少隔多久。**而且角色還在走時一律不下**（見 tick）。
# ⚠⚠ 這是「走不回原點」的真正原因：以前每 0.8 秒就重下一次指令，
#   把還沒走完的**多點路徑砍掉**。那條路徑常常是「先繞開再往前」，
#   我們正好在繞開那一段打斷它 → 回到原處又重算 → 無限來回，
#   看起來就像根本沒在算路徑。實測不打斷之後，176 格外的原點 42 秒就走回去了。
WALK_GAP = 0.4
# 判斷「有沒有在移動」要隔一段時間再比位置。
# ⚠ 心跳是 10ms，而角色約 9 格/秒 = 每拍才 0.09 格 —— 拿相鄰兩拍比
#   幾乎永遠判定「沒在動」（卡住偵測也一起誤判，走路中照樣累積秒數）。
# 走路的到達容差：距離只超過目標一點點就不要再走了。
# 沒有它的話角色會在定位附近一直被推一小步（「打一打又往前一格」）。
WALK_SLACK = 1.5
MOVE_SAMPLE = 0.3
MOVE_EPS = 0.5                  # 這段時間內位移超過這個就算在移動
KILL_MEMORY = 60.0              # 打死的實體 ID 記多久（避免又挑到同一具屍體）
# 「一直沒給血量」而跳過的冷卻。那只是推測，牠可能還活著，所以比擊殺短。
# ⚠ 不能設 0：挑目標永遠挑最近的，冷卻 0 的話下一拍又挑到同一具，
#   變成無限迴圈。
# ⚠ 也不能太短：實測搶怪區 90 秒有 41 隻「沒給血量」的實體，其中 12 隻被
#   反覆挑到（從第一次到最後一次**中位相隔 13 秒**、最久 34 秒）——
#   冷卻 5 秒時 64 段裡有 23 段是重複挑同一具屍體。20 秒才擋得住。
NOHP_MEMORY = 20.0
NEAR_HEIGHT = 130               # 「周圍怪物」清單高度；使用者要求小一點
STUCK_SECS = 10.0               # 沒掉血、玩家也沒移動這麼久 → 這隻走不過去，換一隻
# ⛔ 不要做「打不到就一步一步走近」那種自動收斂 —— 使用者明講看起來卡卡的，
#    而且根本不需要：實測（黑狐，目標有寫進記憶體）12.2 / 11.6 / 8.9 格
#    都打得死（血量 100 → 47 → 0）。曾經以為 13 格打不到，那是診斷時
#    **忘了寫目標**、讀到的血量根本不是那隻怪，量錯了。
#    另外目標血量欄位會被遊戲清成 0，拿它當「沒打到」的訊號一定誤判。

# ★ 攻擊封包真正有效的距離。遊戲的固定值，不是角色屬性（使用者指出的）。
#
# ⚠⚠ 曾經設成 15.0，那是**錯的**，而且錯得很難看出來：
#   15.7 是「客戶端在多遠會**送出**攻擊封包」量到的最大值 —— 但客戶端送完
#   會自己走過去，**伺服器並不接受那麼遠的施放**。我們自己送封包沒有那段
#   走過去的行為，於是角色就停在「剛好打不到」的地方站著。
#
#   唯讀觀察正在掛機的黑狐 90 秒（完全沒干擾）：
#     13.0 / 13.2 / 13.4 / 13.6 / 13.9 / 14.3 / 14.5 格 → 血量 100 動都不動，
#       8 次發呆全部落在這裡，每次都要等卡住偵測 10 秒才換怪，90 秒只殺 1 隻
#     12.2 / 11.6 / 8.9 / 8.3 / 1.2 格 → 正常掉血、正常擊殺
#   → 真正的界線在 12.2 與 13.0 之間，取 12.0 留餘裕。
ATTACK_PACKET_RANGE = 12.0
# 超出範圍時**一次就走到 10 格內**（使用者定的）。
# 留 2 格餘裕是必要的：怪自己會動，停在射程邊緣的話牠一走就又出界，
# 然後又得重走一次 —— 那就是「卡卡的」的來源。
# 走進攻擊範圍後停在幾格。使用者查到「遠程技能基本上都是 12 射程」，
# 所以停 11 格、只留 1 格餘裕（停 8 格太保守，會多走一段冤枉路）。
# ⚠ 先前量到「站在 10.0 格打不到」，那是路徑點數被誤用造成的
#   （blocked 誤判 → 攻擊距離被縮成 2 格），已經修掉，不是射程問題。
WALK_KEEP = 11.0
# 血量要**連續**讀到 0 這麼久才算死亡。偶爾讀到一次 0 不算
# —— 那會把還活著的怪丟掉。
HP_SETTLE = 0.5
# 鎖定這麼久還**從來沒**看到血量 → 那是屍體（別人先打死的），換一隻。
# ⚠ 門檻是量出來的：活著的目標鎖定後看到血量，中位 0.30 秒、最久 2.22 秒
#   （34 隻樣本），所以 3 秒不會誤殺活的怪。
#   使用者先要求 0.5 秒，實測誤跳過 5% 的活怪、而且「常常真的有那隻怪」，
#   所以放寬到 0.8 秒（活著的目標實測最久 0.52 秒就會顯示血量）。
#   被誤跳過的只冷卻 NOHP_MEMORY 秒，很快會再輪到。
CORPSE_SECS = 0.8
# ★★ 射程**每個角色不一樣**，上面那個 12 是遠程（黑狐）的。
#   雪狐是近戰：牠自己打怪時會送一堆移動封包（使用者攔到 6 包），
#   靠客戶端走到怪身上才打得到。我們停在 10 格送施放，伺服器完全不理
#   —— 症狀就是「雪狐完全無法打怪」。
#   ★ 定案（使用者決定）：**頁面上分「近戰／遠程」兩種攻擊型態**，不自動判斷。
#     近戰 = 選定封包 + 狂按 Fx，走位與射程全交給客戶端；
#     遠程 = 送施放封包，用上面的 12 / 11 格。
#   ⛔ 不要再嘗試讓兩者共用一套 —— 找射程欄位、找自動接近的封包、
#     逆向客戶端攻擊函式、猜靜態單例，四條路全部失敗；
#     自己按鍵量測也被使用者否決（有些技能是原地施放）。
#     完整紀錄見記憶 client-attack-fn-deadend。
# 學技能 ID：送一次鍵、隔 LEARN_GAP 讀一次，正常一次就拿到。
# 只有「這個角色登入後還沒放過任何技能」（欄位是 0）才會多試幾次。
LEARN_GAP = 0.25                # 按鍵到遊戲寫入要隔一幀，讀太密只是白讀
# 兩種攻擊方式（兩個分頁各用一種）：
#   MODE_PACKET —— 選定封包 + 動作/施放封包（原本的「自動掛機」）
#   MODE_KEY    —— 選定封包 + 狂按技能鍵（「自動掛機（按鍵）」）
MODE_PACKET = "packet"
MODE_KEY = "key"
# ⛔ 不要用「補按技能鍵」來讓角色接近（試過，使用者實測還是會卡住）。
#    走過去打是客戶端的行為，但補按鍵會讓客戶端和我們的移動指令互相打架，
#    角色反而鎖著遠處的怪站著不動。接近一律用我們自己的尋路（見下面的 tick）。
CLOSE_ENOUGH = 2.0              # 隔著地形時要走到多近（貼臉）
# ★ 近戰模式：走到 2 格以內才送攻擊封包（使用者指定）。
#   遠程角色維持原本的判斷（攻擊距離 12、走到 10 格內）。
#   為什麼要分：射程是每個角色不一樣的，近戰在 10 格外送施放伺服器不理
#   —— 實測雪狐就是這樣完全打不到怪。
MELEE_RANGE = 2.0
# ★ 近戰模式：進到這個距離就開始鎖定＋狂按 Fx，剩下的走位交給遊戲客戶端 ——
#   它知道每招的射程，也會自己繞地形。我們只負責把角色帶進這個圈子，
#   **不要再自己往怪身上推**（兩邊搶著下移動指令會互相打架，實測會卡住）。
CLIENT_RANGE = 20.0
SPOT_SLACK = 3.0                # 走到離巡邏點這麼近就算到了，換下一個
PATH_GAP = 0.2                  # 問尋路「中間有沒有障礙物」的重試間隔
# 尋路一次算得出的範圍（實測約 30~40 格，超過就回 0）。
# 超過這個距離回 0 只代表「太遠，要接力走」，**不是**「到不了」。
PATHFIND_RANGE = 25.0
# 要連續這麼多次「算不出路徑」才判定走不到。怪會走動，單一次很可能只是
# 牠剛好站到走不進去的格子 —— 每次都信就會變成「打一下就換下一隻」。
UNREACH_HITS = 3
# 目標要連續這麼多次掃不到才當作牠死了／離開視野。
# 熱區掃描偶爾會漏，單次就放棄會在打鬥中間換目標。
GONE_SCANS = 2


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
    """**只**負責發動攻擊，什麼都不判斷。

    刻意跟「寫入目標」拆開：發動只要準時，而讀寫記憶體偶爾會慢。
    綁在一起的話，任何一次記憶體操作變慢都會拖到節奏（使用者感受到的
    「施放速度卡卡的」）。拆開之後這條執行緒永遠只做一件事：到點就發一次。

    兩種發動方式
    ------------
    ① **送技能鍵**（一定做得到，不需要注入）
    ② **直接送封包**（attack.select 一次 + attack.strike 重複）
       —— 需要技能 ID 和 Mover

    ★ 技能 ID 是自己學來的：送出技能鍵之後，遊戲會把剛放的技能 ID 寫進
      「角色屬性基準 −0x50」（見 app/game/player.py 的 read_last_skill）。
      所以流程是「先按鍵打第一下 → 學到 ID → 之後改送封包」，
      使用者不必自己去攔封包。

    ⚠ 送鍵一定是**一次次按放**。曾經想改成「只送 KEYDOWN 模擬按住」，
      方向是錯的 —— 使用者實測這個遊戲按住不放並不會一直放技能。
    """

    learned = Signal(object)        # 讀到的技能 ID（0 = 讀不到）

    def __init__(self, hwnd: int, sc: MemoryScanner) -> None:
        super().__init__(DEFAULT_INTERVAL)
        self.hwnd = hwnd
        self.sc = sc
        self.vk = DEFAULT_KEY
        self.sent = 0
        self.mode = MODE_PACKET     # 這個分頁用哪種攻擊方式
        self.packets = True         # 使用者要不要用封包攻擊（封包模式才有意義）
        self.stats = None           # 角色屬性基準（學技能 ID 用）
        self.mover = None
        self.pf = None              # move.pathfinder_this()：**玩家物件 −8**
        self.eid = None             # 現在要打誰
        # 目標的格子座標，填在施放封包裡 —— 順移那類對地技能沒有座標發不動。
        self.pos: tuple[float, float] = (0.0, 0.0)
        self._sel = None            # 已經送過「選定」的目標（換目標才要再送）
        self.skill = None           # 學到的技能 ID
        self._on = False
        self._learning = False
        self._next_learn = 0.0

    def set_on(self, on: bool) -> None:
        self._on = on

    @property
    def selected(self) -> bool:
        """「選定」封包已經替現在這個目標送出去了嗎？

        ★ 很重要：遊戲**收到選定封包才會填目標血量**。沒送之前血量一直是 0，
          跟屍體長得一模一樣（實測：只寫記憶體選目標，6 隻活怪全部沒血量；
          補送選定封包後同樣那 6 隻全部顯示血 75）。
          所以「多久沒看到血量就算屍體」一定要從這裡開始算。
        """
        return bool(self.eid) and self._sel == self.eid

    def begin_learning(self) -> None:
        """開始學技能 ID —— 只有三步：**清零 → 按選定的 Fx → 讀記憶體**。

        這裡做第一步（清零），後面兩步在 step() 裡：攻擊本來就會按那個鍵，
        每 LEARN_GAP 讀一次，讀到非零值就結束。

        ⚠⚠ **一定要先清零**。單次按鍵不保證會寫入（冷卻／間隔；黑狐在沒有
          目標時甚至完全不寫），不清零就會讀到**上一次殘留的技能 ID** ——
          雪狐就是這樣把 F3 的 0x2E1 當成 F2 的技能，結果完全打不動怪。
          清零之後，讀到任何非零值就一定是這個鍵按出來的。

        學不到就一直是 None，攻擊自然留在按鍵那條（本來就有效）。
        """
        self.skill = None
        self._learning = True
        self._next_learn = 0.0
        try:
            if self.stats:
                player.clear_last_skill(self.sc, self.stats)
        except Exception:                      # noqa: BLE001
            pass

    def stop_learning(self) -> None:
        self._learning = False

    def _learn(self) -> None:
        """第三步：讀記憶體。欄位已清零，所以非零值就是這個鍵的技能。"""
        now = time.perf_counter()
        if now < self._next_learn:
            return                             # 按鍵到寫入要隔一幀，別讀太密
        self._next_learn = now + LEARN_GAP
        if not self.stats:
            return
        sid = player.read_last_skill(self.sc, self.stats)
        if sid:
            self.skill = sid
            self._learning = False
            self.learned.emit(sid)

    def step(self) -> None:
        try:
            if self.eid is None:
                self._sel = None       # 沒目標了，下一隻要重新送「選定」
            if not self._on:
                return
            # ① 換目標才送一次「選定」封包 —— 我們是直接寫記憶體選怪的，
            #    遊戲不會自己送這一包。兩種模式都要送。
            if self.mover is not None and self.eid and self._sel != self.eid:
                if not attack.select(self.mover, self.eid):
                    return                     # 這一拍先不打，下一拍再試
                self._sel = self.eid
            # ② 攻擊。封包模式送「動作 + 施放」，按鍵模式就狂按那個鍵。
            if (self.mode == MODE_PACKET and self.packets and self.skill
                    and self.eid and self.pf and self.mover is not None
                    and attack.strike(self.mover, self.pf, self.skill,
                                      self.eid, *self.pos)):
                self.sent += 1
            else:
                _send_scan(self.hwnd, self.vk)
                self.sent += 1
                if self._learning:
                    self._learn()              # 剛按過鍵，順手讀一下
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
    # 目標沒了（實體 ID, 是不是確定死了）→ UI 執行緒去挑下一隻。
    # 第二個參數 False = 只是「一直沒給血量」的推測，那隻可能還活著，
    # 所以只短暫跳過、不要封鎖一分鐘。
    died = Signal(object, bool)
    failed = Signal(str)        # 讀寫記憶體失敗

    def __init__(self, sc: MemoryScanner) -> None:
        super().__init__(WRITE_INTERVAL)
        self.sc = sc
        self.hp = 0                 # 最近讀到的目標血量，給 UI 顯示
        # 用封包攻擊時 True：只寫目標 ID、**不碰血量欄位**，
        # 這樣血量才是遊戲寫的真值，0 就真的代表死了。見 entity.set_target_id。
        self.packets = False
        self._job: tuple[int, entity.Entity] | None = None
        self._wrote = False
        self._saw_hp = False        # 這隻有沒有讀到過 > 0 的血量
        self._zero_at = 0.0         # 血量開始連續讀到 0 的時間
        self._since = 0.0           # **選定封包送出**之後過了多久（判斷屍體用）
        # 「選定」封包送出去了沒。⚠ 遊戲收到那一包才會填血量，
        #   所以沒送之前不能開始算屍體 —— 否則走過去的路上會把活怪全丟掉。
        self.engaged = False

    def attack(self, state: int, ent: entity.Entity) -> None:
        self._wrote = False
        self._saw_hp = False
        self._zero_at = 0.0
        self._since = time.monotonic()
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
            now = time.monotonic()
            if self.hp > 0:
                self._saw_hp = True
                self._zero_at = 0.0
            elif not self._zero_at:
                self._zero_at = now
            # ★ 用封包攻擊時，血量歸零就是**遊戲告訴我們這隻死了**。
            #   這才是可靠的死亡訊號 —— 屍體不會馬上從實體清單消失，
            #   `is_alive()`（vtable + 實體 ID）也分不出死活。
            # ⚠⚠ 兩個條件缺一不可，**都是實測踩出來的**：
            #   ① 要先看過血量 > 0 —— 遊戲對「還沒交戰」的目標本來就回報 0，
            #      少了這條會剛鎖定就判死（實測 150 秒誤判放棄 22 次、
            #      真正擊殺只有 4 次）。
            #   ② 0 要**連續**持續 HP_SETTLE 秒 —— 中途偶爾讀到一次 0
            #      不算，免得把還活著的怪丟掉。
            dead_by_hp = (self.packets and self._saw_hp and self.hp == 0
                          and now - self._zero_at >= HP_SETTLE)
            # ★ 屍體：鎖定這麼久了還**從來沒有**看到血量 —— 那是別人先打死的。
            #   搶怪嚴重的地方這種很多，實測 120 秒有 26 隻，白白鎖住 52 秒
            #   ＝ 44% 的時間在對屍體發呆。
            #   ⚠ 門檻是量出來的：活著的目標鎖定後看到血量的時間，
            #     中位 0.30 秒、**最久 2.22 秒**（34 隻），所以 3 秒很安全。
            #   ⚠ 只有封包模式能用：按鍵模式我們自己會把血量寫成 100，
            #     `_saw_hp` 一定是 True（見下面寫入那段）。
            # ⚠ 還沒送出選定封包就一直重設計時 —— 遊戲收到那一包才會填血量。
            #   少了這條，走過去的路上（還沒進攻擊範圍、還沒送選定）會把
            #   活著的怪全部當成屍體丟掉（使用者回報「明明有怪，過去看一下就跑」）。
            if not self.engaged:
                self._since = now
            corpse = (self.packets and not self._saw_hp
                      and now - self._since >= CORPSE_SECS)
            if self._wrote and (cur == 0 or dead_by_hp or corpse
                                or not entity.is_alive(self.sc, ent)):
                self._job = None
                self._wrote = False
                self.died.emit(ent.eid, not corpse)
                return

            if self.packets:
                # 只寫 ID。**不要寫血量** —— 那是遊戲用來回報死活的欄位，
                # 寫下去就把死亡訊號蓋掉了（見 entity.set_target_id）。
                entity.set_target_id(self.sc, state, ent.eid)
            else:
                # 按鍵攻擊才需要餵血量：遊戲攻擊前會檢查 +0x2DC > 0
                # （`cmp [esi+0x2dc],0 / jle 跳過`），而它每輪都會清回 0，
                # 所以**每一圈都要重寫**，不能「已經是這隻就跳過」。
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
        self._running = True

    def _inventory_head(self, pid: int, sc: MemoryScanner, now: float):
        """物品陣列表頭。★ 只讀快取，**絕不在這裡重找**。

        重找要跑 AOB 全掃，實測 1.7～2.1 秒／台。這條執行緒是怪物清單的關鍵路徑，
        五台排隊就是十秒的空窗（使用者回報的「搜尋會卡住」）。
        重找交給 InvWorker 那條獨立執行緒，找到再放進這個快取。
        """
        head = self._inv.get(pid)
        return head if head and inventory.is_valid(sc, head) else None

    def set_inventory_head(self, pid: int, head: int) -> None:
        """InvWorker 找到表頭後放進來，之後每輪掃描就直接帶出去。"""
        self._inv[pid] = head

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
                # ★ is_monster 現在看實體的「種類」欄位（見 entity.OFF_KIND），
                #   NPC、別人的寵物、召喚物都會被排掉。
                # ⛔ 曾經改用「遊戲的怪物名稱表」過濾 —— 不可靠，別再試：
                #    那個 vtable 是通用的字串清單容器（同一個 vtable 底下還有
                #    購買紀錄、裝備名稱、角色名），而且**不會跟著換地圖更新**
                #    （換到沼澤之後，表裡還是前一張圖的名字）。
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


class InvWorker(QThread):
    """專門找「物品指標陣列表頭」的執行緒（讀武器耐久要用）。

    ★ 為什麼要獨立一條：找表頭要跑 AOB 全記憶體掃描，實測 **1.7～2.1 秒／台**。
      放在怪物清單那條執行緒上，五台排隊就是十秒的空窗 —— 使用者回報的
      「殺怪殺一殺有時候搜尋會卡住」就是這個。
      （跟 watcher.py 把定位拆成 _Locator 是同一個道理，那次也是最有效的一招。）

    表頭本身很穩：實測 40 秒、五台，0 次失效。所以這條執行緒平時幾乎沒事做，
    只有開場各找一次，之後失效才重找。
    """

    found = Signal(int, object)                  # pid, head

    def __init__(self) -> None:
        super().__init__()
        self._want: dict[int, MemoryScanner] = {}
        self._at: dict[int, float] = {}
        self._running = True

    def request(self, pid: int, sc: MemoryScanner) -> None:
        """請它找（或重找）某台的表頭。有冷卻，不會一直重跑全掃。"""
        if time.monotonic() - self._at.get(pid, -99.0) >= INV_RELOCATE_GAP:
            self._want[pid] = sc

    def run(self) -> None:
        while self._running:
            if not self._want:
                self.msleep(200)
                continue
            pid, sc = next(iter(self._want.items()))
            del self._want[pid]
            self._at[pid] = time.monotonic()
            head = None
            try:
                hits = aob.scan(sc, aob.SKILL_EXP_BALL, limit=4096,
                                should_stop=lambda: not self._running)
                head = inventory.locate(
                    sc, {a - inventory.ITEM_BALL_OFF for a in hits})
            except Exception:                    # noqa: BLE001
                head = None
            if self._running and head:
                self.found.emit(pid, head)

    def stop(self) -> None:
        self._running = False
        self._want.clear()


class CharFarmPage(QWidget):
    """單一分身的掛機介面。"""

    def __init__(self, pid: int, hwnd: int, title: str, sc: MemoryScanner,
                 on_scan, tgt: TargetWorker, keys: KeyWorker,
                 notifier: Notifier | None = None,
                 account: str = "", char_name: str = "",
                 mode: str = MODE_PACKET, prefix: str = "farm") -> None:
        super().__init__()
        self.pid = pid
        self.hwnd = hwnd
        self.title = title
        self.sc = sc
        self.account = account
        self.char_name = char_name
        # 兩個分頁共用這個類別，只差攻擊方式；設定也要分開存（各自一份）。
        self.mode = mode
        self._prefix = prefix
        keys.mode = mode
        self._loading = True          # 載入設定期間不要反過來又存一次
        # 通知器每個分身一個，設定讀自己頁面上的那一列（使用者要求各自獨立）
        self._notifier = notifier or Notifier(
            self, "⚠ 自動掛機警報",
            lambda: ("telegram" if self.rb_tg.isChecked() else "sound",
                     self.tg_id.text()))
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
        self._path_pts = -1        # 尋路點數（-1=還沒算、1=直線通、>1=有地形）
        self._path_t = 0.0         # 距離上次問尋路過了多久
        self._way: list[tuple[float, float]] = []   # 上次算出的繞路路徑點
        self._unreach = 0          # 連續幾次尋路算不出路徑
        self._hurt = False         # 這隻有沒有被我們打傷過
        self._gone = 0             # 連續幾次掃描沒看到目標
        self._walked_ok = True     # 上次下移動指令有沒有成功
        self._moving = False       # 角色是不是正在走路（隔 MOVE_SAMPLE 取樣）
        self._move_ref: tuple[float, float] | None = None
        self._move_t = 0.0
        self._why = ""             # 沒在攻擊的原因（顯示在狀態列）
        self._hp_t = 0.0           # 距離上次檢查自己的 HP 過了多久
        self._hp = -1              # 最近讀到的 HP（給狀態列用）
        self._gear_t = 0.0         # 距離上次檢查武器耐久過了多久
        self._dura = (-1, -1)      # 最近讀到的 (耐久, 上限)
        self._mover: move.Mover | None = None
        self._walk_t = 0.0         # 距離上次下移動指令過了多久
        # 巡邏點：沒怪時依序走過去找怪（取代原本的單一「原點」）
        self._spots: list[tuple[float, float]] = []
        self._spot_i = 0           # 現在要去第幾個
        self._spot_pts = 1         # 上次往巡邏點算出的路徑點數
        self._last_hp = -1
        self._last_pos: tuple[float, float] | None = None
        # 已經打死（或判定走不過去）的實體 ID → 記下來的時間。
        # 怪死掉後物件不會馬上被回收，is_alive() 可能還是 true —— 沒這層擋著，
        # 換下一隻時會又挑到同一具屍體。
        # ⚠ 不能在每次重掃時清空：現在每秒都在刷新清單，一清就等於沒擋。
        #   改成保留 KILL_MEMORY 秒後自動淘汰（實體 ID 久了才可能被重用）。
        self._killed: dict[int, float] = {}

        root = QVBoxLayout(self)

        # 通知列（最上面）。★ 每個分身各自一份設定，不共用
        # —— 使用者要求：不同分身可能要通知到不同的地方。
        nbar = QHBoxLayout()
        nbar.addWidget(QLabel("通知方式"))
        self.rb_sound = QRadioButton("音效警報")
        self.rb_tg = QRadioButton("Telegram")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_sound)
        grp.addButton(self.rb_tg)
        self.rb_sound.setChecked(True)
        nbar.addWidget(self.rb_sound)
        nbar.addWidget(self.rb_tg)
        self.tg_id = QLineEdit()
        self.tg_id.setPlaceholderText("Telegram 群組/房間 ID")
        self.tg_id.setFixedWidth(220)
        nbar.addWidget(self.tg_id)
        self.test_btn = QPushButton("測試通知")
        self.test_btn.setToolTip("立刻送一則測試通知，確認設定會不會通。")
        self.test_btn.clicked.connect(
            lambda: self.notify("這是一則測試通知。"))
        nbar.addWidget(self.test_btn)
        nbar.addStretch(1)
        root.addLayout(nbar)

        # ★ 沒有「掃描周圍怪物」按鈕：分頁一開就會自動持續刷新（見 tick），
        #   所以「周圍怪物」永遠是即時的，也不必先掃過才能開始掛機。
        bar = QHBoxLayout()
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
        # ★ 沒有「封包攻擊」勾選框、也不顯示技能 ID —— 用哪種方式打由
        #   下面的「攻擊型態」決定（遠程送封包、近戰按鍵），技能 ID 是內部
        #   自己學的（清零 → 按選定的 Fx → 讀記憶體），使用者不需要看。
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
        mbar.addWidget(QLabel("攻擊型態"))
        self.type_box = QComboBox()
        self.type_box.addItem("遠程", False)
        self.type_box.addItem("近戰", True)
        self.type_box.setFixedWidth(80)
        self.type_box.setToolTip(
            f"遠程：送攻擊封包（距離 {ATTACK_PACKET_RANGE:.0f} 格內就送，"
            f"走到 {WALK_KEEP:.0f} 格內就停）。\n"
            f"近戰：用封包選定怪物，然後**狂按你選的那個 F 鍵**出手。\n"
            f"　　　我們只把角色帶到 {CLIENT_RANGE:.0f} 格內，剩下的射程與走位\n"
            "　　　全交給遊戲客戶端 —— 它知道每招要站多近，也會自己繞地形。\n"
            "⚠ 射程每個角色不一樣，近戰角色站太遠送攻擊封包伺服器不會理，\n"
            "　 症狀是站著不動、怪完全不掉血 —— 所以近戰交給客戶端比較穩。")
        mbar.addWidget(self.type_box)
        mbar.addSpacing(12)
        self.patrol_cb = QCheckBox("沒怪時去巡邏點找")
        self.patrol_cb.setToolTip(
            "追怪不限距離 —— 只要周圍還有選中的怪，多遠都會走過去打。\n"
            "**完全沒有**選中的怪時，才依序走去右邊的巡邏點找。")
        mbar.addWidget(self.patrol_cb)
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

        # 巡邏點：沒怪時依序走過去找怪（取代原本只有一個的「原點」）
        spot = QGroupBox("巡邏點")
        spot.setFixedWidth(190)
        sv = QVBoxLayout(spot)
        self.spot_list = QListWidget()
        self.spot_list.setFixedHeight(NEAR_HEIGHT)
        self.spot_list.setSelectionMode(QListWidget.ExtendedSelection)
        sv.addWidget(self.spot_list)
        srow = QHBoxLayout()
        # ⚠ 字別太長：主題給按鈕的 padding 是左右各 14px，
        #   「加入目前位置」要 108px，加上 X 鈕就超出區塊寬度被切掉（踩過）。
        add_btn = QPushButton("加入位置")
        add_btn.setToolTip("把角色現在站的位置加進巡邏點。")
        add_btn.clicked.connect(self._add_spot)
        srow.addWidget(add_btn)
        # 用 ASCII 的 X，不要用 ✕（部分中文字型沒有那個字形會變豆腐）
        rm_btn = QPushButton("X")
        rm_btn.setFixedSize(32, 32)
        rm_btn.setStyleSheet("padding: 0;")
        rm_btn.setToolTip("刪掉選起來的巡邏點")
        rm_btn.clicked.connect(self._remove_spots)
        srow.addWidget(rm_btn)
        sv.addLayout(srow)
        panes.addWidget(spot)
        root.addLayout(panes)

        self.status = QLabel("尚未掃描")
        self.status.setStyleSheet("color: #9aa2b8;")
        root.addWidget(self.status)
        root.addStretch(1)

        self._load_settings()
        self._loading = False
        self._wire_saving()

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
    # -- 巡邏點 --------------------------------------------------------
    def _refresh_spots(self) -> None:
        self.spot_list.clear()
        self.spot_list.addItems(
            [f"{n + 1}. ({x:.0f}, {y:.0f})"
             for n, (x, y) in enumerate(self._spots)])

    def _add_spot(self) -> None:
        p = self.my_pos()
        if p is None:
            self.status.setText("讀不到位置，請先按「掃描周圍怪物」")
            return
        self._spots.append(p)
        self._refresh_spots()
        self._save_settings()

    def _remove_spots(self) -> None:
        for row in sorted((self.spot_list.row(i)
                           for i in self.spot_list.selectedItems()),
                          reverse=True):
            del self._spots[row]
        self._spot_i = 0
        self._refresh_spots()
        self._save_settings()

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

    def _walk_toward(self, gx: float, gy: float, me, keep: float) -> int:
        """往 (gx,gy) 走，但在距離 keep 格處停下。有冷卻，不會狂送。

        走的是 move.Mover.walk_to()，它會先請**遊戲自己的尋路**算路徑，
        所以會繞過地形；太遠時自動縮短成中繼點，靠這裡定期重下接力走完
        （實測 85.9 格、8.5 秒到達）。

        回傳尋路算出的路徑點數（0 = 走不了，1 = 直線通，>1 = 中間有地形）。
        """
        if not self._ensure_mover():
            return 0
        d = math.hypot(gx - me[0], gy - me[1])
        if d <= keep:
            return 0
        r = (d - keep) / d                     # 只走到剩 keep 格的位置
        n = self._mover.walk_to(self.sc, self.player,
                                me[0] + (gx - me[0]) * r,
                                me[1] + (gy - me[1]) * r)
        # ★ 縮短版的落點走不了，就直接走去目標本身，讓**遊戲照它自己算的
        #   路徑繞過去**。
        #   ⚠ 這是複雜地形「不會繞路」的主因：遊戲明明算得出到那隻怪的
        #     5 點繞路路徑，但我們是走「直線上距離牠 keep 格的那個點」——
        #     那個幾何點常常正好落在地形裡，於是尋路失敗、角色站著不動，
        #     等於把遊戲算好的繞路丟掉了。
        if n <= 0:
            n = self._mover.walk_to(self.sc, self.player, gx, gy)
        self._walk_t = 0.0
        return n

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
        # 送鍵執行緒要的兩樣東西，跟著掃描一起更新（物件會搬家）：
        #   stats —— 學技能 ID 用（角色屬性基準 −0x50）
        #   pf    —— 三連包第①包的參數，**玩家物件 −8**（純讀取算得出來）
        self._keys.stats = s.stats
        self._keys.pf = move.pathfinder_this(self.sc) if s.player else None
        # 跳板可能是「自動走過去」那邊裝上的（比開始掛機晚），所以跟著更新
        self._keys.mover = self._mover if (
            self._mover is not None and self._mover.active) else None
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
        self._waiting = False

        if err:
            self.status.setText(f"⚠ {err}")
            return

        # ★ 正在打的那隻已經不在掃描結果裡 → 牠死了（或離開視野）。
        # 這是**獨立於血量的死亡訊號**：使用者回報「打死了卻還在選他」，
        # 原因是我們每輪把目標血量寫回 100，等於把遊戲的死亡訊號蓋掉了
        # （`read_target_hp() or 100` —— 死掉時讀到 0，`0 or 100` 就變 100）。
        # ⚠ 要**連續兩次**掃不到才算 —— 熱區掃描偶爾會漏掉一隻，
        #   單次就放棄會在打鬥中間換目標（使用者回報「打一下就換下一隻，
        #   結果一路結仇被打死」）。
        if (self.run_cb.isChecked() and self._cur is not None
                and not any(m.eid == self._cur.eid for m in self.mons)):
            self._gone += 1
            if self._gone >= GONE_SCANS:
                self._on_died(self._cur.eid)
                return
        else:
            self._gone = 0

        # 掛機中且正在等下一隻 → 自動挑名字在清單裡、離自己最近的接上去
        if self.run_cb.isChecked():
            if self._cur is None and not self._pick_next():
                self.status.setText(
                    f"附近沒有選中的怪了（已擊殺 {self._kills} 隻）→ 等新的出現…")
            return          # 掛機中的狀態列由 tick() 負責，別蓋掉
        # 沒在掛機時：掃描一直在背景跑，狀態列**不要**跟著一直重寫，
        # 只有內容真的變了才更新一次（不然又變成一直跳的那種）。
        msg = f"周圍 {len(self.mons)} 隻、{self.near.count()} 種"
        if self.status.text() != msg:
            self.status.setText(msg)

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
        # _killed 存的是「到期時間」，不是記錄時間 —— 因為兩種跳過的冷卻長度
        # 不一樣（確定打死 KILL_MEMORY、只是沒給血量 NOHP_MEMORY）。
        for eid, until in list(self._killed.items()):
            if now > until:
                del self._killed[eid]
        pool = []
        for m in self.mons:
            if m.name not in want or m.eid in self._killed:
                continue
            if not entity.is_alive(self.sc, m):
                continue
            # 座標要當場重讀：怪會走、角色也在走，掃描時記的早就過期了
            # ★ 不限距離：多遠的怪都收進來（使用者要求「想打多遠都可以」），
            #   排序後自然會先打最近的，遠的靠移動封包導航過去。
            p = entity.read_pos(self.sc, m.addr)
            d = (math.hypot(p[0] - me[0], p[1] - me[1])
                 if p and me else float("inf"))
            # ★ 正在打我的排前面（見 entity.OFF_FOE）—— 不先解決牠們的話，
            #   會一路結仇、被圍毆致死（使用者實際遇到）。
            foe = entity.attacking(self.sc, m, self.player)
            pool.append((0 if foe else 1, d, m))
        pool.sort(key=lambda t: (t[0], t[1]))
        if not pool:
            return False
        foe, d, self._cur = pool[0]      # 正在打我的優先，其次才是最近的
        self._stuck = 0.0
        self._path_pts = -1                       # -1 = 還沒算，tick() 會去問尋路
        self._path_t = PATH_GAP                   # 下一拍就問
        self._way = []
        self._unreach = 0
        self._hurt = False
        self._gone = 0
        self._walked_ok = True
        self._why = ""
        self._last_hp = -1
        self._last_pos = me
        self._atk.attack(self.state, self._cur)   # 寫入執行緒：開始鎖定這隻
        self._keys.eid = self._cur.eid            # 送封包時要指名打誰
        self._keys.set_on(True)                   # 攻擊執行緒：開始發動
        self.status.setText(
            f"鎖定「{self._cur.name}」　距離 {d:.1f} 格"
            + ("　⚔ 牠正在打我" if foe == 0 else "")
            + f"　累計擊殺 {self._kills}")
        return True

    def _on_died(self, eid: int, confirmed: bool = True) -> None:
        """攻擊執行緒回報目標沒了 —— 立刻從既有清單接下一隻。

        confirmed=False 代表只是「一直沒給血量」的推測（那隻可能還活著），
        所以只短暫冷卻，不要像確定打死那樣封鎖一分鐘。

        不重掃記憶體：重掃要 0.5 秒還要排隊，每殺一隻就等一次會非常卡。
        清單裡真的沒得打了，才由 tick() 去排重掃。
        """
        m = self._cur
        if confirmed:
            self._kills += 1
        # 免得又挑到同一具還沒回收的屍體（存到期時間，見 _pick_next）
        self._killed[eid] = time.monotonic() + (
            KILL_MEMORY if confirmed else NOHP_MEMORY)
        self._cur = None
        self._keys.eid = None                  # 別再對著屍體送封包
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
            self._keys.stop_learning()
            self._keys.eid = None
            self._atk.hold_off()
            self._cur = None
            # 若是被 _stop_with() 停的（例如角色死亡），它會在這之後蓋上原因
            self.status.setText(f"已停止（本次擊殺 {self._kills} 隻）")
            return
        # ★ 不再擋「還沒掃描」或「還沒選怪」：掃描本來就一直在背景跑，
        #   選中怪物也可以邊掛邊加。沒選到怪就只是不打而已 ——
        #   不需要用彈窗擋住使用者。
        want = self.wanted()
        self._kills = 0
        self._killed.clear()
        self._since_scan = RESCAN_GAP      # 清單裡挑不到的話，立刻重掃
        self._cur = None
        # ★ 每次開始都重學一次技能 ID —— 使用者隨時可能換掉那個鍵上的技能。
        #   學法：先把欄位清成 0，再按那個鍵，讀回來的非零值就是答案。
        #   學到之前用按鍵攻擊（本來就有效），所以不會空等。
        self._keys.stats = self.stats          # 清零要用，先確保是最新的
        self._keys.begin_learning()
        self._ensure_mover()               # 選怪／移動都要用它的跳板
        self._keys.mover = self._mover if (
            self._mover is not None and self._mover.active) else None
        self.status.setText(
            "掛機中：只打「" + "、".join(want) + "」" if want
            else "掛機中：還沒選任何怪物 —— 點右邊的名字加進「選中怪物」")

    def tick(self, dt: float) -> None:
        """UI 側的心跳：只做「挑目標、卡住偵測、更新狀態列」。

        寫目標與送鍵各自在 TargetWorker / KeyWorker 的執行緒上跑，節奏不受 UI
        影響 —— 原本整個迴圈掛在這裡，UI 一忙節奏就漂掉，感受就是「很卡」。
        """
        # ★ 掃描**一直都在跑**，不管有沒有在掛機 ——
        #   這樣「周圍怪物」永遠是即時的，使用者隨時可以把名字加進來，
        #   也不必先按什麼按鈕才能開始（掃描只掃熱區，很便宜）。
        self._since_scan += dt
        gap = (IDLE_SCAN_GAP if not self.run_cb.isChecked()
               else RESCAN_GAP if self._cur is None else REFRESH_GAP)
        if self._since_scan >= gap and not self._waiting:
            self._since_scan = 0.0
            self._waiting = True
            self._on_scan(self.pid)

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
        me = self.my_pos()

        # ★ 「角色正在走路嗎」—— 隔 MOVE_SAMPLE 秒比一次位置（見常數說明）。
        #   移動中一律不重下移動指令，否則會把多點路徑砍掉、原地來回。
        self._move_t += dt
        if self._move_t >= MOVE_SAMPLE:
            self._move_t = 0.0
            if me is not None and self._move_ref is not None:
                self._moving = math.hypot(me[0] - self._move_ref[0],
                                          me[1] - self._move_ref[1]) > MOVE_EPS
            self._move_ref = me

        if self._cur is None:
            # ★ 追怪不限距離；「周圍完全沒有選中的怪」時才去巡邏點找（使用者要求）
            if self.patrol_cb.isChecked() and self._spots and me:
                self._spot_i %= len(self._spots)
                sx, sy = self._spots[self._spot_i]
                d = math.hypot(me[0] - sx, me[1] - sy)
                if d <= SPOT_SLACK:
                    # 到了這個點還是沒怪 → 換下一個點繼續找
                    self._spot_i = (self._spot_i + 1) % len(self._spots)
                    self._spot_pts = 1
                    self._walk_t = WALK_GAP        # 下一拍就往新的點走
                    self.status.setText(
                        f"巡邏點 {self._spot_i or len(self._spots)} 沒怪"
                        f" → 前往下一個（共 {len(self._spots)} 點）")
                    return
                # ⚠ 回傳值不能丟掉：走不了（尋路算不出、或指令槽被佔）時什麼都
                #   不會發生，狀態列卻還寫著「前往」，看起來就像站著發呆。
                if not self._moving and self._walk_t >= WALK_GAP:
                    self._spot_pts = self._walk_toward(sx, sy, me, 0.0)
                self.status.setText(
                    f"周圍沒有選中的怪 → 前往巡邏點 {self._spot_i + 1}"
                    f"/{len(self._spots)} ({sx:.0f},{sy:.0f})　還有 {d:.0f} 格"
                    + ("　⛔ 算不出路徑（被地形擋住？）" if self._spot_pts == 0
                       else f"　⛰ 繞路 {self._spot_pts} 點"
                       if self._spot_pts > 1 else "　直線可通"))
            return

        m = self._cur
        hp = self._atk.hp

        mp = entity.read_pos(self.sc, m.addr)
        dist = math.hypot(mp[0] - me[0], mp[1] - me[1]) if (mp and me) else None
        # 施放封包要帶目標的格子座標 —— 順移那類對地技能沒有座標發不動
        if mp:
            self._keys.pos = (round(mp[0]), round(mp[1]))

        # 接近規則（改用封包攻擊後，接近**完全由我們自己走**，不靠按鍵）：
        #   ① 中間有障礙物（尋路點數 > 1）→ 走到怪臉上
        #      隔著牆時「直線距離近」是假的，站在原地打不到。
        #   ② 沒障礙物、超出攻擊範圍 → 走到範圍的九成
        #   ③ 進到範圍內才送三連包
        # ⚠ 讀不到座標（dist is None）算「不在範圍內」—— 位置不明就別亂送封包，
        #   那多半是怪的物件已經被回收了。
        #
        # ★ 有沒有障礙物**直接問遊戲的尋路函式**（只算不走，實測 5～6ms）：
        #   回 1 點就是直線通、多點就是要繞。挑到新目標時算一次即可，
        #   不必先走一步再看 —— 否則近距離隔著牆的怪會被當成「在範圍內」空打。
        # ⚠ 搶不到指令槽時會回 -1（攻擊執行緒正在送封包）。**絕不阻塞等待**，
        #   那是在 UI 執行緒上，一等畫面就凍住（使用者回報的「打一打卡住」）。
        #   維持 -1 = 還不知道，隔 PATH_GAP 再問一次就好。
        #
        # ⚠⚠ **只有這裡可以寫 _path_pts**。以前 _walk_toward() 的回傳值也會寫進來，
        #   但那是「走到某個中繼點」的路徑點數，跟「我跟這隻怪之間有沒有地形」
        #   根本是兩回事 —— 只要走過一次要繞路的路徑（點數 > 1），就會被當成
        #   隔著地形，攻擊距離縮成 2 格，於是 3~10 格的怪既不打、也走不到，
        #   一路卡到 10 秒逾時（監控實際抓到的距離 3.6 / 5.4 / 9.6 / 10.2）。
        # 怪會走動，所以每 PATH_GAP 重問一次，不是只在「還不知道」時問。
        self._path_t += dt
        if (self._path_t >= PATH_GAP and mp is not None and me
                and self._mover is not None and self._mover.active):
            self._path_t = 0.0
            n = self._mover.path_to(self.sc, mp[0], mp[1])
            if n >= 0:                 # -1 = 這次沒問到，保留上一次的判斷
                self._path_pts = n
                # ★ 緊接著把路徑點讀下來（下一次尋路就會覆蓋掉）。
                #   要繞路時就走去**倒數第二個點** —— 那個點到怪之間一定是
                #   直線（中間若有地形，尋路會再插一個轉折點），
                #   走到那裡就能無阻礙地打，不必一路擠到牠臉上。
                self._way = (self._mover.read_path(self.sc, n)
                             if n > 1 else [])
        # ⚠ blocked 只決定「要走多近」，**不能拿來擋攻擊**。
        #   之前寫成 `in_range = … and not blocked`，結果隔著地形的怪就算已經
        #   走到牠臉上（實測 1.1 格）也永遠不送封包 —— 角色走過去然後發呆，
        #   就是使用者回報的「走過去卻不打」「旁邊有怪也不打」。
        # ★ 近戰／遠程（使用者在頁面上選）：
        #   近戰 —— **選怪用封包、出手用按鍵**（狂按使用者選的那個 Fx）。
        #           射程與走位交給遊戲客戶端判斷，它知道每招要站多近；
        #           我們只負責走到怪身上，不自己算「要站多遠」。
        #   遠程 —— 維持原本：送施放封包，ATTACK_PACKET_RANGE 內就送、
        #           走到 WALK_KEEP 內就停。
        melee = bool(self.type_box.currentData())
        self._keys.mode = MODE_KEY if melee else self.mode
        blocked = self._path_pts > 1
        # ★ 近戰是「鎖定 + 狂按 Fx」，**走位由客戶端自己算** ——
        #   所以我們只要把角色帶到 CLIENT_RANGE 內就交給它，
        #   不要再自己往怪身上推（兩邊搶著下移動指令會互相打架）。
        #   隔著地形也不必特別處理：客戶端用的就是遊戲自己的尋路。
        reach = (CLIENT_RANGE if melee
                 else MELEE_RANGE if blocked else ATTACK_PACKET_RANGE)
        in_range = dist is not None and dist <= reach
        # ★★ 停止距離：近戰交給客戶端（帶到 CLIENT_RANGE 內就好），
        #   遠程停在 WALK_KEEP（11 格，使用者查到技能射程都是 12）。
        #   ⛔ 不要再試圖「自動問出射程」讓兩者共用一套 —— 四條路都失敗，
        #      而且使用者明確否決自己量測（有些技能是原地施放）。
        #      詳見記憶 client-attack-fn-deadend。
        keep = (CLIENT_RANGE if melee
                else MELEE_RANGE if blocked else WALK_KEEP)

        # ★ 尋路說「到不了」→ 換一隻（使用者定的規則）：牠站在走不進去的角落，
        #   我們走不過去、隔著地形也多半打不到，不必耗到 10 秒逾時。
        # ⚠ 只在尋路射程內才算數：更遠時尋路本來就回 0（一次只算得出約
        #   30~40 格），那是要接力走過去，不是到不了。
        # ⚠⚠ **已經打傷的怪絕不放棄**，而且要連續 UNREACH_HITS 次算不出來
        #   才算數 —— 怪會走動，打鬥中某一瞬間牠站到走不進去的格子，
        #   路徑就會變 0。少了這兩條會變成「打一下就換下一隻」，
        #   結果一路結仇、被圍毆致死（使用者實際遇到）。
        self._unreach = (self._unreach + 1) if self._path_pts == 0 else 0
        if (self._unreach >= UNREACH_HITS and not self._hurt
                and dist is not None and dist <= PATHFIND_RANGE):
            self._killed[m.eid] = time.monotonic() + KILL_MEMORY
            self._atk.hold_off()
            self._cur = None
            self._keys.eid = None
            if not self._pick_next():
                self._keys.set_on(False)
                self._since_scan = RESCAN_GAP
            self.status.setText(f"「{m.name}」走不到（卡在地形裡？）→ 換一隻")
            return
        # ★ 要繞路時，目標改成**路徑的倒數第二個點**（使用者的觀察）：
        #   那個點到怪之間一定是直線 —— 中間若有地形，尋路會再插一個轉折點。
        #   走那裡就不必一路擠到牠臉上，也不會在半路被地形卡住。
        # ⚠ 但那個點到怪可能還是超過攻擊範圍，所以再沿著**最後那段直線**
        #   往前推到剩 WALK_KEEP 格 —— 走到定位就直接打得到，不用多跑一趟。
        # ★ 近戰也走同一套：牠平常把走位交給客戶端，但隔著地形時客戶端
        #   常常卡住，這時由我們把牠帶到那個「看得到怪」的點最有效。
        gx, gy, gkeep = (mp[0], mp[1], keep) if mp else (None, None, keep)
        if blocked and len(self._way) >= 2 and mp:
            ax, ay = self._way[-2]
            seg = math.hypot(mp[0] - ax, mp[1] - ay)
            if seg > WALK_KEEP:            # 最後一段太長 → 沿著它再往前
                r = (seg - WALK_KEEP) / seg
                gx, gy = ax + (mp[0] - ax) * r, ay + (mp[1] - ay) * r
            else:
                gx, gy = ax, ay
            gkeep = 0.0
        gd = (math.hypot(gx - me[0], gy - me[1])
              if (me and gx is not None) else None)
        # ⚠ 走路的條件**不能再要求「不在範圍內」** —— 遊戲自己打怪時就是
        #   一邊走一邊打（雪狐那次攔到 6 包移動 + 4 包動作 + 3 包施放）。
        #   要求不在範圍內的話，近戰會站在 10 格外一直送打不到的施放封包。
        # 要超過 gkeep **再多 WALK_SLACK 格**才走 —— 沒有這個容差的話，
        # 角色停在定位附近時 gd 只比 gkeep 多一點點，每個 WALK_GAP 就再推
        # 一小步，看起來就是「打一打又往前一格」（使用者回報的不流暢）。
        # ⚠⚠ 但**打不到的時候容差要失效**，否則會出現死區：
        #   攻擊範圍 12 格、走路門檻 11+1.5=12.5 格 → 怪停在 12.3 格時
        #   既不打也不走，就是「朝一個方向發呆」（監控抓到 3 次，
        #   距離全是 12.3~12.4）。
        need_walk = gd is not None and (
            gd > gkeep + WALK_SLACK or (not in_range and gd > gkeep))
        if (self.move_cb.isChecked() and me and not self._moving
                and self._walk_t >= WALK_GAP and need_walk):
            # ⚠ 這個回傳值**不能**寫進 _path_pts —— 它是「走到中繼點」的路徑
            #   點數，不是「跟怪之間有沒有地形」（見上面那段說明）。
            self._walked_ok = self._walk_toward(gx, gy, me, gkeep) > 0

        # 兩條執行緒對「現在是不是用封包打」要有共識：
        # 寫目標那條要據此決定「寫不寫血量」——
        #   封包攻擊：**不寫**，血量交給遊戲，讀到 0 就是死亡訊號
        #   按鍵攻擊：**要寫**，遊戲出手前會檢查 +0x2DC > 0，不餵就不打
        self._atk.packets = bool(self._keys.mode == MODE_PACKET
                                 and self._keys.packets and self._keys.skill
                                 and self._keys.mover is not None)
        # 選定封包送出去之後，才開始算「多久沒看到血量 = 屍體」
        self._atk.engaged = self._keys.selected
        self._keys.set_on(in_range)

        # ★ 為什麼沒在打？把原因記下來給狀態列 —— 使用者回報「鎖定一隻怪發呆」，
        #   發呆一定是「不在範圍內、又沒有在走過去」，但成因有好幾種，
        #   直接標出來才不必猜。
        if in_range and dist is not None and dist <= keep:
            self._why = ""
        elif in_range:
            self._why = f"打得到，同時走近到 {keep:.0f} 格"
        elif dist is None:
            self._why = "⚠ 讀不到座標"
        elif not self.move_cb.isChecked():
            self._why = "⚠ 沒開「自動走過去」"
        elif self._mover is None or not self._mover.active:
            self._why = "⚠ 移動跳板沒裝上"
        elif not self._walked_ok:
            self._why = "⛔ 走不過去"
        elif blocked:
            self._why = (f"⛰ 隔著地形 → 沿路徑走到 ({gx:.0f},{gy:.0f})"
                         if gx is not None and len(self._way) >= 2
                         else "⛰ 隔著地形，走近一點")
        else:
            self._why = "→ 走進攻擊範圍"

        # ⛔ 這裡曾經加過「讀不到座標超過 N 秒就換一隻」—— 拿掉了。
        #    那是用 timeout 蓋過症狀，而且量過根本沒發生：
        #    掃描 100 輪，狀態與玩家物件**都是 100/100 剛好命中 1 個**。

        # 卡住偵測（次要保險，不是主要機制）：目標已經是最近的一隻，
        # 正常情況下不是打得到就是角色正在走過去。若血量不掉、玩家座標也不動，
        # 代表這隻走不過去（隔著地形之類），換一隻。
        # ⚠ 用上面那個「隔 0.3 秒取樣」的結果，不要拿相鄰兩拍比 ——
        #   心跳 10ms，角色每拍才走 0.09 格，那樣比永遠都是「沒在動」，
        #   於是走路途中也會一直累積卡住秒數，走到一半就被判定走不過去換怪。
        moving = self._moving
        if 0 < hp < self._last_hp:
            self._hurt = True          # 打傷過的怪就不要再放棄（見上面）
        if moving or (0 < hp < self._last_hp) or self._last_hp < 0:
            self._stuck = 0.0
        else:
            self._stuck += dt
        self._last_pos = me
        self._last_hp = hp
        if self._stuck >= STUCK_SECS:
            self._killed[m.eid] = time.monotonic() + KILL_MEMORY  # 走不過去
            self._atk.hold_off()
            self._cur = None
            self._keys.eid = None
            if not self._pick_next():
                self._keys.set_on(False)
                self._since_scan = RESCAN_GAP
            self.status.setText(
                f"「{m.name}」{STUCK_SECS:.0f} 秒沒進展（走不過去？）→ 換一隻")
            return

        # ⛔ 這裡不再每 0.2 秒重寫一次「掛機中：…」——
        #    使用者明講那行一直在跳、看了很煩。狀態列只保留**有事發生**時的
        #    訊息（換目標、走不過去、武器壞了、角色死亡、巡邏中…）。

    # ------------------------------------------------------------------
    # -- 設定的保存與載入（每個帳號各自一份）----------------------------
    #
    # ★ 用**帳號名**當 key，不能用 PID —— 玩家關掉重開遊戲 PID 就變了
    #   （見 [[memory-re-pitfalls]] 第 10 條）。
    # ★ 「開始掛機」刻意**不存**：使用者明講「那個每次都要是關閉的，
    #   因為我不能幫他打開」—— 程式一開就自己開打太危險。
    def _key(self, field: str) -> str:
        # ★ 前綴要跟分頁綁在一起，兩個掛機分頁的設定才不會互相覆蓋。
        return f"{self._prefix}.{self.account}.{field}"

    def _seed_from_default_tab(self) -> None:
        """這個帳號第一次用這個分頁 → 把「自動掛機」那邊的設定整份帶過來。

        ⚠ 沒有這段的話，新分頁對使用者來說就是「設定全不見了」——
          實際踩過：在新分頁掛機時角色不回原點，看起來像「沒在算路徑」，
          其實是那份設定裡原點是 null、「沒怪時回原點」也沒勾。
        帶過來之後才照常載入，所以使用者之後在這個分頁改的設定不受影響。
        """
        if self._prefix == "farm":
            return
        src = config.get(f"farm.{self.account}", None)
        if not isinstance(src, dict):
            return
        mine = config.get(f"{self._prefix}.{self.account}", None)
        mine = mine if isinstance(mine, dict) else {}
        changed = False
        for k, v in src.items():
            # 只補「沒有」或「存成 null」的欄位 —— 使用者在這個分頁明確設過的
            # （例如把「沒怪時回原點」關掉）就不要覆蓋回去。
            if mine.get(k, None) is None:
                config.set(self._key(k), v)
                changed = True
        if changed:
            config.save()

    def _load_settings(self) -> None:
        self._seed_from_default_tab()
        g = config.get
        for name in g(self._key("monsters"), []) or []:
            self._add_name(str(name))
        vk = g(self._key("vk"), DEFAULT_KEY)
        i = next((n for n, (_, v) in enumerate(SKILL_KEYS) if v == vk), None)
        if i is not None:
            self.key_box.setCurrentIndex(i)
        self.interval.setValue(float(g(self._key("interval"), DEFAULT_INTERVAL)))
        self.move_cb.setChecked(bool(g(self._key("move"), True)))
        self.patrol_cb.setChecked(bool(g(self._key("patrol"),
                                         g(self._key("back"), False))))
        self.type_box.setCurrentIndex(1 if g(self._key("melee"), False) else 0)
        # 巡邏點。舊版只有一個「原點」，有的話就當成第一個巡邏點帶過來。
        spots = g(self._key("spots"), None)
        if not spots:
            home = g(self._key("home"), None)
            spots = [home] if (isinstance(home, (list, tuple))
                               and len(home) == 2) else []
        self._spots = [(float(p[0]), float(p[1])) for p in spots
                       if isinstance(p, (list, tuple)) and len(p) == 2]
        self._refresh_spots()
        if g(self._key("notify"), "sound") == "telegram":
            self.rb_tg.setChecked(True)
        self.tg_id.setText(str(g(self._key("tg_id"), "") or ""))

    def _save_settings(self) -> None:
        if self._loading:
            return
        s = config.set
        s(self._key("monsters"), self.wanted())
        s(self._key("vk"), self.key_box.currentData())
        s(self._key("interval"), self.interval.value())
        s(self._key("move"), self.move_cb.isChecked())
        s(self._key("patrol"), self.patrol_cb.isChecked())
        s(self._key("melee"), bool(self.type_box.currentData()))
        s(self._key("spots"), [list(p) for p in self._spots])
        s(self._key("notify"), "telegram" if self.rb_tg.isChecked() else "sound")
        s(self._key("tg_id"), self.tg_id.text().strip())
        config.save()

    def _wire_saving(self) -> None:
        """所有設定一改就存 —— 不要讓使用者每次都重設一遍。"""
        self.picked.model().rowsInserted.connect(self._save_settings)
        self.picked.model().rowsRemoved.connect(self._save_settings)
        self.key_box.currentIndexChanged.connect(self._save_settings)
        self.interval.valueChanged.connect(self._save_settings)
        self.move_cb.toggled.connect(self._save_settings)
        self.patrol_cb.toggled.connect(self._save_settings)
        self.type_box.currentIndexChanged.connect(self._save_settings)
        self.rb_tg.toggled.connect(self._save_settings)
        self.tg_id.editingFinished.connect(self._save_settings)

    # ------------------------------------------------------------------
    def notify(self, msg: str) -> None:
        """送警報通知。設定是這個分身自己的（通知列在頁面最上面）。"""
        if self._notifier is None:
            return
        who = f"{self.account}（{self.char_name}）"
        note = self._notifier.fire(who, msg)
        self.status.setText(self.status.text() + f"　[{note}]")

    def _stop_with(self, msg: str) -> None:
        self.run_cb.setChecked(False)
        self.status.setText(msg)


class FarmTab(BaseTab):
    """自動掛機。

    出手方式由頁面上的「攻擊型態」決定（遠程送封包、近戰按鍵），
    所以不需要兩個分頁 —— 之前那個「自動練功按鍵」分頁已經移除。
    """

    TAB_TITLE = "自動掛機"
    ORDER = 5
    ATTACK_MODE = MODE_PACKET         # 頁面上的「攻擊型態」會覆寫這個
    SETTINGS_PREFIX = "farm"          # 設定存在 config 的哪個前綴底下

    def build_ui(self) -> None:
        self._pages: dict[int, CharFarmPage] = {}
        self._scanners: list[MemoryScanner] = []
        self._worker: ScanWorker | None = None
        self._inv: InvWorker | None = None    # 找物品陣列表頭（AOB 全掃，很慢）
        self._keys: list[_Paced] = []         # 每個分身：寫入執行緒 + 送鍵執行緒

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
            "①「周圍怪物」是即時的 —— 點名字就加進「選中怪物」"
            "（可加多種，也可自己打字後按 Enter，選起來按 X 可刪除，"
            "掛機中也能隨時加）→ ② 勾「開始掛機」。\n"
            "挑**離自己最近**的一隻打，打死立刻接下一隻。"
            "攻擊型態：遠程送攻擊封包、近戰改成按你選的那個 F 鍵。\n"
            "想固定範圍就按「加入目前位置」記幾個巡邏點再勾「沒怪時去巡邏點找」"
            "—— 周圍完全沒有選中的怪時會依序走過去找。\n"
            "設定會自動記住（每個分身各自一份），只有「開始掛機」每次都是關的。"
            "不搶視窗焦點、不占用你的鍵盤滑鼠。")
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
        self._inv = InvWorker()
        self._inv.found.connect(self._on_inv_found)
        self._inv.start(QThread.LowestPriority)
        for pid, hwnd, title, sc in insts:
            tgt, keys = TargetWorker(sc), KeyWorker(hwnd, sc)
            tgt.start(QThread.HighPriority)
            keys.start(QThread.HighPriority)
            self._keys += [tgt, keys]
            acct = charname.account_from_title(title)
            nm = charname.read_character_name(sc, acct) or acct
            # notifier 傳 None → 每個分頁自己建一個，讀自己那一列的設定
            page = CharFarmPage(pid, hwnd, title, sc, self._request_scan,
                                tgt, keys, None, acct, nm,
                                self.ATTACK_MODE, self.SETTINGS_PREFIX)
            page._notifier.failed.connect(self.found.setText)
            self._pages[pid] = page
            self.tabs.addTab(page, nm)
        self.found.setText(f"偵測到 {len(insts)} 個分身")

    def _request_scan(self, pid: int) -> None:
        page = self._pages.get(pid)
        if page is None or self._worker is None:
            return
        self._worker.request(pid, page.sc)

    def _on_scan_done(self, s: Scan) -> None:
        page = self._pages.get(s.pid)
        if page is None:
            return
        page.apply_scan(s)
        # 表頭還沒找到（或失效）就請 InvWorker 去找 —— 那是 AOB 全掃，
        # 絕不能放在怪物清單那條執行緒上。
        if s.inv is None and self._inv is not None:
            self._inv.request(s.pid, page.sc)

    def _on_inv_found(self, pid: int, head: int) -> None:
        if self._worker is not None:
            self._worker.set_inventory_head(pid, head)
        page = self._pages.get(pid)
        if page is not None:
            page.inv = head

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
        for th in [t for t in (self._worker, self._inv) if t] + self._keys:
            th.stop()
            th.wait(5000)
        self._worker = None
        self._inv = None
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
