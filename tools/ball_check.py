"""自動換球離線測試 —— offscreen Qt＋假遊戲層，驗**掛機頁與生產頁**的換球。

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
    ⑧ 商城購買記在**自己那張表**（點數），不汙染商店那張（金幣）
    ⑨⑩⑪ 官方 5 秒動作節流＋「沒成功就補送」（含指令槽忙碌那種沒送出去的）
    ⑫ **精靈開著**（自動練技／自動採集）→ 先關主開關＋按 ESC，換完開回去；
       精靈沒開就一個開關都不要動；中途炸掉也一定要把主開關還回去
    ⑬ 自動生產那一頁走**同一套**規則與同一份程式碼

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
from app.game import mall as real_mall              # noqa: E402
from app.game import actiongate                     # noqa: E402
from app.game import ballswap                       # noqa: E402
from app.tabs import farm_tab                       # noqa: E402
from app.tabs import produce_tab                    # noqa: E402

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

    def swap(self, mover, sc, src, dst, say=None):
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
    """商城層替身：賣什麼、買不買得成、領不領得出來都由測試腳本擺。

    ⚠ 常數也要跟真的模組一致（畫面會拿 ACTION_GAP 算「大概要跑幾秒」）——
      替身少一個屬性，真的跑起來就是 AttributeError。
    """

    ACTION_GAP = actiongate.ACTION_GAP
    ACTION_TRIES = actiongate.TRIES

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

    # ★ 動手前的預檢（倉庫滿／背包沒空格／商城沒賣）。預設不擋。
    blocked_why = None
    is_loaded = True
    reqs = []

    def blocked(self, sc, need, type_id):
        return self.blocked_why

    def loaded(self, sc):
        return self.is_loaded

    def request_data(self, mover, sc, say=None):
        self.reqs.append(1)
        self.is_loaded = True            # 要過就有了（實機：0 → 425 筆）
        return True, "商城資料已載入（425 筆商品）"

    def buy(self, mover, sc, g, say=None):
        self.buys.append(g.mall_id)
        if not self.buy_ok:
            return False, "送出了但商城倉庫沒有多出東西（點數不足？）"
        self._serial += 1
        self.store.append((self._serial, g.type_id, g.count))
        return True, f"已買到「{g.name}」×{g.count}（{g.price} 點）"

    def storage(self, sc):
        return list(self.store)

    def take(self, mover, sc, serial, type_id, say=None):
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
    """QTimer 替身。

    · `singleShot(0, f)` → 直接叫 f（測試裡沒有事件迴圈在轉）
    · 也要能**當類別用**（`QTimer(self)` + `.timeout.connect` + `start/stop`）
      —— produce_tab 的看門狗就是這樣建的；替身少一半介面就建不出頁面。
    """

    class _Sig:
        def connect(self, fn):
            pass

    def __init__(self, *a, **k):
        self.timeout = FakeTimer._Sig()

    def start(self, *a):
        pass

    def stop(self):
        pass

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

    answer = None            # 問句要回什麼（預設「否」，測試才不會亂送封包）

    @staticmethod
    def question(_p, _t, text, _buttons=None, _default=None):
        FakeBox.said.append(text)
        return FakeBox.answer if FakeBox.answer is not None else FakeBox.No


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
# ★★ 假的只換**碰遊戲的那幾支**（讀飾品欄／背包、送封包、買、領），
#    配對與整條流程（`balls.pick_spares` / `run_swap` / `restock`、
#    `ballswap.swap_with_pause`）一律跑**真的** —— 那才是要驗的東西。
#    ⚠ 上一版把整個模組換成替身，重構之後就變成「只測到替身」（21 項假綠）。
_REAL_SWAP = real_balls.swap
real_balls.worn = BALLS.worn
real_balls.spares = BALLS.spares
real_balls.swap = BALLS.swap
real_mall.cheapest = MALL.cheapest
real_mall.buy = MALL.buy
real_mall.storage = MALL.storage
real_mall.take = MALL.take
real_mall.blocked = MALL.blocked
real_mall.loaded = MALL.loaded
real_mall.request_data = MALL.request_data
# 精靈：預設沒開（純掛機）；要驗「練技／採集要先停精靈」時再打開。
ROBOT = types.SimpleNamespace(
    run=False, calls=[],
    is_run=lambda sc: ROBOT.run,
    set_run=lambda mv, sc, on: (ROBOT.calls.append(("set_run", bool(on))),
                                setattr(ROBOT, "run", bool(on)),
                                (True, ""))[2])
ballswap.robot = ROBOT
ballswap.win = types.SimpleNamespace(
    send_key=lambda hwnd, vk: ROBOT.calls.append(("esc", vk)))
ballswap.time = types.SimpleNamespace(sleep=lambda s: None)
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
    real_balls.swap = BALLS.swap     # ⑩ 會換成真的那支，這裡換回來
    page.hwnd = 4242                 # 有視窗才送得出 ESC
    ROBOT.run = False
    ROBOT.calls.clear()
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

print("④ 補貨失敗**不是永久放棄**：冷卻過了要再試（2026-08-22）")
MALL.buys.clear()
page._ball_retry_at = 0.0            # 模擬冷卻時間到了
tick(page)
check("冷卻過了會再試一次", len(MALL.buys) == 1, f"實得 {MALL.buys}")
check("但不會再吵第二次", len(page.notices) == 1, f"實得 {page.notices}")
check("失敗後有排下一次重試",
      page._ball_retry_at > 0, f"實得 {page._ball_retry_at}")
MALL.buys.clear()

print("④ 球換掉之後（不再全滿）門閂重新武裝")
BALLS.worn_out = ([FakeBall(8, 4937, 10), FakeBall(9, 4937, 10)], True)
tick(page)                                   # 沒全滿 → 放掉門閂
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
tick(page)
check("再滿一次會再買／再通知", len(MALL.buys) == 1, f"實得 {MALL.buys}")
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

print("⑧ 商城購買紀錄：跟商店那張分開，幣別是點數")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
BALLS.spare_out = []
nxt2 = [50]


def _restock2(type_id):
    BALLS.spare_out.append(FakeBall(nxt2[0], type_id, 0))
    nxt2[0] += 1


MALL.on_take = _restock2
tick(page)
check("買了兩顆", len(MALL.buys) == 2, f"實得 {MALL.buys}")
check("都領進背包了", len(MALL.takes) == 2, f"實得 {MALL.takes}")
check("兩格都換上了", len(BALLS.swaps) == 2, f"實得 {BALLS.swaps}")
check("商城紀錄記了兩筆", len(page._mall_buys) == 2,
      f"實得 {page._mall_buys}")
check("記的是點數（45×2）", sum(r[4] for r in page._mall_buys) == 90,
      f"實得 {page._mall_buys}")
check("⚠ 沒有汙染商店那張（金幣）", page._purchases == [],
      f"實得 {page._purchases}")
dlg = page._mall_buys_dialog()
check("商城表列數對", dlg._tbl.rowCount() == 2, f"實得 {dlg._tbl.rowCount()}")
check("總額用『點』不是金幣",
      "點" in dlg._head.text() and "金幣" not in dlg._head.text(),
      f"實得 {dlg._head.text()}")
check("表頭有商城編號那一欄",
      dlg._tbl.horizontalHeaderItem(1).text() == "商城編號")
_empty = build_page()._mall_buys_dialog()
check("沒紀錄時也畫得出來（空表不當掉）", _empty._tbl.rowCount() == 0)
MALL.on_take = None

print("⑨ 動作節流（官方兩次動作要隔 5 秒）＋失敗重送")


class FakeClock:
    """把 time.sleep 換成「時鐘直接跳」，測試才不用真的等 6 秒。"""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.slept.append(secs)
        self.now += secs


class ScanStub:
    pid = 4321


CLOCK = FakeClock()
actiongate.time = CLOCK
actiongate._last.clear()
SC = ScanStub()

# ①「隔太近」要被擋著等 —— 第一發不等，第二發要等滿 ACTION_GAP
actiongate.gate(SC)
first = list(CLOCK.slept)
actiongate.gate(SC)
check("第一次動作不用等", first == [], f"實得 {first}")
check(f"第二次要等滿 {actiongate.ACTION_GAP} 秒",
      CLOCK.slept and abs(CLOCK.slept[-1] - actiongate.ACTION_GAP) < 0.01,
      f"實得 {CLOCK.slept}")
check("官方常數就是『超過 5 秒』，我們取 6 留裕度",
      actiongate.ACTION_GAP > 5.0)

# ② 重送之前一定先確認「上一發其實沒成功」（不然買東西會重複扣點）
actiongate._last.clear()
sent = []
state = {"done": False}


def _done_true_after_first():
    return state["done"]


def _build_ok():
    sent.append(1)
    state["done"] = True                     # 這一發其實成功了
    return actiongate.SENT, "", lambda: "成功"


ok, msg = actiongate.retry(SC, _done_true_after_first, _build_ok, None, "測試")
check("送一次就成功", ok and len(sent) == 1, f"實得 {ok} {sent}")

actiongate._last.clear()
sent.clear()
state["done"] = True                          # 呼叫前就已經完成了
ok, msg = actiongate.retry(SC, _done_true_after_first,
                           lambda: (sent.append(1), (actiongate.SENT, "", lambda: "x"))[1],
                           None, "測試")
check("動手前發現已經完成 → 一包都不送（重試安全的關鍵）",
      ok and sent == [], f"實得 {ok} {sent}")

# ③ 一直沒生效 → 送滿 ACTION_TRIES 次才放棄
actiongate._last.clear()
sent.clear()
ok, msg = actiongate.retry(SC, lambda: False,
                           lambda: (sent.append(1), (actiongate.SENT, "", lambda: "x"))[1],
                           None, "測試")
check(f"沒生效就重送，共 {actiongate.TRIES} 次",
      not ok and len(sent) == actiongate.TRIES, f"實得 {ok} {sent}")

# ④「讀不到」不算成功也不算失敗
actiongate._last.clear()
sent.clear()
ok, msg = actiongate.retry(SC, lambda: None,
                           lambda: (sent.append(1), (actiongate.SENT, "", lambda: "x"))[1],
                           None, "測試")
check("done() 回 None（讀不到）不會被當成成功", not ok, f"實得 {ok} {msg}")

# ⑤ 連送都送不出去（不是節流）→ 立刻回報，不要白等三輪
actiongate._last.clear()
sent.clear()
ok, msg = actiongate.retry(SC, lambda: False,
                           lambda: (sent.append(1),
                                    (actiongate.STOP, "跳板沒接上", None))[1],
                           None, "測試")
check("『沒救』就立刻回報（只試一次）",
      not ok and len(sent) == 1 and "跳板" in msg, f"實得 {ok} {sent} {msg}")

# ⑥ 指令槽忙碌那種「根本沒送出去」→ 要重送，不能放棄（使用者 2026-08-21 定）
actiongate._last.clear()
sent.clear()
tries = {"n": 0}


def _busy_then_ok():
    tries["n"] += 1
    sent.append(1)
    if tries["n"] < 2:
        return actiongate.RETRY, "換裝指令排不進去（指令槽忙碌）", None
    state["done"] = True
    return actiongate.SENT, "", lambda: "成功"


state["done"] = False
ok, msg = actiongate.retry(SC, _done_true_after_first, _busy_then_ok,
                           None, "測試")
check("指令槽忙碌（根本沒送出去）→ 下一輪重送，最後成功",
      ok and len(sent) == 2, f"實得 {ok} {sent} {msg}")

print("⑩ 換球被擋就重送；已經換好的**不准再送**（再送一次會換回去）")


class FakeMover:
    """跳板替身：記下送出的 (來源, 目標)，並依腳本決定第幾發才生效。"""

    active = True

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    lock = _Lock()

    def __init__(self, effective_on=1):
        self.sent = []
        self.effective_on = effective_on      # 第幾發才真的生效

    def call_sync(self, fn, a=0, b=0, ecx=0, timeout=0.0):
        self.sent.append((a, b))
        if len(self.sent) >= self.effective_on:
            # 遊戲換裝＝把兩格的**內容**互換（指標不動）
            SERIALS[a], SERIALS[b] = SERIALS.get(b), SERIALS.get(a)
        return 1


# ⚠ 換裝的身分是**序號**不是指標（實機量過：指標不動、內容互換）。
#   替身要照著模擬，不然又會測到一個「跟真的不一樣」的世界。
real_balls.swap = _REAL_SWAP                  # ⑩ 驗的是**真的** swap
PTRS = {8: 111, 9: 222, 30: 333, 31: 444}
SERIALS = {8: 900008, 9: 900009, 30: 900030, 31: 900031}
real_balls._ptr_of = lambda sc, slot: PTRS.get(slot)
real_balls._serial_of = lambda sc, slot: SERIALS.get(slot)

actiongate._last.clear()
mv = FakeMover(effective_on=1)                # 第一發就成功
ok, msg = real_balls.swap(mv, SC, 30, 8)
check("一發就成功 → 只送一包（不會再送把它換回去）",
      ok and len(mv.sent) == 1, f"實得 {ok} {mv.sent}")

actiongate._last.clear()
mv = FakeMover(effective_on=2)                # 第一發被丟掉，第二發才生效
ok, msg = real_balls.swap(mv, SC, 31, 9)
check("第一發被擋 → 重送後成功（就是『只換了左飾品』那個 bug）",
      ok and len(mv.sent) == 2, f"實得 {ok} {mv.sent}")

actiongate._last.clear()
mv = FakeMover(effective_on=99)               # 怎麼送都不生效
ok, msg = real_balls.swap(mv, SC, 30, 8)
check(f"一直不生效就送滿 {actiongate.TRIES} 次才放棄",
      not ok and len(mv.sent) == len(real_balls.SWAP_GAPS),
      f"實得 {ok} {mv.sent}")
check("換球跟商城共用同一條隊伍（重送要等滿官方間隔）",
      real_balls.SWAP_GAPS[1] == actiongate.ACTION_GAP)

print("⑪ 送包函式回的是三態字串，不是 bool（回 bool 會被當成沒送出去 → 重買）")


class SendMover:
    """_send() 用得到的最小跳板替身。"""

    active = True

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    lock = _Lock()

    def __init__(self, busy=False):
        self.busy = busy
        self.calls = 0

    def scratch(self):
        return 0x20000

    def write(self, addr, data):
        return True

    def call_sync(self, fn, a=0, b=0, ecx=0, timeout=0.0):
        self.calls += 1
        return None if self.busy else 1


class SendSC:
    pid = 4321

    def _read_bytes(self, addr, n):
        import struct as _s
        return bytearray(_s.pack("<I", 0x30000))      # 都當成合理指標


st, why = real_mall._send(SendMover(), SendSC(), 0x12B, 7, bytes(5))
check("送成功要回 actiongate.SENT（不是 True）", st == actiongate.SENT,
      f"實得 {st!r}")
check("⚠ 不可以是 bool（bool 會讓每一發都被當成沒送出去）",
      not isinstance(st, bool), f"實得 {type(st).__name__}")
st, why = real_mall._send(SendMover(busy=True), SendSC(), 0x12B, 7, bytes(5))
check("指令槽忙碌要回 RETRY", st == actiongate.RETRY, f"實得 {st!r}")
_keep = real_mall.jumpmap.BUILD_FN
real_mall.jumpmap.BUILD_FN = 0
st, why = real_mall._send(SendMover(), SendSC(), 0x12B, 7, bytes(5))
real_mall.jumpmap.BUILD_FN = _keep
check("位址沒定位要回 STOP（重送沒意義）", st == actiongate.STOP, f"實得 {st!r}")

print("⑫ 精靈開著（自動練技／自動採集）→ 先關主開關＋按 ESC，換完開回去")
page = build_page()
ROBOT.run = True                     # 模擬練技／採集中
ROBOT.calls.clear()
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0), FakeBall(31, 4937, 0)]
tick(page)
check("換球前把主開關關掉", ("set_run", False) in ROBOT.calls,
      f"實得 {ROBOT.calls}")
check("有按 ESC 退出採集／製作狀態",
      ("esc", ballswap.VK_ESCAPE) in ROBOT.calls, f"實得 {ROBOT.calls}")
check("兩格都換了", len(BALLS.swaps) == 2, f"實得 {BALLS.swaps}")
check("換完把主開關開回去", ROBOT.calls[-1] == ("set_run", True),
      f"實得 {ROBOT.calls}")
check("關在換之前、開在換之後（順序對）",
      ROBOT.calls.index(("set_run", False)) < ROBOT.calls.index(("esc", 0x1B))
      < len(ROBOT.calls) - 1, f"實得 {ROBOT.calls}")

print("⑫ 精靈沒開（純掛機）→ 一個開關都不要動")
page = build_page()
ROBOT.run = False
ROBOT.calls.clear()
BALLS.worn_out = ([FakeBall(8, 4937, CAP)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0)]
tick(page)
check("沒碰精靈開關、也沒按 ESC", ROBOT.calls == [], f"實得 {ROBOT.calls}")
check("照樣換好了", len(BALLS.swaps) == 1, f"實得 {BALLS.swaps}")

print("⑫ 換球中途炸掉，也一定要把主開關還回去（finally）")
page = build_page()
ROBOT.run = True
ROBOT.calls.clear()
BALLS.worn_out = ([FakeBall(8, 4937, CAP)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0)]
_boom = BALLS.swap
BALLS.swap = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模擬爆炸"))
real_balls.swap = BALLS.swap
try:
    tick(page)
except Exception:                    # noqa: BLE001
    pass
check("例外之後主開關仍被開回去", ("set_run", True) in ROBOT.calls,
      f"實得 {ROBOT.calls}")
BALLS.swap = _boom
real_balls.swap = _boom

print("⑬ 自動生產那一頁：同一套規則、但要先停精靈按 ESC")
produce_tab.threading = types.SimpleNamespace(Thread=InlineThread)
produce_tab.QTimer = FakeTimer


def build_prod():
    page = produce_tab.CharProducePage(
        1234, 4242, "t", FakeSC(), "acct", "小狐")
    page._ensure_mover = lambda: True
    page._mover = types.SimpleNamespace(active=True)
    page._loading = False
    page.ball_cb.setChecked(True)
    BALLS.swaps.clear()
    MALL.buys.clear()
    MALL.takes.clear()
    MALL.store.clear()
    MALL.buy_ok = MALL.take_ok = True
    BALLS.result = (True, "已換上")
    real_balls.swap = BALLS.swap
    ROBOT.run = True                 # 採集中＝精靈開著
    ROBOT.calls.clear()
    return page


page = build_prod()
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0), FakeBall(31, 4937, 0)]
took = page._ball_tick()
check("接手這一拍（回 True，_gather_tick 後面別做了）", took is True)
check("先關精靈主開關", ("set_run", False) in ROBOT.calls, f"實得 {ROBOT.calls}")
check("有按 ESC", ("esc", ballswap.VK_ESCAPE) in ROBOT.calls,
      f"實得 {ROBOT.calls}")
check("兩格都換了", len(BALLS.swaps) == 2, f"實得 {BALLS.swaps}")
check("換完把精靈開回去", ROBOT.calls[-1] == ("set_run", True),
      f"實得 {ROBOT.calls}")
check("跑完把 _ball_busy 放掉", page._ball_busy is False)

page = build_prod()
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, 5)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0), FakeBall(31, 4937, 0)]
check("只有一顆滿 → 不接手、不動精靈",
      page._ball_tick() is False and ROBOT.calls == [],
      f"實得 {ROBOT.calls}")
check("生產頁也把理由寫在畫面上",
      "要兩顆都滿" in page._ball_lbl.text(), f"實得「{page._ball_lbl.text()}」")

page = build_prod()
BALLS.worn_out = None
check("飾品欄讀不到 → 不接手", page._ball_tick() is False)
check("讀不到也講得出來", "飾品欄讀不到" in page._ball_lbl.text(),
      f"實得「{page._ball_lbl.text()}」")
BALLS.worn_out = ([FakeBall(8, 4937, CAP)], True)
BALLS.spare_out = None
check("背包讀不到 → 不接手、更不會去買",
      page._ball_tick() is False and MALL.buys == [], f"實得 {MALL.buys}")

page = build_prod()
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
BALLS.spare_out = []
MALL.on_take = lambda tid: BALLS.spare_out.append(FakeBall(60 + len(BALLS.spare_out), tid, 0))
page._ball_tick()
check("沒備球 → 去商城買兩顆並記帳", len(page._mall_buys) == 2,
      f"實得 {page._mall_buys}")
check("商城紀錄是點數（45×2）", sum(r[4] for r in page._mall_buys) == 90)
dlg = produce_tab.mall_buys_dialog(page, page._mall_buys, "小狐")
check("生產頁的商城表跟掛機頁同一份", dlg._tbl.rowCount() == 2)
MALL.on_take = None

print("⑭ 每一個「這輪不動作」的出口都要說得出理由（不准安靜跳過）")
page = build_page()
cases = [
    ("沒有開啟", lambda: page.ball_cb.setChecked(False)),
    ("飾品欄讀不到", lambda: setattr(BALLS, "worn_out", None)),
    ("飾品欄沒有裝經驗球", lambda: setattr(BALLS, "worn_out", ([], True))),
    ("讀不到上限", lambda: setattr(
        BALLS, "worn_out", ([FakeBall(8, 4937, 99, 0)], True))),
    ("要兩顆都滿", lambda: setattr(
        BALLS, "worn_out",
        ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, 5)], True))),
    ("背包讀不到", lambda: (setattr(
        BALLS, "worn_out",
        ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)),
        setattr(BALLS, "spare_out", None))),
]
for want, setup in cases:
    page = build_page()
    setup()
    page._ball_t = farm_tab.BALL_GAP
    page._ball_tick(TICK)
    txt = page._ball_lbl.text()
    check(f"「{want}」講得出來", want in txt, f"實得「{txt}」")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, 60_000), FakeBall(9, 4937, 60_000)], True)
BALLS.spare_out = []
page._ball_t = farm_tab.BALL_GAP
page._ball_tick(TICK)
check("沒滿時看得到現在幾分幾", "60,000/120,000" in page._ball_lbl.text(),
      f"實得「{page._ball_lbl.text()}」")

print("⑱ 商城表是空的 → 自己去跟伺服器要，**不准說成「沒有賣」**")
page = build_page()
MALL.is_loaded = False               # 這台沒開過商城，整張表是空的
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
BALLS.spare_out = []
MALL.on_take = lambda tid: BALLS.spare_out.append(
    FakeBall(80 + len(BALLS.spare_out), tid, 0))
tick(page)
check("有去跟伺服器要商城資料", len(MALL.reqs) == 1, f"實得 {MALL.reqs}")
check("要到之後照樣買得成", len(MALL.buys) == 2, f"實得 {MALL.buys}")
check("兩格都換上了", len(BALLS.swaps) == 2, f"實得 {BALLS.swaps}")
check("⚠ 沒有把「表是空的」講成「商城沒有賣這種球」",
      "沒有賣" not in page._ball_lbl.text(), f"實得「{page._ball_lbl.text()}」")
MALL.on_take = None

print("⑰ 動手**之前**就先擋掉會白跑的情況（使用者：商城會不會指令滿）")
for why in ("商城倉庫滿了（10 格），請先去領取",
            "背包只剩 1 格空位，補 2 顆放不下",
            "商城沒有賣這種球"):
    page = build_page()
    MALL.blocked_why = why
    BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
    BALLS.spare_out = []
    tick(page)
    check(f"「{why[:8]}…」→ 一包都不送", MALL.buys == [] and BALLS.swaps == [],
          f"實得 {MALL.buys} {BALLS.swaps}")
    check("而且畫面講得出原因", why[:6] in page._ball_lbl.text(),
          f"實得「{page._ball_lbl.text()}」")
MALL.blocked_why = None

print("⑯ 標籤要跟著跑，不能凍在動作前那句（2026-08-22「文字怪怪的」）")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP), FakeBall(9, 4937, CAP)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0), FakeBall(31, 4937, 0)]
tick(page)
txt = page._ball_lbl.text()
check("成功之後標籤換成結果（不是還停在「→ …」）",
      "✔" in txt and "→ 去商城" not in txt, f"實得「{txt}」")
check("結果講得出換上什麼", "已換上" in txt, f"實得「{txt}」")

page = build_page()
seen_lbl = []
_orig_say = page._ball_say
page._ball_say = lambda t: (seen_lbl.append(t), _orig_say(t))[0]
BALLS.worn_out = ([FakeBall(8, 4937, CAP)], True)
BALLS.spare_out = [FakeBall(30, 4937, 0)]
tick(page)
check("作業期間有把進度寫出去（不會靜默）", bool(seen_lbl),
      f"實得 {seen_lbl}")

print("⑯ 從商城倉庫領回時，不可以說成「買」")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP)], True)
BALLS.spare_out = []
MALL.store.append((888, 4937, 1))
MALL.on_take = lambda tid: BALLS.spare_out.append(FakeBall(71, tid, 0))
tick(page)
res = page._ball_lbl.text()
check("訊息說的是「領回」不是「買」",
      "領回" in res and "買" not in res.split("領回")[0], f"實得「{res}」")
MALL.on_take = None

print("⑮ 商城倉庫已經有現成的 → **直接領，不准再買一次**（省錢的關鍵）")
page = build_page()
BALLS.worn_out = ([FakeBall(8, 4937, CAP)], True)
BALLS.spare_out = []
MALL.store.append((777, 4937, 1))            # 上一輪買成功、領失敗，東西還在倉庫
MALL.on_take = lambda tid: BALLS.spare_out.append(FakeBall(70, tid, 0))
tick(page)
check("一毛錢都沒花", MALL.buys == [], f"實得 {MALL.buys}")
check("直接把倉庫那顆領回來", MALL.takes == [777], f"實得 {MALL.takes}")
check("領完就換上了", len(BALLS.swaps) == 1, f"實得 {BALLS.swaps}")
check("訊息講明沒再花錢",
      any("沒再花錢" in m for m in page.notices), f"實得 {page.notices}")
MALL.on_take = None

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
