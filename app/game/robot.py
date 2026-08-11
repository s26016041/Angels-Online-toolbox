"""官方外掛「天使守護精靈」的開關與設定 —— 全部透過遊戲自己的 Lua 呼叫。

    robot.is_run(sc)                    # 天使守護精靈開著嗎
    robot.apply_prefs(mover, sc, ...)   # 照掛機分頁的勾選把精靈調成該有的樣子
    robot.begin_supply(mover, sc)               # ① 調好開關（等 SETUP_SETTLE 秒）
    robot.do_recall(mover, sc, 背包表頭)         # ② 回程 → 補給開跑
    robot.end_supply(mover, sc)         # 收尾（只關自動攻擊）

為什麼要用它
------------
使用者要的是「掛機時裝備壞了 → 回程 → 自動修裝買水 → 回來繼續」。
這整套官方精靈本來就會做，而且是**它自己的設定頁**（回城道具、要買什麼、
修完回不回戰場）在決定 —— 我們自己重做一份只會又多一套要維護的東西。

所以分工是：**打怪是我們的掛機，補給那一趟交給精靈。**

「精靈開著沒」＝ `RUN_FLAG`（0 = 開、-1 = 關），就是 `game.setrobotisrun`
寫的那個全域。**開始自動戰鬥時會自動把主開關開起來**（使用者要求，
見 `apply_prefs`）；除此之外只讀不寫。

⚠ 精靈的補給設定（要買什麼、回城道具放哪一格）**要使用者自己在遊戲裡設好**，
  我們只負責在對的時機湊齊觸發條件。
"""
from __future__ import annotations

import struct
import time

from app.core.memory import VALUE_TYPES
from app.game import itemname, lua

# ⚠ 這兩個值會被 locate.warm() 依 AOB 重新定位，不要在別處複製。
RUN_FLAG = 0x009CFBA4        # 0 = 精靈執行中、-1 = 停止
# 精靈設定的單例（見下面「直接讀記憶體」那一段）
VAR_MGR_PTR = 0x0096E630

RUN_ON, RUN_OFF = 0, -1
# ★ `setrobotisrun` 動旗標之前會先看這個 byte（見 set_run 的反組譯筆記）：
#   [ [quickbar.MGR_PTR] + ROBOT_READY_OFF ] == 0 → 遊戲自己也不動旗標。
#   ⚠ 這個管理物件跟快捷欄是同一個 —— 直接用 quickbar.MGR_PTR（那份有
#     AOB 自動定位蓋著），**不要在這裡再寫死一份**：以前這裡有第二份
#     0x009B66AC，改版位移後 quickbar 那份會跟上、這份不會，而且它不在
#     SIGS 裡，failed() 永遠不會提醒。取用要在函式內（quickbar.MGR_PTR
#     是 locate.warm() 之後才變成新值的模組屬性，import 時抄一份會凍住舊值）。
#   ⚠ 2026-08-11 改版：0xF35C → 0xF2FC（−0x60，跟快捷欄表、角色屬性同一批）。
#     那次的 setrobotisrun 也小改了寫法：`lea esi,[ecx+0xF2FC] / call 取那個
#     byte 的 getter（0x54D950 就是 mov al,[ecx]; ret）`，讀的還是同一個 byte。
#     沒做成 AOB 特徵的原因：開／關兩支函式除了最後 jmp 的目標之外一模一樣，
#     遮掉 rel32 就分不出來（實測 2 個命中）。壞掉的後果只是這道 guard 讀到
#     別的 byte —— 要嘛說「精靈還沒準備好」（安全退化），要嘛照常寫旗標
#     （寫的位址本身有 AOB 蓋著），不會做出危險的事。
ROBOT_READY_OFF = 0xF2FC

# ---------------------------------------------------------------------------
# ★★★ 直接從記憶體讀精靈的設定（不必呼叫 Lua）
# ---------------------------------------------------------------------------
# 2026-08-06 反組譯 `getrobotvar_int` / `getrobotvar_bool` 得到（工具：
# `tools/find_robot_vars.py`）。以前每讀一個設定要三趟跳板往返去跑 Lua，
# 讀 10 個就是 30 趟、零點幾秒，而且全程在畫面執行緒上等 —— 現在是純讀取。
#
#   getrobotvar_int(L):
#       eax = 0x54E068(0)            ← 取設定管理員單例 = [VAR_MGR_PTR]
#       eax = 0x54E1D4(this=單例, DATAID)   __thiscall, ret 4
#
#   0x54E1D4:
#       lea esi,[ecx+4]              ← 單例 +4 就是那個 map
#       call 0x54D8E5                ← map.find(&DATAID)
#       cmp eax,[esi] / je 找不到    ← 跟 [map+0]（頭節點）比 = 沒找到
#       mov eax,[eax+0x14]           ← 節點 +0x14 = 值記錄的指標
#       cmp byte [eax+8],1 / jne 0   ← 值記錄 +8 = 型別（1=int）
#       mov eax,[eax+0xC]            ← 值記錄 +0xC = 值
#   讀 bool 的 0x54E179 一模一樣，只差型別要 **2**、值取 1 個 byte。
#
# 那是一棵 MSVC 的 std::map 紅黑樹，節點版面：
#       +0x00 左  +0x04 父  +0x08 右  +0x0C 顏色  +0x0D 是不是哨兵
#       +0x10 鍵(DATAID, int32)      +0x14 值記錄指標
#   map 物件：+0x00 頭節點(哨兵)、+0x04 筆數。根 = 哨兵的「父」。
#
# ✅ 實測（雅典娜-4，116 筆設定）：讀出來的值跟先前用 Lua 讀到的完全一致
#    —— HP藥水1 = 道具 4836（高效紅藥水(活動)）、MP藥水1 = 道具 4837、
#    裝備損壞回城 = True、回城後修理裝備 = True。
VAR_T_INT, VAR_T_BOOL = 1, 2
_N_LEFT, _N_PARENT, _N_RIGHT = 0x00, 0x04, 0x08
_N_ISNIL, _N_KEY, _N_VAL = 0x0D, 0x10, 0x14
_V_TYPE, _V_VALUE = 0x08, 0x0C
_TREE_MAX_DEPTH = 48         # 116 筆的紅黑樹頂多十幾層，48 是給爛資料的煞車


def _u32(scanner, addr: int):
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", raw)[0] if raw else None


_MISSING = object()          # 樹讀得到，但裡面沒有這個設定（遊戲會回 0）


