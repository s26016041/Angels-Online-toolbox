"""自動更新的離線測試：斷點續傳／鏡像優先／sha256 驗證（不連網、不碰檔案總管）。

    py tools\\updater_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的規格（2026-09-20 使用者：「自動更新下載很久」）：
  ① **斷點續傳**：上次抓了一半，這次帶 `Range: bytes=N-` 接著抓，⛔ 不從 0 重來
  ② 網路中途斷掉 → **半成品留著**（下次接著抓）；抓完但**驗不過** → 一定刪掉
  ③ 伺服器不理 Range（回 200）→ 自己從頭抓，不會把兩段黏成壞檔
  ④ **鏡像優先**：`update.mirror` 有設就先抓鏡像，失敗自動退回 GitHub
  ⑤ **sha256**：GitHub API 給的摘要對不上就丟掉（鏡像被動手腳也裝不進來）
  ⑥ 大小不符、開頭不是 MZ（抓到錯誤頁面）也都要擋下來
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import updater                                # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


BODY = b"MZ" + bytes(range(256)) * 8_000          # 2MB 多一點，開頭是 MZ
SHA = hashlib.sha256(BODY).hexdigest()
INFO = {"version": "v9.9.9", "url": "https://github.test/a.exe",
        "size": len(BODY), "name": "a.exe", "sha256": SHA}


class FakeResp(io.BytesIO):
    """假的 HTTP 回應：可以指定 status、以及「讀到一半就斷線」。"""

    def __init__(self, data: bytes, status: int = 200, cut: int | None = None):
        super().__init__(data)
        self.status = status
        self._cut = cut
        self._n = 0

    def read(self, n=-1):
        if self._cut is not None and self._n >= self._cut:
            raise ConnectionResetError("連線被切斷（測試）")
        blk = super().read(n)
        self._n += len(blk)
        return blk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


CALLS: list[tuple[str, str]] = []                 # (url, Range 標頭)


def fake_net(plan):
    """plan(url, start) → FakeResp 或丟例外。裝進 updater._urlopen_req。"""
    def _open(req, timeout=None):
        rng = req.headers.get("Range") or req.headers.get("range") or ""
        CALLS.append((req.full_url, rng))
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        return plan(req.full_url, start)
    updater._urlopen_req = _open


def tmp() -> Path:
    d = Path(tempfile.mkdtemp(prefix="aoupd_"))
    return d / "new.exe"


print("① 一般下載：驗大小＋MZ＋sha256")
CALLS.clear()
fake_net(lambda url, start: FakeResp(BODY[start:], 206 if start else 200))
dest = tmp()
check("下載成功", updater.download(INFO, dest))
check("　內容一模一樣", dest.read_bytes() == BODY)

print("② 斷點續傳：上次抓了一半 → 帶 Range 接著抓")
CALLS.clear()
dest = tmp()
dest.write_bytes(BODY[:1000])                     # 上次剩下的半成品
fake_net(lambda url, start: FakeResp(BODY[start:], 206 if start else 200))
ok = updater.download(INFO, dest)
check("★ 續傳成功", ok and dest.read_bytes() == BODY)
check("★ 真的帶了 Range（⛔ 不是從 0 重抓）",
      CALLS and CALLS[0][1] == "bytes=1000-", str(CALLS[:1]))

print("③ 抓到一半斷線 → 半成品**留著**，下次接著抓")
CALLS.clear()
dest = tmp()
fake_net(lambda url, start: FakeResp(BODY[start:], 206 if start else 200,
                                     cut=500_000))
check("這次失敗", not updater.download(INFO, dest))
half = dest.stat().st_size if dest.exists() else 0
check("★ 半成品留著（⛔ 不准刪，不然慢線永遠抓不完）", 0 < half < len(BODY),
      f"實得 {half}")
CALLS.clear()
fake_net(lambda url, start: FakeResp(BODY[start:], 206 if start else 200))
check("★ 下次接著抓就完成", updater.download(INFO, dest)
      and dest.read_bytes() == BODY)
check("　接續的位置正是上次斷掉的地方",
      CALLS and CALLS[0][1] == f"bytes={half}-", f"{CALLS[:1]} half={half}")

print("④ 伺服器不吃 Range（回 200）→ 自己從頭抓，⛔ 不准把兩段黏起來")
CALLS.clear()
dest = tmp()
dest.write_bytes(BODY[:1000])
fake_net(lambda url, start: FakeResp(BODY, 200))   # 不管 Range，一律整包
check("★ 仍然拿到正確內容", updater.download(INFO, dest)
      and dest.read_bytes() == BODY)

print("⑤ sha256 對不上 → 丟掉（鏡像被動手腳也裝不進來）")
dest = tmp()
fake_net(lambda url, start: FakeResp(b"MZ" + b"\x00" * (len(BODY) - 2), 200))
check("★ 判定失敗", not updater.download(INFO, dest))
check("★ 壞檔要刪掉（⛔ 留著會一直續傳到同一個壞結果）", not dest.exists())

print("⑥ 大小不符／不是 MZ 都擋下來")
dest = tmp()
fake_net(lambda url, start: FakeResp(BODY[:-10], 200))
check("大小不符 → 失敗", not updater.download(INFO, dest))
dest = tmp()
fake_net(lambda url, start: FakeResp(b"<html>404</html>" * 200_000, 200))
check("抓到網頁 → 失敗", not updater.download(INFO, dest))

print("⑦ 鏡像優先：先抓鏡像，掛了自動退回 GitHub")
CALLS.clear()
updater.mirror_base = lambda: "https://cdn.test/ao"
dest = tmp()


def plan(url, start):
    if url.startswith("https://cdn.test"):
        raise TimeoutError("鏡像掛了（測試）")
    return FakeResp(BODY[start:], 206 if start else 200)


fake_net(plan)
check("★ 鏡像失敗也照樣更新得到", updater.download(INFO, dest)
      and dest.read_bytes() == BODY)
check("★ 順序是先鏡像、後 GitHub",
      [u.split("/")[2] for u, _r in CALLS] == ["cdn.test", "github.test"],
      str([u for u, _ in CALLS]))
CALLS.clear()
dest = tmp()
fake_net(lambda url, start: FakeResp(BODY[start:], 206 if start else 200))
check("★ 鏡像好的時候就不必去 GitHub", updater.download(INFO, dest)
      and [u.split("/")[2] for u, _r in CALLS] == ["cdn.test"],
      str([u for u, _ in CALLS]))
check("　鏡像的網址＝base + 檔名",
      CALLS[0][0] == "https://cdn.test/ao/a.exe", CALLS[0][0])

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
