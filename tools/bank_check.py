"""存個人倉庫清單 —— 離線測試（不碰遊戲；小視窗那段用 offscreen Qt）。

    py tools\\bank_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的規格（2026-09-23 使用者定）：
    ① 清單存 config（`bank.items`）、勾一下就存檔；能存＝表沒「不可存倉庫」（不可交易／綁定不擋）
    ② supply.run_bank 改吃這張清單：清單空不去、每件送前重掃、序號沒走＝倉庫滿→關窗
       ⛔ 不再讀遊戲補給頁的處理清單（deposit_targets／AS_HANDLE 已拆）
    ③ 小視窗：左＝背包裡能存、右＝清單；**別張清單上的種類左邊不列**（三張互斥）；
       兩台以上有「角色」下拉，切了就換讀那台的背包
⚠ 純離線：假 config／假背包／假 supply 那幾支，**只換 I/O，判斷邏輯跑真的**。
"""
from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.game import bank, itemflags, supply       # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class FakeConfig:
    def __init__(self):
        self.d = {}
        self.saves = 0

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v

    def save(self):
        self.saves += 1


@dataclass
class FItem:
    slot: int
    serial: int
    type_id: int
    count: int = 1
    icon_id: int = 0

    @property
    def name(self):
        return f"物品{self.type_id}"


CFG = FakeConfig()
bank.config = CFG
# 假旗標表：1905 可存、66 可存、137 不可交易+不可存倉庫、500 裝備綁定（個人倉庫收）、
# 4837 可存、999 不在表裡
itemflags._table = {1905: (0, 11), 66: (0, 12), 137: (3, 0), 500: (4, 0), 4837: (0, 13),
                    77: (itemflags.NO_TRADE, 0)}

print("① 清單存 config；能存只看「不可存倉庫」")
bank.set_wanted([66, "1905", 66])
check("寫進去的是去重排序的 int list", CFG.d[bank.CFG_KEY] == [66, 1905], str(CFG.d))
check("有 save", CFG.saves == 1, f"saves={CFG.saves}")
CFG.d[bank.CFG_KEY] = [66, "x", None, 1905]
check("壞值丟掉", bank.wanted() == {66, 1905}, str(bank.wanted()))
BAG = [FItem(20, 1, 1905, 3), FItem(21, 2, 137, 1), FItem(22, 3, 500, 1), FItem(23, 4, 999, 1),
       FItem(24, 5, 66, 9), FItem(26, 7, 77, 2)]
COMPLETE = [True]
bank.bag = types.SimpleNamespace(scan=lambda sc: (list(BAG), COMPLETE[0]), Item=FItem)
ok, no, complete = bank.candidates(None)
check("能存＝可存的＋裝備綁定＋不可交易（個人倉庫都收）", [i.type_id for i in ok] == [1905, 500, 66, 77],
      str([i.type_id for i in ok]))
check("不能存＝不可存倉庫／不在表裡", [i.type_id for i in no] == [137, 999], str([i.type_id for i in no]))
pend = bank.pending(None, {66, 137, 999})
check("pending 只挑清單上且能存的", [i.serial for i in pend] == [5], str(pend))
COMPLETE[0] = False
check("背包沒讀完整 → None（不是空）", bank.pending(None, {66}) is None)
COMPLETE[0] = True
check("清單 None／空 → []", bank.pending(None, None) == [] and bank.pending(None, set()) == [])

print()
print("② supply.run_bank 吃清單")
check("⛔ 遊戲處理清單的讀法已拆掉",
      not hasattr(supply, "deposit_targets") and not hasattr(supply, "AS_HANDLE_NAMES")
      and not hasattr(supply, "run_bank_here"))
closes: list[int] = []
sent: list[int] = []
GONE: set[int] = set()
engaged: list[list[int]] = []
supply._engage_npc = lambda m, s, nid, fb, codes, wnd: (engaged.append(codes) or True)
supply._bank_close = lambda m, s: closes.append(1)
supply._dist_to_npc = lambda s, nid: 1.0
supply._nap = lambda t: None
supply.DEPOSIT_POLL = 2
supply.bag = bank.bag
supply._item_gone = lambda s, serial: serial in GONE
MOVER = types.SimpleNamespace(active=True)


def _deposit(m, s, slot):
    sent.append(slot)
    it = next(i for i in BAG if i.slot == slot)
    if it.serial != 5:                        # 序號 5 那格存不進去（倉庫滿）
        GONE.add(it.serial)
        BAG[:] = [i for i in BAG if i.serial != it.serial]
    return True, ""


