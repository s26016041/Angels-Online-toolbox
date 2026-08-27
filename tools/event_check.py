"""活動分頁離線測試（自動使用活動硬幣 ＋ 自動抽轉盤）—— offscreen Qt ＋ 假遊戲層。

    py tools\\event_check.py       （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的規格（2026-08-27 使用者定）：

① 名字含「啤酒節」**而且**有「x<數字>」的才使用；沒有「x<數字>」的是
   **硬幣本體**，絕對不能動到。
② ★★★ 送給遊戲的**格號**要是 `inventory.find_by_type()` 給的（物品自己記的
   `+0x25`），**不是** `bag.Item.slot`（陣列索引）—— 表頭實測偏過 6 格，
   拿索引打封包會**用到別的東西**。
③ 送出去 ≠ 成功：總數變少了才算用掉；連 `CONFIRM_TRIES` 輪沒變少就停
   （⛔ 不准無限重送，重送一次就可能多用一個）。
④ 背包沒同步完（`bag.scan` 第二值 False）**不准**判「都用完了」。
⑤ 轉盤：**叫遊戲自己抽**。沒開視窗／正在轉一律拒絕；抽完靠**背包多了什麼**
   對帳，沒多也照實記，不編故事。

第①條同時**回頭對真的物品名表**：2026-08-27 官方在活動中途把銅幣編號從
79912 換成 87395、舊的改名「(舊)」—— 規則認名字不認編號，要能同時吃到新舊
兩組，而且四種本體一個都不能中。
"""
from __future__ import annotations

import gzip
import os
import struct
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtWidgets import QApplication              # noqa: E402

APP = QApplication.instance() or QApplication([])

from app.game import bag, roulette                      # noqa: E402
from app.tabs import event_tab                          # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


def item(type_id: int, count: int = 1, slot: int = 30, gear: bool = False):
    """真的 `bag.Item`（名字走真的 item_names 表，不是假字串）。"""
    return bag.Item(slot=slot, serial=0x1000 + type_id, stamp=1,
                    type_id=type_id, count=count, dura=0, kind=0, price=10,
                    grade=0, dura_max=(80 if gear else 0))


print("① 「x<數字>」認得出來嗎")
for nm, want in (("2026-啤酒節銅幣", None), ("2026-啤酒節銅幣(舊)", None),
                 ("2026-啤酒節銅幣 x10 (1)", 10),
                 ("2026-啤酒節銀幣 x150", 150),
                 ("2026-啤酒節銅幣(舊) x50", 50),
                 ("300杯-香醇啤酒禮盒", None)):
    got = event_tab.stack_size(nm)
    check(f"{nm!r} → {want}", got == want, f"實得 {got}")

print()
print("② 回頭對真的物品名表（官方 8/27 中途換編號也要吃得到）")
names = {int(a): b for a, b in
         (l.rstrip("\n").split("\t") for l in
          gzip.open("assets/item_names.tsv.gz", "rt", encoding="utf-8"))}
BODY = [79912, 79913, 79914, 87395]                  # 四種本體
STACKS = [i for i in list(range(79915, 79944)) + list(range(87396, 87403))
          if i in names]
pool = [item(i) for i in BODY + STACKS if i in names]
hits = {it.type_id for it in event_tab.pick_targets(pool)}
check(f"{len(STACKS)} 種「x數字」全部會被使用",
      all(i in hits for i in STACKS),
      f"漏掉 {[i for i in STACKS if i not in hits]}")
check("四種硬幣本體一個都不會被使用", not any(i in hits for i in BODY),
      f"誤中 {[(i, names[i]) for i in BODY if i in hits]}")
check("新舊兩組銅幣都吃得到（改名不影響）", 87399 in hits and 79918 in hits)
check("關鍵字空白 → 什麼都不做", event_tab.pick_targets(pool, "  ") == [])
check("關鍵字不符 → 不動作", event_tab.pick_targets(pool, "幸運草") == [])
check("裝備不會被使用（關鍵字打太寬的保險）",
      event_tab.pick_targets([item(87399, gear=True)]) == [])

print()
print("③ 轉盤狀態解析（跑真的 roulette.state）")
MGR_PTR, OBJ_OFF, SPIN_FN, MGR = 0x9BD6AC, 0xC7D430, 0x6132F5, 0x39000000
OBJ = MGR + OBJ_OFF
SPOT = roulette.Spot(cmd_fn=0x58E66D, mgr_ptr=MGR_PTR, obj_off=OBJ_OFF,
                     spin_fn=SPIN_FN)


