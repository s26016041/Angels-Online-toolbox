"""長距離導航：讀地形圖自己算最短路，一段一段走過去。

★★★ 只有一條路：**我們自己算**
------------------------------
整張地圖的可走格就攤在遊戲的記憶體裡（`app/game/terrain.py`，讀完 5~8ms、
A* 12~19ms），所以路線一律自己算 —— 純讀記憶體、不呼叫遊戲、不會崩潰，
而且是真正的最短路。

⛔ 2026-08-10 全部刪除的東西（使用者指定「把官方幫忙算的那條全面刪除」）
------------------------------------------------------------------
以前這裡還有兩層退路，兩層都是**問遊戲的尋路函式**：

  ① 360 度找中繼點：一次問 96 個方向哪個走得通，允許暫時走遠。
  ② 路線記憶：走通過一次就把軌跡記起來，下次重播，再用「抄近路」修短。

它們是「還沒有地形圖」那個年代的產物。留著的害處是實際發生過的：
遊戲的尋路一次只算得出約 30 格，中繼點又都取在**往目標的方向**，
隔著岩層時每一個候選都在牆裡 —— 角色就一路推著牆走
（檔案史上兩筆實錄：「60 秒送 158 次移動指令、一格都沒動」、
使用者 2026-08-10 回報「搜尋到障礙物對面的怪，卡在牆邊直到周圍怪物重生」）。

而且地形圖正常時它們**根本不會被執行到**（最短路一定先算出來）。
實測（5 台分身、4 張地圖、150 次連續讀取）地形圖 **150/150 全部讀得到**，
讀不到只發生在換圖／換頻道那一瞬間 —— 那一瞬間本來就不該走路。

所以現在只剩兩種結果，兩種都看得見：
  · 算得出路 → 照最短路走。
  · 算不出來 → **這一拍不走**（換圖中）或 `stuck=True`（真的到不了），
    呼叫端換目標。**絕不猜一個方向推推看。**

實測（惡礁峽谷 142 格直線）：
    舊的探索      429 格 / 49.6 秒
    舊的路線記憶  194~205 格 / 20 秒
    地形圖最短路  175 格　　←　現在只剩這個
"""
from __future__ import annotations

import math
import time

from app.game import entity, terrain

# ⚠⚠ 送出移動指令到角色真的開始動，實測 107~154ms。這段期間動畫狀態還是
#   'Wait'，如果照 10ms 的心跳一直重送，會**把上一個指令重置掉**，角色就在
#   原地抖（實測改成 0.12 秒重送，卡頓從 0.5 秒變成一路互相打斷）。
#   所以送完先當作「忙碌」這麼久。0.3 秒留了兩倍餘裕。
SEND_GRACE = 0.30

ARRIVE = 3.0             # 離目標這麼近就算到了
NEAR_SUB = 3.0           # 離路線上的轉折點這麼近就算走到了
STALL_TRIES = 4          # 同一個轉折點連續這麼多拍沒進展就重算
# 最短路走不動（多半是被怪／別的玩家擋在路上）最多從頭重算幾次。
# 超過就認輸並讓呼叫端換目標 —— 無限重算會讓巡邏永遠停在同一個地方。
# ⚠⚠ 數的是**連續毫無進展**的重算：只要往前挪了（走到下一個轉折點、或離目前
#   這個轉折點又近了 0.5 格）額度就還回去（2026-08-24 修）。
#   舊版 `_replans` 一路累加、只有換目標才歸零 —— 走一趟兩百格的巡邏，
#   路上被怪擋三次（每次都自己脫困、也真的一路走近了）就會在第四次被判
#   「走不到」，然後那張圖只有一個巡邏點的話**直接停掉掛機**。
#   使用者 2026-08-24 回報「巡邏點設得到就一定走得到，為什麼會走不到就停」
#   —— 就是這個。
REPLAN_MAX = 3


