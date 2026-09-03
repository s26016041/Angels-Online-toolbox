"""自動打孔：對一件裝備輪流用「N星打孔錘」與寶石，直到孔數到目標。

    run = holes.Run(scanner, mover, gear_slot=50, serial=..., target=5, gem_cap=40)
    while not run.done:
        for ev in run.tick():
            print(ev.kind, ev.text)

## 送的是什麼 —— 跟強化**完全同一支函式**（2026-09-03 使用者實機擷取）

`封包/打孔.txt` 與 `封包/鑲嵌寶石.txt` 兩份擷取的呼叫鏈一模一樣：
UI 指令 `useitem2itemconfirm`（0x599075）→ `0x5D97C8(道具格號, 目標格號)` →
封包 0x2E（`push 7 / push 0x2E`，格號 > 255 走 0x132）。`0x5D97C8` 就是這一版
`locate.warm()` 定位出來的 **`recall.USE_ITEM_FN`**。擷取到的參數 (0x34, 0x32)
與 (0x15, 0x32) 當場對上那台背包：**格 52 = 13星打孔錘、格 21 = 寶石、
格 50 = 狂暴衝擊戰靴**（事後讀到 2 孔、鑲了 3115／3100）。

→ 打孔 ＝ `enhance.strike(錘子格, 裝備格)`、鑲嵌 ＝ `enhance.strike(寶石格, 裝備格)`。
  同一個位址不登記第二份特徵（CLAUDE.md）。

## 規則 —— 全部來自遊戲自己的說明文字＋記憶體欄位，沒有猜的

**打孔錘**
* 範本分類 `+0x18 == 37`（item.xml「打孔道具」；✅ 13星打孔錘 14636 實機 kind=37）。
* 星級 ＝ 物品等級（範本 `+0x34`）`// 10`：0星=1、1星=11、11星=110、13星=130、
  13星祝福=131 → 跟名字的星數全部對上；名字對不上的那支**不用**（不猜）。
* 說明文字「139級以下的武器裝備所使用的打孔錘」→ **裝備物品等級 // 10 ≤ 星級**
  （✅ 實機：戰靴物品等級 120、等級限制 0，用 13星成功 → 比的是物品等級不是等級限制）。
* ⛔ **祝福打孔錘不用**（使用者 2026-09-03 指定）：範本動態資料1 `+0x108 == 1`
  或名字含「祝福」都排除。
* ⚠ 一般打孔錘失敗 → **裝備毀損**（祝福錘的說明文字反證：「即使打孔失敗也
  不會導致裝備毀損」）。所以每一發都要重讀那一格、認 serial。
* 同時有好幾種星級時挑**夠用的最低星級**（貴的留著）。

**寶石**
* 範本分類 `+0x18 == 29`（item.xml「寶石」；✅ 完美的黃寶石 3105 實機 kind=29）。
* 動態資料1 ＝ 寶石效果編號 → 寶石效果表 `[[gear.JEWEL_TABLE_PTR] + 編號*4]` 那一列
  **`+0x6C` 最低等級（＝說明文字的「裝備等限」）、`+0x70` 等級上限**
  （✅ 實機 row[15] = (80, 450) ↔ jeweleffect.xml 編號 15 完美的黃寶石
  最低等級 80、等級上限 450）。
* 可鑲：**最低等級 ≤ 裝備物品等級 ≤ 等級上限**，而且最低等級 ≤ 使用者選的
  「寶石等限上限」（只拿便宜的當墊子，好寶石不動）。挑符合的裡面**最低等級最低**的。
* 表讀不到／數值不合理的那顆**不用**。

**流程**（使用者說明：每打一孔要先把寶石鑲進去才能打下一孔；最多 5 孔）
* 有空孔 → 先鑲；沒空孔 → 打孔。孔數到目標就停，**最後那個孔留空**
  （要鑲什麼由使用者自己決定）。

## 每一發都驗結果（跟 `enhance.Run` 同一套三態）

| 讀到 | 判定 |
|---|---|
| serial 同、孔數 +1／那個孔有寶石了 | 成功，繼續 |
| **確定讀到**、那格空了 | 打孔失敗毀損 → 停 |
| serial 變了 | 驗不了 → 停 |
| 沒變、用掉的那種東西數量也沒少 | 沒送出去 → 補送（上限 MAX_RESEND） |
| 沒變、但東西少了 | 判不出來 → 停 |
| 讀不到 | 等 UNREADABLE_SECS，還是讀不到就說驗不了並停 |

回歸測試 `tools\\holes_check.py`。
"""
from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass

