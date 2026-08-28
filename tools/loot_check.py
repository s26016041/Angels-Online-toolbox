"""「獲得物品」離線回歸測試 —— app/game/loot.py 的對帳規則＋掛機頁的「紀錄」視窗。

驗的規格（2026-08-28 使用者要求）：
    · 第一拍只建基準，不算收穫
    · 只認**增加**的量；賣掉／喝掉（負差值）不倒扣
    · 同一種東西累加成一列，附圖示編號
    · ⚠⚠ 背包讀不到（`bag.scan` 的第二個回傳值 False）→ 整拍作廢、基準不動，
      恢復之後**不可以**把整袋算成剛獲得（[[bag-false-empty-guards]] 那個
      復發七次的坑）
    · 買來的不算收穫，而且「記帳先／快照先」兩種順序都要對得起來
    · 換角色（斷線重登洗牌）→ 只重建基準，不把別人整袋算進來
    · ⛔ **不算金幣**（使用者原話：「錢不算好了」）
    · 重新計算＝全部歸零並重新起算
    · 獲得物品／商店紀錄／商城紀錄＝**一顆「紀錄」鈕、視窗裡三個分頁**
      （表沒有合併：幣別／單位不同，2026-08-21 那條規矩還在）

前半段不碰遊戲也不碰 Qt；後半段用 offscreen Qt 建**真的**掛機分頁與視窗。

用法：py tools\\loot_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import loot                        # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


SC = object()          # 假的 scanner：底下的假 bag 根本不看它


class FakeBag:
    """假背包：`bag` 擺一袋（{種類id: 數量}），`ok=False` 讓它讀不到。

    ⚠ `gold()` 故意留著並且照樣回值 —— 拿來證明**沒有人再去讀它**
      （2026-08-28 起金幣不算）。
    """

    def __init__(self) -> None:
        self.bag: dict[int, int] = {}
        self.gold_ = 0
        self.ok = True
        self.gold_ok = True
        self.icons: dict[int, int] = {}

    def scan(self, sc, *a, **k):
        if not self.ok:
            # ⚠ 讀不到時 bag.scan 回的就是 ([], False) —— 跟「整袋賣光」
            #   長得一模一樣，這正是這支測試要盯的地方。
            return [], False
        items = [types.SimpleNamespace(type_id=t, count=n,
                                       icon_id=self.icons.get(t, 0))
                 for t, n in self.bag.items() if n]
        return items, True

    def gold(self, sc):
        return self.gold_ if (self.ok and self.gold_ok) else None


FB = FakeBag()
loot.bag = FB          # 整支模組只透過 bag.scan 讀遊戲


def new(bag_now: dict | None = None) -> loot.Loot:
    """建一個累計器並用現在這袋當基準（＝第一拍）。"""
    FB.bag = dict(bag_now or {})
    lt = loot.Loot()
    lt.update(SC, "甲")
    return lt


def qty(lt: loot.Loot, tid: int) -> int:
    for t, n, _icon, _ts in lt.rows():
        if t == tid:
            return n
    return 0


print("① 第一拍只建基準，不算收穫")
FB.bag, FB.gold_, FB.ok, FB.gold_ok = {1905: 30}, 1000, True, True
lt = loot.Loot()
check("回 True（這一拍算數）", lt.update(SC, "甲") is True)
check("沒有任何收穫", lt.rows() == [], f"實得 {lt.rows()}")

print("② 撿到東西 → 累加（含圖示編號）")
FB.icons = {1905: 4321}
lt = new({1905: 30})
FB.bag = {1905: 33}
lt.update(SC, "甲")
FB.bag = {1905: 35, 4836: 2}
lt.update(SC, "甲")
check("藥水累計 5", qty(lt, 1905) == 5, f"實得 {qty(lt, 1905)}")
check("新種類 2 件", qty(lt, 4836) == 2)
check("兩種", lt.kinds() == 2)
check("圖示編號有帶出來",
      [i for t, _n, i, _s in lt.rows() if t == 1905] == [4321])

print("③ 賣掉／喝掉不倒扣，後來又撿到只算真的增加")
lt = new({1905: 30})
FB.bag = {1905: 40}
lt.update(SC, "甲")              # +10
FB.bag = {1905: 0}
lt.update(SC, "甲")              # 全賣掉：不倒扣
check("賣光之後還是 10", qty(lt, 1905) == 10, f"實得 {qty(lt, 1905)}")
FB.bag = {1905: 7}
lt.update(SC, "甲")              # 再撿 7
check("再撿 7 → 17", qty(lt, 1905) == 17, f"實得 {qty(lt, 1905)}")

print("④ ⚠⚠ 背包讀不到 → 整拍作廢，恢復後不可以把整袋當成剛獲得")
lt = new({1905: 30, 4836: 5})
FB.ok = False
check("回 False（這一拍不算數）", lt.update(SC, "甲") is False)
check("讀不到期間沒有任何收穫", lt.rows() == [])
FB.ok = True
FB.bag = {1905: 31, 4836: 5}
lt.update(SC, "甲")
check("恢復後只算真的多的 1 件", qty(lt, 1905) == 1 and qty(lt, 4836) == 0,
      f"實得 {lt.rows()}")

print("⑤ ⛔ 金幣不算（使用者 2026-08-28：「錢不算好了」）")
check("累計器沒有金幣這個東西", not hasattr(loot.Loot(), "gold"))
lt = new({1905: 30})
FB.gold_ = 999999                     # 錢暴增
FB.bag = {1905: 31}
lt.update(SC, "甲")
check("只記東西、不受金幣影響",
      [(t, n) for t, n, _i, _s in lt.rows()] == [(1905, 1)], f"實得 {lt.rows()}")

print("⑥ 買來的不算收穫 —— 記帳先、快照後")
lt = new({1905: 10})
lt.bought(1905, 50)              # 補給那趟買了 50（ledger 的實測差額）
FB.bag = {1905: 60}
lt.update(SC, "甲")
check("買的 50 全扣掉", qty(lt, 1905) == 0, f"實得 {qty(lt, 1905)}")
FB.bag = {1905: 63}
lt.update(SC, "甲")
check("之後撿到的照算 3", qty(lt, 1905) == 3, f"實得 {qty(lt, 1905)}")

print("⑦ 買來的不算收穫 —— 快照先、記帳後（時序相反也要對）")
lt = new({1905: 10})
FB.bag = {1905: 60}
lt.update(SC, "甲")              # 先被算成獲得 50
check("先算進來 50", qty(lt, 1905) == 50)
lt.bought(1905, 50)              # 補給的記帳晚一步到
check("記帳倒扣回 0", qty(lt, 1905) == 0, f"實得 {qty(lt, 1905)}")
check("那一列整個消失", lt.kinds() == 0)

print("⑧ 買的比撿的多 → 扣完為止，不會扣成負的")
lt = new({1905: 10})
FB.bag = {1905: 15}
lt.update(SC, "甲")              # +5
lt.bought(1905, 50)              # 記帳 50（其中 45 還沒進背包）
check("倒扣到 0 不會變負", qty(lt, 1905) == 0)
FB.bag = {1905: 60}
lt.update(SC, "甲")              # 剩下的 45 進來
check("剩下的待扣帳也扣掉", qty(lt, 1905) == 0, f"實得 {qty(lt, 1905)}")
FB.bag = {1905: 62}
lt.update(SC, "甲")
check("扣完之後恢復正常記帳", qty(lt, 1905) == 2, f"實得 {qty(lt, 1905)}")

print("⑨ 換角色（斷線重登洗牌）→ 只重建基準，不把別人整袋算進來")
lt = new({1905: 10})
FB.bag = {1905: 12}
lt.update(SC, "甲")
FB.bag = {7777: 300, 1905: 999}
check("回 True（讀得到）", lt.update(SC, "乙") is True)
check("別人的東西沒算進來", qty(lt, 7777) == 0 and qty(lt, 1905) == 2,
      f"實得 {lt.rows()}")
FB.bag = {7777: 305, 1905: 999}
lt.update(SC, "乙")
check("換人之後照樣繼續記", qty(lt, 7777) == 5, f"實得 {qty(lt, 7777)}")

print("⑩ 重新計算 → 全部歸零、重新起算")
lt = new({1905: 10})
FB.bag = {1905: 20}
lt.update(SC, "甲")
old_since = lt.since
lt.reset()
check("物品清空", lt.rows() == [])
check("起算時間有更新", lt.since >= old_since)
FB.bag = {1905: 25}
lt.update(SC, "甲")              # 歸零後的第一拍＝重建基準
check("歸零後第一拍不算收穫", lt.rows() == [], f"實得 {lt.rows()}")
FB.bag = {1905: 28}
lt.update(SC, "甲")
check("之後照常累加 3", qty(lt, 1905) == 3, f"實得 {qty(lt, 1905)}")

print("⑪ 排序：最後獲得的在最上面")
lt = new({})
FB.bag = {111: 1}
lt.update(SC, "甲")
FB.bag = {111: 1, 222: 1}
lt.update(SC, "甲")
check("新的在第一列", [t for t, *_ in lt.rows()][0] == 222,
      f"實得 {[t for t, *_ in lt.rows()]}")

# ---------------------------------------------------------------------------
# 分頁整合（offscreen Qt ＋ 假遊戲層）：按鈕、心跳節流、視窗、重新計算
# ⚠ 替身只換 I/O（假 scanner／假背包），跑的是**真的** CharFarmPage 與真的
#   `_loot_dialog()` —— memory 的 test-via-button 那條：替身介面跟真的不一樣
#   就等於在測替身。
# ---------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication         # noqa: E402
from app.game import itemicon                      # noqa: E402
from app.tabs import farm_tab                      # noqa: E402

APP = QApplication.instance() or QApplication([])


class FakeSC:
    def _read_bytes(self, addr, n):
        return None

    def alive(self):
        return True


def build_page():
    sc = FakeSC()
    page = farm_tab.CharFarmPage(
        1234, 0, "t", sc, lambda pid, full=False: True,
        farm_tab.TargetWorker(sc), farm_tab.KeyWorker(0, sc),
        account="acct", char_name="小狐")
    page._ensure_mover = lambda: True
    page._mover = types.SimpleNamespace(active=True)
    page.cur_scene = lambda: 122
    page.my_pos = lambda: (10.0, 20.0)
    page._sync_castwatch = lambda: None
    page._buff.armed = True
    page._summon.armed = True
    page.notify = lambda msg: None
    return page


GAP = farm_tab.LOOT_GAP + 0.1
# 隨便一個圖包裡真的有的圖示編號 —— 驗「圖真的畫得出來」用
ICON_OK = sorted(itemicon._open()[1])[0] if itemicon.count() else 0

print("⑫ 分頁整合：按鈕在、心跳會對帳、節流有效")
FB.ok, FB.icons = True, {1905: ICON_OK}
FB.bag = {1905: 10}
page = build_page()
check("按鈕文字是「紀錄」", page.log_btn.text() == "紀錄")
check("三顆舊按鈕已經拿掉",
      not any(hasattr(page, a)
              for a in ("buy_log_btn", "mall_log_btn", "loot_btn")))
page.tick(GAP)                                   # 第一拍：建基準
check("第一拍不算收穫", page._loot.rows() == [])
FB.bag = {1905: 13, 4836: 1}
page.tick(0.5)                                   # 還沒到 LOOT_GAP
check("沒到間隔不對帳", page._loot.rows() == [], f"實得 {page._loot.rows()}")
page.tick(GAP)
check("到了間隔就對帳", page._loot.kinds() == 2, f"實得 {page._loot.rows()}")

print("⑬ 「紀錄」視窗：三個分頁、列數、圖示")
page._record_purchase("聖光城補給商", 7777, 3)   # 商店那頁要有東西可看
#   ⚠ 故意挑一個**不在背包裡**的種類：記帳會扣掉收穫，拿現有的來記會把上面那一列抵掉
dlg = page._logs_dialog()
check("三個分頁", dlg._tabs.count() == 3, f"實得 {dlg._tabs.count()}")
check("分頁名字對",
      [dlg._tabs.tabText(i) for i in range(3)]
      == ["獲得物品", "商店紀錄", "商城紀錄"],
      f"實得 {[dlg._tabs.tabText(i) for i in range(3)]}")
lt_tbl = dlg._loot._tbl
check("獲得物品列數對", lt_tbl.rowCount() == 2, f"實得 {lt_tbl.rowCount()}")
check("件數顯示在上面", "4 件" in dlg._loot._head.text(),
      f"實得 {dlg._loot._head.text()}")
check("⛔ 標題不提金幣", "金幣" not in dlg._loot._head.text(),
      f"實得 {dlg._loot._head.text()}")
check("商店那頁有那一筆", dlg._buys._tbl.rowCount() == 1)
check("商城那頁畫得出來（空的）", dlg._mall._tbl.rowCount() == 0)
icons = {lt_tbl.item(r, 0).icon().isNull() for r in range(2)}
check("有圖的那列畫得出圖示（圖包在）",
      (False in icons) if ICON_OK else True, f"圖包 {itemicon.count()} 張")
blank = [lt_tbl.item(r, 0).text() for r in range(2)
         if lt_tbl.item(r, 0).icon().isNull()]
check("沒圖的那列留白不頂替別張圖", blank == ["—"] or not blank,
      f"實得 {blank}")

print("⑭ 補給買來的不算收穫（走真的 _record_purchase）")
FB.bag = {1905: 13, 4836: 1}
page2 = build_page()
page2.tick(GAP)                                  # 基準
page2._record_purchase("聖光城補給商", 1905, 40)  # 補給那趟買 40
FB.bag = {1905: 53, 4836: 1}
page2.tick(GAP)
check("買的沒被算成收穫", page2._loot.rows() == [], f"實得 {page2._loot.rows()}")
check("商店紀錄照樣記了一筆", len(page2._purchases) == 1)

print("⑮ 重新計算：歸零＋當場重建基準＋表就地重畫")
FB.bag = {1905: 13, 4836: 1}          # ⚠ 上一段動過這袋，先擺回這台的現況
dlg._loot._reset_btn.click()
check("表清空", dlg._loot._tbl.rowCount() == 0)
check("標題改成「還沒對到」", "還沒對到" in dlg._loot._head.text(),
      f"實得 {dlg._loot._head.text()}")
FB.bag = {1905: 14, 4836: 1}                     # 重置後又撿到 1 個
page.tick(GAP)
check("重置後只算新的 1 件",
      [(t, n) for t, n, _i, _s in page._loot.rows()] == [(1905, 1)],
      f"實得 {page._loot.rows()}")
dlg.deleteLater()

print("⑯ 單開一張「獲得物品」（_wrap_panel 那條路）的空表")
empty = build_page()._loot_dialog()
check("說「還沒對到新東西」", "還沒對到" in empty._head.text())
check("零列", empty._tbl.rowCount() == 0)
empty.deleteLater()

print("⑰ 版面（2026-08-28 使用者調的）")


def _row_of(page, w) -> int:
    """這個小工具回「w 在版面的第幾條橫列」（找不到回 -1）。

    ⚠ 分頁的內容掛在捲動區裡那個 `body`，不是 `page.layout()` —— 直接問
      page 會一列都找不到（全 -1，反而看起來「通過」）。
    """
    root = page.run_cb.parentWidget().layout()
    for i in range(root.count()):
        lay = root.itemAt(i).layout()
        if lay is None:
            continue
        for j in range(lay.count()):
            if lay.itemAt(j).widget() is w:
                return i
    return -1


run_row = _row_of(page, page.run_cb)
check("「自動換球」自成一列，就在「開始掛機」下面",
      _row_of(page, page.ball_cb) == run_row + 1,
      f"開始掛機在第 {run_row} 列、自動換球在第 {_row_of(page, page.ball_cb)} 列")
check("「經驗球：…」跟它同一列（不在主開關那列右邊）",
      _row_of(page, page._ball_lbl) == _row_of(page, page.ball_cb) != run_row)
check("最下面那行灰字狀態列不顯示",
      not page.status.isVisibleTo(page))
check("狀態列的內容照樣收得到（流程收尾與離線測試在用）",
      page.status.text() != "")

print()
if FAILS:
    print(f"FAIL：{len(FAILS)} 項沒過 —— " + "、".join(FAILS))
    sys.exit(1)
print("OK：全部通過")
