"""自動召喚：F11 的召喚物不見了／死了就自動重放（掛機用）。

跟 `buff.py`（自動分身）同一個掛法，但多了「召喚物還在不在」的偵測 ——
分身看不到剩餘時間所以按時間補；召喚物在**實體清單**裡看得到，照著看就好。

召喚物長什麼樣（2026-08-13 黑狐「召喚噬魂怪Ⅰ」實測，見 memory summon-creature）
--------------------------------------------------------------------------
* 召喚出來的怪就是一個一般實體（VT_ENTITY），名字＝技能名去掉「召喚」
  （「召喚噬魂怪Ⅰ」→「噬魂怪Ⅰ」），OFF_KIND = 4。
  ⚠ kind=4 **不是召喚物專屬**：擺攤中的玩家也是 4（天使學園實拍 22 隻全是
    玩家名），所以認召喚物不能只看 kind。
* **物件裡沒有主人欄位**：整個物件 0x1000 掃不到玩家 eid／玩家物件指標
  （+0x4E8 曾出現過玩家指標，但那是交戰槽殘留，第二隻就沒有了——
  跟 [[foe-field-unreliable]] 同一回事）。angel.dat 也沒有任何全域指著它。
  → 「這隻是**我的**」唯一可靠的認法：**我施放之後那一刻新冒出來的那隻**，
    記住它的 eid 一路追蹤。
* 換地圖召喚物直接消失、物件被回收；回原圖也不會回來。
* 位址會被重複使用（第二隻跟第一隻同一個位址、不同 eid）——
  所以追蹤一律 `entity.read_live()`（vtable＋eid 一起驗），跟怪一樣。
* **活著時再放一次＝直接換一隻新的**（舊的消失、新 eid 出現）——
  所以「不確定在不在就重放」是安全的，最壞只是多耗一次 MP。
* 死掉跟怪一樣：動畫狀態變 'Dead'。

怎麼放
------
召喚是**對地技能**（對象＝地面）：走 `attack.cast_at`（施放封包帶格子座標），
座標填自己腳下 —— 使用者指定「位置填射程範圍內即可」。
不是對地的話退回 `quickbar.use`（按 F11，自我系技能按了就生效）。

技能編號跟分身一樣**直接讀快捷欄 F11 那一格**（呼叫端 adopt），
F11 空的／放物品就 block() 大聲停手，不盲按。
"""
from __future__ import annotations

import time

from app.game import attack, entity, quickbar, skills

SLOT = 10                 # F11（固定，使用者指定）
PAGE = 0
CHECK_GAP = 0.5           # 追蹤中的召喚物多久驗一次死活（一次 1 個系統呼叫）
MISS_GRACE = 2            # 連續幾次「讀不到」才算不見 —— 防瞬間讀失敗誤判
ADOPT_WAIT = 4.0          # 施放後等新實體出現多久（實測 1.5 秒內就會出現）
RETRY = 6.0               # 施放了卻沒看到新召喚物（MP 不足？）→ 隔這麼久再試
# 認養的兩道錨（使用者 2026-08-13 提醒：**同職業的玩家也會召喚**——
# 同名＋新 eid 一模一樣，能分的只有出生點）：
# ① **正中施放格**優先：施放封包的座標是我們自己填的，而兩輪實測召喚物
#   都**正好出生在那一格**（誤差 0.0）。認「站在我封包打的那一格」的，
#   旁邊的人同拍施放也不會撞——他的出生在**他**填的那格。
# ② 沒有正中的才退回「離施放格 NEAR 格內取最近」。這道退路不能拿掉：
#   「出生點會不會因為那格被佔而挪到隔壁」沒驗證過，賭死正中的話，
#   萬一會挪就變成**永遠認不到 → 每 6 秒白放一次**的無限循環，
#   比偶爾認錯（會自己好：對方召喚物跟主人走遠 → read_live 失敗 → 重放）
#   嚴重得多。
NEAR = 2.0
PREFIX = "召喚"           # 技能名的字首；去掉就是召喚物的名字


