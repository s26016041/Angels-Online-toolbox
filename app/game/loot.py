"""掛機收穫：這段期間背包多了什麼東西、金幣多了多少。

    lt = loot.Loot()
    lt.update(scanner, who="小天使")     # 每隔幾秒叫一次（掛機頁的心跳）
    lt.rows()                            # [(種類id, 累計數量, 圖示編號, 最後時間)]
    lt.gold                              # 累計金幣收入
    lt.reset()                           # 「重新計算」

怎麼算出來的
------------
遊戲沒有「我剛剛撿到什麼」這種可以直接讀的東西（掉落走的是伺服器封包，
入向包只解得開施放廣播那一種，見 memory 的 inbound-packet-cast-broadcast），
所以走**背包快照對帳**：每隔幾秒把整袋按種類數一遍，跟上一拍比，
**只認增加的部分**，減少的一律不看。

只認正差值有兩個好處：
  · 賣掉／存倉／喝掉 → 負差值不計，累計不會倒扣，也不會把「後來又撿到」
    算成第二次（累計 = 這段期間淨增加的總和，跟收益監控算經驗／金幣時薪
    同一套算法，見 profit_tab 檔頭）。
  · 換地圖／重連那種「值瞬間跳掉」的情況方向都是往下（讀不到＝空），
    先被下面那道閘擋掉，擋不掉的也只會少算，不會多算。

⚠⚠ 讀不到一定要整拍作廢（[[bag-false-empty-guards]]，那個坑已經復發七次）
  —— 背包讀不到時 `bag.items()` 回的是空清單，跟「東西全被賣光」長得一模一樣。
  拿它當基準的話，下一拍整袋東西會被算成「剛剛獲得」，數字會離譜地灌水。
  所以這裡只吃 `bag.scan()` 的第二個回傳值（整段真的都讀到了），
  以及 `bag.gold()` 的非 None；有一個不成立就**基準不動、什麼都不算**。

⚠ 買來的東西不算收穫（`bought()`）：回程補給買的兩百瓶藥水本來就會讓背包
  變多，記進「掛機獲得」只會讓人以為打怪掉了兩百瓶。補給那條路本來就在
  對帳（`supply.run_buy` 的 ledger），把同一筆數量報過來扣掉即可。
  ★ 記帳跟快照誰先誰後都要對：`bought()` **先從已經累計的數量倒扣**，
    扣不完的才掛在待扣帳上等下一拍（快照先發生／記帳先發生兩種順序都成立）。

金幣走 `bag.gold()`（背包第 0 格的物品，遊戲自己的取法），不是 `player.read()`
—— 換地圖空窗那支會回一億五千萬的差額，見 `bag.gold()` 檔頭那段實測。

全程只讀記憶體，不寫入、不注入。
"""
from __future__ import annotations

import threading
import time

from app.game import bag