from app.game import bag, enhance, gear, itemname
from app.game.enhance import (BLOCKED, DONE, GONE, SWAPPED, SUCCESS, UNKNOWN,
                              Event)

# 範本分類代號（範本 +0x18；bag.py 那份「1:1 對到 item.xml 物品類別」）。
# ✅ 2026-09-03 實機：13星打孔錘 14636 → 37、完美的黃寶石 3105 → 29。
KIND_HAMMER = 37            # item.xml「打孔道具」
KIND_GEM = 29               # item.xml「寶石」

# 寶石效果表一列（gear.JEWEL_ROW_* 那三組 0x24 bytes 之後）
JEWEL_MIN_LEVEL = 0x6C      # 最低等級 ＝ 說明文字「裝備等限」
JEWEL_MAX_LEVEL = 0x70      # 等級上限
LEVEL_SANE = 1000           # 等級超過這個就是讀到垃圾

MAX_HOLES = 5               # 遊戲上限（使用者 2026-09-03 說明）
STAR_SPAN = 10              # 一星涵蓋 10 級：13星 → 130~139 級以下都能打

WAIT_MS = enhance.WAIT_MS
MAX_RESEND = enhance.MAX_RESEND
UNREADABLE_SECS = enhance.UNREADABLE_SECS

PUNCH = "punch"
INLAY = "inlay"

# 只認這種名字；「祝福」「禮盒」「兌換券」都對不上
_HAMMER_NAME = re.compile(r"^(\d+)星打孔錘$")


@dataclass(frozen=True)
class Hammer:
    slot: int
    type_id: int
    name: str
    star: int
    count: int


@dataclass(frozen=True)
class Gem:
    slot: int
    type_id: int
    name: str
    effect: int
    min_level: int
    max_level: int
    count: int
    icon_id: int = 0            # 範本 +0x00 圖示編號（寶石背包畫圖用）


def star_of(level: int) -> int:
    """物品等級 → 星級（錘子）或「第幾段」（裝備）。"""
    return max(int(level), 0) // STAR_SPAN


# ---------------------------------------------------------------------------
def hammers(scanner, items: list[bag.Item] | None = None,
            complete: bool = True) -> tuple[list[Hammer], bool]:
    """(背包裡能用的一般打孔錘 —— 星級由低到高, 背包掃完了嗎)。

    ⚠ 第二個值不能省（[[bag-false-empty-guards]]）：沒掃完的「沒錘子」跟真的沒有
      長得一樣，呼叫端要下「沒有」結論一定要看它。
    ⚠ 每次送出前重找 —— 格號不能記著用（背包會變）。
    """
    if items is None:
        items, complete = bag.scan(scanner)
    out: list[Hammer] = []
    for it in items:
        if it.kind != KIND_HAMMER or it.param1 != 0:
            continue                                # 祝福錘（動態資料1=1）不用
        name = itemname.of(it.type_id)
        m = _HAMMER_NAME.match(name)
        if not m:
            continue                                # 祝福／認不得的名字都不用
        star = star_of(it.level)
        if int(m.group(1)) != star:
            continue                                # 名字跟等級對不上 → 不猜
        out.append(Hammer(it.slot, it.type_id, name, star, it.count))
    out.sort(key=lambda h: (h.star, h.slot))
    return out, complete


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    if not raw or len(raw) != 4:
        return None
    return struct.unpack("<I", bytes(raw))[0]


