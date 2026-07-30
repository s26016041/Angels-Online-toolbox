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
import shutil
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = "s26016041/Angels-Online-toolbox"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "天使之戀AO工具箱.exe"
# 0.2.5 以前的檔名。那些版本的更新程式是寫死找這個名字的，所以發布時要一併上傳
# 一份同內容、舊檔名的資產，否則他們會永遠找不到更新、靜靜卡在舊版。
LEGACY_ASSET_NAME = "AngelsOnlineToolbox.exe"
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
    """連線，憑證驗證失敗就退回不驗證再試一次。

    ⚠ 這裡不能只 `except ssl.SSLError` —— urlopen 遇到憑證問題丟的是
    `urllib.error.URLError` **包住** SSLError，裸的 SSLError 攔不到，後備等於沒作用，
    錯誤還會被外層吞掉變成「靜靜地不更新」。使用者的弟弟（Windows 10，系統根憑證
    較舊）就是卡在這裡，畫面上完全沒有提示。所以第一次失敗一律重試一次。

    退回不驗證是可接受的：抓的是自己 repo 的公開檔案，而且下載後還會檢查大小與
    PE 標頭，換檔前也會驗證，動不了手腳。
    """
    req = urllib.request.Request(url, headers=UA)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except Exception as first:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=ctx)
        except Exception:
            raise first


def last_error() -> str:
    """上一次查詢失敗的原因（給診斷用）。沒失敗過就是空字串。"""
    return _last_error[0]


_last_error = [""]


def latest_release() -> dict | None:
    """查 GitHub 最新 Release。回傳 {version, url, size, notes}；失敗回 None。

    沒網路、被限流、repo 沒有 Release 都算失敗。**失敗原因一定要留下來** ——
    以前是靜靜回 None，結果使用者的機器更新不了時，畫面與紀錄檔都沒有任何線索，
    完全無從查起。
    """
    _last_error[0] = ""
    try:
        with _urlopen(API_LATEST) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _last_error[0] = f"{type(exc).__name__}: {exc}"
        sys.stderr.write(f"[update] 查詢最新版本失敗 —— {_last_error[0]}\n")
        return None
    tag = data.get("tag_name") or ""
    assets = data.get("assets") or []
    # 先找正式檔名，找不到就退而取任何 .exe —— 這樣以後再改名也不會讓舊版失聯
    # （0.2.5 之前是寫死比對檔名的，改名時就得靠 LEGACY_ASSET_NAME 補一份）。
    picked = next((a for a in assets if a.get("name") == ASSET_NAME), None)
    if picked is None:
        picked = next(
            (a for a in assets
             if str(a.get("name", "")).lower().endswith(".exe")), None)
    if picked is None:
        return None
    return {
        "version": tag,
        "url": picked.get("browser_download_url"),
        "size": int(picked.get("size") or 0),
        "notes": (data.get("body") or "").strip(),
    }


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
        subprocess.Popen([str(cur)], cwd=str(cur.parent), close_fds=True,
                         env=_child_env())
    except OSError:
        return False
    return True


def _child_env() -> dict:
    """啟動新版時要用的環境變數：把 PyInstaller 的解壓目錄指標清掉。

    onefile 的 exe 啟動時會把自己解壓到 %TEMP%\\_MEIxxxxxx，並用 `_PYI_*` /
    `_MEIPASS2` 這些環境變數記住位置。如果直接把環境整份傳給新行程，**新版會沿用
    舊版的解壓目錄**；舊版接著結束、要刪掉那個目錄時，發現裡面的檔案還被新版開著，
    就會跳「Failed to remove temporary directory: ...\\_MEIxxxxxx」的警告。
    實際踩過（使用者更新後就看到這個視窗）。清掉之後新版會自己開一個新目錄。
    """
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("_MEI") or k.startswith("_PYI"))}


def clean_leftovers() -> None:
    """開場清理：刪掉上次更新留下的 .old，並移除舊版的「不再自動檢查」設定。

    刪不掉 .old 就算了（可能還被佔用），下次再試。
    """
    # 0.2.4 之前的版本有「不再自動檢查」選項。改成強制更新後那個設定必須清掉，
    # 否則當初按過的人會永遠停在舊版、拿到錯的記憶體位址還以為程式壞了。
    try:
        from app.config import config

        if config.get("update.auto_check", None) is not None:
            config.set("update.auto_check", None)
            config.save()
    except Exception:
        pass

    if not is_frozen():
        return
    old = exe_path()
    old = old.with_suffix(old.suffix + ".old")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
    _clean_stale_mei()


def _clean_stale_mei() -> None:
    """清掉 %TEMP% 裡別人留下的 _MEIxxxxxx 解壓目錄。

    onefile 的 exe 若沒能正常收尾（更新換檔、當掉、被工作管理員砍掉）就會留下
    這種目錄，一個約 80MB，累積起來很可觀 —— 使用者電腦上實際存過 9 個 / 86MB。
    正在使用中的那些會刪失敗，直接跳過；自己這次的解壓目錄也不能刪。
    """
    mine = os.environ.get("_PYI_APPLICATION_HOME_DIR") or getattr(
        sys, "_MEIPASS", "")
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if not tmp:
        return
    try:
        entries = list(Path(tmp).glob("_MEI*"))
    except OSError:
        return
    for d in entries:
        if not d.is_dir() or (mine and os.path.normcase(str(d))
                              == os.path.normcase(str(mine))):
            continue
        try:
            shutil.rmtree(d)
        except OSError:
            pass          # 還被別的行程開著 → 那個行程結束後自己會清
