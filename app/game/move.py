"""讓角色走到指定座標。

為什麼需要注入
--------------
移動沒有「改一個欄位就會動」的捷徑（都試過了）：
  · 玩家物件 `+0x144/+0x148` 確實是目的地，但**我們自己寫進去角色完全不動**
    ——那是遊戲寫給自己看的，不像攻擊目標 `+0x2D8` 會被重讀。
  · 竄改怪的座標、借攻擊指令去走：能設定目的地，但要同時贏過「伺服器持續更新
    怪的座標」與「進入攻擊範圍就停」兩個機制，實測角色會亂走，不能用。

所以走遊戲自己的移動函式，讓它處理尋路封包、混淆與加密。

兩個函式（反組譯 + 實測參數，見 app/core/injector.py 的參數擷取）
---------------------------------------------------------------
    0x549B81   尋路。__thiscall，eax = 算出來的路徑點數（0 = 算不出來）
               f(u16 目標世界X, u16 目標世界Y, 輸出緩衝=0x9B6684)
               ★ **一般移動一律先走這個** —— 它會繞過地形。

    0x55A046   送出移動封包。f(起點世界X, 起點世界Y, 路徑點數, 亂數)
               封包 = 上面四個欄位 + 全域陣列 0x9B6684 的 路徑點數*4 bytes
               每個路徑點 = 兩個 u16 世界座標（一格 32，見 app/game/entity.py）
               最後那個是 16 位元亂數（實測 7 筆全不同、跟 GetTickCount 對不上、
               也不遞增），隨便給即可。

怎麼呼叫（不開新執行緒）
------------------------
掛 **PeekMessageA 的 IAT**（遊戲訊息迴圈每幀都會呼叫），stub 平時只做兩條指令的
旗標檢查；要移動時把參數寫好、旗標設 1，下一幀遊戲主執行緒就會替我們呼叫一次。

★ 一定要在**遊戲主執行緒**上呼叫，不能開 remote thread —— 那個函式會碰遊戲的
連線物件與封包佇列，在別的執行緒上動它等於資料競爭。
這跟 app/core/injector.py 的 send 攔截是同一套已驗證的 IAT hook 機制。

實測（黑狐）：短距離誤差 0.00 格、位置穩定不回彈（伺服器認）；
長距離從 (139.5,31.5) 走到 (54,40) 共 85.9 格、8.5 秒，全程自動繞地形。
遊戲全程正常、IAT 乾淨還原。

⚠ 尋路算不出路徑時 walk_to() 回 False 而**不會**退回直線走 ——
直線只會讓角色貼著牆推、原地卡住。回 False 讓呼叫端換目標比較好。
"""
from __future__ import annotations

import math
import struct
import threading
import time

from app.game import entity

MOVE_FN = 0x00559F28        # 移動封包建構＋送出函式（指令表 0x201）
WAYPOINTS = 0x009B6684      # 全域路徑點陣列
# ★ 遊戲自己的「走到指定座標」完整常式（反組譯確認，見 [[command-table]]）：
#       f(this, 起點X, 起點Y, 終點X, 終點Y, flag)   全部是世界座標，cdecl
#   內容：起終點同格就直接返回 → 用 this 尋路到終點（寫進 WAYPOINTS）
#         → 有路就送移動封包 → 收尾（0x549B59 / 0x5B5C60）
#   比我們原本「自己呼叫尋路再自己送 MOVE_FN」多做了兩個收尾步驟，
#   那正是遊戲自己走路時會做的，所以改用它。
WALK_FN = 0x005D7C87
# ★ 遊戲自己的尋路。反組譯 0x556982~0x556990（滑鼠點地板走路那條路）：
#       push 0x9B6684        ← 輸出：路徑點陣列
#       push edi             ← 目標 Y（世界座標）
#       push esi             ← 目標 X
#       mov  ecx, ebx        ← this（**不是**我們掃到的玩家物件，見下）
#       call 0x549B81        ← eax = 算出來的路徑點數量，0 = 算不出來
PATHFIND_FN = 0x00549A63
PATH_MAX_TILES = 28.0       # 尋路的有效範圍實測約 30~40 格，超過就回 0
# ★★ **走路終點離目標的下限**，比這個近就不再往前。
#   1.4 = √2，剛好一個斜格 —— 這是遊戲自己的數字：唯讀觀察遊戲內建的
#   自動打怪 45 秒，離怪的距離**最近就是 1.4、最遠 1.8**，一次都沒有更近。
#   走得比這個近，遊戲會判定角色卡在怪的身體裡（使用者的觀察），
#   而且踩到過更嚴重的：走到怪自己的格子上時伺服器不給站，
#   整段移動被退回，客戶端停在「移動中」狀態，攻擊全部被忽略。
#   ⚠ 這是**下限**，不是設定值 —— 使用者把接戰距離調到 1.0，實際還是 1.4。
MIN_GAP = 1.4
# 維持距離的容差：離目標在 [keep-SLACK, keep] 之間就當作已經站好，不再微調。
# 沒有它的話每一拍都會推一小步，看起來就是抖動。
SLACK = 0.4
# ⛔ PATH_TRY／DETOUR_TRY（沿直線取中繼點、再 ±40°/±70° 換角度試）已刪除，
#   2026-08-10。那是還沒有地形圖的年代留下的猜測式繞路：中繼點取在往目標的
#   直線上，隔著岩層時每一個都在牆裡，於是角色一路推牆（使用者實拍「卡在
#   牆邊直到周圍怪物重生」）。現在繞路一律由 app/game/terrain.py 的 A* 算好
#   再用 `points=` 傳進來 —— 那是真正的最短路，而且純讀記憶體。

# --- 尋路要的 this ----------------------------------------------------------
# ⚠⚠ **不是**我們用 VT_PLAYER 掃到的那個位址，而是**它 −8**。
#     第一次直接拿掃到的位址去呼叫，遊戲當場崩潰 —— 就是本專案踩過兩次的老坑：
#     同一個物件有兩個 vtable、相隔 8 bytes。
# 來源鏈（`0x5045DE` 只是表查找，可以純讀取重現，不必呼叫）：
MGR_PTR = 0x0096E638        # 管理器指標
# ⚠⚠ 底下四個偏移**跟 bag.py 是同一組**（同一個場景管理器、同一張實體表）。
#   以前這裡自己抄了一份 —— 那正是 CLAUDE.md 明令禁止的「同一個位址記在
#   兩個地方」：`bag.OFF_MY_ID` 已經進 locate.SIGS 會自動跟上改版，這裡那份
#   不會跟，於是改版後一邊對一邊錯，而且沒有任何警告。改成直接引用。
def _bag():
    from app.game import bag        # 延後 import：bag 也會用到 move
    return bag


