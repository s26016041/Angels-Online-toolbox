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
import struct
import os
import time
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import config
from app.core import charname, injector, preload
from app.core import window as win
from app.core.memory import MemoryScanner
from app.core.notifier import Notifier
from app.game import (aob, attack, buff, channel, entity, inventory,
                      jumpmap, locate, monsters, move, navigate, player,
                      robot, scene)
from app.tabs.base_tab import BaseTab, fit_list, fit_spin, no_elide

# 設 AO_FARM_LOG=1 就會把每一秒的決策寫進 farm_debug_<帳號>.log。
# 平常是關的 —— 從外面看不到「為什麼不走」，只能靠這個。
_FARM_LOG = os.environ.get("AO_FARM_LOG") == "1"
SKILL_KEYS = [(f"F{i}", 0x6F + i) for i in range(1, 13)]   # F1=0x70 … F12=0x7B
DEFAULT_KEY = 0x71              # F2
# ★★ 出手間隔（**固定，不給使用者改**，使用者要求）。
# 為什麼是 0.1 秒：
#   · 攻擊技能一輪最快就是 0.5 秒（黑狐在用的技能 257：前置 0、後置 500ms；
#     全部法術裡「前置+後置」最多的一級就是 500ms，共 3223 個）。
#     再密也沒用，伺服器不收。
#   · 0.1 秒等於每個 0.5 秒的窗口試 5 下，封包排隊有抖動也不會錯過。
# ⛔ 不要再調到 0.02（黑狐原本的設定，每秒 50 次）：跳板的**指令槽只有一個**，
#    攻擊要跟移動、尋路搶，送太密會把移動指令卡在外面。
ATTACK_GAP = 0.1
DEFAULT_INTERVAL = ATTACK_GAP   # _Paced 的預設節奏
WRITE_INTERVAL = 0.02           # 秒；多久重寫一次目標＋檢查它死了沒（50 Hz）
SEND_TIMEOUT_MS = 60            # 送鍵最多等遊戲多久（正常 3.4ms，卡住就放棄這一拍）
TICK_MS = 10                    # UI 心跳
RESCAN_GAP = 0.3                # 沒得打了要多快重掃
IDLE_SCAN_GAP = 1.5             # 沒在掛機時也持續刷新「周圍怪物」的間隔
# 掛機時多久刷新一次怪物清單。熱區掃描實測約 28ms，所以可以一直刷。
# ★ 必須「一直刷」而不是「沒怪才刷」：跟別人搶怪時，清單一過期就會去打
#   別人已經殺掉的、或錯過剛生出來的那隻。
# 掛機中、已經有目標時的刷新間隔。熱區掃描實測 34~35ms，所以可以很密。
# ⚠ 刷新變快**不會讓屍體消失** —— 牠們是真的還在遊戲的實體清單裡
#   （中位賴 5 秒、最久 79.8 秒）。屍體是靠 Entity.dead 濾掉的，不是靠刷新。
#   加快只對「早一點看到新生出來的怪」有幫助，也就是搶怪。
# 0.3 → 0.15（使用者要求，為了搶贏別人）：掃一次 35ms，等於那條執行緒
# 約 23% 的時間在掃，它跑在 LowPriority 不會影響畫面。
REFRESH_GAP = 0.15
FULL_EVERY = 30.0               # 多久強制做一次全記憶體掃描當保險
INV_RELOCATE_GAP = 8.0          # 找不到物品陣列表頭時，多久才重試（要跑 AOB 全掃）
HP_CHECK_GAP = 0.5              # 多久確認一次自己還活著（死了就自動停）
GEAR_CHECK_GAP = 3.0            # 多久看一次武器耐久（掉得很慢，不必常看）
# ★ 交棒給天使精靈跑補給：多久看一次「回到原地圖了沒」、最多等多久就放棄。
#   一趟補給要回城、找 NPC、修裝、買東西、再走回來，慢的時候好幾分鐘。
SUPPLY_POLL = 5.0               # 使用者指定的間隔
SUPPLY_MAX_SECS = 600.0
JUMP_BACK_SECS = 180.0          # ★「用天使趴趴GO回地圖」等多久才傳（使用者指定 3 分鐘）
# ★「死亡自己回練功區」：死了倒數幾秒後用趴趴GO傳回去（使用者指定 10 秒）。
#   這 10 秒也是留給精靈的「陣亡自動復活」把人復活回城的時間。
DEATH_JUMP_SECS = 10.0
DEATH_POLL = 1.0                # 死亡回程模式下多久檢查一次（復活了沒、到了沒）
# 復活＋傳送＋回到地圖整趟最多等多久。超過就當流程斷了：停機並通知，
# 不要讓角色掛在城裡空等（使用者是掛網離開的，要叫得動他）。
DEATH_WAIT_MAX = 90.0
# 多久可以重下一次移動指令。★ 單次指令只走得到約 15 格（見 app/game/move.py
# 的 MAX_HOP），長距離是靠這裡定期重下、一段一段接力走完的，所以不能太久。
# ★ 下一次移動指令最少隔多久。**而且角色還在走時一律不下**（見 tick）。
# ⚠⚠ 這是「走不回原點」的真正原因：以前每 0.8 秒就重下一次指令，
#   把還沒走完的**多點路徑砍掉**。那條路徑常常是「先繞開再往前」，
#   我們正好在繞開那一段打斷它 → 回到原處又重算 → 無限來回，
#   看起來就像根本沒在算路徑。實測不打斷之後，176 格外的原點 42 秒就走回去了。
WALK_GAP = 0.4
# ★★ **趕路時**的指令冷卻。角色一停下來（動畫狀態變 'Wait'）就馬上下一段，
#   長距離才不會一頓一頓 —— 每段之間空等 0.5 秒，走 5 段就多花 2.5 秒。
# ⚠⚠ 這個值**一定要大於「指令排隊到真的執行」的時間**（實測 107~154ms）。
#   設 0.12 反而更糟：角色還沒開始動、狀態還是 'Wait'，我們就又送一次，
#   把上一個指令重置掉 —— 一路互相打斷，實測卡頓變成 0.47~0.51 秒。
#   0.3 秒留了兩倍餘裕；正常情況根本用不到（停下來的那一拍就直接送了），
#   它只是「送出去卻沒生效」時的重試間隔。
WALK_GAP_FAR = 0.30
# 離目標多遠算「還在趕路」。比這個近就是打鬥中的微調，維持原本 0.4 秒的節奏
# —— 遊戲自己打怪時也是 1 秒 1 包，微調不需要更密（密了反而搶指令槽）。
FAR_ENOUGH = 6.0
# 判斷「有沒有在移動」要隔一段時間再比位置。
# ⚠ 心跳是 10ms，而角色約 9 格/秒 = 每拍才 0.09 格 —— 拿相鄰兩拍比
#   幾乎永遠判定「沒在動」（卡住偵測也一起誤判，走路中照樣累積秒數）。
# 走路的到達容差：距離只超過目標一點點就不要再走了。
# 沒有它的話角色會在定位附近一直被推一小步（「打一打又往前一格」）。
WALK_SLACK = 1.5
# ⛔ 舊的「隔 MOVE_SAMPLE 秒比一次位置」已經拿掉 —— 改讀遊戲的動畫狀態
#    （entity.is_walking）。比位置最久要 0.3 秒才知道走完了，那 0.3 秒
#    加上指令冷卻就是使用者說的「長距離卡卡的」。
KILL_MEMORY = 60.0              # 打死的實體 ID 記多久（避免又挑到同一具屍體）
# 「一直沒給血量」而跳過的冷卻。那只是推測，牠可能還活著，所以比擊殺短。
# ⚠ 不能設 0：挑目標永遠挑最近的，冷卻 0 的話下一拍又挑到同一具，
#   變成無限迴圈。
# ⚠ 也不能太短：實測搶怪區 90 秒有 41 隻「沒給血量」的實體，其中 12 隻被
#   反覆挑到（從第一次到最後一次**中位相隔 13 秒**、最久 34 秒）——
#   冷卻 5 秒時 64 段裡有 23 段是重複挑同一具屍體。20 秒才擋得住。
NOHP_MEMORY = 20.0
# 三個清單的**最小**高度。★ 只是下限 —— 有空間時清單會自己長高填滿，
#   實際大多顯示 200px 以上。
# ⚠ 不能設太大：主視窗固定 940x700，最小高度加起來超過可視區的話，
#   整頁就會多一條垂直捲軸（130 時實測需要 599px、只有 556px 可用）。
NEAR_HEIGHT = 84
# 清單的上限。⚠ 少了它清單會一路長高，把下面的「手動輸入」欄和
#   「加入位置」按鈕擠出可視區（要捲才看得到）——
#   主視窗是固定 940x700，整頁高度就這麼多。
NEAR_MAX = 108
STUCK_SECS = 10.0               # 沒掉血、玩家也沒前進這麼久 → 這隻走不過去，換一隻
# ★★ 卡住偵測用「離錨點的**淨位移**」，不是「每 0.3 秒有沒有動」。
#   撞牆時角色會小幅抖動（實測約 0.5~0.6 格），剛好跨過舊的 MOVE_EPS(0.5)
#   門檻，於是每一拍都被當成「正在走路」，_stuck 永遠被歸零 ——
#   唯讀監控實拍**卡了 32 秒**都沒觸發（STUCK_SECS 是 10 秒）。
#   只要人還在這個半徑裡打轉，就一律算沒有前進。
# ⚠ 2.0 太小：實拍到角色在 (88.5,40.5) 與 (90.5,42.5) 之間來回，
#   相距 2.8 格，錨點一直被重設，卡了 35 秒還是沒觸發。
STUCK_EPS = 4.0
# ★★ 「走不到」的冷卻，**會遞增**。有兩種完全不同的走不到，一個數字擋不住：
#
#   · 暫時的 —— 角色站的地方剛好算不出那個方向。換個位置就好了。
#     用 60 秒（跟確定打死同長）的後果實拍到了：近處 12.6 / 13.0 格的怪
#     全部被自己冷凍，只剩 29.4 格外的可挑，追過去又失敗，越陷越深。
#
#   · 永久的 —— **那隻怪就站在尋路走不進去的格子上**。實測（黑狐）：
#     同一個位置對周圍每隻怪各問一次尋路，
#         曼陀羅怪菇 ×9（11.8~28.3 格）全部回 0
#         夜香食人花 ×4（12.8~28.6 格）全部回 1（直線可通）
#     跟距離無關，就是那一種怪站在走不到的地形上。
#     這種用 8 秒的話每 8 秒就白試一次。
#
#   所以：第一次 8 秒，同一隻再失敗就翻倍，上限 UNREACH_MAX。
#   暫時性的很快恢復，永久走不到的自動淡出。
UNREACH_MEMORY = 8.0
UNREACH_MAX = 120.0
# ⛔ 曾經有「正在打我的優先」（FOE_RANGE），已經拿掉 —— 見 _pick_next()。
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
# ★★ 射程其實**每個角色不一樣**（近戰 vs 遠程），上面那個 12 是在黑狐量的。
#   雪狐是近戰：牠自己打怪時會送一堆移動封包（使用者攔到 6 包），
#   靠客戶端走到怪身上才打得到。我們停在 10 格送施放，伺服器完全不理
#   —— 症狀就是「雪狐完全無法打怪」。
#   解法不寫死也不量測：**還沒打中過就一路走到貼臉**，
#   一旦看到怪掉血就把那個距離記起來，之後就不必再靠近。
#   近戰會收斂到 ~2 格、遠程第一隻之後就回到 10 格，兩邊都對。
# 學技能 ID：送一次鍵、隔 LEARN_GAP 讀一次，正常一次就拿到。
# 只有「這個角色登入後還沒放過任何技能」（欄位是 0）才會多試幾次。
LEARN_GAP = 0.25                # 按鍵到遊戲寫入要隔一幀，讀太密只是白讀
# 攻擊方式。⛔ 以前還有一個 MODE_KEY（「自動掛機（按鍵）」那個分頁），
#   分頁已經拿掉了，常數也跟著移除 —— 現在只剩封包這一種。
#   （KeyWorker 裡「不是封包模式就送鍵」那條路還在，當作封包送不出去時的退路。）
MODE_PACKET = "packet"
# ⛔ 不要用「補按技能鍵」來讓角色接近（試過，使用者實測還是會卡住）。
#    走過去打是客戶端的行為，但補按鍵會讓客戶端和我們的移動指令互相打架，
#    角色反而鎖著遠處的怪站著不動。接近一律用我們自己的尋路（見下面的 tick）。
# ★★ 比這個近就**不問尋路、也不算走不到**。
#   尋路到「貼在自己身上的怪」等於算路徑到自己腳下那一格，一定回 0，
#   而 0 在 tick 裡的意思是「走不過去」。近戰每打一隻都會貼上去，
#   於是每隻都被冷凍，只好去追遠處打不到的（實拍：周圍 1.2 格有怪，
#   卻鎖著 16.0 格外的那隻）。取 3.0：實拍貼身距離落在 1.0~2.1 格。
NO_PATH_NEED = 3.0
# ★ 近戰模式：走到 2 格以內才送攻擊封包（使用者指定）。
#   遠程角色維持原本的判斷（攻擊距離 12、走到 10 格內）。
#   為什麼要分：射程是每個角色不一樣的，近戰在 10 格外送施放伺服器不理
#   —— 實測雪狐就是這樣完全打不到怪。
MELEE_RANGE = 2.0
# ★ 自動分身按哪個鍵（使用者指定 F12）。技能編號不寫死 ——
#   按一次讀「角色屬性 −0x50」就知道，再去 skills.py 查持續時間。
BUFF_KEY = 0x7B                 # VK_F12
SPOT_SLACK = 3.0                # 走到離巡邏點這麼近就算到了，換下一個
# 多久讀一次「目前在哪張地圖」。換圖是很少發生的事，而心跳是 10ms 一拍，
# 每拍都讀等於白花 CPU（雖然一次只要 1.5ms）。
SCENE_SAMPLE = 0.5
# 靜態指標失效時，「全掃找場景物件」最快隔多久才准再做一次。
# ⚠ 那一掃 0.25 秒、跑在 GUI 執行緒上，沒有冷卻的話換地圖／重連期間
#   會每 SCENE_SAMPLE 掃一次，畫面直接卡住。跟 watcher 的重新定位同一個值。
SCENE_RELOCATE_GAP = 5.0


def _mmss(seconds: float) -> str:
    """剩餘時間，精確到秒（使用者要求）。一分鐘以內就只講秒。"""
    s = max(0, int(seconds))
    return f"{s // 60} 分 {s % 60:02d} 秒" if s >= 60 else f"{s} 秒"
