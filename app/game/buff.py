"""自動分身：身上**剩不到 LEAD 秒或根本沒有**就補放（2026-08-22 起）。

怎麼放
------
★★★ 走**客戶端自己的快捷鍵函式**（`quickbar.use` ＝ usequickkey，跟真按
  F12 同一支），快捷欄整個讀不到（改版位移）才退回真送鍵。

## 2026-08-20 嵐狐實機三路對照（reports/lanfox_*_probe.txt）

技能 5471 雙體分身Ⅰ 的 magic.xml：**對象＝自己、消耗MP＝51、持續 300 秒、
魔法狀態＝分身攻擊加成、後置時間 2000**。⚠ 它是**自我 buff，不生成實體** ——
「實體清單裡看不到分身」不是失敗的證據（before/after 差分：新出現 0 個）。

三條路**都成功施放**，測到的東西一模一樣：
    A 裸包 attack.CAST_FN　B 真按 F12（send_key）　C quickbar.use
    → 每條都 MP −51（＝magic.xml 的消耗，2 秒後回滿）
    → 每條都收到伺服器廣播 0x0301＋0x0401（帶 300000ms＝300 秒時長）
⚠⚠ **MP 一定要 0.25 秒取樣**：我第一次只看 6 秒後的數字（已回滿），
  誤判成「封包不扣魔」，差點把好的那條路砍掉 —— 這正是檔頭早就寫過的坑。

為什麼還是選 C（快捷鍵路徑）：它跟使用者親手按 F12 是同一支函式，
**客戶端側的效果（施法動作、快捷欄 CD、畫面上的分身）也會一起發生**；
裸包只有伺服器端生效、客戶端什麼都不演（[[skill-data-and-buff]] 實測），
而使用者的抱怨正是「看不到分身」。代價：後置時間 2 秒的硬直（5 分鐘一次）。
⚠ 記憶體層面兩條等價 —— 若日後證明畫面效果與此無關，換回裸包也不算錯。
## ★★★ 使用者實機回報「無法分身、一直重複卡補發」的真根因（2026-08-20 修）

2026-08-19 起補放後等 castwatch 的「施放廣播」：看到＝確認受理。
⚠⚠ 但舊版把**等不到廣播**判成「被拒收」→ 每 RETRY(8) 秒重放一次、
  `_cast_at` 永遠不記 → **無限補發迴圈**，狀態列一直刷「補發」。
  嵐狐實測證明廣播本來就會回（閒置時 0.4 秒內收到），所以真實情境裡
  fired() 會失敗的原因不是拒收，而是**沒接到那一包**：環形緩衝只有 512 槽，
  掛機打怪時入向封包洪流幾秒就繞一圈，那一包早被蓋掉。
✅ 修法：等不到＝「**驗不了**」→ 當作已補（回到 8/19 以前一直正常的行為）
  ＋這招之後跳過確認（`_cw_skip`），**絕不重放**。真的收到廣播時照樣
  回報「伺服器已受理」。沒有監聽（裝不起來）一樣退回「按出就當成功」。

技能編號怎麼來
--------------
★★★ **直接讀快捷欄**（`quickbar.read_page`，2026-08-06 找到的表）——
  零副作用、當場拿到，呼叫端用 `adopt()` 塞進來。
⚠⚠ 舊的「按一下 F12 再讀角色屬性 −0x50」是**保底**，只有快捷欄整個讀不到
  （改版位移）才會走。2026-08-10 實機抓到它的代價：白狐的 F12 是**空的**，
  於是每 `RETRY`(8) 秒按一次、**永遠學不到、無限重試**；而 `win.send_key`
  中間有 40ms 的 `sleep`，又是在 GUI 執行緒上呼叫的 —— 等於每 8 秒把整個
  程式（五台分頁共用一條 GUI 執行緒）卡一下，狀態列也被錯誤訊息一直蓋掉。
  → 現在那一格沒有技能時用 `block()` **大聲停手**，不再盲按。
⛔ 更早的註記「快捷欄的技能欄位找不到（試過五種方法）」已經過時 ——
  那是因為一格 9 bytes、技能 ID 落在未對齊位址（見 [[quickbar-table]]）。

計時
----
★★★ **2026-08-22 起看身上真正的剩餘時間**（使用者指定：`剩 < LEAD` 或
`身上沒有` 才放）。來源＝身上生效中的 buff 樹（`app/game/buffs.py`，實體
`+0x420`，節點 `+0x18` 是到期 Unix 秒）—— 那是官方精靈「自動輔助」用來判斷
「這招還在不在」的同一份資料。

⚠ 讀不到（沒進場／改版位移／驗證不過）就**退回舊做法**：自己記時間、
  持續時間查 `skills.py`（單位秒，黑狐的 5424 是 1200 秒），開始時無腦放一次。
  絕不把「讀不到」當成「身上沒有」——那會每輪白放一次、白扣魔。

⛔ 舊註記「不偵測身上有沒有（使用者要求）／buff 剩餘時間找不到（試過 11 種
  方法）」已作廢：那 11 種都在找「每秒遞減的欄位」，而遊戲存的是**固定不動的
  到期戳記**，本來就不會遞減（見 buffs.py 檔頭）。
"""
from __future__ import annotations

