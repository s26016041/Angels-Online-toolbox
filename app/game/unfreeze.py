"""失焦不凍畫面：patch 遊戲的視窗程序，讓它失焦時不停止畫面更新。

## 根因（完整調查見 memory 的 background-unfreeze-investigation）

天使 Online 的視窗**失焦（不是最小化）時畫面會凍住，但邏輯／網路照跑**。
反組譯出來的機制（angel.dat 無 ASLR，基底 0x400000）：

* 視窗訊息 handler 收到 `WM_ACTIVATE(0x06)` / `WM_ACTIVATEAPP(0x1C)` 時，
  會算出「現在是不是 active」這個布林，存進視窗物件 `[this+0xC]`；
  **只有狀態真的變了**才去呼叫 `OnActivate([vtable+0x50])`。
  ⚠ 2026-08-25 改版把這一層重寫了（v3，詳見下方與 locate.SIGS 的註解）：
    WndProc 變成 thunk、真正的 handler 是虛擬函式、訊息用跳表分派，而且
    **兩個訊息拆成兩支 handler** —— active 狀態機只留在 WM_ACTIVATEAPP
    那支。實務上不影響：分身彼此是不同行程，切換時照樣發 WM_ACTIVATEAPP。
* `OnActivate` 做三件事（2026-08-13 全支反組譯定案）：
  1. `0x70B630(bActive)` → **輸入層**（this=[0xA158F4]）：記下 active 狀態，
     取得焦點時呼叫 `0x70BCF0`（重新取得輸入裝置）、失焦呼叫 `0x70B7F0`（放掉）。
  2. 依 bActive 分支：取得焦點＝恢復音效（0x70A970/0x70A980）；失焦＝暫停
     （0x70A650/0x70A660）＋全螢幕時 ShowWindow 最小化。
  3. 廣播 `0x705F60(bActive)`：對每個 listener 叫 `vtable+0x1C`（active）或
     `vtable+0x20`（deactivate）。**實測真正凍住畫面的就是這個 OnDeactivate**
     （它停掉場景／動畫的更新）——只擋「暫停音效」那條完全沒用（覆蓋率 0%）。

## 這個 patch（v2，2026-08-13 —— 防凍＋不擋手動操作）

第一版只把 handler 裡「失焦」算出來的布林壓成 1（永遠 active → 狀態沒變化
→ OnActivate 完全不跑）。失焦不凍實測 100%，**但重新聚焦時 OnActivate(1)
也不跑了** —— 輸入層的重新取得（0x70BCF0）沒人叫，DirectInput 裝置在失焦時
被系統放掉之後再也接不回來 → 「右鍵鎖怪→走過去放技能」失靈（走路正常、
放不出招；見 memory 的 manual-cast-broken-investigation）。

v2/v3 都是兩個位置、共 9 個位元組，都在同一段 WM_ACTIVATEAPP handler 裡
（下面的位址與位元組是 v3＝2026-08-25 改版後的樣子；v2 的是 0x70CC5A／
32 C0→B0 01 與 +0xE／je +0x0A，語意一模一樣）：

    0x70D387（A）  0F 95 C2（setne dl）    → B2 01 90（mov dl,1 / nop）
        失焦也算 active → [this+0xC] 恆為 1 → OnActivate(0) 永不執行
        ＝不暫停、不廣播 deactivate ＝ 失焦不凍（跟 v1 一模一樣，實測過）。
    0x70D38F（B）  0F 84 rel32（je 沒變就跳走）→ 83 7D 10 00 74 08
        ＝ cmp dword [ebp+0x10], 0（wParam；0 ＝ 失焦）
          je  +8（跳到等價的 epilogue，什麼都不做）
        wParam ≠ 0（取得焦點）→ **每次都呼叫 OnActivate(1)** ＝ 遊戲原本
        重新聚焦要做的整套（輸入層重新取得＋恢復音效＋廣播 active）——
        手動操作就是靠這一下復活的。

* 失焦路徑跟 v1 完全相同（什麼都不跑）；唯一新增的是「取得焦點時把遊戲
  原本就會做的那段補回來」——那段程式碼原版每次重新聚焦都在跑，是熟路。
* 位置 B 的原樣是 `je rel32`，**位移量是那一版程式碼的版面、改版會變**，
  所以：判斷「原始狀態」只認前兩個位元組 `0F 84`（je rel32 的形狀）；
  還原時寫 `0F 84 08 00 00 00`＝跳到 call 後面那個 epilogue（v3 的 0x70D39D）
  —— 跟原本跳的 0x70D970 **語意完全等價**（反組譯核對過：同樣恢復暫存器、
  eax=0、cookie 檢查、ret 0x10），而且位移量由特徵鎖住的骨架保證。
  ⚠ 位移量本身**會隨改版變**（v2 是 0x0A、v3 是 8）—— 改版時要照特徵鎖住的
  骨架重新數一次，不是「不隨版本變」。⚠ 不能把今天讀到的位移記起來還原 —— 改版後寫回舊位移
  ＝跳到錯的地方＝當機。

## 位址怎麼來

`PATCH_ADDR`（位置 A）由 `locate.warm()` 依 AOB 特徵自動定位（見 locate.py
的 `unfreeze.PATCH_ADDR`；特徵把 A 的 3 個與 B 的 6 個位元組**全部遮成 ??**，
所以 patch 前後都定位得到）。位置 B ＝ `PATCH_ADDR + SITE2_OFF`，中間那幾個
位元組正是特徵的錨，同一版之內距離不會變。定位失敗會清成 0，這裡每個寫入前都會問
`locate.located()` 並**當場重讀確認內容是預期的樣子**才動手 ——
寫錯位址是直接破壞遊戲記憶體，這條退路一定要有。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from app.game import locate

# locate.warm() 會把這個改寫成當下的絕對位址。沒定位成功前是 2026-08-12 的值，
# 但 apply()/remove() 一律先問 locate.located() 才會用，所以這個預設值只是文件。
PATCH_ADDR = 0x0070D387
# ★ 位置 B（je 那 6 個位元組）＝ PATCH_ADDR + 8。中間隔的 5 bytes
#   （88 57 0C 3A D1 ＝ mov [edi+0xc],dl / cmp dl,cl）就是 AOB 特徵的錨
#   —— 相對距離由特徵骨架保證，改版時 PATCH_ADDR 重新定位、這個距離不變。
#   ⚠⚠ 這個距離是**版面**：v2（2026-08-13）是 0xE，v3（2026-08-25 改版）是 8。
#     改版後一定要重新數 —— 寫錯＝跳進指令中間＝當場當機。
SITE2_OFF = 0x8

ORIG = b"\x0f\x95\xc2"      # setne dl        → 算出「失焦＝inactive」
PATCHED = b"\xb2\x01\x90"   # mov dl,1 / nop  → 永遠 active

# 位置 B：cmp dword [ebp+0x10],0（wParam）/ je +8（失焦→什麼都不做）
PATCHED2 = b"\x83\x7d\x10\x00\x74\x08"
# 原樣＝je rel32 的形狀。位移量（後 4 bytes）是版面、改版會變，不比對。
JE_SHAPE = b"\x0f\x84"
# 還原寫這個：je rel32 → 跳過那 8 bytes 的 OnActivate 呼叫，落在等價的
# epilogue（語意同原本的遠跳目標，位移量由特徵骨架保證）。
# ⚠ 不是原始位移 —— 理由見檔頭。
RESTORE2 = b"\x0f\x84\x08\x00\x00\x00"

PAGE_EXECUTE_READWRITE = 0x40

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.VirtualProtectEx.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
_k32.VirtualProtectEx.restype = wintypes.BOOL
_k32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
_k32.WriteProcessMemory.restype = wintypes.BOOL


def _located() -> bool:
    return locate.located("unfreeze", "PATCH_ADDR")


def _site2() -> int:
    return PATCH_ADDR + SITE2_OFF


def state(scanner) -> str:
    """回傳這一台目前的狀態：

    * ``"on"``      —— 兩處都已套用（失焦不凍＋重新聚焦會重設輸入）。
    * ``"off"``     —— 兩處都是原始狀態（失焦會凍）。
    * ``"mixed"``   —— 套了一半：多半是**舊版工具箱只套了位置 A**（v1），
                       或上次寫到一半。apply()/remove() 都能把它補齊／收乾淨，
                       呼叫端照「想要的方向」推一把即可。
    * ``"unknown"`` —— 還沒定位成功、讀不到、或內容不是我們認得的樣子
                       （可能改版搬家了）。這種狀態一律**不准寫入**。
    """
    if not _located():
        return "unknown"
    a = scanner._read_bytes(PATCH_ADDR, len(ORIG))
    b = scanner._read_bytes(_site2(), len(PATCHED2))
    if not a or not b or len(a) < len(ORIG) or len(b) < len(PATCHED2):
        return "unknown"
    a_on, a_off = a == PATCHED, a == ORIG
    b_on, b_off = b == PATCHED2, bytes(b[:2]) == JE_SHAPE
    if a_on and b_on:
        return "on"
    if a_off and b_off:
        return "off"
    if (a_on or a_off) and (b_on or b_off):
        return "mixed"
    return "unknown"


def _write(scanner, addr: int, data: bytes, ok_before) -> bool:
    """把 `data` 寫進 `addr`，但**寫之前當場重讀**，只有 `ok_before(現值)`
    點頭才動手；寫完再讀回來確認。任何一關過不了就不寫／回 False。
    """
    if not _located():
        return False
    if not getattr(scanner, "can_write", False):
        return False
    handle = scanner._handle
    if not handle:
        return False
    n = len(data)
    # ★ 當場重讀重驗（見 CLAUDE.md「交給遊戲的位址送出前當場重讀重驗」）。
    now = scanner._read_bytes(addr, n)
    if not now or len(now) < n:
        return False
    if now == data:
        return True                          # 已經是想要的樣子了，不必再寫
    if not ok_before(bytes(now)):
        return False                         # 不是預期的原樣 → 抓錯位址，拒寫
    old = wintypes.DWORD()
    if not _k32.VirtualProtectEx(handle, ctypes.c_void_p(addr), n,
                                 PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
        return False
    written = ctypes.c_size_t(0)
    ok = _k32.WriteProcessMemory(handle, ctypes.c_void_p(addr),
                                 data, n, ctypes.byref(written))
    # 還原頁面保護（VirtualProtectEx 回來的 old 才是原本的值）。
    tmp = wintypes.DWORD()
    _k32.VirtualProtectEx(handle, ctypes.c_void_p(addr), n,
                          old.value, ctypes.byref(tmp))
    if not ok or written.value != n:
        return False
    return scanner._read_bytes(addr, n) == data


def apply(scanner) -> bool:
    """套用（失焦不凍＋重新聚焦重設輸入）。成功或本來就已套用回 True。

    兩處各自冪等：已套的跳過、原樣的才寫 —— 所以「舊版只套了 A」的分身
    （state()=="mixed"）呼叫這支就會把 B 補上。
    """
    ok_a = _write(scanner, PATCH_ADDR, PATCHED, lambda b: b == ORIG)
    # B 的原樣只認 je rel32 的形狀（位移量版本相依，見檔頭）。
    ok_b = _write(scanner, _site2(), PATCHED2,
                  lambda b: bytes(b[:2]) == JE_SHAPE)
    return ok_a and ok_b


def remove(scanner) -> bool:
    """還原成原始（失焦會凍）。成功或本來就已還原回 True。

    ⚠ 位置 B 還原寫的是**等價**的 je（跳到近處的 epilogue），不是當初的
      位移量 —— 舊位移改版後就是錯的，寫回去＝跳錯地方（檔頭有完整理由）。
    """
    ok_a = _write(scanner, PATCH_ADDR, ORIG, lambda b: b == PATCHED)
    # B 已經是 je 形狀（原版原樣或我們還原過的等價 je）＝不必動；
    # 只有還掛著我們的 PATCHED2 才寫回 RESTORE2。
    now = scanner._read_bytes(_site2(), len(PATCHED2)) if _located() else None
    if now and len(now) >= 2 and bytes(now[:2]) == JE_SHAPE:
        ok_b = True
    else:
        ok_b = _write(scanner, _site2(), RESTORE2, lambda b: b == PATCHED2)
    return ok_a and ok_b