def _find_var(scanner, data_id: int):
    """照遊戲自己的做法在紅黑樹裡二分找。

    回傳「值記錄」的位址、`_MISSING`（樹是好的但沒這一筆）、
    或 `None`（**整條路走不通** —— 位址失效、改版、遊戲還沒載完）。
    這三者要分清楚：只有 None 才該退回 Lua；`_MISSING` 要跟遊戲一樣回 0。
    ⚠ 全程只讀不寫。
    """
    mgr = _u32(scanner, VAR_MGR_PTR)
    if not mgr or not 0x10000 < mgr < 0x7FFF0000:
        return None
    head = _u32(scanner, mgr + 4)
    if not head or not 0x10000 < head < 0x7FFF0000:
        return None
    node = _u32(scanner, head + _N_PARENT)          # 根 = 哨兵的父
    if node is None:
        return None
    for _ in range(_TREE_MAX_DEPTH):
        if not 0x10000 < node < 0x7FFF0000:
            return None
        # ★ 整個節點一次讀完（0x18 bytes）：左/右/哨兵旗標/鍵/值都在裡面。
        #   分開讀是每一層三次系統呼叫，這樣是一次。
        raw = scanner._read_bytes(node, _N_VAL + 4)
        if not raw:
            return None
        if raw[_N_ISNIL]:
            return _MISSING                         # 走到哨兵 = 沒這個設定
        key, val = struct.unpack_from("<iI", raw, _N_KEY)
        if data_id == key:
            return val if 0x10000 < val < 0x7FFF0000 else _MISSING
        node = struct.unpack_from(
            "<I", raw, _N_LEFT if data_id < key else _N_RIGHT)[0]
    return None                                     # 太深 = 資料不對，別再走


def _read_var(scanner, data_id: int, want_type: int):
    """讀一個設定。**行為跟遊戲的 getrobotvar_* 完全一樣**：

    · 有這一筆而且型別對 → 值
    · 沒這一筆、或型別不對 → 0 / False（反組譯裡就是 `xor eax,eax`）
    · **整棵樹讀不到** → None，呼叫端要退回 Lua
    """
    rec = _find_var(scanner, data_id)
    if rec is None:
        return None
    zero = False if want_type == VAR_T_BOOL else 0
    if rec is _MISSING:
        return zero
    raw = scanner._read_bytes(rec + _V_TYPE, 8)     # 型別(4) + 值(4)
    if not raw:
        return None
    if raw[0] != want_type:
        return zero                                 # 型別不對 → 跟遊戲一樣回 0
    value = struct.unpack("<i", raw[4:8])[0]
    return bool(value & 0xFF) if want_type == VAR_T_BOOL else value

# ★★ 兩段等待都是使用者實測調出來的，**不要為了「快一點」去縮**。
#   ⓐ 開關調好之後要**等一下再回程**：太快送回程，精靈那邊還沒進入狀態，
#      整趟就不會啟動。使用者回報 2~4 秒比較穩，取 3。
SETUP_SETTLE = 3.0
#   ⓑ **回程送出之後**「自動攻擊」要留一段時間才能關：
#      立刻關（0 秒）→ 只到城裡就停住，不修裝也不走回去（實測過）
#      隔 5 秒關     → 可以（使用者確認）
#   ⚠ 這 5 秒是從**回程送出**開始算，不是從調設定開始算 —— 先前一度以為
#     5 秒不夠而改成 6，其實是那時計時的起點不對（設定和回程還沒拆成兩段）。
AF_HOLD_SECS = 5.0

# 精靈的變數代號（從遊戲的 Lua 全域常數讀出來的，不是猜的）
AF_IS_AUTO_FIGHT = 1001      # AF_BOL_ISAUTOFIGHT
# ⚠ 「攻擊指定敵人」（面板控制項 974）。**開著補給就不會觸發**（使用者實測）。
AF_ATTACK_TARGET_ONLY = 1006  # AF_BOL_ISATTACKTARGET
AS_BACK_NO_HP_ITEM = 1500    # 補 HP 物品用完自動回城
AS_BACK_NO_MP_ITEM = 1501    # 補 MP 物品用完自動回城
AS_BACK_BROKEN_EQ = 1502     # ★ 裝備損壞回城
AS_BACK_NO_SPACE = 1504      # 背包滿了回城
AS_IS_REPAIR = 1508          # ★ 回城後修理裝備
# ⚠ 1509 是「使用**標記傳送捲軸**回練功點」，不是「補給完回去戰鬥」。
#   （對照 SETTING/BASE/WND01.XML 控制項 10073 的 appdata。我一開始標錯過。）
#   關著的話精靈是**用走的**走回原練功點 —— 距離太遠或走不到就會停在城裡。
AS_USE_RETURN_SCROLL = 1509
AS_IS_BUY_ITEM = 1511        # 購買物品保持身上數量（⚠ 這**不是**回城觸發條件）

# ★★ 補給頁的「購買清單」（勾了 AS_IS_BUY_ITEM 進城會照這張表補貨）。
#   兩個 DATAID 是**平行陣列**：TOBUY_ID[i] 放種類 ID、TOBUY_NUM[i] 放
#   「要保持的數量」，同一列共用索引 i。
#   （39556 實機對照：ID 清單第 14 格=1905 天使之翼、NUM 清單第 14 格=50，
#     跟補給頁畫面一致 —— 平行索引就是這樣確認的。）
AS_TOBUY_ID = 1512           # AS_INTLIST_TOBUYID
AS_TOBUY_NUM = 1517          # AS_INTLIST_TOBUYNUM
# 清單型設定在同一棵樹裡，型別碼 4（字串清單是 6）。記錄版面
# （反組譯 0x54DF4C append／0x54E2C0 get_at／0x54F297 set_at，
#   全量在 reports/intlist_probe*.txt）：
#     +0x08 型別(4)　+0x0C 筆數　+0x10 起內嵌 int32 元素
# ★ 記錄一次配足 0x1F50 bytes、容量寫死 0x7CF=1999 筆 ——
#   **內嵌陣列、永不搬家**，所以從外面照遊戲的步驟寫是安全的：
#   append＝先寫元素、再把筆數 +1（0x54DF4C 就是這兩步）。
VAR_T_INTLIST = 4
_L_COUNT, _L_ELEMS, _L_CAP = 0x0C, 0x10, 0x7CF
# 清單類編輯完，遊戲的 add/set/remove 收尾都會把管理員單例 +0x4C 的
# 髒 byte 設 1（0x54EDEF 整支就這一行）—— 我們編輯完也照做，
# 遊戲才知道設定變了。⚠ 只有 1 個 byte。
_MGR_DIRTY_OFF = 0x4C
# 回程補給要幫使用者保持的天使之翼數量（使用者定的）。
BUY_KEEP_WINGS = 50

# ★「輔助」頁的陣亡自動復活。DATAID 從遊戲的 Lua 全域常數純讀出來
#   （DATAID_AUTO_RESURRECTION_CHECK / _MODE），「復活方式」的值意義是把
#   `ResurrectionSelected` 的 bytecode 反組譯確認的：
#     settitle(getstring(1955 + 列號)) → setrobotvar_int(2102, 列號)
#   —— 值就是下拉選單的列號，而三列的字串是 1955 靈魂、1956 回城、1957 原地。
#   （五台分身實測都設在 2100=True、2102=1，跟畫面顯示「回城」互相印證。）
AS_AUTO_REVIVE = 2100        # 陣亡時自動復活（bool）
# ⚠ 下面兩行現在**沒有人寫**（apply_prefs 只會把 2100 關掉 —— 死亡回程改成
#   自己送「回標記點」封包，見 app/game/revive.py）。留著當逆向文件。
AS_REVIVE_MODE = 2102        # 復活方式（int，值 = 下拉列號）
REVIVE_SOUL, REVIVE_RECALL, REVIVE_SPOT = 0, 1, 2   # 靈魂／回城／原地
# 畫面同步用的勾選框控制項 id（XML 裡的固定值，不是執行期代號）
SUPPLY_SCROLL_CHECK_ID = 10073   # 補給頁「使用標記傳送捲軸回練功點」
REVIVE_CHECK_ID = 14100          # 輔助頁「陣亡時自動復活」

