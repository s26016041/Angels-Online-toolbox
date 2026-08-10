"""技能的 MP／SP 消耗，與角色**目前**的 SP —— 全部讀遊戲自己載進記憶體的資料。

為什麼需要它
------------
掛機的「首次攻擊」要求「這一隻的第一下一定要是指定的那招」，那招沒放出去
就不放別招。可是**有些技能吃的是 SP（能量燈）不是 MP**：SP 是打怪打出來的，
站著等它自己長只會等到天荒地老 —— 所以 SP 不夠時要**跳過**，不要等
（使用者指定）。要分辨「在冷卻（等得到）」還是「SP 不夠（等不到）」，
就得知道這一招要幾點 SP、身上又還有幾點。

資料哪來（2026-08-10 反組譯 angel.dat）
---------------------------------------
① 技能範本表 —— 跟怪物範本表一模一樣的純指標陣列，**直接讀，不必呼叫函式**::

        範本 = [ [TABLE_PTR] + 技能ID * 4 ]          （技能 ID 1 ~ 0x61A8）

   出處：`0x505DAD` 的查表本體 ``lea ecx,[esi-1] / cmp ecx,0x61A7 / ja 錯誤 /
   mov eax,[0x98BCB0] / mov eax,[eax+esi*4]``。

② 範本的欄位偏移 —— 從 magic.xml 的解析器（`0x56D000` 那一大段
   ``push "欄位名" ... mov [esi+偏移], eax``）逐欄抄出來的，不是猜的::

        +0x50 shoot_range（射程）   +0x58 cost_mp（消耗MP）
        +0x54 range（範圍）         +0x5C cost_sp（消耗SP燈）

③ 角色目前的 MP／SP —— 客戶端自己的「放得出來嗎」檢查 `0x549E7D`：
   `this` = **玩家實體 + 0x210**（內嵌子物件），比的是::

        消耗MP != 0 → 目前MP([this+0x84]) >= 消耗MP      （否則訊息 0x1E9）
        否則         → 目前SP([this+0x88]) >= 消耗SP燈   （否則訊息 0x1EA）

   ⚠ 是**二選一**：只要 cost_mp 不是 0，客戶端就完全不看 SP。

驗證方式（讀取端自己驗，不驗過就回 None）
-----------------------------------------
* 範本指標要落在合理範圍，而且 **`+0x50`／`+0x54` 要有一欄對得上
  `skills.range_of()`**（資源包抽出來的射程表）—— 兩份不同來源的資料交叉
  比對，對不上就當版面變了。
* 目前 MP／SP 那個子物件的基準**當場用 MP 對帳**：實體物件有兩種基準
  （差 8 bytes，見 [[entity-coordinates]]），拿錯不會失敗、只會安靜讀錯，
  所以三個候選基準都試，**只認 `+0x294` 剛好等於角色屬性那份 MP 的那一個**。

2026-08-10 五台實測（純讀）
---------------------------
基準一律是 −8；SP 讀到 2000／4000（都是 1000 的倍數，跟 `消耗SP燈` 同單位）；
技能範本的射程與資源包表逐一對上；真的吃 SP 的技能也抓到了 ——
幻影刺殺Ⅳ(743)=(0 MP, 1000 SP)、冰凍狙擊Ⅲ(932)=(0, 1000)、
爆彈狙擊Ⅱ(920)=(0, 2000)。

驗不過一律回 None（＝「不知道」），呼叫端要有安全退路 —— 絕不拿垃圾值去
決定要不要等一招技能。純讀取，不寫入、不呼叫遊戲的函式。
"""
from __future__ import annotations

import struct

from app.game import skills

# ⚠ 遊戲改版會位移；locate.py 有 AOB 特徵（錨在查表本體的 0x61A7／0x61A9
#   邊界值與錯誤訊息編號）會自動跟上。
TABLE_PTR = 0x0098BCB0
MAX_SKILL_ID = 0x61A8         # 查表本體的邊界：(id-1) <= 0x61A7

# --- 技能範本的欄位（magic.xml 解析器抄來的，見上面）---
OFF_SHOOT_RANGE = 0x50
OFF_RANGE = 0x54
OFF_COST_MP = 0x58
OFF_COST_SP = 0x5C
TMPL_SPAN = OFF_COST_SP + 4

# --- 玩家實體裡的「數值」子物件（0x549E7D 的 this = 實體 + 0x210）---
OFF_ATTR = 0x210
OFF_ATTR_MP = 0x84
OFF_ATTR_SP = 0x88