import time

from app.game import bag, buffs, castwatch, player, quickbar, skills

# 剩幾秒就補（使用者 2026-08-22 指定 20 秒：**剩 < 20 秒或身上沒有**就放）
LEAD = 20.0
# 多久重讀一次身上的 buff 樹（單招走樹搜尋實測 0.05~0.16ms，但心跳 10ms
# 一拍，不節流等於每秒白讀 100 次 × 五台）。
LIVE_GAP = 0.4
# 送出後「樹上一直看不到這招」連續幾次就放棄用樹確認（退回自己記時間）。
# ⚠⚠ 這道閘門是**防無限重放**用的：萬一某招放進身上的鍵不是技能 ID
#   （改版／特殊技能），沒有它就會變成每 RETRY 秒重放一次、永遠停不下來
#   —— 2026-08-20 castwatch 那次翻車就是這個形狀（見檔頭）。
TREE_MISS_MAX = 3
# 按鍵到遊戲把技能編號寫進欄位要一點時間，實測 1 秒內。
CONFIRM_WAIT = 1.2
# 學不到／放不出來時，隔多久再試（別每一拍都狂送）
RETRY = 8.0
# 送包後等「施放廣播」（castwatch）確認多久；等不到＝伺服器沒受理。
# 廣播實測一送就回（<1 秒），4 秒是給掉幀／網路抖動的餘裕。
CAST_WAIT = 4.0


