"""背景監看執行緒：把每個分身的角色屬性與經驗球讀成一份份快照。

分頁只負責畫，掃描與判斷都在這裡，所以換版面不會動到這段邏輯。

兩種資料、兩套定位方式，兩套都是「結構性」的，不靠數值也不靠行為推論
--------------------------------------------------------------------
* 角色屬性（等級 / HP / MP / 經驗 / 金幣）—— 玩家物件，認 vtable 特徵定位一次
  （見 app/game/player.py），之後每輪用固定偏移一次讀齊，一次只花 0.1 毫秒。
  位址因換地圖 / 重連而失效時 `player.read()` 會回 None，這裡自動重新定位。

* 經驗球 —— 直接讀**物品指標陣列的飾品欄兩格**（見 app/game/inventory.py）。
  陣列表頭要先用 AOB 特徵找到任一個物品當錨點反查一次，之後每輪只讀幾個指標。

★ 為什麼沒有「一顆球多久沒動就不算裝備中」這種東西 ★
-----------------------------------------------------
以前不知道飾品欄在哪，只能靠「練功時球會增加」去猜哪幾顆是裝備中的，於是衍生出
一整套機制：記錄誰增加過、多久沒動就踢出清單、換裝時清掉殘影、值變小就重掃…
那些全都是「猜」的補丁，而且會出錯（球滿了就不動、剛裝上還沒漲、記憶體有舊副本）。

**現在飾品欄那兩格是直接讀出來的，裝什麼就是什麼**，所以那套機制整個移除了。
「有沒有在增加」只剩一個用途：決定畫面顏色（綠＝正在練功），不影響任何判定。

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

    # -- 角色名 --------------------------------------------------------
    def _resolve_name(self, pid: int, sc, title: str) -> None:
        """解角色名。解不到就過一陣子再試，不要記成永久失敗。

        名字是從記憶體裡的存檔路徑「RobotData_1_{帳號}_{角色名}.data」解出來的
        （見 app/core/charname.py）。**新角色還沒產生存檔檔案時就抓不到**，
        存檔之後路徑才會出現，所以要重試；記成永久失敗會讓那一格一直空白。

        這個查詢要掃全部記憶體，所以只在「還沒解到」時每隔一段時間重試一次。
        """
        if self._names.get(pid) not in (None, "?"):
            return
        now = time.monotonic()
        if now - self._name_try.get(pid, 0.0) < NAME_RETRY_SECS:
            return
        self._name_try[pid] = now
        try:
            got = charname.read_character_name(
                sc, charname.account_from_title(title))
        except Exception:
            got = None
        self._names[pid] = got or "?"

    # -- 玩家物件 ------------------------------------------------------
    def _stats(self, pid: int, sc):
        try:
            st = player.read(sc, self._bases.get(pid, 0))
        except Exception:
            st = None
        if st is not None:
            return st
        now = time.monotonic()
        if now - self._last_try.get(pid, 0.0) < RELOCATE_GAP_SECS:
            return None
        self._last_try[pid] = now
        try:
            base = player.locate(sc, should_stop=lambda: not self._running) or 0
        except Exception:
            base = 0
        self._bases[pid] = base
        try:
            return player.read(sc, base) if base else None
        except Exception:
            return None

    # -- 經驗球 --------------------------------------------------------
    def _locate_inventory(self, pid: int, sc) -> int | None:
        """定位物品陣列表頭。AOB 特徵在這裡的唯一用途就是提供一個錨點物品。

        帶冷卻：AOB 是全記憶體掃（一台 1～2.5 秒），定位不到時不能每輪都掃。
        """
        now = time.monotonic()
        if now - self._inv_try.get(pid, 0.0) < INV_RELOCATE_GAP_SECS:
            return None
        self._inv_try[pid] = now
        try:
            hits = aob.scan(sc, BALL_SIG, limit=BALL_SCAN_LIMIT,
                            should_stop=lambda: not self._running)
        except Exception:
            return None
        if not self._running or not hits:
            return None
        # 不篩種類 ID —— 只是要一個「在陣列裡的物品」當錨點，是不是已知的球無所謂
        structs = {a - inventory.ITEM_BALL_OFF for a in hits}
        try:
            head = inventory.locate(sc, structs)
        except Exception:
            return None
        if head:
            self._inv[pid] = head
        return head

    def _describe(self, type_id, value: int) -> dict:
        """種類 ID + 數值 → 畫面要的樣子。只查寫死的表，不做任何推測。"""
        bt = items.ball_type(type_id)
        # 保險：球的累積值不可能超過自己的上限。真的超過就代表表裡那筆對照有誤，
        # 這時只顯示數值、不顯示上限與百分比，也不要畫一條騙人的滿格進度條。
        if bt is not None and bt.cap and value > bt.cap:
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
        """讀飾品欄兩格。定位不到物品陣列就回 None（不顯示，也不猜）。"""
        head = self._inv.get(pid)
        if head and not inventory.is_valid(sc, head):
            head = None                 # 換地圖 / 重連 → 陣列搬家了
            self._inv.pop(pid, None)
        if not head:
            head = self._locate_inventory(pid, sc)
        if not head:
            return None

        try:
            slots = inventory.scan_slots(sc, head)
        except Exception:
            return None

        equipped, bag = [], []
        for idx, tid, ptr, val in slots:
            is_acc = idx in inventory.SLOT_ACCESSORY
            # 飾品欄那兩格「一定顯示」，就算對照表裡沒有那個種類 ID ——
            # 使用者要看到「飾品欄右裝了個我不認識的東西」，而不是那一格憑空消失。
            # 背包則只算確定是球的，否則會把所有雜物都數進去。
            if not is_acc and not items.is_ball(tid):
                continue
            entry = {**self._describe(tid, val),
                     "addr": ptr + inventory.ITEM_BALL_OFF,
                     "slot": idx, "side": inventory.slot_side(idx)}
            (equipped if is_acc else bag).append(entry)

        # 「有沒有在增加」只拿來決定顏色（綠＝正在練功），不影響任何判定
        now = time.monotonic()
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
            "idle": sorted((e["type_id"], e["value"]) for e in bag),
            "quiet": None if live else (
                (now - last_inc) if last_inc else None),
        }

    # -- 主迴圈 --------------------------------------------------------
    def run(self) -> None:
        while self._running:
            for pid, _hwnd, title, sc in self._insts:
                if not self._running:
                    return
                self._resolve_name(pid, sc, title)
                st = self._stats(pid, sc)
                ball = self._ball(pid, sc)
                if not self._running:
                    return
                self.snapshot.emit(pid, st, ball)
            self.msleep(READ_INTERVAL_MS)

    def stop(self) -> None:
        self._running = False
