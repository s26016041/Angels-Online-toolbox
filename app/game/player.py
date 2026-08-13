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

from app.core.memory import VALUE_TYPES

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
#   ★ **不需要有怪**（實測）：雪狐在全程沒有目標的情況下按鍵，
#     F3 → 0x2E1、F5 → 0x279，與有目標時量到的一致。
#   ⚠ 但**單次按鍵不保證寫得進去**（冷卻／間隔），要按幾下再判定。
#   ⚠ 它記的是「最近用過的」不是「F2 上的」—— 要在送出那個鍵之後才讀。
OFF_LAST_SKILL = -0x50

# --- 結構特徵 ---------------------------------------------------------------
GAME_MODULE = "angel.dat"
# vtable 指標的值 = 模組基底 + 這個偏移。用 RVA 記錄、執行時才加基底，所以不怕
# 模組載到別的位址（這遊戲其實無 ASLR，固定 0x400000，但這樣寫比較保險）。
# ⚠⚠ **遊戲改版會讓它位移**。2026-08-04 12:22 那次：0x3E3E0C → 0x3E3E1C（+0x10）
#   ★ 物件版面完全沒變，只有 vtable 位址移了（等級仍在 vtable+0x20、
#     HP +0x24/+0x28、MP +0x2C/+0x30，跟下面的 OFF_* 完全吻合）。
#   重新定位：拿已知的等級/最大HP/最大MP 去掃舊值附近的候選即可
#   （scratchpad/find_stats.py）。同一次改版 entity.py 的 VT_STATE 也是 +0x10。
VTABLE_RVA = 0x3E3E1C
# 物件起點相對「角色資料基準」的位置
OFF_VTABLE = -0x20
# vtable 之後有幾個 dword 是 0（五台一致）。加這條讓特徵更不容易誤中。
ZERO_DWORDS = 7
# ⚠⚠⚠ 那 7 個「0」裡有一格**不是保留欄位**：vtable+PET_EID_OFF 是
#   「我的召喚物 eid」（2026-08-13 交叉掃描實證，見 [[summon-creature]]）。
#   身上有召喚物時它非零 → 舊版特徵整個驗不過 → **有召喚物的那台
#   等級/HP/金幣全部定位失敗**（收益監控「定位中…」、掛機讀不到 HP）。
#   使用者實機踩到（黑狐帶著噬魂怪，全記憶體 0 個候選）。
#   → _signature_ok 檢查零值時**跳過這一格**；它同時也是 summon.py 讀
#   「召喚物還在不在」的來源（那邊透過 pet_eid() 拿，不另抄位址）。
PET_EID_OFF = 0x14


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
    """物件開頭是不是我們要的類別：vtable + 連續 7 個 0。純結構，不看數值。

    ⚠ **召喚物 eid 那一格（+PET_EID_OFF）不算**：那是活欄位，身上有
      召喚物時非零。把它算進「必須為 0」害過整台定位全滅（見常數說明）。
    """
    raw = scanner._read_bytes(obj, 4 + ZERO_DWORDS * 4)
    if not raw or len(raw) < 4 + ZERO_DWORDS * 4:
        return False
    vals = struct.unpack(f"<{1 + ZERO_DWORDS}I", raw)
    if vals[0] != vtable:
        return False
    skip = PET_EID_OFF // 4                  # vals[0] 是 vtable，欄位從 1 起算
    return all(v == 0 for i, v in enumerate(vals[1:], start=1) if i != skip)


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


