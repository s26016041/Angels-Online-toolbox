"""玩家物件：一次定位、之後所有屬性用固定偏移直接讀齊。

跟技能經驗球那套 AOB 特徵不同 —— 這裡不靠位元組樣式，而是靠「一整組欄位必須同時
合理」來認出玩家物件。掃一次拿到基準位址，之後等級 / HP / MP / 經驗 / 金幣全部是
一次 read 就到手，成本幾乎為零。

版面怎麼來的
------------
先用共位掃描（tools/find_player_struct.py）把畫面上看得到的數值一起餵進去，要求它們
落在同一塊記憶體內 —— 巧合機率極低，直接命中。再拿 4 個「沒參與推導」的分身用下面
的純結構條件各自驗證，全部找到唯一命中且數值與畫面吻合，才敢寫死這組偏移。

怎麼定位（★ 精準法：認 vtable 指標）
------------------------------------
玩家資料塊其實是一個 C++ 物件的一部分：**物件起點（= 資料基準 -0x20）放的是 vtable
指標**，值在五個分身上完全相同，而且落在 `angel.dat+0x3E3E0C` —— 是模組內的靜態位址
（這遊戲沒有 ASLR）。所以只要掃「哪裡存著這個值」就能認出玩家物件本尊：
實測**每個分身剛好命中 1 筆**，比舊的條件式掃描更快也更確定。

備援法（形狀比對）
------------------
萬一遊戲改版讓 vtable 位移，還留著舊的「一整組欄位必須同時合理」掃描：關鍵是經驗那
三個欄位（本級起 <= 目前 <= 下一級）加上 HP 上限 >= 500。它能用，但**不夠精準** ——
實測踩過一次：一張 `131073 / 262147 / 393221 / 524295` 的等差位元樣式表通過了全部條件，
整張卡顯示垃圾。所以它只當備援，不是主要手段。

純讀記憶體，不寫入、不注入、不掛除錯器。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

# 欄位偏移（相對玩家物件基準位址，全部是 int32）
OFF_LEVEL = 0x00
OFF_HP = 0x04
OFF_MAX_HP = 0x08
OFF_MP = 0x0C
OFF_MAX_MP = 0x10
OFF_EXP_LO = 0x20   # 本級起始經驗
OFF_EXP_HI = 0x28   # 升下一級所需經驗
OFF_EXP = 0x30      # 目前經驗
OFF_GOLD = 0x50

STRUCT_BYTES = OFF_GOLD + 4

# --- vtable 錨點（精準定位用）------------------------------------------------
# 物件起點在「玩家資料基準」的 -0x20，那裡存著這個類別的 vtable 指標。
# 值是 angel.dat 內的靜態位址，用 RVA 記錄，執行時再加上模組當下的基底
# （這遊戲無 ASLR，基底固定 0x400000，但用 RVA 寫比較不怕哪天變了）。
GAME_MODULE = "angel.dat"
VTABLE_RVA = 0x3E3E0C
OFF_VTABLE = -0x20

# 以 int32 為單位的索引，掃描時用
_I = [o // 4 for o in
      (OFF_LEVEL, OFF_HP, OFF_MAX_HP, OFF_MP, OFF_MAX_MP,
       OFF_EXP_LO, OFF_EXP_HI, OFF_EXP, OFF_GOLD)]
_SPAN = OFF_GOLD // 4 + 1

# 合理範圍。放寬會冒出巧合、收緊會漏掉真角色，這組是實測過 5 個分身的平衡點。
MAX_LEVEL = 150
MIN_MAX_HP = 500          # ★ 關鍵判別：巧合命中的 HP 都只有兩三位數
# HP / MP 上限。原本開到 1e6 太鬆 —— 實測抓到一張「131073 / 262147 / 393221 / 524295」
# 的表（0x20001、0x40003…的等差位元樣式）通過了全部條件，害整張卡顯示垃圾資料。
# 實際角色 Lv74～89 的上限只有 2,715～5,647，收到 10 萬仍有兩個數量級的餘裕。
MAX_STAT = 100_000
MIN_EXP_LO = 100_000
MIN_LEVEL_SPAN = 10_000   # 一級的經驗跨距至少這麼多
MAX_GOLD = 1_000_000_000


@dataclass(frozen=True)
class PlayerStats:
    """一次讀齊的角色屬性。"""

    level: int
    hp: int
    max_hp: int
    mp: int
    max_mp: int
    exp: int
    exp_lo: int
    exp_hi: int
    gold: int

    @property
    def exp_pct(self) -> float:
        """本級進度 0～100。"""
        span = self.exp_hi - self.exp_lo
        if span <= 0:
            return 0.0
        return (self.exp - self.exp_lo) / span * 100.0


def _plausible(level, hp, max_hp, mp, max_mp, lo, hi, exp, gold,
               alive_only: bool = True) -> bool:
    """單一位址版的合理性判斷（掃描用的是下面的向量化版本，條件必須一致）。

    alive_only=True（定位時用）要求 HP/MP 都 > 0：定位是在茫茫記憶體裡認人，條件越
    嚴越好，而且角色死著的時候本來就不該拿來當定位樣本。
    alive_only=False（已定位、單純讀值時用）允許 HP/MP = 0 —— **角色死亡時 HP 就是 0**，
    要是這裡也擋掉，read() 會回 None，死亡監控就永遠等不到那一刻。
    """
    floor = 1 if alive_only else 0
    return (
        10 <= level <= MAX_LEVEL
        and floor <= hp <= max_hp <= MAX_STAT and max_hp >= MIN_MAX_HP
        and floor <= mp <= max_mp <= MAX_STAT and max_mp >= MIN_MAX_HP
        and lo >= MIN_EXP_LO and lo <= exp <= hi and hi - lo >= MIN_LEVEL_SPAN
        and 0 <= gold <= MAX_GOLD
    )


def locate(scanner, should_stop=None) -> int | None:
    """找玩家物件，回傳資料基準位址；找不到回傳 None。

    先用 vtable 錨點（精準）；萬一遊戲改版讓它位移，才退回形狀比對。
    找不到通常不是壞事 —— 還在登入 / 選角畫面時本來就沒有這個物件，等進場再掃即可。

    should_stop: 可選的 callable，每個記憶體區塊掃之前呼叫一次，回傳 True 就放棄。
    """
    addr = _locate_by_vtable(scanner, should_stop)
    if addr is not None:
        return addr
    return _locate_by_shape(scanner, should_stop)


def _locate_by_vtable(scanner, should_stop=None) -> int | None:
    """認 vtable 指標：掃「哪裡存著 angel.dat+VTABLE_RVA 這個位址」。

    只有這個類別的實例會在物件開頭放這個值，所以命中數極少（實測每台剛好 1 筆）。
    仍然跑一次 read() 驗證，確定後面接的是合理的角色資料才採用。
    """
    mod = scanner.module_base(GAME_MODULE)
    if not mod:
        return None
    want = np.uint32(mod + VTABLE_RVA)
    for base, size in scanner._iter_regions(writable_only=True):
        if should_stop is not None and should_stop():
            return None
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        arr = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")
        for i in np.flatnonzero(arr == want):
            obj = base + int(i) * 4
            stats_base = obj - OFF_VTABLE
            if read(scanner, stats_base) is not None:
                return stats_base
    return None


def _locate_by_shape(scanner, should_stop=None) -> int | None:
    """備援：靠「一整組欄位同時合理」找。能用，但會有偽陽性，見模組開頭說明。"""
    for base, size in scanner._iter_regions(writable_only=True):
        if should_stop is not None and should_stop():
            return None
        raw = scanner._read_region(base, size)
        if not raw or len(raw) < STRUCT_BYTES:
            continue
        a = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<i4")
        n = a.size - _SPAN
        if n <= 0:
            continue
        lvl, hp, max_hp, mp, max_mp, lo, hi, exp, gold = (
            a[i: i + n] for i in _I)

        ok = (lvl >= 10) & (lvl <= MAX_LEVEL)
        ok &= (hp > 0) & (hp <= max_hp) & (max_hp >= MIN_MAX_HP) & (max_hp <= MAX_STAT)
        ok &= (mp > 0) & (mp <= max_mp) & (max_mp >= MIN_MAX_HP) & (max_mp <= MAX_STAT)
        # 經驗三兄弟的排序關係 —— 這條扛掉絕大多數巧合
        ok &= (lo >= MIN_EXP_LO) & (lo <= exp) & (exp <= hi) & (hi - lo >= MIN_LEVEL_SPAN)
        ok &= (gold >= 0) & (gold <= MAX_GOLD)

        hits = np.flatnonzero(ok)
        if hits.size:
            return base + int(hits[0]) * 4
    return None


def read(scanner, base: int) -> PlayerStats | None:
    """從基準位址讀齊所有屬性。

    位址失效時回傳 None（換地圖 / 重連 / 遊戲關閉都會讓結構搬家，舊位址不是讀不到
    就是變成一堆不合理的數字）→ 呼叫端看到 None 就重新 locate()。
    """
    if not base:
        return None
    raw = scanner._read_bytes(base, STRUCT_BYTES)
    if not raw or len(raw) < STRUCT_BYTES:
        return None
    vals = [struct.unpack_from("<i", raw, o)[0] for o in
            (OFF_LEVEL, OFF_HP, OFF_MAX_HP, OFF_MP, OFF_MAX_MP,
             OFF_EXP_LO, OFF_EXP_HI, OFF_EXP, OFF_GOLD)]
    level, hp, max_hp, mp, max_mp, lo, hi, exp, gold = vals
    # alive_only=False：角色死亡時 HP=0 也要讀得出來（死亡監控要靠這個），
    # 其餘條件（等級範圍、HP 上限、經驗三兄弟的排序）照樣把搬家後的垃圾擋掉。
    if not _plausible(level, hp, max_hp, mp, max_mp, lo, hi, exp, gold,
                      alive_only=False):
        return None
    return PlayerStats(level=level, hp=hp, max_hp=max_hp, mp=mp, max_mp=max_mp,
                       exp=exp, exp_lo=lo, exp_hi=hi, gold=gold)
