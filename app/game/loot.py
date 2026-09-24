"""獲得物品：**伺服器說是我殺的怪掉的**東西（自動掛機／自動刷副本共用）。

    lt = loot.Loot()
    start, wc, slots = hook.read_since(wc)            # castwatch 環槽（呼叫端自己輪詢）
    lt.feed(start, slots, is_mine, credit=True)       # 掉落只在 credit 期間入帳
    lt.note_bag(scanner, wc_before_scan, who)         # 每隔幾秒校一次「每件現在幾個」
    lt.rows()                                         # [(種類id, 累計數量, 圖示編號, 最後時間)]
    lt.reset()                                        # 「重新計算」

⛔ **金幣不算**（使用者 2026-08-28：「錢不算好了」）—— 只數東西。

怎麼認「這是打怪掉的」（2026-09-24 使用者：「都改成吃官方伺服器給的」）
--------------------------------------------------------------------
舊版是背包快照對帳（多出來的都算），刷副本、飛去別張圖、買的、別人給的全混進來。
現在只認伺服器封包（memory `loot-into-bag-packet`、`kill-credit-packet`）：

  · 掉落**不用撿、直接進背包**；那一刻伺服器送物品整筆同步 `0x1b`
    （序號／種類／格號／目前總數，`castwatch.parse_item`）。
  · `0x1b` 本身**沒有來源欄位**（喝水、買東西也是這包）→ 靠**順序**：緊跟在
    「殺手＝我（或我的召喚物）」的死亡廣播 `0x0a` 後面的那幾包 `0x1b`，而且總數
    **變多**，才算掉落。一隻可能掉 2 件 → 連續幾包都收；窗口在下一包 `0x0a`
    或 DROP_WINDOW 包別的封包後關上（實錄 6/6 次都是緊接著，一包都沒隔）。
  · 增加量＝這包的總數 − 這一件（序號）先前的總數。先前的總數兩個來源：
    每一包 `0x1b`（不論來源都更新）＋定期的背包快照 `note_bag`。
    ⚠ 快照不准蓋掉比它新的封包：快照前先記 write_count，快照讀完**先把封包吃完**
      再套快照，而且只套「快照開始後沒收過封包」的那幾件 —— 不然掉落剛好落在
      快照與封包處理之間，那一件的舊總數已經含掉落，增加量變 0＝漏記。
  · 還沒有完整快照前看到的**舊序號**不知道原本幾個 → 不記（少記，不猜）。
    快照後才出現的新序號＝新的一格，增加量＝總數。

⚠⚠ 快照讀不到就整次不套（[[bag-false-empty-guards]]）：只吃 `bag.scan()` 第二個
  回傳值成立的那次。
⚠ 換角色（斷線重登洗牌）→ 序號表整個丟掉重建，累計保留。

全程只讀（封包是 castwatch 攔讀的環槽），不寫入、不注入。
"""
from __future__ import annotations

import threading
import time

from app.game import bag, castwatch


# 殺手＝我那包之後，最多再看幾包「不是物品同步」的封包就關窗口（實錄 0 包）。
#   同一批跟著來的有 0x13／0x0d(系統訊息)／0x0b 這些，給幾包餘裕；窗口內總數
#   **變少**的（喝水）本來就不算，買東西不會剛好落在擊殺那一批裡。
DROP_WINDOW = 6


