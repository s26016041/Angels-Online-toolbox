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
⛔ 快捷欄的技能欄位找不到（連同這次用五個已知技能當特徵，共試過五種方法）。
★ 所以**學一次**：第一次啟用時按一下 F12，讀「角色屬性 −0x50」就知道是哪個
  技能，然後**存進設定**。之後一律走封包，重開也不用再按。
  那一下按鍵正好就是使用者要的「開起來先無腦放一次」。

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
        self.skill = skill_id or None
        self.secs = float(getattr(skills.of(self.skill), "secs", 0)
                          if self.skill else 0)
        self.reset()

    def reset(self) -> None:
        self._cast_at = 0.0
        self._sent_at = 0.0
        self._learning = False
        self.note = ""
        self.armed = False

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
        if self._cast_at and self.left() > LEAD:
            self.note = f"分身還有 {self.left() / 60:.1f} 分"
            return self.note
        if now - self._sent_at < RETRY and self._sent_at:
            return self.note                    # 剛失敗過，先等等

        # ③ 還沒學過技能編號 → 按一次 F12（這一下也就是第一次施放）
        if not self.skill:
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
