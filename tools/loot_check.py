"""「獲得物品」離線回歸測試 —— app/game/loot.py 的封包入帳規則＋掛機頁的「紀錄」視窗。

驗的規格（2026-09-24 使用者：「自動戰鬥真的在自動戰鬥的時候才能計算」
「在自動掛機時才會算要在巡邏點」「都改成吃官方伺服器給的」；memory `loot-into-bag-packet`）：
    · 只認「殺手＝我／我的召喚物」的 0x0a **緊跟著**的 0x1b 物品同步、總數**變多**
    · 一隻掉 2 件（連兩包 0x1b）都算；別人殺的、沒擊殺的（買的、別人給的）都不算
    · 窗口：DROP_WINDOW 包別的封包或下一包 0x0a 就關
    · 喝水（總數變少）不算，但總數要跟著更新
    · credit=False（沒勾掛機／不在巡邏圖）照樣更新總數、不入帳
    · 還沒完整快照 → 舊序號不知道原本幾個 → 不記（少記不猜）；快照後新序號＝新一格
    · ⚠ 快照不准蓋掉比它新的封包（掉落落在快照與封包處理之間不能漏）
    · ⚠⚠ 快照讀不到（`bag.scan` 第二值 False）不套（[[bag-false-empty-guards]]）
    · 換角色 → 序號表丟掉；resync → 等下一次快照
    · 封包長度 ≠ 92 不認
    · ⛔ **不算金幣**；重新計算＝累計歸零
    · 掛機頁 _poll_kills 真的走這條（勾掛機＋在巡邏圖才入帳）
    · 獲得物品／商店紀錄／商城紀錄＝**一顆「紀錄」鈕、視窗裡三個分頁**

前半段不碰遊戲也不碰 Qt；後半段用 offscreen Qt 建**真的**掛機分頁與視窗。

用法：py tools\loot_check.py   （全 PASS 結尾印 OK，有 FAIL 結束碼 1）
"""
from __future__ import annotations

import os
import struct
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.game import castwatch, loot                 # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, why: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"　{why}"))
    if not cond:
        FAILS.append(name)


ME, PET, OTHER = 0x6A2F02E8, 0x5000AAAA, 0x11112222


def kill(victim, killer):
    d = struct.pack("<HIII", 0x0A, victim, killer, 7)
    return (len(d), d)


def item(serial, tid, total, slot=24, n=92):
    """實錄版面（北極狐 2026-09-24）：序號@7 種類@15 格號@44 總數@46，共 92 bytes；
    環槽只存前 _CAP bytes。"""
    d = bytearray(92)
    d[0:7] = bytes((0x1B, 0, 1, 0, 0, 0, 1))
    struct.pack_into("<I", d, 7, serial)
    struct.pack_into("<I", d, 11, 0x6AB49442)
    struct.pack_into("<I", d, 15, tid)
    d[39:44] = bytes((1, 0x21, 0x71, 0, 0))
    struct.pack_into("<H", d, 44, slot)
    struct.pack_into("<H", d, 46, total)
    return (n, bytes(d[:castwatch._CAP]))


def other(op=0x13):
    d = struct.pack("<HI", op, ME) + b"\0" * 6
    return (len(d), d)


def it(serial, tid, count, icon=0):
    return types.SimpleNamespace(serial=serial, type_id=tid, count=count, icon_id=icon)


def mine(k):
    return k in (ME, PET)


class Feeder:
    """幫一個 Loot 記封包序號（像 CastHook.read_since 那樣連號）。"""

    def __init__(self, lt):
        self.lt, self.seq = lt, 0

    def __call__(self, *pkts, credit=True):
        got = self.lt.feed(self.seq, list(pkts), mine, credit)
        self.seq += len(pkts)
        return got


def qty(lt: loot.Loot, tid: int) -> int:
    for t, n, _icon, _ts in lt.rows():
        if t == tid:
            return n
    return 0


print("⓪ castwatch.parse_item／環槽放得下")
check("解得出 (序號, 種類, 格號, 總數)",
      castwatch.parse_item(item(0x608F619, 1228, 6)[1], 92) == (0x608F619, 1228, 24, 6))
check("長度不是 92 不認", castwatch.parse_item(item(1, 2, 3)[1], 90) is None)
check("死亡廣播不是物品包", castwatch.parse_item(kill(1, ME)[1], 14) is None)
check("環槽＋stub 還塞得進 0x8000 的區塊",
      64 + castwatch._N * castwatch._SLOT + 0x100 <= 0x8000)

print("① 還沒快照：舊序號不知道原本幾個 → 不記")
lt = loot.Loot(); f = Feeder(lt)
f(kill(0x100, ME), item(0xA, 2, 8))
check("沒記", lt.rows() == [], str(lt.rows()))
check("要求拍快照", lt.need_bag())