# ★★★ 掉血當下「最大 HP」會暫時等於「當前 HP」，要多久才修正回來。
#   ✅ 2026-08-09 實測（五台 10Hz 全程採樣）：北極狐被打掉 89 血的那一刻，
#      `+0x04`（HP）與 `+0x08`（最大 HP）**同時**變成 3149，
#      **1.31 秒後** `+0x08` 才修正回真值 3238：
#        t=26.89  3238/3238
#        t=27.00  3149/3149   ← 這裡開始，hp / max_hp 算出來是 100%
#        t=28.31  3149/3238   ← 修正回來
#   ⚠⚠ 這 1.3 秒**血量百分比會算成 100%**，休息判斷會被騙 —— 而角色連續
#     挨打時每一次掉血都會重新開始這 1.3 秒，等於「血很低卻一直顯示滿血」，
#     那就是「掛機被打死、藥水還剩很多」的死法之一。
#   ⚠ 找過替代欄位：整個物件（−0x80 ~ +0x400）除了 +0x04／+0x08 沒有第三個
#     存最大 HP 的地方，所以只能在讀取端擋。
MAXV_SETTLE = 3.0


class MaxTracker:
    """把「最大值」穩下來，擋掉上面那個暫態。HP 一個、MP 一個。

        mhp = tracker.value(stats.max_hp, stats.level)

    規則（失效方向刻意選在**安全**那一邊）：
      · 變大 → 立刻採用（升級、穿裝備）。
      · 變小 → **先不採用**，除非**同一個較小的值穩定地**撐過 `MAXV_SETTLE` 秒
        （真的脫裝備／buff 到期）。
      · 等級變了 → 整個重來（升級會換一組上限）。

    ⚠⚠ 「**同一個**值」這三個字不能省（離線測試抓到的）：光看「比較小」
      加計時器的話，連續挨打超過 `MAXV_SETTLE` 秒就會採信撕裂值 ——
      實測血從 3238 一路掉到 508，百分比被算成 42% 而不是 16%。
      撕裂的特徵正是**上限跟著血一起往下跑**（每一拍都是新的較小值），
      真的降上限則是**停在一個固定值**。所以值一變就重新計時。
      ★ 停止挨打之後上限 1.3 秒內就會自己修正回真值，不會卡在小值上。

    高估上限的後果是百分比偏低 → 偏向「多休息一下」；低估的後果是
    該休息卻不休息 → 被打死。所以寧可高估。
    """

    def __init__(self) -> None:
        self.seen = 0
        self.level = None
        self._low_val = None      # 正在觀察的那個「較小的值」
        self._low_since = 0.0

    def value(self, cur: int, level=None, now: float | None = None) -> int:
        import time as _t

        now = _t.monotonic() if now is None else now
        if level is not None and level != self.level:
            self.level, self.seen = level, cur
            self._low_val, self._low_since = None, 0.0
            return self.seen
        if cur >= self.seen:
            self.seen = cur
            self._low_val, self._low_since = None, 0.0
        elif cur != self._low_val:
            self._low_val, self._low_since = cur, now   # 換了值 → 重新計時
        elif now - self._low_since >= MAXV_SETTLE:
            self.seen = cur
            self._low_val, self._low_since = None, 0.0
        return self.seen


# ★★★ 角色資料物件掛在**狀態物件**（`[quickbar.MGR_PTR]`）底下的固定位置。
#   ✅ 2026-08-09 五台實測 + 兩次換頻道全程 10Hz 採樣：
#      · 慢層對帳 25/25：`[MGR_PTR] + 這個` == 全掃找到的基準
#      · 快層 2895/2895 拍算得出來，而且 vtable 特徵全部驗得過
#      · 換頻道後拿到角色屬性：**捷徑 1.6 秒 vs 全掃 5.1~13.4 秒**
#   ⚠ 這是結構偏移（同一塊配置內的位置），屬於「大更新改版面才會壞」那一類；
#     壞掉也只是**變慢**，不會讀到錯的東西 —— 因為 `_signature_ok()` 會擋下來，
#     然後自動退回底下那條全掃。
#   ⚠⚠ 2026-08-11 改版真的變了：0xCB68 → 0xCB08（−0x60）。症狀就是上面說的
#     「只是變慢」—— 一聲不吭每拍全掃。所以現在改成**跟著 AOB 自動定位**：
#     locate.SIGS 的 `player.VT_OFF_FROM_MGR` 從狀態物件建構函式那道
#     `mov [edi+偏移], 角色屬性vtable` 直接把偏移讀回來（同一段特徵也解出
#     VTABLE_RVA）。下面這個值只是「還沒 warm() 或定位失敗」時的退路。
VT_OFF_FROM_MGR = 0xCB08          # 角色屬性物件的 vtable 在狀態物件裡的位置


