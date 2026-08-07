"""長距離導航：走得到、而且不會卡在牆前面。

為什麼需要這個
--------------
遊戲的尋路函式一次只算得出約 30 格。超過就回 0，這時要自己找中繼點接力。
舊做法是「沿著**往目標的直線**取 28/18/10/5 格」，全失敗才試 ±70° 繞路 ——
遇到凹形地形就死。實測（黑狐，人馬崗哨）：角色停在 (97.5, 62.5)，
**60 秒送了 158 次移動指令、一格都沒動**，而且每次都回報「直線可通」。

那個位置往目標（-73°）整片是牆，唯一的出路是**正東 0°**，離目標方位 73°
—— 剛好超出 ±70°。而且就算試到了也不會選，因為往東會離目標更遠。

怎麼做（照使用者攔到的官方封包學的）
------------------------------------
官方點地圖走同一段路，解出來的路線是：

    (22.5,159.5) → (33.5,148.5) → (77.5,97.5) → (67.9,77.9) → (73.2,32.7) → 終點
                                                 ↑ 往西，離目標更遠

它會**主動往反方向繞**。而且一包最多 5 個路徑點、一段走 67 格。

所以這裡的規則是：
  ① 先問直達路徑，算得出來就直接走。
  ② 算不出來 → **360 度**找可達的中繼點（不是只沿著往目標的直線）。
  ③ **允許暫時走遠**：走過的地方半徑內不再當候選，逼它往外探索。
  ④ 走完**用實際位移驗證**：沒走到就把那個中繼點列黑名單。
     ⚠⚠ 尋路會說謊 —— 實測它說 (85,56) 可達，走過去停在 9.8 格外。
     只在「完全沒動」才列黑名單會出現**乒乓迴圈**（實測在兩點之間來回 60 次
     都不停），所以是「沒走到就列」。

實測（黑狐，人馬崗哨，183 格）：
    去程 7 段 25 秒、回程 10 段 49 秒，重跑三次路線一致。
    舊做法：走到一半就永遠卡住。

⚠ 掃描很貴（96 個候選 × 約 6ms ≈ 0.5 秒），**不能在心跳裡一次做完**，
  否則畫面會凍住。所以 `step()` 每次只評估 SCAN_PER_STEP 個候選，
  分好幾拍做完。

2026-08-07 補的兩件事（嵐狐實測 142 格腿：探索走了 429 格、49.6 秒，
官方同段 5 包直達 —— 差距就出在這兩處）：
  ⑤ **路線記憶**：走通一次之後 `learned()` 把實際軌跡去彎（_prune），
     呼叫端存起來，下次同段路傳回 `step(route=…)` 直接重播 ——
     巡邏來回不必每趟重新探路（乒乓探索正是「卡很久」的主因）。
  ⑥ **段尾不停頓**（_go_now）：走到轉折點不等角色停下，下一拍就送下一段。
     官方攔包就是這個節奏。⚠ 只在「換點」那一拍舉旗，不是連發重送 ——
     連發會把還沒開始的指令重置掉（SEND_GRACE 的老坑）。
"""
from __future__ import annotations

import math
import time

from app.game import entity

# ⚠⚠ 送出移動指令到角色真的開始動，實測 107~154ms。這段期間動畫狀態還是
#   'Wait'，如果照 10ms 的心跳一直重送，會**把上一個指令重置掉**，角色就在
#   原地抖（實測改成 0.12 秒重送，卡頓從 0.5 秒變成一路互相打斷）。
#   所以送完先當作「忙碌」這麼久。0.3 秒留了兩倍餘裕。
SEND_GRACE = 0.30

