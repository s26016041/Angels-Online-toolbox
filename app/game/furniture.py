"""傢俱魔力錘：對著傢俱一直敲，魔力值（基礎＋加成）到目標就停，**每一錘都驗結果**。

    run = furniture.Run(scanner, mover, slot, serial, hammer_type=11118, target=300)
    while not run.done:
        for ev in run.tick():
            print(ev.kind, ev.text)

## 魔力值怎麼算（2026-09-23 反組譯遊戲提示框 `0x5F34CE`）

    魔力值 ＝ 基礎 ＋ 加成　　（提示框字串 2537「魔力值：%d(%d+%d)」）
    基礎 ＝ 傢俱表的擺設分數 —— **查表**（assets/furniture.tsv.gz，
           `tools/build_furniture.py` 從 GAMEDATA 抽；有表對表）
    加成 ＝ 物品實體 **+0xA0（u16）** —— `0x5F363F movzx ecx, word [edi+0xA0]`，
           edi ＝ 物品實體（同一段 `[edi+8]` 取種類 ID，跟 bag 的 +0x08 一致）

✅ 對帳：工匠狐格 76 日式榻榻米地板，表 164、+0xA0 = 35（剛被下級祝福錘敲過，
   祝福錘下限正是 35），同款沒敲過的那件 +0xA0 = 0。

## 送的是什麼

跟強化裝備一模一樣 ——「對物品使用物品」`recall.USE_ITEM_FN(錘子格, 傢俱格)`，
封包 0x2E（格號 > 255 走 0x132）。出處：使用者 2026-09-23 擷取（封包/強化家具.txt）
呼叫鏈 `0x589D79 call 0x5DA0C6(0x47, 0x4C)`；當場讀那台：格 71 下級祝福傢俱魔力錘、
格 76 傢俱；`locate.warm()` 定出來的 `USE_ITEM_FN` 就是 0x5DA0C6 ——
同一支，**不另外登記特徵**。

## 結果怎麼判（魔力錘不會把傢俱打掉，但會把好的加成洗掉）

| 讀到 | 判定 |
|---|---|
| 加成變了 | 敲到了 → 記一筆；到目標就停，沒到就再敲 |
| 加成沒變、錘子**少了**（等過 SETTLE_MS） | 敲到了、剛好擲出一樣的數字 → 記一筆 |
| 加成沒變、錘子沒少、等滿 WAIT_MS | 沒送出去 → 補送（上限 MAX_RESEND） |
| 那一格空了／換成別件（認 serial） | 驗不了 → 停 |
| 讀不到（背包／物件／掃描） | 等 UNREADABLE_SECS，還是讀不到就停 |
| 加成超出合理範圍 | 讀到垃圾 → 當讀不到 |

錘子數量只認 `bag.scan` **掃完整**的結果（[[bag-false-empty-guards]]）。
"""
from __future__ import annotations

import gzip
import struct
import time
from dataclasses import dataclass

from app.game import attack, bag, itemname, recall
from app.paths import resource

DATA_FILE = "assets/furniture.tsv.gz"

# 物品實體 +0xA0（u16）＝ 魔力值加成。出處見檔頭（提示框 0x5F363F）。
OFF_BONUS = 0xA0

WAIT_MS = 6000               # 送出後最多等多久看結果
SETTLE_MS = 1500             # 錘子少了但加成沒變：至少等這麼久才認定「擲出一樣的數字」
MAX_RESEND = 3               # 確定沒送出去才補送，而且有上限
UNREADABLE_SECS = 3.0

ROLLED = "rolled"            # 敲到了（不管好壞）
BLOCKED = "blocked"          # 開不了工／沒錘子了
UNKNOWN = "unknown"          # 驗不出來 —— 停手
DONE = "done"

READ_OK = "ok"
READ_UNREADABLE = "unreadable"
READ_ABSENT = "absent"       # 確定讀到，那一格空了或不是傢俱
READ_SWAPPED = "swapped"     # 讀到了，但那一格換成別件（serial 不同）

