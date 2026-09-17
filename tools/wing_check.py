"""天使之翼（回程道具）「讀不到 ≠ 沒有」的離線測試 —— supply._wing_slot()。

    py tools\\wing_check.py

★★★ 2026-09-17 黑狐實錄（無限塔第 9 趟）：腳本跑完那一拍問背包，整袋讀不到 →
  舊版 `_wing_slot()` 回 None → 補給當成「背包沒有天使之翼（回程道具）」整趟放棄
  （背包其實有 50 個，純讀探針對過）→ 人沒回城，副本頁照常開下一場 → 三秒後
  伺服器把人送出副本 →「⛔ 地圖變了（無限塔 → 棕櫚基地）」停機六小時。
  ＝ [[bag-false-empty-guards]] 第八次復發。

驗的規格（`inventory.count_by_types` 自己的註解就是這樣寫的）：
    · 要下「沒有」的結論一定要**整條陣列走完**（count_by_types 的第二個值）
    · 表頭問不到／整條沒走完 → 回「不確定」，⛔ 不准當成「沒有」
    · 找得到就是找得到（結論可信）
呼叫端那一半（沒跑完 → 隔幾秒重跑一次、連兩次才停機）在 dungeon_run_check.py。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import supply                            # noqa: E402

FAILS: list[str] = []


def ck(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


class Fake:
    """背包三條讀取路的替身（表頭／找一格／整條數）。"""

    def __init__(self, head, found=None, complete=True, boom=False):
        self.head, self.found, self.complete, self.boom = head, found, complete, boom

    def __enter__(self):
        self._orig = (supply.bag.head, supply.inventory.find_by_type,
                      supply.inventory.count_by_types)
        supply.bag.head = lambda _sc: self.head
        supply.inventory.find_by_type = lambda _sc, _h, _t: self.found

        def _counts(_sc, _h, ids):
            if self.boom:
                raise RuntimeError("讀記憶體炸了")
            return dict.fromkeys(ids, 0), self.complete
        supply.inventory.count_by_types = _counts
        return self

    def __exit__(self, *_exc):
        (supply.bag.head, supply.inventory.find_by_type,
         supply.inventory.count_by_types) = self._orig
        return False


print("\n_wing_slot 的三態（格號, 這個結論可信嗎）")
with Fake(head=(0x1000, 743), found=(39, 0x2000, 50)):
    ck("找得到 → (格號, True)", supply._wing_slot(None) == (39, True),
       str(supply._wing_slot(None)))
with Fake(head=(0x1000, 743), found=None, complete=True):
    ck("★★ 整條走完了、真的沒有 → (None, True)",
       supply._wing_slot(None) == (None, True), str(supply._wing_slot(None)))
with Fake(head=(0x1000, 743), found=None, complete=False):
    ck("★★★ 整條**沒走完** → (None, False)：⛔ 不准當成「沒有」",
       supply._wing_slot(None) == (None, False), str(supply._wing_slot(None)))
with Fake(head=None):
    ck("★★★ 表頭問不到（換圖那一拍）→ (None, False)",
       supply._wing_slot(None) == (None, False), str(supply._wing_slot(None)))
with Fake(head=(), found=None):
    ck("　表頭是空的也一樣 → (None, False)",
       supply._wing_slot(None) == (None, False), str(supply._wing_slot(None)))
with Fake(head=(0x1000, 743), found=None, boom=True):
    ck("　數整條時炸了 → (None, False)，不吞成「沒有」",
       supply._wing_slot(None) == (None, False), str(supply._wing_slot(None)))

print("\n呼叫端的訊息分得出兩種失敗")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "app", "game", "supply.py"), encoding="utf-8").read()
ck("★★ run_full_supply 讀不到時說「背包讀不到」，不說「背包沒有」",
   "背包讀不到，問不出有沒有" in src)
ck("★★ 讀不到會先等人站穩再問一次（_wait_ready）",
   "等人站穩再問一次" in src and "_wait_ready(scanner)" in src)

print("\nOK" if not FAILS else "\n失敗：" + "、".join(FAILS))
sys.exit(1 if FAILS else 0)