class Loot:
    """一台分身的收穫累計器。

    ⚠ `bought()` 是**背景執行緒**（補給那條）呼叫的，`update()` / `rows()`
      在 UI 執行緒 —— 所有共用資料都在同一把鎖底下改。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    # -- 對外狀態 -------------------------------------------------------
    def reset(self) -> None:
        """歸零（「重新計算」鈕）。基準也一起丟掉 —— 下一拍重建。

        ⚠ 基準設 None 而不是留著：留著的話，歸零到下一拍之間背包的變動會被
          算進新的一輪，看起來就像「才剛按重置就有東西」。
        """
        with self._lock:
            self.since = time.time()
            self.gold = 0
            # 種類id → [累計數量, 圖示編號, 最後一次增加的時間, 第幾次增加]
            # ⚠ 排序要用最後那個流水號當第二鍵：同一拍進來的好幾種東西時間戳
            #   會一模一樣（`time.time()` 的解析度不夠），只看時間的話順序
            #   等於「誰先被 dict 走到」，看起來就像沒照新舊排。
            self._items: dict[int, list] = {}
            self._seq = 0
            self._prev: dict[int, int] | None = None   # 上一拍的整袋數量
            self._prev_gold: int | None = None
            self._pending: dict[int, int] = {}         # 買來的待扣帳
            self._who: str | None = None               # 上一拍是哪隻角色

    def rows(self) -> list[tuple[int, int, int, float]]:
        """[(種類id, 累計數量, 圖示編號, 最後獲得時間)]，**新的在上面**。

        跟商店／商城兩張紀錄表同一個順序（使用者習慣新的在最上面）；
        同一種東西只有一列，數量累加。
        """
        with self._lock:
            rows = [(tid, v[0], v[1], v[2], v[3])
                    for tid, v in self._items.items()]
        rows.sort(key=lambda r: (r[3], r[4]), reverse=True)
        return [r[:4] for r in rows]

    def kinds(self) -> int:
        """累計到幾種東西（畫面上的「共 N 種」）。"""
        with self._lock:
            return len(self._items)

    # -- 記帳 -----------------------------------------------------------
    def bought(self, type_id: int, qty: int) -> None:
        """買到的不算收穫。**背景執行緒呼叫**：只碰純資料，不碰 Qt。

        數量是補給那趟的背包實測差額（`supply.run_buy` 的 ledger 給什麼就是
        什麼，不猜）。先從已經累計的倒扣，扣不完的掛帳等下一拍的正差值扣。
        """
        type_id, qty = int(type_id), int(qty)
        if qty <= 0:
            return
        with self._lock:
            cur = self._items.get(type_id)
            if cur is not None:
                take = min(qty, cur[0])
                cur[0] -= take
                qty -= take
                if cur[0] <= 0:
                    del self._items[type_id]
            if qty > 0:
                self._pending[type_id] = self._pending.get(type_id, 0) + qty

    # -- 主要心跳 -------------------------------------------------------
    def update(self, scanner, who: str | None = None) -> bool:
        """對帳一拍。回傳「這一拍算不算數」（False ＝ 讀不到，基準沒動）。

        `who` 是角色識別（角色名／帳號都行）。⚠ 換人了就只重建基準不累加 ——
        斷線重登會換到別隻角色（memory 的 auto-login-memory-driven：空視窗
        會洗牌），不擋的話**別人整袋的東西**會被算成這一趟的收穫。
        """
        items, complete = bag.scan(scanner)
        if not complete:
            return False                     # ⚠⚠ 讀不到 ≠ 沒有，整拍作廢
        gold = bag.gold(scanner)
        if gold is None:
            return False
        cur: dict[int, int] = {}
        icons: dict[int, int] = {}
        for it in items:
            cur[it.type_id] = cur.get(it.type_id, 0) + it.count
            if it.icon_id:
                icons[it.type_id] = it.icon_id
        now = time.time()
        with self._lock:
            same_who = who is None or self._who is None or who == self._who
            self._who = who if who is not None else self._who
            if self._prev is None or self._prev_gold is None or not same_who:
                # 第一拍（或剛歸零／剛換人）：只建基準，不算收穫。
                self._prev, self._prev_gold = cur, gold
                return True
            for tid, n in cur.items():
                gain = n - self._prev.get(tid, 0)
                if gain <= 0:
                    continue
                # 買來的先扣（`bought()` 掛的待扣帳）
                owed = self._pending.get(tid, 0)
                if owed:
                    take = min(owed, gain)
                    gain -= take
                    if owed - take:
                        self._pending[tid] = owed - take
                    else:
                        del self._pending[tid]
                if gain <= 0:
                    continue
                self._seq += 1
                slot = self._items.get(tid)
                if slot is None:
                    self._items[tid] = [gain, icons.get(tid, 0), now, self._seq]
                else:
                    slot[0] += gain
                    slot[2], slot[3] = now, self._seq
                    if not slot[1] and icons.get(tid):
                        slot[1] = icons[tid]
            if gold > self._prev_gold:
                self.gold += gold - self._prev_gold
            self._prev, self._prev_gold = cur, gold
        return True
