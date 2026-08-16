"""一鍵發布：GitHub main 的版本若還沒 tag，就編譯 .exe、打 tag、上傳到 Releases。

流程
----
1. 讀 app/__init__.py 的 __version__ → tag = v<version>。
2. git fetch；若 origin 已有這個 tag → 代表發布過了，直接結束（不重複）。
3. 用 PyInstaller 把 main.py 編成單一 .exe。
4. 用 gh 在 origin/main 上建立 tag <tag> 的 Release，並上傳 .exe。

要換版本 → 改 app/__init__.py 的 __version__、push 到 main、再跑本工具。

需要
----
- Python（會自動裝 PyInstaller）
- git、GitHub CLI「gh」（需先 `gh auth login` 登入）

用法： py release.py   或雙擊 release.bat
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXE_NAME = "天使之戀AO工具箱"
# 0.2.5 以前的檔名。那些版本的自動更新是「寫死比對這個資產名稱」，所以每次發布都要
# 一併上傳一份同內容、舊檔名的副本，否則他們永遠找不到更新、靜靜卡在舊版。
# 0.2.6 之後的版本改成「找不到正式檔名就取任何 .exe」，等舊版都汰換完就能移除這段。
LEGACY_EXE_NAME = "AngelsOnlineToolbox"


def sh(cmd: list[str]) -> int:
    """執行並即時顯示輸出，回傳 returncode。"""
    print("$", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT).returncode


def cap(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def die(msg: str) -> None:
    print("\n✗ " + msg)
    sys.exit(1)


def read_version() -> str:
    p = ROOT / "VERSION"
    if not p.exists():
        die("找不到根目錄的 VERSION 檔。")
    # utf-8-sig：帶 BOM 的 VERSION 也照樣乾淨（2026-08-16 實踩過 tag 帶 U+FEFF）
    v = p.read_text(encoding="utf-8-sig").strip()
    if not v:
        die("VERSION 檔是空的。")
    return v


def main() -> None:
    if not shutil.which("git"):
        die("找不到 git。")
    if not shutil.which("gh"):
        die("找不到 GitHub CLI「gh」。請安裝：https://cli.github.com/ 然後 `gh auth login`。")
    if cap(["gh", "auth", "status"]).returncode != 0:
        die("gh 尚未登入。請先執行： gh auth login")

    version = read_version()
    tag = f"v{version}"
    print(f"目前版本 = {version}  →  tag = {tag}")

    # 取得遠端最新資訊
    sh(["git", "fetch", "origin", "--tags", "--quiet"])

    # 遠端是否已有這個 tag？有 → 發布過了，結束。
    if cap(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"]).stdout.strip():
        print(f"\n✓ GitHub 上已有 {tag}（已發布過）→ 不重複發布。")
        return

    # Release 要標在 GitHub main 上
    target = cap(["git", "rev-parse", "origin/main"]).stdout.strip()
    if not target:
        die("讀不到 origin/main，請先 git push。")
    head = cap(["git", "rev-parse", "HEAD"]).stdout.strip()
    dirty = bool(cap(["git", "status", "--porcelain"]).stdout.strip())
    if head != target or dirty:
        print("\n⚠ 注意：本機程式碼與 GitHub main 不完全一致"
              f"（{'有未提交變更' if dirty else 'HEAD 不等於 origin/main'}）。")
        print("  .exe 會用『本機目前的程式碼』編譯。若要編 main 上的版本，請先 push / 同步。")
        if input("  仍要繼續嗎？(y/N) ").strip().lower() != "y":
            die("已取消。")

    print(f"\n將在 origin/main（{target[:8]}）建立 {tag} 的 Release。")

    # 確保有 PyInstaller
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller 未安裝，安裝中…")
        if sh([sys.executable, "-m", "pip", "install", "pyinstaller"]) != 0:
            die("安裝 PyInstaller 失敗。")

    # 清掉舊產物
    for d in ("build", "dist"):
        shutil.rmtree(ROOT / d, ignore_errors=True)

    # 一律用 AngelsOnlineToolbox.spec 編（單一設定來源，發布版＝本地版）。
    # spec 裡已用 collect_submodules('app') 收動態載入的分頁，不能用裸 CLI flag
    # 編，否則會漏收分頁 → 打包後一片白。
    print("\n開始編譯 .exe（第一次可能要幾分鐘）…")
    rc = sh([
        sys.executable, "-m", "PyInstaller",
        "--clean", "--noconfirm",
        "AngelsOnlineToolbox.spec",
    ])
    exe = ROOT / "dist" / f"{EXE_NAME}.exe"
    if rc != 0 or not exe.exists():
        die("編譯失敗，請看上面 PyInstaller 的訊息。")

    # 冒煙測試：實際跑打包好的 exe，確認分頁真的載入（避免又是白屏才發現）。
    print("\n冒煙測試：執行打包後的 exe --selftest …")
    if sh([str(exe), "--selftest"]) != 0:
        die("冒煙測試失敗：打包後的 exe 沒有載入任何分頁，發布中止。請用 build_local.py --debug 追查。")
    print("✓ 冒煙測試通過。")
    size_mb = exe.stat().st_size // (1024 * 1024)
    print(f"\n✓ 編譯完成：{exe}（約 {size_mb} MB）")

    # 舊檔名的副本：給 0.2.5 以前的版本抓（它們寫死比對舊資產名稱）
    legacy = ROOT / "dist" / f"{LEGACY_EXE_NAME}.exe"
    shutil.copy2(exe, legacy)
    print(f"✓ 另存舊檔名副本供舊版自動更新使用：{legacy.name}")

    # 建立 Release（gh 會順便建 tag）並上傳兩份 .exe
    print("\n建立 GitHub Release 並上傳 .exe…")
    rc = sh([
        "gh", "release", "create", tag,
        "--target", target,
        "--title", tag,
        "--notes", f"天使之戀工具箱 {tag}\n\n由 release.py 自動編譯發布。\n\n"
                   f"下載 `{EXE_NAME}.exe` 即可。"
                   f"（`{LEGACY_EXE_NAME}.exe` 是同一個檔案，"
                   f"只是為了讓 0.2.5 以前的版本能自動更新而保留的舊檔名。）",
        str(exe), str(legacy),
    ])
    if rc != 0:
        die("建立 Release 失敗，請看上面 gh 的訊息。")
    print(f"\n✅ 已發布 {tag} 到 GitHub Releases，"
          f"並上傳 {EXE_NAME}.exe 與 {LEGACY_EXE_NAME}.exe")


if __name__ == "__main__":
    main()