_furn: dict[int, int] | None = None                  # 種類 → 基礎魔力值
_hammer: dict[int, tuple[int, int]] = {}             # 種類 → (最低, 最高)


def _load() -> dict[int, int]:
    global _furn
    if _furn is None:
        furn: dict[int, int] = {}
        try:
            with gzip.open(resource(DATA_FILE), "rt", encoding="utf-8") as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    if len(p) != 4:
                        continue
                    if p[0] == "F":
                        furn[int(p[1])] = int(p[2])
                    elif p[0] == "H":
                        _hammer[int(p[1])] = (int(p[2]), int(p[3]))
        except Exception:                                  # noqa: BLE001
            furn = {}          # 表缺了＝一件都不列（安全退化），不要爆掉
            _hammer.clear()
        _furn = furn
    return _furn


def base_of(type_id: int) -> int | None:
    """傢俱的基礎魔力值（擺設分數）；不是傢俱／表沒這筆回 None。"""
    return _load().get(int(type_id))


def hammer_range(type_id: int) -> tuple[int, int] | None:
    """魔力錘擲出的加成 (最低, 最高)；不是魔力錘回 None。"""
    _load()
    return _hammer.get(int(type_id))


def bonus_sane() -> int:
    """加成的合理上限 ＝ 表裡所有魔力錘的最高值；讀到比這還大就是垃圾。"""
    _load()
    return max((hi for _lo, hi in _hammer.values()), default=0)


@dataclass(frozen=True)
class Furn:
    item: bag.Item
    base: int
    bonus: int

    @property
    def total(self) -> int:
        return self.base + self.bonus

    @property
    def slot(self) -> int:
        return self.item.slot

    @property
    def serial(self) -> int:
        return self.item.serial

    @property
    def name(self) -> str:
        return self.item.name

    @property
    def icon_id(self) -> int:
        return self.item.icon_id


@dataclass(frozen=True)
class Hammer:
    type_id: int
    slot: int                # 第一堆的格號（送出前一律重找）
    count: int               # 全部堆加總
    lo: int
    hi: int
    icon_id: int

    @property
    def name(self) -> str:
        return itemname.of(self.type_id) or f"物品 {self.type_id}"


def _bonus_at(scanner, ptr: int, serial: int) -> tuple[int | None, str]:
    """讀物品實體的加成；順便認 serial。"""
    if not 0x10000 < ptr < 0x7FFF0000:
        return None, READ_UNREADABLE
    raw = scanner._read_bytes(ptr, OFF_BONUS + 2)
    if not raw or len(raw) < OFF_BONUS + 2:
        return None, READ_UNREADABLE
    b = bytes(raw)
    if struct.unpack_from("<I", b, bag.ITEM_SERIAL)[0] != serial:
        return None, READ_SWAPPED
    val = struct.unpack_from("<H", b, OFF_BONUS)[0]
    if val > bonus_sane():
        return None, READ_UNREADABLE          # 垃圾值不准拿來算
    return val, READ_OK


def in_bag(scanner, items=None, complete=None) -> tuple[list[Furn], bool]:
    """(背包裡查得到表的傢俱, 整段是不是真的都讀到了)。"""
    if items is None:
        items, complete = bag.scan(scanner)
    got = bag.head(scanner)
    if got is None:
        return [], False
    begin, _count = got
    out: list[Furn] = []
    for it in items:
        base = base_of(it.type_id)
        if base is None:
            continue
        raw = scanner._read_bytes(begin + it.slot * 4, 4)
        ptr = struct.unpack_from("<I", bytes(raw), 0)[0] if raw else 0
        val, _st = _bonus_at(scanner, ptr, it.serial)
        if val is None:
            complete = False
            continue
        out.append(Furn(item=it, base=base, bonus=val))
    return out, bool(complete)