class _MgrOff:
    """場景管理器的欄位偏移 —— 一律轉問 bag.py（那邊才是唯一來源）。"""

    @property
    def ID(self) -> int:            # 本尊的實體 ID（locate 會更新 bag 那份）
        return _bag().OFF_MY_ID

    @property
    def TBL(self) -> int:           # ID → 物件的表
        return _bag().OFF_ENT_TABLE

    @property
    def MAX(self) -> int:           # 表的上限
        return _bag().OFF_ENT_CAP

    @property
    def OBJ_ID(self) -> int:        # 物件裡回存的 ID（要跟查表用的一致才算數）
        return _bag().OFF_ENT_ID


MGR = _MgrOff()


def _approach_point(here: tuple[float, float],
                    pts: list[tuple[float, float]],
                    keep: float) -> tuple[float, float] | None:
    """這一趟要走到哪 —— **只在路徑的最後一段上退，絕不退過倒數第二個點**。

    規則（使用者定的）：
      · 路徑有 **2 個點以上** = 中間還有地形要繞 →
        **走下一個轉角（pts[0]）就好，一個點一個點照著走**。
        走到之後下一拍重新尋路，剩下的路徑會少一個點，如此推進。
      · 路徑只剩 **1 個點** = 中間完全沒有障礙物 →
        這時才朝目標直走，走到剩 keep 格為止，然後開始送攻擊。

    ⚠⚠ **不可以直接跳到倒數第二個點，也不可以沿整條路徑往回退。**
      直接跳過去等於叫遊戲重新規劃一條到那裡的路（可能走別條）；
      沿整條退則會退過轉角，落在「看不到怪」的地方，站在那裡打不到。
      只有「最後一段」保證跟目標之間沒有地形。
    """
    if not pts:
        return None
    # ★ 首個轉折點就在腳下時要跳過：尋路的第一個點常常是「目前所在的格子」，
    #   走去它＝原地踏步（2026-08-07 黑狐實錄：站 11.7 格重覆下指令 4.4 秒
    #   一步沒動，就是一直走去 pts[0]＝自己腳下）。
    while len(pts) >= 2 and math.hypot(pts[0][0] - here[0],
                                       pts[0][1] - here[1]) < 1.0:
        pts = pts[1:]
    if len(pts) >= 2:
        # ★★ **趕路時整段走完**（keep <= 0 = 呼叫端不需要保持距離）。
        #   遊戲自己點地圖就是這樣：一包最多 5 個路徑點，一段走 67 格
        #   （使用者攔到的封包解出來的）。一個轉角一個轉角走的話，每個轉角
        #   都要停下來等指令排隊（實測 107~154ms），30 格有四個轉角就多停
        #   半秒 —— 那就是「走路卡卡的」。
        #   實測（黑狐，183 格）：改成整段走完，去程 9 段 35 秒 → 7 段 25 秒，
        #   回程 32 段 135 秒 → 10 段 49 秒。
        # ⚠ **打怪的接近不走這條**（keep 一定 > 0）：那邊要控制跟怪的距離，
        #   規則是調過好幾輪的，不要動。
        if keep <= 0:
            return pts[-1]
        return pts[0]              # 照著路徑走下一個轉角
    tx, ty = pts[-1]               # 只剩最後一個點 = 直線可通
    # ★ 不管設定多小，離目標一律留 MIN_GAP —— 太近會被判定卡在怪身體裡。
    keep = max(keep, MIN_GAP)
    seg = math.hypot(tx - here[0], ty - here[1])
    if seg > keep:                 # 太遠 → 往前走到剩 keep 格
        r = (seg - keep) / seg
        return (here[0] + (tx - here[0]) * r, here[1] + (ty - here[1]) * r)
    if seg >= MIN_GAP:             # 在 [MIN_GAP, keep] 帶內 → 不動
        return None
    # ★★ **太近 → 退回到 keep + SLACK**（維持距離，這是遊戲自己的做法）。
    #   唯讀觀察遊戲內建的自動打怪 45 秒：移動封包 45 包（1 秒 1 包、
    #   點數都是 1），全程把距離維持在 1.4~1.8 格 —— 它是一邊持續微調
    #   一邊打的，不是走到定位就不管了。
    #   我們原本走一次就停，怪自己貼上來就再也調不回去，然後卡在牠身體裡
    #   （遠程停在 10 格永遠不會發生，所以同一份碼黑狐正常、雪狐會卡）。
    # ⚠ 只退到「剛好脫離重疊」（MIN_GAP+SLACK = 1.8，正好是遊戲觀察到的
    #   上緣 1.4~1.8），**不要退回 keep** —— 遠程的 keep 是 10 格，
    #   退回去等於放風箏，那是行為改變（使用者要求別影響黑狐）。
    want = MIN_GAP + SLACK
    if seg < 0.05:
        # 完全重疊，算不出方向 —— 隨便挑一個方向退開，總比不動好
        return (tx + want, ty)
    return (tx + (here[0] - tx) / seg * want,
            ty + (here[1] - ty) / seg * want)


def pathfinder_this(scanner) -> int | None:
    """算出尋路要的 this；算不出或驗不過回 None（那就別呼叫）。

    驗證（實測 5/5 台）：算出來的物件 +0xC6/+0xCA（int16 世界座標）÷32
    與玩家的格子座標完全一致，而且它正好等於「VT_PLAYER 掃到的位址 − 8」。
    """
    def u32(a):
        raw = scanner._read_bytes(a, 4)
        return struct.unpack("<I", raw)[0] if raw else None

    mgr = u32(MGR_PTR)
    if not mgr:
        return None
    ident, tbl, mx = u32(mgr + MGR.ID), u32(mgr + MGR.TBL), u32(mgr + MGR.MAX)
    if None in (ident, tbl, mx) or (ident & 0xFFFF) > mx:
        return None
    obj = u32(tbl + (ident & 0xFFFF) * 4)
    if not obj or u32(obj + MGR.OBJ_ID) != ident:
        return None
    return obj


def entity_alive(scanner, eid: int) -> bool:
    """實體 id 在場景實體表裡**現在**查不查得到（純讀，不呼叫遊戲）。

    跟 `pathfinder_this()` 走同一條鏈（同一張 ID→物件表、同一組 bag 偏移；
    那條鏈就是遊戲查表函式 `0x5045DE` 的純讀重現，見檔頭 MGR 的說明），
    差別只是 id 由呼叫端給。查到、而且物件回存的 id 一致才算活著。

    ★ 用途（2026-08-16）：`quickbar.self_entity_ok` 在按快捷鍵前驗
      「自己實體」還查不查得到 —— 換地圖／重連的實體重建空窗裡查不到，
      而遊戲的 usequickkey 對查表結果**不驗 NULL 就寫入**（崩潰 dump
      EIP=0x5B75F0 ×2 定案）。讀不到一律回 False：讀不到本身就是
      「世界正在拆建」的訊號。
    """
    def u32(a):
        raw = scanner._read_bytes(a, 4)
        return struct.unpack("<I", raw)[0] if raw else None

    if not eid:
        return False
    mgr = u32(MGR_PTR)
    if not mgr:
        return False
    tbl, mx = u32(mgr + MGR.TBL), u32(mgr + MGR.MAX)
    if tbl is None or mx is None or not tbl or (eid & 0xFFFF) > mx:
        return False
    obj = u32(tbl + (eid & 0xFFFF) * 4)
    return bool(obj) and u32(obj + MGR.OBJ_ID) == eid
