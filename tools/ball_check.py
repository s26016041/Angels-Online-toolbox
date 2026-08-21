"""自動換球離線測試 —— offscreen Qt＋假遊戲層，驗 farm_tab 的換球流程。

驗的規格（2026-08-21 使用者定）：
    ① **飾品欄裝著的球全部都滿了才動**，一次全換掉；
       只有一顆滿、或根本沒裝球 → 什麼都不做、也不通知
    ② 換上去的必須是**同族**（技能／角色／寵物）而且**沒滿**的球；
       兩格一起配對，同一顆備球不會被兩格重複認領
    ③ 備球不夠 → 去天使商城買（買 → 從商城倉庫領進背包）→ 下一輪才換
    ④ 商城買不到 → 通知**一次**，掛機不停；一個「都滿了」事件最多買一輪
    ⑤ 讀不到（飾品欄沒讀完／背包沒讀完／上限讀不到）→ 什麼都不做、不通知
    ⑥ 球的上限走遊戲的範本 +0x10C（`bag.Item.ball_cap`），不是寫死表
    ⑦ 換裝函式定位失敗 → 大聲停用（通知＋不再重試）

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
from app.game import balls as real_balls            # noqa: E402
from app.tabs import farm_tab                       # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


CAP = 120_000


class FakeBall:
    """`balls.Ball` 的替身（同樣的欄位與判斷）。"""

    def __init__(self, slot, type_id, value, cap=CAP, kind=69, name="三階技能經驗球"):
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
    """balls 模組的替身。配對邏輯借**真的**那一份，只有 I/O 換成假的。"""

    pick_spare = staticmethod(real_balls.pick_spare)
    pick_spares = staticmethod(real_balls.pick_spares)

    def __init__(self):
        self.worn_out = ([], True)   # worn() 的回傳；None = 讀不到
        self.spare_out = []          # spares() 的回傳；None = 讀不到
        self.swaps = []              # 送出去的 (來源, 目標)
        self.result = (True, "已換上")

    def worn(self, sc):
        return self.worn_out

    def spares(self, sc):
        return self.spare_out

    def swap(self, mover, sc, src, dst):
        """記帳並把假背包／假飾品欄照著換。

        兩種來源都要支援：背包備球 → 飾品欄（真流程），以及飾品欄左右對調
        （測試鈕在球還沒滿的時候用的那條）。
        """
        self.swaps.append((src, dst))
        if not self.result[0]:
            return self.result
        worn = list(self.worn_out[0])
        spare = next((b for b in self.spare_out if b.slot == src), None)
        if spare is not None:                      # 背包 → 飾品欄
            self.worn_out = ([spare if b.slot == dst else b for b in worn], True)
            spare.slot = dst
            self.spare_out = [b for b in self.spare_out if b is not spare]
            return self.result
        a = next((b for b in worn if b.slot == src), None)
        b2 = next((b for b in worn if b.slot == dst), None)
        if a is not None and b2 is not None:       # 飾品欄左右對調
            a.slot, b2.slot = dst, src
            self.worn_out = (worn, True)
        return self.result


class FakeGoods:
    def __init__(self, mall_id, type_id, count, price, name="三階技能經驗球"):
        self.mall_id, self.type_id = mall_id, type_id
        self.count, self.price, self.name = count, price, name


class FakeMall:
    """商城層替身：賣什麼、買不買得成、領不領得出來都由測試腳本擺。"""

    def __init__(self):
        self.sells = {4937: FakeGoods(363, 4937, 1, 45)}
        self.buys = []               # 送出去的商城編號
        self.takes = []              # 領出來的流水號
        self.buy_ok = True
        self.take_ok = True
        self._serial = 1000
        self.store = []              # [(流水號, 道具編號, 數量)]
        self.on_take = None          # 領成功時的副作用（把球塞進背包）

    def cheapest(self, sc, type_id):
        return self.sells.get(type_id)

    def buy(self, mover, sc, g):
        self.buys.append(g.mall_id)
        if not self.buy_ok:
            return False, "送出了但商城倉庫沒有多出東西（點數不足？）"
        self._serial += 1
        self.store.append((self._serial, g.type_id, g.count))
        return True, f"已買到「{g.name}」×{g.count}（{g.price} 點）"

    def storage(self, sc):
        return list(self.store)

    def take(self, mover, sc, serial, type_id):
        self.takes.append(serial)
        if not self.take_ok:
            return False, "送出了但東西還在商城倉庫（背包滿了？）"
        self.store = [r for r in self.store if r[0] != serial]
        if self.on_take:
            self.on_take(type_id)
        return True, "已把球領進背包"


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


class FakeBox:
    """QMessageBox 替身：記下講了什麼，問句一律回「否」（測試不送封包）。"""

    Yes = 2
    No = 4
    said: list[str] = []

    @staticmethod
    def warning(_p, _t, text):
        FakeBox.said.append(text)

    @staticmethod
    def information(_p, _t, text):
        FakeBox.said.append(text)

    # PySide6 的旗標常數：程式碼會傳 `QMessageBox.Yes | QMessageBox.No`，
    # 假物件要吃得下（真的介面長什麼樣，替身就要長什麼樣 —— 這正是
    # attached 被寫成方法那次的教訓）。
    Ok = 1

    @staticmethod
    def question(_p, _t, text, _buttons=None, _default=None):
        FakeBox.said.append(text)
        return FakeBox.No


class FakeSC:
    def _read_bytes(self, addr, n):
        return None

    # ⚠ MemoryScanner.attached 是 **property** 不是方法 —— 假物件寫成方法
    #   的話，程式碼裡誤寫成 `sc.attached()` 也照樣過測試，真的跑才炸
    #   （2026-08-21 實際踩到：TypeError: 'bool' object is not callable）。
    @property
    def attached(self):
        return True

    def alive(self):
        return True


BALLS = FakeBalls()
MALL = FakeMall()
# ⚠ 假物件要 patch 進「用到它的模組」的命名空間（farm_tab）
farm_tab.balls = BALLS
farm_tab.mall = MALL
farm_tab.threading = types.SimpleNamespace(Thread=InlineThread)
farm_tab.QTimer = FakeTimer
farm_tab.QMessageBox = FakeBox

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
    BALLS.swaps.clear()
    MALL.buys.clear()
    MALL.takes.clear()
    MALL.store.clear()
    MALL.buy_ok = MALL.take_ok = True
    BALLS.result = (True, "已換上")
    return page


def tick(page, n=1):
    for _ in range(n):
        page._ball_tick(TICK)


print("⑥ 上限走遊戲範本（bag.Item.ball_cap），不是寫死表")
mk = lambda kind, p2: bag.Item(slot=8, serial=0, stamp=0, type_id=4937,
                               count=1, dura=0, kind=kind, price=0, grade=0,
                               dura_max=0, decomp_value=p2)
check("分類 69（技能經驗球）→ 上限讀得到", mk(69, CAP).ball_cap == CAP)
check("分類 68（角色經驗球）也算球", mk(68, 400_000).is_ball is True)
check("分類 70（寵物經驗球）也算球", mk(70, 200_000).is_ball is True)
check("分類 46（紙娃娃）不是球、上限回 0",
      mk(46, 500).is_ball is False and mk(46, 500).ball_cap == 0)

print("① 只有一顆滿 → 完全不動作、不通知")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, 500)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0), FakeBall(31, 4937, 0)]
tick(page, 3)
check("一句換球都沒送", BALLS.swaps == [], f"實得 {BALLS.swaps}")
check("沒有去商城買", MALL.buys == [], f"實得 {MALL.buys}")
check("一則通知都沒有", page.notices == [], f"實得 {page.notices}")

print("① 飾品欄根本沒裝球 → 完全不動作（使用者：沒用球就不用管）")
page = build_page()
BALLS.worn_out = ([], True)
BALLS.spare_out = [FakeBall(30, 4937, 0)]
tick(page, 3)
check("沒換沒買沒通知",
      BALLS.swaps == [] and MALL.buys == [] and page.notices == [],
      f"實得 {BALLS.swaps} {MALL.buys} {page.notices}")

print("① 兩顆都滿 → 一次換兩顆")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0), FakeBall(31, 4937, 0)]
tick(page)
check("同一拍送了兩包", sorted(BALLS.swaps) == [(30, 8), (31, 9)],
      f"實得 {BALLS.swaps}")
check("同一顆備球沒有被兩格重複認領",
      len({s for s, _ in BALLS.swaps}) == 2, f"實得 {BALLS.swaps}")
check("通知講了換上什麼",
      any("已換上" in m for m in page.notices), f"實得 {page.notices}")

print("② 只換同族、沒滿的")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP)], True)
BALLS.spare_out = [
    FakeBall(30, 5160, 0, 20_000_000, kind=68, name="三階角色經驗球"),   # 別族
    FakeBall(31, 4936, 35_000, 35_000, kind=69, name="二階技能經驗球"),  # 滿的
    FakeBall(32, 4936, 100, 35_000, kind=69, name="二階技能經驗球"),     # ✔
]
tick(page)
check("跳過別族與滿的，換第 32 格", BALLS.swaps == [(32, 8)],
      f"實得 {BALLS.swaps}")

print("② 同族時優先挑同種")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP)], True)
BALLS.spare_out = [FakeBall(30, 4936, 0, 35_000, name="二階技能經驗球"),
                   FakeBall(31, 4937, 900)]
tick(page)
check("挑同種那顆（第 31 格）", BALLS.swaps == [(31, 8)], f"實得 {BALLS.swaps}")

print("③ 備球不夠 → 去商城買、領進背包、再換上")
page = build_page()
w = [FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)]
BALLS.worn_out = (w, True)
BALLS.spare_out = [FakeBall(30, 4937, 0)]          # 只有一顆備球，缺一顆
nxt = [40]


def _restock(type_id):                             # 領進背包＝多一顆備球
    BALLS.spare_out.append(FakeBall(nxt[0], type_id, 0))
    nxt[0] += 1


MALL.on_take = _restock
tick(page)
check("去商城買了一次", MALL.buys == [363], f"實得 {MALL.buys}")
check("有從商城倉庫領出來", len(MALL.takes) == 1, f"實得 {MALL.takes}")
check("補完就換了兩顆", len(BALLS.swaps) == 2, f"實得 {BALLS.swaps}")
check("通知同時講了花幾點與換上什麼",
      any("點）" in m and "已換上" in m for m in page.notices),
      f"實得 {page.notices}")
MALL.on_take = None

print("④ 商城買不到 → 通知一次、不重複燒點數、掛機不停")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
BALLS.spare_out = []
MALL.buy_ok = False
tick(page, 5)
check("只買過一次（門閂擋住重複花點數）", len(MALL.buys) == 1,
      f"實得 {MALL.buys}")
check("只通知一次", len(page.notices) == 1, f"實得 {page.notices}")
check("掛機沒有被停掉", not page.run_cb.isChecked())

print("④ 球換掉之後（不再全滿）門閂重新武裝")
BALLS.worn_out = ([FakeBall(8, 4937, 10), FakeBall(9, 4937, 10)], True)
tick(page)                                   # 沒全滿 → 放掉門閂
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
tick(page)
check("再滿一次會再買／再通知", len(MALL.buys) == 2, f"實得 {MALL.buys}")
MALL.buy_ok = True

print("③ 商城根本沒賣這顆 → 通知一次，一毛點數都不花")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 5160, 100, 100, kind=68,
                            name="三階角色經驗球")], True)
BALLS.spare_out = []
tick(page, 4)
check("沒送過購買", MALL.buys == [], f"實得 {MALL.buys}")
check("只通知一次", len(page.notices) == 1, f"實得 {page.notices}")
check("訊息講得出是商城查不到",
      any("商城查不到" in m for m in page.notices), f"實得 {page.notices}")

print("⑤ 讀不到就什麼都不做（不換、不買、不通知）")
page = build_page()
BALLS.worn_out = None                        # 飾品欄整段沒讀完
BALLS.spare_out = []
tick(page, 3)
check("飾品欄讀不到 → 不動作",
      BALLS.swaps == [] and MALL.buys == [] and page.notices == [],
      f"實得 {BALLS.swaps} {MALL.buys} {page.notices}")
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
BALLS.spare_out = None                       # 背包沒讀完
tick(page, 3)
check("背包讀不到 → 不准說『沒有備球』、更不准去買",
      BALLS.swaps == [] and MALL.buys == [] and page.notices == [],
      f"實得 {MALL.buys} {page.notices}")
BALLS.spare_out = []
BALLS.worn_out = ([FakeBall(8, 4937, 999_999, 0),
                   FakeBall(9, 4937, 999_999, 0)], True)
tick(page, 3)
check("上限讀不到（cap=0）→ 不判斷滿沒滿",
      BALLS.swaps == [] and MALL.buys == [] and page.notices == [],
      f"實得 {page.notices}")

print("⑦ 換裝函式定位失敗 → 大聲停用（通知＋不再重試）")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0)]
BALLS.result = (False, "換裝函式定位失敗（改版？）—— 已停用換球")
tick(page)
check("試了一次", len(BALLS.swaps) == 1, f"實得 {BALLS.swaps}")
check("有停用通知", any("已停用" in m for m in page.notices),
      f"實得 {page.notices}")
tick(page, 3)
check("停用後不再重試", len(BALLS.swaps) == 1, f"實得 {BALLS.swaps}")
BALLS.result = (True, "已換上")

print("⑧ 「測試換球」鈕：不論狀態都不能炸（真的踩過 sc.attached 誤當方法）")
for label, worn, spare in (
        ("兩顆沒滿", ([FakeBall(8, 4937, 10), FakeBall(9, 4937, 20)], True),
         [FakeBall(30, 4937, 0)]),
        ("兩顆都滿有備球", ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)],
                            True), [FakeBall(30, 4937, 0),
                                    FakeBall(31, 4937, 0)]),
        ("飾品欄沒球", ([], True), []),
        ("飾品欄讀不到", None, []),
        ("背包讀不到", ([FakeBall(8, 4937, 10)], True), None)):
    page = build_page()
    BALLS.worn_out, BALLS.spare_out = worn, spare
    MALL.store.append((999, 2017, 1))       # 倉庫有東西 → 會問「順便測領取嗎」
    FakeBox.said.clear()
    try:
        page._test_ball_swap()
        ok, why = True, ""
    except Exception as exc:                # noqa: BLE001
        ok, why = False, repr(exc)
    check(f"{label} → 不丟例外", ok, why)
    if ok and worn is not None and worn[0]:
        check(f"{label} → 有畫面可看", bool(FakeBox.said), "一句話都沒說")
check("問了『要不要順便測領取』就不會擅自送包（回否 → 沒領）",
      MALL.takes == [], f"實得 {MALL.takes}")
check("『要不要測購買』也是回否就不扣點（沒送過購買）",
      MALL.buys == [], f"實得 {MALL.buys}")
check("購買問句有把價錢寫進去",
      any("點" in t and "商城編號" in t for t in FakeBox.said),
      f"實得 {FakeBox.said}")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
