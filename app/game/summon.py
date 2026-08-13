"""自動召喚：F11 的召喚物不見了／死了就自動重放（掛機用）。

跟 `buff.py`（自動分身）同一個掛法，但多了「召喚物還在不在」的偵測。

「還在不在」怎麼看（2026-08-13 第二輪實測大翻案）
------------------------------------------------
⚠⚠ **召喚物的 eid 不穩定**：伺服器會不時整隻重建（施放後前 10 秒連換三次、
  走遠了瞬移跟上主人時也換），每次都是新物件＋新 eid。所以「記 eid 用
  read_live 盯」會一直誤判成「不見了」——使用者實際回報的症狀就是
  「走太快就重召，其實牠還在」。
✅ 正解＝讀遊戲自己記的「我的召喚物」：**[[quickbar.MGR_PTR]+PET_SLOT_OFF]**
  （就在角色屬性基準 +0xCB88 前面，同一個角色管理結構）。實測語意：
    · 在視野內：等於實體清單裡那隻的 eid
    · 走遠（不在視野、正在重建）：**保持非零**，換 eid 後 ~1 秒內跟上
    · 跳地圖：歸零 2~4 秒 → 召喚物**自己跟過來** → 恢復非零（新 eid）
    · 重召喚：~1 秒內變成新 eid；連環重建期間**從不歸零**
  → 存在偵測＝「槽 != 0」；歸零**持續 LOST_GRACE 秒**才算真的沒了。
⚠ PET_SLOT_OFF 是**結構偏移**（大更新改版面才會壞的那類），所以要
  **執行時自我驗證**：施放並認養到新實體後，槽值曾經等於牠的 eid 才敢信
  （slot_ok 閂）；驗不過就退回舊的實體追蹤法（會誤判但不會不動作）。

召喚物長什麼樣（2026-08-13 黑狐「召喚噬魂怪Ⅰ」實測，見 memory summon-creature）
--------------------------------------------------------------------------
* 召喚出來的怪就是一個一般實體（VT_ENTITY），名字＝技能名去掉「召喚」
  （「召喚噬魂怪Ⅰ」→「噬魂怪Ⅰ」），OFF_KIND = 4。
  ⚠ kind=4 **不是召喚物專屬**：擺攤中的玩家也是 4（天使學園實拍 22 隻全是
    玩家名），所以認召喚物不能只看 kind。
* **物件裡沒有主人欄位**（+0x4E8 是交戰槽殘留，不可信）；
  「這隻是我的」＝我施放後出生在封包那一格的那隻（認養用）。
* 位址、eid 都會變 —— 別拿任何一個當長期身分，長期身分只有上面那個槽。
* **活著時再放一次＝直接換一隻新的**（舊的消失、新 eid 出現）——
  所以「不確定在不在就重放」是安全的，最壞只是多耗一次 MP。
* ⚠ 死掉會不會變 'Dead' 沒實機看過（測試沒讓牠死成）；死亡偵測靠槽歸零。

怎麼放
------
召喚是**對地技能**（對象＝地面）：走 `attack.cast_at`（施放封包帶格子座標），
座標填自己腳下 —— 使用者指定「位置填射程範圍內即可」。
不是對地的話退回 `quickbar.use`（按 F11，自我系技能按了就生效）。

技能編號跟分身一樣**直接讀快捷欄 F11 那一格**（呼叫端 adopt），
F11 空的／放物品就 block() 大聲停手，不盲按。
"""
from __future__ import annotations

import struct
import time

from app.game import attack, entity, quickbar, skills

SLOT = 10                 # F11（固定，使用者指定）
PAGE = 0
CHECK_GAP = 0.5           # 多久驗一次存在（讀槽 1 次系統呼叫）
MISS_GRACE = 2            # （退路模式）連續幾次讀不到才算不見
ADOPT_WAIT = 4.0          # 施放後等新實體出現多久（實測 1.5 秒內就會出現）
RETRY = 6.0               # 施放了卻沒看到新召喚物（MP 不足？）→ 隔這麼久再試
# 「我的召喚物 eid」在角色管理結構裡的偏移（[MGR_PTR]+這個）。
# ⚠ 結構偏移（大更新才會壞的類別）；壞掉靠 slot_ok 自我驗證退回實體追蹤。
#   出處：2026-08-13 全記憶體交叉掃描（兩個不同 eid 都出現在這一格）＋
#   跳圖歸零／走遠不歸零／重召喚跟上，三種事件實測（見檔頭）。
#   角色屬性基準在 [MGR_PTR]+0xCB88（player.py 那條捷徑），這格在它前面 0x6C。
PET_SLOT_OFF = 0xCB1C
# 槽歸零要**連續**這麼久才算真的沒了：跳地圖時會暫時歸零 2~4 秒
# （召喚物幾秒後自己跟過來），馬上重召等於把跟過來的那隻白白換掉。
LOST_GRACE = 8.0
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


