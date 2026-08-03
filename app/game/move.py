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
import random
import struct
import threading
import time

from app.game import entity

MOVE_FN = 0x0055A046        # 移動封包建構＋送出函式（指令表 0x201）
WAYPOINTS = 0x009B6684      # 全域路徑點陣列
# ★ 遊戲自己的「走到指定座標」完整常式（反組譯確認，見 [[command-table]]）：
#       f(this, 起點X, 起點Y, 終點X, 終點Y, flag)   全部是世界座標，cdecl
#   內容：起終點同格就直接返回 → 用 this 尋路到終點（寫進 WAYPOINTS）
#         → 有路就送移動封包 → 收尾（0x549B59 / 0x5B5C60）
#   比我們原本「自己呼叫尋路再自己送 MOVE_FN」多做了兩個收尾步驟，
#   那正是遊戲自己走路時會做的，所以改用它。
WALK_FN = 0x005D7D96
# ★ WALK_FN 結尾的「移動結束」收尾，可以單獨呼叫（見 Mover.end_move）：
#       ecx = *0x9B66AC ；0x5B5C60(1, 終點X, 終點Y)
END_MOVE_FN = 0x005B5C60
END_MOVE_THIS = 0x009B66AC
# ★ 遊戲自己的尋路。反組譯 0x556982~0x556990（滑鼠點地板走路那條路）：
#       push 0x9B6684        ← 輸出：路徑點陣列
#       push edi             ← 目標 Y（世界座標）
#       push esi             ← 目標 X
#       mov  ecx, ebx        ← this（**不是**我們掃到的玩家物件，見下）
#       call 0x549B81        ← eax = 算出來的路徑點數量，0 = 算不出來
PATHFIND_FN = 0x00549B81
PATH_MAX_TILES = 28.0       # 尋路的有效範圍實測約 30~40 格，超過就回 0
# ★★ **走路終點離目標的下限**，比這個近就不再往前。
#   1.4 = √2，剛好一個斜格 —— 這是遊戲自己的數字：唯讀觀察遊戲內建的
#   自動打怪 45 秒，離怪的距離**最近就是 1.4、最遠 1.8**，一次都沒有更近。
#   走得比這個近，遊戲會判定角色卡在怪的身體裡（使用者的觀察），
#   而且踩到過更嚴重的：走到怪自己的格子上時伺服器不給站，
#   整段移動被退回，客戶端停在「移動中」狀態，攻擊全部被忽略。
#   ⚠ 這是**下限**，不是設定值 —— 使用者把接戰距離調到 1.0，實際還是 1.4。
MIN_GAP = 1.4
# 這段算不出來就縮短再試（由遠到近）
PATH_TRY = (28.0, 18.0, 10.0, 5.0)
# ★ 直線方向被牆擋住時，換角度找繞路。
# 為什麼需要：中繼點全部取在「往目標的直線」上，那個方向有牆的話會全部算不出來，
# 角色就停在牆邊不動 —— 使用者回報「沒怪物回原點怎麼沒有計算路徑」。
# 實測（黑狐，原點在 195 格外）：直線 28/18 格算不出，但 −40°/−60° 的 20 格
# 都算得出 3 點路徑，也就是繞得過去，只是我們沒去試。
# 角度控制在 ±70° 以內，這樣繞路仍然會朝目標前進（cos70° ≈ 0.34）。
DETOUR_TRY = ((20.0, -40), (20.0, 40), (20.0, -70), (20.0, 70),
              (12.0, -40), (12.0, 40))