print("② 快照後：我殺的緊接的 0x1b 增加量入帳（圖示從快照學）")
lt.note_bag([it(0xA, 2, 8, icon=55), it(0xB, 1228, 5)], True, f.seq, "甲")
got = f(kill(0x101, ME), item(0xA, 2, 9))
check("+1", qty(lt, 2) == 1 and got == [(2, 1)], str(lt.rows()))
check("圖示編號帶上", lt.rows()[0][2] == 55, str(lt.rows()))

print("③ 一隻掉兩件（連兩包）都算；新序號＝新一格")
f(kill(0x102, ME), item(0xB, 1228, 7), item(0xC, 3994, 1))
check("疊加那件 +2", qty(lt, 1228) == 2, str(lt.rows()))
check("新格那件 +1", qty(lt, 3994) == 1, str(lt.rows()))

print("④ 別人殺的、沒擊殺的都不算；召喚物殺的算")
f(kill(0x103, OTHER), item(0xA, 2, 10))
f(other(), item(0xA, 2, 30))            # 補給買了 20 個（沒擊殺）
check("別人殺的／買的沒算", qty(lt, 2) == 1, str(lt.rows()))
f(kill(0x104, PET), item(0xA, 2, 31))
check("召喚物殺的 +1（從 30 起算，不是從 9）", qty(lt, 2) == 2, str(lt.rows()))

print("⑤ 窗口內喝水（變少）不算、總數照樣更新")
lt.note_bag([it(0xD, 8101, 429)] + [it(0xA, 2, 31), it(0xB, 1228, 7), it(0xC, 3994, 1)],
            True, f.seq, "甲")
f(kill(0x106, ME), item(0xD, 8101, 428), item(0xA, 2, 32))
check("紅水沒算", qty(lt, 8101) == 0, str(lt.rows()))
check("同批的掉落照算", qty(lt, 2) == 3, str(lt.rows()))

print("⑥ 窗口會關：DROP_WINDOW 包別的之後、或下一包 0x0a")
f(kill(0x107, ME), *[other() for _ in range(loot.DROP_WINDOW)], item(0xA, 2, 40))
check("隔太多包不算", qty(lt, 2) == 3, str(lt.rows()))
f(kill(0x108, ME), other(), other(), item(0xA, 2, 41))
check("隔兩包還在窗口內", qty(lt, 2) == 4, str(lt.rows()))
f(kill(0x109, ME), kill(0x10A, OTHER), item(0xA, 2, 42))
check("下一包是別人的擊殺 → 關窗", qty(lt, 2) == 4, str(lt.rows()))

print("⑦ credit=False（沒勾掛機／不在巡邏圖）不入帳、總數照樣更新")
f(kill(0x10B, ME), item(0xA, 2, 50), credit=False)
check("沒入帳", qty(lt, 2) == 4, str(lt.rows()))
f(kill(0x10C, ME), item(0xA, 2, 51))
check("回來後從 50 起算 +1", qty(lt, 2) == 5, str(lt.rows()))

print("⑧ ⚠ 快照不蓋比它新的封包（掉落落在快照與處理之間）")
wc0 = f.seq
snap = [it(0xA, 2, 52), it(0xB, 1228, 7), it(0xC, 3994, 1), it(0xD, 8101, 428)]  # 快照已含掉落
f(kill(0x10D, ME), item(0xA, 2, 52))     # 封包先吃（呼叫端的順序）
lt.note_bag(snap, True, wc0, "甲")
check("那一件 +1 沒漏", qty(lt, 2) == 6, str(lt.rows()))
f(kill(0x10E, ME), item(0xA, 2, 53))
check("下一次照樣 +1（快照沒把它蓋錯）", qty(lt, 2) == 7, str(lt.rows()))

print("⑨ ⚠⚠ 快照讀不到不套")
lt2 = loot.Loot(); f2 = Feeder(lt2)
check("回 False", lt2.note_bag([], False, 0, "甲") is False)
f2(kill(1, ME), item(0xA, 2, 9))
check("還是不記（沒有基準）", lt2.rows() == [])

print("⑩ 換角色 → 序號表丟掉；resync → 等下一次快照")
lt.note_bag([it(0xF, 2, 3)], True, f.seq, "乙")
f(kill(0x10F, ME), item(0xA, 2, 99))
check("換人後新序號照樣算新一格（別人那袋不算）", qty(lt, 2) == 7 + 99, str(lt.rows()))
lt.resync()
f(kill(0x110, ME), item(0xF, 2, 5))
check("resync 後舊序號不記", qty(lt, 2) == 106, str(lt.rows()))

print("⑪ 重新計算：累計歸零、序號表留著")
lt.note_bag([it(0xF, 2, 5)], True, f.seq, "乙")
lt.reset()
check("歸零", lt.rows() == [])
f(kill(0x111, ME), item(0xF, 2, 6))
check("重置後直接 +1", qty(lt, 2) == 1, str(lt.rows()))