# ★★ 補血／補魔藥水設在「天使輔助精靈」那一頁，用 DATAID 存（也是 robot var）。
#   每一格是一對：`DATAID` 放**型別**、`DATAID + 10` 放**值**。
#     型別 1 = 技能（用技能補，不吃藥水）　型別 2 = 道具（值就是種類 ID）
#   實測：雪狐 HP1=道具 4836、MP1=道具 4837；白狐 HP1=**技能 64**、MP2=道具 4837。
#   （`SKILLITEM_VALUE_DIFF` 這個 Lua 常數就是那個 +10。）
SKILLITEM_VALUE_DIFF = 10
SKILLITEM_TYPE_SKILL = 1
SKILLITEM_TYPE_ITEM = 2
HP_ITEM_SLOTS = (2121, 2122, 2123)   # DATAID_RECOVER_HP1~3_SKILLITEM
MP_ITEM_SLOTS = (2124, 2125)         # DATAID_RECOVER_MP1~2_SKILLITEM

# 精靈的「原練功點」—— 補給完會走回這裡。座標是格子 × 32 的原始值。
AF_ORG_MAP = 1030
AF_ORG_X = 1025
AF_ORG_Y = 1026
TILE = 32

# 「補給那一趟」需要的三項；缺任何一項就等於白開
SUPPLY_NEEDED = (
    (AS_BACK_BROKEN_EQ, "裝備損壞回城"),
    (AS_IS_REPAIR, "修理裝備"),
)


def is_run(scanner) -> bool:
    """精靈現在在跑嗎？（純讀記憶體，成本趨近於零）"""
    raw = scanner._read_bytes(RUN_FLAG, 4)
    if not raw:
        return False
    return struct.unpack("<i", bytes(raw))[0] == RUN_ON


def _wnd(mover, scanner, name: str) -> int:
    """讀某個視窗的**執行期代號**；視窗沒開就是 0。

    ⚠⚠ **這些代號是執行期才配的，不能寫死也不能快取。**
      同一台黑狐先後讀到 `WND_AUTOROBOT` = 1281171713（面板開著）與 0（關著）、
      `WND_AUTOFIGHT` = 1282892244 與 1584955875。我一度把它快取起來，那是錯的。
    """
    v = lua.get_global(mover, scanner, name)
    return int(v) if isinstance(v, (int, float)) else 0


def set_run(mover, scanner, on: bool) -> tuple[bool, object]:
    """開／關天使守護精靈。回傳 (旗標最後是不是想要的值, 說明)。

    ⚠⚠⚠ **不要再去動畫面上那個勾選框**（2026-08-06 使用者實際踩到）。
      以前這裡會叫 `CreateRobotWindow` / `window.setcheck` /
      `OnClickOpenCloseRobot` 讓勾選框跟著變 —— 那些都是 Lua，而從外部呼叫
      Lua 有一個**還沒根治**的破口：我們「讀 top → 寫參數 → 寫 top」的中間，
      遊戲主執行緒自己的 UI Lua 會插隊，堆疊就此錯位。症狀正是使用者回報的
      「用了我們的自動戰鬥之後，去點精靈面板的勾選框，遊戲就崩潰」。
      `CreateRobotWindow` 更毒：開視窗會自己抽訊息 → 重入跳板。
    ★ 現在只寫 `RUN_FLAG`（純記憶體）。**這跟遊戲自己做的一模一樣** ——
      反組譯 `setrobotisrun`（0x5976B8）：
          ecx = [0x9B66AC]
          al  = *(byte*)(ecx + ROBOT_READY_OFF)      ← 沒準備好就什麼都不做
          al ? (on ? 0x54D81E : 0x54D813) : 直接 ret
          0x54D81E: mov [0x9CFBA4], 0     ← RUN_ON
          0x54D813: mov [0x9CFBA4], -1    ← RUN_OFF
      也就是說整支函式的作用就是寫這個旗標，沒有別的副作用。
    ⚠ 那個 guard 我們也照做：它是 0 的時候遊戲自己也不會動旗標，
      我們硬寫只會讓「開了」變成假象。
    ⚠ 代價：**面板上的勾選框可能顯示成舊的**（那是遊戲自己畫的），
      重開面板就會對。這是刻意的取捨 —— 顯示不同步只是看起來怪，
      去戳它卻會讓遊戲崩潰。
    """
    from app.game import locate, quickbar  # 取最新值，見檔頭 ROBOT_READY_OFF 註記

    # ⚠⚠ 寫全域之前先確認這一輪 AOB 真的定位到它。定位失敗時 data 類是**保留
    #   舊值**的，拿舊位址去寫 = 砸新版遊戲裡不相干的記憶體（2026-08-11 用
    #   舊的 login.EULA_OK 寫壞登入畫面 Lua UI，實際踩過）。
    if not locate.located("robot", "RUN_FLAG"):
        return False, "遊戲位址還沒驗證過（改版？）—— 先跑 tools/patch_doctor.py"
    mgr = _u32(scanner, quickbar.MGR_PTR)
    ready = scanner._read_bytes(mgr + ROBOT_READY_OFF, 1) if mgr else None
    if not ready or not ready[0]:
        return False, "精靈子系統還沒準備好（遊戲自己這時也不會動旗標）"
    try:
        scanner.write_value(RUN_FLAG, VALUE_TYPES["int32"],
                            RUN_ON if on else RUN_OFF)
    except Exception as exc:                               # noqa: BLE001
        return False, f"寫不進旗標（{exc}）"
    good = is_run(scanner) == bool(on)
    return good, ("" if good else "旗標沒變成想要的值")


def get_bool(mover, scanner, var_id: int) -> tuple[bool, object]:
    """讀精靈的一個布林設定。

    ★ **先直接讀記憶體**（微秒、純讀取）；讀不到才退回 Lua。
      Lua 那條要三趟跳板往返、在畫面執行緒上等，而且會動遊戲的 Lua 堆疊。
    """
    got = _read_var(scanner, var_id, VAR_T_BOOL)
    if got is not None:
        return True, got
    return lua.call(mover, scanner, "game.getrobotvar_bool", var_id)


