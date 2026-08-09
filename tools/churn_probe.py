"""換頻道／換地圖／長時間掛著時，**每一條讀取路徑**還讀得對嗎。

    py tools\\churn_probe.py            # 一直跑，Ctrl+C 收工
    py tools\\churn_probe.py --secs 60  # 跑 60 秒

跟 `tools/selfcheck.py` 的分工
------------------------------
`selfcheck` 是「現在這一拍讀得到嗎」的快照。這一支盯的是**時間軸**：
高頻連續採樣，把「換場景那零點幾秒」也拍進去，然後回答兩個不同層級的問題：

  ⚠ **讀不到**（安全退化）—— 記下來、算佔比，不算壞。
  ⛔ **讀錯**（安靜地做錯事）—— 這是本專案一律當 bug 的那種。

「讀錯」怎麼認得出來：**不靠人看數字，靠不變量**。

  · 金幣有兩條完全獨立的路（`bag.gold()` 走物品容器第 0 格、
    `player.read().gold` 走玩家屬性物件）—— 兩邊都讀到卻不一樣 = 有一邊錯。
  · 等級、最大 HP／MP、角色名字在測試期間**不會變**，變了就是讀到別人的。
  · 背包件數不會自己變（沒買賣），跳動就是讀到別份資料。
  · 玩家座標必須落在**目前這張地圖**的地形圖範圍內。
  · 場景編號要嘛不變、要嘛換成另一個合法編號，不能出現垃圾值。

分兩層採樣（跟產品實際的行為一致）
  快層（預設 0.1 秒）：純結構讀取，微秒～毫秒級 —— 掛機每一拍在做的就是這些。
  慢層（預設 5 秒）：要全記憶體掃描的（實體清單、玩家物件定位）。

⚠⚠ **慢層一定要跑在自己的執行緒上**（2026-08-09 第一版踩到）：換頻道重連時
  `entity.snapshot()`／`player.locate()` 找不到東西會一路全掃到底，同一條
  執行緒的快層就整整 6 秒沒有採樣 —— 那 6 秒正是要拍的重連空窗，
  結果報告漂亮地寫著「0 讀錯」，其實是**根本沒拍到**。
  快層因此也不准跟慢層要資料：玩家實體改走 `bag.player_entity()`
  （場景管理器 → 實體表，純結構、微秒級），不等全掃。

⚠ 預設**只讀不寫**。要讓它自己去踩那個坑（時序才對得準）：

    --switch 8,20,32     # 在第 8/20/32 秒各換一次頻道（全部分身一起）
    --jump 15            # 第 15 秒用天使趴趴GO換地圖，之後再跳回來

  只有帶了這些參數才會寫入／呼叫遊戲函式，其餘時間一律純讀。

輸出：整份逐拍紀錄寫 `reports/churn_probe_<時間>.jsonl`，
主控台只印異常與結尾摘要（省 token）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import charname, preload                       # noqa: E402
from app.core.memory import MemoryScanner                    # noqa: E402
from app.game import (bag, channel, energy, entity, inventory, locate,  # noqa: E402,E501
                      monsters, move, player, quickbar, robot, scene,
                      terrain)

FAST_GAP = 0.1
SLOW_GAP = 5.0
# 待驗證的捷徑：角色屬性基準 == [quickbar.MGR_PTR] + 這個。
# 2026-08-09 五台實測全中；這支每一拍都會拿它跟全掃的結果對帳。
STATS_OFF = 0xCB88


def _try(fn, *a, **kw):
    """回 (值, 錯誤字串)。例外一律當「讀不到」，不讓整支掛掉。"""
    try:
        return fn(*a, **kw), ""
    except Exception as exc:                                 # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


class Probe:
    """一台分身。`base`/`inv` 這類要定位的位址**故意快取**——
    產品就是這樣用的，位址失效偵測正是要測的東西。"""

    def __init__(self, w) -> None:
        self.pid, self.hwnd, self.title = w.pid, w.hwnd, w.title
        self.account = charname.account_from_title(w.title) or str(w.pid)
        self.sc = MemoryScanner()
        self.sc.open(w.pid)
        self.name = preload.name_of(w.pid, self.sc, self.account)
        self.base: int | None = None        # 玩家屬性物件（全掃定位，快取）
        self.inv: int | None = None         # 物品陣列表頭（AOB 定位，快取）
        self.state: int | None = None
        self.pobj: int | None = None
        self.maps = terrain.Cache()
        # 不變量的基準（第一次讀到就記起來）
        self.ref: dict = {}
        self.rows = 0
        self.miss: Counter = Counter()      # 每個欄位「讀不到」幾次
        self.bad: list[dict] = []           # ⛔ 讀錯（違反不變量）
        self.tear = 0                       # 已知暫態：上限暫時跟著現值跑
        # 產品用哪一套穩定血魔上限，這裡就用同一套（見 _check_fast）
        self._mhp = player.MaxTracker()
        self._mmp = player.MaxTracker()
        self.mover = None                   # 只有 --switch 才會裝
        self.n_ch: int | None = None        # 這台的分流數（全掃，只算一次）

    # -- 不變量 ------------------------------------------------------
    def _invariant(self, key: str, val, row: dict) -> None:
        """這個值第一次見到就記起來，之後變了就是讀錯。"""
        if val is None:
            return
        old = self.ref.get(key, _MISSING)
        if old is _MISSING:
            self.ref[key] = val
        elif old != val:
            self._flag(row, f"{key} 變了：{old!r} → {val!r}")

    def _flag(self, row: dict, why: str) -> None:
        self.bad.append({"t": row["t"], "who": self.name, "why": why,
                         "scene": row.get("scene"), "ch": row.get("ch")})
        print(f"  ⛔ {self.name}　{why}")

    # -- 快層 --------------------------------------------------------
    def fast(self, t: float) -> dict:
        row: dict = {"t": round(t, 3), "who": self.name, "kind": "fast"}
        sc = self.sc

        sc_now, err = _try(scene.current, sc, allow_scan=False)
        row["scene"] = sc_now.id if sc_now else None
        row["ch"] = channel.current(self.hwnd)

        head, e = _try(bag.head, sc)
        row["bag_head"] = head and [hex(head[0]), head[1]]
        row["bag_synced"], _ = _try(bag.synced, sc)
        gold_bag, _ = _try(bag.gold, sc)
        row["gold_bag"] = gold_bag
        items, e2 = _try(bag.scan, sc, 0, bag.MAX_SLOTS)
        if items is not None:
            row["bag_n"], row["bag_ok"] = len(items[0]), items[1]
        worn, _ = _try(bag.worn_broken, sc)
        row["worn_broken"] = None if worn is None else len(worn)

        # ★★★ 候選捷徑：角色屬性物件 == `[MGR_PTR] + STATS_OFF`（五台實測）。
        #   `player.locate()` 是全記憶體掃描（0.4~1 秒／台），所以換頻道之後
        #   等級／HP／MP 要好幾秒才讀得回來 —— 那段時間休息判斷是瞎的。
        #   這裡每一拍都算一次並跟慢層全掃的結果對帳，**連換頻道那幾拍一起驗**。
        mgr, _ = _try(bag._u32, sc, quickbar.MGR_PTR)
        vt, _ = _try(player.vtable_value, sc)
        fast_base = None
        if mgr and vt:
            cand = mgr + STATS_OFF
            if player._signature_ok(sc, cand + player.OFF_VTABLE, vt):
                fast_base = cand
        row["base_fast"] = fast_base and hex(fast_base)
        row["mgr"] = mgr and hex(mgr)

        # 玩家屬性：**用快取的 base**，讀不到才承認失效（產品的行為）；
        # 失效就當場用捷徑補（產品 2026-08-09 起也是這樣做）
        if not self.base:
            self.base = fast_base
        stats = player.read(sc, self.base) if self.base else None
        if stats is None and self.base:
            row["base_lost"] = True
            self.base = None
        if stats:
            row["lv"], row["hp"], row["mhp"] = stats.level, stats.hp, stats.max_hp
            row["mp"], row["mmp"] = stats.mp, stats.max_mp
            row["gold_pl"], row["exp"] = stats.gold, stats.exp

        row["pf"], _ = _try(move.pathfinder_this, sc)
        # ★ 玩家實體走**純結構**那條（場景管理器 → 實體表 → 驗 +0xBC == 我的 ID），
        #   微秒級、不必等全掃 —— 換地圖後這條立刻就有答案。
        # ⚠⚠⚠ **基準差 8 bytes，別混用**（2026-08-09 實測，我自己先踩了一次）：
        #     bag.player_entity() == move.pathfinder_this()
        #     entity.snapshot() 回的「玩家物件」 == 它 **+8**
        #   而 `+0xBC` 在兩個基準下是完全不同的東西：
        #     player_entity + 0xBC = 實體 ID（bag.OFF_ENT_ID，用來驗身分）
        #     玩家物件     + 0xBC = 座標 X（entity.OFF_POS_X）
        #   拿錯基準去讀座標**不會失敗**，會安靜地把實體 ID 當成座標
        #   （讀出來像 (1833.06, 0.0) 這種 y 恆為 0 的值）。
        ent, _ = _try(bag.player_entity, sc)
        row["ent"] = ent and hex(ent)
        pos = entity.read_pos(sc, ent + 8) if ent else None
        row["pos"] = pos and [round(pos[0], 2), round(pos[1], 2)]
        row["af"], _ = _try(robot.autofight_on, sc)
        slots, _ = _try(robot.potion_slots, None, sc, self.pid)
        row["potion_slots"] = len(slots or {})
        st = self.state
        es = energy.read(sc, st) if st else None
        row["energy"] = es.energy if es else None

        self._check_fast(row, gold_bag)
        return row

    def _check_fast(self, row: dict, gold_bag) -> None:
        # ⛔ 兩條獨立的路對同一個數字 —— 都讀到卻不一樣 = 一定有一邊錯
        gp = row.get("gold_pl")
        if gold_bag is not None and gp is not None and gold_bag != gp:
            self._flag(row, f"金幣兩條路不一致：容器 {gold_bag} vs 屬性 {gp}")
        # ⛔ 測試期間不該變的東西
        self._invariant("lv", row.get("lv"), row)
        # ★ 血魔上限要**先過產品同一套 `MaxTracker` 再檢查**：原始欄位在
        #   掉血／掉魔當下會暫時跟著現值往下跑（見 player.MaxTracker 的時間軸），
        #   那是已知暫態、單獨計數。這樣測到的就是使用者實際會用到的值。
        for k, tr in (("mhp", self._mhp), ("mmp", self._mmp)):
            v = row.get(k)
            if v is None:
                continue
            if v != self.ref.get(k + "_raw", v):
                self.tear += 1
            self.ref[k + "_raw"] = v
            row[k + "_stable"] = tr.value(v, row.get("lv"))
            self._invariant(k, row[k + "_stable"], row)
        # ⛔ 背包件數只在「整段真的讀到了」時才拿來比（沒買賣就不該變）
        if row.get("bag_ok"):
            self._invariant("bag_n", row.get("bag_n"), row)
        # ⛔ 座標要落在目前這張地圖的地形圖裡
        # ⚠ `Cache.get(scanner)` 拿的就是**目前這張**地圖的地形圖，所以
        #   「座標掉出範圍」同時抓得到兩種錯：座標讀錯、或地形圖沒跟著換地圖。
        pos, sid = row.get("pos"), row.get("scene")
        if pos and sid is not None:
            grid, _ = _try(self.maps.get, self.sc)
            if grid is not None:
                w, h = grid.w, grid.h
                row["map_wh"] = [w, h]
                if w and h and not (0 <= pos[0] < w and 0 <= pos[1] < h):
                    self._flag(row, f"座標 {pos} 掉出地圖 {sid} 的範圍 {w}x{h}")
        for k, v in row.items():
            if v is None and k not in ("scene", "pos", "energy"):
                self.miss[k] += 1
        self.rows += 1

    # -- 慢層 --------------------------------------------------------
    def slow(self, t: float) -> dict:
        row: dict = {"t": round(t, 3), "who": self.name, "kind": "slow"}
        sc = self.sc
        got, err = _try(entity.snapshot, sc)
        if got:
            st, pobj, ents, _f, _e = got
            self.state, self.pobj = st, pobj
            row["state"] = st and hex(st)
            row["player_obj"] = pobj and hex(pobj)
            row["mons"] = sum(1 for e in ents if e.is_monster)
        else:
            row["snapshot_err"] = err
        if self.base is None:
            self.base, _ = _try(player.locate, sc)
            row["relocated_base"] = bool(self.base)
        # ★ 產品現在是「位址一失效就用 locate_fast 當場補」（farm_tab 的 HP 檢查）
        #   —— 快層也照做，這樣測到的恢復時間才是使用者實際會遇到的。
        # ⛔ 捷徑對帳：全掃找到的基準 vs [MGR_PTR]+STATS_OFF 算出來的。
        #   不一致就是捷徑不能用（而不是「慢一點」）——直接標成讀錯。
        if self.base:
            mgr, _ = _try(bag._u32, sc, quickbar.MGR_PTR)
            row["base_slow"] = hex(self.base)
            if mgr:
                cand = mgr + STATS_OFF
                row["base_fast_chk"] = hex(cand)
                if cand != self.base:
                    self._flag(row, f"捷徑對不上：全掃 {self.base:#x} vs "
                                    f"[MGR]+{STATS_OFF:#x} {cand:#x}")
        if self.inv is None or not inventory.is_valid(sc, self.inv):
            row["relocated_inv"] = True
            self.inv = None
        row["scene"] = (scene.current(sc, allow_scan=False) or None)
        row["scene"] = row["scene"].id if row["scene"] else None
        row["ch"] = channel.current(self.hwnd)
        # 回程道具：走 inventory 那條（產品判斷「有沒有回程」用的就是它）
        if self.inv:
            tot, ok = inventory.count_by_types(sc, self.inv, [1905])
            row["wing"], row["wing_ok"] = tot.get(1905, 0), ok
        return row


    # -- 換頻道（唯一會寫入的動作，要 --switch 才會走到）---------------
    def switch_channel(self) -> str:
        """換到下一個分流。回傳一句話給主控台。"""
        from app.core import injector

        if self.mover is None:
            self.mover = move.acquire(self.pid,
                                      injector.process_path(self.pid), self)
        if not (self.mover and self.mover.active):
            return "跳板掛不上"
        # ⚠ `channel.count()` 是全記憶體掃描（0.3~1 秒／台）—— 只算一次。
        #   分流數不會中途改變，每次換頻都重算等於白掃五輪。
        if self.n_ch is None:
            self.n_ch, _ = _try(channel.count, self.sc, self.hwnd)
        n = self.n_ch
        cur = channel.current(self.hwnd)
        if not n or not cur:
            return f"查不到分流（目前 {cur}／共 {n}）"
        nxt = cur % n + 1
        ok = channel.switch(self.mover, nxt, n)
        # ⚠ 換頻道＝斷線重連，所有快取位址一定要當場作廢（產品也是這樣做的）
        self.base = self.inv = self.state = self.pobj = None
        return f"{cur} → {nxt} 頻　{'送出' if ok else '❌沒送出'}"

    def jump(self, jump_id: int) -> str:
        """天使趴趴GO 換地圖。"""
        from app.core import injector
        from app.game import jumpmap

        if self.mover is None:
            self.mover = move.acquire(self.pid,
                                      injector.process_path(self.pid), self)
        if not (self.mover and self.mover.active):
            return "跳板掛不上"
        ok, msg = jumpmap.teleport(self.mover, self.sc, jump_id)
        # 換地圖同樣會讓物件全部搬家
        self.base = self.inv = self.state = self.pobj = None
        return f"{'送出' if ok else '❌沒送出'}　{msg}"


_MISSING = object()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=0.0, help="0 = 一直跑")
    ap.add_argument("--fast", type=float, default=FAST_GAP)
    ap.add_argument("--slow", type=float, default=SLOW_GAP)
    ap.add_argument("--switch", default="",
                    help="在這幾秒各換一次頻道，例如 8,20,32")
    ap.add_argument("--jump", default="",
                    help="趴趴GO換地圖：`秒=跳點編號` 用逗號分隔，"
                         "例如 12=4,32=114（只動第一台，影響最小）")
    args = ap.parse_args()
    switch_at = [float(s) for s in args.switch.split(",") if s.strip()]
    jump_at = []
    for part in args.jump.split(","):
        if part.strip():
            sec, _, jid = part.partition("=")
            jump_at.append((float(sec), int(jid)))
    jump_at.sort()

    wins = preload.windows()
    if not wins:
        print("沒有開著的分身")
        return 1
    sc0 = MemoryScanner()
    sc0.open(wins[0].pid)
    locate.warm(sc0)
    sc0.close()

    probes = [Probe(w) for w in wins]
    os.makedirs("reports", exist_ok=True)
    path = os.path.join("reports",
                        f"churn_probe_{time.strftime('%m%d_%H%M%S')}.jsonl")
    print(f"盯著 {len(probes)} 台：" + "、".join(p.name for p in probes))
    print(f"逐拍紀錄 → {path}　（Ctrl+C 收工）\n")

    t0 = time.monotonic()
    stopped = False
    lock = threading.Lock()
    lines: list[str] = []

    def emit(row: dict) -> None:
        with lock:
            lines.append(json.dumps(row, ensure_ascii=False))

    def slow_loop() -> None:
        """慢層自己一條執行緒 —— 它卡住不能連累快層（見模組說明）。"""
        while not stopped:
            for p in probes:
                if stopped:
                    return
                emit(p.slow(time.monotonic() - t0))
            for _ in range(int(args.slow * 20)):
                if stopped:
                    return
                time.sleep(0.05)

    th = threading.Thread(target=slow_loop, daemon=True)
    th.start()
    with open(path, "w", encoding="utf-8") as fh:
        try:
            while not stopped:
                t = time.monotonic() - t0
                if args.secs and t >= args.secs:
                    break
                # 趴趴GO：**只動第一台** —— 換地圖比換頻道影響大，
                # 一台就足以拍到整個過程，其餘四台當對照組。
                while jump_at and t >= jump_at[0][0]:
                    _sec, jid = jump_at.pop(0)
                    p = probes[0]
                    print(f"\n--- t={t:.1f}s {p.name} 趴趴GO → 跳點 {jid} ---")
                    print(f"  {p.jump(jid)}\n")
                while switch_at and t >= switch_at[0]:
                    switch_at.pop(0)
                    print(f"\n--- t={t:.1f}s 換頻道 ---")
                    for p in probes:
                        print(f"  {p.name}：{p.switch_channel()}")
                    print()
                for p in probes:
                    emit(p.fast(t))
                with lock:
                    if lines:
                        fh.write("\n".join(lines) + "\n")
                        lines.clear()
                fh.flush()
                time.sleep(max(0.0, args.fast - (time.monotonic() - t0 - t)))
        except KeyboardInterrupt:
            pass
    stopped = True
    th.join(timeout=5.0)

    print("\n===== 摘要 =====")
    total_bad = 0
    for p in probes:
        total_bad += len(p.bad)
        top = "、".join(f"{k} {v}" for k, v in p.miss.most_common(6)) or "無"
        print(f"\n{p.name}　{p.rows} 拍　⛔讀錯 {len(p.bad)}"
              f"　（血魔上限暫態 {p.tear} 拍，產品端由 MaxTracker 擋住）")
        print(f"  讀不到（次數）：{top}")
    print(f"\n總計 ⛔ 讀錯 {total_bad} 筆。逐拍紀錄：{path}")
    for p in probes:
        p.sc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
