"""失焦不凍畫面：patch 遊戲的視窗程序，讓它失焦時不停止畫面更新。

## 根因（完整調查見 memory 的 background-unfreeze-investigation）

天使 Online 的視窗**失焦（不是最小化）時畫面會凍住，但邏輯／網路照跑**。
反組譯出來的機制（angel.dat 無 ASLR，基底 0x400000）：

* 視窗程序 `WndProc = 0x70CAB0` 收到 `WM_ACTIVATE(0x06)` / `WM_ACTIVATEAPP(0x1C)`
  時，會算出「現在是不是 active」這個布林，存進視窗物件 `[this+0xC]`；
  **只有狀態真的變了**才去呼叫 `OnActivate([vtable+0x50] = 0x70C480)`。
* `OnActivate` 的**失焦分支**做兩件事：暫停繪圖/音效子系統，以及呼叫廣播
  `0x705F60` 把「停用」通知每一個 listener（`[vtable+0x20]`）。
  **實測真正凍住畫面的是那個廣播的 OnDeactivate**（它停掉場景／動畫的更新）——
  只擋「暫停子系統」那條完全沒用（覆蓋率 0%），擋掉整個 OnActivate 才有效
  （失焦時畫面資料的更新覆蓋率 100%）。

## 這個 patch

與其一個一個去攔 listener，直接治本：讓 handler 算出來的 active 布林**永遠是 1**。
handler 裡那句把它設成 0（失焦）的指令是 `xor al,al`（機器碼 `32 C0`），
改成 `mov al,1`（`B0 01`）。這樣失焦時算出來的 al 還是 1，跟 `[this+0xC]` 裡
本來就是 1 的舊值相同 → **狀態沒變化 → 根本不呼叫 OnActivate** → 暫停與廣播
都不會跑，畫面就一直更新。取得焦點那條路線一字未動（本來就設 1）。

* 只有 **2 個位元組**：`32 C0` → `B0 01`；取消＝還原成 `32 C0`。
* 純粹讓遊戲**少做一件事**（少發一次「我失焦了」的通知），不新增任何行為、
  不注入程式碼、不搶前景。實測在正在掛機的分身上套用／還原都正常、不崩。

## 位址怎麼來

`PATCH_ADDR` 由 `locate.warm()` 依 AOB 特徵自動定位（見 locate.py 的
`unfreeze.PATCH_ADDR` 那段；特徵**把要改的那 2 個位元組遮成 `??`**，所以
patch 前、patch 後都定位得到）。定位失敗會清成 0，這裡的每個寫入前都會問
`locate.located()` 並**當場重讀確認那 2 個位元組是預期的值**才動手 ——
寫錯位址是直接破壞遊戲記憶體，這條退路一定要有。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from app.game import locate

# locate.warm() 會把這個改寫成當下的絕對位址。沒定位成功前是 2026-08-12 的值，
# 但 apply()/remove() 一律先問 locate.located() 才會用，所以這個預設值只是文件。
PATCH_ADDR = 0x0070CC5A

ORIG = b"\x32\xc0"      # xor al,al   → 算出「失焦＝inactive」
PATCHED = b"\xb0\x01"   # mov al,1    → 永遠 active

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

    * ``"on"``      —— 已套用，失焦不會凍畫面（那 2 個位元組是 `B0 01`）。
    * ``"off"``     —— 原始狀態，失焦會凍（`32 C0`）。
    * ``"unknown"`` —— 還沒定位成功、讀不到、或那個位址的內容不是我們認得的
                       兩種之一（可能改版搬家了）。這種狀態一律**不准寫入**。
    """
    if not _located():
        return "unknown"
    raw = scanner._read_bytes(PATCH_ADDR, 2)
    if raw == PATCHED:
        return "on"
    if raw == ORIG:
        return "off"
    return "unknown"


def _write(scanner, data: bytes, expect_before: bytes) -> bool:
    """把 `data` 寫進 PATCH_ADDR，但**寫之前當場重讀**，只有內容正好是
    `expect_before` 才動手；寫完再讀回來確認。任何一關過不了就不寫／回 False。
    """
    if not _located():
        return False
    if not getattr(scanner, "can_write", False):
        return False
    handle = scanner._handle
    if not handle:
        return False
    # ★ 當場重讀重驗（見 CLAUDE.md「交給遊戲的位址送出前當場重讀重驗」）。
    now = scanner._read_bytes(PATCH_ADDR, 2)
    if now == data:
        return True                          # 已經是想要的樣子了，不必再寫
    if now != expect_before:
        return False                         # 不是預期的原樣 → 抓錯位址，拒寫
    old = wintypes.DWORD()
    if not _k32.VirtualProtectEx(handle, ctypes.c_void_p(PATCH_ADDR), 2,
                                 PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
        return False
    written = ctypes.c_size_t(0)
    ok = _k32.WriteProcessMemory(handle, ctypes.c_void_p(PATCH_ADDR),
                                 data, 2, ctypes.byref(written))
    # 還原頁面保護（VirtualProtectEx 回來的 old 才是原本的值）。
    tmp = wintypes.DWORD()
    _k32.VirtualProtectEx(handle, ctypes.c_void_p(PATCH_ADDR), 2,
                          old.value, ctypes.byref(tmp))
    if not ok or written.value != 2:
        return False
    return scanner._read_bytes(PATCH_ADDR, 2) == data


def apply(scanner) -> bool:
    """套用（失焦不凍）。成功或本來就已套用回 True。"""
    return _write(scanner, PATCHED, ORIG)


def remove(scanner) -> bool:
    """還原成原始（失焦會凍）。成功或本來就已還原回 True。"""
    return _write(scanner, ORIG, PATCHED)
