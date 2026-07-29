"""資源檔路徑：開發時相對專案根目錄，打包成 exe 後在 PyInstaller 的解壓目錄。

專案根目錄下的資源資料夾（都要在 AngelsOnlineToolbox.spec 的 datas 裡列出來，
否則打包後會找不到）：
    music/  警報聲 mp3
    fonts/  介面字體
"""
from __future__ import annotations

import sys
from pathlib import Path


def resource(rel: str) -> Path:
    """取得資源檔的實際路徑。rel 是相對專案根目錄的路徑，例如 "fonts/X.ttf"。"""
    if hasattr(sys, "_MEIPASS"):          # PyInstaller 打包後的解壓目錄
        return Path(sys._MEIPASS) / rel
    return Path(__file__).resolve().parents[1] / rel