def pet_eid(scanner) -> int | None:
    """遊戲記的「我的召喚物 eid」；0＝現在沒有，None＝讀不到。

    位置＝[MGR] + VT_OFF_FROM_MGR + PET_EID_OFF —— 就在角色屬性物件的
    vtable 後面（那串「保留 0」裡唯一的活欄位）。VT_OFF_FROM_MGR 有 AOB
    自動定位，所以這裡**不寫死絕對偏移**，改版跟著搬。
    語意實測（2026-08-13，見 [[summon-creature]]）：召喚物在視野＝清單那隻
    的 eid；走遠（重建中）保持非零；跳圖歸零 2~4 秒後跟過來恢復；
    真的沒了＝持續 0。
    ⚠ None 是「不知道」不是「沒有」（bag-false-empty-guards 鐵則）。
    """
    from app.game import quickbar                   # 避免模組載入期循環相依

    raw = scanner._read_bytes(quickbar.MGR_PTR, 4)
    if not raw:
        return None
    mgr = struct.unpack("<I", bytes(raw))[0]
    if not 0x10000 < mgr < 0x7FFF0000:
        return None
    raw = scanner._read_bytes(mgr + VT_OFF_FROM_MGR + PET_EID_OFF, 4)
    if not raw:
        return None
    return struct.unpack("<I", bytes(raw))[0]


def locate_fast(scanner) -> int | None:
    """不必全掃的捷徑：狀態物件 + `VT_OFF_FROM_MGR`，**驗過 vtable 才算數**。

    為什麼值得做：`locate()` 的全掃要 0.4~1 秒／台，而換頻道／換地圖之後
    物件會搬家 —— 那幾秒等級／HP／MP 全部讀不到，休息與補給判斷都是瞎的
    （實測換頻道後全掃最久要 13.4 秒才把基準找回來）。
    ⚠ 驗不過一律回 None 讓呼叫端走全掃：**寧可慢，不可錯**。
    """
    from app.game import quickbar                   # 避免模組載入期循環相依

    vtable = _vtable_value(scanner)
    if vtable is None:
        return None
    raw = scanner._read_bytes(quickbar.MGR_PTR, 4)
    if not raw:
        return None
    mgr = struct.unpack("<I", bytes(raw))[0]
    if not 0x10000 < mgr < 0x7FFF0000:
        return None
    # 角色資料基準 = vtable 位置 − OFF_VTABLE（OFF_VTABLE 是負的 −0x20）
    base = mgr + VT_OFF_FROM_MGR - OFF_VTABLE
    return base if _signature_ok(scanner, base + OFF_VTABLE, vtable) else None


def locate(scanner, should_stop=None) -> int | None:
    """找玩家物件，回傳「角色資料基準」位址；找不到回傳 None。

    找不到通常不是壞事 —— 還在登入 / 選角畫面時本來就沒有這個物件，等進場再掃即可。
    真的一直找不到（例如遊戲改版讓 VTABLE_RVA 位移），寧可什麼都不顯示，也不要
    退回「靠數值猜」而顯示錯的資料。

    ★ 先走 `locate_fast()`（兩次讀取），對不上才全掃。
    ⚠ **畫面執行緒不要叫這一支** —— 捷徑失敗時它會全掃（實測 195~231ms），
      畫面就卡那麼久。那種地方請直接叫 `locate_fast()`，回 None 就這一拍算了。

    should_stop: 可選的 callable，每個記憶體區塊掃之前呼叫一次，回傳 True 就放棄。
    """
    got = locate_fast(scanner)
    if got is not None:
        return got
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