def read_pet_slot(scanner) -> int | None:
    """遊戲記的「我的召喚物 eid」；讀不到（沒進遊戲／位移）回 None。

    0 ＝ 現在沒有召喚物（跳圖的暫態也是 0，呼叫端要配 LOST_GRACE 用）。
    ⚠ 回 None 跟回 0 是兩回事：None 是「不知道」，不能當「沒有」
      （[[bag-false-empty-guards]] 那條鐵則）。
    """
    raw = scanner._read_bytes(quickbar.MGR_PTR, 4)
    if not raw:
        return None
    mgr = struct.unpack("<I", bytes(raw))[0]
    if not 0x10000 <= mgr < 0x7FFF0000:
        return None
    raw = scanner._read_bytes(mgr + PET_SLOT_OFF, 4)
    if not raw:
        return None
    return struct.unpack("<I", bytes(raw))[0]


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
        # 「我的召喚物」槽驗證過了沒 —— 驗過才敢拿它當存在依據。
        # ⚠ 故意**不放進 reset()**：偏移對不對是這一版遊戲的性質，
        #   跟勾選框開開關關無關，驗過一次就一直有效。
        self._slot_ok = False
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
        self._zero_since: float | None = None    # 槽從什麼時候開始一直是 0
        self._slot_at_cast = 0                   # 施放那一刻的槽值（認養備援用）

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
        slot_v = read_pet_slot(scanner)

        # 槽的執行時驗證：槽值對得上**畫面上任何一隻同名召喚物的 eid**
        # 就算驗證通過（那正是我們要依賴的語意）。
        # ★ 不等認養：第一次施放時熱區掃描可能晚幾秒才看到新實體，
        #   認養窗撲空 → 沒驗證 → 沒有備援 → 白白多放一次（實機踩到）。
        #   場上有舊召喚物（上一輪的、手動放的）時這裡當場就驗完了。
        if not self._slot_ok and slot_v:
            for e in (pets or []):
                if e.eid == slot_v and (self.expect is None
                                        or e.name == self.expect):
                    self._slot_ok = True
                    break

        # ① 有追蹤中的召喚物 → 定期驗它還在不在
        if self._tracked is not None:
            if now - self._check_t < CHECK_GAP:
                return self.note
            self._check_t = now
            # 驗證的另一半：曾經等於我們認養那隻的 eid 也算數。
            # （施放後槽會晚實體清單 ~1 秒才跟上，所以是「曾經」不是「當下」。）
            if not self._slot_ok and slot_v and slot_v == self._tracked.eid:
                self._slot_ok = True
            if self._slot_ok:
                # ★ 正路：existence ＝ 槽 != 0。eid 重建（走遠瞬移跟上、
                #   伺服器重發）槽只會換值不會歸零 —— 這正是舊法誤判成
                #   「不見了」的情況，這裡完全不會。
                if slot_v:
                    self._zero_since = None
                    return self.note
                if slot_v == 0:
                    if self._zero_since is None:
                        self._zero_since = now      # 開始計時（跳圖暫態也是 0）
                    elif now - self._zero_since >= LOST_GRACE:
                        self._tracked = None
                        self._zero_since = None
                        self._sent_at = 0.0         # 確定沒了 → 馬上重放
                        self.note = "召喚物沒了 → 重新召喚"
                    return self.note
                return self.note                    # None＝這拍讀不到，不動作
            # 退路（槽還沒驗證過／偏移壞掉）：舊的實體追蹤。
            # ⚠ eid 重建時這條路會誤判成不見 → 多放一次（浪費 MP 但不會壞事）。
            alive, st, _pos = entity.read_live(scanner, self._tracked)
            if alive and st != "Dead":
                self._miss = 0
                return self.note
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
                self._zero_since = None
                self.note = f"已召喚：{got.name}"
            elif (self._slot_ok and slot_v
                    and slot_v != self._slot_at_cast):
                # 認養備援：實體沒對上（出生點被佔挪走之類），但槽已經換成
                # 新 eid ＝ 召喚成功。造個占位追蹤物，之後全靠槽看存在。
                self._tracked = entity.Entity(0, slot_v, 0,
                                              self.expect or "召喚物")
                self._miss = 0
                self._check_t = now
                self._zero_since = None
                self.note = f"已召喚：{self.expect or '召喚物'}（用槽認的）"
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
        # 施放那一刻的槽值：認養備援要比「槽有沒有換新」（重放＝換一隻，
        # 槽會從舊 eid 變新 eid；施放失敗就不會變）
        self._slot_at_cast = slot_v or 0
        self._zero_since = None
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