class RouletteSC:
    """只換記憶體讀取；欄位版面交給真的 roulette.state 去解。"""

    def __init__(self, kind=1, due=-1):
        self.kind, self.due = kind, due

    def module_base(self, m):
        return 0x400000

    def _read_bytes(self, addr, n):
        if addr == MGR_PTR:
            return struct.pack("<I", MGR)
        if addr == OBJ:
            b = bytearray(0x2C)
            struct.pack_into("<I", b, 0x00, 0x37FD2D28)     # 參數1 的來源
            b[roulette.OFF_KIND] = self.kind
            struct.pack_into("<i", b, roulette.OFF_DUE_LO, self.due)
            struct.pack_into("<i", b, roulette.OFF_DUE_HI, self.due)
            return bytes(b)
        return None


# ── 位址抽取本人：用**合成的映像**跑真的 locate() ──────────────────
# ⚠⚠ 這一段是 2026-08-27 實機翻車補回來的回歸：第一版寫死掃 4MB，又拿
#   `base + 4MB` 當「這個值在不在模組裡」的上界 → 全域 0x9BD6AC 離基底 5.7MB
#   直接被自己的檢查擋掉，**五台全部回 None**（使用者回報「轉不到轉盤」）。
#   所以合成映像刻意重現那個版面：字串放在接近 4MB 的地方、全域放在 5.7MB。
IMG_BASE, IMG_SIZE = 0x400000, 0x5E7000
STR_AT, TBL_AT, FN_AT = 0x3E2890, 0x3E0138, 0x18E66D
REAL_BODY = bytes.fromhex(
    "558bec51518b45088d4df86a0189 45fcc645f800e89f42faff8b0dacd69b005081c130"
    "d4c700e85d4c080033c040c9c3".replace(" ", ""))


class ImageSC:
    """一整份假的 angel.dat 映像（只有我們在乎的那幾段是真的）。"""

    def __init__(self, size=IMG_SIZE, readable=None):
        self.size, self.readable = size, readable or size
        img = bytearray(size)
        img[STR_AT:STR_AT + len(roulette.CMD_NAME)] = roulette.CMD_NAME
        struct.pack_into("<II", img, TBL_AT, IMG_BASE + STR_AT, IMG_BASE + FN_AT)
        img[FN_AT:FN_AT + len(REAL_BODY)] = REAL_BODY
        self.img = bytes(img)

    def module_base(self, name):
        return IMG_BASE

    def list_modules(self):
        return [types.SimpleNamespace(name="angel.dat", base=IMG_BASE,
                                      size=self.size)]

    def _read_bytes(self, addr, n):
        off = addr - IMG_BASE
        if off < 0 or off + n > self.readable:   # 這一段落在讀不到的尾巴
            return None
        return self.img[off:off + n]


roulette._addr_cache.clear()
got = roulette.locate(ImageSC())
check("★locate() 從合成映像推得出三個值（全域在 5.7MB 也要過）",
      got is not None and (got.mgr_ptr, got.obj_off, got.spin_fn)
      == (0x9BD6AC, 0xC7D430, 0x6132F5),
      f"實得 {got}")
roulette._addr_cache.clear()
got2 = roulette.locate(ImageSC(readable=0x500000))
check("★尾巴有讀不到的段也要推得出來（分段讀＋補零，位移不准歪）",
      got2 is not None and got2.spin_fn == 0x6132F5, f"實得 {got2}")
roulette._addr_cache.clear()


class NoStringSC(ImageSC):
    def __init__(self):
        super().__init__()
        self.img = bytes(self.size)           # 字串被官方改掉了


check("⛔ 推不出來就回 None（大聲停用，不亂猜）",
      roulette.locate(NoStringSC()) is None)
roulette._addr_cache.clear()

roulette.locate = lambda sc: SPOT          # 底下改用固定值驗欄位解析
st = roulette.state(RouletteSC(kind=1, due=-1))
check("讀得到、視窗開著、沒在轉",
      st is not None and st.open and not st.spinning and st.kind == 1)
check("正在轉認得出來", roulette.state(RouletteSC(kind=1, due=1234)).spinning)
check("0xFF ＝ 沒開", not roulette.state(RouletteSC(kind=0xFF)).open)