def hammers(scanner, items=None, complete=None) -> tuple[list[Hammer], bool]:
    """(背包裡的魔力錘，同種合一筆, 整段是不是真的都讀到了)。"""
    if items is None:
        items, complete = bag.scan(scanner)
    by: dict[int, Hammer] = {}
    for it in items:
        rng = hammer_range(it.type_id)
        if rng is None:
            continue
        h = by.get(it.type_id)
        if h is None:
            by[it.type_id] = Hammer(it.type_id, it.slot, it.count, rng[0], rng[1],
                                    it.icon_id)
        else:
            by[it.type_id] = Hammer(h.type_id, h.slot, h.count + it.count,
                                    h.lo, h.hi, h.icon_id)
    return sorted(by.values(), key=lambda h: h.type_id), bool(complete)


def read_state(scanner, slot: int, serial: int) -> tuple[Furn | None, str]:
    """重讀某一格的傢俱，**而且要是同一件**。回 READ_ABSENT 才代表真的不在。"""
    items, complete = bag.scan(scanner, slot, slot)
    if not items:
        return None, (READ_ABSENT if complete else READ_UNREADABLE)
    it = items[0]
    if it.serial != serial:
        return None, READ_SWAPPED
    base = base_of(it.type_id)
    if base is None:
        return None, READ_ABSENT
    got = bag.head(scanner)
    if got is None:
        return None, READ_UNREADABLE
    raw = scanner._read_bytes(got[0] + slot * 4, 4)
    if not raw:
        return None, READ_UNREADABLE
    val, st = _bonus_at(scanner, struct.unpack_from("<I", bytes(raw), 0)[0], serial)
    if val is None:
        return None, st
    return Furn(item=it, base=base, bonus=val), READ_OK


def strike(mover, hammer_slot: int, furn_slot: int) -> bool:
    """送一錘「對第 furn_slot 格的傢俱使用第 hammer_slot 格的魔力錘」。"""
    if not (mover and mover.active):
        return False
    if not 0 <= hammer_slot < bag.MAX_SLOTS or not 0 <= furn_slot < bag.MAX_SLOTS:
        return False
    return attack._send(mover, ((recall.USE_ITEM_FN, (hammer_slot, furn_slot)),))


@dataclass(frozen=True)
class Event:
    kind: str
    text: str
    total: int = 0