def _write_var(scanner, data_id: int, want_type: int, value: int) -> bool:
    """★★★ 直接把設定寫進記憶體（**完全不碰 Lua**）。

    版面跟 `_read_var` 同一份（值記錄 +8 型別、+0xC 值），所以寫之前
    一定會先確認「這一筆存在而且型別對」—— 型別不符就不寫，回 False
    讓呼叫端退回 Lua。

    ⚠⚠ **為什麼要有這支**：以前改設定走 `game.setrobotvar_*`（Lua）。
      從外部呼叫 Lua 有一個**還沒根治**的破口 —— 我們「讀 top → 寫參數 →
      寫 top」的中間，遊戲主執行緒自己的 UI Lua 會插隊，堆疊就此錯位；
      症狀是**過一陣子之後**去點精靈面板上的勾選框，遊戲當場崩潰
      （使用者實際遇到）。純記憶體寫入沒有這個問題。
      （見 [[game-crash-root-causes]] 的「還沒修」那一節。）
    ⚠ 畫面上的勾選框不會跟著動（那是遊戲自己畫的）——**我們也不再去動它**：
      要改畫面就得呼叫 Lua 的 window.setcheck，那正是會出事的那條路。
      面板重開一次就會顯示正確的值。
    """
    rec = _find_var(scanner, data_id)
    if rec is None or rec is _MISSING:
        return False
    raw = scanner._read_bytes(rec + _V_TYPE, 4)
    if not raw or raw[0] != want_type:
        return False                       # 型別不對就別亂寫
    # ⚠⚠ **寬度要跟遊戲一模一樣**（反組譯 setrobotvar_bool → 0x54EEA4）：
    #   bool 是 `mov byte [rec+0xC], bl` —— **只有 1 個 byte**。
    #   寫成 4 bytes 會一併蓋掉 +0xD~+0xF，那不是我們的地盤。
    #   int 的 getter 是 `mov eax,[rec+0xC]`，才是 4 bytes。
    vt = VALUE_TYPES["int8" if want_type == VAR_T_BOOL else "int32"]
    try:
        scanner.write_value(rec + _V_VALUE, vt,
                            (1 if value else 0) if want_type == VAR_T_BOOL
                            else int(value))
    except Exception:                                      # noqa: BLE001
        return False
    return _read_var(scanner, data_id, want_type) == (
        bool(value) if want_type == VAR_T_BOOL else int(value))


def set_bool(mover, scanner, var_id: int, value: bool) -> tuple[bool, object]:
    """改精靈的一個布林設定。**先用純記憶體寫**，寫不了才退回 Lua。

    ⚠ 平常不要用 —— 那是使用者在遊戲裡設好的東西，我們不該偷改。
      留著是為了「缺哪一項就補哪一項」這種明確的情境。
    """
    if _write_var(scanner, var_id, VAR_T_BOOL, 1 if value else 0):
        return True, bool(value)
    return lua.call(mover, scanner, "game.setrobotvar_bool", var_id,
                    bool(value))


def set_int(mover, scanner, var_id: int, value: int) -> tuple[bool, object]:
    """改精靈的一個整數設定。注意事項同 `set_bool`：平常不要用。"""
    if _write_var(scanner, var_id, VAR_T_INT, int(value)):
        return True, int(value)
    return lua.call(mover, scanner, "game.setrobotvar_int", var_id, int(value))


def _read_list(scanner, rec: int) -> list[int] | None:
    """讀一個**清單型**記錄的全部元素。型別／筆數對不上回 None。"""
    head = scanner._read_bytes(rec, _L_ELEMS)
    if not head or head[_V_TYPE] != VAR_T_INTLIST:
        return None
    n = struct.unpack_from("<i", bytes(head), _L_COUNT)[0]
    if not 0 <= n <= _L_CAP:
        return None
    if n == 0:
        return []
    raw = scanner._read_bytes(rec + _L_ELEMS, n * 4)
    return list(struct.unpack(f"<{n}i", bytes(raw))) if raw else None


def _list_append(scanner, rec: int, count: int, value: int) -> bool:
    """照遊戲 append 的步驟（0x54DF4C）：**先寫元素、再把筆數 +1** ——
    這個順序讓遊戲那邊不管什麼時候來讀都讀不到半套。

    ⚠ 寫筆數之前再驗一次它還是我們讀到的值：使用者剛好在補給頁按
      「加入」的話筆數會變，硬寫會把他剛加的那筆蓋掉 —— 那就整個放棄，
      下一次 apply 再來。
    """
    if not 0 <= count < _L_CAP:
        return False
    try:
        scanner.write_value(rec + _L_ELEMS + count * 4,
                            VALUE_TYPES["int32"], int(value))
        raw = scanner._read_bytes(rec + _L_COUNT, 4)
        if not raw or struct.unpack("<i", bytes(raw))[0] != count:
            return False
        scanner.write_value(rec + _L_COUNT, VALUE_TYPES["int32"], count + 1)
    except Exception:                                      # noqa: BLE001
        return False
    return True


def _mark_lists_dirty(scanner) -> None:
    """清單編輯完把管理員單例 +0x4C 的髒 byte 設 1。

    遊戲自己的 robotvar_add/set/remove_intlist 收尾都會做這一步
    （0x54EDEF），跟著做遊戲才會把「設定變了」當一回事。
    ⚠ 反組譯裡就是 `mov byte [mgr+0x4C], al` —— 只能寫 1 個 byte。
    """
    mgr = _u32(scanner, VAR_MGR_PTR)
    if mgr and 0x10000 < mgr < 0x7FFF0000:
        try:
            scanner.write_value(mgr + _MGR_DIRTY_OFF, VALUE_TYPES["int8"], 1)
        except Exception:                                  # noqa: BLE001
            pass


def ensure_buy_item(mover, scanner, item_id: int, keep: int) -> str | None:
    """購買清單裡**保證有** `item_id` 而且數量至少 `keep`。

    回傳做了什麼（給人看）；本來就設好、或任何一步對不上而放棄 → None。

    ★ **只加不動別人**（使用者要求）：他原本設的每一列都不碰；
      這一項他已經設得比 `keep` 高也不改小 —— 那是他的設定。
    ★ 純記憶體優先。任何一步對不上（樹讀不到、型別不對、兩張表筆數
      不同步、筆數剛好被改）就整個放棄 —— 寧可這一輪沒設到，
      也不要把使用者的清單弄壞；反正每次開始掛機都會再試。
    """
    rec_i = _find_var(scanner, AS_TOBUY_ID)
    rec_n = _find_var(scanner, AS_TOBUY_NUM)
    if rec_i is _MISSING and rec_n is _MISSING:
        # 全新角色、清單根本還沒建 —— 建記錄要 malloc＋插紅黑樹，
        # 那不是我們能從外面做的：退回 Lua 讓遊戲自己建
        # （game.robotvar_add_intlist 就是補給頁「加入」按的那支）。
        ok1, _ = lua.call(mover, scanner, "game.robotvar_add_intlist",
                          AS_TOBUY_ID, int(item_id))
        ok2, _ = lua.call(mover, scanner, "game.robotvar_add_intlist",
                          AS_TOBUY_NUM, int(keep))
        if ok1 and ok2:
            return f"購買清單加了{itemname.label(item_id)}×{keep}"
        return None
    if not isinstance(rec_i, int) or not isinstance(rec_n, int):
        return None                        # 樹讀不到／只缺一張（不同步）
    ids = _read_list(scanner, rec_i)
    nums = _read_list(scanner, rec_n)
    if ids is None or nums is None:
        return None

    if item_id in ids:
        i = ids.index(item_id)
        if i >= len(nums):
            return None                    # 兩張表不同步，不敢動
        if nums[i] >= keep:
            return None                    # 已經設好（或設更多），完全不碰
        try:
            scanner.write_value(rec_n + _L_ELEMS + i * 4,
                                VALUE_TYPES["int32"], int(keep))
        except Exception:                                  # noqa: BLE001
            return None
        _mark_lists_dirty(scanner)
        return f"購買清單的{itemname.label(item_id)}補到 {keep} 個"

    if len(ids) != len(nums):
        return None                        # 平行陣列不同步，append 會錯位
    if not _list_append(scanner, rec_i, len(ids), item_id):
        return None
    if not _list_append(scanner, rec_n, len(nums), keep):
        # ID 進了、數量沒進 → 兩張表會差一格錯位。把 ID 的筆數退回去
        # （元素留在界外，遊戲讀不到，無害）。
        try:
            scanner.write_value(rec_i + _L_COUNT,
                                VALUE_TYPES["int32"], len(ids))
        except Exception:                                  # noqa: BLE001
            pass
        return None
    _mark_lists_dirty(scanner)
    return f"購買清單加了{itemname.label(item_id)}×{keep}"


