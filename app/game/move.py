"""讓角色走到指定座標。

為什麼需要注入
--------------
移動沒有「改一個欄位就會動」的捷徑（都試過了）：
  · 玩家物件 `+0x144/+0x148` 確實是目的地，但**我們自己寫進去角色完全不動**
    ——那是遊戲寫給自己看的，不像攻擊目標 `+0x2D8` 會被重讀。
  · 竄改怪的座標、借攻擊指令去走：能設定目的地，但要同時贏過「伺服器持續更新
    怪的座標」與「進入攻擊範圍就停」兩個機制，實測角色會亂走，不能用。

所以走遊戲自己的移動函式，讓它處理尋路封包、混淆與加密。

移動函式（反組譯 + 實測參數，見 app/core/injector.py 的參數擷取）
---------------------------------------------------------------
    0x55A046   f(u16 起點世界X, u16 起點世界Y, int 路徑點數, u16 亂數)
               封包 = 上面四個欄位 + 全域陣列 0x9B6684 的 路徑點數*4 bytes
               每個路徑點 = 兩個 u16 世界座標（一格 32，見 app/game/entity.py）

    a4 是 16 位元亂數（實測 7 筆全不同、跟 GetTickCount 對不上、也不遞增），
    隨便給即可。

怎麼呼叫（不開新執行緒）
------------------------
掛 **PeekMessageA 的 IAT**（遊戲訊息迴圈每幀都會呼叫），stub 平時只做兩條指令的
旗標檢查；要移動時把參數寫好、旗標設 1，下一幀遊戲主執行緒就會替我們呼叫一次。

★ 一定要在**遊戲主執行緒**上呼叫，不能開 remote thread —— 那個函式會碰遊戲的
連線物件與封包佇列，在別的執行緒上動它等於資料競爭。
這跟 app/core/injector.py 的 send 攔截是同一套已驗證的 IAT hook 機制。

實測（黑狐）：走 3 格準確停在 3.00 格、走 18 格 2 秒精準抵達，位置穩定不回彈
（伺服器認），遊戲全程正常、IAT 乾淨還原。

⚠ 只給 1 個路徑點 = 直線走。遇到地形障礙會卡住（遊戲自己的路徑有 2～3 個點，
是它的尋路算出來的，我們沒有）。呼叫端要自己做「位置沒變 = 卡住」的偵測。
"""
from __future__ import annotations

import math
import random
import struct
import time

from app.game import entity
from app.game.entity import read_pos

MOVE_FN = 0x0055A046        # 移動封包建構＋送出函式
WAYPOINTS = 0x009B6684      # 全域路徑點陣列
# ⛔⛔ 遊戲自己的尋路：**不要呼叫，會弄崩遊戲**（實測把黑狐打掛了）。
# 反組譯 0x556982~0x556990（滑鼠點地板走路的那條路）長這樣：
#       call 0x54A572
#       push 0x9B6684        ← 輸出：路徑點陣列
#       push edi             ← 目標 Y（世界座標）
#       push esi             ← 目標 X
#       mov  ecx, ebx        ← this
#       call 0x549B81        ← 尋路，eax = 路徑點數量
# 實測直接呼叫（ecx 給我們的玩家物件 VT_PLAYER=0x7D8BE0）：
#   · 回傳 0（算不出路徑），連呼叫幾次之後**遊戲當場崩潰**
#   · 原因推測：`this` 不是這個物件 —— 上面的 ebx 來自
#     `0x5045de([0x96e638]+0x2a90)`，是另一個東西；而且前面那個
#     `call 0x54A572` 可能有必要的前置狀態。**沒有證據前別再試。**
# 真要走這條路，得先把 ebx 的來源鏈完整解出來並驗證，不能用猜的。
PATHFIND_FN = 0x00549B81        # 只留作紀錄，程式不呼叫它
HOOK_IMPORT = "PeekMessageA"
MAX_POINTS = 32             # 一次最多送幾個路徑點（封包大小 = 點數*4+9）

# ★ 每個路徑點之間隔多遠（格）。**只送一個點的話走不遠**：
#     送 10 格 → 走 9.5　送 20 格 → 走 19.0　送 30 格 → **還是只走 19.0**
#   超過約 19~20 格的部分會被默默丟掉，症狀是「走到一半停住」而且不會報錯。
#   遊戲自己送的移動封包是 38 bytes = 3 個路徑點，就是為了這個。
#   所以我們也切成多點；用比上限小不少的間距，留餘裕。
HOP_TILES = 12.0

# 注入區塊的版面
_FLAG, _ORIG, _A1, _A2, _CNT, _A4, _DONE, _FN = (
    0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C)