def _d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Navigator:
    """一個角色一份。每一拍呼叫 `step()`。

    `note` 是給狀態列看的說明；`stuck` 為 True 代表真的到不了，呼叫端應該
    換下一個目標（別再耗下去）。
    """

    def __init__(self, maps: "terrain.Cache | None" = None) -> None:
        # 地形圖（可走格）。★ 呼叫端可以把自己那份快取傳進來共用：一張圖
        #   54000 格，打怪那邊也要查同一份，各存一份只是白佔記憶體又要各讀一次。
        self._maps = maps if maps is not None else terrain.Cache()
        self.reset()

    def reset(self, goal: tuple[float, float] | None = None) -> None:
        self.goal = goal
        self.note = ""
        self.stuck = False
        # ★★ `stuck` 有**兩種完全不同的意思**，呼叫端要分開處理（2026-08-24）：
        #   "grid"    地形圖說根本沒有路 → 真的到不了（設定／地圖的問題）
        #   "blocked" 路是通的，只是一直被擋著走不動 → **暫時性失敗**，
        #             照使用者的規矩要一直重試，不准拿它停掉掛機
        #             （[[transient-failure-auto-retry]]）
        self.stuck_reason = ""
        self._route: list | None = None      # 目前這條最短路的轉折點
        self._ri = 0                         # 走到第幾個轉折點
        self._best = None                    # 對目前轉折點的最佳距離
        self._stall = 0                      # 連續幾拍沒進展
        self._sent = 0.0                     # 上次送出移動指令的時間
        self._replans = 0                    # 重算過幾次（防無限重算）
        self._grid_fail = 0                  # 地形圖連續算不出幾次
        # ★ 段尾不停頓：剛走到一個轉折點時設 True，讓下一拍**不等角色停下**
        #   就直接送出下一段 —— 官方攔包就是邊走邊送（一段接一段、全程不停）。
        #   只在「換點」那一拍舉旗，不會連發（連發會把還沒開始的指令重置掉）。
        self._go_now = False

    # -- 內部 ---------------------------------------------------------
    def _plan_from_grid(self, scanner, here, goal) -> bool:
        """讀地形圖算最短路，成功就把轉折點放進 `_route`。

        回 False 有兩種意思，都寫在 `note` 裡：
          · 讀不到地形圖（換圖那一瞬間）→ 這一拍不走，下一拍再試。
          · 地形圖說到不了 → `stuck=True`，呼叫端換目標。
        """
        grid = self._maps.get(scanner)
        if grid is None:
            # ★★★ 沒有地形圖 → **這一拍不走**（2026-08-10 使用者指定）。
            #   以前這裡會掉進「360 度找中繼點、問遊戲的尋路」那套猜測式繞路
            #   —— 那條在凹地形會推著牆走。
            self.note = f"⚠ {self._maps.why or '讀不到地形圖'} → 這一拍不走"
            return False
        wp = grid.waypoints(here, goal)
        if not wp:
            # 地形圖說「真的走不到」。
            # ⚠ 但**要連續兩次才算數**：剛傳送完／換圖那一瞬間，圖跟座標可能
            #   還對不起來，一次就判死會把好好的巡邏點誤判成到不了。
            #   第一次先把圖丟掉重讀，下一拍再問一次。
            self._grid_fail += 1
            if self._grid_fail < 2:
                self._maps.drop()
                self.note = "地形圖算不出路徑，重讀一次再試"
                return False
            self.stuck = True
            self.stuck_reason = "grid"
            self.note = "⛔ 地形圖顯示到不了那裡"
            return False
        self._grid_fail = 0
        self._route = [(float(x) + 0.5, float(y) + 0.5) for x, y in wp]
        self._ri = 0
        self._best = None
        self._stall = 0
        return True

    # -- 主迴圈 -------------------------------------------------------
    def step(self, scanner, mover, player_obj, gx: float, gy: float) -> str:
        """走一步。回傳給狀態列看的說明。"""
        goal = (gx, gy)
        if self.goal is None or _d(self.goal, goal) > 1.0:
            self.reset(goal)
        if not (mover is not None and mover.active and player_obj):
            self.note = "⚠ 跳板沒裝上"
            return self.note

        here = entity.read_pos(scanner, player_obj)
        if here is None:
            self.note = "⚠ 讀不到座標"
            return self.note
        if _d(here, goal) <= ARRIVE:
            self.note = "到了"
            return self.note

        # ★★★ 唯一的規劃方式：讀地形圖自己算最短路（見 terrain.py）。
        #   整張圖 5~8ms 讀完、A* 12~19ms 算完，而且是真正的最短路。
        #   ⚠ 純讀記憶體、完全不呼叫遊戲，所以沒有崩潰風險。
        if self._route is None:
            if not self._plan_from_grid(scanner, here, goal):
                # ⛔ 算不出來就**什麼都不做**。以前這裡會往下掉進猜測式繞路
                #   （360 度問遊戲的尋路），那正是「卡在牆邊」的來源。
                return self.note

        # 角色還在走 —— 什麼都別做（重下指令會把上一個打斷）。
        # 剛送出指令那 SEND_GRACE 秒也算「忙碌」：那時它還沒開始動，
        # 狀態仍是 'Wait'，不擋的話心跳會每 10ms 重送一次。
        # ★ 例外：剛走到一個轉折點（_go_now）→ 不等停下，直接送下一段。
        if ((entity.is_walking(scanner, player_obj)
                or time.monotonic() - self._sent < SEND_GRACE)
                and not self._go_now):
            return self.note

        # ── 照最短路一個轉折點一個轉折點走 ────────────────────
        while (self._ri < len(self._route)
               and _d(here, self._route[self._ri]) <= NEAR_SUB):
            self._ri += 1
            self._go_now = True     # 到點了 → 下一拍立刻送下一段
            self._best = None
            self._replans = 0       # ★ 走到一個轉折點＝真的有進展（見 REPLAN_MAX）
        if self._ri >= len(self._route):
            # 路線走完了還沒到（終點被放寬到最近的可走格）→ 重算一次收尾。
            self._route = None
            self.note = "走完這條路線 → 重算收尾"
            return self.note

        pt = self._route[self._ri]
        d = _d(here, pt)
        if self._best is None:
            self._best, self._stall = d, 0      # 這一段剛開始，還沒有基準
        elif d < self._best - 0.5:
            # ★ 真的又走近了 → 重算額度還回去（見 REPLAN_MAX 的 ⚠⚠）。
            #   ⚠ 不可以把這條併回 `_best is None`：剛重算完 `_best` 就是 None，
            #     那樣每重算一次都會立刻把額度洗掉 → REPLAN_MAX 形同虛設。
            self._best, self._stall = d, 0
            self._replans = 0
        else:
            self._stall += 1
            if self._stall >= STALL_TRIES:
                # 走不動（多半是被怪／別的玩家擋住）→ 從現在的位置重算。
                self._replans += 1
                self._route = None
                self._maps.drop()          # 順便重讀圖（可能換圖了）
                if self._replans > REPLAN_MAX:
                    self.stuck = True
                    self.stuck_reason = "blocked"
                    self.note = (f"⛔ 連續 {REPLAN_MAX} 次重算都完全沒往前走"
                                 "（路被擋住？）")
                    return self.note
                self.note = f"路上被擋住 → 重算最短路（第 {self._replans} 次）"
                return self.note
        # ★ 最短路的每一段本來就保證直線可通 → 直接把終點交給遊戲的走路常式，
        #   不必再請它尋一次路（省一次呼叫、不佔指令槽）。
        mover.walk_route(scanner, player_obj, pt[0], pt[1],
                         stop_short=0.0, points=[pt])
        self._sent = time.monotonic()
        self._go_now = False
        self.note = f"走最短路（{self._ri + 1}/{len(self._route)}）"
        return self.note
