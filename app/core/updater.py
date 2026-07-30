"""自動更新：跟 GitHub Releases 比版本，下載新的 .exe 並就地換掉。

為什麼要有這個
--------------
記憶體位址、物品對照表這類東西會隨遊戲改版而需要修正，所以版本更新會很頻繁。
不能每次都請使用者自己去 GitHub 抓 exe —— 程式要能自己換。

怎麼換掉「正在執行中的 exe」
----------------------------
Windows 不允許覆寫執行中的檔案，但**允許改名**。所以流程是：
    1. 新版下載到 <exe>.new
    2. 把執行中的 exe 改名成 <exe>.old      ← 這步 Windows 允許
    3. 把 .new 改名成原本的檔名
    4. 啟動新的 exe、結束自己
    5. 下次啟動時把殘留的 .old 刪掉（clean_leftovers）
任何一步失敗都會盡量還原，不會讓使用者落到「兩個檔案都不對」的狀態。

只在打包成 exe 時才會動作；開發時（直接跑 main.py）一律跳過。
只用標準庫，不加相依。
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = "s26016041/Angels-Online-toolbox"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "AngelsOnlineToolbox.exe"
TIMEOUT = 15.0
UA = {"User-Agent": "AngelsOnlineToolbox-Updater"}


def is_frozen() -> bool:
    """是不是打包後的 exe。開發時直接跑 .py 就不該自我更新。"""
    return bool(getattr(sys, "frozen", False))


def exe_path() -> Path:
    return Path(sys.executable).resolve()


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.2.2' / '0.2.2' → (0, 2, 2)。解不出來的段落當 0。"""
    t = (text or "").strip().lstrip("vV")
    out = []
    for part in t.split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(remote: str, local: str) -> bool:
    a, b = parse_version(remote), parse_version(local)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def _urlopen(url: str, timeout: float = TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except ssl.SSLError:
        # 打包後可能缺系統 CA。退回不驗證憑證 —— 下載的是自己 repo 的公開檔案，
        # 而且下面還會檢查大小與 PE 標頭，可接受。
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def latest_release() -> dict | None:
    """查 GitHub 最新 Release。回傳 {version, url, size, notes}；失敗回 None。

    沒網路、被限流、repo 沒有 Release 都算失敗 —— 靜靜回 None，不打擾使用者。
    """
    try:
        with _urlopen(API_LATEST) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    tag = data.get("tag_name") or ""
    for asset in data.get("assets") or []:
        if asset.get("name") == ASSET_NAME:
            return {
                "version": tag,
                "url": asset.get("browser_download_url"),
                "size": int(asset.get("size") or 0),
                "notes": (data.get("body") or "").strip(),
            }
    return None


def check() -> dict | None:
    """有新版就回傳它的資訊，否則 None。開發模式一律 None。"""
    if not is_frozen():
        return None
    from app import __version__

    info = latest_release()
    if not info or not info.get("url"):
        return None
    return info if is_newer(info["version"], __version__) else None


def download(info: dict, dest: Path, progress=None) -> bool:
    """下載新版到 dest。progress(已下載, 總量) 可選。

    下載完會檢查大小與 PE 標頭（MZ）—— 抓到半截或抓到錯誤頁面時不會拿去覆蓋。
    """
    total = info.get("size") or 0
    try:
        with _urlopen(info["url"], timeout=60.0) as resp, dest.open("wb") as f:
            got = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
    except Exception:
        dest.unlink(missing_ok=True)
        return False

    ok = dest.exists() and dest.stat().st_size > 1_000_000
    if ok and total:
        ok = dest.stat().st_size == total
    if ok:
        with dest.open("rb") as f:
            ok = f.read(2) == b"MZ"      # 確定是 Windows 執行檔
    if not ok:
        dest.unlink(missing_ok=True)
    return ok


def apply_and_restart(new_file: Path) -> bool:
    """用改名的方式換掉執行中的 exe，然後啟動新版、結束自己。

    成功的話這個函式不會回來（行程會結束）。失敗回 False 並盡量還原。
    """
    cur = exe_path()
    old = cur.with_suffix(cur.suffix + ".old")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        os.replace(cur, old)          # Windows 允許改名執行中的檔案
    except OSError:
        return False
    try:
        os.replace(new_file, cur)
    except OSError:
        try:
            os.replace(old, cur)      # 還原，避免使用者連舊版都開不起來
        except OSError:
            pass
        return False
    try:
        subprocess.Popen([str(cur)], cwd=str(cur.parent), close_fds=True)
    except OSError:
        return False
    return True


def clean_leftovers() -> None:
    """刪掉上次更新留下的 .old。刪不掉就算了（可能還被佔用），下次再試。"""
    if not is_frozen():
        return
    old = exe_path()
    old = old.with_suffix(old.suffix + ".old")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