def clear_last_skill(scanner, base: int) -> None:
    """把「最近使用的技能」欄位清成 0，之後讀到的非零值就一定是新按出來的。

    ⚠⚠ **學技能 ID 之前一定要先清**。不清的話，按鍵沒生效時會讀到
      **上一次殘留的技能 ID**，看起來像成功、其實拿到的是別的鍵的技能。
      實際踩過：雪狐設定用 F2（技能 0x2E7），卻學成 F3 的 0x2E1，
      於是一直對怪施放不能打的技能 —— 症狀是「完全無法打怪」。
      而單次按鍵**本來就不保證會寫入**（冷卻／間隔；黑狐在沒有目標時
      更是完全不寫），所以殘留值被誤用的機會很高。

    這是純資料欄位，遊戲下次放技能就會自己覆蓋回去。
    實測寫 0 之後遊戲一切正常（HP/MP 不變、不當機）。

    ⚠⚠⚠ **寫之前一定要確認 base 現在還是角色屬性物件**（`base_ok`）。
      這支的 `base` 是 KeyWorker 快取起來的位址，只有掃描回來時才會更新；
      換地圖／重連／重生之後物件早就搬家，而舊位址那塊記憶體**照樣寫得進去**
      —— 那就是在亂改遊戲的堆積，症狀是「掛久了遊戲莫名其妙掛掉」，而且離
      真正的元凶已經很遠（見 memory 的 game-crash-root-causes）。
      CLAUDE.md 的鐵則：交給遊戲的位址，動手前當場重驗。
    """
    if base_ok(scanner, base):
        scanner.write_value(base + OFF_LAST_SKILL, VALUE_TYPES["int32"], 0)


def base_ok(scanner, base: int) -> bool:
    """這個位址**現在**還是角色屬性物件嗎（比對 vtable 特徵）。

    ★ `read()` 本來就會驗；這支是給「只碰 ±0x50 那個欄位、不讀整份屬性」
      的呼叫端用的同一道驗證，成本是一次 4-byte 讀取。
    """
    if not base:
        return False
    vtable = _vtable_value(scanner)
    return vtable is not None and _signature_ok(scanner, base + OFF_VTABLE,
                                                vtable)


def read_last_skill(scanner, base: int) -> int | None:
    """最近放出的技能 ID；讀不到／位址已失效／還沒放過技能回傳 None。

    用法：送一次技能鍵之後馬上讀，就知道那個鍵上是哪個技能。
    （攻擊封包 0x559FF8 的第一個參數就是這個值，兩邊實測一致。）

    ⚠⚠ **一定要先驗身分**。位址過期時舊記憶體照樣讀得到，`0 < sid < 0x10000`
      這種範圍檢查擋不住別人的資料 —— 學到一個錯的技能 ID，之後就一直對怪
      施放打不到的招，症狀正是這個檔案上面警告過的「完全無法打怪」。
    """
    if not base_ok(scanner, base):
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
    # ⚠ 金幣／經驗要用**無號**讀（bag.gold() 也是 <I）：破 2^31 會顯示成負數。
    #   HP/MP/等級維持有號 —— 死亡偵測要能讀到 0，且數值不會超出範圍。
    vals = [struct.unpack_from(fmt, raw, o)[0] for fmt, o in
            (("<i", OFF_LEVEL), ("<i", OFF_HP), ("<i", OFF_MAX_HP),
             ("<i", OFF_MP), ("<i", OFF_MAX_MP),
             ("<I", OFF_EXP_LO), ("<I", OFF_EXP_HI), ("<I", OFF_EXP),
             ("<I", OFF_GOLD))]
    level, hp, max_hp, mp, max_mp, lo, hi, exp, gold = vals
    return PlayerStats(level=level, hp=hp, max_hp=max_hp, mp=mp, max_mp=max_mp,
                       exp=exp, exp_lo=lo, exp_hi=hi, gold=gold)
