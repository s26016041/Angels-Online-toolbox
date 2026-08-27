"""活動分頁「自動使用活動硬幣」離線測試 —— offscreen Qt ＋ 假遊戲層。

    py tools\\event_check.py       （全 PASS 印 OK，有 FAIL 結束碼 1）

驗的規格（2026-08-27 使用者定）：

① 名字含關鍵字（預設「啤酒節」）**而且**有「x<數字>」的才使用；
   沒有「x<數字>」的是**硬幣本體**，絕對不能動到。
② 關鍵字空白 → 什麼都不做（安全預設，不然會把整袋帶 x 的東西吃掉）。
③ ★★★ 送給遊戲的**格號**要是 `inventory.find_by_type()` 給的
   （物品自己記的 +0x25），**不是** `bag.Item.slot`（陣列索引）——
   表頭實測偏過 6 格，拿索引打封包會**用到別的東西**。
④ 送出去 ≠ 成功：那個種類的總數變少了才算用掉；連 CONFIRM_TRIES 輪
   沒變少就停下來（⛔ 不准無限重送，重送一次就可能多用掉一個）。
⑤ 背包沒同步完（`bag.scan` 第二值 False）**不准**判「都用完了」。

第①條同時**回頭對真的物品名表**：2026-08-27 官方在活動中途把銅幣編號
從 79912 換成 87395、舊的改名「(舊)」—— 規則認名字不認編號，所以要能
同時吃到新舊兩組，而且四種本體一個都不能中。
"""
from __future__ import annotations

import gzip
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtWidgets import QApplication              # noqa: E402

APP = QApplication.instance() or QApplication([])

from app.game import bag                                # noqa: E402
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
hits = {it.type_id for it in event_tab.pick_targets(pool, "啤酒節")}
check(f"{len(STACKS)} 種「x數字」全部會被使用",
      all(i in hits for i in STACKS),
      f"漏掉 {[i for i in STACKS if i not in hits]}")
check("四種硬幣本體一個都不會被使用",
      not any(i in hits for i in BODY),
      f"誤中 {[(i, names[i]) for i in BODY if i in hits]}")
check("新舊兩組銅幣都吃得到（改名不影響）",
      87399 in hits and 79918 in hits)

print()
print("③ 關鍵字閘門與裝備保護")
check("關鍵字空白 → 什麼都不做",
      event_tab.pick_targets(pool, "  ") == [])
check("關鍵字不符 → 不動作",
      event_tab.pick_targets(pool, "幸運草") == [])
gear = item(87399, gear=True)         # 名字有 x10 但它是裝備
check("裝備不會被使用（關鍵字打太寬的保險）",
      event_tab.pick_targets([gear], "啤酒節") == [])

print()
print("④ 接線：挑一個 → 送出 → 對帳（跑真的 EventTab）")

USED: list[int] = []
ARRAY_SLOT = 30            # bag 給的陣列索引（★不可以拿它打封包）
REAL_SLOT = 77             # 物品自己記的格號（+0x25）


class FakeSC:
    def _read_bytes(self, a, n):
        return None


class FakeMover:
    active = True


BAG = {"items": [], "complete": True}
event_tab.bag = types.SimpleNamespace(
    scan=lambda sc: (list(BAG["items"]), BAG["complete"]),
    head=lambda sc: (0x1000, 200),
    Item=bag.Item)
event_tab.inventory = types.SimpleNamespace(
    find_by_type=lambda sc, h, tid: (REAL_SLOT, 0x2000, 1))
event_tab.recall = types.SimpleNamespace(
    use_item=lambda mv, slot: (USED.append(slot), True)[1])


def new_page():
    p = event_tab.EventTab()
    p._scanners = {1: FakeSC()}
    p._movers = {1: FakeMover()}
    p.who.addItem("測試", 1)
    p.who.setCurrentIndex(0)
    p.key.setText("啤酒節")
    p._running = True
    return p


page = new_page()
BAG["items"] = [item(87395, 7), item(87399, 3, slot=ARRAY_SLOT)]
page._use_tick()
check("挑的是有 x 的那個、本體沒被碰",
      len(USED) == 1, f"實得 {USED}")
check("★送出的格號是 find_by_type 給的（不是 bag 的陣列索引）",
      USED == [REAL_SLOT], f"實得 {USED}（陣列索引是 {ARRAY_SLOT}）")
check("送出後進入等對帳狀態", page._pending is not None)

BAG["items"] = [item(87395, 7), item(87399, 2, slot=ARRAY_SLOT)]   # 3 → 2
page._use_tick()
check("總數變少了 → 記一筆成功", page._used == 1 and page.log.rowCount() == 1,
      f"used={page._used} rows={page.log.rowCount()}")

print()
print("⑤ 沒變少不准無限重送；沒同步完不准說「用完了」")
page = new_page()
USED.clear()
BAG["items"] = [item(87399, 3, slot=ARRAY_SLOT)]
for _ in range(event_tab.CONFIRM_TRIES + 2):
    page._use_tick()                       # 數量永遠不變 → 應該要自己停
check("連續沒變少就停下來", page._running is False)
check(f"⛔ 沒有無限重送（只送了 1 次）", len(USED) == 1, f"實得 {len(USED)} 次")
check("停下來有講原因", "沒變少" in page.status.text(), page.status.text())

page = new_page()
USED.clear()
BAG["items"], BAG["complete"] = [], False      # 讀到空的，但沒同步完
page._use_tick()
check("背包沒同步完 → 不准判「都用完了」",
      page._running is True and "同步" in page.status.text(),
      page.status.text())
BAG["complete"] = True
page._use_tick()
check("真的空了才收工", page._running is False and "用完" in page.status.text(),
      page.status.text())

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