_A5, _ECX, _RET, _ESP = 0x20, 0x24, 0x28, 0x2C
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

    ★ 一律推五個參數：推得比它需要的多不會有事，它只是不看後面幾個。
    """
    return f"""
    pushad
    pushfd
    mov eax, dword ptr [{block + _FLAG:#x}]
    test eax, eax
    jz skip
    mov dword ptr [{block + _FLAG:#x}], 0x0
    mov dword ptr [{block + _ESP:#x}], esp
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


# --- 繞行導航 ---------------------------------------------------------------
# 我們只送直線的單一路徑點，**沒有尋路**（遊戲自己的路徑是腳本算的，我們拿不到）。
# 所以撞到地形時角色會一直往牆裡走、原地不動 —— 使用者回報的「往那個方向卡住」。
NAV_STUCK_SECS = 1.2        # 位置沒動這麼久就當作撞牆
NAV_MOVED = 0.4             # 位移超過這麼多格才算「有在動」
NAV_DETOUR = 10.0           # 繞行時往側面走幾格
NAV_DETOUR_SECS = 2.5       # 一次繞行持續多久


class Navigator:
    """朝目標走，撞牆就往側面繞一段再繼續。一個分身一個。

    這不是真正的尋路，是最土的「撞牆就側移」（bug algorithm）：
    左右輪流試，繞不過去就換邊。對付一般地形夠用，
    真的被包死時呼叫端的「卡住偵測」會換目標。
    """

    def __init__(self, mover: "Mover") -> None:
        self._mv = mover
        self.reset()

    def reset(self) -> None:
        self._last: tuple[float, float] | None = None
        self._stuck = 0.0
        self._side = 1              # 1 = 往左繞，-1 = 往右繞
        self._detour: tuple[float, float] | None = None
        self._detour_left = 0.0
        self.detours = 0            # 這一趟繞了幾次（診斷用）

    def step(self, scanner, player_obj: int, gx: float, gy: float,
             dt: float) -> bool:
        """朝 (gx,gy) 前進一步。回傳這次是否正在繞行。"""
        pos = read_pos(scanner, player_obj)
        if pos is None:
            return False
        if self._last is not None:
            moved = math.hypot(pos[0] - self._last[0], pos[1] - self._last[1])
            self._stuck = 0.0 if moved > NAV_MOVED else self._stuck + dt
        self._last = pos

        if self._detour_left > 0:
            self._detour_left -= dt
            if self._detour_left > 0 and self._detour:
                self._mv.walk_to(scanner, player_obj, *self._detour)
                return True
            self._detour = None

        if self._stuck >= NAV_STUCK_SECS:
            # 撞牆了：往「面向目標的側面」走一段，繞過去再說
            self._stuck = 0.0
            self.detours += 1
            self._side = -self._side          # 左右輪流試
            dx, dy = gx - pos[0], gy - pos[1]
            n = math.hypot(dx, dy) or 1.0
            self._detour = (pos[0] - dy / n * NAV_DETOUR * self._side,
                            pos[1] + dx / n * NAV_DETOUR * self._side)
            self._detour_left = NAV_DETOUR_SECS
            self._mv.walk_to(scanner, player_obj, *self._detour)
            return True

        self._mv.walk_to(scanner, player_obj, gx, gy)
        return False


class Mover:
    """單一遊戲行程的移動控制。非執行緒安全，請在同一執行緒使用。"""

    def __init__(self, pid: int, exe_path: str) -> None:
        self._pid = pid
        self._exe = exe_path
        self._pm = None
        self._iat = 0
        self._orig = 0
        self._block = 0
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

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
                         (_A5, 0)):
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
             a4: int = 0, a5: int = 0, ecx: int = 0) -> bool:
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
        if pm.read_uint(self._block + _FLAG):
            return False                       # 上一個還沒執行
        for off, val in ((_FN, fn), (_A1, a1), (_A2, a2), (_CNT, a3),
                         (_A4, a4), (_A5, a5), (_ECX, ecx)):
            pm.write_uint(self._block + off, val & 0xFFFFFFFF)
        pm.write_uint(self._block + _FLAG, 1)
        return True

    def call_sync(self, fn: int, *args, ecx: int = 0,
                  timeout: float = 0.5) -> int | None:
        """呼叫並等它做完，回傳 eax；逾時回 None。

        用在「先尋路、拿到點數，再送移動封包」這種有先後relation的兩步。
        指令槽只有一個，所以一定要等前一個做完才排下一個。
        """
        if not self.call(fn, *args, ecx=ecx):
            return None
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self._pm.read_uint(self._block + _FLAG):
                return self._pm.read_uint(self._block + _RET)
            time.sleep(0.005)
        return None

    def walk_to(self, scanner, player_obj: int,
                tile_x: float, tile_y: float) -> bool:
        """走到指定的格子座標。回傳是否成功送出請求。

        ★ **會自動切成多個路徑點**，一個指令走完全程。
          只送一個點的話超過約 19~20 格就會半路停住（實測：送 30 格只走 19.0），
          症狀是「走到一半卡住」而且不會報錯。遊戲自己也是送多點的
          （使用者擷取到的移動封包 38 bytes = 3 個點）。
        """
        raw = scanner._read_bytes(player_obj + entity.OFF_POS_X, 8)
        if not raw:
            return False
        wx, wy = (v >> 16 for v in struct.unpack("<II", raw))
        cx, cy = wx / entity.TILE_UNITS, wy / entity.TILE_UNITS
        d = math.hypot(tile_x - cx, tile_y - cy)
        k = max(1, min(MAX_POINTS, math.ceil(d / HOP_TILES)))
        pts = [(cx + (tile_x - cx) * (i + 1) / k,
                cy + (tile_y - cy) * (i + 1) / k) for i in range(k)]
        return self.walk_path(scanner, player_obj, pts)

    def walk_path(self, scanner, player_obj: int, tiles) -> bool:
        """依序走過多個格子座標（最後一個是終點）。"""
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
