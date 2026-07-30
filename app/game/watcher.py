"""背景監看執行緒：把每個分身的角色屬性與技能經驗球讀成一份份快照。

分頁只負責畫，掃描與判斷都在這裡，所以換版面不會動到這段邏輯。

兩種資料、兩套定位方式
----------------------
* 角色屬性（等級 / HP / MP / 經驗 / 金幣）——玩家物件，用結構條件定位一次（約 1 秒），
  之後每輪用固定偏移一次讀齊，一次只花 0.1 毫秒。位址因換地圖 / 重連而失效時，
  `player.read()` 會回 None，這裡自動重新定位（帶冷卻，免得定位不到就連環全掃）。
* 技能經驗球——AOB 特徵（見 app/game/aob.py）。特徵會命中該角色所有的球、一些不再
  更新的舊副本、還有一堆根本不是球的物品，所以：
    1. 用種類 ID（球值位址 -0x98，見 app/game/items.py）辨識每個命中是什麼東西。
    2. 靠「誰在增加」挑出**裝備中**的那幾顆——飾品欄可同時裝多顆、階級還可以不同，
       練功時它們會一起漲；舊副本不會動，自然被排除。

貴的動作都是按需執行：玩家物件位址還有效就不重掃，球還在跳就證明位址是對的、也不重掃。
正常運作時這條執行緒幾乎都在睡，只是每 0.7 秒讀幾十個位址而已。

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
# 定位失敗（還在登入/選角畫面、或遊戲剛關）後，隔多久才重試全掃。
# 全掃約 1 秒，失敗時若不設冷卻就會變成無限全掃、把 CPU 燒滿。
RELOCATE_GAP_SECS = 5.0

BALL_SIG = aob.SKILL_EXP_BALL
# 特徵較鬆，上限開大以免漏掉正在練的那顆。
BALL_SCAN_LIMIT = 4096
# 完全沒有候選位址時的重掃間隔（例如還在讀取畫面）。
BALL_NO_CANDS_GAP = 5.0
# 有候選、但從沒看過任何一顆增加（沒在練功）→ 隔這麼久才重掃一次。
BALL_IDLE_GAP = 30.0
# 球「安靜」超過這麼久才考慮重掃（練功順利時每幾秒就跳，這條幾乎不會成立）。
BALL_QUIET_SECS = 10.0
# 安靜越久重掃越稀疏，但不超過這個間隔。
BALL_MAX_GAP = 60.0
# 一顆球多久沒動就不再算「裝備中」。
BALL_LIVE_SECS = 60.0
# 角色名解不到時，多久重試一次（要掃全記憶體，所以別太頻繁）。
NAME_RETRY_SECS = 120.0


class StatsWorker(QThread):
    """每輪對每台分身發出一份 (pid, PlayerStats|None, ball dict|None) 快照。

    ball dict 的內容：
        balls  [{addr, value, name, cap, pct, is_ball, type_id}, ...]
               有在增加就是裝備中的那幾顆；都沒在增加就給數值最大的那顆當參考。
        live   True = 上面那些正在增加
        count  候選位址數
        raw    特徵原始命中數（含非球物品）
        idle   沒在動的球，依「種類+值」去重後的 [(type_id, value), ...]
        quiet  距離上次有球增加過了幾秒；從沒動過是 None

    共享 dict（本執行緒寫、UI 執行緒讀）：self._names {pid: 角色名}
    """

    snapshot = Signal(int, object, object)

    def __init__(self, insts, names: dict) -> None:
        super().__init__()
        self._insts = insts            # [(pid, hwnd, title, sc), ...]
        self._names = names
        self._bases: dict[int, int] = {}
        self._last_try: dict[int, float] = {}
        self._name_try: dict[int, float] = {}    # {pid: 上次嘗試解角色名的時間}
        # 技能球狀態
        self._cands: dict[int, list[int]] = {}        # {pid: [候選位址]}
        self._types: dict[int, dict[int, int]] = {}   # {pid: {位址: 種類 ID}}
        self._raw_hits: dict[int, int] = {}           # {pid: 特徵原始命中數}
        self._prev: dict[int, dict[int, int]] = {}    # {pid: {位址: 上一輪的值}}
        self._live: dict[int, dict[int, float]] = {}  # {pid: {位址: 上次增加的時間}}
        self._ever: dict[int, set[int]] = {}          # {pid: 曾經增加過的位址}
        self._force: set[int] = set()                 # 偵測到換裝 → 下一輪立刻重掃
        self._inv: dict[int, int] = {}                # {pid: 物品陣列表頭}
        self._last_inc: dict[int, float] = {}         # {pid: 任一顆上次增加的時間}
        self._last_scan: dict[int, float] = {}        # {pid: 上次全掃特徵的時間}
        self._running = True

    # -- 角色名 --------------------------------------------------------
    def _resolve_name(self, pid: int, sc, title: str) -> None:
        """解角色名。解不到就過一陣子再試，不要記成永久失敗。

        名字是從記憶體裡的存檔路徑「RobotData_1_{帳號}_{角色名}.data」解出來的
        （見 app/core/charname.py）。**新角色還沒產生存檔檔案時就抓不到** ——
        使用者的弟弟那隻低等角色就是一直空白。存檔之後路徑就會出現，所以要重試；
        以前解不到會記成 "?" 而且永遠不再嘗試，等於卡死。

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

    # -- 技能球 --------------------------------------------------------
    def _needs_ball_scan(self, pid: int, now: float) -> bool:
        if pid in self._force:
            # 偵測到換裝 → 不等冷卻，立刻重掃，不然畫面會卡著已經拔掉的球
            self._force.discard(pid)
            return True
        since = now - self._last_scan.get(pid, 0.0)
        if not self._cands.get(pid):
            return since >= BALL_NO_CANDS_GAP   # 沒候選 → 沒東西可讀，積極找
        last_inc = self._last_inc.get(pid)
        if last_inc is None:
            # 有候選但從沒看過它動：可能沒在練功，也可能掃到的是舊副本 → 久久重掃當保險
            return since >= BALL_IDLE_GAP
        quiet = now - last_inc
        if quiet < BALL_QUIET_SECS:
            return False    # 球還在跳 → 位址是對的 → 完全不用掃（穩態走這條）
        # 安靜了。分不出是搬家（重掃救得回來）還是練完了（重掃也沒用），所以照掃，
        # 但間隔隨安靜時間拉長，避免練完的那台被連續全掃燒 CPU。
        return since >= max(BALL_QUIET_SECS, min(BALL_MAX_GAP, quiet / 2))

    def _describe(self, pid: int, sc, addr: int, type_id, value: int) -> dict:
        """把一個候選位址講成畫面要的樣子。

        只查 app/game/items.py 那張寫死的表 —— 不做任何執行時推測（原因見該檔開頭）。
        不在表裡就標成「非經驗球」，畫面不畫進度條也不顯示百分比。
        """
        bt = items.ball_type(type_id)
        # 保險：球的累積值不可能超過自己的上限。真的超過就代表表裡那筆對照有誤，
        # 這時只顯示數值、不顯示上限與百分比，也不要畫一條騙人的滿格進度條。
        if bt is not None and bt.cap and value > bt.cap:
            sys.stderr.write(
                f"[items] ⚠ ID {type_id} 的值 {value:,} 超過表中「{bt.name}」的"
                f"上限 {bt.cap:,} → 對照表有誤，暫不顯示上限\n")
            return {"addr": addr, "value": value, "type_id": type_id,
                    "name": bt.name, "cap": 0, "pct": None, "is_ball": True}
        return {"addr": addr, "value": value, "type_id": type_id,
                "name": bt.name if bt else items.UNKNOWN_LABEL,
                "cap": bt.cap if bt else 0,
                "pct": bt.pct(value) if bt else None,
                "is_ball": bt is not None}

    def _inventory_ball(self, pid: int, sc) -> dict | None:
        """精確版：直接讀物品陣列的飾品欄兩格，不做任何行為推論。

        表頭要先靠 AOB 找到的球結構反查一次（見 app/game/inventory.py），之後每輪
        只讀幾個指標。換飾品當下就會反映，球滿了也不會從清單消失。
        """
        head = self._inv.get(pid)
        if head and not inventory.is_valid(sc, head):
            head = None                 # 換地圖 / 重連 → 表搬家了
        if not head:
            structs = [a - inventory.ITEM_BALL_OFF
                       for a in (self._cands.get(pid) or [])
                       if items.is_ball(self._types.get(pid, {}).get(a))]
            if not structs:
                return None             # 還沒有任何球可以當錨點 → 交給舊路徑先找
            try:
                head = inventory.locate(sc, structs)
            except Exception:
                head = None
            if not head:
                return None
            self._inv[pid] = head

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
            entry = {"addr": ptr + inventory.ITEM_BALL_OFF, "value": val,
                     "type_id": tid, "slot": idx,
                     "side": inventory.slot_side(idx)}
            (equipped if is_acc else bag).append(entry)

        def described(e):
            # 走 _describe 才會套上「值超過上限就不顯示上限」那道保險
            d = self._describe(pid, sc, e["addr"], e["type_id"], e["value"])
            return {**d, "slot": e["slot"], "side": e["side"]}

        # 有沒有在增加只拿來決定顯示顏色，不再影響「哪幾顆是裝備中」
        prev = self._prev.get(pid, {})
        cur = {e["addr"]: e["value"] for e in equipped}
        now = time.monotonic()
        if any(v > prev.get(a, v) for a, v in cur.items()):
            self._last_inc[pid] = now
        self._prev[pid] = {**prev, **cur}
        last_inc = self._last_inc.get(pid)
        live = last_inc is not None and now - last_inc <= BALL_LIVE_SECS

        return {
            "balls": [described(e) for e in equipped],
            "live": live,
            "count": len(equipped) + len(bag),
            "raw": self._raw_hits.get(pid, 0),
            "idle": sorted((e["type_id"], e["value"]) for e in bag),
            "quiet": None if live else (
                (now - last_inc) if last_inc else None),
            "exact": True,
        }

    def _ball(self, pid: int, sc) -> dict | None:
        now = time.monotonic()
        if self._needs_ball_scan(pid, now):
            try:
                hits = aob.scan(sc, BALL_SIG, limit=BALL_SCAN_LIMIT,
                                should_stop=lambda: not self._running)
            except Exception:
                hits = None
            if not self._running:
                return None     # 中途被喊停 → hits 不完整，別拿去覆蓋候選清單
            if hits is not None:
                # 全部留著（不預先篩掉非球）—— 玩家可能裝了對照表裡沒有的東西，
                # 那也要看得到，只是畫面上會標成「非技能球」。
                self._cands[pid] = list(hits)
                self._types[pid] = {a: items.read_type_id(sc, a) for a in hits}
                self._raw_hits[pid] = len(hits)
            self._last_scan[pid] = time.monotonic()

        addrs = self._cands.get(pid) or []
        if not addrs:
            return None
        try:
            cur = {a: sc.read_value(a, BALL_SIG.vt) for a in addrs}
            # ★ 種類 ID 每輪都要重讀，不能只在掃描時讀一次。
            # 玩家換飾品時遊戲會把那塊記憶體挪去放別的物品，位址還在、值也還讀得到，
            # 但已經是另一顆球了。快取舊 ID 的話就會出現「35000 的球配到 120000 上限」
            # 這種張冠李戴。一顆球才多讀 4 bytes，成本可以忽略。
            types = {a: items.read_type_id(sc, a) for a in addrs}
        except Exception:
            return None

        prev = self._prev.get(pid, {})
        old_types = self._types.get(pid, {})
        self._types[pid] = types

        now = time.monotonic()
        live = self._live.setdefault(pid, {})
        ever = self._ever.setdefault(pid, set())

        # 換裝偵測：種類 ID 變了，或值變小（球只會往上加，變小代表這個位置
        # 已經換成別的東西了）。這些位址的歷史立刻作廢，並要求馬上重掃特徵，
        # 免得畫面卡著舊球、或多出根本已經拔掉的那幾顆。
        swapped = {
            a for a in addrs
            if (a in old_types and types.get(a) != old_types[a])
            or (prev.get(a) is not None and cur.get(a) is not None
                and cur[a] < prev[a])
        }
        if swapped:
            # 整台的「裝備中」記錄全部作廢重認，不是只丟掉變動的那幾個位址：
            # 換裝時被拔掉的那顆常常會以「值沒變的舊副本」留在記憶體裡，光看它自己
            # 看不出有變化，只有整組重認才不會在畫面上留下已經拔掉的殘影。
            live.clear()
            ever.clear()
            for a in swapped:
                prev.pop(a, None)
            self._force.add(pid)

        increased = [a for a, v in cur.items()
                     if a not in swapped and v is not None
                     and prev.get(a) is not None and v > prev[a]]
        self._prev[pid] = cur

        for a in list(live):
            if a not in cur:
                live.pop(a)     # 重掃後不見了的位址（搬家）→ 丟掉
        ever &= set(cur)
        for a in increased:
            live[a] = now
            ever.add(a)
        if increased:
            self._last_inc[pid] = now

        known = [a for a in cur
                 if cur[a] is not None and items.is_ball(types.get(a))]

        def payload(shown: list[int], is_live: bool) -> dict:
            sh = set(shown)
            return {
                "balls": [self._describe(pid, sc, a, types.get(a), cur[a])
                          for a in shown],
                "live": is_live,
                "count": len(addrs),
                "raw": self._raw_hits.get(pid, 0),
                # 「其他球」只算確定是球的，不然會混進一堆別的物品
                "idle": sorted({(types.get(a), cur[a])
                                for a in known if a not in sh}),
                "quiet": None if is_live else (
                    (now - self._last_inc[pid]) if pid in self._last_inc else None),
            }

        # 顯示「這輪期間曾經增加過」的所有球 —— 那就是裝備中的那幾顆。
        # 不能只留「最近 N 秒有增加」的：**球一滿就不會再動**，那樣它會從清單裡消失，
        # 害呼叫端的「球滿」判定忽有忽無、重複發通知。
        shown = [a for a in ever if cur.get(a) is not None]
        if shown:
            shown.sort(key=lambda a: cur[a], reverse=True)
            recent = any(now - live.get(a, 0.0) <= BALL_LIVE_SECS for a in shown)
            return payload(shown, recent)

        # 從沒看過任何一顆增加（沒在練功 / 剛接上）→ 給最大的那顆當參考。
        # 優先從「確定是球」的裡面挑，免得拿一個不相干的物品來充數。
        pool = known or [a for a in cur if cur[a] is not None]
        if not pool:
            return None
        return payload([max(pool, key=lambda a: cur[a])], False)

    # -- 主迴圈 --------------------------------------------------------
    def run(self) -> None:
        while self._running:
            for pid, _hwnd, title, sc in self._insts:
                if not self._running:
                    return
                self._resolve_name(pid, sc, title)
                st = self._stats(pid, sc)
                # 先走精確路徑（直接讀飾品欄兩格）。定位到物品陣列之後就完全不必再
                # 掃 AOB 特徵了 —— 那是每台 1～2.5 秒的全記憶體掃，能省則省。
                # 還沒定位到（剛啟動、換地圖搬家）才退回舊的「靠增加推論」，
                # 那條同時負責用 AOB 找出定位所需的錨點。
                ball = self._inventory_ball(pid, sc)
                if ball is None:
                    ball = self._ball(pid, sc)
                if not self._running:
                    return
                self.snapshot.emit(pid, st, ball)
            self.msleep(READ_INTERVAL_MS)

    def stop(self) -> None:
        self._running = False