def jewel_levels(scanner, effect: int) -> tuple[int, int] | None:
    """寶石效果表那一列的 (最低等級, 等級上限)；讀不到／不合理回 None。"""
    if not 0 < effect < 0x10000:
        return None
    table = _u32(scanner, gear.JEWEL_TABLE_PTR)
    if table is None or not 0x10000 < table < 0x7FFF0000:
        return None
    row = _u32(scanner, table + effect * 4)
    if row is None or not 0x10000 < row < 0x7FFF0000:
        return None
    raw = scanner._read_bytes(row + JEWEL_MIN_LEVEL, 8)
    if not raw or len(raw) != 8:
        return None
    lo, hi = struct.unpack("<ii", bytes(raw))
    if not 0 <= lo <= hi <= LEVEL_SANE:
        return None
    return lo, hi


def gems(scanner, items: list[bag.Item] | None = None,
         complete: bool = True) -> tuple[list[Gem], bool]:
    """(背包裡查得到等限的寶石 —— 等限由低到高, 背包掃完了嗎)。"""
    if items is None:
        items, complete = bag.scan(scanner)
    cache: dict[int, tuple[int, int] | None] = {}
    out: list[Gem] = []
    for it in items:
        if it.kind != KIND_GEM or it.param1 <= 0:
            continue
        if it.param1 not in cache:
            cache[it.param1] = jewel_levels(scanner, it.param1)
        lv = cache[it.param1]
        if lv is None:
            continue                                # 表讀不到 → 這顆不用
        out.append(Gem(it.slot, it.type_id, it.name, it.param1, lv[0], lv[1],
                       it.count, it.icon_id))
    out.sort(key=lambda g: (g.min_level, g.slot))
    return out, complete


def pick_hammer(hs: list[Hammer], gear_level: int) -> Hammer | None:
    """夠用的最低星級：裝備物品等級 // 10 ≤ 星級。"""
    need = star_of(gear_level)
    for h in sorted(hs, key=lambda h: (h.star, h.slot)):
        if h.star >= need:
            return h
    return None


def pick_gem(gs: list[Gem], gear_level: int, cap: int,
             gem_type: int | None = None) -> Gem | None:
    """這件裝備鑲得進去（最低 ≤ 物品等級 ≤ 上限）的寶石裡挑一顆。

    二選一（使用者 2026-09-03）：
    * `gem_type` 有給 → **只用那一種**（等限上限 `cap` 不看）；
    * 沒給 → 等限 ≤ `cap` 裡最便宜的那顆。
    """
    for g in sorted(gs, key=lambda g: (g.min_level, g.slot)):
        if gem_type is not None:
            if g.type_id != gem_type:
                continue
        elif g.min_level > cap:
            continue
        if g.min_level <= gear_level <= g.max_level:
            return g
    return None


def count_of(scanner, type_id: int) -> tuple[int, bool]:
    """(這種東西背包裡總共幾個, 背包掃完了嗎)。分辨「沒送出去」與「扣掉了」用。"""
    items, complete = bag.scan(scanner)
    return sum(it.count for it in items if it.type_id == type_id), complete