# 尋路／移動舉手之後，最多等這麼久拿指令槽。
# 攻擊那邊看到有人舉手就會跳過一拍（約 50ms），所以這個時間綽綽有餘；
# 而且是在 UI 執行緒上等，不能太長。
SLOT_YIELD = 0.12
HOOK_IMPORT = "PeekMessageA"
MAX_POINTS = 32             # 一次最多送幾個路徑點（封包大小 = 點數*4+9）


# 注入區塊的版面
_FLAG, _ORIG, _A1, _A2, _CNT, _A4, _DONE, _FN = (
    0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C)
_A5, _ECX, _RET, _ESP = 0x20, 0x24, 0x28, 0x2C
_A6 = 0x30                  # 第六個參數（WALK_FN 要六個）
_BUSY = 0x34                # 跳板正在替我們執行某個函式（見 _stub_asm）
_CODE = 0x40
# BUSY 卡住這麼久就當作那次呼叫在遊戲那邊被例外吃掉了，強制解鎖。
# 我們叫的函式都是幾毫秒的東西，5 秒遠遠超過任何正常情況。
_BUSY_STUCK_SECS = 5.0
_SCRATCH = 0x800            # 配置的是 0x1000，程式碼用不到 0x100，這之後全空
# 第二段程式碼區（lua.py 的「原子序列」stub 放這裡）：主 stub 在 _CODE(0x40)
# 起、實測不到 0x100，所以 0x200 起到 _SCRATCH 之間整段是空的。
_AUX_CODE = 0x200
_AUX_CODE_MAX = _SCRATCH - _AUX_CODE


def _stub_asm(block: int) -> str:
    """旗標一舉起就呼叫一次移動函式，否則原封不動跳回真正的 PeekMessageA。

    ⚠ keystone 把無前綴數字當十六進位，所以一律寫 0x。
    ⚠ pushad/pushfd 必須成對而且順序相反地還原，否則遊戲的暫存器會壞掉。
    ★ 呼叫前後**保存/還原 esp**，不要用 `add esp, N` 收尾。
      因為要呼叫的函式有兩種慣例：
        · `0x55A046`（送移動封包）是 cdecl —— 由我們清堆疊
        · `0x549B81`（尋路）是 __thiscall —— **由它自己清**，而且要設 ecx
      固定 `add esp,0x14` 遇到後者就會把 esp 推高 12 bytes → 堆疊壞掉 → 當場崩潰。
      存 esp 再還原，兩種都安全，也不必記哪個函式是哪種。

    ★ 一律推六個參數：推得比它需要的多不會有事，它只是不看後面幾個。
      （六個是因為 WALK_FN 0x5D7D96 要 this + 起點XY + 終點XY + flag。）

    ⚠⚠⚠ **`_BUSY` 這道閂不能拿掉**（2026-08-06 補的）。

      我們掛的是 **PeekMessageA** —— 而我們請遊戲呼叫的函式裡，有些自己會
      抽訊息（開視窗那類的 UI／Lua 函式，例如 `CreateRobotWindow`、
      `OnPressSetSearchPoint`）。那就會**再進來一次這段 stub**。

      沒有閂的話：巢狀那次會把 `_ESP` 覆蓋成它自己的 esp，外層執行完
      `mov esp,[_ESP]` 就把 esp 設成裡層的值 → `popfd`／`popad` 讀到垃圾
      → `jmp [_ORIG]` 帶著壞掉的堆疊與暫存器跳進真正的 PeekMessageA。
      遊戲的訊息迴圈執行緒從此堆疊錯位，然後在**不知道多久以後**掛掉。
      本專案記過兩次「客戶端訊息迴圈卡死」（[[jumpmap]] 的清單、
      [[lua-engine]] 連打十幾輪），症狀完全吻合。

      有閂的話：巢狀那次看到 `_BUSY` 就直接跳過，而且**旗標留著不清**，
      所以那個請求不會遺失，下一幀再做。
    """
    return f"""
    pushad
    pushfd
    mov eax, dword ptr [{block + _FLAG:#x}]
    test eax, eax
    jz skip
    cmp dword ptr [{block + _BUSY:#x}], 0x0
    jnz skip
    mov dword ptr [{block + _FLAG:#x}], 0x0
    mov dword ptr [{block + _BUSY:#x}], 0x1
    mov dword ptr [{block + _ESP:#x}], esp
    push dword ptr [{block + _A6:#x}]
    push dword ptr [{block + _A5:#x}]
    push dword ptr [{block + _A4:#x}]
    push dword ptr [{block + _CNT:#x}]
    push dword ptr [{block + _A2:#x}]
    push dword ptr [{block + _A1:#x}]
    mov ecx, dword ptr [{block + _ECX:#x}]
    mov eax, dword ptr [{block + _FN:#x}]
    call eax
    mov esp, dword ptr [{block + _ESP:#x}]
    mov dword ptr [{block + _RET:#x}], eax
    mov dword ptr [{block + _BUSY:#x}], 0x0
    inc dword ptr [{block + _DONE:#x}]
    skip:
    popfd
    popad
    jmp dword ptr [{block + _ORIG:#x}]
    """


