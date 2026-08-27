"""強化裝備：對著裝備使用強化錘，**每一發都驗結果**。

    run = enhance.Run(scanner, mover, gear_slot=22, serial=..., target=10)
    while not run.done:
        for ev in run.tick():
            print(ev.kind, ev.text)

## 送的是什麼

強化 ＝ **對物品使用物品**，跟吃藥、用回程翼是同一個封包：

    0x2E：[u16 代號][u8 道具格號][u32 目標格號]      （格號 > 255 走 0x132）

出處：2026-08-28 使用者實機擷取一發強化，呼叫鏈落在 UI 指令
`useitem2itemconfirm`（`0x59814F`）裡，它呼叫 `0x5D8B69(道具格號, 目標格號)`；
擷取到的參數是 (0x15, 0x16)，當場讀那台的背包 —— **格 21 是強化錘、
格 22 是羅丹的神聖打擊**，完全對上。

★ **那支函式就是 `recall.USE_ITEM_FN`**（吃藥／用翼用的同一支，
  平常第二個參數填 0）—— 已經在 `locate.SIGS` 裡，改版自動跟上，
  這裡**不另外登記特徵**（同一個位址不准寫第二份）。

## ⚠⚠ 一般強化錘失敗 → **裝備直接消失**（使用者 2026-08-28 說明）

（祝福強化錘失敗是退等，但目前只做一般強化錘。）
所以每一發之後都要重讀那一格，而且**認 serial 不認格號、不認指標** ——
換裝那次踩過「陣列指標不動、內容互換」的坑（[[bag-container-path]]）。

| 讀到 | 判定 |
|---|---|
| serial 同、`+0x52` 加 1 | 成功，繼續打到目標 |
| **確定讀到**、那格空了 | **失敗消失** → 立刻停 |
| serial 變了（那格換成別件） | 驗不了 → 停（**不算「被打掉」**）|
| serial 同、`+0x52` 變小 | 退等 → 立刻停 |
| 完全沒變，而且**強化錘數量也沒少** | 沒送出去 → 補送（有上限） |
| 完全沒變，但強化錘少了 | 判不出來 → **停**，不再打 |
| **讀不到**（背包表頭／物件／掃描任一失敗）| 等 `UNREADABLE_SECS`，還是讀不到就說**驗不了**並停 |

最後兩列是刻意的：分不出結果就不准繼續動一件會消失的東西
（[[confirm-and-resend]]「驗不了不能當失敗」的反面 —— 這裡驗不了要停）。

## ⛔ 「讀不到」不是「不見了」（2026-08-28 /_audit）

`gear.read()` 有六種理由回 `None`，四種是暫時讀不到（換地圖、背包正在搬、
頁面沒映射）。原本六種一律判成「強化失敗消失」—— 東西好好的，畫面卻對著
使用者宣告他的裝備沒了（[[bag-false-empty-guards]]，這個坑復發第七次）。
現在一律走 `gear.read_state()` 的三態；錘子數量也只認 `bag.scan` **掃完整**
的結果，否則「沒送出去 → 補送」會再花一支錘子。回歸測試 `tools\\enhance_check.py`。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.game import attack, bag, gear, itemname, recall

# 強化錘的種類 ID。★ 只認這一個 —— 6895~6904 是「N星祝福強化錘」，
# 失敗行為不一樣（退等），使用者指定目前只做一般強化錘。
HAMMER_TYPE = 3121

# 遊戲的強化上限（使用者 2026-08-28 確認）。
MAX_LEVEL = 15

# 送出之後最多等多久看結果（毫秒）。
WAIT_MS = 6000
# 「確定沒送出去」才補送，而且最多補這麼多次
# （[[transient-failure-auto-retry]]：會消耗東西的動作一定要有上限）。
MAX_RESEND = 3
# 背包讀不到時，最多再等這麼久才放棄（秒）。★ 這**不是**重試上限的例外：
# 讀不到期間我們什麼都沒送、也沒消耗東西，等的目的只是「別把讀取失敗當成
# 裝備消失」。等完還是讀不到就**大聲停手說驗不了**，不猜結果（CLAUDE.md：
# 失效只允許大聲停用或安全退化）。
UNREADABLE_SECS = 3.0

SUCCESS = "success"
GONE = "gone"
DOWNGRADE = "downgrade"
BLOCKED = "blocked"          # 開不了工（沒錘子／不是裝備／已滿級）
UNKNOWN = "unknown"          # 驗不出來 —— 停手
DONE = "done"
SWAPPED = "swapped"          # 讀到了，但那一格已經換成別件（serial 不同）


@dataclass(frozen=True)
class Event:
    kind: str
    text: str
    level: int = 0


def hammers(scanner) -> tuple[int | None, int, bool]:
    """(第一個強化錘的格號, 總數, **背包整段真的掃完了嗎**)。

    ⚠ 第三個值不能省：掃不完整的時候「找不到錘子」跟「真的沒錘子」長得一模一樣
      （[[bag-false-empty-guards]]）。錘子數量還被 `_check()` 拿去分辨「沒送出去
      （可以補送）」與「扣掉了（不准再送）」—— 拿沒掃完的數字去比，會把
      「其實已經送出去了」判成「沒送出去」而**再花一支錘子**。
    ⚠ 每次送出前都要重找一次 —— 背包會變，格號不能記著用
      （CLAUDE.md：交給遊戲的格號送出前當場重讀重驗）。
    """
    items, complete = bag.scan(scanner)
    total = 0
    slot = None
    for it in items:
        if it.type_id == HAMMER_TYPE:
            total += it.count
            if slot is None:
                slot = it.slot
    return slot, total, complete


def find_hammer(scanner) -> tuple[int, int] | None:
    """背包裡的強化錘 (格號, 總數)；沒有回 None。

    ⚠ 分不出「沒掃到」與「真的沒有」—— 要判斷數量變化一律用 `hammers()`。
    """
    slot, total, _complete = hammers(scanner)
    return (slot, total) if slot is not None else None


def strike(mover, hammer_slot: int, gear_slot: int) -> bool:
    """送一發「對第 gear_slot 格的裝備使用第 hammer_slot 格的強化錘」。"""
    if not (mover and mover.active):
        return False
    if not 0 <= hammer_slot <= 0xFF or not 0 <= gear_slot < bag.MAX_SLOTS:
        return False
    return attack._send(mover, ((recall.USE_ITEM_FN, (hammer_slot, gear_slot)),))


class Run:
    """把一件裝備打到目標次數的狀態機；由分頁每一拍呼叫 `tick()`。

    `serial` 是開工當下那件裝備的 `+0x00`：**中途變了就是換人了**，一律停。
    """

    def __init__(self, scanner, mover, gear_slot: int, serial: int,
                 target: int, name: str = "") -> None:
        self.sc = scanner
        self.mover = mover
        self.slot = gear_slot
        self.serial = serial
        self.target = min(int(target), MAX_LEVEL)
        self.name = name
        self.done = False
        self.sent_at = 0.0
        self.before = -1
        self.hammer_before = -1
        self.resend = 0
        self.tries = 0
        # 寬限期起點 **一種讀不到記一個**。⚠⚠ 一開始只用一個共用的計時器，
        # 結果「裝備讀到了、但背包沒掃完」那條路每一拍都被裝備那邊的歸零洗掉，
        # 寬限期永遠不會到 → 狀態機無聲卡死在「強化中…」
        # （[[frozen-tick-state-machines]]，enhance_check ⑤ 抓到）。
        self.grace: dict[str, float] = {}

    # ------------------------------------------------------------------
    def _wait_readable(self, why: str) -> bool:
        """讀不到 —— 還在寬限期內就回 True（叫呼叫端這一拍先別下結論）。"""
        now = time.monotonic()
        return (now - self.grace.setdefault(why, now)) < UNREADABLE_SECS

    def _stop(self, kind: str, text: str, level: int = 0) -> list[Event]:
        self.done = True
        return [Event(kind, text, level)]

    def _current(self) -> tuple[gear.Gear | None, str]:
        """重讀那一格，**而且要是同一件**（認 serial）。

        ⚠⚠ 回的第二個值一定要看：`None` 有兩種意思，「讀不到」跟「真的不在了」。
          2026-08-28 /_audit 之前這裡把兩種都當成不在，於是遊戲換個地圖、
          或背包剛好在搬，畫面就跳出「強化失敗消失」—— 對著使用者宣告他的
          裝備被打掉了，其實東西好好的（[[bag-false-empty-guards]]）。
        """
        g, st = gear.read_state(self.sc, self.slot)
        if g is not None and g.serial != self.serial:
            # 讀到了，但不是同一件 —— 那一格換人了，追不下去（但也不是「被打掉」）
            return None, SWAPPED
        return g, st

    def tick(self) -> list[Event]:
        if self.done:
            return []
        if self.sent_at:
            return self._check()
        return self._send()

    # ------------------------------------------------------------------
    def _send(self) -> list[Event]:
        g, st = self._current()
        if g is None:
            # 還沒送出任何東西，重讀完全安全 —— 讀不到就等下一拍
            if st == gear.READ_UNREADABLE and self._wait_readable("gear"):
                return []
            if st == gear.READ_UNREADABLE:
                return self._stop(UNKNOWN,
                                  f"{self.name} 連 {UNREADABLE_SECS:.0f} 秒讀不到"
                                  f"背包（換地圖了？），沒有動手")
            if st == SWAPPED:
                return self._stop(BLOCKED, f"{self.name} 那一格換成別的東西了")
            return self._stop(GONE, f"{self.name} 不在背包裡了")
        self.grace.pop("gear", None)
        if g.enhance >= self.target:
            return self._stop(DONE, f"{self.name} 已經是 +{g.enhance}", g.enhance)
        if g.enhance >= MAX_LEVEL:
            return self._stop(BLOCKED, f"{self.name} 已達強化上限 +{MAX_LEVEL}",
                              g.enhance)
        hslot, hcount, complete = hammers(self.sc)
        if hslot is None:
            if not complete and self._wait_readable("hammer"):
                return []          # 沒掃完就說「沒錘子」＝又一次讀不到當沒有
            return self._stop(BLOCKED,
                              f"背包裡沒有{itemname.of(HAMMER_TYPE) or '強化錘'}"
                              if complete else "背包掃不完整，讀不到錘子")
        if complete:
            self.grace.pop("hammer", None)
        self.before = g.enhance
        self.hammer_before = hcount
        self.tries += 1
        if not strike(self.mover, hslot, self.slot):
            return self._stop(BLOCKED, "跳板沒裝好，送不出去")
        self.sent_at = time.monotonic()
        return []

    def _check(self) -> list[Event]:
        g, st = self._current()
        if g is None:
            # ⚠⚠ 只有**確定讀到、而且那一格是空的**才敢說「被打掉了」。
            #   讀不到就再等幾拍：換地圖／背包正在搬的時候整條路都會讀不到，
            #   把它當成消失＝對使用者謊報裝備沒了。
            if st == gear.READ_UNREADABLE:
                if self._wait_readable("gear"):
                    return []
                return self._stop(UNKNOWN,
                                  f"{self.name} 送出後連 {UNREADABLE_SECS:.0f} 秒"
                                  f"讀不到背包，這一發的結果**沒驗到**，"
                                  f"停手（自己確認一下）", self.before)
            if st == SWAPPED:
                return self._stop(UNKNOWN,
                                  f"{self.name} 那一格換成別的東西了，"
                                  f"驗不出這一發的結果，停手", self.before)
            # ★ 這就是一般強化錘失敗的樣子：東西直接從背包消失
            return self._stop(GONE, f"{self.name} 強化失敗消失", self.before)
        self.grace.pop("gear", None)
        if g.enhance > self.before:
            self.sent_at = 0.0
            self.resend = 0
            evs = [Event(SUCCESS, f"{self.name} 強化成功 → +{g.enhance}",
                         g.enhance)]
            if g.enhance >= self.target:
                self.done = True
                evs.append(Event(DONE, f"{self.name} 已達目標 +{g.enhance}",
                                 g.enhance))
            return evs
        if g.enhance < self.before:
            return self._stop(DOWNGRADE,
                              f"{self.name} 強化失敗退等 → +{g.enhance}",
                              g.enhance)
        if (time.monotonic() - self.sent_at) * 1000 < WAIT_MS:
            return []
        # 等到超時都沒變 —— 分兩種情況，差別在強化錘有沒有少
        _hslot, now, complete = hammers(self.sc)
        if not complete:
            # ⛔ 沒掃完的數量不准拿來比：數少了可能只是沒掃到，判成「沒送出去」
            #   就會再花一支錘子打一件我們其實不知道狀態的裝備。
            if self._wait_readable("hammer"):
                return []
            return self._stop(UNKNOWN,
                              f"{self.name} 背包掃不完整，數不準錘子，"
                              f"驗不出這一發的結果，停手（自己確認一下）",
                              self.before)
        self.grace.pop("hammer", None)
        if now == self.hammer_before:
            if self.resend >= MAX_RESEND:
                return self._stop(UNKNOWN,
                                  f"{self.name} 連送 {MAX_RESEND} 次都沒反應，停手")
            self.resend += 1
            self.sent_at = 0.0
            return [Event(UNKNOWN,
                          f"{self.name} 沒送出去，第 {self.resend} 次補送")]
        # 錘子少了卻沒有任何變化 → 判不出結果，**不准再打**
        return self._stop(UNKNOWN,
                          f"{self.name} 錘子扣掉了但看不出結果，停手（自己確認一下）")