# ---------------------------------------------------------------------------
class Run:
    """把一件裝備打到目標孔數的狀態機；由分頁每一拍呼叫 `tick()`。"""

    def __init__(self, scanner, mover, gear_slot: int, serial: int,
                 target: int, gem_cap: int, name: str = "",
                 gem_type: int | None = None) -> None:
        self.sc = scanner
        self.mover = mover
        self.slot = gear_slot
        self.serial = serial
        self.target = max(1, min(int(target), MAX_HOLES))
        self.gem_cap = int(gem_cap)
        self.gem_type = int(gem_type) if gem_type else None   # 指定寶石（二選一）
        self.name = name
        self.done = False
        self.sent_at = 0.0
        self.phase = ""
        self.before_holes = -1
        self.hole_idx = -1              # 這一發要鑲的是第幾個孔
        self.used_type = 0              # 這一發用掉的是哪種東西
        self.used_name = ""
        self.used_before = -1
        self.resend = 0
        self.tries = 0
        # 一種讀不到記一個計時器（[[frozen-tick-state-machines]]，enhance 踩過）
        self.grace: dict[str, float] = {}

    # ------------------------------------------------------------------
    def _wait_readable(self, why: str) -> bool:
        now = time.monotonic()
        return (now - self.grace.setdefault(why, now)) < UNREADABLE_SECS

    def _stop(self, kind: str, text: str, level: int = 0) -> list[Event]:
        self.done = True
        return [Event(kind, text, level)]

    def _current(self) -> tuple[gear.Gear | None, str]:
        g, st = gear.read_state(self.sc, self.slot)
        if g is not None and g.serial != self.serial:
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
        if g.holes >= self.target:
            return self._stop(DONE, f"{self.name} 已經有 {g.holes} 孔", g.holes)
        if g.holes >= MAX_HOLES:
            return self._stop(BLOCKED, f"{self.name} 已達 {MAX_HOLES} 孔上限",
                              g.holes)
        if "level" not in g.base:
            # 範本讀不到 → 不知道裝備幾級，錘子與寶石都挑不出來；等，不猜
            if self._wait_readable("tmpl"):
                return []
            return self._stop(UNKNOWN, f"{self.name} 讀不到範本（物品等級），沒有動手")
        self.grace.pop("tmpl", None)
        level = g.base["level"]

        empty = next((i for i, x in enumerate(g.gems[:g.holes]) if not x), None)
        if empty is not None:
            return self._send_inlay(g, level, empty)
        return self._send_punch(g, level)

    def _send_inlay(self, g: gear.Gear, level: int, idx: int) -> list[Event]:
        gs, complete = gems(self.sc)
        gem = pick_gem(gs, level, self.gem_cap, self.gem_type)
        if gem is None:
            if not complete and self._wait_readable("gem"):
                return []
            if not complete:
                return self._stop(BLOCKED, "背包掃不完整，讀不到寶石")
            if self.gem_type is not None:
                want = itemname.of(self.gem_type) or f"種類 {self.gem_type}"
                return self._stop(BLOCKED,
                                  f"背包裡沒有能鑲的「{want}」（用完了，或它鑲不進 "
                                  f"{level} 級的裝備）—— 第 {idx + 1} 孔還空著")
            return self._stop(BLOCKED,
                              f"沒有能鑲的寶石（等限 ≤ {self.gem_cap} 級、"
                              f"而且要鑲得進 {level} 級的裝備）—— 第 {idx + 1} 孔還空著")
        self.grace.pop("gem", None)
        total, complete = count_of(self.sc, gem.type_id)
        if not complete:
            if self._wait_readable("count"):
                return []
            return self._stop(BLOCKED, "背包掃不完整，數不準寶石")
        self.grace.pop("count", None)
        self.phase = INLAY
        self.hole_idx = idx
        self.used_type, self.used_name, self.used_before = gem.type_id, gem.name, total
        self.tries += 1
        if not enhance.strike(self.mover, gem.slot, self.slot):
            return self._stop(BLOCKED, "跳板沒裝好，送不出去")
        self.sent_at = time.monotonic()
        return []

    def _send_punch(self, g: gear.Gear, level: int) -> list[Event]:
        hs, complete = hammers(self.sc)
        h = pick_hammer(hs, level)
        if h is None:
            if not complete and self._wait_readable("hammer"):
                return []
            if not complete:
                return self._stop(BLOCKED, "背包掃不完整，讀不到打孔錘")
            need = star_of(level)
            return self._stop(BLOCKED,
                              f"沒有 {need} 星以上的一般打孔錘（{self.name} 是 "
                              f"{level} 級；祝福打孔錘不用）")
        self.grace.pop("hammer", None)
        total, complete = count_of(self.sc, h.type_id)
        if not complete:
            if self._wait_readable("count"):
                return []
            return self._stop(BLOCKED, "背包掃不完整，數不準打孔錘")
        self.grace.pop("count", None)
        self.phase = PUNCH
        self.before_holes = g.holes
        self.used_type, self.used_name, self.used_before = h.type_id, h.name, total
        self.tries += 1
        if not enhance.strike(self.mover, h.slot, self.slot):
            return self._stop(BLOCKED, "跳板沒裝好，送不出去")
        self.sent_at = time.monotonic()
        return []

    # ------------------------------------------------------------------
    def _check(self) -> list[Event]:
        g, st = self._current()
        if g is None:
            if st == gear.READ_UNREADABLE:
                if self._wait_readable("gear"):
                    return []
                return self._stop(UNKNOWN,
                                  f"{self.name} 送出後連 {UNREADABLE_SECS:.0f} 秒"
                                  f"讀不到背包，這一發的結果**沒驗到**，"
                                  f"停手（自己確認一下）")
            if st == SWAPPED:
                return self._stop(UNKNOWN,
                                  f"{self.name} 那一格換成別的東西了，"
                                  f"驗不出這一發的結果，停手")
            # ★ 確定讀到、格子空了 —— 一般打孔錘失敗就是這個樣子
            if self.phase == PUNCH:
                return self._stop(GONE, f"{self.name} 打孔失敗，裝備毀損")
            return self._stop(GONE, f"{self.name} 不在背包裡了（鑲嵌中）")
        self.grace.pop("gear", None)

        if self.phase == PUNCH:
            if g.holes > self.before_holes:
                self.sent_at = 0.0
                self.resend = 0
                evs = [Event(SUCCESS,
                             f"{self.name} 打孔成功 → {g.holes} 孔"
                             f"（用 {self.used_name}）", g.holes)]
                if g.holes >= self.target:
                    self.done = True
                    evs.append(Event(DONE,
                                     f"{self.name} 已達目標 {g.holes} 孔，"
                                     f"最後一孔留空給你自己鑲", g.holes))
                return evs
            if g.holes < self.before_holes:
                return self._stop(UNKNOWN,
                                  f"{self.name} 孔數反而變少（{self.before_holes}"
                                  f"→{g.holes}），停手（自己確認一下）", g.holes)
        else:
            if self.hole_idx < g.holes and g.gems[self.hole_idx]:
                self.sent_at = 0.0
                self.resend = 0
                return [Event(SUCCESS,
                              f"{self.name} 第 {self.hole_idx + 1} 孔鑲上 "
                              f"{self.used_name}", g.holes)]

        if (time.monotonic() - self.sent_at) * 1000 < WAIT_MS:
            return []
        # 逾時沒變 —— 看用掉的那種東西有沒有少
        now, complete = count_of(self.sc, self.used_type)
        if not complete:
            if self._wait_readable("count"):
                return []
            return self._stop(UNKNOWN,
                              f"{self.name} 背包掃不完整，數不準{self.used_name}，"
                              f"驗不出這一發的結果，停手（自己確認一下）")
        self.grace.pop("count", None)
        if now == self.used_before:
            if self.resend >= MAX_RESEND:
                return self._stop(UNKNOWN,
                                  f"{self.name} 連送 {MAX_RESEND} 次都沒反應，停手")
            self.resend += 1
            self.sent_at = 0.0
            return [Event(UNKNOWN,
                          f"{self.name} 沒送出去，第 {self.resend} 次補送")]
        return self._stop(UNKNOWN,
                          f"{self.name} {self.used_name}扣掉了但看不出結果，"
                          f"停手（自己確認一下）")
