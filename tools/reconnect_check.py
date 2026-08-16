"""自動回連離線測試（不碰遊戲、不碰記憶體、不寫真的 config）。

驗分身總控的自動回連狀態機：斷網全關等網重登、崩潰彈窗連兩拍才動手、
視窗消失不重複殺、單台斷線寬限吃得下換頻瞬斷、維修閘門擋住白開遊戲、
沒存帳密只報一次不無限重試、重登失敗退避重試、手動登入中不誤判。

作法照 tools/watch_check.py：offscreen Qt＋假遊戲層＋假時鐘，
假物件 patch 進**用到它的模組**的命名空間，探測改成同步跑。

用法：py tools\\reconnect_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication            # noqa: E402

from app.core import injector, nethealth, netstat, preload  # noqa: E402
from app.core import window as win                    # noqa: E402
from app.tabs import multi_tab                        # noqa: E402

# --- 假遊戲層 ----------------------------------------------------------
CLOCK = [1000.0]
WINDOWS: dict[int, str] = {}     # pid -> 視窗標題
EST: set[int] = set()            # 有 TCP 連線的 pid
DIALOGS: dict[int, str] = {}     # pid -> 錯誤視窗標題
NET_UP = [True]
SRV_UP = [True]
KILLED: list[int] = []


def _t(acct: str, chan: int = 3) -> str:
    return f"Angels Online Global - {acct}(雅典娜-{chan})" if acct \
        else "Angels Online Global"


def fake_windows():
    return [types.SimpleNamespace(pid=p, hwnd=p * 10, title=t)
            for p, t in sorted(WINDOWS.items())]


def fake_est():
    return set(EST)


def fake_dialogs(pids):
    return [types.SimpleNamespace(pid=p, hwnd=p * 10 + 1, title=t,
                                  class_name="#32770")
            for p, t in DIALOGS.items() if p in pids]


def fake_kill(pid):
    KILLED.append(pid)
    WINDOWS.pop(pid, None)      # 行程死了＝視窗、連線、對話框都消失
    EST.discard(pid)
    DIALOGS.pop(pid, None)
    return True


class FakeConfig:
    """MultiTab 只認 get/set/save；絕不能讓測試寫進真的 config.json。"""

    def __init__(self):
        self.data = {
            "login.exe_path": "D:/fake/start.exe",
            "accounts": [{"account": a, "server": "雅典娜"}
                         for a in ("A", "B", "C")],
        }

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        pass


class FakeLoginTab:
    def __init__(self):
        self.known = {"A", "B", "C"}
        self.calls: list[list[str]] = []
        self._busy = False

    def busy(self):
        return self._busy

    def known_accounts(self):
        return set(self.known)

    def start_auto_login(self, accounts):
        self.calls.append(list(accounts))
        self._busy = True
        return ""


# ⚠ patch 進「用到它的模組」的命名空間
preload.windows = fake_windows
netstat.established_pids = fake_est
win.crash_dialogs = fake_dialogs
injector.kill_process = fake_kill
nethealth.internet_up = lambda: NET_UP[0]
nethealth.server_up = lambda addr: SRV_UP[0]
nethealth.login_server_addr = lambda gd, name: ("1.2.3.4", 18111)
multi_tab._spawn = lambda fn: fn()          # 探測同步跑，測試才是決定性的
multi_tab.time = types.SimpleNamespace(monotonic=lambda: CLOCK[0])
multi_tab.config = FakeConfig()

app = QApplication.instance() or QApplication([])
TAB = multi_tab.MultiTab()
LT = FakeLoginTab()
TAB._login_tab = lambda: LT

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}  {detail}")


def tick(secs: float = 2.0, n: int = 1) -> None:
    for _ in range(n):
        CLOCK[0] += secs
        TAB._ar_tick()


def reset(on: bool = True) -> None:
    """回到乾淨狀態：清假遊戲層、重掛勾選（會經過 _ar_toggled → _ar_reset）。"""
    WINDOWS.clear()
    EST.clear()
    DIALOGS.clear()
    KILLED.clear()
    NET_UP[0] = True
    SRV_UP[0] = True
    LT.calls.clear()
    LT._busy = False
    TAB._ar_net.update(ok=None, ts=0.0, busy=False)
    TAB._ar_srv.update(ok=None, ts=0.0, busy=False)
    TAB.ar_box.setChecked(False)
    TAB.ar_box.setChecked(on)


def put(pid: int, acct: str) -> None:
    WINDOWS[pid] = _t(acct)
    if acct:
        EST.add(pid)


# ======================================================================
print("S0 勾選關著 → 什麼都不做")
reset(on=False)
put(11, "A")
tick()
WINDOWS.clear()
EST.clear()
tick(n=3)
check("不殺也不重登", not KILLED and not LT.calls)

# ======================================================================
print("S1 斷網：全關 → 等網路 → 網路穩定 → 一鍵重登 → 驗收")
reset()
put(11, "A")
put(12, "B")
put(13, "C")
WINDOWS[14] = _t("")            # 一台停在登入畫面（沒帳號、沒連線）
tick()                          # 學會目前在線名單
EST.clear()                     # 全部同時掉線
NET_UP[0] = False
tick()                          # 探測（同步）→ 斷網確認 → 全關
check("四台全關（含未登入那台）", sorted(KILLED) == [11, 12, 13, 14],
      f"KILLED={KILLED}")
check("進入等網路狀態", TAB._ar_state == "wait_net")
tick(n=3)
check("網路沒回來不重登", not LT.calls)
NET_UP[0] = True
tick(secs=7.0)                  # 過了探測間隔 → 重新探測 → 網路通了
check("穩定期還不重登", not LT.calls and TAB._ar_state == "wait_net")
tick(secs=5.0, n=2)             # 累積超過 AR_NET_STABLE
check("重登三個帳號", LT.calls and LT.calls[0] == ["A", "B", "C"],
      f"calls={LT.calls}")
check("進入等登入狀態", TAB._ar_state == "wait_login")
for i, a in enumerate(("A", "B", "C")):
    put(21 + i, a)              # 登入成功：三台回來了
LT._busy = False
tick()
check("驗收後回到待命", TAB._ar_state == "idle" and not TAB._ar_want)
check("狀態顯示完成", "完成" in TAB.ar_lbl.text(), TAB.ar_lbl.text())

# ======================================================================
print("S2 崩潰彈窗：連兩拍才動手；一閃而過不誤殺")
reset()
put(21, "A")
put(22, "B")
tick()
DIALOGS[21] = "Error"
tick()                          # 第 1 拍：先記著
check("第一拍不動手", not KILLED)
DIALOGS.pop(21)                 # 一閃而過
tick()
DIALOGS[21] = "Error"
tick()
check("重新出現要重數，仍不動手", not KILLED)
tick()                          # 連續第 2 拍 → 處置
check("殺掉崩潰那台", KILLED == [21], f"KILLED={KILLED}")
check("原因記錄崩潰", "崩潰" in TAB._ar_want.get("A", ""),
      str(TAB._ar_want))
tick(secs=7.0)                  # 過探測間隔 → 走重登路徑
check("只重登崩潰的帳號", LT.calls and LT.calls[-1] == ["A"],
      f"calls={LT.calls}")
check("另一台沒被動到", 22 in WINDOWS and 22 in EST)

# ======================================================================
print("S3 視窗消失：不用殺、直接重登")
reset()
put(31, "A")
tick()
WINDOWS.pop(31)                 # 遊戲整個不見（崩潰退出／被關）
EST.discard(31)
tick(secs=7.0)
check("沒有多餘的殺", not KILLED)
check("有排重登", LT.calls and LT.calls[-1] == ["A"], f"calls={LT.calls}")

# ======================================================================
print("S4 單台斷線：寬限吃得下換頻瞬斷；超過寬限才處置")
reset()
put(41, "A")
put(42, "B")
tick()
EST.discard(41)                 # 換頻瞬斷
tick(secs=2.0, n=2)
EST.add(41)                     # 4 秒後連回來了
tick()
check("瞬斷不處置", not KILLED and not TAB._ar_want)
EST.discard(41)                 # 這次真的斷了
tick(secs=2.0, n=14)            # 28 秒：還在寬限內
check("寬限內不處置", not KILLED)
tick(secs=4.0)                  # 超過 30 秒
check("超過寬限殺掉凍住那台", KILLED == [41], f"KILLED={KILLED}")
check("原因記錄連線中斷", "連線中斷" in TAB._ar_want.get("A", ""),
      str(TAB._ar_want))

# ======================================================================
print("S5 維修閘門：登入伺服器沒開門就不開遊戲")
reset()
put(51, "A")
tick()
SRV_UP[0] = False
WINDOWS.pop(51)
EST.discard(51)
tick(secs=7.0, n=3)
check("維修中不重登", not LT.calls, f"calls={LT.calls}")
check("狀態講清楚在等維修", "連不上" in TAB.ar_lbl.text(), TAB.ar_lbl.text())
SRV_UP[0] = True
tick(secs=61.0)                 # 舊探測過期 → 重探 → 開門了
tick(secs=7.0)
check("開門後才重登", LT.calls and LT.calls[-1] == ["A"], f"calls={LT.calls}")

# ======================================================================
print("S6 沒存帳密：只報一次，不無限重試")
reset()
put(61, "cook")                 # 不在帳號清單裡
tick()
WINDOWS.pop(61)
EST.discard(61)
tick(secs=7.0, n=2)
check("不會拿沒帳密的去登入", not LT.calls, f"calls={LT.calls}")
check("列入放棄名單", "cook" in TAB._ar_dropped)
check("狀態有警示", "帳密" in TAB.ar_lbl.text(), TAB.ar_lbl.text())
WINDOWS[62] = _t("cook")        # cook 又出現又消失 → 不再嘗試
EST.add(62)
tick()
WINDOWS.pop(62)
EST.discard(62)
tick(secs=7.0, n=2)
check("放棄過的不再排入", not LT.calls and not TAB._ar_want)

# ======================================================================
print("S7 重登沒全回來：退避重試不設上限")
reset()
put(71, "A")
tick()
WINDOWS.pop(71)
EST.discard(71)
tick(secs=7.0)
check("第一次重登", len(LT.calls) == 1, f"calls={LT.calls}")
LT._busy = False                # 登入流程結束但 A 沒回來
tick()
check("進入退避", TAB._ar_state == "idle" and TAB._ar_want,
      f"state={TAB._ar_state}")
tick(secs=2.0, n=5)             # 10 秒：還在冷卻
check("冷卻中不重試", len(LT.calls) == 1)
tick(secs=25.0)                 # 超過 30 秒冷卻
check("冷卻後重試", len(LT.calls) == 2, f"calls={LT.calls}")
put(72, "A")                    # 這次成功了
LT._busy = False
tick()
check("成功後清空", not TAB._ar_want and TAB._ar_state == "idle")

# ======================================================================
print("S8 手動一鍵登入中：旁觀，不誤判斷線")
reset()
put(81, "A")
tick()
LT._busy = True                 # 使用者自己按了一鍵登入
WINDOWS.pop(81)                 # 登入流程中視窗開開關關
EST.discard(81)
tick(n=2)
check("登入中不排回連", not TAB._ar_want)
put(82, "A")                    # 登入流程把 A 弄回來（換了 pid）
LT._busy = False
tick()
tick()
check("結束後不誤判視窗消失", not TAB._ar_want and not KILLED)

# ======================================================================
print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 → {FAILS}")
    sys.exit(1)
print("OK：自動回連狀態機全部情境通過")
