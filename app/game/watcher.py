"""背景監看執行緒：把每個分身的角色屬性與經驗球讀成一份份快照。

分頁只負責畫，掃描與判斷都在這裡，所以換版面不會動到這段邏輯。

兩種資料、兩套定位方式，兩套都是「結構性」的，不靠數值也不靠行為推論
--------------------------------------------------------------------
* 角色屬性（等級 / HP / MP / 經驗 / 金幣）—— 玩家物件，認 vtable 特徵定位一次
  （見 app/game/player.py），之後每輪用固定偏移一次讀齊，一次只花 0.1 毫秒。
  位址因換地圖 / 重連而失效時 `player.read()` 會回 None，這裡自動重新定位。

* 經驗球 —— 直接讀**物品指標陣列的飾品欄兩格**（見 app/game/inventory.py）。
  陣列表頭要先用 AOB 特徵找到任一個物品當錨點反查一次，之後每輪把整條陣列
  批次走一遍（~1ms）。★ 格號認**物品自己記的 +0x25**，不信陣列索引 ——
  表頭在少數機台會偏 5～6 格，拿索引讀會把裝備中的球誤判成放在背包。

★ 為什麼沒有「一顆球多久沒動就不算裝備中」這種東西 ★
-----------------------------------------------------
以前不知道飾品欄在哪，只能靠「練功時球會增加」去猜哪幾顆是裝備中的，於是衍生出
一整套機制：記錄誰增加過、多久沒動就踢出清單、換裝時清掉殘影、值變小就重掃…
那些全都是「猜」的補丁，而且會出錯（球滿了就不動、剛裝上還沒漲、記憶體有舊副本）。

**現在飾品欄那兩格是直接讀出來的，裝什麼就是什麼**，所以那套機制整個移除了。
「有沒有在增加」只剩一個用途：決定畫面顏色（綠＝正在練功），不影響任何判定。

★ 為什麼分成兩條執行緒 ★
------------------------
定位要掃全記憶體：解角色名 1.1 秒、AOB 0.85 秒、物品陣列反查 0.9 秒。以前這些跟
「每輪讀值」在同一條迴圈裡，於是開場那二十秒會出現：

    5.9s 解角色名 1161ms ┐
    6.8s AOB      830ms ├─ 同一輪，整整卡住 2.7 秒
    7.5s 反查      708ms ┘

期間**所有**分身的畫面都停著不動，實測更新間隔平均 1.06 秒、最久 4.3 秒（設計值
0.7 秒），而且還沒定位好的分身球區是空的——看起來就是「每隔幾秒閃一下、卡卡的」。

現在拆成：
* **讀值執行緒**（StatsWorker 本身）只做便宜的讀取，穩定每 0.7 秒發一次快照，
  永遠不會被掃描卡住。
* **定位執行緒**（_Locator）專門跑那些全掃，找到什麼就更新共用的位址表。
  順序是「先玩家物件（數值最有感）→ 再物品陣列 → 最後角色名」，
  因為名字沒解出來時畫面本來就會先用帳號頂著。

全程只讀取記憶體、不寫入、不注入、不掛除錯器（安全）。
"""
from __future__ import annotations

import sys
import time

from PySide6.QtCore import QThread, Signal

from app.core import charname
from app.game import aob, inventory, items, player

# 讀值間隔（毫秒）：決定畫面多快更新。讀一次只要 0.1ms，這個頻率完全不佔資源。
READ_INTERVAL_MS = 700
# 玩家物件定位失敗（還在登入/選角畫面、或遊戲剛關）後，隔多久才重試全掃。
RELOCATE_GAP_SECS = 5.0
# 物品陣列定位失敗後隔多久重試。要跑一次 AOB 全掃（約 1～2.5 秒），別太頻繁。
INV_RELOCATE_GAP_SECS = 8.0
# 多久內有增加就算「正在練功」（只影響顏色，不影響任何判定）。
BALL_ACTIVE_SECS = 60.0
# 定位執行緒沒事做時的休息間隔（全部定位完就進入這個狀態）。
LOCATE_IDLE_MS = 500