class Mover:
    """單一遊戲行程的「呼叫遊戲函式」跳板（移動、攻擊封包都借它）。

    ⚠ **指令槽只有一個**，而且會被兩邊同時用：
        · 移動 —— UI 執行緒的 walk_to()
        · 攻擊封包 —— 送鍵執行緒的 attack.select() / attack.strike()
      沒有鎖的話兩邊會交錯寫進同一塊參數區，變成「用 A 的函式配 B 的參數」
      —— 這遊戲對錯誤參數的反應是**當場崩潰**（踩過）。所以整個
      「寫參數 → 舉旗標 → 等執行完」必須是不可分割的一段。
    """

    def __init__(self, pid: int, exe_path: str) -> None:
        # ⚠⚠ **不要自己 new 一個** —— 請用 `move.acquire(pid, exe, 你自己)`。
        #   同一個遊戲行程只能有一份跳板，兩份會互相拆掉（見 acquire 的說明）。
        self._pid = pid
        self._exe = exe_path
        self._pm = None
        self._iat = 0
        self._orig = 0
        self._block = 0
        self._active = False
        self._gone = False          # 讀不到跳板了＝遊戲行程已經不在（見 _sink）
        self._busy_since = None     # _BUSY 從什麼時候舉著（卡住自救用）
        # RLock 而非 Lock：call_sync() 內部會再呼叫 call()，同一條執行緒要能重入。
        self._lock = threading.RLock()
        # ★ 「有人在等指令槽」的計數（見 slot_wanted）。
        # ⚠ `+= 1` 不是原子操作，而且**不能拿 `_lock` 來保護** —— 舉手的意思
        #   就是「我還沒拿到 _lock」，拿它就直接卡住了。所以另外用一把小鎖。
        #   數錯的後果不是崩潰而是**攻擊永遠停擺**：計數卡在非零 →
        #   `slot_wanted` 恆為 True → 攻擊每一拍都讓路（見 attack._yield_now）。
        self._wanted = 0
        self._wanted_lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._active

    @property
    def installed(self) -> bool:
        """遊戲的 IAT 現在**還指著我這一份**跳板嗎。

        ⚠⚠ 2026-09-03 使用者實機事故：`active` 只代表「我裝過而且區塊還讀得到」，
          **不代表 IAT 還指著我**。別人（另一個行程的工具、上一輪沒收乾淨的
          孤兒清除、手動還原）只要把 IAT 換掉，我們送的每一個呼叫就會寫進一塊
          **沒有人在跑**的區塊 —— 旗標舉著沒人收，`call_sync` 全部逾時，
          畫面上看起來就是「點不到物件／走不動／打不到」，而且**不會有任何錯誤**。
          當天就是這樣：黑狐的 IAT 指回真正的 PeekMessageA，副本整趟等於在空轉。
        """
        if not (self._active and self._pm and self._iat and self._block):
            return False
        try:
            return self._pm.read_uint(self._iat) == self._block + _CODE
        except Exception:                      # noqa: BLE001
            return False

    @property
    def gone(self) -> bool:
        """跳板讀不到了 —— 遊戲行程已經不在（跟「還沒裝」要分得開）。"""
        return self._gone

    @property
    def slot_wanted(self) -> bool:
        """尋路／移動那邊正在等指令槽 —— 攻擊要讓路。

        ⚠⚠ 沒有這個機制的話，攻擊執行緒會把指令槽佔到 **82%**（實測），
          UI 想問「這隻怪跟我之間有沒有障礙物」幾乎永遠問不到，
          於是一直當成「沒有障礙物、已經走夠近」→ 站著打卻打不到，
          直到 10 秒卡住偵測才換怪（使用者回報的「掛機還是會卡住」）。
        """
        return self._wanted > 0

    # --- 跟遊戲行程之間的讀寫 ------------------------------------------------
    # ⚠⚠⚠ 底下三支是**這個檔案唯一可以直接碰 pymem 的地方**。
    #   本專案除了 injector 之外，其他讀取都走 scanner（讀不到回 None）；
    #   只有這裡是裸的 pymem，也就是唯一會把例外丟給呼叫端的讀取點。
    #
    #   使用者實際遇到的當機（crash.log）：遊戲被關掉之後 ReadProcessMemory
    #   丟 ERROR_PARTIAL_COPY(299)，例外從 pymem 一路往上穿過
    #   call_sync → attack._send → channel.switch → 掛機分頁的 tick
    #   （QTimer 掛在 UI 執行緒上），全域例外攔截接到就是**整個工具箱關閉**
    #   —— 多開的話所有分身一起被關掉。
    #
    #   遊戲不在了屬於「大聲停用」而不是崩潰：把 `_active` 放掉，之後每個
    #   呼叫都回「排不進去」，分頁看得到 `.active` 是 False、`.gone` 是 True。
    def _sink(self) -> None:
        """跳板讀寫不到了 → 整份作廢，別再對一個不存在的行程下指令。"""
        self._active = False
        self._gone = True

    def _rd(self, off: int) -> int | None:
        """讀跳板區塊的一格；讀不到回 None（並讓整份跳板作廢）。"""
        try:
            return self._pm.read_uint(self._block + off)
        except Exception:                      # noqa: BLE001
            self._sink()
            return None

    def _wr(self, off: int, val: int) -> bool:
        """寫跳板區塊的一格；寫不進去回 False（並讓整份跳板作廢）。"""
        try:
            self._pm.write_uint(self._block + off, val & 0xFFFFFFFF)
            return True
        except Exception:                      # noqa: BLE001
            self._sink()
            return False

    @property
    def lock(self) -> threading.RLock:
        """要「連續送好幾個呼叫、中間不能被插隊」時，自己抓著它。

        例如攻擊三連包必須照 ①②③ 送出，不能被移動指令切開。
        """
        return self._lock

    @staticmethod
    def _orphan_orig(pm, iat: int) -> int | None:
        """IAT 上如果是**我們自己留下的孤兒跳板**，回傳它記著的原始函式位址。

        ⚠⚠ 為什麼需要這個：工具箱如果不是正常關閉（當掉、直接砍行程），
          `stop()` 就不會跑，IAT 會一直指著那塊配置的跳板。遊戲照常運作
          （跳板本身還在），但**下次再安裝就會疊成兩層** —— 誰先收尾誰後收尾
          會決定 IAT 最後指到一塊已經釋放的記憶體，那才是真的會崩。
          實際發生過：使用者被迫關掉工具箱三次，最後發現那個 hook 是孤兒。

        判定條件很嚴（寧可不動，也不要亂改別人的 hook）：
          · IAT 指向的位址**不在任何模組裡**（＝確實被 hook 了）
          · 往前 `_CODE` 當成我們的區塊起點，讀出的 `_ORIG`
            **必須落在 USER32.dll 裡**（那才是真正的 PeekMessageA）
          · ★ 而且**不能是本行程裡還活著的那一份**（見下面）

        ⚠⚠⚠ 最後那條是 2026-08-06 補的。少了它，「還活著的跳板」跟「孤兒」
          長得一模一樣（兩個條件都符合），於是第二個分頁安裝時會把第一個
          分頁**還在用的**跳板拆掉：第一個的 `_active` 仍是 True、旗標永遠
          不會被清，之後每一個呼叫都回「排不進去」，而且不會自己好。
          正解是根本不要有第二份 —— 見 `acquire()`；這裡是第二道防線。
        """
        try:
            cur = pm.read_uint(iat)
            block = cur - _CODE
            orig = pm.read_uint(block + _ORIG)
            mods = [(m.lpBaseOfDll, m.lpBaseOfDll + m.SizeOfImage,
                     (m.name or "").lower()) for m in pm.list_modules()]
        except Exception:                          # noqa: BLE001
            return None
        if any(lo <= cur < hi for lo, hi, _ in mods):
            return None                            # 本來就沒被 hook
        if _is_live_block(block):
            return None                            # 那是本行程還在用的，別碰
        for lo, hi, name in mods:
            if lo <= orig < hi and name.startswith("user32"):
                return orig
        return None

    def start(self) -> None:
        """安裝 hook。失敗會丟例外，呼叫端要能接住（沒有移動功能也要能掛機）。"""
        import keystone
        import pymem

        from app.core import injector

        iat = injector._resolve_iat(self._exe, HOOK_IMPORT)
        if not iat:
            raise RuntimeError(f"匯入表裡找不到 {HOOK_IMPORT}")
        pm = pymem.Pymem()
        pm.open_process_from_id(self._pid)
        # ★ 先清掉「上次沒收乾淨」留下的孤兒跳板，不要疊第二層（見 _orphan_orig）
        orphan = self._orphan_orig(pm, iat)
        if orphan:
            injector.SendCapture._protect(pm, iat, 8, 0x40)
            pm.write_uint(iat, orphan)
        orig = pm.read_uint(iat)
        block = pm.allocate(0x1000)
        for off, val in ((_FLAG, 0), (_ORIG, orig), (_A1, 0), (_A2, 0),
                         (_CNT, 0), (_A4, 0), (_DONE, 0), (_FN, MOVE_FN),
                         (_A5, 0), (_A6, 0), (_ECX, 0), (_RET, 0),
                         (_ESP, 0), (_BUSY, 0)):
            pm.write_uint(block + off, val)

        ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
        ks.syntax = keystone.KS_OPT_SYNTAX_INTEL
        shell, _ = ks.asm(_stub_asm(block), addr=block + _CODE)
        pm.write_bytes(block + _CODE, bytes(shell), len(shell))

        injector.SendCapture._protect(pm, iat, 8, 0x40)   # EXECUTE_READWRITE
        pm.write_uint(iat, block + _CODE)
        self._pm, self._iat, self._orig = pm, iat, orig
        self._block = block
        self._active = True
        self._gone = False          # 重新裝起來了（上一份可能是因為遊戲關掉才作廢）

    def scratch(self) -> int:
        """跳板那塊配置頁裡可以自由使用的暫存區位址（沒裝好回 0）。

        版面：變數 0x00~0x33、跳板程式碼從 _CODE(0x40) 起（實測不到 0x100），
        所以 _SCRATCH 之後整段都是空的。拿來放要傳給遊戲函式的字串
        —— 例如 Lua 的函式名（見 app/game/lua.py）。

        ★ 這樣別的模組就不必去摸 `_block` / `_pm` 這些私有欄位。
        """
        return (self._block + _SCRATCH) if self._active else 0

    def aux_code(self) -> tuple[int, int]:
        """給**第二段程式碼**用的區段 (位址, 大小)；沒裝好回 (0, 0)。

        lua.py 的「原子序列」stub 放這裡（_AUX_CODE=0x200 起、到 _SCRATCH
        為止）。頁面是 pymem allocate 的預設 EXECUTE_READWRITE，
        寫進去就能執行 —— 主 stub（_CODE=0x40）本來就是這樣跑的。
        """
        if not self._active:
            return 0, 0
        return self._block + _AUX_CODE, _AUX_CODE_MAX

    def write(self, addr: int, data: bytes) -> bool:
        """往遊戲行程寫一段位元組（給 scratch 區與 Lua 堆疊用）。"""
        if not self._active:
            return False
        try:
            self._pm.write_bytes(addr, bytes(data), len(data))
        except Exception:                      # noqa: BLE001
            self._sink()                       # 遊戲不在了（見 _sink）
            return False
        return True

    def call(self, fn: int, a1: int = 0, a2: int = 0, a3: int = 0,
             a4: int = 0, a5: int = 0, a6: int = 0, ecx: int = 0) -> bool:
        """請遊戲主執行緒呼叫 fn(a1..a5)；ecx 給 __thiscall 的 this。
        回傳是否排得進去。

        ★ 一律推五個參數。目標函式都是 cdecl（呼叫端清堆疊），
          參數比它需要的多不會有事 —— 它只是不看後面那幾個。

        只有一個指令槽：上一個還沒被執行完就回 False，呼叫端自己決定要不要重試。
        訊息迴圈每秒跑 60 次以上，我們的用量（每秒個位數）綽綽有餘。

        ⚠⚠⚠ **`_BUSY` 也要看**，不能只看 `_FLAG`（2026-08-06 修）。
          stub 是「讀旗標 → **清旗標** → 執行函式」，所以函式跑到一半時
          `_FLAG` 已經是 0 了。以前只擋 `_FLAG`，等於**在 stub 正在讀參數的
          時候把參數改掉** —— 遊戲就會拿「A 的函式配 B 的參數」去跑。
          本類別開頭那句「這遊戲對錯誤參數的反應是當場崩潰」講的就是這個，
          而最毒的組合是 `ACTION_FN(技能編號, …)`：第一個參數本來該是物件
          指標，變成 0x101 之類的小整數 → 解參考 → 遊戲當場掛掉。
          機率很低，但一秒二十幾次、五個分身、掛好幾個小時 ——
          正好就是「偶爾、掛久了才當、完全重現不出來」的樣子。
        """
        # ★★ 先確認 IAT 還指著我們這一份（見 `installed`）——不然旗標舉了
        #   沒有人會來收，呼叫端只會看到「逾時」而完全不知道跳板已經被換掉。
        if not self.installed:
            self._sink()
            return False
        if not self._active:
            return False
        if not fn:
            # ⚠ fn=0 是 locate.warm()「函式位址定位失敗」的記號（改版後
            #   特徵對不上）。這裡是所有遊戲函式呼叫的唯一閘口 —— 擋下來
            #   ＝該功能大聲停用，絕不拿沒驗證過的位址叫遊戲執行。
            return False
        with self._lock:                       # ⚠ 見類別說明：不能兩邊交錯寫
            flag = self._rd(_FLAG)
            if flag is None or flag:
                return False                   # 讀不到（遊戲沒了）／上一個還沒被領走
            if not self._busy_ok():
                return False                   # 上一個還在遊戲那邊跑
            # ⚠ `_FN` **最後才寫**（在 _FLAG 之前）：stub 的讀取順序是
            #   參數 → ecx → fn，寫入順序跟它一致，萬一真的有人插隊也是
            #   「舊函式配舊參數」而不是「新函式配舊參數」。
            for off, val in ((_A1, a1), (_A2, a2), (_CNT, a3), (_A4, a4),
                             (_A5, a5), (_A6, a6), (_ECX, ecx), (_FN, fn)):
                if not self._wr(off, val):
                    return False               # 寫到一半遊戲沒了 → 旗標沒舉，安全
            if not self._wr(_FLAG, 1):
                return False
        return True

    def _busy_ok(self) -> bool:
        """跳板現在有空嗎？（`_BUSY` 沒舉起來）

        ⚠ 附帶「卡住自救」：函式如果在遊戲那邊被例外掀掉，stub 尾巴的
          `_BUSY = 0` 就不會執行，跳板會永遠鎖死。所以卡超過
          `_BUSY_STUCK_SECS` 就強制解鎖 —— 我們叫的都是幾毫秒的函式，
          卡 5 秒一定是出事了。
        """
        busy = self._rd(_BUSY)
        if busy is None:
            return False                           # 讀不到（遊戲沒了）→ 不要下指令
        if not busy:
            self._busy_since = None
            return True
        now = time.monotonic()
        if self._busy_since is None:
            self._busy_since = now
            return False
        if now - self._busy_since < _BUSY_STUCK_SECS:
            return False
        self._wr(_BUSY, 0)                         # 卡太久 → 自救
        self._busy_since = None
        return True

    def call_sync(self, fn: int, *args, ecx: int = 0,
                  timeout: float = 0.5) -> int | None:
        """呼叫並**等它真的做完**，回傳 eax；逾時回 None。

        用在「先尋路、拿到點數，再送移動封包」這種有先後關係的兩步。
        指令槽只有一個，所以一定要等前一個做完才排下一個。

        ⚠⚠⚠ **要等的是 `_DONE` 變大，不是 `_FLAG` 變 0**（2026-08-06 修）。
          stub 的順序是「讀旗標 → 清旗標 → 執行函式 → 寫 eax → `_DONE` 加一」，
          所以旗標在**函式開始跑之前**就已經是 0 了。以前等旗標等於
          「請求被領走了」就回傳 —— 那時函式根本還沒跑完，讀到的 `_RET`
          是**上一次**呼叫的回傳值。
          實際影響：尋路回報的路徑點數是上一次的（尋路本身要 5~6ms，而輪詢
          間隔 5ms，所以這是常態不是邊界情況），接著 `read_path()` 又在遊戲
          還在寫 `WAYPOINTS` 的時候去讀 —— 拿到半舊半新的路線，就走去奇怪的
          地方。`_DONE` 本來就存在而且就是為此設計的，只是沒被用到。

        ⚠⚠ **逾時要把旗標清掉**，否則指令槽會永久卡住：遊戲主執行緒只要忙一下
          （開視窗、等伺服器）就會來不及執行，旗標留在 1，之後**每一個**呼叫都
          回「排不進去」，而且不會自己好。實際踩過：叫了一次開視窗之後，那個
          分身的所有呼叫全部失效，只能重開遊戲。
          清掉是安全的：stub 是「讀到旗標 → 先清掉 → 再執行」，所以我們寫 0
          最壞情況只是那個指令不執行 —— 那本來就是逾時的意思。
          （已經被領走、正在跑的那次不受影響，它跑完會自己把 `_BUSY` 放掉。）
        """
        # ⚠ 鎖要**含等待那段**：只鎖 call() 的話，別人可以在我們等結果時排下一個
        #   呼叫，我們就會讀到別人的 eax。
        with self._lock:
            # ⚠ 這幾個讀寫都走 `_rd`／`_wr`：遊戲被關掉時它們回 None／False
            #   （並讓跳板作廢），**不會丟例外把呼叫端連同整個工具箱帶走**。
            #   見 `_sink()` 的說明（使用者實際遇到的當機就是停在這一行）。
            done0 = self._rd(_DONE)
            if done0 is None or not self.call(fn, *args, ecx=ecx):
                return None
            t0 = time.time()
            while time.time() - t0 < timeout:
                cur = self._rd(_DONE)
                if cur is None:
                    return None                          # 遊戲沒了
                if cur != done0:
                    return self._rd(_RET)
                time.sleep(0.005)
            self._wr(_FLAG, 0)                           # 取消還沒被領走的
        return None

    def path_to(self, scanner, tile_x: float, tile_y: float,
                wait: float = 0.0) -> int:
        """**只算路徑、不移動**，回傳路徑點數。

        這是「中間有沒有障礙物」最直接的答案，不必先走一步再看：
            1 個點  = 直線過得去
            多個點  = 要繞，代表中間有地形
            0       = 算不出路徑（那個方向不通，或超出尋路範圍）
           -1       = 指令槽正被別人用（攻擊封包），這次先跳過，等一下再問
        一次呼叫約一幀（實測 5～6ms），挑到新目標時算一次就夠。

        ⚠ **這個函式常常是在 UI 執行緒上呼叫的**，所以預設 wait=0：
          搶不到鎖就馬上回 -1，絕不卡住畫面。攻擊執行緒送三連包時會連續
          佔著指令槽（約 50ms／次、每 50ms 一次），阻塞等待等於凍結整個介面
          —— 使用者回報的「打一打卡住」就是這樣來的。
        """
        this = pathfinder_this(scanner)
        if not this:
            return 0
        # 舉手說「我要用指令槽」，攻擊那邊看到就會讓一拍出來（見 slot_wanted）。
        with self._wanted_lock:
            self._wanted += 1
        try:
            got = self._lock.acquire(timeout=max(wait, SLOT_YIELD))
        finally:
            with self._wanted_lock:
                self._wanted -= 1
        if not got:
            return -1
        try:
            tx = int(tile_x * entity.TILE_UNITS) & 0xFFFF
            ty = int(tile_y * entity.TILE_UNITS) & 0xFFFF
            n = self.call_sync(PATHFIND_FN, tx, ty, WAYPOINTS,
                               ecx=this, timeout=0.15)
        finally:
            self._lock.release()
        return n if (n and 0 < n <= MAX_POINTS) else 0

    def walk_route(self, scanner, player_obj: int,
                   tile_x: float, tile_y: float, stop_short: float = 0.0,
                   wait: float = 0.12, points: list | None = None) -> int:
        """★ 對**目標本身**尋路，走它算出來的那條路，停在離終點 stop_short 格。

        回傳路徑點數（0 = 算不出路徑）。

        為什麼要有這個（跟 walk_to() 的差別）
        --------------------------------------
        walk_to() 是「先在往目標的**直線上**取一個點，再對那個點尋路」。
        那個幾何點落在牆後面時尋路就失敗，接著往回縮短還是同一條直線、
        還是撞牆，等於把遊戲算得出來的繞路整條丟掉 ——
        使用者實拍：站在原地 32 秒撞牆，而遊戲自己點地圖是走得過去的。

        這裡反過來：先算到目標的完整路徑，再照 `_approach_point()` 的規則
        決定要走到哪 —— **只在最後一段上退**，那一段跟目標之間保證沒有地形。
        （實測遊戲點地圖走長路時，就是連送好幾包、每包最多 5 個點，
          起點是上一段的終點 —— 那條路正是這裡算出來的東西。）

        ⚠⚠ **一定要用 WALK_FN，不能直接送 MOVE_FN**：MOVE_FN 只把封包送出去，
          少了 WALK_FN 的兩個收尾（0x549B59 設目的地 / 0x5B5C60）。實測用
          MOVE_FN 走完之後站在 1.0 格連送 67 發攻擊**全部零傷害**，
          換回 WALK_FN 立刻正常 —— 移動狀態沒收乾淨，攻擊會被忽略。
        ⚠ 終點只用遊戲算出來的路徑點或那些點之間的內插，**不要自己捏座標**：
          落在目標自己的格子上時伺服器不給站，整段移動會被退回
          （症狀是「往前走然後縮回原點」）。

        points: 呼叫端**已經自己算好的路徑點**（格子座標，最後一個是終點）。
          給了就不再問遊戲的尋路，直接照這條路走 —— 省下一次 5~6ms 的呼叫，
          更重要的是不必跟攻擊搶指令槽。現在掛機與巡邏都是從地形圖
          （app/game/terrain.py）算好再傳進來的。
          ⚠ 那些點必須是**真的可走的格子**（地形圖算出來的就是），
            自己捏的座標會被伺服器退回。
        """
        if not self._active or not player_obj:
            return 0
        this = pathfinder_this(scanner)
        if not this:
            return 0
        raw = scanner._read_bytes(player_obj + entity.OFF_POS_X, 8)
        if not raw:
            return 0
        wx, wy = (v >> 16 for v in struct.unpack("<II", raw))
        cx, cy = wx / entity.TILE_UNITS, wy / entity.TILE_UNITS

        with self._wanted_lock:            # 舉手要指令槽，攻擊會讓一拍
            self._wanted += 1
        try:
            got = self._lock.acquire(timeout=max(wait, SLOT_YIELD))
        finally:
            with self._wanted_lock:
                self._wanted -= 1
        if not got:
            return 0
        try:
            # ★ 呼叫端已經算好路了（地形圖）→ 完全不必問遊戲的尋路。
            if points:
                pts = [(float(x), float(y)) for x, y in points]
                gx, gy = _approach_point((cx, cy), pts, stop_short) or pts[-1]
                self.call(WALK_FN, this, wx, wy,
                          int(gx * entity.TILE_UNITS) & 0xFFFF,
                          int(gy * entity.TILE_UNITS) & 0xFFFF, 0)
                return len(pts)

            def path(px: float, py: float) -> int:
                v = self.call_sync(
                    PATHFIND_FN,
                    int(px * entity.TILE_UNITS) & 0xFFFF,
                    int(py * entity.TILE_UNITS) & 0xFFFF,
                    WAYPOINTS, ecx=this, timeout=0.15)
                return v if (v and 0 < v <= MAX_POINTS) else 0

            # ⛔⛔ 「算不出來就沿直線取 28/18/10/5 格的中繼點，再 ±40°/±70°
            #   亂試」那一整段**刪掉了**（2026-08-10 使用者指定）。
            #   它是「卡在牆邊直到怪重生」的直接來源：中繼點全部取在
            #   **往目標的直線**上，目標與我之間隔著岩層時每一個中繼點都在
            #   牆裡，尋路回 0；好不容易試到一個算得出來的，方向也是朝著牆
            #   —— 角色就一路推著牆。
            #   現在走路一律用**我們自己讀地形圖算出來的路**（points=），
            #   這條沒有 points 的路只剩「一次就要算得出來」的短程用途，
            #   算不出來就回 0 讓呼叫端換目標。
            n = path(tile_x, tile_y)
            if not n:
                return 0
            pts = self.read_path(scanner, n)
            gx, gy = _approach_point((cx, cy), pts, stop_short) or pts[-1]
            self.call(WALK_FN, this, wx, wy,
                      int(gx * entity.TILE_UNITS) & 0xFFFF,
                      int(gy * entity.TILE_UNITS) & 0xFFFF, 0)
            return n
        finally:
            self._lock.release()

    def walk_near(self, scanner, player_obj: int, tile_x: float,
                  tile_y: float, keep: float) -> bool:
        """**近距離微調**：不尋路，直接走到「離目標 keep 格、在我這一側」的點。

        為什麼要有它（2026-08-06 實拍）：唯讀跟拍抓到雪狐**站在 2.2 格、
        怪滿血、沒在走、卡 8.2 秒**。近戰打得到 2.0 格，2.2 就差那麼一點，
        照理該再走近 —— 但走不了：`walk_route()` 一開頭要對怪尋路，而
        **尋路到貼身的目標一定回 0**（等於算路徑到自己腳下那一格），
        接力用的中繼點又都比實際距離遠，於是整趟回 0、一步都沒動。
        角色就這樣杵著，直到怪自己走過來或卡住偵測逾時 —— 使用者說的
        「發呆一段時間然後又開始打」。

        ⚠ 這裡**故意不尋路**：短短一兩格用不著，而且尋路正是壞掉的那一步。
        ⚠ 目的地一律在「怪 → 我」這條線上、離怪 keep 格 —— 不會落在怪自己
          的格子上（那會被伺服器整段退回，見 [[walk-to-monster-rules]] 規則①）。
        ⚠ 一樣走 `WALK_FN`，不是 `MOVE_FN`（規則②：少了收尾攻擊會被忽略）。
        ⚠ 只給**短距離**用（呼叫端自己判斷）：長距離、隔著地形還是要
          `walk_route()` 讓遊戲算路。
        """
        if not self._active or not player_obj:
            return False
        this = pathfinder_this(scanner)
        if not this:
            return False
        raw = scanner._read_bytes(player_obj + entity.OFF_POS_X, 8)
        if not raw:
            return False
        wx, wy = (v >> 16 for v in struct.unpack("<II", raw))
        cx, cy = wx / entity.TILE_UNITS, wy / entity.TILE_UNITS
        dx, dy = cx - tile_x, cy - tile_y
        d = math.hypot(dx, dy)
        keep = max(keep, MIN_GAP)
        if d < 0.05:                       # 完全重疊，方向算不出來 → 隨便挑一邊
            dx, dy, d = 1.0, 0.0, 1.0
        gx, gy = tile_x + dx / d * keep, tile_y + dy / d * keep
        with self._wanted_lock:            # 跟 walk_route 一樣要舉手搶指令槽
            self._wanted += 1
        try:
            got = self._lock.acquire(timeout=SLOT_YIELD)
        finally:
            with self._wanted_lock:
                self._wanted -= 1
        if not got:
            return False
        try:
            return self.call(WALK_FN, this, wx, wy,
                             int(gx * entity.TILE_UNITS) & 0xFFFF,
                             int(gy * entity.TILE_UNITS) & 0xFFFF, 0)
        finally:
            self._lock.release()

    def walk_exact(self, scanner, player_obj: int,
                   tile_x: float, tile_y: float) -> bool:
        """**精確走到那一格**（不留距離、不尋路）。

        跟 `walk_near` 是同一條路（送 `WALK_FN` 目的地讓遊戲自己走），
        差別只有一個：**不套 `MIN_GAP`**。

        ⚠ 為什麼要分開一支：`MIN_GAP`(1.4) 是**為了打怪**才有的 ——
          停在怪身體裡會被判定重疊。但「使用者自己站過的格子」
          （製作檯位置、定位點）沒有這個問題，硬套下限的結果就是
          **永遠差一格多**，使用者 2026-08-12 回報「走過去跟我定的位置不同」。
        ⚠ 終點必須是**真的可走的格子**（見 [[walk-to-monster-rules]] 規則①）
          —— 呼叫端要保證，這裡不檢查。使用者站過的地方本來就可走。
        ⚠ 一樣走 `WALK_FN` 不是 `MOVE_FN`（規則②）。
        ★ 只給短距離用：長距離請走 `navigate.Navigator`（讀地形圖算 A*）。
        """
        if not self._active or not player_obj:
            return False
        this = pathfinder_this(scanner)
        if not this:
            return False
        raw = scanner._read_bytes(player_obj + entity.OFF_POS_X, 8)
        if not raw:
            return False
        wx, wy = (v >> 16 for v in struct.unpack("<II", raw))
        with self._wanted_lock:            # 跟 walk_near 一樣要舉手搶指令槽
            self._wanted += 1
        try:
            got = self._lock.acquire(timeout=SLOT_YIELD)
        finally:
            with self._wanted_lock:
                self._wanted -= 1
        if not got:
            return False
        try:
            return self.call(WALK_FN, this, wx, wy,
                             int(tile_x * entity.TILE_UNITS) & 0xFFFF,
                             int(tile_y * entity.TILE_UNITS) & 0xFFFF, 0)
        finally:
            self._lock.release()

    @staticmethod
    def read_path(scanner, count: int) -> list[tuple[float, float]]:
        """讀出剛才 path_to() 算好的路徑點（格子座標）。

        ★ 要**緊接在 path_to() 之後**讀 —— 路徑點寫在全域陣列 WAYPOINTS，
          下一次尋路就會覆蓋掉。
        路徑的最後一個點是目標本身，所以**倒數第二個點到目標之間一定是直線**
        （中間若有地形，尋路會再插一個轉折點）。走到那個點就能無阻礙地打。
        """
        raw = scanner._read_bytes(WAYPOINTS, max(0, count) * 4)
        if not raw:
            return []
        return [(x / entity.TILE_UNITS, y / entity.TILE_UNITS)
                for x, y in struct.iter_unpack("<HH", bytes(raw))]

    # ⛔ walk_to() 已移除（沒有呼叫者）。它是「在直線上取中繼點、
    #   算不出來就 ±70° 繞路」的舊做法，已經被兩層取代：
    #     近距離 → walk_route()（對目標本身尋路，沿路徑往回退）
    #     遠距離 → navigate.Navigator（360° 找中繼點、允許暫時走遠）
    #   舊做法在凹形地形會死循環（實測 60 秒送 158 次指令、一格都沒動），
    #   細節見 app/game/navigate.py 的檔頭。

    # ⛔ walk_path() / calls_done() 已移除（零呼叫者，2026-08-07 清理）。
    #   walk_path 是「自己把路徑點寫進 WAYPOINTS 再叫 MOVE_FN」的低階入口，
    #   docstring 指的 walk_to() 早已拆掉；手寫路徑點的封包版面見 git 紀錄
    #   與 memory 的 move-packet-function。

    def stop(self) -> None:
        """還原 IAT。**一定要呼叫** —— 不還原就等於在遊戲裡留了一段跳板。

        ⚠ 要拿鎖：別條執行緒（攻擊、移動）可能正卡在 `call()`／`call_sync()`
          中間。不拿鎖的話，牠可能在我們清掉旗標之後、還原 IAT 之前又把旗標
          設起來，等於對一段馬上就要拆掉的跳板下指令。
        """
        if not self._active:
            return
        with self._lock:
            try:
                from app.core import injector

                self._pm.write_uint(self._block + _FLAG, 0)
                # ⚠ IAT 已經不是我們的了（別人換掉／已經還原過）就別動它 ——
                #   硬寫回自己的 `_orig` 會把**別人的** hook 拆掉。
                if self._pm.read_uint(self._iat) == self._block + _CODE:
                    injector.SendCapture._protect(self._pm, self._iat, 8, 0x40)
                    self._pm.write_uint(self._iat, self._orig)
            except Exception:               # noqa: BLE001
                pass
            self._active = False


