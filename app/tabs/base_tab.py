"""分頁基底類別。

所有工具分頁都繼承 BaseTab。主視窗靠這個共同介面來自動載入分頁：
每個分頁只要設定 TAB_TITLE（分頁標題）與 ORDER（排序），
並實作 build_ui() 建立自己的介面即可。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget


def fit_spin(spin) -> None:
    """把數字框調成「**最大的那個值**也放得下」的寬度。

    ★ 使用者要求所有文字都要完整顯示。原本各分頁一律寫死 60~70px，
      實測不夠 —— 上下箭頭會把數字擠掉（使用者回報「框框被砍到一半」）。
    ⚠ 不能照**目前的值**算：3600 比 60 寬，用 60 算出來的寬度換到 3600 就切字。
    ⚠ 一定要在**主題套用之後**呼叫，不然量到的是沒有內距的尺寸。
    """
    keep = spin.value()
    spin.setValue(spin.maximum())
    need = spin.sizeHint().width()
    spin.setValue(keep)
    spin.setFixedWidth(need)


def no_elide(lst) -> None:
    """清單不要把字縮成「曼陀羅怪…」，太長就給水平捲軸讓人捲。"""
    lst.setTextElideMode(Qt.ElideNone)
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)


def fit_list(box, lst, sample: str) -> None:
    """讓清單至少放得下 `sample` 這麼長的一行，寬度用**實際字型**量。

    ★ 原本欄寬是寫死的（190/240），換字型或遇到長名字就會切字。
    """
    pad = (lst.frameWidth() * 2 + 26                      # 邊框 + 內距
           + lst.verticalScrollBar().sizeHint().width())  # 直捲軸佔的位置
    box.setMinimumWidth(lst.fontMetrics().horizontalAdvance(sample) + pad)


class BaseTab(QWidget):
    """所有功能分頁的基底類別。

    子類別需要覆寫：
        TAB_TITLE：顯示在分頁上的標題文字。
        ORDER：分頁排序（數字越小越靠左），可選。
        build_ui()：建立此分頁的介面內容。
    """

    TAB_TITLE: str = "未命名分頁"
    ORDER: int = 100
    # 設為 False 可讓自動載入器略過此分頁（例如尚未完成的功能）。
    ENABLED: bool = True

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.build_ui()

    def build_ui(self) -> None:
        """建立分頁介面。子類別必須覆寫。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} 必須實作 build_ui() 方法"
        )

    def on_show(self) -> None:
        """分頁被切換到（顯示）時呼叫，可選覆寫。"""

    def on_close(self) -> None:
        """應用程式關閉前呼叫，用於釋放資源，可選覆寫。"""
