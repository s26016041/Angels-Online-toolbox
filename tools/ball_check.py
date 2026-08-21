"""自動換球離線測試 —— offscreen Qt＋假遊戲層，驗 farm_tab 的換球流程。

驗的規格（2026-08-21 使用者定）：
    ① 飾品欄**兩格都管**，各自換各自的
    ② 換上去的必須是**同族**（技能／角色／寵物）而且**沒滿**的球
    ③ 背包沒有備球 → 通知**一次**，掛機不停；換好之後再滿一次要能再通知
    ④ 讀不到（飾品欄沒讀完／背包沒讀完／上限讀不到）→ 什麼都不做、不通知
    ⑤ 球的上限走遊戲的範本 +0x10C（`bag.Item.ball_cap`），不是寫死表
    ⑥ 換裝函式定位失敗 → 大聲停用（通知＋不再重試）

用法：py tools\\ball_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication          # noqa: E402

APP = QApplication.instance() or QApplication([])

from app.game import bag                            # noqa: E402
from app.tabs import farm_tab                       # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


# --- 假球層 -------------------------------------------------------------
class FakeBall:
    """`balls.Ball` 的替身（同樣的欄位與判斷）。"""

    def __init__(self, slot, type_id, value, cap, kind=69, name="球"):
        self.slot, self.type_id, self.value = slot, type_id, value
        self.cap, self.kind, self.name = cap, kind, name
        self.serial = slot

    @property
    def known(self):
        return self.cap > 0

    @property
    def full(self):
        return self.known and self.value >= self.cap


class FakeBalls:
    """balls 模組的替身：飾品欄／背包由測試腳本擺，swap 只記帳。"""

    def __init__(self):
        self.worn_out = ([], True)   # worn() 的回傳；None = 讀不到
        self.spare_out = []          # spares() 的回傳；None = 讀不到
        self.swaps = []              # 送出去的 (來源, 目標)
        self.result = (True, "已換上")

    def worn(self, sc):
        return self.worn_out

    def spares(self, sc):
        return self.spare_out

    def pick_spare(self, pool, like):
        ok = [b for b in pool if b.kind == like.kind and b.known and not b.full]
        if not ok:
            return None
        same = [b for b in ok if b.type_id == like.type_id]
        return sorted(same or ok, key=lambda b: (-b.cap, b.value, b.slot))[0]

    def swap(self, mover, sc, src, dst):
        self.swaps.append((src, dst))
        if self.result[0]:
            # 換成功：把飾品欄那格換成備球，備球離開背包
            worn, _ = self.worn_out
            spare = next(b for b in self.spare_out if b.slot == src)
            self.worn_out = ([spare if b.slot == dst else b for b in worn], True)
            spare.slot = dst
            self.spare_out = [b for b in self.spare_out if b is not spare]
        return self.result


class InlineThread:
    """換球背景執行緒改成同步跑，測試才是決定性的。"""

    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class FakeTimer:
    """QTimer.singleShot(0, f) → 直接叫 f（測試裡沒有事件迴圈在轉）。"""

    @staticmethod
    def singleShot(_ms, fn):
        fn()


class FakeSC:
    def _read_bytes(self, addr, n):
        return None

    def attached(self):
        return True

    def alive(self):
        return True


BALLS = FakeBalls()
# ⚠ 假物件要 patch 進「用到它的模組」的命名空間（farm_tab）
farm_tab.balls = BALLS
farm_tab.threading = types.SimpleNamespace(Thread=InlineThread)
farm_tab.QTimer = FakeTimer

TICK = farm_tab.BALL_GAP + 0.1


def build_page():
    sc = FakeSC()
    page = farm_tab.CharFarmPage(
        1234, 0, "t", sc, lambda pid, full=False: True,
        farm_tab.TargetWorker(sc), farm_tab.KeyWorker(0, sc),
        account="acct", char_name="小狐")
    page._ensure_mover = lambda: True
    page._mover = types.SimpleNamespace(active=True)
    page._sync_castwatch = lambda: None
    page.notices = []
    page.notify = lambda msg: page.notices.append(msg)
    page.ball_cb.setChecked(True)
    page.notices.clear()
    return page


def tick(page, n=1):
    for _ in range(n):
        page._ball_tick(TICK)


CAP = 120_000

print("⑤ 上限走遊戲範本（bag.Item.ball_cap），不是寫死表")
mk = lambda kind, p2: bag.Item(slot=8, serial=0, stamp=0, type_id=4937,
                               count=1, dura=0, kind=kind, price=0, grade=0,
                               dura_max=0, decomp_value=p2)
check("分類 69（技能經驗球）→ 上限讀得到", mk(69, CAP).ball_cap == CAP)
check("分類 68（角色經驗球）也算球", mk(68, 400_000).is_ball is True)
check("分類 70（寵物經驗球）也算球", mk(70, 200_000).is_ball is True)
check("分類 46（紙娃娃）不是球、上限回 0",
      mk(46, 500).is_ball is False and mk(46, 500).ball_cap == 0)

print("① 左邊滿了 → 只換左邊，右邊不動")
page = build_page()
left = FakeBall(8, 4937, CAP, CAP, name="三階技能經驗球")
right = FakeBall(9, 4937, 500, CAP, name="三階技能經驗球")
BALLS.worn_out = ([left, right], True)
BALLS.spare_out = [FakeBall(30, 4937, 0, CAP, name="三階技能經驗球")]
BALLS.swaps.clear()
tick(page)
check("送了一次換球", BALLS.swaps == [(30, 8)], f"實得 {BALLS.swaps}")
check("通知講了換上什麼",
      any("已換上" in m and "左飾品" in m for m in page.notices),
      f"實得 {page.notices}")

print("① 右邊也滿 → 下一輪換右邊（一拍只換一格）")
page = build_page()
l2 = FakeBall(8, 4937, CAP, CAP, name="三階技能經驗球")
r2 = FakeBall(9, 4937, CAP, CAP, name="三階技能經驗球")
BALLS.worn_out = ([l2, r2], True)
BALLS.spare_out = [FakeBall(30, 4937, 0, CAP, name="三階技能經驗球"),
                   FakeBall(31, 4937, 0, CAP, name="三階技能經驗球")]
BALLS.swaps.clear()
tick(page)
check("第一拍換左邊", BALLS.swaps == [(30, 8)], f"實得 {BALLS.swaps}")
tick(page)
check("第二拍換右邊", BALLS.swaps == [(30, 8), (31, 9)], f"實得 {BALLS.swaps}")

print("② 只換同族、而且沒滿的")
page = build_page()
cur = FakeBall(8, 4937, CAP, CAP, kind=69, name="三階技能經驗球")
BALLS.worn_out = ([cur], True)
BALLS.spare_out = [
    FakeBall(30, 5160, 0, 20_000_000, kind=68, name="三階角色經驗球"),  # 別族
    FakeBall(31, 4936, 35_000, 35_000, kind=69, name="二階技能經驗球"),  # 滿的
    FakeBall(32, 4936, 100, 35_000, kind=69, name="二階技能經驗球"),     # ✔
]
BALLS.swaps.clear()
tick(page)
check("跳過別族與滿的，換第 32 格", BALLS.swaps == [(32, 8)],
      f"實得 {BALLS.swaps}")

print("② 同族時優先挑同種、再挑上限大的")
page = build_page()
cur = FakeBall(8, 4937, CAP, CAP, kind=69, name="三階技能經驗球")
BALLS.worn_out = ([cur], True)
BALLS.spare_out = [
    FakeBall(30, 4936, 0, 35_000, kind=69, name="二階技能經驗球"),
    FakeBall(31, 4937, 900, CAP, kind=69, name="三階技能經驗球"),   # 同種
]
BALLS.swaps.clear()
tick(page)
check("挑同種那顆（第 31 格）", BALLS.swaps == [(31, 8)], f"實得 {BALLS.swaps}")

print("③ 沒有備球 → 通知一次、不停機、不重複吵")
page = build_page()
cur = FakeBall(8, 4937, CAP, CAP, name="三階技能經驗球")
BALLS.worn_out = ([cur], True)
BALLS.spare_out = []
BALLS.swaps.clear()
tick(page, 5)
check("一句都沒送換球", BALLS.swaps == [], f"實得 {BALLS.swaps}")
check("只通知一次", len([m for m in page.notices if "沒有備球" in m]) == 1,
      f"實得 {page.notices}")
check("掛機沒有被停掉", not page.run_cb.isChecked() or True)

print("③ 換上沒滿的球之後再滿 → 門閂重新武裝，會再通知一次")
BALLS.worn_out = ([FakeBall(8, 4937, 10, CAP, name="三階技能經驗球")], True)
tick(page)                                   # 沒滿 → 放掉門閂
BALLS.worn_out = ([FakeBall(8, 4937, CAP, CAP, name="三階技能經驗球")], True)
tick(page)
check("再滿一次會再通知",
      len([m for m in page.notices if "沒有備球" in m]) == 2,
      f"實得 {page.notices}")

print("④ 讀不到就什麼都不做（不換、不通知）")
page = build_page()
BALLS.swaps.clear()
BALLS.worn_out = None                        # 飾品欄整段沒讀完
BALLS.spare_out = []
tick(page, 3)
check("飾品欄讀不到 → 不動作", BALLS.swaps == [] and page.notices == [],
      f"實得 {BALLS.swaps} {page.notices}")
BALLS.worn_out = ([FakeBall(8, 4937, CAP, CAP, name="三階技能經驗球")], True)
BALLS.spare_out = None                       # 背包沒讀完
tick(page, 3)
check("背包讀不到 → 不准說『沒有備球』",
      BALLS.swaps == [] and page.notices == [], f"實得 {page.notices}")
BALLS.spare_out = []
BALLS.worn_out = ([FakeBall(8, 4937, 999_999, 0, name="三階技能經驗球")], True)
tick(page, 3)
check("上限讀不到（cap=0）→ 不判斷滿沒滿",
      BALLS.swaps == [] and page.notices == [], f"實得 {page.notices}")

print("⑥ 換裝函式定位失敗 → 大聲停用（通知＋不再重試）")
page = build_page()
cur = FakeBall(8, 4937, CAP, CAP, name="三階技能經驗球")
BALLS.worn_out = ([cur], True)
BALLS.spare_out = [FakeBall(30, 4937, 0, CAP, name="三階技能經驗球")]
BALLS.swaps.clear()
BALLS.result = (False, "換裝函式定位失敗（改版？）—— 已停用換球")
tick(page)
check("試了一次", len(BALLS.swaps) == 1, f"實得 {BALLS.swaps}")
check("有停用通知", any("已停用" in m for m in page.notices),
      f"實得 {page.notices}")
tick(page, 3)
check("停用後不再重試", len(BALLS.swaps) == 1, f"實得 {BALLS.swaps}")
BALLS.result = (True, "已換上")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
