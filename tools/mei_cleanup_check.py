r"""開場清 %TEMP%\_MEI* 的離線測試 —— 驗「還在跑的解壓目錄一根寒毛都不准動」。

驗的規格（2026-08-28 闖禍後補的，事實見 RO 專案 GAMEDATA [ENV-007]）：
    ① 還在跑的目錄（python3XX.dll 刪不掉）→ 資料檔一個都不准少
    ② 自己這次的解壓目錄 → 不准刪（刪了會當場自殺）
    ③ 沒人在用的舊目錄 → 要清掉（這功能本來的用途）
    ④ 沒有 python3XX.dll 的目錄 → 不是 onefile 解壓目錄，別碰
    ⑤ rmtree 自己爆掉 → 不准往外拋（清理失敗不值得打擾使用者）

⚠ ① 是這支腳本存在的唯一理由。舊寫法直接 rmtree，被鎖住的 DLL 讓它最後拋例外 ——
   但那時 assets/*.gz 早就刪光了，被害的程式不會當，只會安靜地少一塊功能。

用法：py tools\mei_cleanup_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import updater                        # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def make_bundle(root: Path, name: str) -> Path:
    """做一個像 onefile 解壓目錄的東西：一顆 python3XX.dll ＋ 一份資料檔。"""
    d = root / name
    (d / "assets").mkdir(parents=True)
    (d / "python312.dll").write_bytes(b"MZ")
    (d / "assets" / "item_names.tsv.gz").write_bytes(b"data")
    return d


def run(root: Path, mine: Path | None = None) -> None:
    os.environ["TEMP"] = str(root)
    os.environ.pop("_PYI_APPLICATION_HOME_DIR", None)
    sys._MEIPASS = str(mine) if mine else ""        # noqa: SLF001
    updater._clean_stale_mei()                      # noqa: SLF001


base = Path(tempfile.mkdtemp(prefix="meicheck-"))
real_unlink = Path.unlink
try:
    print("① 還在跑的目錄（DLL 刪不掉）→ 資料檔不准少")
    root = base / "a"
    root.mkdir()
    victim = make_bundle(root, "_MEI_running")

    def locked(self, *a, **k):
        if self.suffix == ".dll":
            raise PermissionError("in use")
        return real_unlink(self, *a, **k)

    Path.unlink = locked
    try:
        run(root)
    finally:
        Path.unlink = real_unlink
    check("assets 還在", (victim / "assets" / "item_names.tsv.gz").is_file())
    check("整個目錄還在", victim.is_dir())

    print("② 自己的解壓目錄不准刪")
    root = base / "b"
    root.mkdir()
    mine = make_bundle(root, "_MEI_mine")
    other = make_bundle(root, "_MEI_other")
    run(root, mine=mine)
    check("自己的還在", mine.is_dir())
    check("沒人用的被清掉", not other.exists())

    print("③ 沒有 python3XX.dll 的目錄別碰")
    root = base / "c"
    root.mkdir()
    stranger = root / "_MEI_stranger"
    (stranger / "assets").mkdir(parents=True)
    (stranger / "assets" / "keep.me").write_bytes(b"x")
    run(root)
    check("原封不動", (stranger / "assets" / "keep.me").is_file())

    print("④ rmtree 爆掉不准往外拋")
    root = base / "d"
    root.mkdir()
    make_bundle(root, "_MEI_busy")
    real_rmtree = shutil.rmtree

    def boom(*a, **k):
        raise OSError("in use")

    updater.shutil.rmtree = boom
    try:
        run(root)
        check("沒有拋例外", True)
    except OSError as exc:
        check("沒有拋例外", False, repr(exc))
    finally:
        updater.shutil.rmtree = real_rmtree
finally:
    Path.unlink = real_unlink
    shutil.rmtree(base, ignore_errors=True)

print()
if FAILS:
    print(f"[FAIL] {len(FAILS)} 項沒過：" + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
