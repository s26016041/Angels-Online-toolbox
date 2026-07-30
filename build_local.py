"""本地編譯 + 冒煙測試工具（不上傳 GitHub，純本機驗證用）。

為什麼需要它
------------
打包成 --windowed 的 exe 若出問題（例如漏收分頁）會「一片白」且看不到原因。
這支工具讓你在本機：
  1. 用 AngelsOnlineToolbox.spec 編出 exe（與 release.py 同一份設定）。
  2. 立刻跑 exe 的 --selftest，確認分頁真的載入。
出問題時再用 --debug 編「帶主控台的除錯版」，直接看 traceback。

用法
----
    py build_local.py            # 編正式版（無主控台）＋ 冒煙測試
    py build_local.py --debug    # 編除錯版（有主控台，看得到 traceback）＋ 冒煙測試
    py build_local.py --run      # 冒煙測試通過後，真的把 GUI 開起來讓你眼睛確認
    py build_local.py --debug --run

雙擊 build_local.bat 亦可（等同 py build_local.py）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = "AngelsOnlineToolbox.spec"


def sh(cmd: list[str], env: dict | None = None) -> int:
    print("$", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def main() -> int:
    debug = "--debug" in sys.argv
    run_gui = "--run" in sys.argv

    # 確保有 PyInstaller
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller 未安裝，安裝中…")
        if sh([sys.executable, "-m", "pip", "install", "pyinstaller"]) != 0:
            print("✗ 安裝 PyInstaller 失敗。")
            return 1

    # 清掉舊產物
    for d in ("build", "dist"):
        shutil.rmtree(ROOT / d, ignore_errors=True)

    # 除錯版：設 AOT_CONSOLE=1 → spec 會編出帶主控台、名稱加 -debug 的版本。
    env = dict(os.environ)
    if debug:
        env["AOT_CONSOLE"] = "1"
        exe_name = "天使之戀AO工具箱-debug.exe"
        print("\n=== 編譯除錯版（帶主控台，看得到 traceback）===")
    else:
        env.pop("AOT_CONSOLE", None)
        exe_name = "天使之戀AO工具箱.exe"
        print("\n=== 編譯正式版（無主控台，GUI）===")

    if sh([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", SPEC], env=env) != 0:
        print("✗ 編譯失敗，請看上面 PyInstaller 的訊息。")
        return 1

    exe = ROOT / "dist" / exe_name
    if not exe.exists():
        print(f"✗ 找不到編出的 exe：{exe}")
        return 1
    size_mb = exe.stat().st_size // (1024 * 1024)
    print(f"\n✓ 編譯完成：{exe}（約 {size_mb} MB）")

    # 冒煙測試：實際跑打包好的 exe，確認分頁載入。
    print("\n=== 冒煙測試：exe --selftest ===")
    rc = sh([str(exe), "--selftest"])
    if rc != 0:
        print("\n✗ 冒煙測試失敗：打包後的 exe 沒有載入分頁（就是白屏的根源）。")
        if not debug:
            print("  → 用 `py build_local.py --debug` 重編除錯版，直接看 traceback。")
        return 1
    print("\n✅ 冒煙測試通過：分頁載入正常，這顆 exe 不會白屏。")

    if run_gui:
        print("\n=== 啟動 GUI（關掉視窗即結束）===")
        subprocess.run([str(exe)], cwd=ROOT)

    return 0


if __name__ == "__main__":
    sys.exit(main())
