"""玩家物件：一次定位、之後所有屬性用固定偏移直接讀齊。

★ 定位完全靠結構特徵，不看任何數值 ★
--------------------------------------
玩家資料塊是一個 C++ 物件的一部分，物件開頭放的是 **vtable 指標** —— 那是這個類別
的身分標記，只有這個類別的實例才會有。它跟角色的等級、金幣、HP 一點關係都沒有。

以前的版本在找到錨點之後，還拿「數值範圍」再驗證一次（等級 >= 10、金幣 <= 十億、
本級起始經驗 >= 十萬…）。那些範圍是照五個 74～89 級的角色訂的，結果：
  * 低等角色 —— 升級門檻不到十萬 → 被判定「不是角色資料」→ 什麼都不顯示
  * 有錢角色 —— 金幣破十億 → 同樣被誤殺
  * 死亡角色 —— HP 歸零 → 同樣被誤殺
而且失敗後會退回「靠數值形狀猜」的備援，那條路會抓到剛好符合範圍的垃圾資料，
**顯示出別人的數字**。使用者實際遇到：低等角色顯示了不屬於他的數值。

所以現在：**數值只是數值，不參與任何判定。** 定位與「位址還有效嗎」都只看結構。
那條靠數值猜的備援也整個移除了 —— 顯示錯的數字比不顯示更糟。

結構特徵（跨 5 個分身逐欄位比對出來的）
---------------------------------------
    物件 +0x00              vtable 指標 = angel.dat + VTABLE_RVA
    物件 +0x04 .. +0x1C     7 個 dword 全 0
    物件 +0x20 起           角色資料（本檔的各個 OFF_*）
實測每個分身**剛好命中 1 筆**，掃描約 0.3 秒。

欄位版面怎麼來的
----------------
先用共位掃描（tools/find_player_struct.py）把畫面上看得到的數值一起餵進去，要求
它們落在同一塊記憶體內；再拿 4 個沒參與推導的分身驗證，數值與畫面完全吻合。

純讀記憶體，不寫入、不注入、不掛除錯器。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

# 欄位偏移（相對「角色資料基準」，全部是 int32）
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

# ★ 最近使用的技能 ID（**在基準之前** 0x50）。使用者找到的欄位。
#   按下技能鍵（F1~F12）放技能時，這裡會被寫成那個技能的 ID。
#   所以「送一次 F2 再讀這裡」就知道 F2 上是哪個技能 —— 不必攔封包。
#   驗證：黑狐送 F2 前是 0x103、送出後變成 0x101，正是封包量到的 F2 技能；
#   雪狐讀到 0x2E7，也與封包一致。五台的偏移都一樣。
#   ⚠ 它記的是「最近用過的」不是「F2 上的」—— 要在送出 F2 之後立刻讀。
OFF_LAST_SKILL = -0x50

# --- 結構特徵 ---------------------------------------------------------------
GAME_MODULE = "angel.dat"
# vtable 指標的值 = 模組基底 + 這個偏移。用 RVA 記錄、執行時才加基底，所以不怕
# 模組載到別的位址（這遊戲其實無 ASLR，固定 0x400000，但這樣寫比較保險）。
VTABLE_RVA = 0x3E3E0C
# 物件起點相對「角色資料基準」的位置
OFF_VTABLE = -0x20
# vtable 之後有幾個 dword 是 0（五台一致）。加這條讓特徵更不容易誤中。
ZERO_DWORDS = 7


@dataclass(frozen=True)
class PlayerStats:
    """一次讀齊的角色屬性。這裡的數值不做任何範圍檢查 —— 遊戲給什麼就是什麼。"""

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
        """本級進度 0～100。分母異常時回 0，不影響其他欄位顯示。"""
        span = self.exp_hi - self.exp_lo
        if span <= 0:
            return 0.0
        return max(0.0, min(100.0, (self.exp - self.exp_lo) / span * 100.0))


def _signature_ok(scanner, obj: int, vtable: int) -> bool:
    """物件開頭是不是我們要的類別：vtable + 連續 7 個 0。純結構，不看數值。"""
    raw = scanner._read_bytes(obj, 4 + ZERO_DWORDS * 4)
    if not raw or len(raw) < 4 + ZERO_DWORDS * 4:
        return False
    vals = struct.unpack(f"<{1 + ZERO_DWORDS}I", raw)
    return vals[0] == vtable and all(v == 0 for v in vals[1:])


def _vtable_value(scanner) -> int | None:
    base = scanner.module_base(GAME_MODULE)
    return (base + VTABLE_RVA) if base else None


def vtable_value(scanner) -> int | None:
    """這個類別的 vtable 指標值。給「已經要掃記憶體的呼叫端」把它一起掃走用。"""
    return _vtable_value(scanner)


def pick(scanner, hits) -> int | None:
    """從別人掃到的 vtable 命中位址裡挑出玩家物件，回傳「角色資料基準」。

    給已經在掃記憶體的呼叫端用（例如自動掛機的合併掃描），免得為了同一個物件
    再掃一遍 700MB。判定跟 locate() 完全一樣：只看結構特徵，不看數值。
    掃到 0 個或多個都回傳 None —— 寧可不顯示，也不要拿錯的物件。
    """
    vtable = _vtable_value(scanner)
    if vtable is None:
        return None
    ok = [a for a in hits if _signature_ok(scanner, a, vtable)]
    return (ok[0] - OFF_VTABLE) if len(ok) == 1 else None


def locate(scanner, should_stop=None) -> int | None:
    """找玩家物件，回傳「角色資料基準」位址；找不到回傳 None。

    找不到通常不是壞事 —— 還在登入 / 選角畫面時本來就沒有這個物件，等進場再掃即可。
    真的一直找不到（例如遊戲改版讓 VTABLE_RVA 位移），寧可什麼都不顯示，也不要
    退回「靠數值猜」而顯示錯的資料。

    should_stop: 可選的 callable，每個記憶體區塊掃之前呼叫一次，回傳 True 就放棄。
    """
    vtable = _vtable_value(scanner)
    if vtable is None:
        return None
    want = np.uint32(vtable)
    for base, size in scanner._iter_regions(writable_only=True):
        if should_stop is not None and should_stop():
            return None
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        arr = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")
        for i in np.flatnonzero(arr == want):
            obj = base + int(i) * 4
            if _signature_ok(scanner, obj, vtable):
                return obj - OFF_VTABLE
    return None


def read_last_skill(scanner, base: int) -> int | None:
    """最近放出的技能 ID；讀不到或還沒放過技能回傳 None。

    用法：送一次技能鍵之後馬上讀，就知道那個鍵上是哪個技能。
    （攻擊封包 0x559FF8 的第一個參數就是這個值，兩邊實測一致。）
    """
    if not base:
        return None
    raw = scanner._read_bytes(base + OFF_LAST_SKILL, 4)
    if not raw:
        return None
    sid = struct.unpack("<I", raw)[0]
    return sid if 0 < sid < 0x10000 else None


def read(scanner, base: int) -> PlayerStats | None:
    """從基準位址讀齊所有屬性；位址已失效時回傳 None。

    「還有效嗎」只看結構（物件開頭的 vtable 特徵還在不在），**不看數值**。
    換地圖 / 重連 / 重登會讓物件搬家，那時特徵就對不上了，呼叫端重新 locate() 即可。
    """
    if not base:
        return None
    vtable = _vtable_value(scanner)
    if vtable is None or not _signature_ok(scanner, base + OFF_VTABLE, vtable):
        return None
    raw = scanner._read_bytes(base, STRUCT_BYTES)
    if not raw or len(raw) < STRUCT_BYTES:
        return None
    vals = [struct.unpack_from("<i", raw, o)[0] for o in
            (OFF_LEVEL, OFF_HP, OFF_MAX_HP, OFF_MP, OFF_MAX_MP,
             OFF_EXP_LO, OFF_EXP_HI, OFF_EXP, OFF_GOLD)]
    level, hp, max_hp, mp, max_mp, lo, hi, exp, gold = vals
    return PlayerStats(level=level, hp=hp, max_hp=max_hp, mp=mp, max_mp=max_mp,
                       exp=exp, exp_lo=lo, exp_hi=hi, gold=gold)