ARRIVE = 3.0             # 離目標這麼近就算到了
NEAR_SUB = 3.0           # 離中繼點這麼近就算走到了
# 中繼點的候選半徑（由遠到近）。
# ⛔ **試過放大到 45/34/26/18/11，反而更慢**（同一段路 33 秒 → 75 秒）。
#   原因量出來了：半徑 30 有 15/24 個方向可達，40 只剩 8/24、50 只剩 5/24。
#   半徑一大，掃描要試更多次才找得到走得通的，而且挑到的遠點方向更差。
#   官方客戶端確實會連續走 44 格，但那是它自己的路線規劃給的，
#   硬把我們的半徑拉大只是白花查詢。這組 30/22/15/9 是實測最好的。
RADII = (30.0, 22.0, 15.0, 9.0)
STEP_DEG = 15            # 掃描角度間隔
VISIT_R = 7.0            # 走過的地方半徑內不再當候選（逼它往外探索）
FAIL_R = 5.0             # 黑名單半徑內不再當候選
STALL_TRIES = 4          # 同一個中繼點連續這麼多次沒進展就放棄它
SCAN_PER_STEP = 4        # 每一拍最多評估幾個候選（一個約 6ms）
MEMORY = 60              # 走過的地方最多記幾個（太多會找不到候選）
# ★ 連續這麼多段都沒有比「歷史最近」更靠近目標 → 認定到不了。
#   少了這道，目標在走不到的地方時它會在開闊區一直繞（單元測試 400 拍還在繞）。
#   實測走得到的路線最多只用 10 段，而且中間只退步過一次，所以 12 很寬鬆。
GIVE_UP_HOPS = 12
# ★★ 學路線的去彎半徑：走通之後把「實際走過的點」修剪成乾淨路線 ——
#   兩點若在這個半徑內，中間繞的那一段整段剪掉（探索時的死路、乒乓都會消失）。
#   實測（嵐狐 惡礁峽谷 142 格）：探索走了 429 格、來回彈 6 次；半徑 12 剪完
#   只剩東側峽谷 6 個點，跟官方攔包解出來的路線一致（半徑 8 剪不乾淨，
#   口袋裡的落點彼此相距 9~15 格，會留一段東西彈跳）。
#   ⚠ 上限的依據：重播時每一跳都是 walk_route 重新尋路，兩點之間就算有
#     小地形也會自己繞 —— 只要跳距遠小於尋路範圍（~30 格）就安全。
ROUTE_PRUNE = 12.0
# 重播時最多往後試幾個點能不能直接走到（見「抄近路」）。
SKIP_AHEAD = 4
# 問尋路之前先擋掉太遠的點：一次算得出的範圍實測約 30~40 格，超過必回 0
# —— 那是白花一次 6ms 的呼叫，還會跟攻擊搶指令槽。
PATH_MAX = 28.0


def _d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _prune(pts: list) -> list:
    """把實際走過的軌跡剪成乾淨路線：對每個點，找**最後一個**離它很近的
    後續點直接跳過去 —— 中間的繞路、死路、乒乓整段消失（迴圈消除法）。"""
    out, i = [], 0
    while i < len(pts):
        j = i
        for k in range(len(pts) - 1, i, -1):
            if _d(pts[i], pts[k]) < ROUTE_PRUNE:
                j = k
                break
        out.append(pts[j])
        i = j + 1
    return out


