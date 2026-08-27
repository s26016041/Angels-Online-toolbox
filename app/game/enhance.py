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
| 那格空了 / serial 變了 | **失敗消失** → 立刻停 |
| serial 同、`+0x52` 變小 | 退等 → 立刻停 |
| 完全沒變，而且**強化錘數量也沒少** | 沒送出去 → 補送（有上限） |
| 完全沒變，但強化錘少了 | 判不出來 → **停**，不再打 |

最後一列是刻意的：分不出結果就不准繼續動一件會消失的東西
（[[confirm-and-resend]]「驗不了不能當失敗」的反面 —— 這裡驗不了要停）。
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

SUCCESS = "success"
GONE = "gone"
DOWNGRADE = "downgrade"
BLOCKED = "blocked"          # 開不了工（沒錘子／不是裝備／已滿級）
UNKNOWN = "unknown"          # 驗不出來 —— 停手
DONE = "done"


@dataclass(frozen=True)
class Event:
    kind: str
    text: str
    level: int = 0


def find_hammer(scanner) -> tuple[int, int] | None:
    """背包裡的強化錘 (格號, 總數)；沒有回 None。

    ⚠ 每次送出前都要重找一次 —— 背包會變，格號不能記著用
      （CLAUDE.md：交給遊戲的格號送出前當場重讀重驗）。
    """
    items, complete = bag.scan(scanner)
    if not complete and not items:
        return None
    total = 0
    slot = None
    for it in items:
        if it.type_id == HAMMER_TYPE:
            total += it.count
            if slot is None:
                slot = it.slot
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

    # ------------------------------------------------------------------
    def _stop(self, kind: str, text: str, level: int = 0) -> list[Event]:
        self.done = True
        return [Event(kind, text, level)]

    def _current(self) -> gear.Gear | None:
        """重讀那一格，**而且要是同一件**（認 serial）。"""
        g = gear.read(self.sc, self.slot)
        if g is None or g.serial != self.serial:
            return None
        return g

    def tick(self) -> list[Event]:
        if self.done:
            return []
        if self.sent_at:
            return self._check()
        return self._send()

    # ------------------------------------------------------------------
    def _send(self) -> list[Event]:
        g = self._current()
        if g is None:
            return self._stop(GONE, f"{self.name} 不見了（強化前就讀不到）")
        if g.enhance >= self.target:
            return self._stop(DONE, f"{self.name} 已經是 +{g.enhance}", g.enhance)
        if g.enhance >= MAX_LEVEL:
            return self._stop(BLOCKED, f"{self.name} 已達強化上限 +{MAX_LEVEL}",
                              g.enhance)
        got = find_hammer(self.sc)
        if got is None:
            return self._stop(BLOCKED,
                              f"背包裡沒有{itemname.of(HAMMER_TYPE) or '強化錘'}")
        hslot, hcount = got
        self.before = g.enhance
        self.hammer_before = hcount
        self.tries += 1
        if not strike(self.mover, hslot, self.slot):
            return self._stop(BLOCKED, "跳板沒裝好，送不出去")
        self.sent_at = time.monotonic()
        return []

    def _check(self) -> list[Event]:
        g = self._current()
        if g is None:
            # ★ 這就是一般強化錘失敗的樣子：東西直接從背包消失
            return self._stop(GONE, f"{self.name} 強化失敗消失", self.before)
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
        got = find_hammer(self.sc)
        now = got[1] if got else 0
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