class Run:
    """把一件傢俱敲到「魔力值 ≥ target」的狀態機；由分頁每一拍呼叫 `tick()`。"""

    def __init__(self, scanner, mover, slot: int, serial: int, hammer_type: int,
                 target: int, name: str = "") -> None:
        self.sc = scanner
        self.mover = mover
        self.slot = slot
        self.serial = serial
        self.hammer_type = int(hammer_type)
        self.target = int(target)
        self.name = name
        self.done = False
        self.sent_at = 0.0
        self.before = -1
        self.hammer_before = -1
        self.resend = 0
        self.strikes = 0
        # 寬限期起點一種讀不到記一個（共用一個會互相洗掉 → 無聲卡死，
        # [[frozen-tick-state-machines]]）
        self.grace: dict[str, float] = {}

    def _wait_readable(self, why: str) -> bool:
        now = time.monotonic()
        return (now - self.grace.setdefault(why, now)) < UNREADABLE_SECS

    def _stop(self, kind: str, text: str, total: int = 0) -> list[Event]:
        self.done = True
        return [Event(kind, text, total)]

    def _hammer_now(self) -> tuple[int | None, int, bool]:
        """(這種錘子第一堆的格號, 總數, 掃完整了嗎)。送出前一律重找。"""
        hs, complete = hammers(self.sc)
        for h in hs:
            if h.type_id == self.hammer_type:
                return h.slot, h.count, complete
        return None, 0, complete

    def tick(self) -> list[Event]:
        if self.done:
            return []
        return self._check() if self.sent_at else self._send()

    def _lost(self, st: str, sent: bool) -> list[Event]:
        """讀不到／換人／不在了 —— 共用的停手說法。回空清單＝寬限期內先等。"""
        if st == READ_UNREADABLE:
            if self._wait_readable("furn"):
                return []
            what = "這一錘的結果沒驗到，" if sent else ""
            return self._stop(UNKNOWN, f"{self.name} 連 {UNREADABLE_SECS:.0f} 秒讀不到"
                                       f"背包（換地圖了？），{what}停手")
        if st == READ_SWAPPED:
            return self._stop(UNKNOWN, f"{self.name} 那一格換成別的東西了，停手")
        return self._stop(UNKNOWN, f"{self.name} 不在那一格了，停手")

    def _send(self) -> list[Event]:
        f, st = read_state(self.sc, self.slot, self.serial)
        if f is None:
            return self._lost(st, False)
        self.grace.pop("furn", None)
        if f.total >= self.target:
            return self._stop(DONE, f"{self.name} 魔力值 {f.total}"
                                    f"（{f.base}+{f.bonus}）≥ {self.target}，完成", f.total)
        hslot, hcount, complete = self._hammer_now()
        if hslot is None:
            if not complete and self._wait_readable("hammer"):
                return []
            if not complete:
                return self._stop(UNKNOWN, "背包掃不完整，讀不到錘子，停手", f.total)
            name = itemname.of(self.hammer_type) or "魔力錘"
            return self._stop(BLOCKED, f"{name}用完了（敲了 {self.strikes} 錘，"
                                       f"目前魔力值 {f.total}）", f.total)
        self.grace.pop("hammer", None)
        self.before = f.bonus
        self.hammer_before = hcount if complete else -1
        if not strike(self.mover, hslot, self.slot):
            return self._stop(BLOCKED, "跳板沒裝好，送不出去")
        self.sent_at = time.monotonic()
        return []

    def _rolled(self, f: Furn) -> list[Event]:
        ms = (time.monotonic() - self.sent_at) * 1000
        self.sent_at = 0.0
        self.resend = 0
        self.strikes += 1
        same = "（一樣）" if f.bonus == self.before else ""
        evs = [Event(ROLLED, f"第 {self.strikes} 錘：加成 {self.before} → {f.bonus}{same}"
                             f"，魔力值 {f.total}（{ms:.0f} ms）", f.total)]
        if f.total >= self.target:
            self.done = True
            evs.append(Event(DONE, f"{self.name} 魔力值 {f.total}（{f.base}+{f.bonus}）"
                                   f"≥ {self.target}，停", f.total))
            return evs
        # ★ 結果驗完就當場送下一錘，不多等一拍（使用者 2026-09-23 嫌慢）。
        #   _send() 照樣重讀那一格、認 serial、重找錘子 —— 驗證一步都沒少。
        return evs + self._send()

    def _check(self) -> list[Event]:
        f, st = read_state(self.sc, self.slot, self.serial)
        if f is None:
            return self._lost(st, True)
        self.grace.pop("furn", None)
        if f.bonus != self.before:
            return self._rolled(f)
        elapsed = (time.monotonic() - self.sent_at) * 1000
        if elapsed < SETTLE_MS:
            return []
        _hslot, now, complete = self._hammer_now()
        if complete and self.hammer_before >= 0 and now < self.hammer_before:
            return self._rolled(f)            # 錘子扣了、數字剛好一樣
        if elapsed < WAIT_MS:
            return []
        if not complete or self.hammer_before < 0:
            # ⛔ 沒掃完的數量不准拿來比：少數＝可能只是沒掃到，判成沒送出去就會多花一錘
            if self._wait_readable("hammer"):
                return []
            return self._stop(UNKNOWN, f"{self.name} 背包掃不完整，數不準錘子，"
                                       f"驗不出這一錘的結果，停手", f.total)
        self.grace.pop("hammer", None)
        if now == self.hammer_before:
            if self.resend >= MAX_RESEND:
                return self._stop(UNKNOWN, f"{self.name} 連送 {MAX_RESEND} 次都沒反應，停手",
                                  f.total)
            self.resend += 1
            self.sent_at = 0.0
            return [Event(UNKNOWN, f"{self.name} 沒送出去，第 {self.resend} 次補送")]
        # 錘子變多了（撿到／別人給）—— 看不出這一錘有沒有敲到，停手
        return self._stop(UNKNOWN, f"{self.name} 錘子數量對不上，看不出結果，停手", f.total)
