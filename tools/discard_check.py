"""自動丟棄 —— 離線測試（不碰遊戲；小視窗那段用 offscreen Qt）。

    py tools\\discard_check.py     （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的規格（2026-09-23 使用者定：跟存公會一樣，但是自動丟棄）：
    ① 清單存 config、勾一下就存檔（config.set 要接 save）
    ② 丟棄封包版面＝代號 0x13、內文 8：u16 格號 + u32 **種類 ID**（照 game.destroyitemslot 反組譯；
       ⚠ 物件 +8 是種類不是序號——第一版抄錯，旅行背包被報成丟不掉）
       建/送走 jumpmap.BUILD_FN/SEND_FN（沒定位就拒送）
    ③ run：每件送前重掃背包、送後 poll 序號消失才算；沒消失＝跳過點名；
       背包讀不完整＝提前停手（不當「沒有」）
    ④ 小視窗：左＝這台背包全部、右＝清單；加入／移除改 config
    ⑤ 掛機設定視窗多兩顆鈕（存公會倉庫／自動丟棄）
⚠ 純離線：假 config／假背包／假跳板，**只換 I/O，判斷邏輯跑真的**。
"""
from __future__ import annotations

import os
import struct
import sys
import threading
import types
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.game import discard, jumpmap, supply     # noqa: E402

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
discard.config = CFG
discard.time = Clock()

print("① 清單存 config，勾一下就 save")
discard.set_wanted([66, "1905", 66])
check("寫進去的是去重排序的 int list", CFG.d[discard.CFG_KEY] == [66, 1905], str(CFG.d))
check("有 save（config.set 不寫檔）", CFG.saves == 1, f"saves={CFG.saves}")
CFG.d[discard.CFG_KEY] = [66, "x", None, 1905]
check("壞值丟掉", discard.wanted() == {66, 1905}, str(discard.wanted()))

print()
print("② 丟棄封包版面")
# 假跳板：scratch 在 0x10000；BUILD_FN 會把「內文指標」寫在 buf+4、「封包指標」寫在 buf+0xC。
MEM: dict[int, bytes] = {}
CALLS: list[tuple] = []
DATA_AT, PKT_AT = 0x20000, 0x30000


class FakeMover:
    active = True
    lock = threading.RLock()

    def scratch(self):
        return 0x10000

    def write(self, addr, data):
        MEM[addr] = bytes(data)
        return True

    def call_sync(self, fn, *args, ecx=None, timeout=None):
        CALLS.append((fn, args, ecx))
        if fn == jumpmap.BUILD_FN:
            MEM[ecx + 4] = struct.pack("<I", DATA_AT)
            MEM[ecx + 0xC] = struct.pack("<I", PKT_AT)
        return 0


class FakeScanner:
    def _read_bytes(self, addr, n):
        if addr == jumpmap.CONN_PTR:
            return struct.pack("<I", 0x40000)
        return MEM.get(addr, b"\0" * n)[:n]


jumpmap.BUILD_FN, jumpmap.SEND_FN, jumpmap.CONN_PTR = 0x50D2C9, 0x739570, 0x9F9428
MV, SC = FakeMover(), FakeScanner()
ok, msg = discard.discard_slot(MV, SC, 44, 0x11223344)
check("送得出去", ok, msg)
check("建包＝(0x13, 8)", CALLS and CALLS[0][0] == jumpmap.BUILD_FN and CALLS[0][1] == (0x13, 8)
      and CALLS[0][2] == 0x10000 + discard.SCRATCH_OFF, str(CALLS[:1]))
check("內文 +2 u16 格號、+4 u32 種類 ID", MEM.get(DATA_AT + 2) == struct.pack("<HI", 44, 0x11223344),
      str(MEM.get(DATA_AT + 2)))
check("送出＝SEND_FN(連線, 封包)", CALLS[-1][0] == jumpmap.SEND_FN and CALLS[-1][1] == (0x40000, PKT_AT),
      str(CALLS[-1]))
ok, msg = discard.discard_slot(MV, SC, 44, 0)
check("種類 0 不送", not ok, msg)
saved = jumpmap.BUILD_FN
jumpmap.BUILD_FN = 0
ok, msg = discard.discard_slot(MV, SC, 44, 5)
check("送包位址沒定位就拒送", not ok and "定位" in msg, msg)
jumpmap.BUILD_FN = saved

print()
print("③ run：重掃、確認消失、拒收跳過、讀不到停手")
BAG: list[FItem] = []
COMPLETE = [True]
discard.bag = types.SimpleNamespace(scan=lambda sc: (list(BAG), COMPLETE[0]))
supply.bag = discard.bag                      # supply._item_gone 也讀同一個假背包
SENT: list[tuple[int, int]] = []
REFUSE: set[int] = set()