class FakeMover:
    active = True

    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.calls = []

    def call_sync(self, fn, *args, ecx=None, timeout=None):
        self.calls.append((fn, args, ecx))
        return 1


mv = FakeMover()
ok, why = roulette.spin(mv, RouletteSC(kind=0xFF))
check("⛔ 轉盤沒開就拒絕", not ok and "沒開" in why, why)
check("拒絕的時候一次都沒叫下去", mv.calls == [])
ok, why = roulette.spin(mv, RouletteSC(kind=1, due=999))
check("⛔ 正在轉就拒絕", not ok and mv.calls == [], why)
ok, why = roulette.spin(mv, RouletteSC(kind=3, due=-1))
check("★ 抽的時候 ecx＝轉盤物件、參數＝遊戲自己記的種類",
      ok and mv.calls == [(SPIN_FN, (3,), OBJ)], f"實得 {mv.calls}")

print()
print("④ 接線：用硬幣（跑真的 EventTab）")
USED: list[int] = []
ARRAY_SLOT, REAL_SLOT = 30, 77       # 陣列索引 vs 物品自己記的格號
BAG = {"items": [], "complete": True}
event_tab.bag = types.SimpleNamespace(
    scan=lambda sc: (list(BAG["items"]), BAG["complete"]),
    head=lambda sc: (0x1000, 200), Item=bag.Item)
event_tab.inventory = types.SimpleNamespace(
    find_by_type=lambda sc, h, tid: (REAL_SLOT, 0x2000, 1))
event_tab.recall = types.SimpleNamespace(
    use_item=lambda mv, slot: (USED.append(slot), True)[1])
ROU = {"state": roulette.State(obj=OBJ, kind=1, spinning=False),
       "spin": (True, "已叫下去"), "calls": []}
event_tab.roulette = types.SimpleNamespace(
    state=lambda sc: ROU["state"],
    spin=lambda mv, sc: (ROU["calls"].append("spin"), ROU["spin"])[1],
    State=roulette.State, KIND_NONE=roulette.KIND_NONE)


class FakeSC:
    def _read_bytes(self, a, n):
        return None


def new_page():
    p = event_tab.EventTab()
    p._scanners = {1: FakeSC()}
    p._movers = {1: FakeMover()}
    p.who.addItem("測試", 1)
    p.who.setCurrentIndex(0)
    return p


page = new_page()
page._use_timer.start(999999)                 # 暫停狀態下 tick 不做事（見護欄）
BAG["items"] = [item(87395, 7), item(87399, 3, slot=ARRAY_SLOT)]
page._use_tick()
check("挑的是有 x 的那個、本體沒被碰", len(USED) == 1, f"實得 {USED}")
check("★送出的格號是 find_by_type 給的（不是陣列索引）",
      USED == [REAL_SLOT], f"實得 {USED}（陣列索引是 {ARRAY_SLOT}）")
BAG["items"] = [item(87395, 7), item(87399, 2, slot=ARRAY_SLOT)]     # 3 → 2
page._use_tick()
check("總數變少了 → 記一筆", page._used == 1 and page.log.rowCount() == 1)
check("紀錄是「時間 ＋ 描述」兩欄，描述寫用了什麼",
      page.log.columnCount() == 2
      and "用了" in page.log.item(0, 1).text(),
      page.log.item(0, 1).text() if page.log.item(0, 1) else "（空）")

print()
print("⑤ 硬幣：送了沒少要跳過（不是停機），沒東西也不准收工")
page = new_page()
USED.clear()
BAG["items"], BAG["complete"] = [item(87399, 3, slot=ARRAY_SLOT)], True
page._use_timer.start(999999)
for _ in range(event_tab.CONFIRM_TRIES + 2):
    page._use_tick()
check("★沒少 → 跳過那個種類，**不停機**",
      page._use_timer.isActive() and 87399 in page._skip, f"skip={page._skip}")
check("⛔ 對同一個東西只送過 1 次", len(USED) == 1, f"實得 {len(USED)} 次")
check("跳過有記進歷史", "跳過" in page.log.item(0, 1).text(),
      page.log.item(0, 1).text() if page.log.item(0, 1) else "（空）")
n = len(USED)
for _ in range(3):
    page._use_tick()
