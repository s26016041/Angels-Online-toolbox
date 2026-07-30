"""資源檔路徑：開發時相對專案根目錄，打包成 exe 後在 PyInstaller 的解壓目錄。

所有資源集中在專案根目錄的 assets/（整包在 AngelsOnlineToolbox.spec 的 datas 裡
列出來，否則打包後會找不到）：
    assets/icon.ico          程式圖示（exe、工作列、視窗左上）
    assets/icon.png          圖示的來源圖，改圖時重新產生 .ico 用
    assets/fonts/*.ttf       介面字體
    assets/music/*.mp3       警報聲
"""
from __future__ import annotations

import sys
from pathlib import Path


def resource(rel: str) -> Path:
    """取得資源檔的實際路徑。rel 是相對專案根目錄的路徑，例如 "fonts/X.ttf"。"""
    if hasattr(sys, "_MEIPASS"):          # PyInstaller 打包後的解壓目錄
        return Path(sys._MEIPASS) / rel
    return Path(__file__).resolve().parents[1] / rel