class AutoSummon:
    """一個角色一份。

    `arm()`  —— 開自動戰鬥或中途勾選時呼叫。
    `step()` —— 每一拍呼叫（自己控節奏，沒事就什麼都不做）。
    `skill`  —— F11 的技能編號（呼叫端從快捷欄讀來 adopt）。
    """

    def __init__(self, skill_id: int | None = None) -> None:
        self.skill = skill_id or None
        self.expect: str | None = None       # 預期的召喚物名字；None = 不知道
        self.page = PAGE                     # 讀到技能的那一頁（按鍵退路要用）
        self.reset()

    def reset(self) -> None:
        self.note = ""
        self.armed = False
        self.blocked = ""
        self._tracked: entity.Entity | None = None
        self._miss = 0
        self._sent_at = 0.0
        self._pre: set[int] = set()          # 施放那一刻已存在的 kind=4 eid
        self._cast_pos: tuple[float, float] | None = None
        self._cast_tile: tuple[int, int] | None = None   # 封包裡填的那一格
        self._check_t = 0.0

    def adopt(self, skill_id: int, page: int = PAGE) -> bool:
        """指定 F11 的技能編號（呼叫端從快捷欄讀到的）。

        跟分身不同：**不要求**在 buff 主表裡（召喚技能沒有持續時間資料，
        skills.of(781) 實測就是 None）。名字查得到就順便記「預期的召喚物
        名字」；查不到就靠「施放點附近新冒出來的 kind=4」認養。
        """
        if not skill_id:
            return False
        self.page = page
        if self.skill != skill_id:
            self.skill = skill_id
            name = skills.name_of(skill_id)
            self.expect = (name[len(PREFIX):]
                           if name.startswith(PREFIX) and len(name) > len(PREFIX)
                           else None)
            # 技能換了 → 舊的追蹤作廢，下一拍重新召喚
            self._tracked = None
            self._sent_at = 0.0
        self.blocked = ""
        return True

    def block(self, why: str) -> None:
        """F11 上沒有可用的技能 → 停手（跟 buff.block 同一個道理）。"""
        self.blocked = why

    def arm(self) -> None:
        """開始（或中途勾選）。⚠ 跟分身一樣**不管現在有沒有**，下一拍就放：
        就算使用者手動召喚過，我們也認不出那隻是誰的 —— 重放會直接換一隻
        新的（實測），順便把它變成我們認得的。"""
        self._tracked = None
        self._miss = 0
        self._sent_at = 0.0
        self.armed = True

    @property
    def alive(self) -> bool:
        """目前有沒有一隻**確認還活著**的召喚物（給 UI／測試看）。"""
        return self._tracked is not None

    def step(self, scanner, mover, player_obj: int | None,
             pets: list) -> str:
        """走一步。pets：最新掃描裡 kind=4 的實體（farm_tab 的 Scan.pets）。

        回傳給狀態列看的說明（沒變化就是舊值，呼叫端自己去重）。
        """
        if not self.armed:
            return self.note
        if not self.skill:
            return self.blocked or self.note
        now = time.monotonic()

        # ① 有追蹤中的召喚物 → 定期驗它還在不在（vtable＋eid＋沒死）
        if self._tracked is not None:
            if now - self._check_t < CHECK_GAP:
                return self.note
            self._check_t = now
            alive, st, _pos = entity.read_live(scanner, self._tracked)
            if alive and st != "Dead":
                self._miss = 0
                return self.note
            # 'Dead' 是確定死了；讀不到再給 MISS_GRACE 次機會（瞬間讀失敗）
            self._miss += 1
            if st == "Dead" or self._miss >= MISS_GRACE:
                self._tracked = None
                self._sent_at = 0.0          # 確定沒了 → 馬上重放，不等 RETRY
                self.note = ("召喚物死了 → 重新召喚"
                             if st == "Dead" else "召喚物不見了 → 重新召喚")
            return self.note

        # ② 剛施放完 → 在時間窗內等新實體出現，出現就認養
        if self._sent_at and now - self._sent_at < ADOPT_WAIT:
            got = self._adopt_new(pets)
            if got is not None:
                self._tracked = got
                self._miss = 0
                self._check_t = now
                self.note = f"已召喚：{got.name}"
            return self.note

        # ③ 施放過卻一直沒出現（MP 不足／被打斷）→ 照 RETRY 節奏無限重試
        #   （transient-failure-auto-retry：出口是使用者把勾拿掉）
        if self._sent_at and now - self._sent_at < RETRY:
            return self.note

        # ④ 施放
        if not (mover is not None and mover.active):
            self.note = "⚠ 跳板沒裝上，召喚不了"
            self._sent_at = now              # 也照 RETRY 節奏，別每拍刷
            return self.note
        pos = entity.player_pos(scanner, player_obj) if player_obj else None
        if not pos:
            self.note = "⚠ 讀不到自己的位置，先不召喚"
            self._sent_at = now
            return self.note
        # 施放那一刻場上已有的 kind=4 —— 之後**不在這份名單裡的**才是我的
        self._pre = {e.eid for e in (pets or [])}
        if skills.is_ground(self.skill):
            # 對地：施放封包帶自己腳下的格子座標（實測就長出在**那一格正中**）
            tx, ty = int(pos[0]), int(pos[1])
            self._cast_tile = (tx, ty)
            self._cast_pos = (tx + 0.5, ty + 0.5)    # 預期的出生點＝格子中心
            ok = attack.cast_at(mover, self.skill, 0, tx, ty)
        else:
            # 不是對地 → 按 F11（自我系技能按了就直接生效）。
            # 這條路封包裡沒有我們填的座標，退回「腳下附近」認養。
            self._cast_tile = None
            self._cast_pos = pos
            ok = quickbar.use(mover, scanner, SLOT, self.page)
        self._sent_at = now
        self.note = "召喚中…" if ok else "⚠ 召喚的封包排不進去"
        return self.note

    def _adopt_new(self, pets: list) -> entity.Entity | None:
        """從最新掃描認養「我剛召喚出來的那隻」。

        條件：kind=4、施放那一刻不存在（eid 是新的）、離施放點 NEAR 格內；
        知道名字就再多一道名字比對。
        ★ **站在封包裡我們自己填的那一格的優先**（使用者點的做法）——
          實測召喚物正好出生在施放格，同職業在旁邊同拍施放也分得開；
          沒有正中的才退回「NEAR 格內取最近」（理由見 NEAR 的說明）。
        """
        if not self._cast_pos:
            return None
        px, py = self._cast_pos
        cand = [e for e in (pets or [])
                if e.eid not in self._pre
                and (self.expect is None or e.name == self.expect)
                and (e.x - px) ** 2 + (e.y - py) ** 2 <= NEAR * NEAR]
        if not cand:
            return None
        if self._cast_tile is not None:
            exact = [e for e in cand
                     if (int(e.x), int(e.y)) == self._cast_tile]
            if exact:
                cand = exact
        return min(cand, key=lambda e: (e.x - px) ** 2 + (e.y - py) ** 2)
