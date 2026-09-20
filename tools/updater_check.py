"""自動更新的離線測試：斷點續傳／鏡像優先／sha256 驗證（不連網、不碰檔案總管）。

    py tools\\updater_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的規格（2026-09-20 使用者：「自動更新下載很久」）：
  ① **斷點續傳**：上次抓了一半，這次帶 `Range: bytes=N-` 接著抓，⛔ 不從 0 重來
  ② 網路中途斷掉 → **半成品留著**（下次接著抓）；抓完但**驗不過** → 一定刪掉
  ③ 伺服器不理 Range（回 200）→ 自己從頭抓，不會把兩段黏成壞檔
  ④ **鏡像優先**：`update.mirror` 有設就先抓鏡像，失敗自動退回 GitHub
  ⑤ **sha256**：GitHub API 給的摘要對不上就丟掉（鏡像被動手腳也裝不進來）
  ⑥ 大小不符、開頭不是 MZ（抓到錯誤頁面）也都要擋下來

第二輪（同一天，使用者：「我去 github 直接下載才 10 秒內」→ 快慢跟著連線走）：
  ⑧ **平行分段**：多條 keep-alive 連線各抓各的段、拼起來一模一樣；⛔ 不是每段開新連線
  ⑨ **慢連線斷掉重撥**（重擲骰子），⛔ 不准原樣留著慢慢抓
  ⑩ 抓到一半斷網 → `.par`＋`.par.json` 留著，下次**只抓缺的**；換了版本不准拿來接
  ⑪ 簽名網址過期（403）→ 重走轉址接著抓
  ⑫ 平行抓完驗不過 → 丟掉、退回單線；平行走不通（①~⑦ 的環境）→ 自動退回單線
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
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
    updater._resolve = _no_par


def _no_par(url):
    """①~⑦ 驗的是單線那條路：讓平行那條路一開始就走不通（＝擋 Range 的代理那種環境），
    順便驗「平行走不通 → 自動退回單線」。"""
    raise OSError("平行這條路不通（測試）")


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
check("　平行走不通時不留半成品垃圾", not list(dest.parent.glob("*.par*")))

# ─────────────────────────────────────────────────────────────────────────────
# 平行分段＋慢連線重撥（2026-09-20 第二輪：快慢跟著連線走，單線九成抽到慢的）
# ─────────────────────────────────────────────────────────────────────────────
updater.mirror_base = lambda: ""
updater.PAR_CHUNK = 256 * 1024                    # 2MB 的假檔切成 8 段
updater.PAR_PROBE = 0.05
updater.PAR_RERESOLVE = 0.0
NET = {"path": "/blob?sig=1", "budget": None, "slow_first": 0, "corrupt": False,
       "sent": 0}                                 # sent＝真的送出去幾個位元組
NLOCK = threading.Lock()
CONNS: list = []
REQS: list[tuple[int, int]] = []                  # 每一發 Range 的 [start, end)
RESOLVES: list[str] = []


class ParResp:
    def __init__(self, conn, data: bytes, status: int):
        self.conn, self.data, self.status, self.pos = conn, data, status, 0

    def read(self, n=-1):
        if self.conn.slow:                        # 抽到壞路徑的連線：一次只給 1KB
            time.sleep(0.02)
            n = min(n, 1024)
        blk = self.data[self.pos:self.pos + n]
        with NLOCK:
            if NET["budget"] is not None:
                if NET["budget"] < len(blk):
                    raise ConnectionResetError("網路斷了（測試）")
                NET["budget"] -= len(blk)
            NET["sent"] += len(blk)
        self.pos += len(blk)
        return blk


class ParConn:
    def __init__(self):
        with NLOCK:
            self.slow = len(CONNS) < NET["slow_first"]
            CONNS.append(self)
        self.closed = False
        self.served = 0

    def request(self, method, path, headers=None):
        rng = (headers or {}).get("Range", "")
        a, b = rng.split("=")[1].split("-")
        self._rng, self._path = (int(a), int(b) + 1), path
        with NLOCK:
            REQS.append(self._rng)

    def getresponse(self):
        if self._path != NET["path"]:             # 簽名網址過期
            return ParResp(self, b"", 403)
        s, e = self._rng
        body = BODY[s:e]
        if NET["corrupt"] and s == 0:
            body = b"MZ" + bytes(len(body) - 2)
        self.served += 1
        return ParResp(self, body, 206)

    def close(self):
        self.closed = True


def par_net(**kw):
    NET.update({"path": "/blob?sig=1", "budget": None, "slow_first": 0,
                "corrupt": False, "sent": 0})
    NET.update(kw)
    CONNS.clear()
    REQS.clear()
    RESOLVES.clear()
    CALLS.clear()

    def _res(url):
        RESOLVES.append(url)
        return "cdn.test", NET["path"], None
    updater._resolve = _res
    updater._par_conn = lambda host, ctx: ParConn()
    updater._urlopen_req = _single_forbidden


def _single_forbidden(req, timeout=None):
    CALLS.append((req.full_url, "單線"))
    raise OSError("這一節不該走到單線（測試）")


print("⑧ 平行分段：每段各抓各的、拼起來一模一樣")
check("　缺口切法：中間缺一塊只抓那一塊",
      updater._holes([(0, 100), (300, 1000)], 1000) == [(100, 300)],
      str(updater._holes([(0, 100), (300, 1000)], 1000)))
check("　缺口切法：重疊／相鄰的範圍先併起來",
      updater._merge([(0, 100), (50, 200), (200, 300)]) == [[0, 300]],
      str(updater._merge([(0, 100), (50, 200), (200, 300)])))
par_net()
updater.PAR_CONNS = 3                             # 8 段只給 3 條連線 → 一定得重複用
dest = tmp()
check("★ 下載成功、內容一模一樣", updater.download(INFO, dest)
      and dest.read_bytes() == BODY)
check("★ 真的分段抓（⛔ 不是一發整包）", len(REQS) >= 8, f"{len(REQS)} 發")
check("　每一段剛好蓋滿、沒有哪個位元組抓兩次",
      updater._merge(REQS) == [[0, len(BODY)]] and NET["sent"] == len(BODY),
      f"送出 {NET['sent']}／檔案 {len(BODY)}")
check("★ 快的連線留著一直用（⛔ 不是每段開新連線）",
      len(CONNS) == 3 and sum(c.served for c in CONNS) == len(REQS),
      f"連線 {len(CONNS)} 條／請求 {len(REQS)} 發")
updater.PAR_CONNS = 16
check("　沒碰單線那條路", not CALLS, str(CALLS))
check("　抓完不留 .par／.par.json", not list(dest.parent.glob("*.par*")))

print("⑨ 抽到慢連線 → 斷掉重撥，⛔ 不准原樣留著慢慢抓")
par_net(slow_first=6)                             # 前 6 條撥出去的都是慢的
dest = tmp()
t0 = time.time()
check("★ 照樣抓完、內容正確", updater.download(INFO, dest)
      and dest.read_bytes() == BODY)
slow = [c for c in CONNS if c.slow]
check("★ 慢的連線全部被斷掉", slow and all(c.closed for c in slow),
      f"{sum(c.closed for c in slow)}/{len(slow)}")
check("★ 有重撥（連線數比工人多）", len(CONNS) > 8, f"{len(CONNS)} 條")
check("　沒有被慢連線拖住（慢的 1 段要 5 秒）", time.time() - t0 < 4.0,
      f"{time.time() - t0:.1f}s")

print("⑩ 抓到一半網路斷掉 → 半成品留著，下次**只抓缺的**")
updater.PAR_ERRORS = 1
par_net(budget=900_000)
dest = tmp()
check("這次失敗", not updater.download(INFO, dest))
part = dest.with_name(dest.name + ".par")
side = dest.with_name(dest.name + ".par.json")
check("★ 半成品與進度檔都留著", part.exists() and side.exists())
check("　沒有假裝成功（dest 不存在）", not dest.exists())
check("★ 沒有退回單線從頭抓（已經抓到一半了）", not CALLS, str(CALLS))
had = sum(e - s for s, e in json.loads(side.read_text("utf-8"))["ranges"])
check("　進度檔記到的量合理", 0 < had <= 900_000, str(had))
par_net()
check("★ 下次接著抓就完成", updater.download(INFO, dest)
      and dest.read_bytes() == BODY)
check("★ 第二次只抓缺的（⛔ 不是整包重來）", NET["sent"] == len(BODY) - had,
      f"第二次抓了 {NET['sent']}，缺的是 {len(BODY) - had}")
check("　抓完不留 .par／.par.json", not list(dest.parent.glob("*.par*")))
side_dir = tmp()
par_net(budget=900_000)
updater.download(INFO, side_dir)
par_net()
other = dict(INFO, sha256="0" * 64)               # 換了一版（摘要不同）
updater.download(other, side_dir)
check("★ 換了版本 → 舊的半成品不准拿來接（整包重抓）",
      NET["sent"] == len(BODY), str(NET["sent"]))
updater.PAR_ERRORS = 5

print("⑪ 簽名網址過期（403）→ 重走一次轉址接著抓")
par_net()
dest = tmp()
orig_res = updater._resolve


def _res_expiring(url):
    out = orig_res(url)
    if len(RESOLVES) == 1:                        # 第一次給的是已經過期的網址
        return out[0], "/blob?sig=expired", out[2]
    return out


updater._resolve = _res_expiring
check("★ 照樣抓完", updater.download(INFO, dest) and dest.read_bytes() == BODY)
check("　有重新走轉址", len(RESOLVES) >= 2, str(len(RESOLVES)))

print("⑫ 平行抓完卻驗不過 → 丟掉，⛔ 不准裝進去")
par_net(corrupt=True)
dest = tmp()
check("★ 判定失敗", not updater.download(INFO, dest))
check("★ 壞檔、半成品都不留", not dest.exists()
      and not list(dest.parent.glob("*.par*")))
check("　有退回單線再試一次", bool(CALLS))

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
