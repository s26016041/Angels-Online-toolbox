"""失焦不凍畫面：patch 遊戲的視窗程序，讓它失焦時不停止畫面更新。

## 根因（完整調查見 memory 的 background-unfreeze-investigation）

天使 Online 的視窗**失焦（不是最小化）時畫面會凍住，但邏輯／網路照跑**。
反組譯出來的機制（angel.dat 無 ASLR，基底 0x400000）：

* 視窗訊息 handler 收到 `WM_ACTIVATE(0x06)` / `WM_ACTIVATEAPP(0x1C)` 時，
  會算出「現在是不是 active」這個布林，存進視窗物件 `[this+0xC]`；
  **只有狀態真的變了**才去呼叫 `OnActivate([vtable+0x50])`。
  ⚠ 2026-08-25 改版把這一層重寫了（v3）：WndProc 變成 thunk、真正的 handler
    是虛擬函式、訊息用跳表分派，而且**兩個訊息拆成兩支 handler** —— active
    狀態機只留在 WM_ACTIVATEAPP 那支。實務上不影響：分身彼此是不同行程，
    切換時照樣發 WM_ACTIVATEAPP。
  ⚠ 2026-09-01 改版又動一次（v4，詳見下方與 locate.SIGS 的註解）：那段狀態機
    從 handler 裡**抽成一支獨立的共用小函式** `SetActive(bActive)`，
    handler 只剩「算布林 → 呼叫它」。patch 點跟著搬進那支小函式。
* `OnActivate` 做三件事（2026-08-13 全支反組譯定案）：
  1. `0x70B630(bActive)` → **輸入層**（this=[0xA158F4]）：記下 active 狀態，
     取得焦點時呼叫 `0x70BCF0`（重新取得輸入裝置）、失焦呼叫 `0x70B7F0`（放掉）。
  2. 依 bActive 分支：取得焦點＝恢復音效（0x70A970/0x70A980）；失焦＝暫停
     （0x70A650/0x70A660）＋全螢幕時 ShowWindow 最小化。
  3. 廣播 `0x705F60(bActive)`：對每個 listener 叫 `vtable+0x1C`（active）或
     `vtable+0x20`（deactivate）。**實測真正凍住畫面的就是這個 OnDeactivate**
     （它停掉場景／動畫的更新）——只擋「暫停音效」那條完全沒用（覆蓋率 0%）。

## 這個 patch（v4，2026-09-01 —— 防凍＋不擋手動操作）

第一版（v1）只把「失焦」算出來的布林壓成 1（永遠 active → 狀態沒變化
→ OnActivate 完全不跑）。失焦不凍實測 100%，**但重新聚焦時 OnActivate(1)
也不跑了** —— 輸入層的重新取得（0x70BCF0）沒人叫，DirectInput 裝置在失焦時
被系統放掉之後再也接不回來 → 「右鍵鎖怪→走過去放技能」失靈（走路正常、
放不出招；見 memory 的 manual-cast-broken-investigation）。所以 v2 起一律是
「失焦什麼都不做、取得焦點一定跑 OnActivate(1)」這組語意。

v2/v3 要在 handler 裡改**兩個**位置共 9 個位元組；**v4 只要一處 3 個位元組**
—— 因為官方自己把那段狀態機抽成了共用小函式 `SetActive(bActive)`：

    push ebp / mov ebp,esp / mov edx,[ebp+8]      ; edx = bActive
    cmp byte [ecx+0xC], dl     ← ★ PATCH_ADDR（要改的 3 bytes）
    je  +0xC                   ; 狀態沒變 → 什麼都不做（**這行不動**）
    mov eax,[ecx] / mov [ecx+0xC],dl / mov [ebp+8],edx
    pop ebp / jmp [eax+0x50]   ; ＝ tail-call OnActivate(bActive)
    pop ebp / ret 4

    38 51 0C（cmp byte [ecx+0xC], dl）→ 84 D2 90（test dl,dl / nop）

於是**遊戲自己那行 `je`** 的意思就從「狀態沒變就跳走」變成：

* `bActive == 0`（失焦）→ ZF=1 → 跳走，什麼都不做
  ＝不暫停、不廣播 deactivate ＝ **失焦不凍**（跟 v1 的效果一樣，實測過）。
* `bActive != 0`（取得焦點）→ 往下走，**每次都呼叫 OnActivate(1)** ＝ 遊戲
  原本重新聚焦要做的整套（輸入層重新取得＋恢復音效＋廣播 active）——
  手動操作就是靠這一下復活的。順帶 `[this+0xC]` 照樣被設成 1，狀態欄位
  維持一致（別處有 `cmp byte [this+0xC],0` 的閘門在讀它）。

★ v3 那個「`je` 的位移量是版面、改版要重新數，寫錯＝跳進指令中間＝當場當機」
  的坑，v4 **消失了**：我們沒有動那個 `je`，位移量是遊戲自己的。
⚠ 代價：`SetActive` 有 4 個呼叫點（WM_ACTIVATEAPP、WM_NCLBUTTONDOWN、
  輸入法那兩個）。另外三個都是 `push 1`、而且前面都有「自己是前景視窗」的
  閘門，patch 後它們從「狀態變了才跑」變成「每次都跑 OnActivate(1)」——
  跟取得焦點走的是同一條熟路，又都是使用者手動觸發的低頻訊息。

## 位址怎麼來

`PATCH_ADDR` 由 `locate.warm()` 依 AOB 特徵自動定位（見 locate.py 的
`unfreeze.PATCH_ADDR`；特徵把要改的 3 個位元組**遮成 ??**、拿後面整段函式
骨架當錨，所以 patch 前後都定位得到）。骨架裡的 `88 51 0C`
（mov [ecx+0xC],dl）把 ecx/edx 的配置一起釘住 —— 特徵命中就保證原樣是
`38 51 0C`，還原寫回去一定對。定位失敗會清成 0，這裡每個寫入前都會問
`locate.located()` 並**當場重讀確認內容是預期的樣子**才動手 ——
寫錯位址是直接破壞遊戲記憶體，這條退路一定要有。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from app.game import locate

# locate.warm() 會把這個改寫成當下的絕對位址。沒定位成功前是 2026-09-01 的值，
# 但 apply()/remove() 一律先問 locate.located() 才會用，所以這個預設值只是文件。
PATCH_ADDR = 0x0072FD46

ORIG = b"\x38\x51\x0c"      # cmp byte [ecx+0xC],dl → 「狀態變了沒」
PATCHED = b"\x84\xd2\x90"   # test dl,dl / nop      → 「是不是失焦」

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


def state(scanner) -> str:
    """回傳這一台目前的狀態：

    * ``"on"``      —— 已套用（失焦不凍＋重新聚焦會重設輸入）。
    * ``"off"``     —— 原始狀態（失焦會凍）。
    * ``"unknown"`` —— 還沒定位成功、讀不到、或內容不是我們認得的樣子
                       （可能改版搬家了）。這種狀態一律**不准寫入**。

    ⚠ v4 起只有一處 patch，所以不會再回 ``"mixed"``（v2/v3 兩處時代才有）。
      呼叫端保留那個分支沒關係 —— 它本來就只是「照想要的方向再推一把」。
    """
    if not _located():
        return "unknown"
    a = scanner._read_bytes(PATCH_ADDR, len(ORIG))
    if not a or len(a) < len(ORIG):
        return "unknown"
    if a == PATCHED:
        return "on"
    if a == ORIG:
        return "off"
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

    冪等：已套的跳過、只有原樣（ORIG）才寫。
    """
    return _write(scanner, PATCH_ADDR, PATCHED, lambda b: b == ORIG)


def remove(scanner) -> bool:
    """還原成原始（失焦會凍）。成功或本來就已還原回 True。

    ★ 還原寫回的 `38 51 0C` 裡沒有任何位移量，而且特徵骨架的 `88 51 0C`
      把 ecx/edx 的配置釘住了 —— 只要定位得到，這 3 個位元組一定是對的
      （v3 那個「還原要重新數 je 位移」的坑在 v4 不存在，見檔頭）。
    """
    return _write(scanner, PATCH_ADDR, ORIG, lambda b: b == PATCHED)