# ⛔ `_sync_check()`（用 Lua 的 window.setcheck 讓畫面勾選框跟著變）**已移除**。
#   它是「使用了我們的自動戰鬥之後，去點精靈面板的勾選框，遊戲就崩潰」的
#   來源：從外部呼叫 Lua 會跟遊戲主執行緒自己的 UI Lua 搶堆疊（那個破口
#   還沒根治，見 [[game-crash-root-causes]] 的「還沒修」）。
#   取捨：畫面顯示可能跟實際設定不同步（重開面板就會對），但不會把遊戲弄崩。

def apply_prefs(mover, scanner, *, main_switch: bool = False,
                jump_back: bool = False,
                revive_mark: bool = False,
                supply: bool = False) -> list[str]:
    """照掛機分頁的勾選把精靈調成該有的樣子。回傳實際動了哪些（給人看）。

    ★ 只「往要的方向推」：每一項都先純記憶體讀，已經是對的就完全不碰 Lua。
      使用者取消勾選我們也**不會**把遊戲裡的設定改回去 —— 那是他的設定。

    · main_switch    開始自動戰鬥 → 主開關「開啟天使守護精靈」一律開起來
    · jump_back      勾了「用天使趴趴GO回地圖」→ 關補給頁「使用標記傳送捲軸
                     回練功點」：回地圖已經由趴趴GO負責，精靈再燒一張捲軸
                     只是白花道具
    · revive_mark    勾了「死亡自己回練功區」→ 輔助頁「陣亡時自動復活」
                     **關掉**：死亡回程改成我們自己送「回標記點」封包
                     （app/game/revive.py），精靈搶先把人復活回城反而壞事。
                     ⚠ 只關不開 —— 以前這裡是「開＋復活方式選回城」，
                     使用者 2026-08-07 要求反過來。
    · supply         勾了**任何一個**回程補給觸發（壞裝／HP水／MP水）→
                     把補給那一趟「進了城要做的事」推到位（使用者要求）：
                     開「裝備損壞回城」「修理裝備」「購買物品保持身上數量」，
                     再把天使之翼補進購買清單保持 `BUY_KEEP_WINGS` 個 ——
                     免得補給跑了裝沒修、翼用完了下一趟連回程都回不去。
                     ★ 購買清單**只加翼這一項**，他原本設的都不動；
                       翼已經設更多也不改小（見 `ensure_buy_item`）。
    """
    notes: list[str] = []

    if main_switch and not is_run(scanner):
        ok, why = set_run(mover, scanner, True)
        notes.append("開了天使守護精靈"
                     if ok else f"⚠ 天使守護精靈開不起來（{why}）")

    if jump_back:
        ok, cur = get_bool(mover, scanner, AS_USE_RETURN_SCROLL)
        if ok and cur is not False:
            set_bool(mover, scanner, AS_USE_RETURN_SCROLL, False)
            notes.append("關了「標記傳送捲軸回練功點」")

    if revive_mark:
        ok, cur = get_bool(mover, scanner, AS_AUTO_REVIVE)
        if ok and cur is not False:
            set_bool(mover, scanner, AS_AUTO_REVIVE, False)
            # ⚠ 精靈面板開著的話勾選框的**顯示**不會跟著變（那是遊戲自己
            #   畫的，我們不去戳它 —— 見 set_run 檔頭）。重開面板就會對。
            notes.append("關了「陣亡時自動復活」")

    if supply:
        from app.game import recall                   # 避免循環相依
        for var_id, label in ((AS_BACK_BROKEN_EQ, "裝備損壞回城"),
                              (AS_IS_REPAIR, "修理裝備"),
                              (AS_IS_BUY_ITEM, "購買物品保持身上數量")):
            ok, cur = get_bool(mover, scanner, var_id)
            if ok and cur is not True:
                set_bool(mover, scanner, var_id, True)
                notes.append(f"開了「{label}」")
        note = ensure_buy_item(mover, scanner, recall.RECALL_ITEM,
                               BUY_KEEP_WINGS)
        if note:
            notes.append(note)

    return notes


def begin_supply(mover, scanner) -> list[str]:
    """把精靈調成「會跑補給」的狀態。回傳「實際做了哪些事」給人看。

    ## 使用者實測出來的條件（缺一就不會觸發）

        ① 天使守護精靈主開關要開
        ② **「攻擊指定敵人」要關**  ← 開著就不會觸發
        ③ 按「設定」把搜尋中心點設在現在的位置
        ④ 最後才開「自動攻擊」

    做完等 `SETUP_SETTLE` 秒再回程（見 `do_recall`）。

    ★ **每一項都先查再決定動不動**：已經是對的就跳過（使用者要求）。
    """
    notes: list[str] = []

    if not is_run(scanner):                                          # ①
        ok, why = set_run(mover, scanner, True)
        notes.append("開了主開關" if ok else f"⚠ 主開關開不起來（{why}）")

    ok, cur = get_bool(mover, scanner, AF_ATTACK_TARGET_ONLY)        # ②
    if ok and cur is not False:
        set_bool(mover, scanner, AF_ATTACK_TARGET_ONLY, False)
        notes.append("關了攻擊指定敵人")

    set_org_spot(mover, scanner)                                     # ③
    notes.append("設了中心點")

    ok, cur = get_bool(mover, scanner, AF_IS_AUTO_FIGHT)             # ④
    if cur is not True:
        set_autofight(mover, scanner, True)
        notes.append("開了自動攻擊")
    return notes


