"""底層視窗控制核心模組（Windows 專用）。

提供三件全背景自動化的地基能力，全部不碰使用者真正的滑鼠鍵盤：
1. enumerate_windows()  ：列舉所有可見的最上層視窗（含 HWND / PID / 標題 / 類別）。
2. capture_window()     ：以 PrintWindow 擷取「指定視窗自己」的畫面（背景/被遮住也能抓）。
3. send_text() / send_key() ：以 PostMessage 把按鍵直接送進指定視窗（不搶焦點）。

注意（重要）：
- 對使用 DirectInput / 原始輸入的遊戲，PostMessage 的模擬按鍵可能被忽略。
- 對 DirectX 硬體加速渲染的視窗，PrintWindow 擷取結果可能是全黑。
  這兩點是否可用，必須用實際遊戲測試，程式無法事先保證。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import windll, wintypes
from dataclasses import dataclass

import win32con
import win32gui
import win32process
import win32ui
from PIL import Image

# 訊息常數
WM_CHAR = 0x0102
WM_KEYDOWN = win32con.WM_KEYDOWN
WM_KEYUP = win32con.WM_KEYUP

# PrintWindow 旗標：2 = PW_RENDERFULLCONTENT（對部分 DirectX 視窗較有機會抓到內容）
PW_RENDERFULLCONTENT = 0x00000002

# ⚠⚠ SendMessageTimeoutW 最後一個參數是 PDWORD_PTR —— 64 位元 Python 上要給
#   8 bytes 的緩衝。以前沒宣告型別、拿 4 bytes 的 c_ulong 去接，等於**每送一次鍵
#   就越界寫 4 bytes**（被 CPython 的 8-byte 對齊剛好蓋住才沒炸）。
windll.user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
windll.user32.SendMessageTimeoutW.restype = wintypes.LPARAM
windll.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
windll.user32.MapVirtualKeyW.restype = wintypes.UINT
# ⚠ HDC 在 x64 是 8-byte 控制代碼：不宣告 argtypes 會被 ctypes 當 32-bit int 推，
#   GetSafeHdc() 一旦超過 2^31 就 ArgumentError（或截斷成別的 DC → 抓到全黑圖，
#   而全黑剛好被 is_mostly_black() 判成「不支援擷取」，故障會被靜默吞掉）。
windll.user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
windll.user32.PrintWindow.restype = wintypes.BOOL


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int]  # (left, top, right, bottom)

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


def enumerate_windows(
    title_contains: str | None = None,
    visible_only: bool = True,
) -> list[WindowInfo]:
    """列舉最上層視窗。可用 title_contains 過濾標題（不分大小寫）。"""
    results: list[WindowInfo] = []

    def _callback(hwnd: int, _extra) -> bool:
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if title_contains and title_contains.lower() not in title.lower():
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            class_name = win32gui.GetClassName(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        results.append(
            WindowInfo(hwnd=hwnd, pid=pid, title=title, class_name=class_name, rect=rect)
        )
        return True

    win32gui.EnumWindows(_callback, None)
    return results


def crash_dialogs(pids: set[int]) -> list[WindowInfo]:
    """這些行程有沒有跳出系統對話框（class #32770，例如崩潰錯誤警告）。

    遊戲的介面全是 DirectX 自己畫的，正常運作時遊戲行程**不會**有第二個原生
    最上層視窗；一旦冒出 #32770，幾乎可以確定是崩潰處理器彈的錯誤視窗 ——
    那時遊戲卡死、TCP 也還被握著，靠連線與視窗標題都偵測不到（實際發生過：
    彈著錯誤警告掛整晚，監控一聲不吭）。

    ⚠ 不走 enumerate_windows()：那支會把沒有標題的視窗全部略過，
      而錯誤視窗的標題長什麼樣（甚至有沒有）我們沒有實錄，不能賭。
    """
    results: list[WindowInfo] = []
    if not pids:
        return results

    def _callback(hwnd: int, _extra) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in pids:
                return True
            if win32gui.GetClassName(hwnd) != "#32770":
                return True
            results.append(WindowInfo(
                hwnd=hwnd, pid=pid, title=win32gui.GetWindowText(hwnd),
                class_name="#32770", rect=win32gui.GetWindowRect(hwnd)))
        except Exception:                                  # noqa: BLE001
            pass
        return True

    win32gui.EnumWindows(_callback, None)
    return results


def get_window_title(hwnd: int) -> str:
    """即時讀取指定視窗的目前標題；失敗 / 視窗已關回傳空字串。

    標題會隨遊戲狀態改變（例如切換頻道），所以要拿最新頻道就得重讀，
    不能只用開始監控當下抓到的舊標題。"""
    try:
        return win32gui.GetWindowText(hwnd)
    except Exception:
        return ""


def enumerate_child_windows(parent_hwnd: int) -> list[WindowInfo]:
    """列舉指定視窗底下的所有子孫視窗。

    很多程式（瀏覽器、遊戲）真正接收鍵盤輸入的是子視窗而非最上層視窗，
    要對正確的子視窗送 WM_CHAR 才會有反應。標題可能為空，改用類別名辨識。
    """
    results: list[WindowInfo] = []

    def _callback(hwnd: int, _extra) -> bool:
        try:
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            class_name = win32gui.GetClassName(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        results.append(
            WindowInfo(hwnd=hwnd, pid=pid, title=title, class_name=class_name, rect=rect)
        )
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, _callback, None)
    except Exception:
        pass
    return results


def capture_window(hwnd: int) -> Image.Image | None:
    """以 PrintWindow 擷取指定視窗畫面，回傳 PIL Image；失敗回傳 None。

    若回傳的圖幾乎全黑，代表該視窗（多半是 DirectX）不支援這種擷取。
    """
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        windll.user32.PrintWindow(
            hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT
        )

        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        )
        return img  # 即使 PrintWindow 回 0 也回傳，讓呼叫端自行判斷是否全黑
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def is_mostly_black(img: Image.Image, threshold: int = 8, ratio: float = 0.98) -> bool:
    """判斷擷取結果是否幾乎全黑（用來偵測 PrintWindow 對此視窗無效）。"""
    small = img.convert("L").resize((64, 64))
    pixels = list(small.getdata())
    dark = sum(1 for p in pixels if p <= threshold)
    return dark / len(pixels) >= ratio


def send_text(hwnd: int, text: str) -> None:
    """用 WM_CHAR 把文字逐字送進視窗（背景，不搶焦點）。"""
    for ch in text:
        win32gui.PostMessage(hwnd, WM_CHAR, ord(ch), 0)


def key_lparam(vk_code: int) -> tuple[int, int]:
    """算出 WM_KEYDOWN / WM_KEYUP 該帶的 lParam。

    lParam 的格式：低 16 位 = 重複次數、16~23 位 = **掃描碼**、
    30 位 = 前一次的鍵位狀態、31 位 = 放開旗標。

    ⚠ 傳 0 會讓不少遊戲直接忽略這則訊息（重複次數 0、掃描碼 0 = 無效輸入）。
    實測本專案的遊戲就是這樣：lParam=0 的 F2 完全沒反應，
    帶上掃描碼（F2 = 0x3C）之後才生效。
    """
    scan = windll.user32.MapVirtualKeyW(vk_code, 0)     # MAPVK_VK_TO_VSC
    down = 0x00000001 | (scan << 16)
    up = 0xC0000001 | (scan << 16)
    return down, up


SMTO_ABORTIFHUNG = 0x0002
SEND_TIMEOUT_MS = 200
# ★★★ 按下到放開之間要隔多久。**這是背景送鍵到底有沒有用的關鍵**，
#   比 lParam、比 SendMessage/PostMessage 都重要得多。
#   遊戲是**每幀取樣鍵盤狀態**的，同一幀內按下又放開會整個漏掉。
#   實測（清空技能欄位→送一次鍵→看有沒有寫入，每種 15 次，兩隻角色一致）：
#       按住 0ms →   0%      按住 30ms → 100%
#       按住 10ms →  60%     按住 40ms → 100%
#       按住 20ms →  93%     按住 60ms → 100%
#   門檻在 30ms，取 40ms 留餘裕。
KEY_HOLD_MS = 40


def send_key(hwnd: int, vk_code: int, timeout_ms: int = SEND_TIMEOUT_MS,
             hold_ms: int = KEY_HOLD_MS) -> bool:
    """送出一次完整的按鍵（KEYDOWN → 按住 hold_ms → KEYUP）。回傳是否送達。

    ⚠⚠ **中間一定要隔 hold_ms**（見 KEY_HOLD_MS 的實測數字）。
    以前是按下立刻放開，成功率只有 **26.7%** —— 四次有三次白按。
    「偶爾放不出技能」「學技能 ID 要試好幾次」「近戰有時不出手」都是這個。

    ⚠ 要連續施法就是**重複呼叫這個**，不要改成「只送 KEYDOWN 模擬按住」。
    使用者實測：這個遊戲**按住不放不會一直放技能**，必須一次次按放。
    （實測只送 KEYDOWN 成功率 6.7%，確實不行。）

    ⚠ 舊註解寫「這個遊戲只吃 SendMessage、PostMessage 完全沒反應」——
    **那是錯的**，真正的原因是當時沒有按住：加上 40ms 之後
    PostMessage 一樣 15/15 全中。這裡沿用 SendMessageTimeout 是因為它
    在本專案已經跑很久、行為已知，不是因為 PostMessage 不能用。

    lParam 必須帶掃描碼（見 key_lparam），傳 0 會被忽略。

    SendMessage 會**卡住呼叫端直到對方處理完**，所以走 SendMessageTimeout：
    設定逾時並加上 SMTO_ABORTIFHUNG，對方沒回應就放棄，不會拖住自己。

    全程不碰使用者真正的鍵盤、不搶焦點，可以同時操作多個視窗。
    """
    down, up = key_lparam(vk_code)
    res = ctypes.c_size_t()          # DWORD_PTR：x64 上是 8 bytes，不能用 c_ulong
    ok = bool(windll.user32.SendMessageTimeoutW(
        hwnd, WM_KEYDOWN, vk_code, down, SMTO_ABORTIFHUNG, timeout_ms,
        ctypes.byref(res)))
    if hold_ms:
        time.sleep(hold_ms / 1000)
    ok = bool(windll.user32.SendMessageTimeoutW(
        hwnd, WM_KEYUP, vk_code, up, SMTO_ABORTIFHUNG, timeout_ms,
        ctypes.byref(res))) and ok
    return ok


# 常用虛擬鍵碼
VK_TAB = win32con.VK_TAB
VK_RETURN = win32con.VK_RETURN
