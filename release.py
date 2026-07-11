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
EXE_NAME = "AngelsOnlineToolbox"


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
    v = p.read_text(encoding="utf-8").strip()
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

    # 編譯單一 .exe（GUI 程式 → --windowed；lazy import 的套件要 --collect-all）
    print("\n開始編譯 .exe（第一次可能要幾分鐘）…")
    rc = sh([
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--clean", "--noconfirm",
        "--name", EXE_NAME,
        "--add-data", "VERSION;.",  # 把版本檔收進 exe，app 才讀得到版本
        "--collect-all", "keystone",
        "--collect-all", "pymem",
        "--collect-all", "pefile",
        "main.py",
    ])
    exe = ROOT / "dist" / f"{EXE_NAME}.exe"
    if rc != 0 or not exe.exists():
        die("編譯失敗，請看上面 PyInstaller 的訊息。")
    size_mb = exe.stat().st_size // (1024 * 1024)
    print(f"\n✓ 編譯完成：{exe}（約 {size_mb} MB）")

    # 建立 Release（gh 會順便建 tag）並上傳 .exe
    print("\n建立 GitHub Release 並上傳 .exe…")
    rc = sh([
        "gh", "release", "create", tag,
        "--target", target,
        "--title", tag,
        "--notes", f"天使之戀工具箱 {tag}\n\n由 release.py 自動編譯發布。",
        str(exe),
    ])
    if rc != 0:
        die("建立 Release 失敗，請看上面 gh 的訊息。")
    print(f"\n✅ 已發布 {tag} 到 GitHub Releases，並上傳 {EXE_NAME}.exe")


if __name__ == "__main__":
    main()