# ---------------------------------------------------------------------------
# 一個遊戲行程共用一份跳板
# ---------------------------------------------------------------------------
# ⚠⚠⚠ **同一個 PID 絕對不能有兩份 Mover。**
#
# 踩過的實際狀況：掛機分頁裝好跳板正在跑，使用者切到能量晶化分頁按了晶化，
# 那邊又 new 了一份 Mover 並 start()。`start()` 會先叫 `_orphan_orig()` 清掉
# 「上次沒收乾淨的孤兒跳板」，而**還活著的跳板跟孤兒長得一模一樣**
# （IAT 指向模組外、區塊裡的 _ORIG 落在 user32），於是它把掛機那份拆掉。
# 掛機那邊的 `_active` 仍然是 True、旗標永遠不會被清 ——
# 之後每一次攻擊／移動都回「排不進去」，而且不會自己好，只能重開工具箱。
#
# 所以改成註冊制：`acquire()` 拿、`release()` 還，最後一個人還完才真的 stop()。
# 誰持有用 owner 物件記（分頁自己），同一個 owner 重複 acquire 不會重複計數。
_shared: dict[int, "Mover"] = {}
_owners: dict[int, set] = {}
_shared_lock = threading.Lock()


def _is_live_block(block: int) -> bool:
    """這個區塊是不是本行程裡某個還活著的 Mover 的？（給 _orphan_orig 用）"""
    with _shared_lock:
        return any(mv.active and mv._block == block for mv in _shared.values())


