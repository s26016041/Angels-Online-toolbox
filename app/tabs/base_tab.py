"""分頁基底類別。

所有工具分頁都繼承 BaseTab。主視窗靠這個共同介面來自動載入分頁：
每個分頁只要設定 TAB_TITLE（分頁標題）與 ORDER（排序），
並實作 build_ui() 建立自己的介面即可。
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget


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