class Navigator:
    """一個角色一份。每一拍呼叫 `step()`。

    `note` 是給狀態列看的說明；`stuck` 為 True 代表真的到不了，呼叫端應該
    換下一個目標（別再耗下去）。
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self, goal: tuple[float, float] | None = None) -> None:
        self.goal = goal
        self.sub: tuple[float, float] | None = None
        self.visited: list[tuple[float, float]] = []
        self.failed: list[tuple[float, float]] = []
        self.note = ""
        self.stuck = False
        self._best = None          # 對目前中繼點的最佳距離（判斷有沒有進展）
        self._stall = 0
        self._scan: list | None = None      # 還沒評估完的候選
        self._found = None                  # 掃描過程中目前最好的候選
        self._hops = 0
        self._sent = 0.0                    # 上次送出移動指令的時間
        self._closest = None                # 歷來離目標最近過多少
        self._no_gain = 0                   # 連續幾段沒有更靠近
        # ★ 路線重播（呼叫端傳進來「上次走通的路線」時走這個模式）
        self._route: list | None = None
        self._ri = 0
        self._route_dead = False            # 重播失敗過 → 這一趟不再採用
        self._skip_done = False             # 這個路線點的「抄近路」試過了嗎
        # ★ 段尾不停頓：剛走到一個轉折點／路線點時設 True，讓下一拍**不等
        #   角色停下**就直接規劃並送出下一段 —— 官方就是邊走邊送（攔包實錄
        #   一段接一段、全程不停）。只在「換點」的那一拍舉旗，不會連發。
        self._go_now = False

    def learned(self) -> list[tuple[float, float]]:
        """這一趟**實際走通**的路線（已去彎）。走到目標後、reset 之前呼叫，
        存起來下次同一段路直接重播 —— 巡邏兩點來回就不必每趟重新探路。
        （重播那一趟不記軌跡，回空清單；呼叫端記得別拿空的去覆蓋舊路線。）"""
        return _prune(self.visited)

    # -- 內部 ---------------------------------------------------------
    def _blacklist(self, c) -> None:
        self.failed.append(c)
        if len(self.failed) > MEMORY:
            del self.failed[0]

    def _visit(self, p) -> None:
        if not self.visited or _d(p, self.visited[-1]) > 1.0:
            self.visited.append(p)
            if len(self.visited) > MEMORY:
                del self.visited[0]

    def _usable(self, c) -> bool:
        return (not any(_d(c, v) < VISIT_R for v in self.visited)
                and not any(_d(c, f) < FAIL_R for f in self.failed))

    def _start_scan(self, here, goal) -> None:
        """排出候選，**每一圈先照「離目標多近」排序**。

        ★★ 排序之後就可以「問到第一個走得通的就停」——
          排序後第一個走得通的，正好就是走得通裡面離目標最近的，
          結果跟問完 96 個再挑完全一樣，但通常問幾次就中。
          這是卡頓的主因：舊版每次繞路都要站著把 96 個問完（約 1.5 秒），
          使用者看到的就是「在某個地方卡一下」。
        ⚠ 圈與圈之間**維持由遠到近**，不要全域排序 —— 一次走 30 格比
          走 9 格少繞很多段，寧可遠一點也要挑大圈的。
        """
        self._scan = []
        for r in RADII:
            ring = []
            for deg in range(0, 360, STEP_DEG):
                a = math.radians(deg)
                c = (here[0] + math.cos(a) * r, here[1] + math.sin(a) * r)
                if self._usable(c):
                    ring.append((_d(c, goal), c))
            ring.sort(key=lambda t: t[0])
            self._scan.extend(c for _s, c in ring)
        self._found = None

    # -- 主迴圈 -------------------------------------------------------
    def step(self, scanner, mover, player_obj, gx: float, gy: float,
             route: list | None = None) -> str:
        """走一步。回傳給狀態列看的說明。

        route: 上次走通這段路的路線（`learned()` 的結果）。給了就直接重播，
        不重新探路；重播走不通會自動退回一般探索。
        """
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

        # ★ 有上次走通的路線就直接重播（只採用一次；失敗過就這趟不再試）。
        #   從離自己最近的點接上 —— 被打怪打斷、從半路恢復也接得回去。
        if (route and self._route is None and not self._route_dead
                and self.sub is None and self._scan is None):
            self._route = list(route)
            self._ri = min(range(len(self._route)),
                           key=lambda i: _d(here, self._route[i]) - i * 0.01)
            self._best = None
            self._stall = 0
            self._skip_done = False

        # 角色還在走 —— 什麼都別做（重下指令會把上一個打斷）。
        # 剛送出指令那 SEND_GRACE 秒也算「忙碌」：那時它還沒開始動，
        # 狀態仍是 'Wait'，不擋的話心跳會每 10ms 重送一次。
        # ★ 例外：剛走到一個轉折點（_go_now）→ 不等停下，直接規劃下一段。
        #   送出的新指令會接手路徑，角色就不會在每個轉折點頓一下 ——
        #   官方攔包就是這個節奏（邊走邊送下一段）。
        if ((entity.is_walking(scanner, player_obj)
                or time.monotonic() - self._sent < SEND_GRACE)
                and not self._go_now):
            return self.note

        # ── 路線重播：照上次走通的點一個接一個走 ──────────────
        if self._route is not None:
            while (self._ri < len(self._route)
                   and _d(here, self._route[self._ri]) <= NEAR_SUB):
                self._visit(here)       # ★ 記下來 → learned() 會回傳「這趟真正
                self._ri += 1           #   走過的點」，路線每跑一趟就自己變短
                self._go_now = True     # 到點了 → 下一拍立刻送下一段
                self._best = None
                self._skip_done = False
            if self._ri >= len(self._route):
                self._route = None      # 路線走完，收尾交回一般導航（可直達）
            else:
                # ★★ 抄近路：探索學來的路線裡常留著死路探測（實測那段
                #   西→東→西的來回還在裡面）。每到一個點就問尋路「後面幾個
                #   點有沒有直接走得到的」，有就跳過中間那幾個。
                #   走過一次之後 _visit 記的是跳過後的點，所以**路線會越跑
                #   越短**，最後收斂成真正的正路。
                #   ⚠ 一個點只試一次（_skip_done）：path_to 每次約 6ms，
                #     而且會跟攻擊搶指令槽。
                if not self._skip_done:
                    self._skip_done = True
                    for j in range(min(self._ri + SKIP_AHEAD,
                                       len(self._route) - 1), self._ri, -1):
                        c = self._route[j]
                        if _d(here, c) > PATH_MAX:
                            continue
                        n = mover.path_to(scanner, c[0], c[1])
                        if n > 0:
                            if j > self._ri:
                                self.note = f"抄近路（跳過 {j - self._ri} 個點）"
                            self._ri = j
                            break
                        if n < 0:
                            self._skip_done = False   # 沒問到，下次再試
                            break
                pt = self._route[self._ri]
                d = _d(here, pt)
                if self._best is None or d < self._best - 0.5:
                    self._best, self._stall = d, 0
                else:
                    self._stall += 1
                    if self._stall >= STALL_TRIES:
                        # 地形變了？（多半是被怪卡住）重播失敗 → 回現場探路
                        self._route, self._route_dead = None, True
                        self.note = "記住的路線走不通 → 改回現場探路"
                        return self.note
                mover.walk_route(scanner, player_obj, pt[0], pt[1],
                                 stop_short=0.0)
                self._sent = time.monotonic()
                self._go_now = False
                self.note = f"沿上次的路線走（{self._ri + 1}/{len(self._route)}）"
                return self.note

        # ── 掃描中：每一拍只試幾個，別把畫面凍住 ──────────────
        if self._scan is not None:
            for _ in range(SCAN_PER_STEP):
                if not self._scan or self._found is not None:
                    break
                # ⚠⚠ 先看再拿：`path_to` 的 -1 是「指令槽被攻擊執行緒佔著、
                #   這次沒問到」，**不是**「走不到」（那是 0）。以前直接 pop
                #   再比 > 0，等於把還沒問過的方向永久丟掉 —— 攻擊忙的時候
                #   繞路會少試好幾個方向，更容易誤判「四面八方都走不到」。
                #   farm_tab.tick 對同一個回傳值本來就有 `if n >= 0` 的正確處理。
                c = self._scan[0]
                n = mover.path_to(scanner, c[0], c[1])
                if n < 0:
                    break                            # 留著，下一拍再問
                self._scan.pop(0)
                if n > 0:
                    self._found = (_d(c, goal), c)   # 排序過 → 第一個就是最好的
            if self._scan and self._found is None:
                self.note = f"繞路計算中…（剩 {len(self._scan)} 個方向）"
                return self.note
            self._scan = None
            if self._found is None:
                # 全部走不到 —— 放掉最舊的記憶再試一輪，還是不行就認輸
                if self.visited:
                    del self.visited[0]
                    self.note = "繞路計算中…（放寬已走過的範圍）"
                    return self.note
                self.stuck = True
                self.note = "⛔ 四面八方都走不到"
                return self.note
            self.sub = self._found[1]
            self._best = _d(here, self.sub)
            self._stall = 0
            self._hops += 1
            # ★ 有沒有真的在接近目標？連續好幾段都沒有就認輸，別繞到天亮。
            gd = _d(here, goal)
            if self._closest is None or gd < self._closest - 1.0:
                self._closest, self._no_gain = gd, 0
            else:
                self._no_gain += 1
                if self._no_gain >= GIVE_UP_HOPS:
                    self.stuck = True
                    self.note = (f"⛔ 繞了 {self._hops} 段還是靠不近"
                                 f"（最近只到 {self._closest:.0f} 格）")
                    return self.note

        # ── 有中繼點：一路推到底，中途不重掃 ────────────────
        if self.sub is not None:
            d = _d(here, self.sub)
            if d <= NEAR_SUB:                       # 走到了
                self._visit(here)
                self.sub = None
                self._go_now = True     # 不等停下，下一拍就規劃下一段
                self.note = f"到第 {self._hops} 個轉折點"
                return self.note
            if d < self._best - 0.5:                # 有進展
                self._best, self._stall = d, 0
            else:
                self._stall += 1
                if self._stall >= STALL_TRIES:      # ④ 沒走到 → 黑名單
                    self._blacklist(self.sub)
                    self._visit(here)
                    self.sub = None
                    self.note = "這條走不通 → 換方向"
                    return self.note
            mover.walk_route(scanner, player_obj, self.sub[0], self.sub[1],
                             stop_short=0.0)
            self._sent = time.monotonic()
            self._go_now = False
            self.note = (f"繞路前往 ({self.sub[0]:.0f},{self.sub[1]:.0f})"
                         f"　第 {self._hops} 段")
            return self.note

        # ── 沒有中繼點：先問直達，不行才開始掃 ────────────────
        n = mover.path_to(scanner, gx, gy)
        if n < 0:
            # ⚠⚠ 同上：-1 是「這次沒問到」。以前會直接掉到下面當成
            #   「算不出直達路徑」→ 白啟動一整輪 96 個方向的繞路掃描
            #   （0.5 秒），而且把 here 記進 visited 縮小了之後的候選範圍。
            #   什麼都不做、下一拍再問才對。
            return self.note
        if n > 0:
            self.sub = goal
            self._best = _d(here, goal)
            self._stall = 0
            mover.walk_route(scanner, player_obj, gx, gy, stop_short=0.0)
            self._sent = time.monotonic()
            self._go_now = False
            self.note = "直達" if n == 1 else f"繞過地形（{n} 個轉折）"
            return self.note
        self._visit(here)
        self._start_scan(here, goal)
        self.note = "算不出直達路徑 → 找繞路"
        return self.note
