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

import hashlib
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
# ★ Release 只有一份 exe、就叫這個名字（使用者 2026-09-05 定：「上傳一份 .exe 就好，
#   中文檔案名稱那個不用了」；v0.4.79 以前是中文檔名＋這個各一份）。
#   找不到就退而取任何 .exe（見 pick_asset），所以檔名再改也不會讓舊版失聯。
ASSET_NAME = "AngelsOnlineToolbox.exe"
TIMEOUT = 15.0
UA = {"User-Agent": "AngelsOnlineToolbox-Updater"}
# ★★★★ 2026-09-20 使用者：「自動更新下載很久」→ 實測（同一台機器、同一個檔）：
#     Python 抓 GitHub 的 Release：0.07~1.3 MB/s（單條連線連抓 90 秒全程 0.10）
#     curl   抓同一個檔          ：0.05~5.7 MB/s（一樣飄）
#     Python 抓 Cloudflare 測速檔：**7.9 MB/s** ← 線路本身沒問題
#     4/8 條平行分段 0.25/0.35、每 4MB 換連線 0.13、換 API assets 端點 0.14 → 全都沒用
#   ＝ **GitHub 的 Release 通道從這條線出去常常只有 0.1MB/s**，不是我們的 bug。
#   能做的三件事：① exe 變小（見 .spec 的 DROP_BINARIES）②**斷點續傳**（下面）
#   ③ **鏡像優先**：config 的 `update.mirror` 填一個 base URL（例如自己的 CDN），
#     就先從那裡抓、失敗自動退回 GitHub。⚠ 不管從哪抓，**最後都用 GitHub API 給的
#     sha256 對一次**（對不上就丟掉）——鏡像被換掉也裝不進來。
MIRROR_KEY = "update.mirror"


def mirror_base() -> str:
    """鏡像的 base URL（沒設就是空字串）。設定檔壞掉一律當沒設。"""
    try:
        from app.config import config
        return str(config.get(MIRROR_KEY, "") or "").strip().rstrip("/")
    except Exception:                                      # noqa: BLE001
        return ""


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


def _urlopen_req(req, timeout: float = TIMEOUT):
    """跟 `_urlopen` 同一套（憑證失敗退回不驗證），但收的是現成的 Request
    —— 續傳要自己帶 Range 標頭。"""
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
        if sys.stderr:   # 打包版 stderr 是 None
            sys.stderr.write(f"[update] 查詢最新版本失敗 —— {_last_error[0]}\n")
        return None
    tag = data.get("tag_name") or ""
    picked = pick_asset(data.get("assets") or [])
    if picked is None:
        return None
    # ★ GitHub 會給 "sha256:xxxx"（新版 API）—— 有就帶著，下載完對一次。
    dig = str(picked.get("digest") or "")
    return {
        "version": tag,
        "url": picked.get("browser_download_url"),
        "size": int(picked.get("size") or 0),
        "name": picked.get("name") or ASSET_NAME,
        "sha256": dig.split(":", 1)[1].lower() if dig.startswith("sha256:") else "",
        "notes": (data.get("body") or "").strip(),
    }


def pick_asset(assets: list) -> dict | None:
    """挑要下載的資產：先找正式檔名，找不到就退而取**任何 .exe**；都沒有回 None。

    退路是給「檔名改了」用的：0.2.5 以前寫死比對檔名的版本，靠 Release 上那一份
    `AngelsOnlineToolbox.exe` 就抓得到；之後的版本不管 Release 叫什麼名字都抓得到。
    """
    picked = next((a for a in assets if a.get("name") == ASSET_NAME), None)
    if picked is None:
        picked = next(
            (a for a in assets
             if str(a.get("name", "")).lower().endswith(".exe")), None)
    return picked


def check() -> dict | None:
    """有新版就回傳它的資訊，否則 None。開發模式一律 None。"""
    if not is_frozen():
        return None
    from app import __version__

    info = latest_release()
    if not info or not info.get("url"):
        return None
    return info if is_newer(info["version"], __version__) else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _good(dest: Path, info: dict) -> bool:
    """下載完的檔案能不能用：大小對、是 Windows 執行檔、sha256 對得上。"""
    if not dest.exists() or dest.stat().st_size <= 1_000_000:
        return False
    total = info.get("size") or 0
    if total and dest.stat().st_size != total:
        return False
    with dest.open("rb") as f:
        if f.read(2) != b"MZ":                 # 抓到錯誤頁面那種
            return False
    want = (info.get("sha256") or "").lower()
    return (not want) or _sha256(dest) == want


