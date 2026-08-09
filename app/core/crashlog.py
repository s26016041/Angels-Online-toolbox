"""崩潰／異常紀錄檔的統一入口（`%APPDATA%\\AngelsOnlineToolbox\\crash.log`）。

為什麼要獨立一支：打包成 --windowed 的 exe 沒有主控台，`sys.stderr` 是 None，
所有「印出來看」的東西都會消失，**紀錄檔是唯一的現場**。原本只有 main.py 的
全域例外攔截會寫，但那條路等同「程式即將關閉」；分頁裡「只停一台分身、其他
繼續跑」的異常也要留下完整 traceback，才不會變成安靜地少做事。

⚠ 這支只依賴標準函式庫，任何地方都能 import（含啟動最早期）。
"""
from __future__ import annotations

import os
import traceback
from datetime import datetime
from pathlib import Path

APP_DIR_NAME = "AngelsOnlineToolbox"


def log_dir() -> Path:
    """紀錄檔資料夾放使用者 AppData，打包成 exe 後也寫得進去。"""
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), ".config")
    path = Path(base) / APP_DIR_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(".")
    return path


def record(title: str, exc: BaseException | None = None) -> Path | None:
    """把一則異常寫進 crash.log；回傳紀錄檔路徑（寫不進去回 None）。

    title: 一行說明（會跟時間戳寫在同一行），例如「掛機分頁 心跳例外」。
    exc:   有的話連 traceback 一起寫。
    """
    text = f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {title} =====\n"
    if exc is not None:
        text += "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))
    try:
        path = log_dir() / "crash.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)
        return path
    except OSError:
        return None
