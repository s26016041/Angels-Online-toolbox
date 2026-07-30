"""物品的固定知識：技能經驗球的種類 ID → 名稱 / 容量上限。

為什麼上限要寫死
----------------
容量上限**不在記憶體裡**。找過了：球結構前後 ±0x4000 內完全沒有上限值（四個分身
全部 0 筆），種類 ID 與上限也沒有共同記錄。遊戲只有在玩家打開該道具的提示時，才會
臨時組出「最大累積值 35000」「儲存經驗：26194 / 35000」這些文字；沒開過提示的分身
連一筆都搜不到。不可能要求每個帳號都先去開一次背包，所以這張對照表寫死在這裡。

寫死的是遊戲常數，不是玩家狀態
------------------------------
寫死的只有「4936 這種球的上限是 35000」這件事——它跟角色、帳號、當下裝備都無關。
**每顆球現在是哪一種，是每輪從記憶體即時讀的**（球值位址 + TYPE_OFFSET），所以：
  * 玩家隨時換球 → 下一輪就變。
  * 兩個飾品欄位插不同階級的球 → 各自判斷（白狐實測就是這種情況）。
沒有任何「記住這個帳號用哪種球」的快取。

種類 ID 怎麼找出來的（差異分析，別重做）
----------------------------------------
黑狐兩顆都是三階、嵐狐與雪狐都是二階。先取每台「同角色兩顆球一致」的偏移，再求
「嵐狐 == 雪狐 且 != 黑狐」的交集 —— 球值前後 ±0x400 內只剩 **-0x98** 一個候選，
值分別是 4936 / 4937。後續在五個分身上驗證，過濾結果與實際持有的球完全吻合。

★ 維護：這是全專案唯一寫死的遊戲資料，它會安靜地失效 ★
--------------------------------------------------------
其他東西都是每次執行重新掃出來的，只有這張表不是。遊戲改版新增球種或調整上限時
**不會有任何錯誤訊息**，只會：
  * 新球種的 ID 不在表裡 → is_ball() 回 False → 那顆球在畫面上**整個消失**（不是
    顯示錯，是不見了，非常難察覺）。
  * 上限被改動 → 百分比默默算錯。
所以每次遊戲改版後都該複驗一次這張表。

更新時三個地方要一起改，只改一處會留下互相矛盾的資訊：
  1. 本檔的 TIER_BALLS / SHOP_BALLS（SKILL_EXP_BALLS 由它們自動生成，別手改）
  2. 專案記憶 items-table-maintenance
  3. 專案記憶 exp-ball-aob-signature 裡的種類 ID 對照表

怎麼查新資料：
  * 種類 ID —— 在有裝該球的分身上掃 AOB，讀「球值位址 + TYPE_OFFSET」的 int32。
  * 上限 —— 在遊戲裡打開該道具的提示，再搜 UTF-8 字串 `儲存經驗：`（會拿到
    「儲存經驗：26194 / 35000」）或 `最大累積值`。沒開過提示的分身搜不到。
  * 交叉驗證 —— 記憶體有 `<ID> 1 <欄位> 30 <數量> <售價>` 的商店記錄，可確認 ID
    對應哪個名稱（實測 4935→x1 售價 10、4936→x1 售價 25，對上遊戲商店列表）。
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, replace

import numpy as np

from app.config import config

# 物品種類 ID 的合理上限（實測看過的最大值是 87xxx 這種等級）
MAX_ITEM_ID = 200_000

# 種類 ID 相對「球值位址」的偏移（球值位址 = AOB 命中 + 0x80，見 app/game/aob.py）
TYPE_OFFSET = -0x98


@dataclass(frozen=True)
class BallType:
    """一種經驗球（裝在飾品欄、隨著打怪或用技能累積經驗的道具）。

    name       遊戲內名稱
    cap        最大累積值
    bonus      「額外再獲得 N% 經驗值儲存入經驗球內」
    family     技能 / 角色 / 寵物 —— 三個系列都裝飾品欄，只是累積的經驗種類不同
    min_level  裝備需求等級；0 = 不明
    type_id    記憶體裡的物品種類 ID；None = 還沒在任何分身上看過，認不出來
    """

    name: str
    cap: int = 0
    bonus: int = 0
    family: str = "技能"
    min_level: int = 0
    type_id: int | None = None

    @property
    def known_cap(self) -> bool:
        return self.cap > 0

    def pct(self, value: int) -> float | None:
        """目前累積的百分比；上限未知時回傳 None。"""
        if self.cap <= 0:
            return None
        return min(100.0, value / self.cap * 100.0)


# ---------------------------------------------------------------------------
# 完整目錄：名稱 / 加成 / 上限全部來自**遊戲自己的物品說明表**（原文如
# 「裝備至飾品欄位後，使用技能將可額外再獲得 35% 技能經驗值儲存入經驗球內，
#   最大累積值 35,000。」），用 scratchpad/ball_catalog_raw.py 逐條抄下來的。
# 可信度佐證：其中二階 35,000 與三階 120,000 與實機讀到的值完全吻合。
#
# 三個系列都裝在飾品欄，差別在累積哪一種經驗：
#   技能經驗球 —— 用技能時累積技能經驗
#   角色經驗球 —— 打怪時累積角色經驗（右鍵使用給予經驗後球會消失）
#   寵物經驗球 —— 打怪時累積，給召喚中的寵物
# （「經驗寶石 / 鑽石 / 彩鑽」那些是直接用在寵物身上的道具，不進飾品欄，不收錄。）
#
# type_id 只有實際在分身上看過的才填得出來，目前只有技能系列的一/二/三階。
# ⚠ 沒有 ID 就認不出來：玩家若裝了沒 ID 的球，is_ball() 會回 False、畫面顯示
#   「非經驗球」。要補很簡單 —— 在有裝那種球的分身上讀「球值位址 + TYPE_OFFSET」，
#   把數字填進對應那一行即可，其餘欄位都已經查好了。
#
# ID 怎麼確認的：
#   4936 / 4937 由差異分析直接鎖定（見本檔開頭），並在五個分身上驗證過。
#   4935 由商店記錄反推 —— 記憶體裡有 `<ID> 1 <欄位> 30 <數量> <售價>` 這種樣式，
#   實測 4935→(x1,10)(x2,20)、4936→(x1,25)(x2,50)，正好對上遊戲商店列出的
#   「一階技能經驗球 x1 10 / x2 20」「二階技能經驗球 x1 25 / x2 50」。
#   5008 由遊戲聊天訊息確認（見下方 SKILL_BALLS 的註解）。
#
# ⏳ 待確認的推測（**證據不足，刻意不填進表**）：
#   記憶體有一張清單 `5007 5008 5009 | 5162 5163 5164 | 5347 5348 5349`，三組各三個
#   連號；已確認 5008＝二階迷你技能經驗球，所以第一組很可能是「技能」的迷你三階。
#   另外在記憶體裡各找到「名稱指標 +4 = ID」的**單一**命中：
#       5007→一階迷你技能經驗球、5162→一階迷你角色經驗球、5347→一階迷你寵物經驗球
#   但每個都只出現一次，而且 5162 還有另一個候選 79768 同樣只出現一次。
#   ⚠ 填錯比留空更糟 —— 留空只會顯示「非經驗球」，填錯會顯示一個很有自信的錯誤
#   百分比，而且若猜的上限偏大，下面那道「值超過上限」的保險也永遠不會觸發。
#   要用哪一種，就實際買一顆、看聊天訊息「獲得 XXX」來確認再填。
# ---------------------------------------------------------------------------

# --- 技能經驗球 ---
SKILL_BALLS = (
    BallType("一階迷你技能經驗球", cap=1_600,    bonus=10),
    # 5008 由遊戲聊天訊息「獲得 二階迷你技能經驗球（數量 2）」確認 —— 使用者買兩顆，
    # 物品欄同時出現兩筆 ID 5008。這是目前最可靠的 ID↔名稱來源。
    BallType("二階迷你技能經驗球", cap=10_000,   bonus=10, type_id=5008),
    BallType("三階迷你技能經驗球", cap=30_000,   bonus=10),
    BallType("一階技能經驗球",     cap=5_000,    bonus=30, type_id=4935),
    BallType("二階技能經驗球",     cap=35_000,   bonus=35, min_level=60, type_id=4936),
    BallType("三階技能經驗球",     cap=120_000,  bonus=40, min_level=80, type_id=4937),
    BallType("新手技能經驗球",     cap=50_000,   bonus=30),
    BallType("普通技能經驗球",     cap=100_000,  bonus=30),
    BallType("優質技能經驗球",     cap=150_000,  bonus=35),
    BallType("精選技能經驗球",     cap=300_000,  bonus=35),
)

# --- 角色經驗球（打怪累積角色經驗）---
CHAR_BALLS = (
    BallType("一階迷你角色經驗球", cap=100_000,      bonus=10, family="角色"),
    BallType("二階迷你角色經驗球", cap=1_000_000,    bonus=10, family="角色"),
    BallType("三階迷你角色經驗球", cap=5_000_000,    bonus=10, family="角色"),
    BallType("四階迷你角色經驗球", cap=10_000_000,   bonus=10, family="角色"),
    BallType("一階角色經驗球",     cap=400_000,      bonus=30, family="角色"),
    BallType("二階角色經驗球",     cap=4_000_000,    bonus=30, family="角色"),
    BallType("三階角色經驗球",     cap=20_000_000,   bonus=30, family="角色"),
    BallType("四階角色經驗球",     cap=40_000_000,   bonus=30, family="角色"),
    BallType("新手經驗球",         cap=100_000,      bonus=25, family="角色"),
    BallType("普通經驗球",         cap=18_000_000,   bonus=20, family="角色"),
    BallType("優質經驗球",         cap=300_000_000,  bonus=15, family="角色"),
    BallType("精選經驗球",         cap=500_000_000,  bonus=5,  family="角色"),
    BallType("測試-經驗球",        cap=400_000,      bonus=35, family="角色"),  # 測試道具
)

# --- 寵物經驗球（打怪累積，給召喚中的寵物）---
PET_BALLS = (
    BallType("一階迷你寵物經驗球", cap=50_000,     bonus=10, family="寵物"),
    BallType("二階迷你寵物經驗球", cap=500_000,    bonus=10, family="寵物"),
    BallType("三階迷你寵物經驗球", cap=2_000_000,  bonus=10, family="寵物"),
    BallType("一階寵物經驗球",     cap=200_000,    bonus=30, family="寵物"),
    BallType("二階寵物經驗球",     cap=2_000_000,  bonus=30, family="寵物"),
    BallType("三階寵物經驗球",     cap=8_000_000,  bonus=30, family="寵物"),
)

ALL_BALLS = SKILL_BALLS + CHAR_BALLS + PET_BALLS

# 實際查表用的：只有已知 ID 的才進得來
SKILL_EXP_BALLS: dict[int, BallType] = {
    b.type_id: b for b in ALL_BALLS if b.type_id is not None
}

# AOB 特徵不只命中球，也會命中一堆別的物品（實測看過 3516～3531、4870、4942～4963、
# 81379、87303 …）。所以拿到候選位址後一定要用種類 ID 過濾，別靠數值大小猜。


# ---------------------------------------------------------------------------
# 執行時從遊戲讀目錄：讓上面寫死的上限在遊戲改版後自動跟上
#
# 遊戲的物品說明表**一直都在記憶體裡**（不必開商城或道具提示 —— 在完全沒點過的
# 分身上照樣搜得到「最大累積值」64 筆）。所以名稱／加成／上限可以現場解析，
# 寫死的那份只當離線後備。
#
# ⚠ 名稱在記憶體裡是**分開存的**：有的是完整一條「新手技能經驗球」，有的是
#   前綴 +基底名兩條（「三階」｜「技能經驗球」）。一開始沒注意這點就配對錯了。
# ---------------------------------------------------------------------------
_BASE_NAMES = ("技能經驗球", "角色經驗球", "寵物經驗球")
_CAP_RE = re.compile(r"額外再獲得\s*(\d+)\s*%.*?最大累積值\s*([\d,]+)", re.S)
_PREFIX_RE = re.compile(r"^[一二三四五六七八九十]階(迷你)?$")
_CAP_MARK = "最大累積值".encode("utf-8")

# 名稱 → (加成, 上限)，由 refresh_from_game() 填。空的就代表還沒解析過。
_game_caps: dict[str, tuple[int, int]] = {}


def refresh_from_game(scanner) -> int:
    """解析遊戲自己的物品說明表，取得 名稱 →（加成, 上限）。回傳解析到幾種。

    要走一遍所有可讀區塊（約一秒），所以每個程序只需要在開場做一次。
    解析失敗不影響功能 —— 會退回本檔寫死的那份。
    """
    got: dict[str, tuple[int, int]] = {}
    for base, size in scanner._iter_regions(writable_only=False):
        raw = scanner._read_region(base, size)
        if not raw or _CAP_MARK not in raw:
            continue
        parts = raw.split(b"\x00")
        for i, p in enumerate(parts):
            if _CAP_MARK not in p:
                continue
            m = _CAP_RE.search(p.decode("utf-8", errors="ignore"))
            if not m:
                continue
            bonus, cap = int(m.group(1)), int(m.group(2).replace(",", ""))
            if cap <= 0:
                continue
            name = _name_before(parts, i)
            if name:
                got[name] = (bonus, cap)
    if got:
        _game_caps.clear()
        _game_caps.update(got)
    return len(got)


def _name_before(parts: list[bytes], i: int) -> str | None:
    """說明字串前面找名稱：可能是完整一條，也可能是「前綴」+「基底名」兩條。"""
    for back in (1, 2, 3):
        if i - back < 0:
            return None
        cand = parts[i - back].decode("utf-8", errors="ignore").strip()
        if not cand:
            continue
        if cand in _BASE_NAMES:
            prefix = ""
            if i - back - 1 >= 0:
                prefix = parts[i - back - 1].decode(
                    "utf-8", errors="ignore").strip()
            return (prefix + cand) if _PREFIX_RE.match(prefix) else cand
        if cand.endswith("經驗球") and len(cand) <= 16:
            return cand
        return None
    return None


# ---------------------------------------------------------------------------
# 學到的種類 ID：存進設定檔，學一次就永久記得
#
# 對照表以外的球認不出來，是因為「種類 ID → 名稱」的對照只在客戶端資料檔裡、
# 不在記憶體。但玩家把滑鼠移到球上時，遊戲會把「儲存經驗：X / Y」組成文字，
# 那時就能反查出上限（見 probe_ball）。把結果存下來，之後不管重開幾次都認得。
# ---------------------------------------------------------------------------
_CONFIG_KEY = "items.learned_balls"
_learned: dict[int, BallType] = {}


def load_learned() -> int:
    """從設定檔載入學過的球。回傳載入幾種。"""
    _learned.clear()
    raw = config.get(_CONFIG_KEY, {}) or {}
    for k, v in raw.items():
        try:
            tid = int(k)
            _learned[tid] = BallType(
                name=str(v.get("name") or f"經驗球#{tid}"),
                cap=int(v.get("cap") or 0),
                bonus=int(v.get("bonus") or 0),
                family=str(v.get("family") or "技能"),
                type_id=tid,
            )
        except (TypeError, ValueError, AttributeError):
            continue
    return len(_learned)


def remember(type_id: int, ball: BallType) -> None:
    """把新學到的種類 ID 記進設定檔（同一個 ID 重複學不會重寫）。"""
    if type_id is None or type_id in _learned or type_id in SKILL_EXP_BALLS:
        return
    _learned[type_id] = replace(ball, type_id=type_id)
    raw = dict(config.get(_CONFIG_KEY, {}) or {})
    raw[str(type_id)] = {"name": ball.name, "cap": ball.cap,
                         "bonus": ball.bonus, "family": ball.family}
    config.set(_CONFIG_KEY, raw)
    config.save()


load_learned()


def missing_ids() -> list[str]:
    """目錄裡還沒有種類 ID（因此認不出來）的球名。全部學會後回傳空清單。"""
    have = {b.name for b in ALL_BALLS if b.type_id is not None}
    have |= {b.name for b in _learned.values()}
    return [b.name for b in ALL_BALLS if b.name not in have]


# --- 從商店列表學 ID（不必擁有那顆球）--------------------------------------
# 商店文字：「名稱 …… x數量　售價」
# 商店記錄：`<種類ID> 1 <欄位> 30 <數量> <售價>`（int32 陣列）
# 兩邊都有「數量 + 售價」，拿它當接合鍵就能把名稱對到 ID。
# 已用三個已知 ID 驗證：4935→(x1,10)、4936→(x1,25)、4937→(x1,45) 全部正確。
_SHOP_LINE = re.compile(r"([一-鿿\-]{2,16}經驗球)\s+x(\d+)\s+(\d+)")
_SHOP_QTY_MAX = 100
_BALL_MARK = "經驗球".encode("utf-8")


def learn_from_shop(scanner) -> int:
    """掃商店列表，把能唯一對上的「名稱 ↔ 種類 ID」學起來。回傳這次學到幾種。

    商店列表只在玩家打開商店／商城時才在記憶體裡，所以這支要定期重試；
    學到的會永久存進設定檔，不必再學第二次。
    """
    # 1) 商店文字 →（數量, 售價）對應到哪些名稱
    groups: dict[tuple[int, int], set[str]] = {}
    for base, size in scanner._iter_regions(writable_only=False):
        raw = scanner._read_region(base, size)
        if not raw or _BALL_MARK not in raw:
            continue
        for m in _SHOP_LINE.finditer(raw.decode("utf-8", errors="ignore")):
            qty, price = int(m.group(2)), int(m.group(3))
            if 0 < qty <= _SHOP_QTY_MAX and price > 0:
                groups.setdefault((qty, price), set()).add(m.group(1))
    if not groups:
        return 0

    # 名稱在記憶體裡是「前綴」+「基底名」分開存的，正則有時只抓到後半段
    # （「階技能經驗球」）。同一組裡若某個名稱是另一個的字尾，丟掉短的那個。
    for key, names in list(groups.items()):
        groups[key] = {n for n in names
                       if not any(o != n and o.endswith(n) for o in names)}

    known = {b.name: b for b in ALL_BALLS}
    learned = 0
    for base, size in scanner._iter_regions(writable_only=True):
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        a = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<i4")
        n = a.size - 5
        if n <= 0:
            continue
        ok = ((a[1:n + 1] == 1) & (a[3:n + 3] == 30)
              & (a[:n] > 1000) & (a[:n] < MAX_ITEM_ID)
              & (a[4:n + 4] > 0) & (a[4:n + 4] <= _SHOP_QTY_MAX)
              & (a[5:n + 5] > 0))
        for i in np.flatnonzero(ok):
            i = int(i)
            tid = int(a[i])
            if tid in SKILL_EXP_BALLS or tid in _learned:
                continue
            names = groups.get((int(a[i + 4]), int(a[i + 5])))
            if not names or len(names) != 1:
                continue        # 對不到唯一一種就跳過，不亂猜
            bt = known.get(next(iter(names)))
            if bt is None:
                continue        # 目錄裡沒這種球（拿不到上限）→ 不學
            remember(tid, bt)
            learned += 1
    return learned


def ball_type(type_id: int | None) -> BallType | None:
    """種類 ID → 球的定義；不是（已知或學過的）球就回傳 None。

    上限優先採用「執行時從遊戲讀到的值」，寫死的那份只當後備 —— 這樣遊戲調整
    容量之後不必改程式碼。
    """
    if type_id is None:
        return None
    bt = SKILL_EXP_BALLS.get(type_id) or _learned.get(type_id)
    if bt is None:
        return None
    live = _game_caps.get(bt.name)
    if live and (live[0], live[1]) != (bt.bonus, bt.cap):
        bt = replace(bt, bonus=live[0], cap=live[1])
    return bt


def is_ball(type_id: int | None) -> bool:
    return ball_type(type_id) is not None


def read_type_id(scanner, ball_addr: int) -> int | None:
    """讀某顆球的種類 ID（純讀，4 bytes）。讀不到回傳 None。"""
    raw = scanner._read_bytes(ball_addr + TYPE_OFFSET, 4)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack("<i", raw)[0]


# ---------------------------------------------------------------------------
# 對照表以外的東西：試著問遊戲自己
# ---------------------------------------------------------------------------
UNKNOWN_LABEL = "非經驗球"

# 遊戲在玩家打開道具提示時，會把這行組成文字放進記憶體：「儲存經驗：26194 / 35000」
_TIP_RE = re.compile(r"儲存經驗：(\d+)\s*/\s*(\d+)")
# 名稱一定以「經驗球」結尾（一階／二階／三階／新手／普通／優質／精選…）
_NAME_RE = re.compile(r"[一-鿿]{1,8}經驗球")
_TIP_MARK = "儲存經驗".encode("utf-8")   # 先用位元組粗篩，免得每個區塊都去 decode
_NAME_SPAN = 2000       # 名稱與提示是分開配置的字串，得往前後找遠一點


def probe_ball(scanner, value: int) -> BallType | None:
    """對照表裡沒有的種類 ID，就地問遊戲：這顆球叫什麼、上限多少。

    原理：遊戲會把「儲存經驗：X / Y」這行提示組成文字留在記憶體裡，名稱字串通常就在
    附近。用「X 最接近目前值」挑出對應的那一筆 —— 提示是玩家開道具的當下組的，球之後
    還會繼續漲，所以不能要求完全相等。

    ⚠ 只有玩家在遊戲裡開過該道具的提示／飾品欄，這些字串才會存在；沒開過就回 None，
    呼叫端會把它標成 UNKNOWN_LABEL。找到的結果值得補進上面的對照表，變成永久知識。
    """
    best: tuple[int, str | None, int] | None = None
    for base, size in scanner._iter_regions(writable_only=True):
        raw = scanner._read_region(base, size)
        if not raw or _TIP_MARK not in raw:
            continue
        txt = raw.decode("utf-8", errors="ignore")
        for m in _TIP_RE.finditer(txt):
            got, cap = int(m.group(1)), int(m.group(2))
            if cap <= 0:
                continue
            diff = abs(got - value)
            if best is not None and diff >= best[0]:
                continue
            seg = txt[max(0, m.start() - _NAME_SPAN): m.end() + _NAME_SPAN]
            names = _NAME_RE.findall(seg)
            best = (diff, names[0] if names else None, cap)

    if best is None:
        return None
    diff, name, cap = best
    # 差太多代表這行提示講的是別顆球 —— 寧可回 None 也不要標錯
    if diff > max(2000, cap * 0.2):
        return None
    if not name:
        # 提示字串附近沒抓到名字時，用上限回頭查目錄：上限唯一對應某一種球就採用。
        # （目錄是執行時從遊戲讀的，見 refresh_from_game）
        hit = [n for n, (_b, c) in _game_caps.items() if c == cap]
        if len(hit) != 1:
            hit = [b.name for b in ALL_BALLS if b.cap == cap]
        if len(hit) != 1:
            return None     # 對不到唯一一種 → 老實標成「非經驗球」，不亂猜
        name = hit[0]
    live = _game_caps.get(name)
    fam = next((b.family for b in ALL_BALLS if b.name == name), "技能")
    return BallType(name, cap=cap, bonus=live[0] if live else 0, family=fam)
