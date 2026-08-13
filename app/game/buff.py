"""自動分身：開自動戰鬥時無腦放一次，之後時間到就用**封包**補。

怎麼放
------
`attack.CAST_FN(技能ID, 自己的實體ID, 0, 0, 0)` —— 一包就夠。
照使用者攔到的 F12 封包原樣重做：

    0x664517(0x1530, 0x47B403C2, 0, 0, 0)
             ↑技能5424  ↑自己的實體ID   ↑座標填 0，不是實際位置

★ 實測扣魔驗證：MP 4729 → 4699（正好 30，就是技能 5424 的消耗）。
⚠⚠ 驗證要**每 0.25 秒取樣**：MP 只低 2 秒就補回來了，隔 2.5 秒才看會什麼都
  看不到 —— 我第一次就是這樣誤判成「封包沒生效」，白繞了一圈。
★ 送封包**不必停下來**：走路中、打怪中都送得出去，不像按鍵會被當前動作吃掉。

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
持續時間查 `skills.py`（資源包抽出來的，單位是秒；黑狐的 5424 是 1200 秒）。
剩不到 LEAD 秒就補。**不偵測身上有沒有**（使用者要求）——
遊戲的 buff 剩餘時間找不到（試過 11 種方法），與其猜不如每次開就重放一次。
"""
from __future__ import annotations

import time

from app.game import attack, player, skills

# 剩幾秒就補（使用者指定 10 秒）
LEAD = 10.0
# 按鍵到遊戲把技能編號寫進欄位要一點時間，實測 1 秒內。
CONFIRM_WAIT = 1.2
# 學不到／放不出來時，隔多久再試（別每一拍都狂送）
RETRY = 8.0


class AutoBuff:
    """一個角色一份。

    `arm()`  —— 開自動戰鬥或中途勾選時呼叫，下一拍就無腦放一次。
    `step()` —— 每一拍呼叫。
    `skill`  —— 學到的技能編號（呼叫端負責存進設定）。
    """

    def __init__(self, vk: int, skill_id: int | None = None) -> None:
        self.vk = vk
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
        """開始（或中途勾選）—— 下一拍無腦放一次，不管身上有沒有。"""
        self._cast_at = 0.0
        self._sent_at = 0.0
        self._learning = False
        self.armed = True

    def left(self) -> float:
        if not self._cast_at or not self.secs:
            return 0.0
        return max(0.0, self.secs - (time.monotonic() - self._cast_at))

    def step(self, scanner, mover, hwnd, pf_this: int, my_id,
             stats_base: int, send_key) -> str:
        """走一步。回傳給狀態列看的說明。

        my_id: 自己的實體 ID，**也可以傳一個 callable**，要用到時才呼叫。
            ★ 那個值要讀一次記憶體，但只有真的要送補分身封包（20 分鐘一次）
              才用得到 —— 掛機的心跳是 10ms 一拍，每拍都先算好等於白讀。
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

        # ② 時間還夠 → 什麼都不做
        #   ⛔ 這裡不要回「分身還有 X.X 分」的倒數 —— 那個字每 6 秒變一次，
        #     狀態列會被它一直蓋掉（使用者明講不要在下面看到倒數）。
        #     狀態列只留事件：放了／補了／失敗。
        if self._cast_at and self.left() > LEAD:
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

        # ④ 已經知道技能編號 → 用封包補（不必停下來、不占鍵盤）
        mid = my_id() if callable(my_id) else my_id
        if not (mover is not None and mover.active and pf_this and mid):
            self.note = "⚠ 跳板沒裝上，補不了分身"
            self._sent_at = now
            return self.note
        ok = mover.call(attack.CAST_FN, self.skill, mid, 0, 0, 0)
        self._sent_at = now
        if ok:
            self._cast_at = now
            self.note = (f"已用封包補分身：技能 {self.skill}"
                         f"（持續 {self.secs / 60:.0f} 分）")
        else:
            self.note = "⚠ 補分身的封包排不進去"
        return self.note
