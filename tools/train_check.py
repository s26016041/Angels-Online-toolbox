"""自動練技離線測試 —— offscreen Qt＋假遊戲層，驗 farm_tab 的練技狀態機。

驗的規格（2026-08-19 使用者定）：
    ① 開「自動練技」→ 精靈主開關＋輔助頁「原地重複練習技能」都開起來
    ② 跟「開始掛機」互斥（同一隻角色只能有一個在指揮）
    ③ 遊戲重開會自己把練習技能關掉 → 看門狗每拍往開推（主開關也一樣）
    ④ 藥水見底 → 關主開關 → 跑「只有補給商那站」的補給（potion_only、
       back_to=練技原地、不走巡邏點）→ 回來主開關開回去
    ⑤ 見底的是店裡沒賣的藥水 → 通知＋自動關閉練技
    ⑥ 連續兩趟回來還是見底 → 通知＋自動關閉（燒翼煞車）
    ⑦ 沒回程道具連續 NO_RECALL_TRIES 次 → 通知＋自動關閉

用法：py tools\\train_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication          # noqa: E402

APP = QApplication.instance() or QApplication([])

from app.tabs import farm_tab                       # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


# --- 假遊戲層 ----------------------------------------------------------
class FakeRobot:
    """精靈層的替身：開關是兩個旗標，藥水／翼的「現況」由測試腳本改。"""

    POTION_LOW = 5
    BUY_KEEP_WINGS = 50
    AS_EXERCISE_SKILL = 2090

    def __init__(self):
        self.run = False           # 精靈主開關
        self.ex = False            # 原地重複練習技能
        self.dry = []              # potions_out 的回傳（見底清單）
        self.wing = (5, 30)        # has_recall_item：(格號, 幾個)／()／None
        self.calls = []            # (名字, 參數) 流水帳

    def is_run(self, sc):
        return self.run

    def set_run(self, mover, sc, on):
        self.calls.append(("set_run", bool(on)))
        self.run = bool(on)
        return True, ""

    def exercise_on(self, sc):
        return self.ex

    def force_exercise(self, sc, on):
        self.calls.append(("exercise", bool(on)))
        self.ex = bool(on)
        return True

    def set_bool(self, mover, sc, vid, val):
        self.calls.append(("set_bool", vid, bool(val)))
        return True, bool(val)

    def apply_prefs(self, mover, sc, **kw):
        return []

    def disable_return_supply(self, mover, sc):
        self.calls.append(("disable_return_supply",))
        return []

    def ensure_buy_item(self, mover, sc, item_id, keep):
        self.calls.append(("ensure_buy", item_id, keep))
        return None

    def potion_buy_ids(self, mover, sc, pid=0):
        return {"HP": [4836], "MP": []}

    def potions_out(self, mover, sc, inv, pid=0, hp=True, mp=True):
        return list(self.dry)

    def supply_needed(self, *a, **kw):
        return None

    def has_recall_item(self, sc, inv):
        return self.wing


class FakeSupply:
    SHOP_TABLE = {4836: (2, 30)}   # 有表（_dry_stop 的「表載不到」註記不觸發）

    def __init__(self):
        self.trips = []            # 每趟 run_full_supply 收到的參數
        self.sell = True           # shop_sells 的答案

    def shop_sells(self, tid):
        return self.sell

    def run_full_supply(self, mv, sc, say=None, back_to=None, potions=None,
                        potion_only=False):
        self.trips.append({"back_to": back_to, "potions": potions,
                           "potion_only": potion_only})
        if say:
            say("補藥水中…")
        return True, "HP藥水+40"


class InlineThread:
    """補給背景執行緒改成同步跑，測試才是決定性的。"""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)

    def is_alive(self):
        return False


class FakeSC:
    def _read_bytes(self, addr, n):
        return None

    def alive(self):
        return True


ROBOT = FakeRobot()
SUPPLY = FakeSupply()
# ⚠ 假物件要 patch 進「用到它的模組」的命名空間（farm_tab）
farm_tab.robot = ROBOT
farm_tab.supply = SUPPLY
farm_tab.threading = types.SimpleNamespace(Thread=InlineThread)

TICK = farm_tab.TRAIN_GAP + 0.1


def build_page():
    sc = FakeSC()
    page = farm_tab.CharFarmPage(
        1234, 0, "t", sc, lambda pid, full=False: True,
        farm_tab.TargetWorker(sc), farm_tab.KeyWorker(0, sc),
        account="acct", char_name="小狐")
    # 練技用到的遊戲讀取，全部換成決定性的假資料
    page._ensure_mover = lambda: True
    page._mover = types.SimpleNamespace(active=True)
    page.cur_scene = lambda: 122
    page.my_pos = lambda: (10.0, 20.0)
    page.inv = 0x1000
    page._sync_castwatch = lambda: None
    page._keys.begin_learning = lambda: None
    page._buff.armed = True            # _on_toggle(on) 別去 arm 真的分身
    page._summon.armed = True
    page.notices = []
    page.notify = lambda msg: page.notices.append(msg)
    return page


def tick(page, n=1):
    for _ in range(n):
        page._train_tick(TICK)


print("① 開練技 → 主開關＋原地重複練習技能都開")
page = build_page()
page.train_cb.setChecked(True)
check("主開關開了", ROBOT.run is True)
check("練習技能開了", ROBOT.ex is True)
check("精靈自己的回城補給觸發關掉了",
      ("disable_return_supply",) in ROBOT.calls)
check("購買清單保證有翼×50", ("ensure_buy", farm_tab.recall.RECALL_ITEM,
                              50) in ROBOT.calls)

print("② 互斥：練技中開掛機 → 練技放掉、精靈關回去")
page.run_cb.setChecked(True)
check("練技勾勾被放掉", not page.train_cb.isChecked())
check("精靈主開關關回去", ROBOT.run is False)
page.run_cb.setChecked(False)
page.train_cb.setChecked(True)          # 開回來給下面的場景用
check("反向：開練技把掛機放掉", not page.run_cb.isChecked())

print("③ 遊戲重開自己把練習技能關了 → 看門狗推回開")
ROBOT.ex = False                        # 模擬遊戲重開清掉
ROBOT.run = False
tick(page)
check("練習技能推回開", ROBOT.ex is True)
check("主開關推回開", ROBOT.run is True)

print("④ 藥水見底 → 關主開關跑 potion_only 補給 → 回來開回去")
ROBOT.dry = [("HP", "HP藥水（高效紅藥水）")]
tick(page)
check("補給趟跑了", len(SUPPLY.trips) == 1)
if SUPPLY.trips:
    t = SUPPLY.trips[0]
    check("只跑補給商那站（potion_only）", t["potion_only"] is True)
    check("回程點＝練技原地（不是巡邏點）",
          t["back_to"] == (10.0, 20.0, 122), f"實得 {t['back_to']}")
    check("帶了藥水購買計畫", t["potions"] == {"HP": [4836], "MP": []})
check("出發前主開關關了", ("set_run", False) in ROBOT.calls)
check("進入補給狀態", page._train_supply is True)
ROBOT.dry = []                          # 買到了
tick(page)                              # 收結果
check("收工：離開補給狀態", page._train_supply is False)
check("回來主開關開回去", ROBOT.run is True)
check("煞車歸零", page._train_dry_trips == 0)
page.inv = 0x1000                       # _drop_cached_addrs 清掉了，補回來

print("⑤ 非商店藥水 → 通知＋自動關閉")
SUPPLY.sell = False
ROBOT.dry = [("HP", "HP藥水（活動紅藥水）")]
tick(page)
check("練技自動關閉", not page.train_cb.isChecked())
check("有通知而且說了沒賣",
      any("沒賣" in m for m in page.notices), f"實得 {page.notices}")
check("沒有多跑補給趟", len(SUPPLY.trips) == 1)
check("精靈主開關關掉（收尾）", ROBOT.run is False)

print("⑥ 連續兩趟回來還是見底 → 通知＋自動關閉")
SUPPLY.sell = True
page = build_page()
ROBOT.calls.clear()
ROBOT.dry = [("MP", "MP藥水（藍藥水）")]
ROBOT.run = ROBOT.ex = False
page.train_cb.setChecked(True)
tick(page)                              # 第 1 趟出發
tick(page)                              # 收結果：還是見底 → 煞車 1
check("第一趟後煞車＝1（還不停）", page._train_dry_trips == 1
      and page.train_cb.isChecked())
page.inv = 0x1000
tick(page)                              # 第 2 趟出發
tick(page)                              # 收結果：還是見底 → 煞車 2 → 停
check("第二趟後自動關閉", not page.train_cb.isChecked())
check("有「還是見底」通知",
      any("還是見底" in m for m in page.notices), f"實得 {page.notices}")

print("⑦ 沒回程道具連續確認 → 通知＋自動關閉")
page = build_page()
ROBOT.dry = [("HP", "HP藥水（高效紅藥水）")]
ROBOT.wing = ()                         # 確定沒有翼
n_trips = len(SUPPLY.trips)
page.train_cb.setChecked(True)
tick(page, farm_tab.NO_RECALL_TRIES)
check("到次數就自動關閉", not page.train_cb.isChecked())
check("有「找不到回程道具」通知",
      any("回程道具" in m for m in page.notices), f"實得 {page.notices}")
check("一趟都沒出發（沒翼不出發）", len(SUPPLY.trips) == n_trips)
ROBOT.wing = (5, 30)
ROBOT.dry = []

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print(f"OK：全部通過")