def end_supply(mover, scanner) -> None:
    """收尾。**只做一件事：把「自動攻擊」關掉。**

    ⚠ 其餘狀態（主開關、攻擊指定敵人、中心點）**刻意不還原** ——
      使用者明確要求「結束不用幫忙回復原狀，但自動攻擊關閉是肯定的」。
      自動攻擊是唯一非關不可的，因為它一開著精靈就會跟我們的掛機搶怪。
    """
    set_autofight(mover, scanner, False)


def get_int(mover, scanner, var_id: int) -> int | None:
    """讀精靈的一個整數設定。

    ★ **先直接讀記憶體**（微秒、純讀取）；讀不到才退回 Lua。
      藥水設定一次要讀 10 個，走 Lua 就是 30 趟跳板往返、零點幾秒的畫面卡頓。
    """
    got = _read_var(scanner, var_id, VAR_T_INT)
    if got is not None:
        return got
    ok, val = lua.call(mover, scanner, "game.getrobotvar_int", var_id)
    return int(val) if ok and isinstance(val, (int, float)) else None


# 藥水設定多久重讀一次（見 potion_slots）。
# ⚠⚠ 這個值直接決定「純打怪時我們戳遊戲 Lua 的頻率」——
#   `_check_dry()` 每 3 秒跑一次而且**跟有沒有勾補給無關**，所以掛機全程都在
#   走這條。60 秒時是每分鐘 10 次 Lua 呼叫，五台就是每分鐘 50 次。
#   每一次 Lua 呼叫都要動遊戲的 Lua 堆疊，是目前風險最高的動作
#   （見 lua.py `_restore_top` 與 `_sync` 的說明；使用者實測過呼叫太密會把
#   客戶端的訊息迴圈弄卡死）。
#   ★ 這是**設定值**，使用者偶爾才改一次 —— 拉長到 5 分鐘等於風險降到 1/5，
#     而且真的要「宣告藥水見底」之前會強制重讀確認（見 potions_out），
#     所以判斷結果跟以前一模一樣，不會因為快取變舊而誤報。
SLOT_CACHE_SECS = 300.0
_slot_cache: dict[int, tuple[float, dict]] = {}


def potion_slots(mover, scanner, pid: int, force: bool = False) -> dict:
    """讀「輔助精靈」那幾格藥水設成什麼，**結果快取 `SLOT_CACHE_SECS` 秒**。

    ⚠⚠ **一定要快取**：每次判斷要讀 10 個設定，每個都是一次 Lua 呼叫。
      掛機每 3 秒判斷一次就是每分鐘 200 次 —— 實測連打十幾輪就會把客戶端的
      訊息迴圈弄卡死（白狐）。設定是使用者偶爾才改的東西。
    ★ 背包數量**不快取** —— 那是純記憶體讀取，不經過 Lua，成本趨近於零。

    force=True：忽略快取重讀。**只有在要真的下判斷之前才用**
    （見 `potions_out`），平常呼叫一律走快取。

    回傳 {DATAID: (型別, 值)}；任何一個讀失敗就整份不快取（下次再試）。
    """
    # ★★ 先試純記憶體：全部讀到就直接用 —— **連快取都不需要**，
    #   因為那是 0.3ms 的純讀取。這樣設定改了我們下一拍就知道，
    #   比以前的「快取 60 秒」還準，而且完全不碰遊戲的 Lua。
    fast = {}
    for base in HP_ITEM_SLOTS + MP_ITEM_SLOTS:
        kind = _read_var(scanner, base, VAR_T_INT)
        val = _read_var(scanner, base + SKILLITEM_VALUE_DIFF, VAR_T_INT)
        if kind is None or val is None:
            fast = None
            break                        # 樹讀不到 → 退回底下的 Lua 路徑
        fast[base] = (kind, val)
    if fast:
        return fast

    now = time.time()
    hit = _slot_cache.get(pid)
    if hit and not force and now - hit[0] < SLOT_CACHE_SECS:
        return hit[1]
    out, ok = {}, True
    for base in HP_ITEM_SLOTS + MP_ITEM_SLOTS:
        kind = get_int(mover, scanner, base)
        val = get_int(mover, scanner, base + SKILLITEM_VALUE_DIFF)
        if kind is None or val is None:
            ok = False
            break
        out[base] = (kind, val)
    if not ok:
        return hit[1] if hit else {}       # 讀失敗就沿用舊的，別當成「沒設」
    _slot_cache[pid] = (now, out)
    return out


def _potion_out(slots_info: dict, scanner, inv_head: int,
                slots) -> list[int] | None:
    """這一組（紅水或藍水）的藥水**全部歸零**了嗎？是的話回傳那些種類 ID。

    ⚠⚠ **要「全部」歸零才算，不是「任何一格」**：有人紅水配兩格，第一格
      用完但第二格還有，角色明明還補得到血 —— 那時把人拉回城是錯的。
    ★ 設成「技能」的格子代表**不靠藥水補**（白狐紅水1 就是技能 64），
      這一組只要有技能格就直接當「不缺」，不觸發。
    ★ 一格都沒設、或設定讀不到，就無從判斷，也不觸發。
    """
    from app.game import bag, inventory               # 避免循環相依

    items = []
    for base in slots:
        kind, tid = slots_info.get(base, (None, None))
        if kind == SKILLITEM_TYPE_SKILL:
            return None                    # 有技能可補，不算缺藥水
        if kind != SKILLITEM_TYPE_ITEM or not tid:
            continue
        items.append(tid)
    if not items:
        return None                        # 一格都沒設
    # ⚠ 判定跟 count_by_type 一樣是「同種類全部加總」：藥水會散成好幾疊
    #   （黑狐的 4837 分在 8 格）。差別是**一趟走完**同時累加所有種類 ——
    #   以前每種各走一整條陣列（各 300~400 次系統呼叫），而這裡在 GUI 執行緒。
    total, complete = inventory.count_by_types(scanner, inv_head, items)
    if any(v > 0 for v in total.values()):
        return None                        # 還有一格有貨就不算用完
    # ★★ 數到「全部歸零」才需要嚴格把關 —— 這個結論會觸發通知／回程補給：
    #   ① 沒走完整條（截斷）→「沒看到」不是「用完了」；
    #   ② 表頭重驗不過 → 陣列剛搬家（換地圖、物品增減都可能重配置），
    #     剛剛數的是死掉的舊副本（實測舊副本還殘留幾個像物品的指標）。
    #   掃描執行緒雖然每輪都 is_valid，但那跟這裡隔了最多零點幾秒 ——
    #   使用者實際遇過「藥水明明很多卻突然報用完」。
    #   ③ 容器**還沒同步**（`bag.synced`）→ 換頻道／傳送後重連的那段空窗裡，
    #     格數配好了但物品還沒推過來，每一格都是空的。①②都會通過
    #     （讀取成功、表頭有效），只有第 0 格的金幣物件在不在看得出來。
    #     使用者回報「換頻道有時會跳沒有藥水」就是撞上這個。
    #   跟 potions_out「宣告前強制重讀設定」同一個精神：要做決定才多花檢查。
    if not complete or not inventory.is_valid(scanner, inv_head):
        return None
    if not bag.synced(scanner):
        return None
    return items


