"""巡迴換頻道離線測試 —— offscreen Qt＋假的 channel 層，驗 farm_tab 的換頻節奏。

驗的規格（「打王換頻道」是 2026-08-28 使用者定的）：
    ① 介面：「巡迴換頻道」方框裡有兩個勾選，預設都沒勾
    ② 互斥：兩個只能擇一，勾「打王換頻道」時「每 N 分一輪」反灰
    ③ 打王模式**完全不看時間**：放著幾小時也不換頻
    ④ 確認打死一隻王 → 下一拍出發，一輪＝繞完每一頻再回到原本那一頻
    ⑤ 每頻停留＝「每頻 N 秒」，停留期間照常打怪（只有換頻那幾秒暫停）
    ⑥ 一輪跑完就待命；巡迴途中再打到王**不另外排**（使用者定案：忽略）
    ⑦ is_boss 回 None（查不到／改版位移）＝不算王，不觸發（不猜）
    ⑧ 打到小怪、或沒勾打王換頻道時打到王 → 都不觸發
    ⑨ 自動換頻的舊行為沒被改壞：時間到才出發，王殺再多也不管
    ⑩ 讀不到頻道／分流數 → 這一輪跳過，且**不會每拍重掃**（全掃 0.3~1 秒）
    ⑪ 換頻排不進指令槽 → 原地重試，不會漏掉那一站
    ⑫ 補給那一趟完全不換頻
    ⑬ 設定：rot_boss 存得起來、載得回來，互斥狀態跟著回來

用法：py tools\\rotation_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication          # noqa: E402

APP = QApplication.instance() or QApplication([])

from app.config import Config                       # noqa: E402
from app.tabs import farm_tab                       # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


# --- 假遊戲層 ----------------------------------------------------------
class FakeChannel:
    """換頻道那一層的替身：真正換頻是送封包，這裡只記帳。

    ⚠ 只換掉 I/O（哪一頻、換過去、有幾頻），節奏判斷跑的是 farm_tab 真的
      `_tick_rotation`／`_switch_channel` —— 不然就是在測替身。
    """

    def __init__(self) -> None:
        self.here: int | None = 3      # 目前在第幾頻（None ＝ 讀不到標題）
        self.total: int | None = 5     # 這台伺服器幾個分流（None ＝ 讀不到）
        self.ok = True                 # switch() 排得進指令槽嗎
        self.switches: list[int] = []  # 換頻流水帳
        self.counts = 0                # count() 被叫了幾次（全掃很貴）

    def current(self, hwnd):
        return self.here

    def count(self, sc, hwnd):
        self.counts += 1
        return self.total

    def switch(self, mover, n, maxn):
        if not self.ok:
            return False
        self.switches.append(n)
        self.here = n
        return True


class FakeSC:
    def _read_bytes(self, addr, n):
        return None

    def alive(self):
        return True


CH = FakeChannel()
BOSS: bool | None = True          # monsters.is_boss 的答案（None ＝ 查不到）

farm_tab.channel = CH
# ⚠ 只換掉 is_boss 這一支，monsters 其他部分維持真的。
farm_tab.monsters.is_boss = lambda sc, type_id, idx=None: BOSS
# ⚠ 設定檔導去暫存檔：測試不可以動到使用者真的 config.json。
#   用的是**真的 Config 類別**（不是替身），存讀路徑跟產品一模一樣。
CFG_PATH = Path(tempfile.mkdtemp(prefix="rotchk_")) / "config.json"
farm_tab.config = Config(CFG_PATH)


def build_page(account: str = "acct", stay: float | None = 10.0):
    sc = FakeSC()
    page = farm_tab.CharFarmPage(
        1234, 0, "t", sc, lambda pid, full=False: True,
        farm_tab.TargetWorker(sc), farm_tab.KeyWorker(0, sc),
        account=account, char_name="小狐")
    page._ensure_mover = lambda: True
    page._mover = types.SimpleNamespace(active=True)
    page.cur_scene = lambda: 122
    page.my_pos = lambda: (10.0, 20.0)
    page._sync_castwatch = lambda: None
    page._keys.begin_learning = lambda: None
    # ⚠ stay=None ＝ 不要蓋掉設定檔載進來的值（存讀那一節要驗的就是它）
    if stay is not None:
        page.rot_stay.setValue(stay)   # 每頻停 10 秒，測試才跑得快
    return page


def kill_boss(page, type_id: int = 999) -> None:
    """走真的 _on_died（confirmed=True）—— 引信掛在那裡，不要繞過去。"""
    page._cur = types.SimpleNamespace(eid=0x1234, type_id=type_id, name="王")
    page._on_died(0x1234, True)


def drive(page, seconds: float, step: float = 1.0) -> list[bool]:
    """跑心跳。回傳每一拍的「這拍不要打怪」旗標。"""
    out = []
    n = int(round(seconds / step))
    for _ in range(n):
        out.append(page._tick_rotation(step))
    return out


print("① 介面：兩個勾選都在，預設都沒勾")
page = build_page()
check("有「自動換頻」", page.rot_cb.text() == "自動換頻")
check("有「打王換頻道」", page.rot_boss_cb.text() == "打王換頻道")
check("預設都沒勾",
      not page.rot_cb.isChecked() and not page.rot_boss_cb.isChecked())
check("預設「每 N 分一輪」可以改", page.rot_every.isEnabled())

print("② 互斥＋「每 N 分一輪」反灰")
page.rot_cb.click()
check("勾了自動換頻", page.rot_cb.isChecked())
page.rot_boss_cb.click()
check("勾打王換頻道 → 自動換頻自己放掉", not page.rot_cb.isChecked())
check("打王換頻道勾著", page.rot_boss_cb.isChecked())
check("「每 N 分一輪」反灰", not page.rot_every.isEnabled())
page.rot_cb.click()
check("勾回自動換頻 → 打王換頻道放掉", not page.rot_boss_cb.isChecked())
check("「每 N 分一輪」恢復可用", page.rot_every.isEnabled())
page.rot_cb.click()
check("兩個都可以不勾",
      not page.rot_cb.isChecked() and not page.rot_boss_cb.isChecked())

print("③ 打王模式不看時間：放著兩小時也不換頻")
page = build_page()
CH.switches.clear()
page.rot_boss_cb.setChecked(True)
page.rot_every.setValue(1.0)              # 就算設 1 分鐘也不該有用
busy = drive(page, 7200.0, step=10.0)
check("一次都沒換頻", CH.switches == [], f"換了 {CH.switches}")
check("整段都照常打怪", not any(busy))
check("狀態文字在等王", "打到一隻王" in page.rot_lbl.text(),
      page.rot_lbl.text())

print("④ 打死一隻王 → 當場跑一輪完整巡迴（3 頻出發，共 5 頻）")
CH.here, CH.total, CH.switches = 3, 5, []
kill_boss(page)
check("王倒下當拍還沒換頻（下一拍才出發）", CH.switches == [])
first = page._tick_rotation(0.1)
check("下一拍就排班", first is True)
check("排的順序是 4,5,1,2,3", page._rot_seq == [4, 5, 1, 2, 3],
      str(page._rot_seq))
check("狀態文字說是打到王才走", "打到王了" in page.rot_lbl.text(),
      page.rot_lbl.text())
busy = drive(page, 120.0, step=1.0)
check("五站都換到了", CH.switches == [4, 5, 1, 2, 3], str(CH.switches))
check("回到原本那一頻", CH.here == 3)
check("一輪跑完就收工", page._rot_seq == [] and page._rot_settle <= 0)
check("停留期間照常打怪（大部分拍沒暫停）",
      busy.count(False) > busy.count(True),
      f"暫停 {busy.count(True)} 拍／照打 {busy.count(False)} 拍")

print("⑤ 每頻停留＝「每頻 N 秒」")
CH.here, CH.total, CH.switches = 2, 5, []
page = build_page()
page.rot_boss_cb.setChecked(True)
page.rot_stay.setValue(30.0)
kill_boss(page)
page._tick_rotation(0.1)                   # 排班
page._tick_rotation(0.1)                   # 換到第一站
drive(page, farm_tab.ROT_SETTLE + 1.0)     # 等穩定期過
check("換完第一站就停手了", CH.switches == [3], str(CH.switches))
drive(page, 20.0)
check("還沒滿 30 秒不會換下一站", CH.switches == [3], str(CH.switches))
drive(page, 12.0)
check("滿 30 秒才換下一站", CH.switches == [3, 4], str(CH.switches))

print("⑥ 巡迴途中再打到王 → 忽略，跑完就待命")
CH.here, CH.total, CH.switches = 1, 3, []
page = build_page()
page.rot_boss_cb.setChecked(True)
kill_boss(page)
page._tick_rotation(0.1)
kill_boss(page)                            # ← 途中又殺一隻
check("途中的王沒被記下來", page._rot_boss_hit is False)
drive(page, 120.0)
check("只跑了一輪（3 站）", CH.switches == [2, 3, 1], str(CH.switches))
drive(page, 600.0, step=10.0)
check("之後就待命，不會自己再跑", CH.switches == [2, 3, 1],
      str(CH.switches))
check("狀態文字回到等王", "等下一隻王" in page.rot_lbl.text()
      or "打到一隻王" in page.rot_lbl.text(), page.rot_lbl.text())

print("⑦⑧ 不是王／查不到／沒勾 → 都不觸發")
CH.here, CH.total, CH.switches = 1, 3, []
page = build_page()
page.rot_boss_cb.setChecked(True)
BOSS = None                                # 查不到（改版位移、表還沒載）
kill_boss(page)
check("is_boss 回 None 不算王", page._rot_boss_hit is False)
BOSS = False                               # 小怪
kill_boss(page)
check("小怪不算王", page._rot_boss_hit is False)
BOSS = True
drive(page, 60.0)
check("整段一次都沒換頻", CH.switches == [], str(CH.switches))
page.rot_boss_cb.setChecked(False)
kill_boss(page)
check("沒勾打王換頻道 → 打到王也不記", page._rot_boss_hit is False)

print("⑨ 自動換頻舊行為沒被改壞")
CH.here, CH.total, CH.switches = 1, 3, []
page = build_page()
page.rot_cb.setChecked(True)
page.rot_every.setValue(1.0)               # 1 分鐘一輪
kill_boss(page)
check("殺王不會讓自動換頻提早出發", page._rot_boss_hit is False)
drive(page, 50.0, step=5.0)
check("不到 1 分鐘不出發", CH.switches == [], str(CH.switches))
drive(page, 20.0, step=5.0)
check("滿 1 分鐘就出發", CH.switches[:1] == [2], str(CH.switches))

print("⑩ 讀不到頻道／分流數 → 跳過，而且不會每拍重掃")
CH.here, CH.total, CH.switches, CH.counts = None, 5, [], 0
page = build_page()
page.rot_boss_cb.setChecked(True)
kill_boss(page)
page._tick_rotation(0.1)
check("讀不到頻道就跳過", CH.switches == [] and page._rot_boss_hit is False)
check("狀態文字說了跳過", "跳過" in page.rot_lbl.text(), page.rot_lbl.text())
drive(page, 60.0, step=0.1)
check("跳過之後不再重掃分流數（≤1 次）", CH.counts <= 1, f"掃了 {CH.counts} 次")
CH.here, CH.total = 3, None
page = build_page()
page.rot_boss_cb.setChecked(True)
kill_boss(page)
page._tick_rotation(0.1)
check("讀不到分流數也跳過", CH.switches == [])
CH.total = 5

print("⑪ 換頻排不進指令槽 → 原地重試，不漏站")
CH.here, CH.total, CH.switches = 1, 3, []
page = build_page()
page.rot_boss_cb.setChecked(True)
kill_boss(page)
page._tick_rotation(0.1)                   # 排班
CH.ok = False
drive(page, 10.0)
check("送不出去就不算換過", CH.switches == [], str(CH.switches))
check("那一站還排在最前面", page._rot_seq[:1] == [2], str(page._rot_seq))
CH.ok = True
drive(page, 120.0)
check("恢復之後整輪照樣跑完", CH.switches == [2, 3, 1], str(CH.switches))

print("⑫ 補給那一趟完全不換頻")
CH.here, CH.total, CH.switches = 1, 3, []
page = build_page()
page.rot_boss_cb.setChecked(True)
kill_boss(page)
page._supply = True
busy = drive(page, 60.0)
check("補給中不換頻", CH.switches == [], str(CH.switches))
check("補給中不接管心跳", not any(busy))
page._supply = None
drive(page, 120.0)
check("補給結束後那隻王還算數", CH.switches == [2, 3, 1], str(CH.switches))

print("⑬ 設定存讀")
page = build_page(account="setchk")
page.rot_boss_cb.setChecked(True)
page.rot_stay.setValue(45.0)
page._save_settings()
again = build_page(account="setchk", stay=None)
check("rot_boss 載得回來", again.rot_boss_cb.isChecked())
check("自動換頻沒跟著被打開", not again.rot_cb.isChecked())
check("「每頻 N 秒」共用同一個值", again.rot_stay.value() == 45.0)
check("載回來時「每 N 分一輪」也是反灰的", not again.rot_every.isEnabled())

print()
if FAILS:
    print(f"✘ {len(FAILS)} 項失敗：" + "、".join(FAILS))
    sys.exit(1)
print("OK —— 巡迴換頻道（含打王換頻道）全部通過")