BALL_SIG = aob.SKILL_EXP_BALL
BALL_SCAN_LIMIT = 4096
# 角色名解不到時，多久重試一次（要掃全記憶體，所以別太頻繁）。
NAME_RETRY_SECS = 120.0


class StatsWorker(QThread):
    """每輪對每台分身發出一份 (pid, PlayerStats|None, ball dict|None) 快照。

    ball dict 的內容：
        balls  飾品欄兩格的內容 [{addr, value, name, cap, pct, is_ball,
               type_id, slot, side}, ...]；空格不會出現在清單裡
        live   True = 最近有球在增加（畫面用來上綠色）
        idle   飾品欄以外、物品欄裡的球 [(type_id, value), ...]
        quiet  距離上次有球增加過了幾秒；從沒動過是 None

    共享 dict（本執行緒寫、UI 執行緒讀）：self._names {pid: 角色名}
    """

    snapshot = Signal(int, object, object)

    def __init__(self, insts, names: dict) -> None:
        super().__init__()
        self._insts = insts            # [(pid, hwnd, title, sc), ...]
        self._names = names
        self._bases: dict[int, int] = {}         # {pid: 玩家物件基準}
        self._last_try: dict[int, float] = {}    # {pid: 上次重新定位玩家物件的時間}
        self._name_try: dict[int, float] = {}    # {pid: 上次嘗試解角色名的時間}
        self._inv: dict[int, int] = {}           # {pid: 物品陣列表頭}
        self._inv_try: dict[int, float] = {}     # {pid: 上次重新定位物品陣列的時間}
        self._prev: dict[int, dict[int, int]] = {}   # {pid: {球位址: 上輪的值}}
        self._last_inc: dict[int, float] = {}    # {pid: 上次有球增加的時間}
        self._running = True

    # ==================================================================
    # 以下三個 _ensure_* 只在「定位執行緒」裡跑（每個都要掃全記憶體）。
    # 回傳 True = 這次真的花力氣掃了，定位執行緒用它判斷還要不要繼續忙。
    # ==================================================================
    def _ensure_name(self, pid: int, sc, title: str) -> bool:
        """解角色名。解不到就過一陣子再試，不要記成永久失敗。

        名字是從記憶體裡的存檔路徑「RobotData_1_{帳號}_{角色名}.data」解出來的
        （見 app/core/charname.py）。**新角色還沒產生存檔檔案時就抓不到**，
        存檔之後路徑才會出現，所以要重試；記成永久失敗會讓那一格一直空白。
        """
        if self._names.get(pid) not in (None, "?"):
            return False
        now = time.monotonic()
        if now - self._name_try.get(pid, 0.0) < NAME_RETRY_SECS:
            return False
        self._name_try[pid] = now
        try:
            got = charname.read_character_name(
                sc, charname.account_from_title(title))
        except Exception:
            got = None
        self._names[pid] = got or "?"
        return True

    def _ensure_base(self, pid: int, sc) -> bool:
        """玩家物件還沒定位（或剛失效）就重新找。"""
        if self._bases.get(pid):
            return False
        now = time.monotonic()
        if now - self._last_try.get(pid, 0.0) < RELOCATE_GAP_SECS:
            return False
        self._last_try[pid] = now
        try:
            base = player.locate(sc, should_stop=lambda: not self._running) or 0
        except Exception:
            base = 0
        self._bases[pid] = base
        # 玩家物件搬家 = 換地圖 / 重登，物品陣列一定也跟著搬。舊位址上可能還留著
        # 讀得到的殘影（讀起來像有效的表），所以這裡主動作廢、重新定位。
        if base:
            self._inv.pop(pid, None)
            self._prev.pop(pid, None)
        return True

    def _ensure_inventory(self, pid: int, sc) -> bool:
        """定位物品陣列表頭。AOB 特徵在這裡的唯一用途就是提供一個錨點物品。"""
        if self._inv.get(pid):
            return False
        now = time.monotonic()
        if now - self._inv_try.get(pid, 0.0) < INV_RELOCATE_GAP_SECS:
            return False
        self._inv_try[pid] = now
        try:
            hits = aob.scan(sc, BALL_SIG, limit=BALL_SCAN_LIMIT,
                            should_stop=lambda: not self._running)
        except Exception:
            return True
        if not self._running or not hits:
            return True
        # 不篩種類 ID —— 只是要一個「在陣列裡的物品」當錨點，是不是已知的球無所謂
        structs = {a - inventory.ITEM_BALL_OFF for a in hits}
        try:
            head = inventory.locate(sc, structs)
        except Exception:
            head = None
        if head:
            self._inv[pid] = head
        return True

    # ==================================================================
    # 以下是「讀值執行緒」的工作：只讀已知位址，不做任何全掃，成本 ~2ms/輪。
    # ==================================================================
    def _stats(self, pid: int, sc):
        """讀角色屬性。位址失效就作廢，交給定位執行緒重找（本輪先回 None）。"""
        base = self._bases.get(pid, 0)
        if not base:
            return None
        try:
            st = player.read(sc, base)
        except Exception:
            st = None
        if st is None:
            self._bases[pid] = 0        # 換地圖 / 重連 → 請定位執行緒重找
        return st

    def _describe(self, type_id, value: int) -> dict:
        """種類 ID + 數值 → 畫面要的樣子。只查寫死的表，不做任何推測。"""
        bt = items.ball_type(type_id)
        # 保險：球的累積值不可能超過自己的上限。真的超過就代表表裡那筆對照有誤，
        # 這時只顯示數值、不顯示上限與百分比，也不要畫一條騙人的滿格進度條。
        if bt is not None and bt.cap and value > bt.cap:
            if sys.stderr:   # 打包版 stderr 是 None，寫了整個球面板會直接讀失敗
                sys.stderr.write(
                    f"[items] ⚠ ID {type_id} 的值 {value:,} 超過表中「{bt.name}」的"
                    f"上限 {bt.cap:,} → 對照表有誤，暫不顯示上限\n")
            return {"value": value, "type_id": type_id, "name": bt.name,
                    "cap": 0, "pct": None, "is_ball": True}
        return {"value": value, "type_id": type_id,
                "name": bt.name if bt else items.UNKNOWN_LABEL,
                "cap": bt.cap if bt else 0,
                "pct": bt.pct(value) if bt else None,
                "is_ball": bt is not None}

    def _ball(self, pid: int, sc) -> dict | None:
        """讀飾品欄兩格與背包。還沒定位到物品陣列就回 None（不顯示，也不猜）。"""
        head = self._inv.get(pid)
        if head and not inventory.is_valid(sc, head):
            self._inv.pop(pid, None)    # 換地圖 / 重連 → 陣列搬家了
            head = None
        if not head:
            return None

        now = time.monotonic()
        # 一趟走完整條陣列，格號**認物品自己記的 +0x25**，不信陣列索引：
        # locate() 挑的表頭在少數機台會偏 5～6 格（inventory 模組說明第 4 點），
        # 拿索引讀第 8、9 格會把裝備中的球讀丟、反把它數進背包 ——
        # 使用者實機遇過「明明裝備著技能球，卻偵測成放在背包」。
        # _walk() 每輪先用 align_head() 校正表頭再逐件認格號，兩份欄位交叉印證；
        # 每輪都做（成本 ~1ms），一降頻反而是畫面每隔幾秒跳一次，看起來像在閃。
        #
        # 飾品欄兩格「一定顯示」，就算對照表裡沒有那個種類 ID：使用者要看到
        # 「飾品欄左裝了個我不認識的東西」，而不是那一格憑空消失。
        # 背包只算確定是球的，否則會把所有雜物都數進去。
        # ⚠ 不做「同格號去重」：_walk() 會連表頭前 24 格一起看（截斷防護），
        #   萬一外圍殘留指標也自稱第 8/9 格，挑哪個都可能安靜選錯 ——
        #   寧可兩筆都顯示（看得見的怪），也不要挑錯（看不見的錯）。
        equipped, bag = [], []
        try:
            for slot, tid, _cnt, p in inventory._walk(sc, head):
                if slot in inventory.SLOT_ACCESSORY:
                    val = inventory.ball_value(sc, p)
                    equipped.append({**self._describe(tid, val),
                                     "addr": p + inventory.ITEM_BALL_OFF,
                                     "slot": slot,
                                     "side": inventory.slot_side(slot)})
                elif items.is_ball(tid):
                    bag.append((tid, inventory.ball_value(sc, p)))
        except Exception:
            return None
        equipped.sort(key=lambda e: e["slot"])
        bag.sort()

        # 「有沒有在增加」只拿來決定顏色（綠＝正在練功），不影響任何判定
        cur = {e["addr"]: e["value"] for e in equipped}
        prev = self._prev.get(pid, {})
        if any(v > prev.get(a, v) for a, v in cur.items()):
            self._last_inc[pid] = now
        self._prev[pid] = cur
        last_inc = self._last_inc.get(pid)
        live = last_inc is not None and now - last_inc <= BALL_ACTIVE_SECS

        return {
            "balls": equipped,
            "live": live,
            "idle": bag,
            "quiet": None if live else (
                (now - last_inc) if last_inc else None),
        }

    # -- 讀值主迴圈（永遠不做全掃，所以節奏是穩的）----------------------
    def run(self) -> None:
        locator = _Locator(self)
        locator.start()
        try:
            while self._running:
                for pid, _hwnd, _title, sc in self._insts:
                    if not self._running:
                        return
                    st = self._stats(pid, sc)
                    ball = self._ball(pid, sc)
                    if not self._running:
                        return
                    self.snapshot.emit(pid, st, ball)
                self.msleep(READ_INTERVAL_MS)
        finally:
            # 定位執行緒掃到一半時，should_stop 會讓它提早收手；
            # 一定要等它真的結束，否則它還在用即將被關掉的 handle。
            self._running = False
            locator.wait(15000)

    def stop(self) -> None:
        self._running = False