# 自動巡迴換頻道：換完之後要停多久才恢復打怪。
# 客戶端重連實測約 1 秒，但重連後所有物件都會搬家，還要等掃描重新定位到，
# 所以留寬一點。這段期間不打怪、不下移動指令。
ROT_SETTLE = 5.0
# --- 血/魔不足時坐下回復 ---------------------------------------------------
VK_INSERT = 0x2D          # 遊戲裡按 Insert 切換坐下／站起
REST_FULL = 100           # 回到這個百分比才起身
REST_HP_DEFAULT = 50
REST_MP_DEFAULT = 30
# 送出 Insert 之後隔多久才准再送一次。坐下要一段時間才反映到記憶體
# （實測 91ms），太快重送會變成坐下→站起→坐下。
SIT_GAP = 1.5
# ⚠⚠ Insert 是**切換**不是「坐下」。所以「狀態跟預期不合就補送一次」很危險：
#   只要讀到一次假的「沒坐著」，那一送就把角色站起來，接著又讀到沒坐著、
#   再送、又坐下 —— 自己跟自己打架，畫面上就是不停坐下站起來（使用者回報）。
#   解法：狀態不合要**連續**這麼久才動作，短暫的抖動一律忽略。
SIT_CONFIRM = 1.2
# 休息判斷多久做一次。心跳是 10ms 一拍，每拍都去讀「誰在打我」等於每秒
# 幾千次記憶體讀取，沒必要。
REST_SAMPLE = 0.2
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

    def __init__(self, hwnd: int, sc: MemoryScanner) -> None:
        super().__init__(DEFAULT_INTERVAL)
        self.hwnd = hwnd
        self.sc = sc
        self.vk = DEFAULT_KEY
        self.mode = MODE_PACKET     # 這個分頁用哪種攻擊方式
        self.packets = True         # 使用者要不要用封包攻擊（封包模式才有意義）
        self.stats = None           # 角色屬性基準（學技能 ID 用）
        self.mover = None
        # move.pathfinder_this()：**玩家物件 −8**。
        # ⚠ 攻擊時**不用**這份快取（step() 會自己當場重算，見那裡的說明）；
        #   留著只是給自動分身當「玩家物件定位好了沒」的判斷用。
        self.pf = None
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

    def step(self) -> None:
        # ⚠⚠ 先把 GUI 執行緒會改的欄位抄成區域變數，整拍只看這份快照。
        #   踩點在 `_sel`：以前是「送 select(self.eid) → 成功 → self._sel =
        #   self.eid」，兩次 self.eid 之間 GUI 剛好換了目標的話，就把**新目標**
        #   記成「已選定」，可是那一包從來沒送出去。接著 `selected` 回 True →
        #   屍體計時開始跑 → 遊戲永遠不會替這隻填血量 → 0.8 秒後把一隻活怪
        #   當屍體丟掉。每殺一隻換一次目標，這個窗口每一輪都存在。
        eid, mover, vk = self.eid, self.mover, self.vk
        mode, packets, skill, pos = self.mode, self.packets, self.skill, self.pos
        try:
            if eid is None:
                self._sel = None       # 沒目標了，下一隻要重新送「選定」
            if not self._on:
                return
            # ① 換目標才送一次「選定」封包 —— 我們是直接寫記憶體選怪的，
            #    遊戲不會自己送這一包。兩種模式都要送。
            if mover is not None and eid and self._sel != eid:
                if not attack.select(mover, eid):
                    return                     # 這一拍先不打，下一拍再試
                self._sel = eid
            # ② 攻擊。封包模式送「動作 + 施放」，送不出去（或不是封包模式）
            #    就退回狂按那個鍵。
            #
            # ⚠⚠⚠ `pf`（玩家物件 −8）一定要**當場重算**，不能用掃描時存的。
            #   它是直接交給遊戲函式當 `this` 用的**裸指標**，遊戲會去解參考它
            #   （attack.py 開頭就寫著「傳錯會當場讓遊戲崩潰」）。而玩家物件會
            #   搬家：死亡重生、走傳送點、伺服器重連、官方精靈把人拉回城。
            #   以前只有 `apply_scan()` 會更新它，而那是 GUI 執行緒的事 ——
            #   掃描本身要 0.15~0.4 秒，GUI 又可能自己卡住零點幾秒，
            #   這條執行緒 10Hz 照送，等於**拿已經被釋放的指標去叫遊戲函式好幾次**。
            #   `pathfinder_this()` 是純讀取而且自己會驗證（沿著遊戲的管理表走，
            #   再比對物件 ID），算不出來就回 None —— 那時寧可退回送鍵。
            pf = move.pathfinder_this(self.sc) if mover is not None else None
            struck = (mode == MODE_PACKET and packets and skill
                      and eid and pf and mover is not None
                      and attack.strike(mover, pf, skill, eid, *pos))
            if not struck:
                _send_scan(self.hwnd, vk)
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
    # 我們手上的狀態物件位址已經不是狀態物件了（換地圖／重生／重連之後
    # 物件搬家）。UI 收到就把所有快取位址作廢並重新定位。
    stale = Signal()

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
        # ⚠⚠ 這幾個欄位是 GUI 執行緒隨時會改的（換目標、切模式）。
        #   一開始就抄成區域變數，整個 step() 只看這份快照 ——
        #   否則同一輪裡前後讀到不同的值，會算出前後矛盾的結論。
        packets, engaged = self.packets, self.engaged
        try:
            # ★ 先讀後判斷、最後才寫 —— 順序不能換。
            # 目標死掉時遊戲會把 +0x2D8 清成 0，那是我們唯一的死亡訊號；
            # 先寫回去就把訊號蓋掉了。
            # ★★ 同一次讀取順便確認「這個位址還是狀態物件」（比對 vtable）。
            #   物件搬家後這裡若不停手，就是每秒 50 次往別人的記憶體寫 4 bytes
            #   —— 遊戲會在很久以後莫名其妙掛掉。見 entity.read_target_checked。
            ok, cur, self.hp = entity.read_target_checked(self.sc, state)
            if not ok:
                self._job = None
                self._wrote = False
                self.stale.emit()          # 叫 UI 那邊重新定位
                return
            now = time.monotonic()
            # ★★ 最快也最準的死亡訊號：怪自己的動畫狀態變成 'Dead'。
            #   50Hz 就讀得到，不必等 HP_SETTLE(0.5s) 或 CORPSE_SECS(0.8s)。
            #   別人搶先殺掉我們鎖定的那隻時，這條會立刻放手去挑下一隻 ——
            #   搶怪的地方「對著空氣卡個零點幾秒」就是這樣來的。
            #   ⚠ confirmed 用 _saw_hp 而不是 True：沒看過血量代表我們根本
            #     沒交戰過（那是別人的屍體），算進擊殺數會灌水。
            #   ⚠ 不需要 self._wrote —— 我們有沒有寫過目標，跟牠死了沒無關。
            # ★ 死活與動畫狀態一次讀回來（相鄰欄位）：以前狀態一次、
            #   下面的 is_alive 又兩次，這是 50Hz 的迴圈。
            alive, st, _p = entity.read_live(self.sc, ent)
            if st == "Dead":
                self._job = None
                self._wrote = False
                self.died.emit(ent.eid, self._saw_hp)
                return
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
            dead_by_hp = (packets and self._saw_hp and self.hp == 0
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
            if not engaged:
                self._since = now
            corpse = (packets and not self._saw_hp
                      and now - self._since >= CORPSE_SECS)
            if self._wrote and (cur == 0 or dead_by_hp or corpse or not alive):
                self._job = None
                self._wrote = False
                self.died.emit(ent.eid, not corpse)
                return

            if packets:
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

    def _inventory_head(self, pid: int, sc: MemoryScanner):
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
                out.inv = self._inventory_head(pid, sc)
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
            # ⚠ 用 popitem() 而不是「先檢查再 next(iter(...))」：`stop()` 是
            #   GUI 執行緒呼叫的，它 clear() 的瞬間如果卡在 iter 與 next 之間，
            #   會丟 StopIteration／RuntimeError 把這條執行緒炸掉 ——
            #   關程式時 wait() 就白等了。
            try:
                pid, sc = self._want.popitem()
            except KeyError:
                self.msleep(200)
                continue
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
        tgt.stale.connect(self._on_state_stale)
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
        self._anchor = None        # 卡住偵測的錨點（淨位移超過就重設）
        self._dbg_t = 0.0          # 診斷紀錄的計時（見 _FARM_LOG）
        self._path_pts = -1        # 尋路點數（-1=還沒算、1=直線通、>1=有地形）
        self._path_t = 0.0         # 距離上次問尋路過了多久
        self._way: list[tuple[float, float]] = []   # 上次算出的繞路路徑點
        self._unreach = 0          # 連續幾次尋路算不出路徑
        self._hurt = False         # 這隻有沒有被我們打傷過
        self._gone = 0             # 連續幾次掃描沒看到目標
        self._walked_ok = True     # 上次下移動指令有沒有成功
        self._moving = False       # 角色是不是正在走路（讀動畫狀態，見 tick）
        self._why = ""             # 沒在攻擊的原因（顯示在狀態列）
        # 看過「怪在幾格外掉血」的最大值 = 這個角色真正打得到的距離。
        # 0 = 還沒看過任何一次掉血 → 先走到貼臉（近戰唯一打得到的距離）。
        self._hit_dist = 0.0
        self._hp_t = 0.0           # 距離上次檢查自己的 HP 過了多久
        self._gear_t = 0.0         # 距離上次檢查武器耐久過了多久
        self._dura = (-1, -1)      # 最近讀到的 (耐久, 上限)
        self._supply = False       # 正在讓天使精靈跑補給（我們完全讓開）
        self._supply_t = 0.0       # 這一趟補給跑多久了
        self._supply_poll = 0.0    # 距離上次檢查補給進度過了多久
        self._supply_scene = None  # 出發時人在哪張地圖（回到它才算補完）
        self._supply_left = False  # 已經離開過那張地圖了嗎
        self._supply_pos = None    # 出發時站在哪（挑最近的傳送落點用）
        self._dry = ""             # 哪一組藥水正見底（門閂：空著的期間只通知一次）
        self._supply_gen = 0       # 第幾趟補給（讓上一趟排的計時器自己作廢）
        self._robot_ours = False   # 精靈是我們開的（停手時要負責把自動攻擊關掉）
        # 死亡回程模式（勾了「死亡自己回練功區」，角色死掉才會進，見 _death_tick）
        self._death = False        # 進行中（我們完全讓開，等復活＋傳送）
        self._death_t = 0.0        # 死了多久（倒數 DEATH_JUMP_SECS、超時判斷共用）
        self._death_poll = 0.0     # 距離上次檢查過了多久
        self._death_scene = None   # 死在哪張地圖（趴趴GO要傳回去的目的地）
        self._death_pos = None     # 死在哪（同圖多落點時挑最近的）
        self._death_jumped = False # 傳送已送出，接下來只等回到地圖
        self._mover: move.Mover | None = None
        self._mover_failed = False   # 裝過一次失敗了就別每一拍重試
        self._walk_t = 0.0         # 距離上次下移動指令過了多久
        # 巡邏點：沒怪時依序走過去找怪（取代原本的單一「原點」）。
        # 每個點記 (x, y, 場景編號)；場景編號 None = 舊版存的、沒標記地圖。
        # ★ 座標在每張地圖都是從 0 開始算的格子，光看座標分不出是哪張圖 ——
        #   不記地圖就會拿 A 圖的點在 B 圖亂走（使用者要求：不同圖就不要去）。
        self._spots: list[tuple[float, float, int | None]] = []
        self._spot_i = 0           # 現在要去第幾個
        # ★ 走去巡邏點的導航器（會繞路、會判斷到不了），見 navigate.py
        self._nav = navigate.Navigator()
        # ★ 自動分身：時間到了自動補 F12 的 buff（見 app/game/buff.py）
        self._buff = buff.AutoBuff(BUFF_KEY)
        self._buff_note = ""     # 上次顯示過的補 buff 訊息（沒變就不重畫）
        # 目前所在地圖。心跳每 SCENE_SAMPLE 秒更新一次（見 _refresh_scene）。
        self._scene: int | None = None
        self._scene_t = SCENE_SAMPLE    # 第一拍就讀
        self._scene_obj: int | None = None   # 靜態指標失效時掃到的物件，快取起來
        self._scene_scanned = False          # 全掃備援只做一次，不要每次重掃
        self._scene_try = 0.0                # 上次全掃的時間（見 SCENE_RELOCATE_GAP）
        # 自動巡迴換頻道。從目前這一頻出發繞一圈再回來（在 3 頻 → 4,5,1,2,3）。
        self._rot_t = 0.0            # 距離上次開始巡迴過了多久
        self._rot_seq: list[int] = []   # 這一輪還沒去的頻道（依序）
        self._rot_wait = 0.0         # 這一頻還要待多久才換下一個
        self._rot_settle = 0.0       # 剛換完，還在重連／重新定位，先別打怪
        self._rot_home = 0           # 出發時在哪一頻（只給顯示用）
        self._rot_max = 0            # 這台伺服器有幾個分流（開始巡迴時讀）
        self._rot_last = ""          # 上次顯示的巡迴文字（沒變就不重畫）
        # 休息（血/魔不足 → 收尾 → 坐下 → 回滿 → 起身）
        self._rest = ""              # "" / "finish"（打完手上這隻）/ "sit"
        self._rest_why = ""          # 為什麼要休息，顯示用
        self._rest_key_t = SIT_GAP   # 距離上次送 Insert 過了多久
        self._hp_pct = self._mp_pct = 100.0
        self._sit_want = False       # 我「想要」坐著還是站著
        self._sit_bad_t = 0.0        # 實際狀態跟想要的不合已經持續多久
        self._rest_t = REST_SAMPLE   # 距離上次做休息判斷過了多久
        self._last_hp = -1
        # 已經打死（或判定走不過去）的實體 ID → 記下來的時間。
        # 怪死掉後物件不會馬上被回收，is_alive() 可能還是 true —— 沒這層擋著，
        # 換下一隻時會又挑到同一具屍體。
        # ⚠ 不能在每次重掃時清空：現在每秒都在刷新清單，一清就等於沒擋。
        #   改成保留 KILL_MEMORY 秒後自動淘汰（實體 ID 久了才可能被重用）。
        self._killed: dict[int, float] = {}
        # 「走不到」失敗過幾次（每隻怪各自算）—— 冷卻時間照這個翻倍。
        self._unreach_n: dict[int, int] = {}

        # ★ 內容放進可捲動區。**主視窗是固定 940x700**（見 main_window.py），
        #   分頁塞不下時 Qt 會硬把控制項壓到比最小尺寸還小 —— 那就是使用者
        #   看到的「字被切掉一半」。有捲軸就永遠不會壓，頂多要捲一下。
        #   其他分頁（記憶體掃描、收益監控、封包）本來就是這樣做的。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setSpacing(6)

        # 通知列（最上面）。★ 每個分身各自一份設定，不共用
        # —— 使用者要求：不同分身可能要通知到不同的地方。
        nbar = QHBoxLayout()
        # ★ 通知總開關（使用者要求）。關掉之後所有掛機通知都不送 ——
        #   下面那些「藥水沒了」「武器壞了」「角色死亡」都受它管。
        self.notify_cb = QCheckBox("啟用通知")
        self.notify_cb.setChecked(True)
        self.notify_cb.setToolTip(
            "掛機的通知總開關。關掉之後這些都不會通知：\n"
            "・角色死亡、武器耐久 0、藥水用完\n"
            "・回程補給失敗、走不到巡邏點\n"
            "★ 只影響通知，掛機該停還是會停（狀態列照樣寫原因）。")
        self.notify_cb.toggled.connect(self._save_settings)
        nbar.addWidget(self.notify_cb)
        nbar.addSpacing(10)
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

        # ★ 主開關自己一列，放在所有分類方框**上面** —— 它管的是整個頁面，
        #   塞進任何一個分類裡都不對，而且要一眼就找得到。
        self.run_cb = QCheckBox("開始掛機")
        self.run_cb.setStyleSheet("font-weight: bold;")
        self.run_cb.setToolTip(
            "勾選後開始：把「選中怪物」裡離自己最近的一隻寫進遊戲的目前目標，"
            "並持續送技能鍵。\n打死會自動接下一隻。取消勾選立刻停止。\n"
            "★ 開始的當下會自動把天使精靈的主開關「開啟天使守護精靈」打開\n"
            "　 （本來就開著就不動它；停止掛機也不會幫你關）。")
        self.run_cb.toggled.connect(self._on_toggle)
        root.addWidget(self.run_cb)

        # ★ 依分類分組（使用者要求）：原本 5 條平鋪的橫列很難一眼看懂哪個是
        #   哪個，改成有標題的方框、排成兩欄，垂直空間也省下來給下面的清單。
        # ★ 沒有「掃描周圍怪物」按鈕：分頁一開就會自動持續刷新（見 tick），
        #   所以「周圍怪物」永遠是即時的，也不必先掃過才能開始掛機。
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)

        # ── 攻擊 ────────────────────────────────────────────
        g_atk = QGroupBox("攻擊")
        a = QHBoxLayout(g_atk)
        a.addWidget(QLabel("技能鍵"))
        self.key_box = QComboBox()
        for label, vk in SKILL_KEYS:
            self.key_box.addItem(label, vk)
        self.key_box.setCurrentIndex(
            next(i for i, (_, vk) in enumerate(SKILL_KEYS) if vk == DEFAULT_KEY))
        # 照內容自動調寬（F10~F12 比 F2 寬）
        self.key_box.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.key_box.setToolTip("要一直按的攻擊／技能鍵。")
        self.key_box.currentIndexChanged.connect(
            lambda i: setattr(self._keys, "vk", self.key_box.itemData(i)))
        a.addWidget(self.key_box)
        # ⛔ 「每隔幾秒」的輸入框拿掉了（使用者要求固定，不給輸入）——
        #    見 ATTACK_GAP 的說明，那個值是照技能施法週期算出來的。
        self._keys.set_interval(ATTACK_GAP)
        a.addSpacing(10)
        # ★ 只打王：勾起來就**完全不看「選中怪物」的名字**，改成看種類 ID
        #   是不是王（見 app/game/monsters.py）。名字比對分不出來 ——
        #   有 20 種怪同名卻一個是王一個不是（哥布林幹部 78 / 663王…）。
        self.boss_cb = QCheckBox("只打王")
        self.boss_cb.setToolTip(
            "勾起來之後**不看左邊「選中怪物」的名字**，改成只打「王」。\n"
            "等於自動把周圍所有的王加進選中清單，而且只加王。\n"
            "\n"
            "王是讀遊戲自己的怪物資料判斷的（種類 ID → 王旗標），\n"
            "不是靠名字猜 —— 有 20 種怪同名卻一個是王一個不是。\n"
            "\n"
            "⚠ 不分等級：周圍出現的**任何**王都會打。真的很硬的王打不贏時，\n"
            "　 角色一死掛機會自動停下來，但還是留意一下。")
        a.addWidget(self.boss_cb)
        a.addSpacing(10)
        # ★ 自動分身：時間快到了自動補 F12
        self.buff_cb = QCheckBox("自動分身")
        self.buff_cb.setToolTip(
            "⚠⚠ **技能要自己先放到遊戲的 F12**。\n"
            "　　 這裡只認 F12，放在別的鍵不會動。\n"
            "⚠　 F12 要放**對象是「自己」**的技能（按了就直接生效）；\n"
            "　　 對象是「角色」的要先選人，這裡不處理。\n"
            "⚠　 **要開「開始掛機」才有作用**，單獨勾這個沒有用。\n"
            "\n"
            f"時間到就自動補：剩不到 {buff.LEAD:.0f} 秒再放一次。\n"
            "・每次開始掛機（或打到一半才勾）都會**先無腦放一次**，\n"
            "　 不去偵測身上還有沒有。\n"
            "・補的時候送**封包**，不占鍵盤、也不必停下腳步。\n"
            "・持續時間讀遊戲資源包（黑狐的 F12 是技能 5424，20 分鐘）。\n"
            "・只有**第一次**要按一下 F12 認技能編號，認到就記住，\n"
            "　 之後連重開程式都不用再按。\n"
            "\n"
            "⚠ 被驅散或施法被打斷我們不會知道，最壞情況是斷到下次補的空窗。")
        self.buff_cb.toggled.connect(self._save_settings)
        a.addWidget(self.buff_cb)
        a.addStretch(1)
        grid.addWidget(g_atk, 0, 0)

        # ── 移動與巡邏 ──────────────────────────────────────
        g_mov = QGroupBox("移動與巡邏")
        m = QHBoxLayout(g_mov)
        self.move_cb = QCheckBox("自動走過去")
        self.move_cb.setChecked(True)
        self.move_cb.setToolTip(
            "半徑內沒有選中的怪時，主動走到最近那隻旁邊。\n"
            "⚠ 這項功能需要在遊戲行程裡執行一小段程式碼（掛 PeekMessageA），\n"
            "　 才能呼叫遊戲自己的移動函式 —— 純讀寫記憶體做不到移動。")
        m.addWidget(self.move_cb)
        m.addSpacing(10)
        m.addWidget(QLabel("接戰距離"))
        self.range_spin = QDoubleSpinBox()
        self.range_spin.setRange(1.0, 15.0)
        self.range_spin.setSingleStep(0.5)
        self.range_spin.setDecimals(1)
        self.range_spin.setValue(WALK_KEEP)
        fit_spin(self.range_spin)
        self.range_spin.setToolTip(
            "追到怪之後最遠停在幾格 —— 也就是「不用再靠近」的門檻。\n"
            "\n"
            "遠程角色：10～12（技能射程實測 12 格）。\n"
            "近戰角色：調小到 1～2，不然站太遠伺服器不受理施放，\n"
            "　　　　　症狀是站著不動、怪完全不掉血。\n"
            "\n"
            "⚠ 送攻擊封包仍固定在 12 格內，不受這個設定影響。\n"
            "⚠ 隔著地形時一律貼臉走過去，也不受這個設定影響。")
        m.addWidget(self.range_spin)
        m.addWidget(QLabel("格"))
        m.addSpacing(10)
        # ⚠ 字數就是版面寬度：這個勾選框每多一個字，整個右欄就多 13px，
        #   超過視窗（固定 940）就會被推到水平捲軸外面。完整說明在滑鼠提示。
        self.patrol_cb = QCheckBox("沒怪去巡邏點")
        self.patrol_cb.setToolTip(
            "追怪不限距離 —— 只要周圍還有選中的怪，多遠都會走過去打。\n"
            "**完全沒有**選中的怪時，才依序走去右邊的巡邏點找。\n"
            "★ 只走「目前這張地圖」的巡邏點；這張圖一個點都沒有就原地不動。\n"
            "　 同一張圖的不同分流算同一張，會照走。")
        m.addWidget(self.patrol_cb)
        m.addStretch(1)
        grid.addWidget(g_mov, 0, 1)

        # ── 坐下休息 ───────────────────────────────────────
        # ★ 血/魔不足就坐下回復。流程：低於門檻 → **先把手上的怪打完、
        #   確認沒有怪在打我** → 坐下 → 回到 100% → 起身繼續。
        #   「要先清乾淨才坐」是使用者明確要求的，理由很實際：
        #   坐著被怪打會死（這個專案有前科，測試時留著怪沒殺，角色被打死）。
        g_rest = QGroupBox("坐下休息")
        s = QHBoxLayout(g_rest)
        self.rest_hp_cb = QCheckBox("HP 低於")
        self.rest_hp_cb.setToolTip(
            "血量低於這個百分比就停下來坐著回血，回到 100% 再繼續。\n"
            "⚠ 不會馬上坐 —— 會先把手上的怪打完、確認沒有怪在打你。")
        self.rest_hp_cb.toggled.connect(self._save_settings)
        s.addWidget(self.rest_hp_cb)
        self.rest_hp = QSpinBox()
        self.rest_hp.setRange(1, 99)
        self.rest_hp.setValue(REST_HP_DEFAULT)
        fit_spin(self.rest_hp)
        self.rest_hp.valueChanged.connect(self._save_settings)
        s.addWidget(self.rest_hp)
        s.addWidget(QLabel("%"))
        s.addSpacing(10)
        self.rest_mp_cb = QCheckBox("MP 低於")
        self.rest_mp_cb.setToolTip(
            "魔力低於這個百分比就停下來坐著回魔，回到 100% 再繼續。\n"
            "⚠ 同樣會先把手上的怪打完才坐。")
        self.rest_mp_cb.toggled.connect(self._save_settings)
        s.addWidget(self.rest_mp_cb)
        self.rest_mp = QSpinBox()
        self.rest_mp.setRange(1, 99)
        self.rest_mp.setValue(REST_MP_DEFAULT)
        fit_spin(self.rest_mp)
        self.rest_mp.valueChanged.connect(self._save_settings)
        s.addWidget(self.rest_mp)
        # ⚠ 這裡曾經寫「%　回滿再繼續」，那五個字讓「坐下休息」要 499px，
        #   兩欄加起來就超過視窗寬度（固定 940）→ 整個右欄被推到捲軸外面。
        #   「回滿再繼續」滑鼠提示裡本來就有寫，拿掉不會少資訊。
        s.addWidget(QLabel("%"))
        self.rest_lbl = QLabel("")
        self.rest_lbl.setStyleSheet("color: #9aa2b8;")
        s.addWidget(self.rest_lbl)
        s.addStretch(1)
        grid.addWidget(g_rest, 1, 0)

        # ── 巡迴換頻道 ─────────────────────────────────────
        # ★ 從目前這一頻出發繞一圈再回到原本那一頻。
        #   在 3 頻就是 3 → 4 → 5 → 1 → 2 → 3。切換靠 app/game/channel.py
        #   （跟選定怪物同一個遊戲函式，只差種類碼）。
        g_rot = QGroupBox("巡迴換頻道")
        r = QHBoxLayout(g_rot)
        self.rot_cb = QCheckBox("自動換頻")
        self.rot_cb.setToolTip(
            "從**目前這一頻**出發，依序換過每一頻再回到原本那一頻。\n"
            "例：現在在 3 頻 → 4 → 5 → 1 → 2 → 3（共 5 次切換）。\n"
            "\n"
            "換頻道期間會暫停打怪幾秒（要等重連＋重新定位），\n"
            "停留那段時間照常掛機。")
        r.addWidget(self.rot_cb)
        r.addWidget(QLabel("每"))
        self.rot_every = QDoubleSpinBox()
        self.rot_every.setRange(1.0, 600.0)
        self.rot_every.setSingleStep(5.0)
        self.rot_every.setDecimals(0)
        self.rot_every.setValue(30.0)
        fit_spin(self.rot_every)
        self.rot_every.setToolTip("隔多久開始新的一輪巡迴（從上一輪開始算）。")
        r.addWidget(self.rot_every)
        r.addWidget(QLabel("分一輪，每頻"))
        self.rot_stay = QDoubleSpinBox()
        self.rot_stay.setRange(5.0, 3600.0)
        self.rot_stay.setSingleStep(10.0)
        self.rot_stay.setDecimals(0)
        self.rot_stay.setValue(60.0)
        fit_spin(self.rot_stay)
        self.rot_stay.setToolTip(
            "換到一頻之後待多久才換下一頻。這段時間照常打怪。\n"
            "⚠ 最少 5 秒 —— 客戶端重連要 1~2 秒，太短會一直在換線。")
        r.addWidget(self.rot_stay)
        r.addWidget(QLabel("秒"))
        self.rot_lbl = QLabel("")
        self.rot_lbl.setStyleSheet("color: #9aa2b8;")
        r.addWidget(self.rot_lbl)
        r.addStretch(1)
        grid.addWidget(g_rot, 1, 1)

        # ── 回程補給（整列）─────────────────────────────────
        # ★ 條件到了就交棒給官方的天使守護精靈跑補給（回城→修裝→買水→回戰場），
        #   補完再自己接回來。打怪還是我們的掛機在做。
        # ★ 三個獨立的觸發開關。**不看遊戲裡精靈自己的回城勾選**
        #   （使用者要求），這樣我們的觸發跟官方互相獨立。
        g_sup = QGroupBox("回程補給")
        c = QHBoxLayout(g_sup)
        common = (
            "\n\n觸發之後：停掉我們的自動戰鬥 → 開精靈 → 回程 →"
            "\n它修裝買東西 → **回到原本那張地圖**就自動接回自動戰鬥。"
            f"\n（每 {SUPPLY_POLL:.0f} 秒檢查一次回到原地圖了沒）"
            "\n★ 開了自動攻擊之後如果發現地圖已經自己變了，代表客戶端"
            "\n　 已經自己跑回程，我們就不再送一次，避免跟官方打架。")
        potion = (
            "\n・藥水是哪一種，讀「天使輔助精靈」那頁你設的，換藥水自動跟著換"
            "\n・同一組配了好幾格時要**全部**歸零才算（還有一格有貨就不算）"
            "\n・數量是全背包加總（同一種水散在好幾格會加起來算）"
            "\n・那格設成技能的話整組跳過 —— 用技能補本來就不吃藥水"
            "\n⚠ 不看遊戲裡「補HP/MP物品用完自動回城」有沒有勾。"
            "\n★ 水沒了一定會通知，**沒勾這裡也會通知**，只是不跑補給。")
        self.sup_gear_cb = QCheckBox("武器壞掉")
        self.sup_gear_cb.setToolTip("武器耐久歸零就跑回程補給。" + common)
        self.sup_gear_cb.toggled.connect(self._save_settings)
        c.addWidget(self.sup_gear_cb)
        c.addSpacing(10)
        # ★ HP / MP **分開兩個開關**（使用者要求）：勾 HP 就只看 HP 那一組
        #   全歸零，MP 有沒有水完全不影響，反之亦然。
        self.sup_hp_cb = QCheckBox("HP 藥水沒了")
        self.sup_hp_cb.setToolTip(
            "「天使輔助精靈」那頁設的 **HP 藥水整組**用完就跑回程補給。"
            "\n（MP 有沒有水不影響這一項）" + potion + common)
        self.sup_hp_cb.toggled.connect(self._save_settings)
        c.addWidget(self.sup_hp_cb)
        self.sup_mp_cb = QCheckBox("MP 藥水沒了")
        self.sup_mp_cb.setToolTip(
            "「天使輔助精靈」那頁設的 **MP 藥水整組**用完就跑回程補給。"
            "\n（HP 有沒有水不影響這一項）" + potion + common)
        self.sup_mp_cb.toggled.connect(self._save_settings)
        c.addWidget(self.sup_mp_cb)
        c.addSpacing(10)
        # ★ 不等精靈自己走回來，時間到就用遊戲的「天使趴趴GO」直接傳回去。
        self.sup_jump_cb = QCheckBox("用天使趴趴GO回地圖")
        self.sup_jump_cb.setToolTip(
            f"觸發回程補給之後 **{JUMP_BACK_SECS / 60:.0f} 分鐘**，直接用"
            "「天使趴趴GO」傳回原本那張地圖，不再等精靈自己走回來。\n"
            "・傳完就照原本的流程接回自動戰鬥\n"
            "・同一張地圖有好幾個落點時，挑**離你出發位置最近**的那個\n"
            "・時間到時如果人已經回到原地圖了就不傳\n"
            "⚠ 走的是遊戲自己的傳送封包，跟你手動開趴趴GO按下去完全一樣，\n"
            "　 所以**傳送費用、等級限制**照樣算。\n"
            "⚠ 那張地圖不在趴趴GO清單裡就不傳（會在狀態列說一聲）。\n"
            "★ 勾了這個，**開始掛機時**會自動把精靈補給頁的\n"
            "　 「使用標記傳送捲軸回練功點」**取消**掉 —— 回地圖已經由\n"
            "　 趴趴GO負責，精靈再燒一張捲軸只是浪費道具。\n"
            "　 之後你在遊戲裡改回去我們就不再管；取消勾選也不會幫你勾回去。")
        self.sup_jump_cb.toggled.connect(self._on_robot_pref)
        c.addWidget(self.sup_jump_cb)
        c.addSpacing(10)
        # ★ 死亡自己回練功區：把精靈「輔助」頁調成 陣亡自動復活＋回城。
        #   DATAID 與「回城 = 1」的由來見 app/game/robot.py 的 AS_AUTO_REVIVE。
        self.sup_revive_cb = QCheckBox("死亡自己回練功區")
        self.sup_revive_cb.setToolTip(
            "角色死掉也不停止掛機，自動回來繼續打：\n"
            "・開始掛機時自動把天使精靈「輔助」頁調好 ——\n"
            "　「陣亡時自動復活」勾起來、「復活方式」選成「回城」\n"
            f"・死亡 **{DEATH_JUMP_SECS:.0f} 秒後**（等精靈把人復活回城）\n"
            "　 用天使趴趴GO傳回死掉時那張地圖，回到就自動接回自動戰鬥\n"
            "・沒勾的話維持原樣：角色死亡就自動停止掛機\n"
            "\n"
            "・掛機中才勾也會立刻把精靈設定調好\n"
            "・取消勾選不會幫你把遊戲裡的精靈設定改回來\n"
            "⚠ 趴趴GO的**傳送費用**照算；那張地圖不在趴趴GO清單裡、\n"
            "　 或復活／傳送一直沒完成，就停止掛機並發通知。\n"
            "⚠ 精靈面板開著的話「復活方式」下拉的**文字**可能沒立刻跟上，\n"
            "　 重點一次就會顯示對，實際行為一律以設定值為準。")
        self.sup_revive_cb.toggled.connect(self._on_robot_pref)
        c.addWidget(self.sup_revive_cb)
        c.addStretch(1)
        grid.addWidget(g_sup, 2, 0, 1, 2)

        # ⛔ 不要把兩欄硬拉成一樣寬（setColumnStretch(0,1)+(1,1)）——
        #    「坐下休息」需要 499px、「攻擊」只要 413px，硬均分會讓前者被切掉
        #    半個字。讓每一欄照自己的內容決定寬度就好。
        # 每個方框的上下留白縮一點 —— 主視窗固定 940x700，整頁高度很吃緊，
        # 預設留白會讓整頁多出一條垂直捲軸。
        for _lay in (a, m, s, r, c):
            _lay.setContentsMargins(12, 6, 12, 6)
        root.addLayout(grid)

        # 三個清單。★ 寬度一律照「內容需要多少」給，不寫死 ——
        # 使用者要求所有文字都要完整顯示，不能被切掉。
        # 一律只顯示中文名字 —— 比對也是用名字，所以手動打字才會通。
        panes = QHBoxLayout()

        left = QGroupBox("選中怪物")
        lv = QVBoxLayout(left)
        self.picked = QListWidget()
        self.picked.setMinimumHeight(NEAR_HEIGHT)
        self.picked.setMaximumHeight(NEAR_MAX)
        self.picked.setSelectionMode(QListWidget.ExtendedSelection)
        no_elide(self.picked)
        lv.addWidget(self.picked)
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("手動輸入後按 Enter")
        self.manual.returnPressed.connect(self._add_manual)
        lv.addWidget(self.manual)
        # 「選中怪物」放得下最長的怪物名（實際資料裡最長 8 個字）
        fit_list(left, self.picked, "曼陀羅怪菇菌絲體")
        panes.addWidget(left)

        # 中間的刪除鈕：把「選中怪物」裡選起來的移除
        midw = QWidget()
        mid = QVBoxLayout(midw)
        mid.setContentsMargins(0, 0, 0, 0)
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
        panes.addWidget(midw)

        right = QGroupBox("周圍怪物")
        rv = QVBoxLayout(right)
        self.near = QListWidget()
        self.near.setMinimumHeight(NEAR_HEIGHT)
        self.near.setMaximumHeight(NEAR_MAX)
        self.near.setSelectionMode(QListWidget.ExtendedSelection)
        no_elide(self.near)
        # ⚠ 用 UserRole 的名字，不要用顯示文字 —— 王的前面有「👑 」字首。
        self.near.itemClicked.connect(
            lambda it: self._add_name(it.data(Qt.UserRole) or it.text()))
        rv.addWidget(self.near)
        # 「周圍怪物」多一個「【王】」字首
        fit_list(right, self.near, "【王】曼陀羅怪菇菌絲體")
        panes.addWidget(right, 1)

        # 巡邏點：沒怪時依序走過去找怪（取代原本只有一個的「原點」）
        spot = QGroupBox("巡邏點")
        # 每一列是「編號. 地圖名 (x, y)」，最長的地圖名有 7 個字
        # （專家級遺落之地／史萊姆晴空牧場）—— 太窄會被切掉。
        sv = QVBoxLayout(spot)
        self.spot_list = QListWidget()
        self.spot_list.setMinimumHeight(NEAR_HEIGHT)
        self.spot_list.setMaximumHeight(NEAR_MAX)
        self.spot_list.setSelectionMode(QListWidget.ExtendedSelection)
        no_elide(self.spot_list)
        sv.addWidget(self.spot_list)
        srow = QHBoxLayout()
        # ⚠ 字別太長：主題給按鈕的 padding 是左右各 14px，
        #   「加入目前位置」要 108px，加上 X 鈕就超出區塊寬度被切掉（踩過）。
        add_btn = QPushButton("加入位置")
        add_btn.setToolTip(
            "把角色現在站的位置加進巡邏點，並記下是哪一張地圖。\n"
            "掛機時只會走「目前這張地圖」的點 —— 換到別張圖就不會亂跑。")
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
        # 「巡邏點」每列是「編號. 地圖名 (x, y)」，地圖名最長 7 個字
        # （專家級遺落之地／史萊姆晴空牧場），座標最多各 3 位數
        fit_list(spot, self.spot_list, "10. 專家級遺落之地 (242, 178)")
        panes.addWidget(spot)
        root.addLayout(panes)

        self.status = QLabel("尚未掃描")
        self.status.setStyleSheet("color: #9aa2b8;")
        self.status.setWordWrap(True)
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
    # -- 自動巡迴換頻道 --------------------------------------------------
    def _switch_channel(self, n: int) -> bool:
        """換到第 n 頻，並把所有快取的位址作廢。

        ⚠⚠ **作廢位址是必要的，不是保險**：換頻道會斷線重連，狀態物件／玩家
          物件／角色屬性／物品陣列全部搬家。舊位址還留著的話，寫入執行緒每
          20ms 就會往一塊已經是別人的記憶體寫目標 ID —— 那是在亂改遊戲的堆積。
        """
        if not self._ensure_mover():
            return False
        self._keys.set_on(False)          # 先停手，別對著要消失的目標送封包
        self._atk.hold_off()
        self._cur = None
        if not channel.switch(self._mover, n, self._rot_max):
            return False
        self._drop_cached_addrs()
        return True

    def _drop_cached_addrs(self) -> None:
        """傳送／重連之後把所有快取的位址作廢（換頻道、回程都要）。

        ⚠⚠ **這是必要的，不是保險**：斷線重連或換地圖之後，狀態物件／玩家
          物件／角色屬性／物品陣列全部搬家。舊位址還留著的話，寫入執行緒每
          20ms 就會往一塊已經是別人的記憶體寫目標 ID —— 那是在亂改遊戲的堆積。
        """
        # ⚠ 先叫寫入執行緒停手，再把位址清掉 —— 順序不能反。
        #   目前每個呼叫端都有自己先 hold_off()，但那是「剛好都有做」，
        #   不是這支保證的。放這裡才是真的保證。
        self._atk.hold_off()
        self._keys.set_on(False)
        self.state = self.player = self.stats = self.inv = None
        self._keys.eid = None
        self._keys.stats = None
        self._keys.pf = None
        self._killed.clear()              # 換頻＝整批怪都換了，冷卻名單沒意義
        self._unreach_n.clear()
        self._scene_obj = None            # 場景物件也會搬家
        self._scene_scanned = False
        self._scene_try = 0.0             # 剛傳送完，允許立刻重新定位一次
        self._since_scan = RESCAN_GAP     # 重連完立刻重掃

    def _on_state_stale(self) -> None:
        """寫入執行緒發現手上的狀態物件位址已經失效（vtable 對不上）。

        ★ 這是**最後一道防線**：`apply_scan()` 那邊已經會在位址一變就叫它停手，
          但物件搬家到下一次掃描之間還是有空窗，而寫入執行緒每 20ms 跑一次。
          它自己驗出來就自己停，這裡只負責把其餘快取一起作廢並馬上重掃。
        """
        self._keys.set_on(False)
        self._cur = None
        self._drop_cached_addrs()
        self.status.setText("位址失效（換地圖／重生／重連？）→ 重新定位中…")

    def _fire_recall(self) -> None:
        """觸發的第二段：真的把回程道具用掉，然後排定「關自動攻擊」。

        ★★ 送之前先看**地圖是不是已經自己變了**：開了自動攻擊之後，官方
          客戶端可能已經按它自己的設定把人傳回城了。那時我們再送一次回程
          只會多花一個道具、而且跟官方的流程打架 —— 直接跳過就好。
        """
        say = self.status.setText

        if not (self._mover is not None and self._mover.active) or not self.inv:
            say("⚠ 回程沒送出去（跳板或背包位置變了）")
            return
        now = scene.current(self.sc, allow_scan=False)
        here = now.id if now else None
        if (self._supply_scene is not None and here is not None
                and here != self._supply_scene):
            self._supply_left = True        # 已經在外面了，等它回來就好
            self._drop_cached_addrs()
            self._schedule_af_off()
            self._schedule_jump_back()
            say(f"★ 客戶端已自己回程（現在在 {now}）→ 略過我們的回程")
            return
        ok, msg = robot.do_recall(self._mover, self.sc, self.inv)
        if not ok:
            self._end_supply(f"⚠ 交棒失敗：{msg}", stop=True)
            return
        self._drop_cached_addrs()
        self._schedule_af_off()
        self._schedule_jump_back()
        say("🔧 已交給天使精靈跑補給　" + msg)

    def _schedule_af_off(self) -> None:
        """**回程送出之後**隔幾秒把「自動攻擊」關回去（見 robot.AF_HOLD_SECS）。

        ★ 這樣使用者的「自動攻擊」幾乎全程都是關的，精靈不會跟我們搶怪，
          但補給那一趟照樣跑完。
        ⚠ 太早關會讓精靈只走到城裡就停住（0 秒實測失敗），所以這個秒數
          是實測調出來的，不要為了「快一點」去縮。
        """
        QTimer.singleShot(
            int(robot.AF_HOLD_SECS * 1000),
            lambda: (self._mover is not None and self._mover.active
                     and robot.autofight_off(self._mover, self.sc)))

    def _schedule_jump_back(self) -> None:
        """勾了「使用天使趴趴GO回地圖」的話，排定幾分鐘後直接傳回原地圖。

        ★ 用計時器而不是掛在補給的輪詢上 —— 補給那段時間輪詢在做別的事，
          而這件事只跟「觸發之後過了多久」有關，各自算比較單純。
        ⚠ 補給一趟就一個號碼（_supply_gen）：中途又按一次、或補給提早結束，
          舊的計時器醒來看到號碼對不上就自己作廢，不會亂傳。
        """
        if not self.sup_jump_cb.isChecked():
            return
        if self._supply_scene is None:
            self.status.setText("⚠ 不知道出發地圖，這趟不用趴趴GO傳回去")
            return
        gen = self._supply_gen
        QTimer.singleShot(int(JUMP_BACK_SECS * 1000),
                          lambda: self._jump_back(gen))

    def _jump_back(self, gen: int) -> None:
        """時間到，用遊戲的傳送封包回原本那張地圖。"""
        say = self.status.setText
        if gen != self._supply_gen or self._supply_scene is None:
            return                              # 上一趟排的，早就過期了
        if not (self._mover is not None and self._mover.active):
            say("⚠ 跳板不在，沒辦法傳回去")
            return
        now = scene.current(self.sc, allow_scan=False)
        if now is not None and now.id == self._supply_scene:
            return                              # 精靈已經自己走回來了
        pos = self._supply_pos or (None, None)
        e = jumpmap.nearest(self._supply_scene, pos[0], pos[1])
        if e is None:
            say("⚠ 趴趴GO 沒有回"
                f"{scene.scene_name(self._supply_scene)}的傳送點")
            return
        ok, msg = jumpmap.teleport(self._mover, self.sc, e.jump_id)
        self._drop_cached_addrs()
        say((f"★ 補給 {JUMP_BACK_SECS / 60:.0f} 分鐘到 → " if ok else "⚠ ")
            + msg)

    # ------------------------------------------------------------------
    # -- 死亡回程（勾了「死亡自己回練功區」才會進）-------------------------
    def _start_death_return(self) -> None:
        """角色死了、而且勾了「死亡自己回練功區」→ 進死亡回程模式。

        分工：**復活是精靈的事**（開始掛機時已把「陣亡自動復活＋回城」調好，
        見 robot.apply_prefs），我們只負責等角色活過來、死亡滿
        `DEATH_JUMP_SECS` 秒後用趴趴GO把人傳回死掉時那張地圖，
        回到地圖就接回自動戰鬥。
        """
        self._death = True
        self._death_t = 0.0
        self._death_poll = 0.0
        self._death_scene = self._scene
        self._death_pos = self.my_pos()      # 屍體的位置還讀得到，拿來挑落點
        self._death_jumped = False
        # 我們自己完全讓開（跟交棒補給時一樣）：不送技能鍵、不寫目標
        self._keys.set_on(False)
        self._atk.hold_off()
        self._cur = None
        # 極罕見：補給跑到一半死掉 → 補給那趟作廢，讓死亡回程接管。
        #（排著的趴趴GO/關自動攻擊計時器會因 gen 對不上自己作廢。）
        if self._supply:
            self._supply = False
            self._supply_gen += 1
        self.status.setText(f"☠ 角色死亡 → 等精靈復活，"
                            f"{DEATH_JUMP_SECS:.0f} 秒後用趴趴GO傳回練功區…")

    def _death_fail(self, why: str) -> None:
        """死亡回程走不下去 → 停機＋通知（使用者掛網離開，要叫得動他）。"""
        self._death = False
        self._stop_with(f"☠ {why} → 已停止掛機")
        self.notify(f"{why}，掛機已停止。")

    def _death_tick(self, dt: float) -> bool:
        """死亡回程模式。進行中回 True（呼叫端整個 tick 都要讓開）。

        流程：等復活（精靈代勞）→ 死亡滿 `DEATH_JUMP_SECS` 秒且人活著
        → 趴趴GO傳回 `_death_scene` → 回到那張地圖就接回自動戰鬥。
        ★ 人在死亡那張圖上活過來（原地復活、或使用者自己處理了）就直接
          接回來，不傳送。
        """
        if not self._death:
            return False
        self._death_t += dt
        self._death_poll += dt
        if self._death_t >= DEATH_WAIT_MAX:
            self._death_fail(f"死亡回程超過 {DEATH_WAIT_MAX:.0f} 秒沒完成"
                             "（復活或傳送卡住了）")
            return True
        if self._death_poll < DEATH_POLL:
            return True
        self._death_poll = 0.0

        # 活過來了沒。stats 在復活／傳送時會搬家，讀不到就當「還沒」，
        # 等掃描重新定位（超時有 DEATH_WAIT_MAX 兜底）。
        alive = False
        if self.stats:
            st = player.read(self.sc, self.stats)
            alive = st is not None and st.hp > 0
        now = scene.current(self.sc, allow_scan=False)
        here = now.id if now else None
        back = (alive and here is not None and self._death_scene is not None
                and here == self._death_scene)

        if back:
            self._death = False
            self._since_scan = RESCAN_GAP        # 立刻重掃，馬上接回打怪
            self.status.setText("★ 回到練功地圖 → 接回自動戰鬥"
                                if self._death_jumped else
                                "★ 原地活過來了 → 接回自動戰鬥")
            return True
        if self._death_jumped:
            return True                          # 傳送送出去了，等著陸

        if self._death_t < DEATH_JUMP_SECS or not alive:
            left = max(DEATH_JUMP_SECS - self._death_t, 0.0)
            self.status.setText(
                "☠ 角色死亡 → " + ("等精靈復活" if not alive else "復活了")
                + (f"，{left:.0f} 秒後" if left > 0 else "，")
                + "用趴趴GO傳回練功區…")
            return True

        # 倒數到了、人也活了 → 傳回去
        if self._death_scene is None:
            self._death_fail("死亡時不知道人在哪張地圖，沒辦法傳回去")
            return True
        if not self._ensure_mover():
            self._death_fail("跳板裝不起來，沒辦法用趴趴GO傳回去")
            return True
        pos = self._death_pos or (None, None)
        e = jumpmap.nearest(self._death_scene, pos[0], pos[1])
        if e is None:
            self._death_fail(f"{scene.scene_name(self._death_scene)}"
                             "不在趴趴GO清單裡，沒辦法傳回去")
            return True
        ok, msg = jumpmap.teleport(self._mover, self.sc, e.jump_id)
        self._drop_cached_addrs()                # 傳送必換地圖，位址全作廢
        if not ok:
            self._death_fail(f"趴趴GO傳送失敗（{msg}）")
            return True
        self._death_jumped = True
        self.status.setText("☠ 復活了 → 已用趴趴GO傳回練功區，等著陸…")
        return True

    # ------------------------------------------------------------------
    # -- 回程補給（判斷 → 交棒 → 回到原地圖再接回來）-----------------------
    def _check_dry(self) -> list | None:
        """藥水見底就通知一聲 —— **跟有沒有勾「藥水觸發」無關**（使用者要求）。

        ★ 用門閂（self._dry）：空著的那段期間只叫一次，補到貨才重新武裝。
          少了它每 GEAR_CHECK_GAP 秒就會吵一次。
        ★ 只通知不停機：水沒了照樣打得動，血低了本來就有「坐下休息」擋著。
          武器耐久 0 才是真的打不了，那個維持停機。

        回傳算出來的見底清單（兩組都算），讓同一拍的補給判斷直接拿去用，
        不必再走一次物品陣列；沒算成就回 None。
        """
        if not (self.inv and self._mover is not None and self._mover.active):
            return None
        try:
            # 兩組都要看：**通知不看勾選**，勾選只決定要不要跑補給。
            dry = robot.potions_out(self._mover, self.sc, self.inv, self.pid)
        except Exception:                              # noqa: BLE001
            return None                                # 讀不到就當沒事，別擋掛機
        key = "|".join(w for w, _ in dry)
        if dry and key != self._dry:
            on = {"HP": self.sup_hp_cb.isChecked(),
                  "MP": self.sup_mp_cb.isChecked()}
            # 見底的那幾組裡，有勾觸發的才會跑補給
            will = [w for w, _ in dry if on[w]]
            self.notify(
                "、".join(d for _, d in dry) + "用完了。"
                + ("即將跑回程補給。" if will
                   else "（沒勾這一項的觸發，掛機繼續）"))
        self._dry = key
        return dry

    def _start_supply(self, why: str) -> bool:
        """把控制權交給官方的天使守護精靈。接得起來才回 True。

        ★ 為什麼用官方的：回城→找 NPC→修裝→買東西→走回戰場這一整趟，
          遊戲本來就會做，而且**細節是使用者在遊戲那一頁設好的**。
          我們自己重做一份只是多一套要維護的東西。
          精靈是 Lua 寫的、不送任何封包，所以只能這樣叫（見 app/game/lua.py）。
        """
        if not self._ensure_mover():
            return False
        miss = robot.missing_supply_settings(self._mover, self.sc)
        if miss:
            self._stop_with(f"🔧 {why}，但天使精靈的補給頁還缺："
                            + "、".join(miss))
            self.notify(f"{why}，但天使精靈的補給頁沒設好（缺 "
                        + "、".join(miss) + "），掛機已停止。")
            return False
        # 沒有回程道具就別開始，免得把設定改了卻走不了
        if not robot.has_recall_item(self.sc, self.inv):
            self._stop_with(f"🔧 {why}，但背包裡沒有回程道具")
            self.notify(f"{why}，但背包裡沒有回程道具，掛機已停止。")
            return False
        # ★ 記下現在這張地圖 —— 回到它才算補給結束（使用者要求）
        # ⚠⚠ self._scene **本來就是場景編號（int）**，不是 Scene 物件 ——
        #   寫成 `self._scene.id` 會在真的觸發補給的那一刻才炸
        #   （AttributeError: 'int' object has no attribute 'id'，使用者實際遇到）。
        #   會拖這麼久才發現，是因為平常沒有補給條件根本走不到這一行。
        self._supply_scene = self._scene
        self._supply_left = False
        self._supply_pos = self.my_pos()
        self._supply_gen += 1
        # ⚠ 這裡刻意**不**重驗「標記傳送捲軸」的設定 —— 開始掛機時關過一次
        #   就夠了，之後使用者在遊戲裡改回去是他自己的決定（使用者明講）。
        # ★ 第一段：調好開關 + 設中心點。第二段（回程）隔幾秒才送，
        #   順序與間隔都不能省（見 robot.do_recall）。
        notes = robot.begin_supply(self._mover, self.sc)
        self._robot_ours = True
        # 我們自己要完全讓開：不送技能鍵、不寫目標
        self._keys.set_on(False)
        self._atk.hold_off()
        self._cur = None
        self._supply = True
        self._supply_t = 0.0
        self._supply_poll = 0.0
        self.status.setText(
            f"🔧 {why} → 調整精靈設定："
            + ("、".join(notes) if notes else "本來就都對")
            + f"　{robot.SETUP_SETTLE:.0f} 秒後回程…")
        QTimer.singleShot(int(robot.SETUP_SETTLE * 1000), self._fire_recall)
        return True

    def _end_supply(self, why: str, stop: bool = False) -> None:
        """收回控制權。stop=True 代表補給失敗，順便停掉掛機並通知。"""
        self._supply = False
        self._supply_gen += 1          # 這趟結束，排著的趴趴GO傳送作廢
        if self._mover is not None and self._mover.active:
            robot.end_supply(self._mover, self.sc)   # 只關自動攻擊
        self._robot_ours = False
        # 補給跑完一定換過地圖，所有快取位址都要作廢
        self._drop_cached_addrs()
        if stop:
            self._stop_with(why)
            self.notify(why)
        else:
            self.status.setText(why)

    def _supply_tick(self, dt: float) -> bool:
        """補給進行中回 True（呼叫端要整個讓開）。

        ★ **判完成看「回到原本那張地圖」**（使用者要求）：離開過、又回來了
          就代表整趟跑完。比看耐久通用 —— 缺水那種觸發修不修裝都一樣。
        ⚠ 一定要先「離開過」才算，不然剛觸發還沒傳送出去就會被判定完成。
        """
        if not self._supply:
            return False
        self._supply_t += dt
        self._supply_poll += dt
        if self._supply_t >= SUPPLY_MAX_SECS:
            self._end_supply(
                f"🔧 補給超過 {SUPPLY_MAX_SECS / 60:.0f} 分鐘還沒完成 → 已停止掛機",
                stop=True)
            return True
        if self._supply_poll >= SUPPLY_POLL:
            self._supply_poll = 0.0
            if self.inv:
                d = inventory.durability(self.sc, self.inv)
                if d is not None:
                    self._dura = d
            now = scene.current(self.sc, allow_scan=False)
            here = now.id if now else None
            if self._supply_scene is not None and here is not None:
                if here != self._supply_scene:
                    self._supply_left = True
                elif self._supply_left:
                    self._end_supply(
                        f"🔧 補給完成，已回到{now}　"
                        f"耐久 {self._dura[0]}　共花 {_mmss(self._supply_t)}")
                    return True
            self.status.setText(
                f"🔧 天使精靈補給中…（{_mmss(self._supply_t)}）"
                + (f"　目前在 {now}" if now else "")
                + ("" if robot.is_run(self.sc) else "　⚠ 精靈已停下"))
        return True

    # ------------------------------------------------------------------
    # -- 血/魔不足時坐下回復 ---------------------------------------------
    def _foes(self) -> list:
        """現在有哪些怪正在打我（用實體的「交戰對象」欄位，見 entity.OFF_FOE）。

        ⚠ 屍體要排掉：怪死了物件不會馬上回收，交戰欄位還留著舊值。
        """
        if not self.player:
            return []
        return [m for m in self.mons
                if entity.read_state(self.sc, m.addr) != "Dead"
                and entity.attacking(self.sc, m, self.player)]

    def _sit_down(self, dt: float) -> bool:
        """確保角色坐著。回傳現在是不是已經坐著了。

        ★ **只送「坐下」，不送「站起來」**（使用者指出的）：繼續打怪角色就會
          自己站起來，所以起身不需要按鍵。Insert 是切換鍵，少按一個方向就少
          一半弄反的機會。

        ⚠⚠ 即使只按一個方向，也不能「讀到沒坐著就補送」——
          讀到一次假訊號就會把剛坐好的角色又站起來，然後一路來回
          （使用者回報的「一直坐下站起來」）。所以：
            · 剛決定要坐 → 立刻送一次
            · 之後要補送，得**連續** SIT_CONFIRM 秒都讀到沒坐著
            · 兩次之間至少隔 SIT_GAP 秒（坐下要 ~91ms 才反映到記憶體）
        """
        if not self._sit_want:
            self._sit_want = True
            self._sit_bad_t = SIT_CONFIRM     # 剛決定 → 立刻可以送
        self._rest_key_t += dt
        if entity.is_sitting(self.sc, self.player):
            self._sit_bad_t = 0.0
            return True
        self._sit_bad_t += dt
        if self._sit_bad_t >= SIT_CONFIRM and self._rest_key_t >= SIT_GAP:
            self._rest_key_t = 0.0
            self._sit_bad_t = 0.0
            _send_scan(self.hwnd, VK_INSERT)
        return False

    def _stand_up(self) -> None:
        """不再需要坐著。**不送按鍵** —— 打怪／移動時角色會自己站起來。"""
        self._sit_want = False
        self._sit_bad_t = 0.0

    def _rest_wanted(self) -> str:
        """現在該不該休息；要的話回傳原因文字，不要回空字串。"""
        if self.rest_hp_cb.isChecked() and self._hp_pct < self.rest_hp.value():
            return f"HP {self._hp_pct:.0f}%"
        if self.rest_mp_cb.isChecked() and self._mp_pct < self.rest_mp.value():
            return f"MP {self._mp_pct:.0f}%"
        return ""

    def _rest_full(self) -> bool:
        """有勾的那幾項都回到 100% 了嗎。"""
        if self.rest_hp_cb.isChecked() and self._hp_pct < REST_FULL:
            return False
        if self.rest_mp_cb.isChecked() and self._mp_pct < REST_FULL:
            return False
        return True

    def _end_rest(self, why: str) -> None:
        """結束休息。**不送起身按鍵** —— 恢復打怪之後角色會自己站起來。"""
        self._stand_up()
        self._rest = ""
        self.rest_lbl.setText(f"　休息結束（{why}）")

    def _rest_tick(self, dt: float) -> bool:
        """休息流程。回傳 True = 這一拍不要打怪。

        三個階段：
          ""       正常打怪，只是盯著血魔
          "finish" 已經低於門檻，**但還在收尾** —— 手上的怪要打完、
                   而且不能有怪在打我，才准坐下（使用者明確要求）
          "sit"    坐著回復，回滿就起身
        """
        # 沒勾就完全不動作（也把「想坐著」清掉，免得殘留）
        if not (self.rest_hp_cb.isChecked() or self.rest_mp_cb.isChecked()):
            if self._rest:
                self._rest = ""
                self._sit_want = False
                self.rest_lbl.setText("")
            return False

        # ⚠ 節流：心跳 10ms 一拍，每拍都去讀「誰在打我」是每秒幾千次記憶體
        #   讀取，沒必要。但**坐著時要維持接管**，所以沒輪到就直接沿用上次結論。
        self._rest_t += dt
        if self._rest_t < REST_SAMPLE:
            return self._rest == "sit"
        dt, self._rest_t = self._rest_t, 0.0

        if self._rest == "sit":
            foes = self._foes()
            if foes:
                # ⚠ 坐著被打會死，要起身反擊 —— 但**只回到收尾階段**
                #   （只打那幾隻），不是回到完整掛機。
                #   第一版是直接結束休息，結果它跑去打新怪、把魔力又花掉，
                #   MP 在門檻附近來回震盪、永遠到不了 100%（實測黑狐
                #   86~93% 上上下下）。
                self._stand_up()               # 起身不用按鍵，打下去就站起來
                self._rest = "finish"
                self._rest_why = f"{len(foes)} 隻怪在打我"
                return False
            if self._rest_full():
                self._end_rest(f"HP {self._hp_pct:.0f}% MP {self._mp_pct:.0f}%")
                return False
            sitting = self._sit_down(dt)         # 被打斷會補坐，但不會亂切換
            self.rest_lbl.setText(
                ("　休息中：" if sitting else "　準備坐下…　")
                + f"HP {self._hp_pct:.0f}%　MP {self._mp_pct:.0f}%")
            return True

        if self._rest == "finish":
            # ⚠⚠ **進出用不同門檻**：低於設定值才進休息，但要回到 100% 才出來。
            #   用同一個門檻判斷進出的話，在門檻附近必然來回震盪 ——
            #   實測黑狐 MP 在 86~93% 之間坐下站起來反覆，永遠到不了 100%。
            #   （這也正是使用者要的：「多少% 以下停止打怪，坐下直到 100% 再繼續」。）
            if self._rest_full():
                self._rest = ""
                self.rest_lbl.setText("")
                return False
            foes = self._foes()
            if self._cur is not None or foes:
                self.rest_lbl.setText(
                    f"　{self._rest_why} → 收尾中"
                    + ("（手上還有一隻）" if self._cur is not None else "")
                    + (f"（{len(foes)} 隻怪在打我）" if foes else ""))
                return False                   # ← 繼續打，把牠們解決掉
            self._sit_down(dt)
            self._rest = "sit"
            self.rest_lbl.setText("　坐下休息…")
            return True

        why = self._rest_wanted()
        if why:
            self._rest = "finish"
            self._rest_why = why
            return False

        # 不需要休息了 —— 直接放行去打怪，角色會自己站起來（不必送 Insert）。
        self._stand_up()
        self.rest_lbl.setText("")
        return False

    def _rot_say(self, text: str) -> None:
        """更新巡迴狀態文字。**內容沒變就不要動它** ——
        心跳是 10ms 一拍，每拍都 setText 等於每秒重畫 100 次，白花 CPU 也會閃。
        """
        if text != self._rot_last:
            self._rot_last = text
            self.rot_lbl.setText(text)

    def _tick_rotation(self, dt: float) -> bool:
        """巡迴換頻道的節奏。回傳 True = 這一拍不要打怪（正在換／剛換完）。

        ★ 只有「切換的那幾秒」會暫停，**停留期間照常掛機** ——
          不然巡迴就只是在浪費時間。
        """
        if not self.rot_cb.isChecked():
            if self._rot_seq or self._rot_settle:
                self._rot_seq, self._rot_settle = [], 0.0
                self._rot_say("")
            return False

        # 剛換完 → 等重連與重新定位，這段不打怪
        if self._rot_settle > 0:
            self._rot_settle -= dt
            here = channel.current(self.hwnd)
            self._rot_say(
                f"　換到 {here or '?'} 頻，穩定中…{_mmss(self._rot_settle)}")
            return True

        if self._rot_seq:
            self._rot_wait -= dt
            if self._rot_wait > 0:
                left = len(self._rot_seq)
                self._rot_say(
                    f"　巡迴中：{channel.current(self.hwnd) or '?'} 頻"
                    f"　還有 {_mmss(self._rot_wait)} 換下一個（剩 {left} 站）")
                return False              # ← 停留期間照常打怪
            nxt = self._rot_seq.pop(0)
            if not self._switch_channel(nxt):
                self._rot_seq.insert(0, nxt)   # 排不進指令槽，下一拍再試
                self._rot_wait = 1.0
                self._rot_say("　換頻道失敗，重試中…")
                return True
            self._rot_wait = float(self.rot_stay.value())
            self._rot_settle = ROT_SETTLE
            if not self._rot_seq:
                self._rot_t = 0.0        # 繞完一圈了，重新計時
                self._rot_say(f"　巡迴完成，回到 {self._rot_home} 頻")
            return True

        # 沒在巡迴 → 累積時間，到了就排一輪
        self._rot_t += dt
        every = float(self.rot_every.value()) * 60.0
        if self._rot_t < every:
            left = every - self._rot_t
            self._rot_say(f"　下一輪巡迴還有 {_mmss(left)}")
            return False
        here = channel.current(self.hwnd)
        # ★ 分流數讀遊戲自己的 server.xml（不是寫死、也不是記憶體位址），
        #   所以改版增減分流會自動跟上。讀不到就整輪跳過 ——
        #   寧可不換，也不要拿猜的數字去送。
        n = channel.count(self.sc, self.hwnd)
        if here is None or not n:
            self._rot_t = 0.0
            self._rot_say(
                "　⚠ 讀不到目前頻道（視窗標題），這一輪跳過" if here is None
                else "　⚠ 讀不到分流數（伺服器清單），這一輪跳過")
            return False
        # 從目前這一頻繞一圈回來：在 3 頻（共 5 頻）→ 4,5,1,2,3
        self._rot_max = n
        self._rot_home = here
        self._rot_seq = [(here - 1 + i) % n + 1 for i in range(1, n + 1)]
        self._rot_wait = 0.0             # 下一拍就出發
        self._rot_say(
            "　開始巡迴：" + " → ".join(str(c) for c in [here] + self._rot_seq))
        return True

    # ------------------------------------------------------------------
    # -- 目前在哪張地圖 --------------------------------------------------
    def _read_scene(self) -> int | None:
        """目前場景編號。優先走靜態指標（1.5ms）；它失效才掃一次留著用。"""
        sid = scene.current_id(self.sc, allow_scan=False)
        if sid is not None:
            return sid
        # 走到這裡代表靜態指標失效（多半是遊戲改版位移）。全掃 0.25 秒，
        # 只做一次並把物件位址記下來 —— 心跳是 10ms 一拍，不能每次都掃。
        # ⚠⚠ 還要加**冷卻**：下面讀失敗時會把 _scene_scanned 放回 False，
        #   所以「掃到了但物件一直讀不到」（換地圖、重連時的正常現象）會變成
        #   每 SCENE_SAMPLE(0.5 秒) 就在 GUI 執行緒上全掃一次 0.25 秒 ——
        #   畫面等於半凍住。讀不到場景本來就是容許的狀態（各處都會處理 None），
        #   所以慢一點重試不影響任何判斷。
        if not self._scene_scanned:
            now = time.monotonic()
            if now - self._scene_try < SCENE_RELOCATE_GAP:
                return None
            self._scene_try = now
            self._scene_scanned = True
            found = scene.locate(self.sc)
            self._scene_obj = found.obj if found else None
        if self._scene_obj is None:
            return None
        s = scene.read_at(self.sc, self._scene_obj)
        if s is None:
            self._scene_obj = None      # 物件搬家了，下次重新定位
            self._scene_scanned = False
        return None if s is None else s.id

    def cur_scene(self) -> int | None:
        """給 UI 用的即時版（加入巡邏點時要用，不能拿半秒前的舊值）。"""
        self._scene = self._read_scene()
        self._scene_t = 0.0
        return self._scene

    def _spot_here(self, sid: int | None) -> bool:
        """這個巡邏點是不是「現在這張圖」的。

        沒標記地圖的舊點一律視為可以去 —— 我們不知道它當初存在哪，
        直接擋掉會讓使用者原本設好的巡邏路線突然全部失效。
        """
        if sid is None:
            return True
        return scene.same_map(sid, self._scene)

    # ------------------------------------------------------------------
    # -- 巡邏點 --------------------------------------------------------
    def _refresh_spots(self) -> None:
        self.spot_list.clear()
        for n, (x, y, sid) in enumerate(self._spots):
            where = scene.scene_name(sid) if sid is not None else "未標記"
            self.spot_list.addItem(f"{n + 1}. {where} ({x:.0f}, {y:.0f})")
            self.spot_list.item(n).setToolTip(
                f"{where}　格子 ({x:.1f}, {y:.1f})\n"
                + (f"場景編號 {sid}" if sid is not None else
                   "舊版存的點，沒有記地圖 —— 在任何地圖都會走，"
                   "想要地圖比對請刪掉重加。"))

    def _add_spot(self) -> None:
        p = self.my_pos()
        if p is None:
            self.status.setText("讀不到位置，請先按「掃描周圍怪物」")
            return
        sid = self.cur_scene()
        self._spots.append((p[0], p[1], sid))
        self._refresh_spots()
        self._save_settings()
        where = scene.scene_name(sid) if sid is not None else "未知地圖"
        self.status.setText(
            f"已加入巡邏點 {len(self._spots)}：{where} ({p[0]:.0f}, {p[1]:.0f})"
            + ("" if sid is not None else "　⚠ 讀不到地圖，這個點不會做地圖比對"))

    def _remove_spots(self) -> None:
        for row in sorted((self.spot_list.row(i)
                           for i in self.spot_list.selectedItems()),
                          reverse=True):
            del self._spots[row]
        self._spot_i = 0
        self._refresh_spots()
        self._save_settings()

    def _ensure_mover(self) -> bool:
        """需要移動時才安裝 hook —— 沒用到就不要在遊戲裡放程式碼。

        ⚠ 一定要走 `move.acquire()`：同一個遊戲行程只能有一份跳板，
          自己 new 一份會把別的分頁（能量晶化）正在用的那份拆掉。
        """
        if self._mover is not None and self._mover.active:
            return True
        if self._mover_failed:
            return False                       # 之前裝失敗過，別一直重試
        try:
            self._mover = move.acquire(
                self.pid, injector.process_path(self.pid), self)
            return True
        except Exception as exc:               # noqa: BLE001
            self._mover = None
            self._mover_failed = True
            self.status.setText(f"⚠ 無法啟用移動：{exc}（掛機其他功能不受影響）")
            return False

    def _walk_toward(self, gx: float, gy: float, me, keep: float) -> int:
        """往 (gx,gy) 走，但在距離 keep 格處停下。有冷卻，不會狂送。

        ★★ 走的是 move.Mover.walk_route()：**對目標本身尋路**，走遊戲算出來
        的那條路，要留距離就沿著那條路往回退 —— 退到的點一定在它走得到的
        路段上。

        ⛔ 以前是「先在直線上取一個距離目標 keep 格的點，再對那個點尋路」。
           那個幾何點落在牆後面時尋路就失敗，往回縮短還是同一條直線、
           還是撞牆 —— 使用者實拍：站在原地 **32 秒**撞牆，而同樣的位置
           自己點地圖遊戲是走得過去的（那時它一包送 5 個繞路點）。

        回傳尋路算出的路徑點數（0 = 走不了，1 = 直線通，>1 = 中間有地形）。
        """
        if not self._ensure_mover():
            return 0
        if math.hypot(gx - me[0], gy - me[1]) <= keep:
            return 0
        n = self._mover.walk_route(self.sc, self.player, gx, gy,
                                   stop_short=keep)
        self._walk_t = 0.0
        return n

    def _my_id(self) -> int:
        """自己的實體 ID —— 施放封包的「目標」欄位要填它。

        ★ 實測跟使用者攔到的封包完全一致（0x47b403c2）。
        """
        if not self.player:
            return 0
        raw = self.sc._read_bytes(self.player + entity.OFF_ID, 4)
        return struct.unpack("<I", bytes(raw))[0] if raw else 0

    def my_pos(self) -> tuple[float, float] | None:
        """玩家目前的格子座標（每次都重讀，因為角色會走動）。"""
        if self.player is None:
            return None
        return entity.read_pos(self.sc, self.player)

    def apply_scan(self, s: Scan) -> None:
        # ⚠⚠⚠ 狀態物件**換位址或不見了** → 一定要先叫寫入執行緒停手。
        #   它手上的 `_job` 記著舊位址，而它每 20ms 就往 `舊位址 + 0x2D8`
        #   寫 4 bytes。物件搬家（換地圖、死亡重生、斷線重連、傳送）之後
        #   那塊記憶體早就是別人的了 —— 等於每秒 50 次亂改遊戲的堆積，
        #   遊戲會在很久以後跳「This program will be terminated…」掛掉，
        #   而且離真正的元凶已經很遠，完全看不出關聯。
        #   以前只有「我們自己觸發的傳送／換頻」有作廢位址（_drop_cached_addrs），
        #   角色自己死掉重生、使用者手動換地圖、伺服器重連都沒人管。
        if s.state != self.state:
            self._atk.hold_off()
            self._keys.set_on(False)
            self._keys.eid = None
            self._cur = None
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
        # ★ 王的前面加「【王】」（讀遊戲自己的怪物資料，不是靠名字猜）。
        #   ⚠ 顯示文字加了字首，所以**名字要另外存在 UserRole** —— 點名字是拿它
        #     加進「選中怪物」的，直接用顯示文字會加成「【王】水晶傘蜥蜴」。
        #   ⚠ 不要用 👑：中文字型沒有那個字形，會變豆腐方塊（離屏截圖實拍到）。
        idx = monsters.index_base(self.sc)
        seen: list[tuple[str, str]] = []          # (顯示文字, 真正的名字)
        for m in self.mons:
            if any(n == m.name for _, n in seen):
                continue
            crown = "【王】" if monsters.is_boss(self.sc, m.type_id, idx) else ""
            seen.append((crown + m.name, m.name))
        if [t for t, _ in seen] != [self.near.item(i).text()
                                    for i in range(self.near.count())]:
            self.near.clear()
            for text, name in seen:
                it = QListWidgetItem(text)
                it.setData(Qt.UserRole, name)
                self.near.addItem(it)
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

    def _cool_unreach(self, eid: int) -> None:
        """把「走不到」的怪冷凍起來，**時間隨失敗次數翻倍**。

        暫時算不出來的（角色站的位置的問題）8 秒後就會再試；
        真的站在走不進去的地形上的（實測：曼陀羅怪菇一律回 0），
        會一路翻倍到 UNREACH_MAX，等於自動淡出，不再浪費時間。
        """
        n = self._unreach_n.get(eid, 0)
        self._unreach_n[eid] = n + 1
        wait = min(UNREACH_MEMORY * (2 ** n), UNREACH_MAX)
        self._killed[eid] = time.monotonic() + wait

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
        # ★ 休息流程啟動後**只挑「正在打我的怪」**。
        #   ⚠⚠ 第一版是「一律不挑」，結果死結：有怪在打我 → 不准坐下，
        #     但也不准挑目標 → 那隻怪永遠殺不掉 → 卡在收尾中，畫面上就是
        #     「收尾中（1 隻怪在打我）」不動（使用者實際遇到）。
        #   坐著也要 —— 坐著被打會起身，起身後要能反擊。
        only_foes = bool(self._rest)
        want = self.wanted()
        me = self.my_pos()
        now = time.monotonic()
        # ★ 只打王：完全不看名字，改看種類 ID 的王旗標。
        #   索引表位址先解一次，迴圈裡每隻只要兩次 4-byte 讀取。
        boss_only = self.boss_cb.isChecked()
        idx = monsters.index_base(self.sc) if boss_only else None
        # _killed 存的是「到期時間」，不是記錄時間 —— 因為兩種跳過的冷卻長度
        # 不一樣（確定打死 KILL_MEMORY、只是沒給血量 NOHP_MEMORY）。
        for eid, until in list(self._killed.items()):
            if now > until:
                del self._killed[eid]
        pool = []
        for m in self.mons:
            if m.eid in self._killed:
                continue
            # 休息中／收尾中：只打正在打我的那幾隻，把牠們清掉才坐得下去。
            # ⚠ 這裡**不看「選中怪物」也不看只打王** —— 打我的怪不管是什麼
            #   都得處理掉，不然就是站在那裡挨打。
            if only_foes:
                if not entity.attacking(self.sc, m, self.player):
                    continue
            elif boss_only:
                # ⚠ is_boss 回 None 代表「查不到」（改版位移之類）——
                #   這種模式下一律不打，寧可不動也不要打到不該打的。
                if monsters.is_boss(self.sc, m.type_id, idx) is not True:
                    continue
            elif m.name not in want:
                continue
            # ★ 死活、動畫狀態、座標**一次讀回來**（相鄰欄位，見 read_live）。
            #   以前是四次系統呼叫，場上幾十隻怪、每殺一隻重挑一次。
            alive, st, p = entity.read_live(self.sc, m)
            if not alive:
                continue
            # ★★ 屍體直接跳過（別人先殺掉的）。動畫狀態當場重讀 ——
            #   掃描到現在可能已經過了 0.3 秒，牠剛好是在那之間倒下的。
            #   ⚠ 這是搶怪區「卡卡的」的主因：實測任何一個瞬間清單裡有
            #     中位 20%、最高 50% 是屍體，而且屍體會賴著中位 5 秒
            #     （最久 79.8 秒）。挑最近的就常常挑到牠們，鎖上去要等
            #     CORPSE_SECS 才發現不對 —— 每次白花快一秒。
            #   ⚠ is_alive() 擋不掉：它只比對 vtable + 實體 ID，分不出屍體。
            if st == "Dead":
                continue
            # 座標也是當場讀的（跟上面同一次讀取）：怪會走、角色也在走，
            # 掃描時記的早就過期了。
            # ★ 不限距離：多遠的怪都收進來（使用者要求「想打多遠都可以」），
            #   排序後自然會先打最近的，遠的靠移動封包導航過去。
            d = (math.hypot(p[0] - me[0], p[1] - me[1])
                 if p and me else float("inf"))
            # ⛔ 這裡曾經有「正在打我的排最前面」（entity.attacking）——
            #   **拿掉了**。沒有距離限制的話，20 格外的仇人會贏過 13 格的
            #   正常目標（唯讀監控實拍：目標 20.3 格，周圍就有 13.1/13.3 格），
            #   然後為了追那隻遠的去撞牆。
            #   使用者的判斷：純粹挑最近的就好 —— 打我的怪本來就貼在身上，
            #   排序自然會先輪到牠們。
            pool.append((d, m))
        pool.sort(key=lambda t: t[0])
        if not pool:
            return False
        d, self._cur = pool[0]           # 純粹挑最近的
        self._stuck = 0.0
        self._anchor = me                # 換目標 → 卡住偵測從這裡重新算
        self._path_pts = -1                       # -1 = 還沒算，tick() 會去問尋路
        self._path_t = PATH_GAP                   # 下一拍就問
        self._way = []
        self._unreach = 0
        self._hurt = False
        self._gone = 0
        self._walked_ok = True
        self._why = ""
        self._last_hp = -1
        self._atk.attack(self.state, self._cur)   # 寫入執行緒：開始鎖定這隻
        self._keys.eid = self._cur.eid            # 送封包時要指名打誰
        self._keys.set_on(True)                   # 攻擊執行緒：開始發動
        info = monsters.info(self.sc, self._cur.type_id, idx) if boss_only else None
        self.status.setText(
            ("【只打王】　" if boss_only else "")
            + f"鎖定「{self._cur.name}」"
            + (f"（{info}）" if info else "")
            + f"　距離 {d:.1f} 格　累計擊殺 {self._kills}")
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
        if on:
            # ★ 開自動戰鬥就無腦放一次分身（使用者要求，不偵測身上有沒有）
            self._buff.arm()
        if not on:
            self._keys.set_on(False)
            self._keys.stop_learning()
            self._keys.eid = None
            self._atk.hold_off()
            self._cur = None
            self._death = False        # 死亡回程等到一半就作廢，別再傳送
            # ⚠ 停掛機時如果正在讓精靈跑補給，一定要把精靈也關掉 ——
            #   不然使用者以為停了，角色卻還在自己跑。
            # ⚠ 精靈如果是我們開的（自動交棒或測試按鈕），停掛機時一定要把
            #   自動攻擊關掉 —— 不然使用者以為停了，角色還在自己打。
            self._buff.reset()
            self._buff_note = ""
            if self._supply or self._robot_ours:
                self._supply = False
                self._supply_gen += 1        # 排著的趴趴GO傳送就此作廢
                if self._mover is not None and self._mover.active:
                    robot.end_supply(self._mover, self.sc)
                self._robot_ours = False
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
        # ★ 開始自動戰鬥就把精靈調好：主開關一律開（使用者要求）；勾了
        #   趴趴GO回地圖／死亡回程的話，把對應的精靈設定一起調到位。
        #   每一項都先讀再動，平常（都已經是對的）只花幾次純記憶體讀取。
        notes = []
        if self._mover is not None and self._mover.active:
            notes = robot.apply_prefs(
                self._mover, self.sc, main_switch=True,
                jump_back=self.sup_jump_cb.isChecked(),
                revive_recall=self.sup_revive_cb.isChecked())
        self.status.setText(
            ("掛機中：只打「" + "、".join(want) + "」" if want
             else "掛機中：還沒選任何怪物 —— 點右邊的名字加進「選中怪物」")
            + ("　精靈：" + "、".join(notes) if notes else ""))

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

        if not self.run_cb.isChecked():
            return

        # ★ 死亡回程要在最前面：死亡／復活／傳送期間 state 常常是 None、
        #   也絕不能讓巡迴換頻道插進來，所以整個 tick 都讓給它。
        if self._death_tick(dt):
            return

        # ★ 巡迴換頻道要在「state is None」的檢查**之前** —— 換頻道會斷線重連，
        #   那幾秒 state 本來就是 None，放後面的話狀態機會停在半路。
        if self._tick_rotation(dt):
            return

        if self.state is None:
            return

        # ★ 角色死了就自動停：不然會對著空氣一直送技能鍵。
        # HP 走 app/game/player.py（跨 5 台驗證過的定位）。
        # ⚠ 它的 read() 刻意不做數值檢查，HP 歸零照樣讀得到 —— 早期版本把
        #   「HP > 0」寫進合理性檢查，結果角色一死就回 None，死亡永遠測不到。
        #
        # ⚠⚠ 這一段**必須在 _rest_tick 之前**。放在後面的話，一旦坐下
        #   （_rest_tick 回 True → tick 直接 return），血魔百分比就再也不會更新
        #   → 永遠等不到「回滿」→ 坐著不動。實際發生過（使用者回報
        #   「血魔 100 了也不會開始打」，現場看到黑狐 100%/100% 還坐著）。
        self._hp_t += dt
        if self._hp_t >= HP_CHECK_GAP:
            self._hp_t = 0.0
            if self.stats:
                st = player.read(self.sc, self.stats)
                if st is not None:
                    # 血/魔百分比：休息判斷用。上限是 0 時當作滿的，
                    # 免得剛換地圖讀到一半的資料就誤觸發休息。
                    self._hp_pct = (st.hp / st.max_hp * 100
                                    if st.max_hp else 100.0)
                    self._mp_pct = (st.mp / st.max_mp * 100
                                    if st.max_mp else 100.0)
                    if st.hp <= 0:
                        # ★ 勾了「死亡自己回練功區」→ 不停機，交給死亡回程
                        #   模式（等精靈復活回城 → 趴趴GO傳回來 → 接著打）。
                        #   沒勾就維持原樣：自動停止掛機。
                        if self.sup_revive_cb.isChecked():
                            self._start_death_return()
                        else:
                            self._stop_with(
                                f"☠ 角色死亡 → 已自動停止掛機"
                                f"（本次擊殺 {self._kills} 隻）")
                        return
                else:
                    self.stats = None       # 物件搬家了，等下次掃描重新定位

        # ★ 休息（血/魔不足坐下）。收尾階段會回 False 讓它繼續打，
        #   只有真的坐下時才接管。放在讀完血魔之後（見上面那段的警告）。
        if self._rest_tick(dt):
            return

        # ★ 補給中就整個讓開 —— 那段時間是天使精靈在開車，我們不能同時下指令。
        if self._supply_tick(dt):
            return

        # ★ 自動分身：時間快到了就補一次 F12（見 app/game/buff.py）。
        # ⚠ 位置刻意放在**休息／補給之後、打怪之前**：
        #   坐著或交棒給精靈時不要插隊按鍵，但打怪中該補還是要補
        #   —— buff 斷掉的損失比少打一下大。
        # ⚠ 它自己會控制節奏（沒到時間就什麼都不做），不必在這裡節流。
        # ★ 只有開了自動戰鬥才會走到這裡（上面 run_cb 沒勾就 return 了）——
        #   使用者要求：單獨勾這個沒有用。
        if self.buff_cb.isChecked():
            if not self._buff.armed:
                self._buff.arm()          # 中途才勾的話，下一拍就無腦放一次
            # ⚠ `_my_id` 傳的是**函式本身**不是值：它要讀一次記憶體，而只有
            #   真的要補分身（20 分鐘一次）才用得到，心跳每 10ms 一拍先算好
            #   等於每秒白讀 100 次。
            note = self._buff.step(
                self.sc, self._mover, self.hwnd, self._keys.pf,
                self._my_id, self.stats, win.send_key)
            if note and self._buff_note != note:
                self._buff_note = note
                self.status.setText(f"✨ {note}")
                if self._buff.skill:      # 學到了就存起來，之後不用再按 F12
                    self._save_settings()
        elif self._buff.armed:
            self._buff.armed = False

        # ★ 該不該回去補給。勾了「回程補給」就照精靈自己的設定判斷（耐久、
        #   採買清單缺貨…）；沒勾就只看耐久 0 並停機通知。幾秒看一次就夠。
        self._gear_t += dt
        if self._gear_t >= GEAR_CHECK_GAP and self.inv:
            self._gear_t = 0.0
            d = inventory.durability(self.sc, self.inv)
            if d is not None:
                self._dura = d
            gear = self.sup_gear_cb.isChecked()
            hp_on = self.sup_hp_cb.isChecked()
            mp_on = self.sup_mp_cb.isChecked()
            # ★ 水沒了一律通知（使用者要求），跟觸發開關無關。
            #   放在觸發判斷**之前** —— 勾了觸發的話，這一拍就會接著跑補給，
            #   通知要先發出去才不會被 return 吃掉。
            dry = self._check_dry()
            if (gear or hp_on or mp_on) and self._ensure_mover():
                # ★ 耐久與見底清單都是這一拍剛算好的，直接傳進去別再算一次
                #   （各要走一趟物品陣列，上百次記憶體讀取）。
                why = robot.supply_needed(self._mover, self.sc, self.inv,
                                          gear, hp_on, mp_on, self.pid,
                                          dura=d, dry=dry)
                if why and self._start_supply(why):
                    return
            if d is not None and d[0] <= 0:
                self._stop_with(f"🔧 武器已損壞（耐久 0）→ 已自動停止掛機"
                                f"（本次擊殺 {self._kills} 隻）")
                self.notify("武器已損壞（耐久 0），掛機已自動停止。")
                return

        self._walk_t += dt
        me = self.my_pos()

        # 目前在哪張地圖 —— 巡邏點要靠它決定「這個點是不是這張圖的」。
        # 換圖很少發生，半秒讀一次就夠（心跳是 10ms 一拍）。
        self._scene_t += dt
        if self._scene_t >= SCENE_SAMPLE:
            self._scene_t = 0.0
            was = self._scene
            self._scene = self._read_scene()
            # ⚠ 換地圖時導航記憶一定要清掉：走過的地方／黑名單都是座標，
            #   在別張圖上完全沒有意義，留著會讓它一開始就排除掉正確方向。
            if was != self._scene:
                self._nav.reset()

        # ★★ 「角色正在走路嗎」——**直接讀遊戲的動畫狀態**（'Run' / 'Wait'），
        #   不要再隔 0.3 秒比一次位置。移動中一律不重下移動指令（會把多點路徑
        #   砍掉、原地來回），所以這個判斷慢多少，每個路段就多空等多少。
        #   實測同一條來回路線：比位置 28% 的時間站著不動，讀狀態只剩 10%。
        #   （見 entity.is_walking 的實測數字。）
        # ⚠ 讀不到玩家物件時保留上一次的判斷，不要當成「停著」——
        #   那會在掃描空窗期狂送移動指令。
        if self.player:
            self._moving = entity.is_walking(self.sc, self.player)

        if self._cur is None:
            # ★ 追怪不限距離；「周圍完全沒有選中的怪」時才去巡邏點找（使用者要求）
            # ★ 只走「現在這張圖」的巡邏點 —— 座標在每張地圖都從 0 開始算，
            #   拿別張圖的點來走會往一個完全不相干的地方衝（使用者要求擋掉）。
            #   分流不算不同圖（天使學園 41/141/241 是同一張），見 scene.map_key。
            if not (self.patrol_cb.isChecked() and self._spots and me):
                return
            # ⚠ 讀不到目前地圖時**寧可不動**：無法確認就走，等於有機會拿 A 圖的
            #   座標在 B 圖亂衝。停下來並把原因寫在狀態列，比默默走錯好。
            if self._scene is None and any(s[2] is not None for s in self._spots):
                self.status.setText(
                    "⛔ 讀不到目前在哪張地圖 → 不移動"
                    "（遊戲改版位移？巡邏點的地圖比對停用中）")
                return
            here = [n for n, s in enumerate(self._spots) if self._spot_here(s[2])]
            if not here:
                self.status.setText(
                    f"⛔ {scene.scene_name(self._scene)} 沒有巡邏點"
                    f"（已存的 {len(self._spots)} 點都在別張地圖）→ 不移動")
                return
            if self._spot_i not in here:
                # 上次要去的點不在這張圖（剛換圖）→ 從這張圖的第一個開始
                self._spot_i = here[0]
            sx, sy, _sid = self._spots[self._spot_i]
            d = math.hypot(me[0] - sx, me[1] - sy)
            if d <= SPOT_SLACK:
                # 到了這個點還是沒怪 → 換下一個點繼續找（只在這張圖的點裡輪）
                self._spot_i = here[(here.index(self._spot_i) + 1) % len(here)]
                self._nav.reset()
                self._walk_t = WALK_GAP        # 下一拍就往新的點走
                self.status.setText(
                    f"巡邏點 {self._spot_i + 1} 沒怪 → 前往下一個"
                    f"（{scene.scene_name(self._scene)} 共 {len(here)} 點）")
                return
            # ★★ 走巡邏點交給 app/game/navigate.py：算不出直達路徑時會 360 度
            #   找中繼點繞過去，而且**允許暫時走遠**（凹形地形非這樣不可）。
            #   舊做法只沿著往目標的直線取點，實測會停在牆前面
            #   「60 秒送 158 次指令、一格沒動」，而且一直顯示「直線可通」。
            # ⚠ 它自己會判斷角色在不在走路、要不要重下指令，所以這裡**不要**
            #   再加 _moving / _walk_t 的節流，會互相打架。
            if not self._ensure_mover():
                self.status.setText("⛔ 移動跳板沒裝上 → 不移動")
                return
            note = self._nav.step(self.sc, self._mover, self.player, sx, sy)
            if self._nav.stuck:
                # ★ 真的到不了就換下一個點（舊版沒有這道，會站到天亮）
                nxt = here[(here.index(self._spot_i) + 1) % len(here)]
                stuck_msg = (f"⛔ 走不到巡邏點 {self._spot_i + 1}"
                             f" ({sx:.0f},{sy:.0f})")
                self._nav.reset()
                if nxt == self._spot_i:
                    self._stop_with(stuck_msg + " → 這張圖只有這一個點，已停止掛機")
                    self.notify(stuck_msg + "，掛機已停止。")
                    return
                self._spot_i = nxt
                self.status.setText(stuck_msg + f" → 改去巡邏點 {nxt + 1}")
                return
            self.status.setText(
                f"周圍沒有選中的怪 → 前往巡邏點 {self._spot_i + 1}"
                f"（{scene.scene_name(self._scene)} 第 "
                f"{here.index(self._spot_i) + 1}/{len(here)} 點）"
                f" ({sx:.0f},{sy:.0f})　還有 {d:.0f} 格　{note}")
            return

        m = self._cur
        hp = self._atk.hp

        mp = entity.read_pos(self.sc, m.addr)
        dist = math.hypot(mp[0] - me[0], mp[1] - me[1]) if (mp and me) else None
        # 施放封包帶目標的格子座標 —— 順移那類對地技能沒有座標發不動。
        # ⚠ 攔包看到遊戲自己送的是 0（`0x664627(技能, 目標, 0, 0, 0)`），
        #   我照著改成 0 試過，**使用者實跑沒有變好，而且順移會廢掉**，
        #   所以改回來。帶座標實測不影響傷害（同一批怪交替測 3 對 3 都打得到）。
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
        # ⚠⚠⚠ **貼在身上的怪不要問尋路** —— 尋路到「自己腳下那一格」本來
        #   就會回 0，而 0 在這裡是「走不過去」的意思。
        #   近戰每打一隻就會貼上去，於是每隻都被判定走不到、丟進冷卻，
        #   然後跑去鎖 3~16 格外的怪 —— 唯讀監控實拍（雪狐）：
        #       目標 16.0 格，而周圍 1.2 / 2.7 格就有怪
        #   使用者的症狀是「沒打幾隻就卡，手動按 F2 也沒用」
        #   （目標欄位裡是那隻遠的，按 F2 打的是打不到的那隻）。
        #   遠程停在 10~12 格所以完全看不出這個問題。
        self._path_t += dt
        if (self._path_t >= PATH_GAP and mp is not None and me
                and dist is not None and dist > NO_PATH_NEED
                and self._mover is not None and self._mover.active):
            self._path_t = 0.0
            n = self._mover.path_to(self.sc, mp[0], mp[1])
            if n >= 0:                 # -1 = 這次沒問到，保留上一次的判斷
                self._path_pts = n
                # ★★ 「連續幾次算不出路徑」**只能在這裡數** —— 這裡才是
                #   真的問到一次新結果。以前放在下面每一拍都跑的地方，
                #   而 _path_pts 每 PATH_GAP(0.2s) 才更新一次，於是
                #   「連續 3 次」實際變成「連續 3 個心跳」= **30 毫秒**：
                #   尋路只要失敗一次，30ms 後就把那隻怪丟掉並加黑名單 60 秒。
                #   症狀是一直換目標、跑來跑去卻沒進帳（實測 3 分鐘裡
                #   39% 的時間在空轉，每秒目標都不一樣）。
                self._unreach = (self._unreach + 1) if n == 0 else 0
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
        # ★★ 不再分近戰／遠程，一律用「送攻擊封包」這一套（使用者定的）。
        #   原本寫死的 WALK_KEEP 換成頁面上的「接戰距離」，其餘流程一字不動。
        #   遠程放 11（技能射程 12）；近戰角色自己調小到 1~2。
        self._keys.mode = self.mode
        want = float(self.range_spin.value())
        blocked = self._path_pts > 1
        # 送不送攻擊：**固定 12 格**，不跟著設定走。
        reach = MELEE_RANGE if blocked else ATTACK_PACKET_RANGE
        in_range = dist is not None and dist <= reach
        # 走到多近：隔著地形就貼臉；否則走到「看過掉血的距離」，最多 want。
        # ⚠ 還沒看過掉血時（_hit_dist = 0）要用 want，**不能用 2 格** ——
        #   否則一開始就一路走到怪臉上（使用者回報的「都走很近」，實測
        #   鎖定距離最小 0.5 格）。
        # ⚠ 下限平常是 MELEE_RANGE(2)；使用者把設定調得比 2 小（近戰）時
        #   跟著降，否則永遠走不進近戰射程。
        keep = (MELEE_RANGE if blocked
                else want if self._hit_dist <= 0
                else max(min(MELEE_RANGE, want), min(want, self._hit_dist)))

        # ★ 尋路說「到不了」→ 換一隻（使用者定的規則）：牠站在走不進去的角落，
        #   我們走不過去、隔著地形也多半打不到，不必耗到 10 秒逾時。
        # ⚠ 只在尋路射程內才算數：更遠時尋路本來就回 0（一次只算得出約
        #   30~40 格），那是要接力走過去，不是到不了。
        # ⚠⚠ **已經打傷的怪絕不放棄**，而且要連續 UNREACH_HITS 次算不出來
        #   才算數 —— 怪會走動，打鬥中某一瞬間牠站到走不進去的格子，
        #   路徑就會變 0。少了這兩條會變成「打一下就換下一隻」，
        #   結果一路結仇、被圍毆致死（使用者實際遇到）。
        # ⚠ 貼在身上的**永遠不算走不到**：都走到牠旁邊了，還需要走去哪？
        if (self._unreach >= UNREACH_HITS and not self._hurt
                and dist is not None
                and NO_PATH_NEED < dist <= PATHFIND_RANGE):
            self._cool_unreach(m.eid)
            self._atk.hold_off()
            self._cur = None
            self._keys.eid = None
            if not self._pick_next():
                self._keys.set_on(False)
                self._since_scan = RESCAN_GAP
            self.status.setText(f"「{m.name}」走不到（卡在地形裡？）→ 換一隻")
            return
        # ★★ 走路目標**一律就是怪本身**，繞路完全交給 walk_route()：
        #   它會對怪尋路，然後照 _approach_point() 的規則決定這一趟走到哪
        #   —— 路徑 2 點以上就走下一個轉角、只剩 1 點才朝怪推進到接戰距離。
        # ⛔ 這裡曾經自己算「路徑倒數第二點再沿最後一段推進」。**拿掉了**：
        #   那等於叫遊戲重新規劃一條到那個點的路（可能走別條），
        #   而且跟 walk_route 的逐點走法互相打架（使用者指出的）。
        gx, gy, gkeep = (mp[0], mp[1], keep) if mp else (None, None, keep)
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
        # ★★ **太近也要下移動指令** —— 到位之後持續微調，攻擊穿插移動，
        #   這是遊戲自己的做法（內建自動打怪 45 秒送了 45 包移動、
        #   每包點數 1，全程把距離維持在 1.4~1.8 格）。
        #   我們原本走一次就不管了，怪自己貼上來就再也調不回去，
        #   然後卡在牠身體裡打不動 —— 而遠程停在 10 格永遠不會發生，
        #   所以同一份程式碼黑狐正常、雪狐會卡。
        need_walk = gd is not None and (
            gd > gkeep + WALK_SLACK or (not in_range and gd > gkeep)
            or gd < move.MIN_GAP)
        # ★ 還在趕路（離目標 > FAR_ENOUGH）就用短冷卻，貼身微調維持 0.4 秒。
        walk_gap = (WALK_GAP_FAR if (gd is not None and gd > FAR_ENOUGH)
                    else WALK_GAP)
        if (self.move_cb.isChecked() and me and not self._moving
                and self._walk_t >= walk_gap and need_walk):
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
            self._why = (f"打得到，同時走近到 {keep:.0f} 格"
                         if self._hit_dist else "打得到，同時走過去（還沒確認射程）")
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

        # ★ 診斷紀錄：每秒把這一拍的**決策**寫進檔案。
        #   從外面只看得到「站太遠」，看不到為什麼不走 —— 這幾輪我猜了太多次。
        #   環境變數 AO_FARM_LOG=1 才會開，平常完全不做事。
        self._dbg_t += dt
        if _FARM_LOG and self._dbg_t >= 1.0:
            self._dbg_t = 0.0
            try:
                with open(f"farm_debug_{self.account}.log", "a",
                          encoding="utf-8") as fh:
                    fh.write(
                        f"{time.strftime('%H:%M:%S')} "
                        f"離怪 {dist if dist is None else round(dist,1)} "
                        f"停 {keep:.1f} 打得到={in_range} "
                        f"路徑點={self._path_pts} 走不到次數={self._unreach} "
                        f"要走={need_walk} 走成功={self._walked_ok} "
                        f"移動中={self._moving} 卡住={self._stuck:.1f}s "
                        f"血={hp} 冷卻中={len(self._killed)} "
                        f"｜{self._why or '正常攻擊中'}\n")
            except OSError:
                pass

        # ⛔ 這裡曾經加過「讀不到座標超過 N 秒就換一隻」—— 拿掉了。
        #    那是用 timeout 蓋過症狀，而且量過根本沒發生：
        #    掃描 100 輪，狀態與玩家物件**都是 100/100 剛好命中 1 個**。

        # 卡住偵測（次要保險，不是主要機制）：目標已經是最近的一隻，
        # 正常情況下不是打得到就是角色正在走過去。若血量不掉、玩家座標也不動，
        # 代表這隻走不過去（隔著地形之類），換一隻。
        # ⚠ 用上面那個「隔 0.3 秒取樣」的結果，不要拿相鄰兩拍比 ——
        #   心跳 10ms，角色每拍才走 0.09 格，那樣比永遠都是「沒在動」，
        #   於是走路途中也會一直累積卡住秒數，走到一半就被判定走不過去換怪。
        # ★ 怪掉血 = 這個距離打得到 → 記下來當作「不用再靠近」的門檻。
        #   這是**唯一**不必知道各角色射程、也不必量測的辦法。
        if 0 < hp < self._last_hp:
            self._hurt = True          # 打傷過的怪就不要再放棄（見上面）
            if dist is not None:
                self._hit_dist = max(self._hit_dist, dist)
        # ⚠⚠ **用「離錨點的淨位移」判斷有沒有前進**，不要用 self._moving。
        #   撞牆時角色會原地抖動（實測約 0.5~0.6 格），而 self._moving 讀的是
        #   遊戲的動畫狀態 —— 撞著牆它照樣是 'Run'，那個計時器就永遠歸零，
        #   唯讀監控實拍卡了 32 秒都沒觸發（見 STUCK_EPS 的說明）。
        #   「有沒有真的前進」只能看位移。
        if me and (self._anchor is None
                   or math.hypot(me[0] - self._anchor[0],
                                 me[1] - self._anchor[1]) > STUCK_EPS):
            self._anchor = me          # 真的移動了 → 重設錨點
            self._stuck = 0.0
        elif (0 < hp < self._last_hp) or self._last_hp < 0:
            self._stuck = 0.0          # 在掉血 = 有進展，站著打也算
        else:
            self._stuck += dt
        self._last_hp = hp
        if self._stuck >= STUCK_SECS:
            self._cool_unreach(m.eid)      # 走不過去，一樣用遞增冷卻
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
        self.move_cb.setChecked(bool(g(self._key("move"), True)))
        self.patrol_cb.setChecked(bool(g(self._key("patrol"),
                                         g(self._key("back"), False))))
        self.boss_cb.setChecked(bool(g(self._key("boss_only"), False)))
        self.notify_cb.setChecked(bool(g(self._key("notify_on"), True)))
        self.buff_cb.setChecked(bool(g(self._key("auto_buff"), False)))
        # 學過的分身技能編號 —— 有存就不用再按 F12 學一次
        self._buff = buff.AutoBuff(
            BUFF_KEY, int(g(self._key("buff_skill"), 0) or 0))
        self.rot_cb.setChecked(bool(g(self._key("rotate"), False)))
        self.sup_gear_cb.setChecked(bool(g(self._key("supply_gear"), False)))
        # 「藥水觸發」拆成 HP／MP 兩個之前存的舊值，兩邊都吃 ——
        # 不然使用者原本設好的會憑空變成沒勾。
        old_potion = bool(g(self._key("supply_potion"), False))
        self.sup_hp_cb.setChecked(bool(g(self._key("supply_hp"), old_potion)))
        self.sup_mp_cb.setChecked(bool(g(self._key("supply_mp"), old_potion)))
        self.sup_jump_cb.setChecked(bool(g(self._key("supply_jump"), False)))
        self.sup_revive_cb.setChecked(
            bool(g(self._key("supply_revive"), False)))
        self.rot_every.setValue(float(g(self._key("rot_every"), 30.0)))
        self.rot_stay.setValue(float(g(self._key("rot_stay"), 60.0)))
        self.rest_hp_cb.setChecked(bool(g(self._key("rest_hp_on"), False)))
        self.rest_hp.setValue(int(g(self._key("rest_hp"), REST_HP_DEFAULT)))
        self.rest_mp_cb.setChecked(bool(g(self._key("rest_mp_on"), False)))
        self.rest_mp.setValue(int(g(self._key("rest_mp"), REST_MP_DEFAULT)))
        self.range_spin.setValue(float(g(self._key("range"), WALK_KEEP)))
        # 巡邏點。兩種舊格式都要吃得下，不然使用者設好的點會憑空消失：
        #   最舊：只有一個「原點」home = [x, y]
        #   舊  ：spots = [[x, y], ...]        ← 沒有地圖，載進來標成「未標記」
        #   現在：spots = [[x, y, 場景編號], ...]
        spots = g(self._key("spots"), None)
        if not spots:
            home = g(self._key("home"), None)
            spots = [home] if (isinstance(home, (list, tuple))
                               and len(home) == 2) else []
        self._spots = []
        for p in spots:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue
            sid = int(p[2]) if len(p) > 2 and p[2] is not None else None
            self._spots.append((float(p[0]), float(p[1]), sid))
        self._refresh_spots()
        if g(self._key("notify"), "sound") == "telegram":
            self.rb_tg.setChecked(True)
        self.tg_id.setText(str(g(self._key("tg_id"), "") or ""))

    def _on_robot_pref(self, _on: bool) -> None:
        """「用天使趴趴GO回地圖」／「死亡自己回練功區」勾選變動。

        一改就存；**掛機中**勾上還會立刻把精靈設定調到位，不必重開掛機。
        取消勾選只代表「我們不再管」—— 不會把遊戲裡的設定改回去。
        """
        self._save_settings()
        if self._loading or not self.run_cb.isChecked():
            return
        if not (self._mover is not None and self._mover.active):
            return
        notes = robot.apply_prefs(
            self._mover, self.sc,
            jump_back=self.sup_jump_cb.isChecked(),
            revive_recall=self.sup_revive_cb.isChecked())
        if notes:
            self.status.setText("精靈設定：" + "、".join(notes))

    def _save_settings(self) -> None:
        if self._loading:
            return
        s = config.set
        s(self._key("monsters"), self.wanted())
        s(self._key("vk"), self.key_box.currentData())
        s(self._key("move"), self.move_cb.isChecked())
        s(self._key("patrol"), self.patrol_cb.isChecked())
        s(self._key("boss_only"), self.boss_cb.isChecked())
        s(self._key("notify_on"), self.notify_cb.isChecked())
        s(self._key("auto_buff"), self.buff_cb.isChecked())
        s(self._key("buff_skill"), int(self._buff.skill or 0))
        s(self._key("rotate"), self.rot_cb.isChecked())
        s(self._key("supply_gear"), self.sup_gear_cb.isChecked())
        s(self._key("supply_hp"), self.sup_hp_cb.isChecked())
        s(self._key("supply_mp"), self.sup_mp_cb.isChecked())
        s(self._key("supply_jump"), self.sup_jump_cb.isChecked())
        s(self._key("supply_revive"), self.sup_revive_cb.isChecked())
        s(self._key("rot_every"), float(self.rot_every.value()))
        s(self._key("rot_stay"), float(self.rot_stay.value()))
        s(self._key("rest_hp_on"), self.rest_hp_cb.isChecked())
        s(self._key("rest_hp"), int(self.rest_hp.value()))
        s(self._key("rest_mp_on"), self.rest_mp_cb.isChecked())
        s(self._key("rest_mp"), int(self.rest_mp.value()))
        s(self._key("range"), float(self.range_spin.value()))
        s(self._key("spots"), [[x, y, sid] for x, y, sid in self._spots])
        s(self._key("notify"), "telegram" if self.rb_tg.isChecked() else "sound")
        s(self._key("tg_id"), self.tg_id.text().strip())
        config.save()

    def _wire_saving(self) -> None:
        """所有設定一改就存 —— 不要讓使用者每次都重設一遍。"""
        self.picked.model().rowsInserted.connect(self._save_settings)
        self.picked.model().rowsRemoved.connect(self._save_settings)
        self.key_box.currentIndexChanged.connect(self._save_settings)
        self.move_cb.toggled.connect(self._save_settings)
        self.patrol_cb.toggled.connect(self._save_settings)
        self.boss_cb.toggled.connect(self._save_settings)
        self.rot_cb.toggled.connect(self._save_settings)
        self.rot_every.valueChanged.connect(self._save_settings)
        self.rot_stay.valueChanged.connect(self._save_settings)
        self.range_spin.valueChanged.connect(self._save_settings)
        self.rb_tg.toggled.connect(self._save_settings)
        self.tg_id.editingFinished.connect(self._save_settings)

    # ------------------------------------------------------------------
    def notify(self, msg: str) -> None:
        """送警報通知。設定是這個分身自己的（通知列在頁面最上面）。

        ⚠ 受「啟用通知」總開關管（使用者要求）。關掉只是不送通知，
          該停的還是會停 —— 停機原因照樣寫在狀態列。
        """
        if self._notifier is None or not self.notify_cb.isChecked():
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
        # ★ AOB 自動定位的結果。平常是空的；遊戲改版讓位址位移時會在這裡說出來，
        #   不然使用者只會看到「怪怪的」卻不知道發生什麼事（上次改版就是這樣）。
        self.locate_lbl = QLabel("")
        bar.addWidget(self.locate_lbl)
        bar.addStretch(1)
        root.addLayout(bar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ⛔ 這裡原本有一大段說明文字，使用者要求全部拿掉 ——
        #    每個控制項自己的滑鼠提示已經寫得夠清楚了。

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    def _show_locate(self) -> None:
        """把 AOB 自動定位的結果顯示出來（沒事就不顯示）。"""
        moved, failed = locate.moved(), locate.failed()
        if failed:
            self.locate_lbl.setText(
                f"⚠ 有 {len(failed)} 個遊戲位址定位失敗（沿用舊值）："
                + "、".join(failed[:3]))
            self.locate_lbl.setStyleSheet("color: #e0b040;")
        elif moved:
            self.locate_lbl.setText(
                f"偵測到遊戲改版，已自動重新定位 {len(moved)} 個位址")
            self.locate_lbl.setStyleSheet("color: #7fc97f;")
        else:
            self.locate_lbl.setText("")

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
            # ★ 接上第一台就用 AOB 掃一次，把所有寫死的遊戲位址換成當下正確的
            #   —— 遊戲改版會讓它們整批位移（見 app/game/locate.py）。
            #   只做一次（五台載的是同一份 angel.dat），失敗就保留原值。
            try:
                locate.warm(sc)
                self._show_locate()
            except Exception:                  # noqa: BLE001
                pass                           # 定位是加分項，壞掉不能擋住掛機
            self._scanners.append(sc)
            insts.append((w.pid, w.hwnd, w.title, sc))
        if not insts:
            self.found.setText("找不到分身")
            return
        self._show_locate()
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
            # ⚠ 用預讀的快取，**不要叫 charname.read_character_name()** ——
            #   那是全記憶體掃字串，一台 1.1~1.8 秒、五台就是七、八秒的凍結
            #   （使用者回報「第一次切過去會卡一下」）。見 app/core/preload.py。
            nm = preload.name_of(pid, sc, acct)
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
            # ⚠ 一定要還掉移動 hook —— 不還原 IAT 就等於在遊戲裡留了一段跳板。
            #   ★ 用 release() 不要直接 stop()：跳板是同一個 PID 共用的，
            #     能量晶化分頁可能還在用（見 move.acquire）。
            if page._mover is not None:
                move.release(page.pid, page)
                page._mover = None
            # ⚠ 警報也要停：BeepThread／QMediaPlayer 還在響的話，物件被回收
            #   時等於「執行緒還在跑就被解構」，而且聲音會一直放到關掉程式。
            if page._notifier is not None:
                try:
                    page._notifier.stop()
                except Exception:                    # noqa: BLE001
                    pass
        stuck = False
        for th in [t for t in (self._worker, self._inv) if t] + self._keys:
            th.stop()
            if not th.wait(5000):
                stuck = True
        self._worker = None
        self._inv = None
        self._keys = []
        self.tabs.clear()
        self._pages = {}
        # ⚠⚠ 有執行緒沒收乾淨就**不要關控制碼**：牠可能正卡在
        #   ReadProcessMemory 裡（AOB 全掃跑很久），控制碼在腳下被關掉，
        #   最壞的情況是那個控制碼值被系統回收給別的物件用。
        #   寧可留著不關（行程結束時作業系統自然會收）。
        if stuck:
            self._scanners = []
            return
        for sc in self._scanners:
            try:
                sc.close()
            except Exception:
                pass
        self._scanners = []

    def on_close(self) -> None:
        self._timer.stop()
        self._teardown()