# --- 尋路要的 this ----------------------------------------------------------
# ⚠⚠ **不是**我們用 VT_PLAYER 掃到的那個位址，而是**它 −8**。
#     第一次直接拿掃到的位址去呼叫，遊戲當場崩潰 —— 就是本專案踩過兩次的老坑：
#     同一個物件有兩個 vtable、相隔 8 bytes。
# 來源鏈（`0x5045DE` 只是表查找，可以純讀取重現，不必呼叫）：
MGR_PTR = 0x0096E638        # 管理器指標
MGR_ID = 0x2A90             # 本尊的實體 ID
MGR_TBL = 0x2A74            # ID → 物件的表
MGR_MAX = 0x2AA4            # 表的上限
OBJ_ID = 0xBC               # 物件裡回存的 ID（要跟查表用的 ID 一致才算數）


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
    if len(pts) >= 2:
        return pts[0]              # 照著路徑走下一個轉角
    tx, ty = pts[-1]               # 只剩最後一個點 = 直線可通
    # ★ 不管設定多小，離目標一律留 MIN_GAP —— 太近會被判定卡在怪身體裡。
    keep = max(keep, MIN_GAP)
    seg = math.hypot(tx - here[0], ty - here[1])
    if seg <= keep:
        return None                # 已經夠近，不用走
    r = (seg - keep) / seg
    return (here[0] + (tx - here[0]) * r, here[1] + (ty - here[1]) * r)


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
    ident, tbl, mx = u32(mgr + MGR_ID), u32(mgr + MGR_TBL), u32(mgr + MGR_MAX)
    if None in (ident, tbl, mx) or (ident & 0xFFFF) > mx:
        return None
    obj = u32(tbl + (ident & 0xFFFF) * 4)
    if not obj or u32(obj + OBJ_ID) != ident:
        return None
    return obj
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
_CODE = 0x40


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
    """
    return f"""
    pushad
    pushfd
    mov eax, dword ptr [{block + _FLAG:#x}]
    test eax, eax
    jz skip
    mov dword ptr [{block + _FLAG:#x}], 0x0
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
        self._pid = pid
        self._exe = exe_path
        self._pm = None
        self._iat = 0
        self._orig = 0
        self._block = 0
        self._active = False
        # RLock 而非 Lock：call_sync() 內部會再呼叫 call()，同一條執行緒要能重入。
        self._lock = threading.RLock()
        # ★ 「有人在等指令槽」的計數（見 slot_wanted）。
        self._wanted = 0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def slot_wanted(self) -> bool:
        """尋路／移動那邊正在等指令槽 —— 攻擊要讓路。

        ⚠⚠ 沒有這個機制的話，攻擊執行緒會把指令槽佔到 **82%**（實測），
          UI 想問「這隻怪跟我之間有沒有障礙物」幾乎永遠問不到，
          於是一直當成「沒有障礙物、已經走夠近」→ 站著打卻打不到，
          直到 10 秒卡住偵測才換怪（使用者回報的「掛機還是會卡住」）。
        """
        return self._wanted > 0

    @property
    def lock(self) -> threading.RLock:
        """要「連續送好幾個呼叫、中間不能被插隊」時，自己抓著它。

        例如攻擊三連包必須照 ①②③ 送出，不能被移動指令切開。
        """
        return self._lock

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
        orig = pm.read_uint(iat)
        block = pm.allocate(0x1000)
        for off, val in ((_FLAG, 0), (_ORIG, orig), (_A1, 0), (_A2, 0),
                         (_CNT, 0), (_A4, 0), (_DONE, 0), (_FN, MOVE_FN),
                         (_A5, 0), (_A6, 0)):
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

    def call(self, fn: int, a1: int = 0, a2: int = 0, a3: int = 0,
             a4: int = 0, a5: int = 0, a6: int = 0, ecx: int = 0) -> bool:
        """請遊戲主執行緒呼叫 fn(a1..a5)；ecx 給 __thiscall 的 this。
        回傳是否排得進去。

        ★ 一律推五個參數。目標函式都是 cdecl（呼叫端清堆疊），
          參數比它需要的多不會有事 —— 它只是不看後面那幾個。

        只有一個指令槽：上一個還沒被執行完就回 False，呼叫端自己決定要不要重試。
        訊息迴圈每秒跑 60 次以上，我們的用量（每秒個位數）綽綽有餘。
        """
        if not self._active:
            return False
        pm = self._pm
        with self._lock:                       # ⚠ 見類別說明：不能兩邊交錯寫
            if pm.read_uint(self._block + _FLAG):
                return False                   # 上一個還沒執行
            for off, val in ((_FN, fn), (_A1, a1), (_A2, a2), (_CNT, a3),
                             (_A4, a4), (_A5, a5), (_A6, a6), (_ECX, ecx)):
                pm.write_uint(self._block + off, val & 0xFFFFFFFF)
            pm.write_uint(self._block + _FLAG, 1)
        return True

    def call_sync(self, fn: int, *args, ecx: int = 0,
                  timeout: float = 0.5) -> int | None:
        """呼叫並等它做完，回傳 eax；逾時回 None。

        用在「先尋路、拿到點數，再送移動封包」這種有先後relation的兩步。
        指令槽只有一個，所以一定要等前一個做完才排下一個。
        """
        # ⚠ 鎖要**含等待那段**：只鎖 call() 的話，別人可以在我們等結果時排下一個
        #   呼叫，我們就會讀到別人的 eax。
        with self._lock:
            if not self.call(fn, *args, ecx=ecx):
                return None
            t0 = time.time()
            while time.time() - t0 < timeout:
                if not self._pm.read_uint(self._block + _FLAG):
                    return self._pm.read_uint(self._block + _RET)
                time.sleep(0.005)
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
        self._wanted += 1
        try:
            got = self._lock.acquire(timeout=max(wait, SLOT_YIELD))
        finally:
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

    def end_move(self, scanner, player_obj: int) -> bool:
        """告訴客戶端「移動結束了、我就停在這裡」。**不會讓角色移動。**

        用在「卡進怪身體裡」：角色跟怪重疊時伺服器不給站，那段移動不算完成，
        客戶端就一直停在「移動中」狀態，之後的攻擊全部被忽略
        （實測：雪狐站 0.7 格連送 80 發零傷害，等怪自己走開才恢復）。
        使用者手動挪一步就會解除 —— 這裡改成直接把那個狀態收掉。

        參數全部來自 WALK_FN(0x5D7D96) 結尾那段的反組譯，沒有猜的成分：
            0x5D7E28  push [ebp+0x18]          ← 終點 Y
            0x5D7E2B  mov  ecx, [0x9B66AC]     ← this（全域單例）
            0x5D7E31  push [ebp+0x14]          ← 終點 X
            0x5D7E34  push 1
            0x5D7E36  call 0x5B5C60
        →  0x5B5C60(this, 1, 終點X, 終點Y)，終點就給「我現在站的地方」。

        ⚠ this 是全域指標，讀到 0 就不呼叫（遊戲還沒初始化好）。
        """
        if not self._active or not player_obj:
            return False
        raw = scanner._read_bytes(END_MOVE_THIS, 4)
        this = struct.unpack("<I", bytes(raw))[0] if raw else 0
        if not this:
            return False
        pos = scanner._read_bytes(player_obj + entity.OFF_POS_X, 8)
        if not pos:
            return False
        wx, wy = (v >> 16 for v in struct.unpack("<II", bytes(pos)))
        return self.call(END_MOVE_FN, 1, wx, wy, ecx=this)

    def walk_route(self, scanner, player_obj: int,
                   tile_x: float, tile_y: float, stop_short: float = 0.0,
                   wait: float = 0.12) -> int:
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

        self._wanted += 1                  # 舉手要指令槽，攻擊會讓一拍
        try:
            got = self._lock.acquire(timeout=max(wait, SLOT_YIELD))
        finally:
            self._wanted -= 1
        if not got:
            return 0
        try:
            def path(px: float, py: float) -> int:
                v = self.call_sync(
                    PATHFIND_FN,
                    int(px * entity.TILE_UNITS) & 0xFFFF,
                    int(py * entity.TILE_UNITS) & 0xFFFF,
                    WAYPOINTS, ecx=this, timeout=0.15)
                return v if (v and 0 < v <= MAX_POINTS) else 0

            n = path(tile_x, tile_y)
            short = stop_short
            if not n:
                # 目標超出尋路一次算得出的範圍（實測約 28~40 格）→ 走中繼點
                # 接力，呼叫端下一拍再下一次就能走很遠。
                # ⚠ 直線方向整條算不出來 = 那個方向有牆，換角度繞。
                dx, dy = tile_x - cx, tile_y - cy
                full = math.hypot(dx, dy) or 1.0
                for hop in PATH_TRY:
                    if hop < full:
                        n = path(cx + dx / full * hop, cy + dy / full * hop)
                        if n:
                            break
                if not n:
                    ang = math.atan2(dy, dx)
                    for hop, deg in DETOUR_TRY:
                        if hop >= full:
                            continue
                        a = ang + math.radians(deg)
                        px, py = cx + math.cos(a) * hop, cy + math.sin(a) * hop
                        if math.hypot(tile_x - px, tile_y - py) >= full:
                            continue      # 繞過去反而更遠，不要
                        n = path(px, py)
                        if n:
                            break
                if not n:
                    return 0
                short = 0.0               # 中繼點本身就要走到底

            pts = self.read_path(scanner, n)
            gx, gy = _approach_point((cx, cy), pts, short) or pts[-1]
            self.call(WALK_FN, this, wx, wy,
                      int(gx * entity.TILE_UNITS) & 0xFFFF,
                      int(gy * entity.TILE_UNITS) & 0xFFFF, 0)
            return n
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

    def walk_to(self, scanner, player_obj: int,
                tile_x: float, tile_y: float, wait: float = 0.12) -> int:
        """走到指定的格子座標。**回傳尋路算出的路徑點數**（0 = 走不了）。

        ★ 那個點數就是免費的「有沒有障礙物」指標：
              1 個點  = 直線過得去，中間沒東西擋
              多個點  = 要繞，代表我們跟目標之間有地形
          呼叫端可以拿它決定「是不是該一路走到怪臉上」——
          隔著牆的話站在原地打是打不到的，距離再近也一樣。

        ★ **會自動切成多個路徑點**，一個指令走完全程。
          只送一個點的話超過約 19~20 格就會半路停住（實測：送 30 格只走 19.0），
          症狀是「走到一半卡住」而且不會報錯。遊戲自己也是送多點的
          （使用者擷取到的移動封包 38 bytes = 3 個點）。
        """
        raw = scanner._read_bytes(player_obj + entity.OFF_POS_X, 8)
        if not raw:
            return 0
        wx, wy = (v >> 16 for v in struct.unpack("<II", raw))
        cx, cy = wx / entity.TILE_UNITS, wy / entity.TILE_UNITS
        dx, dy = tile_x - cx, tile_y - cy
        full = math.hypot(dx, dy)
        if full < 0.5:
            return 0

        # ★ 一律走遊戲自己的尋路：它會繞過地形，我們自己只會畫直線。
        # 尋路有效範圍約 30~40 格，太遠回 0，所以由遠到近試幾個中繼距離；
        # 走到中繼點後呼叫端再下一次，就能接力走很遠
        # （實測 85.9 格、8.5 秒到達，全程繞過地形）。
        this = pathfinder_this(scanner)
        if not this:
            return 0
        # ★ 「算路徑 → 送移動封包」中間不能被插隊：路徑點是寫在**全域**陣列
        #   0x9B6684 裡的，別人（攻擊封包）中間插一個呼叫倒是不會動到它，
        #   但我們自己若被切開就可能拿舊路徑去送。整段一起鎖最安全。
        # ⚠ 但這也常在 UI 執行緒上呼叫，所以只等 wait 秒 —— 等不到就回 0，
        #   呼叫端過一下會再試一次，總之不要凍住畫面（見 path_to 的說明）。
        self._wanted += 1                  # 舉手要指令槽，攻擊會讓一拍
        try:
            got = self._lock.acquire(timeout=max(wait, SLOT_YIELD))
        finally:
            self._wanted -= 1
        if not got:
            return 0

        def try_point(px: float, py: float) -> int:
            tx = int(px * entity.TILE_UNITS) & 0xFFFF
            ty = int(py * entity.TILE_UNITS) & 0xFFFF
            # 先問路徑點數：一來確認走得到，二來點數是「有沒有障礙物」的指標。
            n = self.call_sync(PATHFIND_FN, tx, ty, WAYPOINTS,
                               ecx=this, timeout=0.15)
            if n and 0 < n <= MAX_POINTS:
                # ★ 交給遊戲自己的「走到座標」常式，不要自己送 MOVE_FN ——
                #   它會重算一次路徑並補做收尾（我們手動那版少了那兩步）。
                self.call(WALK_FN, this, wx, wy, tx, ty, 0)
                return n
            return 0

        try:
            for hop in (full,) + PATH_TRY:
                if hop > full:
                    continue
                n = try_point(cx + dx / full * hop, cy + dy / full * hop)
                if n:
                    return n
            # 直線方向整條都算不出來 = 那個方向有牆。換角度繞（見 DETOUR_TRY）。
            # ⚠ 只有在直線全失敗時才做，平常不會多花這些呼叫。
            # ⚠⚠ **繞路的落點一定要比現在更靠近目標**，否則就是原地打轉：
            #    實測（黑狐回 195 格外的原點）沒有這條規則時，走到某一格之後
            #    繞路又把它帶回原處，然後無限來回，看起來一直在動卻永遠到不了。
            ang = math.atan2(dy, dx)
            for hop, deg in DETOUR_TRY:
                if hop > full:
                    continue
                a = ang + math.radians(deg)
                px, py = cx + math.cos(a) * hop, cy + math.sin(a) * hop
                if math.hypot(tile_x - px, tile_y - py) >= full:
                    continue                   # 繞過去反而更遠，不要
                n = try_point(px, py)
                if n:
                    return n
        finally:
            self._lock.release()

        # ⚠ 連最短的中繼點都算不出路徑 = 那個方向真的不通。
        # **不要退回直線走** —— 那只會讓角色貼著牆推、原地卡住
        # （使用者回報的「往那個方向卡住」就是這樣來的）。
        # 回 0 讓呼叫端自己決定（掛機那邊會由卡住偵測換目標）。
        return 0

    def walk_path(self, scanner, player_obj: int, tiles) -> bool:
        """低階：直接送出指定的路徑點（最後一個是終點）。

        ⚠ **不會尋路**，遊戲就照你給的點走直線 —— 撞到地形會卡住。
        一般移動請用 walk_to()，它會先叫遊戲算路徑。
        這個留給「已經有現成路徑」的情況（例如測試）。
        """
        if not self._active or not player_obj or not tiles:
            return False
        pts = list(tiles)[:MAX_POINTS]
        raw = scanner._read_bytes(player_obj + entity.OFF_POS_X, 8)
        if not raw:
            return False
        # 起點要用**當下**的世界座標，遊戲會拿它跟路徑點算路線
        wx, wy = (v >> 16 for v in struct.unpack("<II", raw))
        buf = b"".join(
            struct.pack("<HH",
                        int(x * entity.TILE_UNITS) & 0xFFFF,
                        int(y * entity.TILE_UNITS) & 0xFFFF)
            for x, y in pts)
        self._pm.write_bytes(WAYPOINTS, buf, len(buf))
        return self.call(MOVE_FN, wx, wy, len(pts), random.randint(1, 0xFFFF))

    def calls_done(self) -> int:
        """stub 總共替我們呼叫了幾次（診斷用）。"""
        return self._pm.read_uint(self._block + _DONE) if self._active else 0

    def stop(self) -> None:
        """還原 IAT。**一定要呼叫** —— 不還原就等於在遊戲裡留了一段跳板。"""
        if not self._active:
            return
        try:
            from app.core import injector

            self._pm.write_uint(self._block + _FLAG, 0)
            injector.SendCapture._protect(self._pm, self._iat, 8, 0x40)
            self._pm.write_uint(self._iat, self._orig)
        except Exception:               # noqa: BLE001
            pass
        self._active = False
