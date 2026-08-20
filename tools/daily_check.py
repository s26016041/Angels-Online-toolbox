"""領取每日離線測試 —— offscreen Qt＋假遊戲層，驗 daily_tab 的兩顆兌換鈕與翅膀表。

驗的規格（2026-08-20 使用者要的新功能）：
    ① 全部換取翔宇聖翼 → 送的是「券→翔宇聖翼」那筆，翔宇聖翼變多
    ② 全部換取導引之翼 → 送的是「券→導引之翼」那筆（★不是沿用上一輪的編號）
    ③ ⚠⚠ 暫停在掃描途中改按另一顆 → 一定要重新掃，不准把上一輪掃到的
       兌換編號拿來送（送出去不會失敗，只會安靜地換錯東西 —— 券就沒了）
    ④ 找不到那筆兌換（改版動過表）→ 一包都不送
    ⑤ 當前翅膀數量：每台一列＋合計；讀不到寫「讀不到」**不准寫 0**、
       沒進遊戲寫「未進遊戲」
    ⑥ 領取在線獎勵照舊（回歸）

用法：py tools\\daily_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication          # noqa: E402

APP = QApplication.instance() or QApplication([])

from app.game import exchange as real_exchange      # noqa: E402
from app.game import itemname                       # noqa: E402
from app.tabs import daily_tab                      # noqa: E402

FAILS: list[str] = []

TOKEN = daily_tab.TOKEN_ITEM
WING = daily_tab.WING_ITEM
GUIDE = daily_tab.GUIDE_ITEM


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


# --- 假遊戲層 ----------------------------------------------------------
class Client:
    """一台分身：背包內容、讀不讀得到、進沒進遊戲。"""

    def __init__(self, pid, name, tokens=3, guide=0, wing=0,
                 readable=True, in_game=True):
        self.pid, self.name = pid, name
        self.items = {TOKEN: tokens, GUIDE: guide, WING: wing}
        self.readable, self.in_game = readable, in_game
        self.claims: list[int] = []       # 送出去的領獎格編號


CLIENTS: dict[int, Client] = {}


class FakeSC:
    def __init__(self, pid):
        self.pid = pid

    def close(self):
        pass


class FakeScanner(FakeSC):
    def __init__(self):
        super().__init__(0)

    def open(self, pid):
        self.pid = pid


class Item:
    def __init__(self, type_id, count):
        self.type_id, self.count = type_id, count


class FakeBag:
    MAX_SLOTS = 4096

    @staticmethod
    def scan(sc, first=0, last=0):
        c = CLIENTS.get(sc.pid)
        if c is None or not c.readable:
            return [], False
        return [Item(t, n) for t, n in c.items.items() if n], True

    @staticmethod
    def player_entity(sc):
        c = CLIENTS.get(sc.pid)
        return 0x1000 if (c and c.in_game) else None


class FakeMover:
    active = True


class FakeMove:
    Mover = FakeMover

    @staticmethod
    def acquire(pid, path, owner):
        return FakeMover()

    @staticmethod
    def release(pid, owner):
        pass


class FakeExchange:
    """兌換層替身：finder 分兩拍回答（第一拍 None，模擬還沒掃完）。"""

    Entry = real_exchange.Entry
    # 兩筆真的長得不一樣的兌換 —— 送錯編號就會被下面的斷言抓到
    ENTRIES = {
        WING: real_exchange.Entry(id=39, group=580, addr=0,
                                  rewards=((WING, 5),), materials=((TOKEN, 1),)),
        GUIDE: real_exchange.Entry(id=77, group=581, addr=0,
                                   rewards=((GUIDE, 1),), materials=((TOKEN, 1),)),
    }

    def __init__(self):
        self.missing: set[int] = set()    # 這些獎賞「表裡找不到」
        self.sent: list[tuple[int, int, int]] = []   # (pid, 兌換編號, 次數)
        self.opened: list[tuple[int, int]] = []      # (pid, 群組)

    def finder(self, sc, reward, material):
        yield None                        # 還沒掃完，讓 GUI 喘一口氣
        ent = self.ENTRIES.get(reward)
        if ent is None or reward in self.missing or material != TOKEN:
            return                        # 掃完沒找到 → StopIteration
        yield ent

    def open_shop(self, mover, group):
        self.opened.append((getattr(mover, "pid", 0), group))
        return True

    def confirm(self, mover, sc, entry_id, times):
        ent = next((e for e in self.ENTRIES.values() if e.id == entry_id), None)
        if ent is None:
            return False, f"沒有這筆兌換 {entry_id}"
        c = CLIENTS[sc.pid]
        self.sent.append((sc.pid, entry_id, times))
        c.items[ent.material[0]] -= times * ent.material[1]
        c.items[ent.reward[0]] += times * ent.reward[1]
        return True, ""

    def close_window(self, mover, sc):
        return True, ""


class FakeDailyGift:
    REWARD_IDS = (1, 2, 3, 4, 5, 6)

    @staticmethod
    def reward_ids(sc):
        return FakeDailyGift.REWARD_IDS

    @staticmethod
    def claim(mover, rid):
        return True


class Win:
    def __init__(self, pid, title):
        self.pid, self.title = pid, title
        self.class_name = "_MIDAGEONL_"


EX = FakeExchange()
daily_tab.bag = FakeBag
daily_tab.move = FakeMove
daily_tab.exchange = EX
daily_tab.dailygift = FakeDailyGift
daily_tab.MemoryScanner = FakeScanner
daily_tab.locate = types.SimpleNamespace(warm=lambda sc: None)
daily_tab.injector = types.SimpleNamespace(process_path=lambda pid: "angel.dat")
daily_tab.charname = types.SimpleNamespace(
    account_from_title=lambda t: t.split("-")[-1].strip())
daily_tab.preload = types.SimpleNamespace(
    name_of=lambda pid, *a, **kw: CLIENTS[pid].name if pid in CLIENTS else "")
daily_tab.win = types.SimpleNamespace(
    enumerate_windows=lambda title_contains=None: [
        Win(c.pid, f"Angels Online - {c.name}") for c in CLIENTS.values()])


def setup(*clients: Client) -> "daily_tab.DailyTab":
    """換一組分身、清掉全域兌換快取，回一個乾淨的分頁。"""
    CLIENTS.clear()
    for c in clients:
        CLIENTS[c.pid] = c
    daily_tab._entries.clear()
    EX.sent.clear()
    EX.opened.clear()
    return daily_tab.DailyTab()


def pump(page, limit=400) -> None:
    """把整串步驟跑完（QTimer 在沒有事件迴圈時不會自己跑，這裡手動推）。"""
    for _ in range(limit):
        if not page._steps:
            break
        page._tick()
    page._tick()                          # 最後一拍收尾（_finish）


# ----------------------------------------------------------------------
print("① 全部換取翔宇聖翼")
a, b = Client(101, "小狐", tokens=3), Client(102, "小白", tokens=0)
page = setup(a, b)
page.ex_btn.click()
pump(page)
check("送的是翔宇聖翼那筆（編號 39）",
      [s[1] for s in EX.sent] == [39], f"實得 {EX.sent}")
check("換到 3×5 個翔宇聖翼", a.items[WING] == 15, f"實得 {a.items[WING]}")
check("券扣光", a.items[TOKEN] == 0)
check("沒券那台完全不開店", 102 not in [p for p, _g in EX.opened])
check("導引之翼沒被動到", a.items[GUIDE] == 0)

print("② 全部換取導引之翼")
a = Client(101, "小狐", tokens=4)
page = setup(a)
page.ex_guide_btn.click()
pump(page)
check("送的是導引之翼那筆（編號 77）",
      [s[1] for s in EX.sent] == [77], f"實得 {EX.sent}")
check("開的是導引之翼的商店群組 581",
      [g for _p, g in EX.opened] == [581], f"實得 {EX.opened}")
check("換到 4 個導引之翼", a.items[GUIDE] == 4, f"實得 {a.items[GUIDE]}")
check("翔宇聖翼沒被動到", a.items[WING] == 0)

print("③ ⚠⚠ 掃到一半暫停 → 改按另一顆：一定要重掃，不准送上一輪的編號")
a = Client(101, "小狐", tokens=2)
page = setup(a)
page.ex_btn.click()                       # 翔宇聖翼：第一拍 finder 只回 None
check("停在掃描途中（還沒找到）", daily_tab._entries.get(WING) is None)
page.pause_btn.click()                    # 暫停 → 主按鈕亮回來
page.ex_guide_btn.click()                 # 改按導引之翼
pump(page)
check("導引之翼的編號是自己掃出來的",
      daily_tab._entries.get(GUIDE) is not None
      and daily_tab._entries[GUIDE].id == 77)
check("沒有把翔宇聖翼那筆記成導引之翼",
      daily_tab._entries.get(WING) is None, f"實得 {daily_tab._entries}")
check("送出去的是 77 不是 39", [s[1] for s in EX.sent] == [77],
      f"實得 {EX.sent}")
check("背包真的多了導引之翼", (a.items[GUIDE], a.items[WING]) == (2, 0))

print("④ 表裡找不到那筆兌換（改版動過）→ 一包都不送")
a = Client(101, "小狐", tokens=5)
page = setup(a)
EX.missing = {GUIDE}
page.ex_guide_btn.click()
pump(page)
EX.missing = set()
check("沒送出任何兌換", EX.sent == [], f"實得 {EX.sent}")
check("沒開任何商店", EX.opened == [], f"實得 {EX.opened}")
check("券原封不動", a.items[TOKEN] == 5)
# ⚠ 收工訊息不准把警告蓋掉（曾經蓋掉，看起來像正常收工）
check("狀態列說找不到", "找不到" in page.status.text(), page.status.text())
check("紀錄表也留了一列警告",
      any("找不到" in (page.log.item(0, c).text() or "") for c in range(4)),
      f"實得 {[page.log.item(0, c).text() for c in range(4)]}")

print("⑤ 當前翅膀數量")
a = Client(101, "小狐", guide=7, wing=2)
b = Client(102, "小白", guide=1, wing=30)
c = Client(103, "登入中", readable=False, in_game=False)
d = Client(104, "壞掉", readable=False, in_game=True)
page = setup(a, b, c, d)
page.wings_btn.click()
tbl = page._wing_table
rows = [tuple(tbl.item(r, col).text() for col in range(3))
        for r in range(tbl.rowCount())]
check("每台一列＋合計列", len(rows) == 5, f"實得 {rows}")
check("欄位標題是兩種翼的真名",
      (tbl.horizontalHeaderItem(1).text(),
       tbl.horizontalHeaderItem(2).text()) == ("導引之翼", "翔宇聖翼"))
check("小狐 7／2", rows[0][1:] == ("7", "2"), f"實得 {rows[0]}")
check("小白 1／30", rows[1][1:] == ("1", "30"), f"實得 {rows[1]}")
check("沒進遊戲寫「未進遊戲」不是 0", rows[2][1:] == ("未進遊戲", "未進遊戲"),
      f"實得 {rows[2]}")
check("進遊戲卻讀不到 → 大聲說讀不到，不准寫 0",
      all("讀不到" in x for x in rows[3][1:]), f"實得 {rows[3]}")
check("合計只加讀得到的（8／32）",
      rows[4] == ("合計", "8", "32"), f"實得 {rows[4]}")
a.items[GUIDE] = 99
page._fill_wing_counts()
check("重新整理會重讀", page._wing_table.item(0, 1).text() == "99")

print("⑥ 領取在線獎勵（回歸）")
a, b = Client(101, "小狐"), Client(102, "小白")
page = setup(a, b)
page.claim_btn.click()
pump(page)
check("兩台都送滿 6 格", page._sent == {101: 6, 102: 6}, f"實得 {page._sent}")
check("按鈕恢復可按", page.claim_btn.isEnabled()
      and page.ex_guide_btn.isEnabled())

print("⑦ 寫死的道具編號在資源包表裡對得上")
check("82050 = 導引之翼", itemname.of(GUIDE) == "導引之翼",
      f"實得 {itemname.of(GUIDE)!r}")
check("25832 = 翔宇聖翼", itemname.of(WING) == "翔宇聖翼",
      f"實得 {itemname.of(WING)!r}")

print()
if FAILS:
    print(f"✘ {len(FAILS)} 項沒過：" + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
