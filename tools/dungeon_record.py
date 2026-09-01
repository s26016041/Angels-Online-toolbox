"""副本錄影：使用者手打一趟，把「怎麼打」全部記下來（**純讀，不寫記憶體**）。

    py tools\\dungeon_record.py [名字片段]        # 開始錄，Ctrl+C 或建停止檔收工
    py tools\\dungeon_record.py --stop            # 從另一個視窗叫它收工

要回答的問題（2026-09-01，設計自動刷副本用）
---------------------------------------------------------------------------
吞噬之間 1（場景 76）的地形圖泛洪出來是 **6 塊互不相通的房間**，
表示房間之間只能靠傳送點；而傳送點**不在地形圖的格子資料裡**
（7 bytes 全倒過：byte0 地表材質、byte2 阻擋、其餘全 0）。
唯一還沒排除的候選是「獨立物件」（`scenery.nearby()`，跟採集樹同一條路）。

所以這支要抓的是：
  ① **傳送點**：座標一拍之內跳超過 JUMP 格、而場景編號沒變 → 記下 (從哪→到哪)，
     連同跳之前站的那格附近有哪些獨立物件（model 編號）。
  ② **刷怪點位**：每隻怪第一次被看到的位置、種類、是不是王（讀怪物範本表）。
  ③ **殺怪點位**：怪的動畫狀態轉成 'Dead' 的位置，以及當下玩家站哪。
  ④ **玩家走過哪些房間**：每一拍換算成連通區編號，收工時列出覆蓋率。
  ⑤ **停留熱點**：站著不動超過 STILL_SECS 的地方（＝清怪點／等王點）。

輸出：逐拍 `reports/dungeon_run_<時間>.jsonl`；主控台只印事件與收工摘要。
⚠ 全程純讀：不呼叫遊戲函式、不寫任何記憶體。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import charname, preload                      # noqa: E402
from app.core.memory import MemoryScanner                   # noqa: E402
from app.game import (bag, entity, locate, monsters, scene,  # noqa: E402
                      scenery, terrain)

GAP = 0.1                # 快拍間隔（座標／場景）
SLOW_GAP = 0.5           # 慢拍間隔（掃實體／獨立物件）
JUMP = 5.0               # 一拍位移超過幾格算「被傳送」
STILL_EPS = 1.5          # 站在原地的容忍半徑（格）
STILL_SECS = 3.0         # 站多久算一個停留點

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
STOP_FILE = os.path.join(REPORTS, ".stop_dungeon_record")


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Recorder:
    def __init__(self, sc, name: str, out) -> None:
        self.sc, self.name, self.out = sc, name, out
        self.t0 = time.monotonic()
        self.scene_id: int | None = None
        self.grid = None
        self.blob_of: dict[tuple[int, int], int] = {}
        self.blob_size: dict[int, int] = {}
        self.prev_pos: tuple[float, float] | None = None
        self.seen: dict[int, dict] = {}       # eid -> 第一次看到的資料
        self.dead_at: dict[int, dict] = {}    # eid -> 死掉的資料
        self.props: dict[int, dict] = {}      # oid -> 獨立物件
        self.teleports: list[dict] = []
        self.stops: list[dict] = []
        self.visited: Counter = Counter()     # 連通區編號 -> 拍數
        self.tiles: set[tuple[int, int]] = set()
        self._still_from = 0.0
        self._still_at: tuple[float, float] | None = None
        self._still_logged = False
        self.n_ticks = 0

    # -- 事件 ---------------------------------------------------------
    def ev(self, kind: str, **kw) -> None:
        row = {"t": round(time.monotonic() - self.t0, 2), "ev": kind, **kw}
        self.out.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.out.flush()
        bits = " ".join(f"{k}={v}" for k, v in kw.items())
        print(f"[{row['t']:>7.2f}] {kind:<9} {bits}", flush=True)

    def row(self, **kw) -> None:
        kw["t"] = round(time.monotonic() - self.t0, 2)
        self.out.write(json.dumps(kw, ensure_ascii=False) + "\n")

    # -- 地圖（換場景就重算連通區）------------------------------------
    def load_map(self, sid: int | None) -> None:
        self.scene_id = sid
        self.grid, why = terrain.load(self.sc)
        self.blob_of, self.blob_size = {}, {}
        if self.grid is None:
            self.ev("MAP", scene=sid, err=why)
            return
        g = self.grid
        seen: set = set()
        n = 0
        for y in range(g.h):
            r = g.open[y]
            for x in range(g.w):
                if r[x] and (x, y) not in seen:
                    comp = g.reachable(x, y) or set()
                    seen |= comp
                    if len(comp) >= 20:      # 碎片不編號
                        for c in comp:
                            self.blob_of[c] = n
                        self.blob_size[n] = len(comp)
                        n += 1
        self.ev("MAP", scene=sid, name=scene.scene_name(sid),
                size=f"{g.w}x{g.h}", rooms=n,
                sizes=sorted(self.blob_size.values(), reverse=True))

    def blob(self, pos) -> int | None:
        return self.blob_of.get((int(pos[0]), int(pos[1])))

    # -- 快拍 ---------------------------------------------------------
    def fast(self) -> None:
        sc = self.sc
        try:
            s = scene.current(sc, allow_scan=False)
            sid = s.id if s else None
        except Exception:
            sid = self.scene_id
        if sid != self.scene_id:
            old = self.scene_id
            self.load_map(sid)
            self.ev("SCENE", frm=old, to=sid, name=scene.scene_name(sid))
            self.prev_pos = None

        ent = bag.player_entity(sc)
        if not ent:
            return
        pos = entity.read_pos(sc, ent + 8)
        if not pos:
            return
        self.n_ticks += 1
        self.tiles.add((int(pos[0]), int(pos[1])))
        b = self.blob(pos)
        if b is not None:
            self.visited[b] += 1

        # ① 傳送：一拍跳很遠、場景沒變
        if self.prev_pos and _dist(self.prev_pos, pos) > JUMP:
            d = _dist(self.prev_pos, pos)
            near = [p for p in self.props.values()
                    if _dist((p["x"], p["y"]), self.prev_pos) < 6]
            rec = {"frm": [round(self.prev_pos[0], 1),
                           round(self.prev_pos[1], 1)],
                   "to": [round(pos[0], 1), round(pos[1], 1)],
                   "frm_room": self.blob(self.prev_pos), "to_room": b,
                   "dist": round(d, 1),
                   "props_near_from": [p["model"] for p in near]}
            self.teleports.append(rec)
            self.ev("TELEPORT", **rec)

        # ⑤ 停留熱點
        now = time.monotonic()
        if self._still_at is None or _dist(self._still_at, pos) > STILL_EPS:
            self._still_at, self._still_from = pos, now
            self._still_logged = False
        elif (not self._still_logged
              and now - self._still_from >= STILL_SECS):
            self._still_logged = True
            rec = {"at": [round(pos[0], 1), round(pos[1], 1)], "room": b}
            self.stops.append(rec)
            self.ev("STILL", **rec)

        self.prev_pos = pos
        self.row(pos=[round(pos[0], 2), round(pos[1], 2)], room=b,
                 anim=entity.read_state(sc, ent + 8))

    # -- 慢拍：實體與獨立物件 -----------------------------------------
    def slow(self, hot) -> object:
        sc = self.sc
        try:
            _state, _pobj, ents, hot, _x = entity.snapshot(sc, regions=hot)
        except Exception as e:
            self.ev("SCANERR", err=str(e)[:60])
            return hot
        me = self.prev_pos
        alive_now = set()
        for e in ents:
            if not e.is_monster:
                continue
            alive_now.add(e.eid)
            if e.eid not in self.seen:
                mi = None
                try:
                    mi = monsters.info(sc, e.type_id)
                except Exception:
                    pass
                rec = {"eid": e.eid, "name": e.name, "type": e.type_id,
                       "at": [round(e.x, 1), round(e.y, 1)],
                       "room": self.blob((e.x, e.y)),
                       "boss": bool(mi and mi.boss),
                       "lv": mi.level if mi else None,
                       "hp": mi.max_hp if mi else None}
                self.seen[e.eid] = rec
                self.ev("SPAWN", **{k: rec[k] for k in
                                    ("name", "eid", "at", "room", "boss")})
            if e.dead and e.eid not in self.dead_at:
                rec = {"eid": e.eid, "name": e.name,
                       "at": [round(e.x, 1), round(e.y, 1)],
                       "room": self.blob((e.x, e.y)),
                       "me": me and [round(me[0], 1), round(me[1], 1)],
                       "boss": self.seen.get(e.eid, {}).get("boss")}
                self.dead_at[e.eid] = rec
                self.ev("DEAD", **{k: rec[k] for k in
                                   ("name", "at", "me", "boss")})
        # 獨立物件（傳送點候選）
        try:
            props = scenery.nearby(sc)
        except Exception:
            props = []
        now_ids = set()
        for p in props:
            now_ids.add(p.oid)
            if p.oid not in self.props:
                rec = {"oid": p.oid, "model": p.model,
                       "x": round(p.x, 1), "y": round(p.y, 1),
                       "room": self.blob((p.x, p.y))}
                self.props[p.oid] = rec
                self.ev("PROP", model=p.model, at=[rec["x"], rec["y"]],
                        room=rec["room"],
                        d=me and round(_dist((p.x, p.y), me), 1))
        return hot

    # -- 收工摘要 -----------------------------------------------------
    def summary(self) -> str:
        L = []
        add = L.append
        add(f"角色 {self.name}　場景 {self.scene_id} "
            f"{scene.scene_name(self.scene_id)}　共 {self.n_ticks} 拍")
        add(f"房間（連通區）大小：{self.blob_size}")
        add(f"走過的房間：{dict(self.visited)}　踩過 {len(self.tiles)} 格")
        for b, size in sorted(self.blob_size.items()):
            hit = self.visited.get(b, 0)
            add(f"   房間 #{b} {size:>5} 格 —— "
                + (f"走過（{hit} 拍）" if hit else "**沒去過**"))
        add("")
        add(f"傳送 {len(self.teleports)} 次：")
        for t in self.teleports:
            add(f"   房間{t['frm_room']} {t['frm']} → 房間{t['to_room']} "
                f"{t['to']}（{t['dist']} 格）"
                f" 起點附近物件 model={t['props_near_from']}")
        add("")
        boss = [r for r in self.seen.values() if r["boss"]]
        add(f"看到的怪 {len(self.seen)} 隻（王 {len(boss)}）、"
            f"死掉 {len(self.dead_at)} 隻")
        per_room: Counter = Counter()
        for r in self.seen.values():
            per_room[r["room"]] += 1
        add(f"   出現位置分佈（房間: 隻數）：{dict(per_room)}")
        kinds: dict[str, list] = {}
        for r in self.seen.values():
            kinds.setdefault(r["name"], []).append(r)
        for nm, rs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
            r0 = rs[0]
            add(f"   {nm:<16} x{len(rs):<3} type={r0['type']} "
                f"Lv{r0['lv']} HP{r0['hp']}"
                + ("  ★王" if r0["boss"] else ""))
            for r in rs[:40]:
                add(f"        出現 {r['at']} 房間{r['room']}"
                    + (f"　死於 {self.dead_at[r['eid']]['at']}"
                       f"（我站 {self.dead_at[r['eid']]['me']}）"
                       if r["eid"] in self.dead_at else "　（沒看到牠死）"))
        add("")
        add(f"獨立物件 {len(self.props)} 個（傳送點候選）：")
        by_model: dict[int, list] = {}
        for p in self.props.values():
            by_model.setdefault(p["model"], []).append(p)
        for m, ps in sorted(by_model.items()):
            add(f"   model={m} x{len(ps)}")
            for p in ps:
                add(f"        ({p['x']}, {p['y']}) 房間{p['room']}")
        add("")
        add(f"停留點 {len(self.stops)}：")
        for s in self.stops:
            add(f"   {s['at']} 房間{s['room']}")
        return "\n".join(L)


def main() -> int:
    os.makedirs(REPORTS, exist_ok=True)
    if "--stop" in sys.argv:
        open(STOP_FILE, "w").close()
        print("已放下停止檔，錄影會在下一拍收工")
        return 0
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    who = next((a for a in sys.argv[1:] if not a.startswith("-")), "")
    target = None
    for w in preload.windows():
        acct = charname.account_from_title(w.title) or str(w.pid)
        sc = MemoryScanner()
        sc.open(w.pid)
        nm = preload.name_of(w.pid, sc, acct) or ""
        print(f"   pid={w.pid} 帳號={acct} 角色={nm}")
        if not who or who in nm or who in acct or who in w.title:
            target = (sc, nm or acct)
            break
    if target is None:
        print(f"找不到符合「{who}」的視窗")
        return 1
    sc, nm = target
    locate.warm(sc)
    bad = locate.failed()
    if bad:
        print(f"⚠ AOB 定位失敗：{bad}")

    stamp = time.strftime("%m%d_%H%M%S")
    jsonl = os.path.join(REPORTS, f"dungeon_run_{stamp}.jsonl")
    print(f"\n開始錄（{nm}）—— 逐拍 → {jsonl}")
    print(f"收工：Ctrl+C，或另開視窗跑 py tools\\dungeon_record.py --stop\n")

    with open(jsonl, "w", encoding="utf-8") as f:
        rec = Recorder(sc, nm, f)
        hot = None
        next_slow = 0.0
        try:
            while not os.path.exists(STOP_FILE):
                t = time.monotonic()
                try:
                    rec.fast()
                except Exception as e:
                    print(f"[快拍例外] {e}")
                if t >= next_slow:
                    next_slow = t + SLOW_GAP
                    try:
                        hot = rec.slow(hot)
                    except Exception as e:
                        print(f"[慢拍例外] {e}")
                time.sleep(max(0.0, GAP - (time.monotonic() - t)))
        except KeyboardInterrupt:
            pass

    text = rec.summary()
    path = os.path.join(REPORTS, f"dungeon_run_{stamp}_摘要.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)
    print(f"\n摘要 → {path}\n逐拍 → {jsonl}")
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
