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
from ctypes import windll
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

        result = windll.user32.PrintWindow(
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
        return img if result else img  # 即使 result=0 也回傳，讓呼叫端自行判斷是否全黑
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


def send_key(hwnd: int, vk_code: int) -> None:
    """送出一次完整的按鍵（KEYDOWN + KEYUP）。"""
    win32gui.PostMessage(hwnd, WM_KEYDOWN, vk_code, 0)
    win32gui.PostMessage(hwnd, WM_KEYUP, vk_code, 0)


# 常用虛擬鍵碼
VK_TAB = win32con.VK_TAB
VK_RETURN = win32con.VK_RETURN