def potions_out(mover, scanner, inv_head: int, pid: int = 0,
                hp: bool = True, mp: bool = True) -> list[tuple[str, str]]:
    """哪幾組藥水見底了 → `[('HP', 'HP藥水（高效紅藥水(活動)）'), …]`。

    ★ **HP 和 MP 完全分開**（使用者要求）：勾 HP 就只看 HP 那一組全歸零，
      MP 那組有沒有水完全不影響，反之亦然。
    ★ 拆出來是因為**通知和觸發是兩回事**：使用者要求「水沒了也要通知」，
      所以通知會兩組都看，觸發只看勾起來的那組。
    """
    if not inv_head:
        return []
    want = ([(HP_ITEM_SLOTS, "HP")] if hp else []) + \
           ([(MP_ITEM_SLOTS, "MP")] if mp else [])
    if not want:
        return []

    def scan(info) -> list[tuple[str, str]]:
        out = []
        for slots, what in want:
            gone = _potion_out(info, scanner, inv_head, slots)
            if gone:
                names = "、".join(itemname.label(t) for t in gone)
                out.append((what, f"{what}藥水（{names}）"))
        return out

    info = potion_slots(mover, scanner, pid)
    out = scan(info)
    # ★★ 平常一律吃快取（純記憶體數數，不碰 Lua）；**只有在要宣告「見底」
    #   之前才強制重讀設定確認一次**。
    #   為什麼要確認：使用者如果中途把藥水換成別種，快取裡還是舊的種類 ID，
    #   數舊的當然數到 0 —— 那會變成誤報，還可能誤觸發回程補給。
    #   為什麼這樣就夠：有水的時候（99.9% 的時間）完全不需要碰 Lua；
    #   真的數到 0 才多花一次確認，而那本來就是要做決定的時刻。
    #   設定沒變就直接用原本的結果，連重算都省。
    if out:
        fresh = potion_slots(mover, scanner, pid, force=True)
        if fresh != info:
            out = scan(fresh)
    return out


_UNSET = object()      # 「這個參數沒給」——用來跟「給了但值是 None」區分


def supply_needed(mover, scanner, inv_head: int, on_broken: bool,
                  on_hp: bool, on_mp: bool, pid: int = 0,
                  broken=_UNSET, dry=None) -> str | None:
    """**該回去補給了嗎？** 是的話回傳原因（可直接顯示），否則 None。

    broken / dry: 呼叫端**同一拍剛算過**的壞裝清單與見底清單，給了就不重算。
        ⚠ 兩者都要是「這一拍」的值，不能是上一輪留下來的。
        掛機的裝備檢查本來就會先算壞裝、再叫 `potions_out()` 通知，
        接著這裡又各算一次 —— 各要走一趟容器，上百次記憶體讀取，
        而且是在 GUI 執行緒上，五台同時做就是看得見的頓一下。
        `dry` 是**兩組都算過**的完整清單，這裡只挑有勾的那幾組
        （`_potion_out` 各組獨立計算，挑出來跟重算完全一樣）。

    ⚠⚠ **要不要判斷由呼叫端的三個開關決定，不看遊戲裡精靈的回城勾選**
      （使用者要求：「不要管他遊戲裡面有沒有設定」）。這樣我們的觸發跟
      官方的觸發互相獨立，不會因為他在遊戲裡沒勾就變成不動。

    · `on_broken` → **身上穿的裝備**任何一件耐久 0（`bag.worn_broken()`；
      使用者 2026-08-07 要求：以前只看武器，改成全身、不含背包）
    · `on_hp` → **HP 藥水那一組**全部歸零（見 `_potion_out`）
    · `on_mp` → **MP 藥水那一組**全部歸零
      ★ 兩組各判各的：勾 HP 就只看 HP 那三格，MP 有沒有水完全不影響。

    藥水是哪一種還是讀「天使輔助精靈」那頁設的（見 `HP_ITEM_SLOTS`），
    所以換藥水會自動跟著換，不必寫死。

    ⛔ 採買清單**不是**觸發條件：那是「進城順便補到幾個」，不是「該回城了」。
    ⛔ 還沒支援背包滿 —— `game.getbagsize()` 回 30，但物品陣列裡有 60 幾件，
      對不起來，還沒確定背包格的範圍。
    """
    from app.game import bag                          # 避免循環相依

    if on_broken:
        # ★ 壞裝走 bag.py（遊戲自己的容器路徑），**不吃 inv_head** ——
        #   物品陣列表頭還沒定位到也照樣判得出來。None = 讀不到 = 不觸發。
        bad = (bag.worn_broken(scanner) if broken is _UNSET else broken)
        if bad:
            return "裝備損壞（" + "、".join(it.name for it in bad) + "）"

    if not inv_head:
        return None

    if dry is None:
        dry = potions_out(mover, scanner, inv_head, pid, on_hp, on_mp)
    else:
        want = ({"HP"} if on_hp else set()) | ({"MP"} if on_mp else set())
        dry = [(w, text) for w, text in dry if w in want]
    if dry:
        return "、".join(d for _, d in dry) + "用完了"
    return None


def has_recall_item(scanner, inv_head: int) -> tuple | None:
    """背包裡的回程道具 (格號, 剩幾個)；**確定沒有**回 ()；讀不到回 None。

    ⚠ None 跟 () 是兩回事（同 bag.worn_broken 的 None／[] 慣例）：
      None = 陣列走不完（截斷）／表頭剛死 → 無法判斷，呼叫端**先不要動作**；
      ()   = 整條走完了、真的一個都沒有 → 才輪到「沒有回程道具」的處置。
      以前兩種都回 None，表頭剛搬家的那一拍會被判成「沒有」→ 誤停機
      （跟藥水誤報歸零同一類，見 _potion_out 的把關）。
    ⚠ 換頻道／傳送後重連的空窗（容器配好了、物品還沒推過來）也算「讀不到」
      —— 那時整條陣列每一格都是空的，看起來就是「一個都沒有」。
      判準見 `bag.synced()`。
    """
    from app.game import bag, inventory, recall       # 避免循環相依

    if not inv_head:
        return None
    total, complete = inventory.count_by_types(scanner, inv_head,
                                               [recall.RECALL_ITEM])
    n = total.get(recall.RECALL_ITEM, 0)
    if n > 0:
        # ⚠ 格號要用物品自己記的（find_by_type），數量要用總數（可疊物品
        #   會散成好幾疊）。剛剛數得到、轉頭卻找不到格號 = 陣列正在動，
        #   當「讀不到」處理。
        got = inventory.find_by_type(scanner, inv_head, recall.RECALL_ITEM)
        return (got[0], n) if got else None
    if not complete or not inventory.is_valid(scanner, inv_head):
        return None                        # 沒看完整條 →「沒數到」≠「沒有」
    if not bag.synced(scanner):
        return None                        # 重連空窗：東西還沒推過來
    return ()