check("跳過之後就不再碰它", len(USED) == n, f"又送了 {len(USED)-n} 次")

page = new_page()
USED.clear()
page._use_timer.start(999999)
BAG["items"], BAG["complete"] = [], True          # 背包空的
page._use_tick()
check("★背包沒東西**也不收工**（一直掃到按暫停）",
      page._use_timer.isActive() and USED == [], page.status.text())
check("狀態列講清楚在等什麼", "繼續盯著" in page.status.text(),
      page.status.text())
BAG["items"] = [item(87399, 3, slot=ARRAY_SLOT)]  # 抽到新的禮盒 → 自動接上
page._use_tick()
check("新東西進背包會自己接上", len(USED) == 1, f"實得 {USED}")

page = new_page()
USED.clear()
page._use_timer.start(999999)
BAG["items"], BAG["complete"] = [], False
page._use_tick()
check("背包沒同步完不會亂送", USED == [] and page._use_timer.isActive())
page._stop_use("已暫停")
page._use_tick()
check("★暫停之後再叫 tick 也不會再送", USED == [], f"實得 {len(USED)} 次")

print()
print("⑥ 轉盤：一整轉（叫下去 → 轉 → 轉完 → 對背包的帳）")
page = new_page()
BAG["items"], BAG["complete"] = [item(87395, 100)], True
ROU["calls"].clear()
ROU["state"] = roulette.State(obj=OBJ, kind=1, spinning=False)
ROU["spin"] = (True, "已叫下去")
page._spin_timer.start(999999)
page._spin_tick()
check("走的是 spin（叫遊戲自己抽）", ROU["calls"] == ["spin"], f"實得 {ROU['calls']}")
check("進入等動畫的狀態機", page._spin[0] == "start")
ROU["state"] = roulette.State(obj=OBJ, kind=1, spinning=True)
page._spin_tick()
check("看到開始轉", page._spin[0] == "run")
ROU["state"] = roulette.State(obj=OBJ, kind=1, spinning=False)
page._spin_tick()
check("看到轉完", page._spin[0] == "settle")
BAG["items"] = [item(87395, 90), item(79913, 4)]
page._spin = ("settle", 0.0, page._spin[2])
page._spin_tick()
check("抽到什麼有記進歷史", page._spins == 1
      and "轉盤抽到" in page.log.item(0, 1).text()
      and "銀幣" in page.log.item(0, 1).text(),
      page.log.item(0, 1).text() if page.log.item(0, 1) else "（空）")

print()
print("⑦ 轉盤：⛔ 任何狀況都不准自己暫停")
page = new_page()
page._spin_timer.start(999999)
ROU["calls"].clear()
ROU["state"] = roulette.State(obj=OBJ, kind=roulette.KIND_NONE, spinning=False)
page._spin_tick()
check("★轉盤視窗沒開 → 等他打開，**不停**",
      page._spin_timer.isActive() and ROU["calls"] == []
      and "等你打開" in page.status.text(), page.status.text())
ROU["state"] = roulette.State(obj=OBJ, kind=1, spinning=False)
page._spin_tick()
check("開回來就自己接上", ROU["calls"] == ["spin"], f"實得 {ROU['calls']}")

page = new_page()
page._spin_timer.start(999999)
ROU["calls"].clear()
ROU["spin"] = (False, "指令槽忙，等下一輪")
page._spin_tick()
check("★叫不動（冷卻／指令槽忙）→ 下一輪再叫，**不停**",
      page._spin_timer.isActive() and page._spin is None,
      page.status.text())
ROU["spin"] = (True, "已叫下去")
page._spin_tick()
check("好了就自己接上", page._spin is not None and page._spin[0] == "start")

page = new_page()
page._spin_timer.start(999999)
ROU["calls"].clear()
page._spin_tick()                                   # 叫下去
page._spin = ("start", 0.0, page._spin[2])          # 讓 START_WAIT 過去
page._spin_tick()
check("★叫了沒轉（冷卻中）→ 歸零重來，**不停**",
      page._spin_timer.isActive() and page._spin is None
      and "冷卻" in page.status.text(), page.status.text())

page = new_page()
page._spin_timer.start(999999)
page._scanners.clear()                              # 分身不見了
page._spin_tick()
check("只有分身消失才會真的停",
      not page._spin_timer.isActive() and "分身不見" in page.status.text(),
      page.status.text())

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
