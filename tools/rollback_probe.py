"""回朔探針：抓「角色發呆一陣子之後被拉回原位」的完整時間軸（純讀取）。

    py tools\\rollback_probe.py              # 盯所有分身，Ctrl+C 收工
    py tools\\rollback_probe.py --who 白狐   # 只盯名字含「白狐」的那台

要回答的問題（2026-08-14 使用者回報：白狐掛機常發呆→過段時間位置回朔，只有他會）
---------------------------------------------------------------------------
「回朔」＝伺服器不承認客戶端的移動、事後一次修正。可能的病因有三種，
發作當下的訊號完全不同，所以要逐拍拍下來：

  ① 伺服器端互動狀態沒解除（NPC 對話/維修/倉庫沒送「離開」包 0x22）
     → 角色被鎖住不能走（實測 2026-08-14 嵐狐）：客戶端照走、伺服器全退回。
     訊號：回朔反覆發生、每次都拉回同一個點；互動/自動走路狀態機（+0x41A4）
     有殘值；發呆期間經驗值照樣會動（連線是好的）。
  ② 那台的 TCP 短暫斷流：畫面照動、數值凍住，恢復時一次修正。
     訊號：發呆期間經驗/金幣/怪物全部凍住；ESTABLISHED 連線消失或
     恢復瞬間大量變動；回朔落點＝斷流開始前的位置。
  ③ 我們地形圖算的路徑點踩到伺服器不給站的格子（單次移動被退回）。
     訊號：偶發、單段、回朔距離＝那一段路的長度；狀態機乾淨、連線正常。

搭配主程式的 AO_FARM_LOG=1（farm_debug_<帳號>.log 每秒一行決策）一起看，
就能把「我們下了什麼指令」跟「遊戲實際發生什麼」對上。

輸出：逐拍紀錄 reports/rollback_probe_<時間>.jsonl；主控台只印事件與摘要。
⚠ 全程純讀：不呼叫遊戲函式、不寫記憶體（locate_state 是唯讀全掃）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import charname, netstat, preload              # noqa: E402
from app.core.memory import MemoryScanner                    # noqa: E402
from app.game import bag, entity, locate, move, player, scene  # noqa: E402

GAP = 0.1                 # 快拍間隔
CONN_GAP = 1.0            # 連線表多久查一次（整張表所有分身共用）
HIST_SECS = 120.0         # 位置歷史留多久（找「拉回到幾秒前」用）

# 一拍 0.1 秒角色最多走 ~0.9 格；超過 3 格＝瞬移（回朔或傳送）。
JUMP_GRIDS = 3.0
# 發呆判定：離錨點淨位移一直小於這個、持續 STALL_SECS 以上。
STALL_EPS = 2.0
STALL_SECS = 20.0

# 自動走路／互動狀態機（玩家物件−8 ＝ pathfinder_this 為基準；出處
# memory automove-state-machine 與 supply.INTERACT_FN 的反組譯：
# 函式先查 +0x41A0，參數寫進 +0x41A4/+0x41A8/+0x41B4）。結構偏移，純讀。
AUTOMOVE_OFFS = (0x41A0, 0x41A4, 0x41A8, 0x41B4)


def _d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Probe:
    def __init__(self, w) -> None:
        self.pid, self.hwnd, self.title = w.pid, w.hwnd, w.title
        self.account = charname.account_from_title(w.title) or str(w.pid)
        self.sc = MemoryScanner()
        self.sc.open(w.pid)
        self.name = preload.name_of(w.pid, self.sc, self.account)
        self.base: int | None = None      # 玩家屬性物件（locate_fast，快取）
        self.state: int | None = None     # 狀態物件（全掃，慢執行緒定位）
        self._state_tried = 0.0
        self.rows = 0
        self.conn = True                  # 上一次查連線表的結果
        # 位置歷史：(t, (x, y))
        self.hist: deque[tuple[float, tuple[float, float]]] = deque()
        self.prev: dict | None = None     # 上一拍
        # 發呆追蹤
        self._anchor: tuple[float, float] | None = None
        self._anchor_t = 0.0
        self.stalling = False
        self._stall_from = 0.0
        # 目標追蹤（抓「打傷了沒打死就換目標」）
        self._tgt = 0
        self._tgt_t = 0.0
        self._tgt_hp_after = 0.0   # 換目標後短暫時間血量欄可能還是舊值
        self._tgt_first: int | None = None
        self._tgt_min: int | None = None
        self._tgt_last: int | None = None
        self._tgt_zero_at: float | None = None
        self._dmg_t = -999.0               # 上次看到目標血量下降的時刻
        # 驗屍：premature 換目標 2 秒後掃實體清單看舊目標死了沒
        self.pending_necro: list[dict] = []          # {due(絕對時間), eid, ev}
        self.necro_results: deque = deque()          # 掃描執行緒 → 主迴圈
        # 目標怪的位置（掃描執行緒 1Hz 更新；驗「走路終點是不是怪那格」）
        self.tgt_at: tuple[int, float, float] | None = None   # (eid, x, y)
        self._tgt_at_last: list[float] | None = None  # 目前目標最後一次已知位置
        self._hot = None                              # snapshot 熱區快取
        # 統計
        self.events: list[dict] = []
        self.n_rollback = 0
        self.n_stall = 0
        self.n_conn_drop = 0
        self.n_premature = 0
        self.n_switch = 0
        self.n_kill = 0
        self.n_necro_corpse = 0
        self.n_necro_gone = 0
        self.n_necro_alive = 0

    # -- 讀一拍 ------------------------------------------------------
    def sample(self, t: float) -> dict:
        row: dict = {"t": round(t, 2), "who": self.name}
        sc = self.sc
        try:
            sc_now = scene.current(sc, allow_scan=False)
            row["scene"] = sc_now.id if sc_now else None
        except Exception:
            row["scene"] = None
        ent = None
        try:
            ent = bag.player_entity(sc)
        except Exception:
            pass
        pos = entity.read_pos(sc, ent + 8) if ent else None
        row["pos"] = pos and [round(pos[0], 2), round(pos[1], 2)]
        try:
            row["anim"] = entity.read_state(sc, ent + 8) if ent else None
        except Exception:
            row["anim"] = None
        try:
            row["walking"] = entity.is_walking(sc, ent + 8) if ent else None
        except Exception:
            row["walking"] = None
        # 自動走路／互動狀態機（+0x41A0 有殘值＝伺服器端互動嫌疑）
        pf = None
        try:
            pf = move.pathfinder_this(sc)
        except Exception:
            pass
        if pf:
            am = []
            for off in AUTOMOVE_OFFS:
                try:
                    am.append(bag._u32(sc, pf + off))
                except Exception:
                    am.append(None)
            row["automove"] = am
        # 玩家屬性（經驗/金幣凍不凍＝分辨斷流的關鍵）
        if not self.base:
            try:
                self.base = player.locate_fast(sc)
            except Exception:
                self.base = None
        stats = player.read(sc, self.base) if self.base else None
        if stats is None:
            self.base = None
        else:
            row["hp"], row["mp"] = stats.hp, stats.mp
            row["exp"], row["gold"] = stats.exp, stats.gold
        # 狀態物件的「選定目標」（有在打誰）
        if self.state:
            try:
                if not entity.state_ok(sc, self.state):
                    self.state = None
                else:
                    ok, eid, thp = entity.read_target_checked(sc, self.state)
                    if ok:
                        row["target"] = eid
                        row["target_hp"] = thp
            except Exception:
                self.state = None
        # 目標怪的位置（掃描執行緒 1Hz 更新的快取，eid 對得上才用）
        ta = self.tgt_at
        if ta and row.get("target") == ta[0]:
            row["tgt_at"] = [round(ta[1], 1), round(ta[2], 1)]
        row["conn"] = self.conn
        self.rows += 1
        return row

    # -- 目標追蹤：抓「打傷了沒打死就換目標」 -------------------------
    def _track_target(self, row: dict, emit) -> None:
        """目標欄 +4 是遊戲自己維護的血量百分比，死亡訊號＝寫成 ≤0
        （出處 entity.set_target_id 的反組譯說明）。所以：
        換目標當下上一隻 last_hp>0 且 min_hp<first_hp ＝ 打傷了沒打死就放生。"""
        eid = row.get("target")
        if eid is None:                     # 狀態物件還沒定位到，欄位不存在
            return
        t, hp = row["t"], row.get("target_hp")
        if eid == self._tgt:
            self._tgt_zero_at = None
            if row.get("tgt_at"):
                self._tgt_at_last = row["tgt_at"]
            # 換目標後 0.4 秒內血量欄可能還是上一隻的殘值（遊戲 0.3 秒內回填）
            if eid and hp is not None and t >= self._tgt_hp_after and hp <= 100:
                if self._tgt_first is None and hp > 0:
                    self._tgt_first = hp
                if self._tgt_first is not None:
                    if self._tgt_last is not None and hp < self._tgt_last:
                        self._dmg_t = t          # 正在輸出（發呆判定要排除）
                    self._tgt_min = (hp if self._tgt_min is None
                                     else min(self._tgt_min, hp))
                    self._tgt_last = hp
            return
        if eid == 0:
            # 單拍讀失敗 read_target_checked 也會回 0——清空要持續 0.5 秒才算
            if self._tgt_zero_at is None:
                self._tgt_zero_at = t
                return
            if t - self._tgt_zero_at < 0.5:
                return
        old, first, mn, last = self._tgt, self._tgt_first, self._tgt_min, self._tgt_last
        old_at = self._tgt_at_last
        dur = t - self._tgt_t
        self._tgt, self._tgt_t = eid, t
        self._tgt_hp_after = t + 0.4
        self._tgt_first = self._tgt_min = self._tgt_last = None
        self._tgt_zero_at = None
        self._tgt_at_last = None
        if not old:                          # 首次選定
            return
        if first is None:
            # 鎖定整段從沒看到血量>0：放棄的（15s 逃生／屍體判定），
            # 這正是「打不中呆等」的樣子——記下來，附怪的位置算距離。
            ev = {"ev": "target_giveup", "t": t, "who": self.name,
                  "from": old, "to": eid, "secs": round(dur, 1),
                  "old_at": old_at, "pos": row.get("pos")}
            self.events.append(ev)
            emit(ev)
            if dur >= 8.0:
                p0, p1 = row.get("pos"), old_at
                dist = (p0 and p1 and
                        round(math.hypot(p0[0] - p1[0], p0[1] - p1[1]), 1))
                print(f"🕳 [{time.strftime('%H:%M:%S')}] {self.name} "
                      f"鎖定 {dur:.0f} 秒血量不動放棄 {old:#x}"
                      f"（我在 {p0}，怪在 {p1}，距離 {dist}）")
            return
        if last is not None and last <= 0:
            verdict = "killed"              # 有看見死亡訊號才換＝正常
        elif mn is not None and mn < first:
            verdict = "premature"           # 打傷了、最後一眼還活著就換
        else:
            verdict = "no_damage"           # 沒打傷就換（規則允許）
        ev = {"ev": "target_switch", "t": t, "who": self.name,
              "from": old, "to": eid, "secs": round(dur, 1),
              "first_hp": first, "min_hp": mn, "last_hp": last,
              "verdict": verdict, "pos": row.get("pos"), "old_at": old_at}
        self.n_switch += 1
        if verdict == "killed":
            self.n_kill += 1
        self.events.append(ev)
        emit(ev)
        if verdict == "premature":
            self.n_premature += 1
            # 別急著喊：last_hp>0 也可能只是 10Hz 拍不到歸 0 的那一瞬
            # （掛機在死亡訊號後 20ms 內就換走）。2 秒後回頭驗屍才算數。
            self.pending_necro.append(
                {"due": time.monotonic() + 2.0, "eid": old, "ev": ev})

    # -- 事件判讀 ----------------------------------------------------
    def judge(self, row: dict, emit) -> None:
        t, pos = row["t"], row.get("pos")
        prev = self.prev
        self.prev = row
        self._track_target(row, emit)
        if pos is None:
            return
        pos = (pos[0], pos[1])
        # 位置歷史
        self.hist.append((t, pos))
        while self.hist and t - self.hist[0][0] > HIST_SECS:
            self.hist.popleft()

        # 瞬移偵測（同場景、相鄰兩拍跳超過 JUMP_GRIDS）
        if (prev and prev.get("pos") is not None
                and prev.get("scene") == row.get("scene")
                and t - prev["t"] <= 0.35):
            p0 = (prev["pos"][0], prev["pos"][1])
            jump = _d(p0, pos)
            if jump > JUMP_GRIDS:
                # 落點是不是「以前待過的位置」？往回找最近一次靠近落點的時刻
                back = None
                for ht, hp_ in reversed(self.hist):
                    if t - ht < 1.0:
                        continue                     # 跳過剛剛這幾拍
                    if _d(hp_, pos) <= 2.5:
                        back = t - ht
                        break
                self.n_rollback += 1
                ev = {"ev": "rollback", "t": t, "who": self.name,
                      "jump": round(jump, 1),
                      "from": [round(p0[0], 1), round(p0[1], 1)],
                      "to": [round(pos[0], 1), round(pos[1], 1)],
                      "back_secs": back and round(back, 1),
                      "anim": row.get("anim"), "conn": row.get("conn"),
                      "automove": row.get("automove"),
                      "stalling": self.stalling}
                self.events.append(ev)
                emit(ev)
                where = (f"＝{back:.0f} 秒前待過的位置" if back
                         else "（近 2 分鐘沒待過的點）")
                print(f"⛔ [{time.strftime('%H:%M:%S')}] {self.name} 回朔！"
                      f"瞬移 {jump:.1f} 格 {ev['from']}→{ev['to']} 落點{where}"
                      f"　發呆中={self.stalling} 連線={row.get('conn')}"
                      f" automove={row.get('automove')}")
                # 回朔後重新定錨，別把落點當成還在發呆
                self._anchor, self._anchor_t = pos, t
                if self.stalling:
                    self.stalling = False
                return

        # 發呆偵測（淨位移，照 farm 的錨點邏輯）
        if self._anchor is None or _d(pos, self._anchor) > STALL_EPS:
            if self.stalling:
                dur = t - self._stall_from
                print(f"　[{time.strftime('%H:%M:%S')}] {self.name} "
                      f"發呆結束（共 {dur:.0f} 秒，自己動起來了）")
                self.events.append({"ev": "stall_end", "t": t,
                                    "who": self.name, "secs": round(dur, 1)})
                emit(self.events[-1])
                self.stalling = False
            self._anchor, self._anchor_t = pos, t
        elif (not self.stalling and t - self._anchor_t >= STALL_SECS
              and row.get("anim") != "Sit"
              and t - self._dmg_t > 5.0):   # 站定輸出中（遠程砲台）不算發呆
            self.stalling = True
            self.n_stall += 1
            self._stall_from = self._anchor_t
            ev = {"ev": "stall", "t": t, "who": self.name,
                  "at": [round(pos[0], 1), round(pos[1], 1)],
                  "anim": row.get("anim"), "conn": row.get("conn"),
                  "automove": row.get("automove"),
                  "target": row.get("target"),
                  "target_hp": row.get("target_hp"),
                  "tgt_at": row.get("tgt_at")}
            self.events.append(ev)
            emit(ev)
            print(f"⚠ [{time.strftime('%H:%M:%S')}] {self.name} "
                  f"發呆 {STALL_SECS:.0f} 秒＋（在 {ev['at']}，"
                  f"anim={row.get('anim')} 連線={row.get('conn')}"
                  f" automove={row.get('automove')}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--who", default="", help="只盯名字/帳號含這個字串的分身")
    ap.add_argument("--secs", type=float, default=0.0, help="0 = 一直跑")
    args = ap.parse_args()

    wins = preload.windows()
    if not wins:
        print("沒有開著的分身")
        return 1
    sc0 = MemoryScanner()
    sc0.open(wins[0].pid)
    locate.warm(sc0)
    sc0.close()

    probes = [Probe(w) for w in wins]
    if args.who:
        probes = [p for p in probes
                  if args.who in p.name or args.who in p.account]
        if not probes:
            print(f"找不到名字/帳號含「{args.who}」的分身")
            return 1

    os.makedirs("reports", exist_ok=True)
    path = os.path.join(
        "reports", f"rollback_probe_{time.strftime('%m%d_%H%M%S')}.jsonl")
    print(f"盯著 {len(probes)} 台：" + "、".join(p.name for p in probes))
    print(f"逐拍紀錄 → {path}　（Ctrl+C 收工；只印事件，安靜＝沒事）\n")

    stopped = False

    # 狀態物件定位是全掃（0.5~1 秒/台），丟到自己的執行緒慢慢補，
    # 沒補到之前 target 欄位先空著，不擋 10Hz 快拍。
    def state_loop() -> None:
        while not stopped:
            for p in probes:
                if stopped:
                    return
                if p.state is None and time.monotonic() - p._state_tried > 15:
                    p._state_tried = time.monotonic()
                    try:
                        p.state = entity.locate_state(p.sc)
                    except Exception:
                        p.state = None
                # 每秒一遍實體清單（首遍全掃、之後熱區）：
                # ① 更新目標怪的位置（驗「走路終點是不是怪那格」）
                # ② 驗屍（premature 換目標滿 2 秒的找舊目標死了沒）
                ents = None
                try:
                    _, _, ents, hot, _ = entity.snapshot(p.sc,
                                                         regions=p._hot)
                    p._hot = hot or None
                except Exception:
                    p._hot = None
                if ents is not None:
                    want = p._tgt
                    e = next((x for x in ents if x.eid == want), None)
                    if e is not None:
                        p.tgt_at = (e.eid, e.x, e.y)
                    elif want:
                        # 目前的目標在熱區找不到 → 熱區可能過期，下一輪全掃
                        p._hot = None
                # 屍體會躺著（中位 5 秒）所以「打死了」多半找得到 Dead；
                # 還活著＝真放生實錘。結果丟回主迴圈印／寫檔（避免兩執行緒搶檔案）。
                due = [q for q in p.pending_necro
                       if time.monotonic() >= q["due"]]
                if due:
                    if ents is not None:
                        p.pending_necro = [q for q in p.pending_necro
                                           if q not in due]
                        for q in due:
                            e = next((x for x in ents
                                      if x.eid == q["eid"]), None)
                            if e is None:
                                v = "gone"
                                p.n_necro_gone += 1
                            elif e.dead:
                                v = "corpse"
                                p.n_necro_corpse += 1
                            else:
                                v = "alive"
                                p.n_necro_alive += 1
                            p.necro_results.append(
                                {"ev": "necropsy", "t": q["ev"]["t"],
                                 "who": p.name, "eid": q["eid"], "verdict": v,
                                 "state": e.state if e else None,
                                 "at": e and [round(e.x, 1), round(e.y, 1)],
                                 "switch_ev": q["ev"]})
            time.sleep(1.0)

    threading.Thread(target=state_loop, daemon=True).start()

    t0 = time.monotonic()
    last_conn = 0.0
    last_report = 0.0
    with open(path, "w", encoding="utf-8") as fh:
        def emit(row: dict) -> None:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        try:
            while True:
                t = time.monotonic() - t0
                if args.secs and t >= args.secs:
                    break
                if t - last_conn >= CONN_GAP:
                    last_conn = t
                    alive = netstat.established_pids()
                    if alive:               # 查失敗回空集合 → 不當斷線
                        for p in probes:
                            now = p.pid in alive
                            if p.conn and not now:
                                p.n_conn_drop += 1
                                print(f"🔌 [{time.strftime('%H:%M:%S')}] "
                                      f"{p.name} TCP 連線消失！")
                                emit({"ev": "conn_lost", "t": round(t, 2),
                                      "who": p.name})
                            elif now and not p.conn:
                                print(f"🔌 [{time.strftime('%H:%M:%S')}] "
                                      f"{p.name} TCP 連線恢復")
                                emit({"ev": "conn_back", "t": round(t, 2),
                                      "who": p.name})
                            p.conn = now
                for p in probes:
                    try:
                        row = p.sample(t)
                    except Exception as exc:   # 分身關掉之類，別讓整支死
                        emit({"ev": "err", "t": round(t, 2), "who": p.name,
                              "why": f"{type(exc).__name__}: {exc}"})
                        continue
                    emit(row)
                    p.judge(row, emit)
                    while p.necro_results:
                        res = p.necro_results.popleft()
                        emit(res)
                        if res["verdict"] == "alive":
                            p.events.append(res)
                            se = res["switch_ev"]
                            print(f"⚠⚠ [{time.strftime('%H:%M:%S')}] {p.name}"
                                  f" 真放生實錘！{se['first_hp']}%→"
                                  f"{se['last_hp']}% 打了 {se['secs']}s 就換，"
                                  f"2 秒後那隻還活著"
                                  f"（state={res['state']} 在 {res['at']}）")
                if t - last_report >= 120.0:
                    last_report = t
                    for p in probes:
                        print(f"[{time.strftime('%H:%M:%S')}] {p.name} 累計："
                              f"換目標 {p.n_switch}"
                              f"（見證死亡 {p.n_kill}、驗屍＝屍體 "
                              f"{p.n_necro_corpse}、消失 {p.n_necro_gone}、"
                              f"⚠還活著 {p.n_necro_alive}）"
                              f"　回朔 {p.n_rollback}　發呆 {p.n_stall}")
                fh.flush()
                time.sleep(max(0.0, GAP - (time.monotonic() - t0 - t)))
        except KeyboardInterrupt:
            pass
    stopped = True

    print("\n===== 摘要 =====")
    for p in probes:
        print(f"{p.name}　{p.rows} 拍　回朔 {p.n_rollback} 次"
              f"　發呆(≥{STALL_SECS:.0f}s) {p.n_stall} 段"
              f"　斷線 {p.n_conn_drop} 次"
              f"　換目標 {p.n_switch}（最後一眼還活著 {p.n_premature}："
              f"驗屍屍體 {p.n_necro_corpse}／消失 {p.n_necro_gone}"
              f"／⚠真活著 {p.n_necro_alive}）")
        for ev in p.events:
            if ev["ev"] == "rollback":
                back = ev.get("back_secs")
                print(f"  ⛔ t={ev['t']:.0f}s 回朔 {ev['jump']} 格 "
                      f"{ev['from']}→{ev['to']}"
                      f"（落點{'＝%.0f 秒前的位置' % back if back else '不明'}，"
                      f"發呆中={ev['stalling']} 連線={ev['conn']}）")
            elif ev["ev"] == "necropsy" and ev["verdict"] == "alive":
                se = ev["switch_ev"]
                print(f"  ⚠⚠ t={ev['t']:.0f}s 真放生："
                      f"{se['first_hp']}%→{se['last_hp']}%"
                      f" 打了 {se['secs']}s 就換，驗屍還活著"
                      f"（state={ev['state']} 在 {ev['at']}）")
    print(f"\n逐拍紀錄：{path}")
    print("→ 把這個檔案路徑（和 farm_debug_*.log，如果有開）拿回來分析。")
    for p in probes:
        p.sc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