class Loot:
    """一台分身（一個角色）的掉落累計器。

    ⚠ `rows()` / `kinds()` 可能在別的執行緒被叫（畫表）—— 共用資料都在鎖底下改。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    # -- 對外狀態 -------------------------------------------------------
    def reset(self) -> None:
        """歸零（「重新計算」鈕）。序號表留著 —— 那是「每件現在幾個」，跟累計無關。"""
        with self._lock:
            self.since = time.time()
            # 種類id →[累計數量, 圖示編號, 最後一次增加的時間, 第幾次增加]
            # ⚠ 排序要用流水號當第二鍵：同一拍進來的幾種東西時間戳一樣。
            self._items: dict[int, list] = {}
            self._seq = 0
            if not hasattr(self, "_known"):
                self._forget()

    def _forget(self) -> None:
        self._known: dict[int, int] = {}     # 序號 → 目前總數
        self._touched: dict[int, int] = {}   # 序號 → 最後一包 0x1b 的封包序號
        self._icons: dict[int, int] = {}     # 種類 → 圖示編號（快照學來的）
        self._ready = False                  # 有過一次完整快照了嗎
        self._armed = 0                      # 擊殺窗口還剩幾包
        self._who: str | None = None

    def resync(self) -> None:
        """「每件現在幾個」不可信了（封包漏看：監聽重裝／環槽被蓋過／有一段沒在輪詢）
        → 丟掉序號表，等下一次完整快照重建；累計保留。重建前舊序號的增加量不記（少記）。"""
        with self._lock:
            self._known.clear()
            self._touched.clear()
            self._ready = False
            self._armed = 0

    def need_bag(self) -> bool:
        """還沒有完整快照（剛開／剛 resync）→ 呼叫端這一拍就該拍一次。"""
        return not self._ready

    def rows(self) -> list[tuple[int, int, int, float]]:
        """[(種類id, 累計數量, 圖示編號, 最後獲得時間)]，**新的在上面**。"""
        with self._lock:
            rows = [(tid, v[0], v[1] or self._icons.get(tid, 0), v[2], v[3])
                    for tid, v in self._items.items()]
        rows.sort(key=lambda r: (r[3], r[4]), reverse=True)
        return [r[:4] for r in rows]

    def kinds(self) -> int:
        """累計到幾種東西（畫面上的「共 N 種」）。"""
        with self._lock:
            return len(self._items)

    # -- 封包 -----------------------------------------------------------
    def feed(self, start: int, slots, is_mine, credit: bool) -> list[tuple[int, int]]:
        """吃一批入向封包（`CastHook.read_since` 的 start 與 [(長度, 內容)]）。

        is_mine(殺手ID) → 這隻算不算我殺的；credit＝現在算不算數（掛機在巡邏圖／
        副本裡跑腳本）。credit=False 照樣吃 0x1b 更新總數，只是不入帳。
        回這批入帳的 [(種類id, 數量)]（給呼叫端寫 debug）。
        """
        got: list[tuple[int, int]] = []
        now = time.time()
        with self._lock:
            for i, pkt in enumerate(slots):
                n, data = pkt
                k = castwatch.parse_kill(data)
                if k is not None:
                    self._armed = DROP_WINDOW if (credit and is_mine(k[1])) else 0
                    continue
                it = castwatch.parse_item(data, n)
                if it is None:
                    if self._armed:
                        self._armed -= 1
                    continue
                serial, tid, _slot, total = it
                if tid == bag.GOLD_TYPE:
                    # ⛔ 金幣不算（2026-08-28）。⚠ 金幣也走這包（北極狐實機：擊殺那批
                    #   一起送），而且第 0 格不在 bag.scan 的清單裡 → 不擋會被當成
                    #   「新的一格、增加量＝總額」整筆灌進來（2026-09-24 驗到過）。
                    continue
                prev = self._known.get(serial)
                if prev is None and self._ready:
                    prev = 0                  # 快照後才出現的序號＝新的一格
                if self._armed and prev is not None and total > prev:
                    gain = total - prev
                    self._seq += 1
                    row = self._items.get(tid)
                    if row is None:
                        self._items[tid] = [gain, self._icons.get(tid, 0), now, self._seq]
                    else:
                        row[0] += gain
                        row[2], row[3] = now, self._seq
                    got.append((tid, gain))
                self._known[serial] = total
                self._touched[serial] = start + i
        return got

    # -- 快照 -----------------------------------------------------------
    def note_bag(self, items, complete: bool, wc_before: int,
                 who: str | None = None) -> bool:
        """套一次背包快照（`bag.scan` 的兩個回傳值）。回「有沒有套上」。

        wc_before＝**快照前**讀的 write_count；⚠ 呼叫端要先把封包吃到快照之後
        （feed）再叫這支 —— 快照開始後收過封包的那幾件以封包為準、不蓋。
        """
        if not complete:
            return False                     # ⚠⚠ 讀不到 ≠ 沒有，整次不套
        with self._lock:
            if who is not None and self._who is not None and who != self._who:
                self._forget()               # 換角色：序號全換了
            if who is not None:
                self._who = who
            seen = set()
            for it in items:
                seen.add(it.serial)
                if it.icon_id:
                    self._icons[it.type_id] = it.icon_id
                if self._touched.get(it.serial, -1) >= wc_before:
                    continue
                self._known[it.serial] = it.count
            for serial in [s for s in self._known if s not in seen]:
                if self._touched.get(serial, -1) < wc_before:
                    del self._known[serial]  # 賣掉／用完／存倉
                    self._touched.pop(serial, None)
            self._ready = True
        return True
