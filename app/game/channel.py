"""切換分流（頻道）：呼叫遊戲自己的送包函式，一個整數搞定。

    channel.switch(mover, 5)      # 換到雅典娜-5

原理
----
分流不是「用封包指定」也不是「客戶端自己記」，而是送一個**泛用封包**給伺服器：

    0x5D3D97(0x0C, 目標實體ID)    ← 選定怪物（attack.select 一直在用的）
    0x5D3D97(0x47, 分流編號)      ← 切換分流   ★ 就差一個種類碼

`0x5D3D97` 就是 `attack.SELECT_FN`。cdecl、兩個整數、**沒有 this** ——
所以這件事不需要任何新機制，也沒有「拿猜的 this 去呼叫」的崩潰風險：
我們每次打怪都在叫同一個函式。

⚠ 參數是 **0 起算**：`0` = 雅典娜-1、`4` = 雅典娜-5。本檔的 `switch()` 收的是
**人看的編號（1 起算）**，內部才減一 —— 呼叫端不要自己減。

送出之後客戶端會自己斷線、重連到那個分流的伺服器（實測約 1 秒），整套加解密
與重連登入握手都是它自己做，我們完全不用碰。

## 怎麼找到的（2026-08-04）

使用者提供三份「換分流」的封包擷取。關鍵不在封包內容（**內文是加密的**：
程式碼每次推同樣的常數，三份的密文卻完全不同），而在**呼叫鏈上的參數**：

    A 換到雅典娜-1   0x591B44　參數 (0x47, 0, …)
    B 換到雅典娜-2   0x591B44　參數 (0x47, 1, …)
    C 換到雅典娜-?   0x591B44　參數 (0x47, 2, …)

反組譯 `0x591B3F` 那道 `call` → 打到 `0x5D3D97`。

⛔ 繞過的死路（別再走）：
  · 「重送同樣的封包、改一個位元組」—— 那包（`0x5C05AE` 送的 `0x143`）的內文
    **只有 2 bytes、就是代號本身**，根本沒有頻道欄位；而且內文加密、每包換金鑰。
  · `SYSTEM_CUR_CHANNEL` 這個具名 UI 變數 —— 五台實測**全部讀到 1**，
    不管實際在第幾頻。它是存檔用的設定，不是即時值。
  · 控制項 `0x1A6`~`0x1AA` —— 不是頻道按鈕，是依分流分別儲存的五個核取方塊。

## 實測（黑狐）

    0x5D3D97(0x47, 4) → 1 秒後 雅典娜-2 → 雅典娜-5
                        連線也換了：20.205.19.179:18304 → 20.2.232.142:18605
    0x5D3D97(0x47, 1) → 1 秒後 換回 雅典娜-2
沒有崩潰、沒有掉線視窗。
"""
from __future__ import annotations

import ctypes
import re

from app.game import attack

# 泛用送包函式的「切換分流」種類碼。函式本身沿用 attack.SELECT_FN。
SWITCH_CODE = 0x47

# 遊戲共 5 個分流（使用者確認）。超出範圍不送 —— 沒驗過的值不要亂送給伺服器。
MIN_CHANNEL = 1
MAX_CHANNEL = 5


def switch(mover, channel: int) -> bool:
    """換到指定分流（**1 起算**，1~5）。排不進指令槽或編號超範圍時回 False。

    mover: 已 start() 的 move.Mover（借它的跳板讓遊戲主執行緒替我們呼叫）。
    """
    if not (MIN_CHANNEL <= channel <= MAX_CHANNEL):
        return False
    return attack._send(
        mover, ((attack.SELECT_FN, (SWITCH_CODE, channel - 1)),))


_TITLE_RE = re.compile(r"\(([^()]*?)-(\d+)\)\s*$")


def current(hwnd: int) -> int | None:
    """從視窗標題讀目前分流（1 起算）；讀不到回 None。

    標題長這樣：`Angels Online Global - fred26011034(雅典娜-2)`。
    ⚠ 這是目前**唯一**可靠的來源 —— 記憶體裡的 `SYSTEM_CUR_CHANNEL` 是存檔用的
      設定，五台實測全是 1，不能拿來當即時值（見檔頭）。
    """
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
    m = _TITLE_RE.search(buf.value)
    return int(m.group(2)) if m else None


def server_name(hwnd: int) -> str | None:
    """標題裡的伺服器名（例如「雅典娜」）；讀不到回 None。"""
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
    m = _TITLE_RE.search(buf.value)
    return m.group(1) if m else None
