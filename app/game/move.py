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

from app.game import entity

MOVE_FN = 0x0055A046        # 移動封包建構＋送出函式
WAYPOINTS = 0x009B6684      # 全域路徑點陣列
HOOK_IMPORT = "PeekMessageA"
MAX_POINTS = 32             # 一次最多送幾個路徑點（封包大小 = 點數*4+9）

# ★ 單次移動指令走得到的上限（格）。實測：
#     送 10 格 → 走 9.5　送 20 格 → 走 19.0　送 30 格 → **還是只走 19.0**
#   也就是超過約 19~20 格的部分會被丟掉。使用者回報「太遠會失效、走不回原點」
#   就是這個 —— 而且症狀是「走到一半停住」，不會報錯，很容易誤判成程式沒送出。
#   （試過改送多個路徑點，有進步但仍走不完：45 格只走 32 格。）
# 所以一律把目的地夾到這個距離內，靠呼叫端重複下令接力走完全程。
MAX_HOP = 15.0

# 注入區塊的版面
_FLAG, _ORIG, _A1, _A2, _CNT, _A4, _DONE, _FN = (
    0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C)
_A5 = 0x20
_CODE = 0x40


def _stub_asm(block: int) -> str:
    """旗標一舉起就呼叫一次移動函式，否則原封不動跳回真正的 PeekMessageA。

    ⚠ keystone 把無前綴數字當十六進位，所以一律寫 0x。
    ⚠ pushad/pushfd 必須成對而且順序相反地還原，否則遊戲的暫存器會壞掉。
    ⚠ 目標函式都是 cdecl（結尾是單純的 ret），所以參數要由我們自己清
      （一律推五個 = add esp,0x14）。推得比它需要的多不會有事，它只是不看後面幾個。
    """
    return f"""
    pushad
    pushfd
    mov eax, dword ptr [{block + _FLAG:#x}]
    test eax, eax
    jz skip
    mov dword ptr [{block + _FLAG:#x}], 0x0
    push dword ptr [{block + _A5:#x}]
    push dword ptr [{block + _A4:#x}]
    push dword ptr [{block + _CNT:#x}]
    push dword ptr [{block + _A2:#x}]
    push dword ptr [{block + _A1:#x}]
    mov eax, dword ptr [{block + _FN:#x}]
    call eax
    add esp, 0x14
    inc dword ptr [{block + _DONE:#x}]
    skip:
    popfd
    popad
    jmp dword ptr [{block + _ORIG:#x}]
    """


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

    def call(self, fn: int, a1: int = 0, a2: int = 0,
             a3: int = 0, a4: int = 0, a5: int = 0) -> bool:
        """請遊戲主執行緒呼叫 fn(a1, a2, a3, a4, a5)。回傳是否排得進去。

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
        for off, val in ((_FN, fn), (_A1, a1), (_A2, a2),
                         (_CNT, a3), (_A4, a4), (_A5, a5)):
            pm.write_uint(self._block + off, val & 0xFFFFFFFF)
        pm.write_uint(self._block + _FLAG, 1)
        return True

    def walk_to(self, scanner, player_obj: int,
                tile_x: float, tile_y: float) -> bool:
        """朝指定的格子座標走。回傳是否成功送出請求。

        ★ 太遠的目的地會**自動夾到 MAX_HOP 格**（見上面的實測）——
          單次指令走不到那麼遠，硬送只會走一半就停。
          呼叫端定期重下就會一段一段接力走完，不必自己算分段。
        """
        raw = scanner._read_bytes(player_obj + entity.OFF_POS_X, 8)
        if raw:
            wx, wy = (v >> 16 for v in struct.unpack("<II", raw))
            cx, cy = wx / entity.TILE_UNITS, wy / entity.TILE_UNITS
            d = math.hypot(tile_x - cx, tile_y - cy)
            if d > MAX_HOP:
                r = MAX_HOP / d
                tile_x, tile_y = cx + (tile_x - cx) * r, cy + (tile_y - cy) * r
        return self.walk_path(scanner, player_obj, [(tile_x, tile_y)])

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