class _Locator(QThread):
    """專跑全記憶體掃描的執行緒：找位址，不碰畫面。

    跟讀值執行緒共用 StatsWorker 上那幾個 dict（一邊寫、一邊讀）。dict 的單筆
    存取在 CPython 裡是不可分割的，而且兩邊都只認「有值 / 沒值」，不會讀到寫一半
    的狀態，所以不需要鎖。

    順序刻意排成「玩家物件 → 物品陣列 → 角色名」：等級 / 經驗 / 金幣最有感，
    名字最不急（畫面在解出來之前會先用帳號頂著）。
    """

    def __init__(self, worker: "StatsWorker") -> None:
        super().__init__()
        self._w = worker

    def run(self) -> None:
        w = self._w
        while w._running:
            worked = False
            for step in (w._ensure_base, w._ensure_inventory):
                for pid, _hwnd, _title, sc in w._insts:
                    if not w._running:
                        return
                    try:
                        worked |= step(pid, sc)
                    except Exception:
                        pass
            for pid, _hwnd, title, sc in w._insts:
                if not w._running:
                    return
                try:
                    worked |= w._ensure_name(pid, sc, title)
                except Exception:
                    pass
            if not worked:          # 全部定位完了就別空轉燒 CPU
                self.msleep(LOCATE_IDLE_MS)