print("⑫ ⛔ 金幣（種類 1）也走 0x1b，一律不算")
f(kill(0x114, ME), item(0x99, 1, 26611, slot=0))
check("金幣沒進表", qty(lt, 1) == 0, str(lt.rows()))

print("⑬ 排序：最後獲得的在最上面")
f(kill(0x112, ME), item(0xE1, 111, 1))
f(kill(0x113, ME), item(0xE2, 222, 1))
check("新的在第一列", [t for t, *_ in lt.rows()][0] == 222, str(lt.rows()))

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


# 隨便一個圖包裡真的有的圖示編號 —— 驗「圖真的畫得出來」用
ICON_OK = sorted(itemicon._open()[1])[0] if itemicon.count() else 0


class Ring:
    """假的 CastHook：pk＝環槽裡的 [(長度, 內容)]，介面跟真的一樣（read_since 回三元組）。"""

    def __init__(self):
        self.pk: list = []
        self.active = True

    def installed(self):
        return True

    def mark_lost(self):
        self.active = False

    def write_count(self):
        return len(self.pk)

    def read_since(self, since):
        return since, len(self.pk), self.pk[since:]


BAG = {"items": [], "ok": True}
SCENE = [122]
farm_tab.bag.scan = lambda sc, *a, **k: (list(BAG["items"]), BAG["ok"])
farm_tab.scene.current_id = lambda sc, allow_scan=False: SCENE[0]
farm_tab.player.pet_eid = lambda sc: 0


def poll(page):
    page._kill_poll_t = -9.0              # 跳過 0.25 秒節流
    page._poll_kills()


print("⑭ 分頁整合：_poll_kills 吃封包入帳；只在巡邏點那張圖算")
page = build_page()
check("按鈕文字是「紀錄」", page.log_btn.text() == "紀錄")
check("三顆舊按鈕已經拿掉",
      not any(hasattr(page, a)
              for a in ("buy_log_btn", "mall_log_btn", "loot_btn")))
page._my_id = lambda: ME
page._home = (10.0, 20.0, 122)
ring = Ring()
page._castwatch = ring
BAG["items"] = [it(0xA, 1905, 10, icon=ICON_OK)]
poll(page)                                  # 換 hook
poll(page)                                  # 第一次快照
ring.pk += [kill(0x201, ME), item(0xA, 1905, 13)]
poll(page)
check("我殺的掉落 +3", qty(page._loot, 1905) == 3, str(page._loot.rows()))
page.tick(0.5)
check("心跳本身不再對帳背包（背包變多也不算）",
      qty(page._loot, 1905) == 3, str(page._loot.rows()))
SCENE[0] = 999
ring.pk += [kill(0x202, ME), item(0xB, 4836, 1)]
poll(page)
check("不在巡邏點那張圖 → 不算", qty(page._loot, 4836) == 0, str(page._loot.rows()))
SCENE[0] = 122
ring.pk += [kill(0x203, ME), item(0xC, 4837, 1)]
poll(page)
check("回到巡邏圖 → 照算", qty(page._loot, 4837) == 1, str(page._loot.rows()))
page._home = None
ring.pk += [kill(0x204, ME), item(0xC, 4837, 2)]
poll(page)
check("沒有巡邏點 → 不算", qty(page._loot, 4837) == 1, str(page._loot.rows()))
page._home = (10.0, 20.0, 122)
k0 = page._kills
ring.pk += [kill(0x205, OTHER), item(0xC, 4837, 3)]
poll(page)
check("別人殺的 → 不算（擊殺數也不加）",
      qty(page._loot, 4837) == 1 and page._kills == k0, f"{page._loot.rows()} kills={page._kills}")

print("⑮ 「紀錄」視窗：三個分頁、列數、圖示")
page._record_purchase("聖光城補給商", 7777, 3)   # 商店那頁要有東西可看
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

print("⑯ 重新計算：歸零＋表就地重畫")
dlg._loot._reset_btn.click()
check("表清空", dlg._loot._tbl.rowCount() == 0)
check("標題改成「還沒有掉落」", "還沒有掉落" in dlg._loot._head.text(),
      f"實得 {dlg._loot._head.text()}")
ring.pk += [kill(0x206, ME), item(0xA, 1905, 14)]
poll(page)
check("重置後只算新的 1 件",
      [(t, n) for t, n, _i, _s in page._loot.rows()] == [(1905, 1)],
      f"實得 {page._loot.rows()}")
dlg.deleteLater()

print("⑰ 單開一張「獲得物品」（_wrap_panel 那條路）的空表")
empty = build_page()._loot_dialog()
check("說「還沒有掉落」", "還沒有掉落" in empty._head.text())
check("零列", empty._tbl.rowCount() == 0)
empty.deleteLater()

print("⑱ 版面（2026-08-28 使用者調的）")


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