class AutoBuff:
    """一個角色一份。

    `arm()`  —— 開自動戰鬥或中途勾選時呼叫，下一拍起開始顧。
    `step()` —— 每一拍呼叫。
    `skill`  —— 學到的技能編號（呼叫端負責存進設定）。
    """

    def __init__(self, vk: int, skill_id: int | None = None) -> None:
        self.vk = vk
        self._rd: quickbar.Reader | None = None   # 讀「目前頁碼」用（懶建）
        # ⚠ 存檔帶回來的技能編號也要**重新過表**（跟 adopt() 同一道門檻）：
        #   查不到持續時間就不收、當沒學過，讓快捷欄那條路重新學。
        #   不擋的話 secs=0 → left() 恆為 0 → 每 RETRY(8) 秒盲補一次，
        #   安靜地一直扣魔（表改版把那招移掉時就會發生）。
        info = skills.of(skill_id) if skill_id else None
        self.skill = info.id if info else None
        self.secs = float(info.secs) if info else 0.0
        self.reset()

    def reset(self) -> None:
        self._cast_at = 0.0
        self._sent_at = 0.0
        self._learning = False
        # 施放廣播確認（castwatch）：送了包、還在等「伺服器受理」那一包
        self._confirming = False
        self._cw_since = 0       # 送包前記下的攔包計數（只看之後的廣播）
        self._srv = 0            # 我的施法者伺服器ID（每次送包重讀，不快取）
        # ★ 這招的廣播「驗不了」（等過一次沒等到）→ 之後跳過確認（見 ①.5）
        self._cw_skip = False
        # 身上 buff 樹（buffs.py）的快取與失敗計數
        self._live_at = 0.0       # 上次讀樹的時間（monotonic）
        self._live: float | None = None   # 上次讀到的剩餘秒；None＝讀不到
        self._tree_miss = 0       # 送出後樹上看不到這招的連續次數
        self._live_before = 0.0   # 送出**前**身上還剩幾秒（確認的基準線）
        self._tree_skip = False   # 連續太多次 → 這招不用樹確認（退回自記時間）
        self.note = ""
        self.armed = False
        self.blocked = ""        # 那個鍵上根本沒有可用的技能 → 停手（見 block）

    def adopt(self, skill_id: int) -> bool:
        """直接指定技能編號（呼叫端從**快捷欄**讀到的），成功回 True。

        ★ 這是現在的正路：零副作用、不必按鍵、當場拿到。
        ⚠ 查不到持續時間（`skills.of` 只收 buff 類）就**不採用** ——
          那代表那一格放的不是 buff，硬補只會白扣魔。
        """
        info = skills.of(skill_id) if skill_id else None
        if info is None:
            return False
        if self.skill == info.id:
            self.blocked = ""
            return True
        self.skill, self.secs = info.id, float(info.secs)
        self._cast_at = 0.0                  # 還沒放過 → 下一拍就補一次
        self._sent_at = 0.0
        self._learning = False
        self._confirming = False
        self._cw_skip = False                # 換了技能 → 廣播驗不驗得到重新試
        self._live_at, self._live = 0.0, None
        self._tree_miss, self._tree_skip = 0, False   # 樹確認也重新試
        self.blocked = ""
        return True

    def block(self, why: str) -> None:
        """那個鍵上沒有可用的技能 → **停手**，別再盲按（大聲說明原因）。

        ⚠⚠ 這不是「暫時性失敗要無限重試」那一類（見 memory
          transient-failure-auto-retry）：F12 是空的是**確定的狀態**，
          再按幾次都一樣。舊版每 8 秒按一次、在 GUI 執行緒 sleep 40ms，
          白狐實機上就是這樣一直卡（2026-08-10）。
        ★ 使用者把技能放上去之後，呼叫端下一次 adopt() 會自動解除。
        """
        self.blocked = why

    def arm(self) -> None:
        """開始（或中途勾選）—— 下一拍看身上還剩幾秒再決定放不放。

        ⚠ 舊行為是「無腦放一次，不管身上有沒有」；現在身上還夠（> LEAD）
          就不放了（使用者 2026-08-22）。只有**讀不到** buff 樹時才會退回
          舊行為（`_cast_at = 0` → `left()` 是 0 → 這一拍就補）。
        """
        self._cast_at = 0.0
        self._sent_at = 0.0
        self._learning = False
        self._confirming = False
        self.armed = True

    def left(self) -> float:
        """自己記時間算出來的剩餘秒 —— **只在讀不到身上 buff 樹時才用**。"""
        if not self._cast_at or not self.secs:
            return 0.0
        return max(0.0, self.secs - (time.monotonic() - self._cast_at))

    def live_left(self, scanner) -> float | None:
        """身上**真正**還剩幾秒；身上沒有回 0.0；**讀不到回 None**。

        ★ 這是現在的主判據（使用者 2026-08-22：剩 < LEAD 或沒有才放）。
        ⚠ 節流 LIVE_GAP：心跳每 10ms 一拍，不快取等於每秒白讀 100 次。
        ⚠ `_tree_skip`（這招驗不了）時一律回 None，讓呼叫端退回自己記時間。
        """
        if not self.skill or self._tree_skip or scanner is None:
            return None
        now = time.monotonic()
        if now - self._live_at < LIVE_GAP:
            return self._live
        self._live_at = now
        try:
            self._live = buffs.left_of(scanner, self.skill)
        except Exception:                              # noqa: BLE001
            self._live = None                          # 讀取層炸了＝不知道
        return self._live

    def _forget_live(self) -> None:
        """讓下一拍一定重讀（剛送出去、狀態會變的時候呼叫）。"""
        self._live_at = 0.0

    def step(self, scanner, mover, hwnd, pf_this: int, my_id,
             stats_base: int, send_key, cast_hook=None) -> str:
        """走一步。回傳給狀態列看的說明。

        my_id / pf_this: ⛔ 補放改走快捷鍵路徑後**不再使用**（2026-08-20，
            見檔頭）——參數留著是為了呼叫端簽名不動；別拿掉，也別再依賴。
        cast_hook: castwatch.CastHook（施放廣播監聽），None＝沒有。
            有它才能 100% 確認「這一放真的被伺服器受理」——拒收不回、
            受理才回（見 app/game/castwatch.py）。沒有就退回舊行為
            （送出就當成功）。
        """
        if not self.armed:
            return self.note
        now = time.monotonic()

        # ① 學技能編號中（按過鍵，等遊戲把編號寫進欄位）
        if self._learning:
            if now - self._sent_at < CONFIRM_WAIT:
                return self.note
            self._learning = False
            sid = player.read_last_skill(scanner, stats_base) if stats_base else 0
            info = skills.of(sid) if sid else None
            if info is None:
                self.note = f"⚠ 學不到 F12 的技能，{RETRY:.0f} 秒後再試"
                self._sent_at = now
                return self.note
            self.skill, self.secs = info.id, float(info.secs)
            self._cast_at = now                 # 那一下按鍵本身就是第一次施放
            self.note = (f"已放分身：技能 {info.id}"
                         f"（持續 {info.secs / 60:.0f} 分）")
            return self.note

        # ①.5 按出去了 → 等伺服器的「施放廣播」確認真的放出去（castwatch）。
        #   看到＝確認受理。⚠⚠ 但**等不到 ≠ 被拒收**（2026-08-20 實機翻案，
        #   見檔頭）：嵐狐閒置實測廣播 0.4 秒內就回，所以真實情境等不到多半是
        #   **沒接到那一包**（環形緩衝 512 槽，打怪時封包洪流幾秒就繞一圈）。
        #   舊版把等不到當拒收、每 8 秒重放 → 無限補發迴圈（使用者實機症狀）。
        #   → 改判「驗不了」：當作已補、記住這招跳過確認（_cw_skip），不再重放。
        if self._confirming:
            # ★★★ 最硬的證據：**身上真的多了這個 buff**（buffs.py 讀樹）。
            #   比施放廣播可靠 —— 廣播會被封包洪流蓋掉（512 槽環形緩衝），
            #   身上有沒有是狀態不是事件，晚幾拍讀還是讀得到。
            live = self.live_left(scanner)
            # ⚠ 確認條件是「**剩餘比送出前變多**」，不是「剩 > LEAD」：
            #   ① 放成功＝重新計時，剩餘會跳回接近表定值；
            #   ② 沒放到＝剩餘只會繼續往下掉，永遠過不了這一關；
            #   ③ 用 `> LEAD` 的話，持續時間本來就比 LEAD 短的 buff
            #      **永遠確認不了**（放對了也判成沒放）。
            if live is not None and live > self._live_before + 1.0:
                self._confirming = False
                self._tree_miss = 0
                self._cast_at = self._sent_at
                self.note = (f"已補分身：技能 {self.skill}"
                             f"（身上確認，剩 {live / 60:.0f} 分）")
                return self.note
            if live is not None and now - self._sent_at >= CAST_WAIT:
                # 樹讀得到、卻**沒看到這招** → 這一發真的沒放到（硬證據）。
                #   照 RETRY 節奏再放；連續 TREE_MISS_MAX 次都這樣就別再用樹
                #   確認（免得鍵對不上時無限重放，見 TREE_MISS_MAX 的說明）。
                self._confirming = False
                self._tree_miss += 1
                if self._tree_miss >= TREE_MISS_MAX:
                    self._tree_skip = True
                    self._cast_at = self._sent_at      # 退回自己記時間
                    self.note = (f"⚠ 技能 {self.skill} 放了 {self._tree_miss} 次"
                                 f"都沒出現在身上 → 改用自己記時間，不再重放")
                else:
                    self.note = (f"⚠ 補分身沒生效（身上沒看到技能 {self.skill}）"
                                 f"→ {RETRY:.0f} 秒後再試")
                return self.note
            if (cast_hook is not None and cast_hook.active and self._srv
                    and cast_hook.fired(self._cw_since, self._srv, self.skill)):
                self._confirming = False
                self._cast_at = self._sent_at    # 從送包那一刻起算（保守）
                self.note = (f"已補分身：技能 {self.skill}"
                             f"（伺服器已受理，持續 {self.secs / 60:.0f} 分）")
                return self.note
            if live is None and (cast_hook is None or not cast_hook.active):
                # 樹也讀不到、監聽也沒有 → 沒訊號可等，退回「送出就當成功」
                #   ⚠ 條件裡的 `live is None` 不能拿掉：樹讀得到時要等它出現
                #   （上面那兩段），這裡搶著判成功會讓下一拍又看到「身上沒有」
                #   而重放一次。
                self._confirming = False
                self._cast_at = self._sent_at
                return self.note
            if now - self._sent_at >= CAST_WAIT:
                self._confirming = False
                self._cw_skip = True             # 這招驗不了，之後不再等廣播
                self._cast_at = self._sent_at    # 當作已補（送出就算）
                self.note = (f"已補分身：技能 {self.skill}（沒接到施放廣播——"
                             f"改回按出就算，不重放；"
                             f"持續 {self.secs / 60:.0f} 分）")
            return self.note

        # ② 時間還夠 → 什麼都不做
        #   ★★★ 判據是**身上真正的剩餘時間**（使用者 2026-08-22 指定：
        #     剩 < LEAD 秒、或身上根本沒有，才放）。`live_left` 回 0.0 就是
        #     「身上沒有」，回 None 才是「讀不到」。
        #   ⚠ 讀不到時退回自己記時間（舊行為），不可以當成「沒有」——
        #     那會每輪白放一次、白扣魔。
        #   ⛔ 這裡不要回「分身還有 X.X 分」的倒數 —— 那個字每 6 秒變一次，
        #     狀態列會被它一直蓋掉（使用者明講不要在下面看到倒數）。
        #     狀態列只留事件：放了／補了／失敗。
        live = self.live_left(scanner)
        if live is not None:
            if live > LEAD:
                return self.note
        elif self._cast_at and self.left() > LEAD:
            return self.note
        if now - self._sent_at < RETRY and self._sent_at:
            return self.note                    # 剛失敗過，先等等

        # ③ 還沒學過技能編號 → 按一次 F12（這一下也就是第一次施放）
        #    ⚠⚠ 這是**保底**：正路是呼叫端直接讀快捷欄再 adopt()。
        #      那一格確定沒有技能時呼叫端會 block()，這裡就停手不再盲按 ——
        #      按鍵會在 GUI 執行緒 sleep 40ms，每 8 秒一次會拖累全部分頁。
        if not self.skill:
            if self.blocked:
                return self.blocked
            if not stats_base:
                self.note = "⚠ 讀不到角色屬性，沒辦法學 F12 的技能"
                return self.note
            # ⚠ 一定要先清零：欄位會留上一次的技能，不清會學錯（踩過）。
            try:
                player.clear_last_skill(scanner, stats_base)
            except Exception:                              # noqa: BLE001
                pass
            send_key(hwnd, self.vk)
            self._sent_at = now
            self._learning = True
            self.note = "第一次啟用：按 F12 學技能編號…"
            return self.note

        # ④ 已經知道技能編號 → 走**客戶端自己的快捷鍵路徑**補
        #   （quickbar.use＝usequickkey，跟真按 F12 同一支，含「自己實體
        #   NULL 就不按」的崩潰閘門）。裸 CAST_FN 在記憶體層面等價（都扣
        #   51 MP、都有廣播），差別在客戶端側效果會不會演——見檔頭實測。
        if not (mover is not None and mover.active):
            self.note = "⚠ 跳板沒裝上，補不了分身"
            self._sent_at = now
            return self.note
        slot = self.vk - quickbar.VK_F1
        if self._rd is None or self._rd._sc is not scanner:
            self._rd = quickbar.Reader(scanner)
        page = self._rd.page()               # F12 作用在「目前顯示的頁」
        try:
            cells = quickbar.read_page(scanner, page)
        except Exception:                              # noqa: BLE001
            cells = None
        cell = (cells[slot] if cells is not None and 0 <= slot < len(cells)
                else None)
        if cells is not None and cell is not None and cell.is_skill \
                and cell.value != self.skill:
            # ★★ 那一格換了另一招（使用者換技能／設定檔存的是舊編號）→
            #   **當場收下現況**，不要卡住。2026-08-20 實機根因就是這個：
            #   設定裡 5424、F12 上其實是 5471，舊版一路放不存在的那招 →
            #   分身放不出來又永遠等不到廣播 → 無限補發。
            #   ⚠ adopt() 自己會過 skills.of 那道門檻（不是 buff 就不收）。
            if not self.adopt(cell.value):
                self.note = (f"⚠ 快捷欄第 {slot + 1} 格的技能 {cell.value}"
                             f"不是 buff（查不到持續時間）→ 先不補")
                self._sent_at = now
                return self.note
        if cells is not None and (cell is None or not cell.is_skill):
            # 空格／物品 —— 硬按等於亂用東西。呼叫端每 2 秒重讀，放上去就接手。
            self.note = (f"⚠ 快捷欄目前頁第 {slot + 1} 格沒有技能"
                         f"（翻頁？）→ {RETRY:.0f} 秒後再看")
            self._sent_at = now
            return self.note
        # ★ 確認用的兩個錨要在**按下去前**先記：廣播可能一送就回來，送完才記
        #   write_count 會把那一包漏掉。施法者ID每次重讀（換圖/重連會重建
        #   玩家物件，快取的是舊值 → 永遠對不上）。
        # ★ _cw_skip＝上次等過廣播沒等到（這招可能不廣播）→ 不再白等 4 秒。
        srv = since = 0
        if cast_hook is not None and cast_hook.active and not self._cw_skip:
            try:
                srv = castwatch.own_server_id(
                    scanner, bag.player_entity(scanner)) or 0
            except Exception:                              # noqa: BLE001
                srv = 0
            since = cast_hook.write_count()
        if cells is not None:
            ok = quickbar.use(mover, scanner, slot, page)
        else:
            # 快捷欄整個讀不到（改版位移）→ 跟人按鍵同一條路保底。
            send_key(hwnd, self.vk)
            ok = True
        self._sent_at = now
        if not ok:
            # use() 回 False＝換圖空窗（自己實體 NULL）／指令槽忙——都是
            # 暫時性的，照 RETRY 節奏再試（transient-failure-auto-retry）。
            self.note = f"⚠ 補分身這一下沒按成（換圖中？）→ {RETRY:.0f} 秒後再試"
        elif srv or live is not None:
            # 有確認手段就先別當成功，等 ①.5：
            #   ★ 身上 buff 樹讀得到（live 不是 None）＝最硬的證據；
            #   ★ 施放廣播（srv）＝次強，樹壞掉時的退路。
            self._confirming = True
            self._cw_since = since
            self._srv = srv
            self._live_before = live or 0.0   # 確認的基準線（見 ①.5）
            self._forget_live()          # 狀態剛變 → 下一拍一定重讀
            self.note = ("補分身：已按出，確認中…" if live is not None
                         else "補分身：已按出，等伺服器受理…")
        else:
            # 兩種確認都沒有（樹讀不到＋監聽裝不起來）→ 按出就當成功
            self._cast_at = now
            self.note = (f"已補分身：技能 {self.skill}（快捷鍵路徑，"
                         f"持續 {self.secs / 60:.0f} 分）")
        return self.note
