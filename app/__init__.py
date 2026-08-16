"""天使之戀工具箱應用程式套件。

版本號的唯一來源是專案根目錄的 VERSION 檔（發布工具 release.py 也讀它）。
打包成 exe 時 VERSION 會一併被 PyInstaller 收進去（--add-data），這裡照樣讀得到。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _read_version() -> str:
    candidates = []
    if hasattr(sys, "_MEIPASS"):  # PyInstaller 打包後的解壓目錄
        candidates.append(Path(sys._MEIPASS) / "VERSION")
    candidates.append(Path(__file__).resolve().parent.parent / "VERSION")  # 開發時：專案根目錄
    for p in candidates:
        try:
            # utf-8-sig：VERSION 若被帶 BOM 的編輯器/指令碰過也照樣乾淨。
            # 2026-08-16 實踩：PowerShell 5.1 的 Set-Content -Encoding utf8 會加
            # BOM，而 strip() 不會去掉 U+FEFF → Release tag 變成 v﻿0.4.32。
            return p.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
    return "0.0.0"


__version__ = _read_version()
__app_name__ = "天使之戀工具箱"