def acquire(pid: int, exe_path: str, owner) -> "Mover":
    """拿這個遊戲行程的共用跳板；還沒裝就裝一份。裝不起來會丟例外。

    owner: 隨便一個可雜湊的物件（通常是呼叫的分頁自己），用來記「誰還在用」。
    ⚠ 用完一定要 `release(pid, owner)`，否則跳板會一直留在遊戲裡。
    """
    with _shared_lock:
        mv = _shared.get(pid)
        if mv is not None and mv.active and mv.installed:
            _owners.setdefault(pid, set()).add(owner)
            return mv
        if mv is not None and not mv.installed:
            # ★ 快取裡那份的 IAT 已經被別人換掉了 → 它是死的，重裝一份。
            #   ⛔ 不叫 mv.stop()：現在 IAT 指的是別人的東西，寫回我們的
            #     `_orig` 會把別人的 hook 也拆掉（start() 的孤兒清除會處理）。
            _shared.pop(pid, None)
            _owners.pop(pid, None)
        # 沒有、或上一份已經被 stop 掉了 → 裝一份新的
        mv = Mover(pid, exe_path)
    # ⚠ start() 會讀寫遊戲記憶體、還會 import keystone（第一次要幾百毫秒），
    #   不要抓著全域鎖做 —— 別的 PID 也在等這把鎖。
    mv.start()
    with _shared_lock:
        cur = _shared.get(pid)
        if cur is not None and cur.active and cur is not mv:
            # 極少見：等 start() 的時候別人也裝好了。把自己這份收掉，用他的。
            mv.stop()
            _owners.setdefault(pid, set()).add(owner)
            return cur
        _shared[pid] = mv
        _owners.setdefault(pid, set()).add(owner)
    return mv


def release(pid: int, owner) -> None:
    """還掉。**最後一個人還完才真的還原 IAT。**"""
    with _shared_lock:
        owners = _owners.get(pid)
        if owners is not None:
            owners.discard(owner)
            if owners:
                return                  # 還有別人在用，跳板要留著
            _owners.pop(pid, None)
        mv = _shared.pop(pid, None)
    if mv is not None:
        mv.stop()                       # ⚠ 在全域鎖外面做：stop() 自己會拿 mover 的鎖