supply.deposit_slot = _deposit
ok, msg = supply.run_bank(MOVER, None, 1890, (129, 168), set())
check("清單空：不講話、不送", ok is True and engaged == [] and sent == [], msg)
ok, msg = supply.run_bank(MOVER, None, 1890, (129, 168), None)
check("清單 None 也一樣", ok is True and engaged == [] and sent == [], msg)
BAG[:] = [FItem(20, 1, 1905, 3), FItem(21, 2, 137, 1), FItem(24, 5, 66, 9), FItem(22, 3, 500, 1)]
ok, msg = supply.run_bank(MOVER, None, 1890, (129, 168), {1905, 500, 66, 137})
check("講話碼＝我要用倉庫→自己的倉庫", engaged == [[supply.TALK_BANK_USE, supply.TALK_BANK_SELF]],
      str(engaged))
check("清單上能存的照格號送；不可存倉庫的 137 不送", sent[:2] == [20, 22] or sent[:2] == [20, 24], str(sent))
check("序號 5 沒走＝倉庫滿 → 停、關窗、訊息寫滿了", ok is True and "滿" in msg and closes == [1], msg)
check("不可存倉庫那件還在、沒被送", any(i.type_id == 137 for i in BAG) and 21 not in sent, str(sent))
sent.clear(); closes.clear(); GONE.clear()
BAG[:] = [FItem(20, 1, 1905, 3), FItem(22, 3, 500, 1)]
ok, msg = supply.run_bank(MOVER, None, 1890, (129, 168), {1905, 500})
check("全存完：存了 2 件、關窗", ok is True and "存了 2 件" in msg and closes == [1] and BAG == [], msg)

print()
print("③ 小視窗：三張互斥、切角色")
from PySide6.QtWidgets import QApplication                        # noqa: E402
from app.tabs import bank_dialog, itemlist_dialog                 # noqa: E402

app = QApplication.instance() or QApplication([])
itemlist_dialog.itemname = types.SimpleNamespace(
    label=lambda tid, c=None: {1905: "天使之翼", 66: "低效紅藥水", 4837: "高效藍藥水",
                               500: "華麗駱駝", 77: "綁定石"}.get(tid, f"種類 {tid}"))
# 兩台：A 的背包／B 的背包各一套（scanner 物件當鑰匙）
SC_A, SC_B = object(), object()
BAGS = {SC_A: [FItem(20, 1, 1905, 3, 11), FItem(24, 5, 66, 9, 12), FItem(22, 3, 500, 1, 14)],
        SC_B: [FItem(20, 1, 4837, 2, 13), FItem(21, 2, 77, 1, 15)]}
bank.bag = types.SimpleNamespace(scan=lambda sc: (list(BAGS[sc]), True), Item=FItem)
CFG.d[bank.CFG_KEY] = [66]
bank_dialog.guildbank = types.SimpleNamespace(wanted=lambda: {500})     # 華麗駱駝在公會清單
bank_dialog.discard = types.SimpleNamespace(wanted=lambda: {77})        # 綁定石在丟棄清單
dlg = bank_dialog.BankDialog(None, SC_A, "A", clients=[("A", SC_A, None), ("B", SC_B, None)])


def texts(lst):
    return [lst.item(i).text() for i in range(lst.count())]


check("左邊：在自己清單的（66）與別張清單的（500 華麗駱駝）都不列", texts(dlg.bag_list) == ["天使之翼 ×3"],
      str(texts(dlg.bag_list)))
check("右邊＝自己的清單", texts(dlg.want_list) == ["低效紅藥水"], str(texts(dlg.want_list)))
check("摘要有寫「已在別張清單」", "已在別張清單 1 種" in dlg.summary.text(), dlg.summary.text())
check("有角色下拉、預設選 A", dlg.char_combo is not None and dlg.char_combo.currentText() == "A")
dlg.char_combo.setCurrentIndex(1)
check("切到 B：左邊換成 B 的背包，丟棄清單上的綁定石不列", texts(dlg.bag_list) == ["高效藍藥水 ×2"],
      str(texts(dlg.bag_list)))
check("標題跟著換", dlg.windowTitle().endswith("B"), dlg.windowTitle())
CFG.saves = 0
dlg.bag_list.item(0).setSelected(True)
dlg._add()
check("在 B 加入 → 寫同一張 config", CFG.d[bank.CFG_KEY] == [66, 4837] and CFG.saves == 1,
      f"{CFG.d.get(bank.CFG_KEY)} saves={CFG.saves}")
check("沒有測試鈕", not hasattr(dlg, "test_btn"))
dlg.close()
one = bank_dialog.BankDialog(None, SC_A, "A", clients=[("A", SC_A, None)])
check("只有一台就沒有下拉", one.char_combo is None)
one.close()

print()
if FAILS:
    print(f"FAIL {len(FAILS)}：" + "、".join(FAILS))
    sys.exit(1)
print("OK")