def _fetch(url: str, info: dict, dest: Path, progress=None) -> bool:
    """把 `url` 抓成 `dest`。**支援斷點續傳**：dest 已經有一半就從那裡接著抓。

    ⚠ 網路中斷時**不刪掉半成品**（下一次啟動接著抓）；只有「抓完卻驗不過」
      才刪 —— 那種檔案留著會一直續傳到同一個壞結果。
    """
    total = info.get("size") or 0
    have = dest.stat().st_size if dest.exists() else 0
    if total and have >= total:                # 上次其實抓完了
        return _good(dest, info) or (dest.unlink(missing_ok=True) or False)
    headers = dict(UA)
    mode = "wb"
    if have > 0:
        headers["Range"] = f"bytes={have}-"
        mode = "ab"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = _urlopen_req(req, timeout=60.0)
    except Exception:                                      # noqa: BLE001
        return False
    with resp:
        if have > 0 and resp.status != 206:     # 伺服器不吃續傳 → 從頭來
            have, mode = 0, "wb"
        try:
            with dest.open(mode) as f:
                got = have
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
        except Exception:                                  # noqa: BLE001
            return False                       # ⚠ 半成品留著，下次續傳
    if _good(dest, info):
        return True
    dest.unlink(missing_ok=True)               # 驗不過的一定要丟
    return False


def download(info: dict, dest: Path, progress=None) -> bool:
    """下載新版到 dest。progress(已下載, 總量) 可選。

    ① 設定了鏡像（`update.mirror`）就**先從鏡像抓**，失敗才退回 GitHub。
    ② 斷點續傳：上次沒抓完的接著抓（GitHub 與一般 CDN 都支援 Range）。
    ③ 不管從哪抓，最後都驗大小＋PE 標頭＋**GitHub API 給的 sha256**，
       對不上就丟掉重來 —— 鏡像被動手腳也裝不進來。
    """
    urls = []
    base = mirror_base()
    if base:
        urls.append(f"{base}/{info.get('name') or ASSET_NAME}")
    if info.get("url"):
        urls.append(info["url"])
    for k, url in enumerate(urls):
        if _fetch(url, info, dest, progress):
            return True
        if k + 1 < len(urls):                  # 鏡像不行 → 換 GitHub，從頭抓
            dest.unlink(missing_ok=True)
    return False


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


def _looks_abandoned(d: Path) -> bool:
    """這個解壓目錄真的沒人在用嗎 —— **先刪 python3XX.dll，刪得掉才算**。

    為什麼要有這道關卡，而不是直接 rmtree 看它成不成功（2026-08-28 實際闖的禍）：
    rmtree 是**一路刪下去**的，遇到刪不掉的檔案只會在最後拋例外 —— 在那之前
    它已經把所有刪得掉的東西刪光了。對一個**還在執行**的 onefile 解壓目錄來說：

      - 被映射的 .dll / .pyd 留著 → **被害的程式不會當**（所以沒人發現）
      - assets/*.gz 這種沒被開著的純資料檔 → **全部被刪光**

    也就是最糟的那種失效：安靜地少一塊功能。我們就是這樣把姊妹專案
    RO-Online-toolbox 正在跑的目錄挖空的（它的怪物表憑空消失、怪物過濾退化、
    全程沒有任何錯誤）。舊註解那句「還被別的行程開著 → 那個行程結束後自己會清」
    從頭到尾就是錯的。

    探測要挑**行程活著就一定映射著**的檔案，才問得出「有沒有人在跑」：
    python3XX.dll 就是。python3.dll（穩定 ABI 的轉送層）不保證被載入，不能用。

    ⛔ 兩個直覺的探測法實測都不成立，別再試（見 RO 專案 GAMEDATA [ENV-007]）：
      - 目錄 rename 得動嗎 → **改得動**（LoadLibrary 開檔時帶 FILE_SHARE_DELETE）
      - 獨佔開啟（CreateFileW, share=0）得開嗎 → **開得起來**
      只有 unlink 對映射中的映像檔會失敗。

    探測本身是破壞性的，但破壞的是「通過＝馬上要整個刪掉」的目錄，
    而且它是整個清理流程動的**第一個**檔案：不通過就等於什麼都沒發生。
    """
    probes = sorted(d.glob("python3[0-9][0-9].dll"))
    if not probes:
        # 不像 onefile 的解壓目錄（或已經被誰清到一半）→ 不歸我們處理，別碰。
        return False
    for dll in probes:
        try:
            dll.unlink()
        except OSError:
            return False          # 還映射著 → 有人在跑 → 收手
    return True


def _clean_stale_mei() -> None:
    """清掉 %TEMP% 裡**已經沒人在用**的 _MEIxxxxxx 解壓目錄。

    onefile 的 exe 若沒能正常收尾（更新換檔、當掉、被工作管理員砍掉）就會留下
    這種目錄，一個約 80MB，累積起來很可觀 —— 使用者電腦上實際存過 9 個 / 86MB。
    自己這次的解壓目錄不能刪。

    ⚠ 「刪不掉的自然會失敗」**不是**安全網 —— 那樣會把還在跑的程式挖空，
    理由見 `_looks_abandoned()`。動手前一定要先過那道關卡。
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
        if not _looks_abandoned(d):
            continue              # 檔案還鎖著 → 有人在跑 → 一根寒毛都不准動
        try:
            shutil.rmtree(d)
        except OSError:
            pass                  # 剩下的下次再清，清理失敗不值得打擾使用者