# 合理性上限（驗不過就當讀錯）。消耗SP燈 實測只有 0/1000~5000 五種，
# MP 最貴的也只有幾百；上限開寬一點，只要擋掉明顯的垃圾值就好。
MAX_COST_MP = 1_000_000
MAX_COST_SP = 100_000
# 實體物件的兩種基準差 8 bytes；哪一種**當場用 MP 對帳**才算數。
# ★ 2026-08-10 五台實測全部是 −8（entity.py 的玩家物件 − 8），所以先試它；
#   但不寫死 —— 對不上帳就換下一個，全都對不上就回 None。
BASE_CANDIDATES = (-8, 0, 8)


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<I", bytes(raw[:4]))[0]


def _sane_ptr(v: int | None) -> bool:
    return bool(v) and 0x10000 <= v < 0x7FFF0000


def template(scanner, skill_id: int) -> int | None:
    """技能範本的位址；讀不到／編號超出表的範圍回 None。"""
    sid = int(skill_id or 0)
    if not 1 <= sid <= MAX_SKILL_ID:
        return None
    tab = _u32(scanner, TABLE_PTR)
    if not _sane_ptr(tab):
        return None
    tmpl = _u32(scanner, tab + sid * 4)
    return tmpl if _sane_ptr(tmpl) else None


def cost(scanner, skill_id: int) -> tuple[int, int] | None:
    """這一招的 (消耗MP, 消耗SP燈)；**驗不過回 None**（＝不知道）。

    ⚠ 回 None 有三種可能：還沒進遊戲、改版把表搬走了、版面變了。
      呼叫端一律當「不知道」處理，不要拿它當 0。
    """
    tmpl = template(scanner, skill_id)
    if tmpl is None:
        return None
    raw = scanner._read_bytes(tmpl + OFF_SHOOT_RANGE,
                              TMPL_SPAN - OFF_SHOOT_RANGE)
    if not raw or len(raw) < TMPL_SPAN - OFF_SHOOT_RANGE:
        return None
    b = bytes(raw)
    shoot = struct.unpack_from("<i", b, 0)[0]                   # +0x50 射程
    area = struct.unpack_from("<i", b, OFF_RANGE - OFF_SHOOT_RANGE)[0]  # +0x54 範圍
    mp = struct.unpack_from("<i", b, OFF_COST_MP - OFF_SHOOT_RANGE)[0]
    sp = struct.unpack_from("<i", b, OFF_COST_SP - OFF_SHOOT_RANGE)[0]
    # 交叉比對：範本的射程要跟資源包抽出來的射程表對得上（查不到就跳過這道）。
    # ⚠ 射程表在「對象＝自己」的範圍技能上收的是 `範圍`（+0x54）不是 `射程`
    #   （+0x50 那時是 0）—— 震地一擊Ⅲ、絕對零度Ⅳ 實測就是這樣。所以兩欄
    #   **對上任何一欄就算過**，否則那些技能會被誤判成「讀不到」。
    want = skills.range_of(skill_id)
    if want is not None and want not in (shoot, area):
        return None
    if not (0 <= mp <= MAX_COST_MP and 0 <= sp <= MAX_COST_SP):
        return None
    return mp, sp


def sp_now(scanner, entity_addr: int, mp_now: int) -> int | None:
    """角色**目前**的 SP；對不上帳就回 None（絕不回猜的值）。

    entity_addr: 玩家實體（entity.py 那個基準；差 8 的那種也吃得下）
    mp_now:      角色屬性那份「目前 MP」—— 拿它當對帳的鑰匙，所以
                 **必須 > 0**（0 太容易巧合命中，寧可回 None）。
    """
    if not (_sane_ptr(entity_addr) and mp_now and mp_now > 0):
        return None
    for delta in BASE_CANDIDATES:
        base = entity_addr + delta + OFF_ATTR
        raw = scanner._read_bytes(base + OFF_ATTR_MP, 8)
        if not raw or len(raw) < 8:
            continue
        mp, sp = struct.unpack("<ii", bytes(raw[:8]))
        if mp == mp_now and 0 <= sp <= MAX_COST_SP:
            return sp
    return None


def sp_enough(scanner, entity_addr: int, mp_now: int,
              skill_id: int) -> bool | None:
    """這一招的 SP 夠不夠？True 夠／False 不夠／**None 不知道**。

    ★ 不吃 SP 的技能（消耗SP燈 = 0）一律回 True —— 對它們來說「等冷卻」
      永遠等得到，這支不該擋。
    """
    got = cost(scanner, skill_id)
    if got is None:
        return None
    _mp_cost, sp_cost = got
    if sp_cost <= 0:
        return True
    have = sp_now(scanner, entity_addr, mp_now)
    if have is None:
        return None
    return have >= sp_cost
