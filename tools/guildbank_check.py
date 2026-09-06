"""公會倉庫 —— 離線測試（不碰遊戲；小視窗那段用 offscreen Qt）。

    py tools\\guildbank_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的規格（2026-09-06 使用者定）：
    ① 清單存 config、勾一下就存檔（config.set 要接 save）
    ② 只列／只存「能存」的：綁定／不可交易／不可存倉庫（itemflags 三旗）不列不送；
       表裡沒那筆也不送
    ③ 開到的不是公會倉（CUR_BANK_TYPE≠1）→ 一件都不送、關窗
    ④ 送了沒進去：單件＝跳過換下一件；連續 FAIL_STREAK 件＝倉庫滿 → 關窗、訊息寫明、
       回 True（安靜，不當失敗）
    ⑤ 小視窗兩張表：左＝背包裡能存的、右＝清單；子字串過濾兩邊一起、加入／移除改 config、
       右邊只有名字＋圖示（圖示查表，這台沒有的也有）、列高留夠圖示
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

from app.game import guildbank, itemflags, supply           # noqa: E402

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


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


CFG = FakeConfig()
guildbank.config = CFG
guildbank.time = Clock()
# 假旗標表：1905 天使之翼可存、66 可存、137 不可交易+不可存倉庫、500 裝備綁定、999 不在表裡
itemflags._table = {1905: (0, 11), 66: (0, 12), 137: (3, 0), 500: (4, 0), 4837: (0, 13)}
check("icon_of 查表（4837 → 13）、不在表裡回 0", itemflags.icon_of(4837) == 13 and itemflags.icon_of(999) == 0)

print("① 清單存 config，勾一下就 save")
guildbank.set_wanted([66, "1905", 66])
check("寫進去的是去重排序的 int list", CFG.d[guildbank.CFG_KEY] == [66, 1905],
      str(CFG.d))
check("有 save（config.set 不寫檔）", CFG.saves == 1, f"saves={CFG.saves}")
CFG.d[guildbank.CFG_KEY] = [66, "x", None, 1905]
check("壞值丟掉", guildbank.wanted() == {66, 1905}, str(guildbank.wanted()))

print()
print("② 只列／只存能存的")
BAG = [FItem(20, 1, 1905, 3), FItem(21, 2, 137, 1), FItem(22, 3, 500, 1),
       FItem(23, 4, 999, 1), FItem(24, 5, 66, 9), FItem(25, 6, 66, 4)]
COMPLETE = [True]
guildbank.bag = types.SimpleNamespace(scan=lambda sc: (list(BAG), COMPLETE[0]),
                                      Item=FItem)
ok, no, complete = guildbank.candidates(None)
check("能存＝天使之翼＋兩格低效紅藥水", [i.type_id for i in ok] == [1905, 66, 66],
      str([i.type_id for i in ok]))
check("不能存＝不可交易／裝備綁定／不在表裡", [i.type_id for i in no] == [137, 500, 999],
      str([i.type_id for i in no]))
pend = guildbank.pending(None, {66, 137, 999})
check("pending 只挑清單上且能存的", [i.serial for i in pend] == [5, 6], str(pend))
COMPLETE[0] = False
check("背包沒讀完整 → None（不是空）", guildbank.pending(None, {66}) is None)
COMPLETE[0] = True

print()
print("③ 開到的不是公會倉 → 一件都不送")
closes = []
sent = []
guildbank.supply = types.SimpleNamespace(
    BANK_WND=supply.BANK_WND, TALK_BANK_USE=supply.TALK_BANK_USE,
    MAX_DEPOSIT=supply.MAX_DEPOSIT, DEPOSIT_POLL=2, DEPOSIT_WAIT=0.1,
    NPC_TABLE=supply.NPC_TABLE,
    _wnd_open=lambda m, s, n: False,
    _bank_close=lambda m, s: closes.append(1),
    _engage_npc=lambda m, s, nid, fb, codes, wnd: codes == [supply.TALK_BANK_USE, 11],
    _dist_to_npc=lambda s, nid: 1.0,
    deposit_slot=lambda m, s, slot: (sent.append(slot) or (True, "")),
    _item_gone=lambda s, serial: serial in GONE,
)
GONE: set[int] = set()
MOVER = types.SimpleNamespace(active=True)
guildbank.is_open = lambda sc: False
ok, msg = guildbank.run(MOVER, None, 1890, (129, 168), {66, 1905})
check("回 False 且訊息說不是公會倉", ok is False and "不是公會倉庫" in msg, msg)
check("⛔ 一包都沒送", sent == [], str(sent))
check("有關窗", closes == [1])
guildbank.is_open = lambda sc: None
ok, msg = guildbank.run(MOVER, None, 1890, (129, 168), {66, 1905})
check("讀不到種類也不送", ok is False and sent == [] and "讀不到" in msg, msg)

print()
print("④ 存：單件沒進去跳過、連續兩件沒進去＝滿")
guildbank.is_open = lambda sc: True
closes.clear()
sent.clear()


def _deposit(m, s, slot):
    sent.append(slot)
    it = next(i for i in BAG if i.slot == slot)
    if it.serial != 5:                       # 序號 5 那格存不進去（拒收）
        GONE.add(it.serial)
        BAG[:] = [i for i in BAG if i.serial != it.serial]
    return True, ""


guildbank.supply.deposit_slot = _deposit
ok, msg = guildbank.run(MOVER, None, 1890, (129, 168), {66, 1905})
check("回 True（不當失敗）", ok is True, msg)
check("送的順序＝清單上的三格，拒收那格只送一次", sent == [20, 24, 25], str(sent))
check("訊息：存了 2 件、跳過那件有寫", "存了 2 件" in msg and "跳過" in msg, msg)
check("關窗一次", closes == [1])
# 滿：全部都存不進去
BAG[:] = [FItem(20, 1, 1905, 3), FItem(24, 5, 66, 9), FItem(25, 6, 66, 4)]
GONE.clear()
sent.clear()
closes.clear()
guildbank.supply.deposit_slot = lambda m, s, slot: (sent.append(slot) or (True, ""))
ok, msg = guildbank.run(MOVER, None, 1890, (129, 168), {66, 1905})
check("連續 FAIL_STREAK 件沒進去就停（不會把清單磨完）", len(sent) == guildbank.FAIL_STREAK,
      str(sent))
check("訊息寫「滿了」、回 True（安靜）", ok is True and "滿了" in msg, msg)
check("關窗", closes == [1])
# 上一步個人倉窗還開著 → 先關再講話
closes.clear()
guildbank.supply._wnd_open = lambda m, s, n: True
BAG[:] = [FItem(20, 1, 1905, 3)]
GONE.clear()
guildbank.supply.deposit_slot = lambda m, s, slot: (GONE.add(1) or BAG.clear() or (True, ""))
ok, msg = guildbank.run(MOVER, None, 1890, (129, 168), {1905})
check("窗開著先關一次再講話（關窗共 2 次）", closes == [1, 1] and ok, f"{closes} {msg}")

print()
print("⑤ 小視窗（兩張表）")
from PySide6.QtCore import QSize                                  # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402
from app.tabs import guildbank_dialog                             # noqa: E402

app = QApplication.instance() or QApplication([])
BAG[:] = [FItem(20, 1, 1905, 3, 11), FItem(24, 5, 66, 9, 12), FItem(25, 6, 66, 4, 12),
          FItem(21, 2, 137, 1)]
CFG.d[guildbank.CFG_KEY] = [66, 4837]         # 4837 清單上但不在這台背包
guildbank_dialog.guildbank = guildbank
guildbank_dialog.itemname = types.SimpleNamespace(
    label=lambda tid, c=None: {1905: "天使之翼", 66: "低效紅藥水", 4837: "高效藍藥水"}.get(tid, f"種類 {tid}"))
dlg = guildbank_dialog.GuildBankDialog(None, object(), "測試")


def texts(lst):
    return [lst.item(i).text() for i in range(lst.count())]


check("左邊＝背包裡能存、還沒在清單上的（不可交易那件不列）", texts(dlg.bag_list) == ["天使之翼 ×3"],
      str(texts(dlg.bag_list)))
check("右邊＝清單：只有名字（含這台沒有的），照名字排",
      texts(dlg.want_list) == ["低效紅藥水", "高效藍藥水"], str(texts(dlg.want_list)))
check("右邊的種類＝config", dlg.wanted_ids() == {66, 4837}, str(dlg.wanted_ids()))
check("列高留夠（圖示 32 不被裁）", dlg.want_list.item(0).sizeHint().height() >= 32 + 8,
      str(dlg.want_list.item(0).sizeHint()))
dlg.search.setText("藥")
hidden = ([dlg.bag_list.item(i).isHidden() for i in range(dlg.bag_list.count())],
          [dlg.want_list.item(i).isHidden() for i in range(dlg.want_list.count())])
check("打「藥」：兩邊一起過濾", hidden == ([True], [False, False]), str(hidden))
dlg.search.setText("")
CFG.saves = 0
dlg.bag_list.item(0).setSelected(True)
dlg._add()                                                # 加入天使之翼
check("加入 → 寫 config＋save、左邊消失右邊出現",
      CFG.d[guildbank.CFG_KEY] == [66, 1905, 4837] and CFG.saves == 1
      and texts(dlg.bag_list) == [] and "天使之翼" in texts(dlg.want_list),
      f"{CFG.d.get(guildbank.CFG_KEY)} saves={CFG.saves} {texts(dlg.bag_list)} {texts(dlg.want_list)}")
row = next(dlg.want_list.item(i) for i in range(dlg.want_list.count())
           if dlg.want_list.item(i).text() == "高效藍藥水")
row.setSelected(True)
dlg._remove()                                             # 拿掉這台背包沒有的那個
check("移除這台背包沒有的也存得掉", CFG.d[guildbank.CFG_KEY] == [66, 1905],
      str(CFG.d.get(guildbank.CFG_KEY)))
dlg.close()

print()
if FAILS:
    print(f"FAIL {len(FAILS)}：" + "、".join(FAILS))
    sys.exit(1)
print("OK")