def do_recall(mover, scanner, inv_head: int) -> tuple[bool | None, str]:
    """觸發的**第二段**：用掉回程道具。第一段是 `begin_supply()`。
    回傳 (成功嗎, 說明)；第一個值 **None = 暫時性失敗，過幾秒重試就好**。

    ## 為什麼要分兩段

    使用者實測：開關調好之後**要隔 2~4 秒再回程**才穩，太快送回程精靈那邊
    還沒進入狀態，整趟就不會啟動。而那幾秒**不能用 sleep 卡住畫面**，
    所以拆成兩段、由呼叫端用計時器接（`SETUP_SETTLE`）。

    ## 整套觸發條件（使用者實測的規律，缺一不可）

        ① 主開關開　② **「攻擊指定敵人」關**　③ 按「設定」記下中心點
        ④ 自動攻擊開　→ 等 `SETUP_SETTLE` 秒 → ⑤ 人在**非主城**時「回程」
        → 再等 `AF_HOLD_SECS` 秒才把自動攻擊關掉

    ⚠⚠ **順序不能反**：先回程後調開關完全沒反應（我一開始就是這樣寫）。
    ⚠ 遊戲**沒有**「立刻跑一趟補給」的指令（補給頁每個控制項都查過了，
      全是設定用的勾選框）。所以只能靠這個組合去湊條件。
    ⚠ **耐久是滿的就沒有補給條件**，精靈到城裡會沒事可做（黑狐 70/70
      實測兩次都停在城裡）。真正的觸發是裝備真的壞了。

    ## 實測

        雪狐   靜謐海域 耐久 31 → 和風林 → +1.1 分修好 → +1.5 分走回戰場
        北極狐 靜謐海域 耐久  9 → 聖光城 → +0.7 分修好 → +0.9 分走回戰場

    ⚠ 會用掉一個回程道具（種類 RECALL_ITEM）。
    """
    from app.game import recall                       # 避免循環相依

    got = has_recall_item(scanner, inv_head)
    if got is None:
        # ★ 暫時性失敗（陣列剛搬家／被截斷）—— 回 None 讓呼叫端重試，
        #   別跟「確定沒有」混在一起停機（見 has_recall_item 的三態）。
        return None, "背包暫時讀不到（物品陣列剛搬家？）"
    if not got:
        return False, f"背包裡沒有{itemname.label(recall.RECALL_ITEM)}"
    slot, count = got
    if not is_run(scanner):
        return False, "天使精靈不在執行中（旗標沒變）"
    if not recall.use_item(mover, slot):
        return False, "回程道具送不出去"
    return True, (f"用了第 {slot} 格的{itemname.label(recall.RECALL_ITEM)}"
                  f"（剩 {max(count - 1, 0)} 個）")


def set_org_spot(mover, scanner) -> tuple[bool, object]:
    """按下面板上的「設定」鈕 —— 把**現在站的地方**記成搜尋中心／原練功點。

    就是 `SETTING/BASE/WND01.XML` 控制項 10047 綁的那支 Lua 全域，0 個參數。

    ⚠⚠ **不設會半路停住**：黑狐記的原地圖是 125、人卻在 122，回城之後精靈
      不知道要回哪，就杵在城裡不動。
    ⚠ **它會順手把大地圖叫出來**（使用者回報，實測確認 `WND_STAGEMAP`
      從無變成有）。這裡用 `OnCloseStageMapWnd()` 關回去，不必去送 Esc／Alt+M。
      ★ 使用者原本就開著地圖的話**不動它** —— 只收拾自己弄出來的東西。
    ⛔ 不要改用 `game.autofightsetorgpos()`：實測它更新座標但把地圖 ID
      寫成 0（北極狐 95 → 0），比不設還糟。
    """
    was_open = _wnd(mover, scanner, "WND_STAGEMAP")
    got = lua.call(mover, scanner, "OnPressSetSearchPoint")
    if not was_open and _wnd(mover, scanner, "WND_STAGEMAP"):
        lua.call(mover, scanner, "OnCloseStageMapWnd")
    return got


AUTOFIGHT_CHECK_ID = 960          # 面板上「自動攻擊」勾選框（XML 裡的固定 id）


def set_autofight(mover, scanner, on: bool) -> None:
    """開／關「自動攻擊」。

    ⚠ 這裡**不叫** `OnCheckAutoFight` —— 那是舊版路徑（會去註冊一個計時器），
      而且被 `[0x8909BA]` 擋著。真正決定行為的是變數 `AF_BOL_ISAUTOFIGHT`。
    ⚠⚠ 也**不再同步畫面上的勾選框**：那要走 Lua 的 window.setcheck，而從
      外部呼叫 Lua 會跟遊戲自己的 UI Lua 搶堆疊 —— 使用者實際踩到的症狀是
      「之後去點精靈面板的勾選框，遊戲當場崩潰」。顯示不同步只是看起來怪，
      重開面板就會對；把遊戲弄崩才是大事。見 set_run 的說明。
    """
    set_bool(mover, scanner, AF_IS_AUTO_FIGHT, bool(on))


def autofight_off(mover, scanner) -> None:
    """把「自動攻擊」關掉，但**不動主開關** —— 補給流程會繼續跑完。

    觸發後隔 `AF_HOLD_SECS` 秒呼叫這個，就等於「全程自動攻擊是關的」，
    精靈不會跟我們的掛機搶怪。（`end_supply()` 做的也是同一件事。）
    """
    set_autofight(mover, scanner, False)


def autofight_on(scanner) -> bool | None:
    """精靈的「自動攻擊」現在是開的嗎？**純記憶體讀，不碰 Lua、不用跳板**。

    讀不到（樹還沒載好／位址失效）回 None —— 呼叫端不准當成「關著」。
    """
    return _read_var(scanner, AF_IS_AUTO_FIGHT, VAR_T_BOOL)


def force_autofight_off(scanner) -> bool:
    """把「自動攻擊」關掉，**純記憶體、不需要跳板**；關成了才回 True。

    ⚠⚠ 給看門狗用（`farm_tab._af_tick`）。跟 `autofight_off()` 的差別是
      **不會因為跳板剛好不在就整個做不成**：關掉自動攻擊是一個要一直維持的
      狀態，而補給送出後那幾秒正好是換地圖／重連中，跳板最容易掛不上 ——
      舊寫法在那一拍失敗就永遠不補，精靈於是一路幫我們打怪，
      而且**牠完全不看我們的「選中怪物」名單**（使用者回報「會打我沒選的怪」）。
    ★ `_write_var` 本來就寫完重讀確認，所以 False ＝這一拍真的沒關成；
      呼叫端下一輪再關就好，不設上限（見 [[transient-failure-auto-retry]]）。
    """
    return _write_var(scanner, AF_IS_AUTO_FIGHT, VAR_T_BOOL, 0)


def missing_supply_settings(mover, scanner) -> list[str]:
    """補給那一趟缺了哪些必要設定（回傳中文項目名；全都設好就是空清單）。

    讀不到就當作沒問題 —— 寧可讓它跑，也不要因為讀失敗擋住功能。
    """
    out = []
    for var_id, label in SUPPLY_NEEDED:
        ok, val = get_bool(mover, scanner, var_id)
        if ok and val is False:
            out.append(label)
    return out
