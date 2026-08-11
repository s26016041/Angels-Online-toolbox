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
import struct
from ctypes import wintypes

from app.game import attack

# ⚠ HWND 在 x64 是 8-byte：不宣告 argtypes 會被 ctypes 當 32-bit int 推。
#   目前沒炸只是因為 Windows 保證視窗控制代碼有效位在 32 位內（injector.py 檔頭
#   有同一段警語），不是設計如此 —— 這裡把型別補齊。
ctypes.windll.user32.GetWindowTextW.argtypes = [
    wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
ctypes.windll.user32.GetWindowTextW.restype = ctypes.c_int

# 泛用送包函式的「切換分流」種類碼。函式本身沿用 attack.SELECT_FN。
SWITCH_CODE = 0x47

MIN_CHANNEL = 1
# ⚠ 只在**真的讀不到 server.xml** 時才用（見 count()）。不要拿它當常數用 ——
#   分流數是遊戲隨時會調的東西，寫死就會在改版後安靜地壞掉。
FALLBACK_MAX = 5


def switch(mover, channel: int, max_channel: int = FALLBACK_MAX) -> bool:
    """換到指定分流（**1 起算**）。排不進指令槽或編號超範圍時回 False。

    mover:       已 start() 的 move.Mover（借它的跳板讓遊戲主執行緒替我們呼叫）
    max_channel: 這個伺服器有幾個分流，用 `count()` 拿。超範圍一律不送 ——
                 沒驗過的值不要丟給伺服器。
    """
    if not (MIN_CHANNEL <= channel <= max_channel):
        return False
    return attack._send(
        mover, ((attack.SELECT_FN, (SWITCH_CODE, channel - 1)),))


# --- 記憶體裡的伺服器清單 ---------------------------------------------------
# 客戶端啟動時把伺服器清單解析進記憶體，是一個等距陣列（相鄰記錄差 0x178），
# 記錄開頭就是伺服器名字串。用視窗標題的伺服器名去找那筆，就能讀到分流數。
#
# 版面（相對記錄起點＝名稱字串）：
#     +0x00  名稱（UTF-8，內嵌）        +0x40  ip 字串（內嵌）
#     +0x50  port      +0x54  分流?     +0x58  編號      +0x5C  分流?
#     +0x60  fip 字串  +0x70  fport     +0x74  tip 字串
#
# ⚠ +0x54 與 +0x5C **兩格都是 5**，三台伺服器都一樣，所以分不出哪一格才是
#   「分流」。因此 `count()` 兩格都讀、**要求兩格一致**才採用 ——
#   哪天版面變了或兩格意義分岔，我們會得到 None（不換頻道），而不是一個錯的數字。
#
# ⚠⚠ **版面不要在這裡再寫一份** —— `app/game/login.py` 的 `SRV_*` 是同一個
#   結構（那邊還有 AOB 錨：遊戲自己的 `imul esi,[伺服器索引],0x178` /
#   `cmp [edx+esi+0x5C],0` 一次給出 stride 與分流欄）。以前這裡有自己的
#   OFF_IP/OFF_PORT/… 五個常數，等於同一個版面兩份 —— 改版時 login 那份會
#   被人維護、這份不會，而且**不會有任何警告**。現在直接借用。
from app.game.login import (SRV_ID as OFF_ID,               # noqa: E402
                            SRV_NAME as _SRV_NAME,          # noqa: F401
                            SRV_PORT as OFF_PORT,
                            SRV_SUBSET_A as OFF_SUBSET_A,
                            SRV_SUBSET_B as OFF_SUBSET_B)

OFF_IP = 0x40             # login.py 沒有用到這欄（那邊靠索引直接取記錄）
REC_SPAN = 0x60           # 讀到 +0x5C 就夠了
MAX_SUBSET = 32           # 合理性上限：分流數不可能比這大


def count(scanner, hwnd: int, should_stop=None) -> int | None:
    """這個伺服器有幾個分流；**讀不到就回 None**（呼叫端應該乾脆不要換頻道）。

    ★ 純讀記憶體，不碰任何檔案。改版增減分流會自動跟上，因為讀的是客戶端
      自己解析出來的那份清單。

    ⚠ 這是**全記憶體掃描**（一台約 0.3～1 秒），不要放在每一拍會跑到的地方。
    should_stop: 可選 callable，每個記憶體區塊前問一次；回傳 True 就中止回 None。

    ⛔ 這兩條走不通，別再試（五台實測）：
       · `SYSTEM_CHANNEL_SIZE` 具名變數 —— 讀到垃圾值
       · `SYSTEM_CUR_CHANNEL` 具名變數 —— 五台全是 1，不管實際在第幾頻
    """
    name = server_name(hwnd)
    if not name:
        return None
    # ★ 先走 login.servers()：它從**應用程式主物件的伺服器陣列**直接讀
    #   （`[APP_PTR]+0x500` 那條，位址有 AOB 蓋著），毫秒級。
    #   下面那條全掃是退路 —— 兩條讀的是同一份清單，2026-08-11 五台實測
    #   結果一致（都 5），速度 0.0~1.1ms vs 35~58ms（memory 記過最壞 0.3~1 秒）。
    #   ⚠ 在函式內 import：`login` 的模組常數要等 locate.warm() 改寫過才是新值。
    try:
        from app.game import login

        got = dict(login.servers(scanner)).get(name)
        if got and 1 <= got <= MAX_SUBSET:
            return got
    except Exception:                                      # noqa: BLE001
        pass                       # 讀不到就走下面的全掃，不要因此整個功能停用
    pat = name.encode("utf-8") + b"\x00"
    for base, size in scanner._iter_regions(writable_only=True):
        if should_stop is not None and should_stop():
            return None
        if base + size > 0xFFFFFFFF:
            continue
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        raw = bytes(raw)
        i = raw.find(pat)
        while i >= 0:
            got = _read_record(raw, i)
            if got is not None:
                return got
            i = raw.find(pat, i + 1)
    return None


def _read_record(raw: bytes, i: int) -> int | None:
    """從候選記錄起點讀分流數；不像伺服器記錄就回 None。

    合理性條件全部是**結構性的**（不看特定數值），所以換伺服器、改分流數
    都照樣成立：port 要像 port、編號要小、兩格分流要一致且合理。
    """
    if i + REC_SPAN > len(raw):
        return None
    port, a, sid, b = struct.unpack_from("<IIII", raw, i + OFF_PORT)
    if not (1024 <= port <= 65535):
        return None
    if not (1 <= sid <= 99):
        return None
    if a != b or not (1 <= a <= MAX_SUBSET):
        return None
    # ip 欄位要是像 IPv4 的字串，才不會撞到剛好長得像的資料
    ip = raw[i + OFF_IP:i + OFF_IP + 16].split(b"\x00")[0]
    if not re.fullmatch(rb"\d{1,3}(\.\d{1,3}){3}", ip):
        return None
    return a


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