def fake_discard_slot(mover, scanner, slot, type_id):
    it = next(i for i in BAG if i.slot == slot)
    check("送的是那格的種類 ID 不是序號", type_id == it.type_id, f"{type_id} vs {it.type_id}")
    SENT.append((slot, it.serial))
    if it.serial not in REFUSE:
        BAG[:] = [i for i in BAG if i.serial != it.serial]
    return True, ""


discard.discard_slot = fake_discard_slot
BAG[:] = [FItem(20, 1, 1905, 3), FItem(21, 2, 137, 1), FItem(22, 3, 66, 9), FItem(23, 4, 66, 4)]
ok, msg = discard.run(MV, None, {66, 1905})
check("清單上的三格都丟了、不在清單的留著", ok and SENT == [(20, 1), (22, 3), (23, 4)]
      and [it.serial for it in BAG] == [2] and "丟了 3 件" in msg, f"{SENT} {BAG} {msg}")
SENT.clear()
BAG[:] = [FItem(20, 1, 1905, 3), FItem(22, 3, 66, 9)]
REFUSE.add(1)
ok, msg = discard.run(MV, None, {66, 1905})
check("伺服器不讓丟的那件只送一次、點名跳過，其他照丟", ok and SENT == [(20, 1), (22, 3)]
      and [it.serial for it in BAG] == [1] and "丟不掉" in msg, f"{SENT} {BAG} {msg}")
REFUSE.clear()
SENT.clear()
COMPLETE[0] = False
BAG[:] = [FItem(20, 1, 1905, 3)]
ok, msg = discard.run(MV, None, {1905})
check("背包讀不完整：一件都不送、訊息說讀不到", ok and SENT == [] and "讀不到" in msg, f"{SENT} {msg}")
COMPLETE[0] = True
ok, msg = discard.run(MV, None, set())
check("清單空＝不動", ok and SENT == [] and "空" in msg, msg)
ok, msg = discard.run(MV, None, {999})
check("背包沒清單上的東西", ok and SENT == [] and "沒有" in msg, msg)
ok, msg = discard.run(MV, None, {1905}, should_stop=lambda: True)
check("should_stop 一開始就停", ok and SENT == [], msg)

print()
print("④ 小視窗（兩張表）")
from PySide6.QtWidgets import QApplication                        # noqa: E402
from app.tabs import discard_dialog, itemlist_dialog              # noqa: E402

app = QApplication.instance() or QApplication([])
BAG[:] = [FItem(20, 1, 1905, 3, 11), FItem(24, 5, 66, 9, 12), FItem(25, 6, 66, 4, 12),
          FItem(21, 2, 137, 1)]
CFG.d[discard.CFG_KEY] = [66, 4837]           # 4837 清單上但不在這台背包
itemlist_dialog.itemname = types.SimpleNamespace(
    label=lambda tid, c=None: {1905: "天使之翼", 66: "低效紅藥水", 4837: "高效藍藥水",
                               137: "綁定石"}.get(tid, f"種類 {tid}"))
dlg = discard_dialog.DiscardDialog(None, object(), "測試")


def texts(lst):
    return [lst.item(i).text() for i in range(lst.count())]


check("左邊＝背包裡全部、還沒在清單上的（不看綁定）", texts(dlg.bag_list) == ["天使之翼 ×3", "綁定石 ×1"],
      str(texts(dlg.bag_list)))
check("右邊＝清單（含這台沒有的），照名字排", texts(dlg.want_list) == ["低效紅藥水", "高效藍藥水"],
      str(texts(dlg.want_list)))
check("標題是自動丟棄", dlg.windowTitle().startswith("自動丟棄"), dlg.windowTitle())
CFG.saves = 0
dlg.bag_list.item(0).setSelected(True)
dlg._add()
check("加入 → 寫 config＋save", CFG.d[discard.CFG_KEY] == [66, 1905, 4837] and CFG.saves == 1,
      f"{CFG.d.get(discard.CFG_KEY)} saves={CFG.saves}")
dlg.close()

print()
print("⑤ 掛機設定視窗多兩顆鈕")
from app.tabs import farm_settings_dialog                         # noqa: E402

farm_settings_dialog.farmsettings = types.SimpleNamespace(
    FILL_MIN=10, FILL_MAX=100, fill_pct=lambda: 95, set_fill_pct=lambda v: v)
hits = []
sd = farm_settings_dialog.FarmSettingsDialog(
    None, actions=(("存公會倉庫", "t1", lambda: hits.append("g")),
                   ("自動丟棄", "t2", lambda: hits.append("d"))))
check("兩顆鈕都在", [b.text() for b in sd.action_btns] == ["存公會倉庫", "自動丟棄"],
      str([b.text() for b in sd.action_btns]))
sd.action_btns[1].click()
check("按下去叫到對的函式", hits == ["d"], str(hits))
sd.close()

print()
if FAILS:
    print(f"FAIL {len(FAILS)}：" + "、".join(FAILS))
    sys.exit(1)
print("OK")
