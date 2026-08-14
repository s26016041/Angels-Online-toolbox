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
        # 統計
        self.events: list[dict] = []
        self.n_rollback = 0
        self.n_stall = 0
        self.n_conn_drop = 0

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
        row["conn"] = self.conn
        self.rows += 1
        return row

    # -- 事件判讀 ----------------------------------------------------
    def judge(self, row: dict, emit) -> None:
        t, pos = row["t"], row.get("pos")
        prev = self.prev
        self.prev = row
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
              and row.get("anim") != "Sit"):
            self.stalling = True
            self.n_stall += 1
            self._stall_from = self._anchor_t
            ev = {"ev": "stall", "t": t, "who": self.name,
                  "at": [round(pos[0], 1), round(pos[1], 1)],
                  "anim": row.get("anim"), "conn": row.get("conn"),
                  "automove": row.get("automove"),
                  "target": row.get("target")}
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
            time.sleep(1.0)

    threading.Thread(target=state_loop, daemon=True).start()

    t0 = time.monotonic()
    last_conn = 0.0
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
                fh.flush()
                time.sleep(max(0.0, GAP - (time.monotonic() - t0 - t)))
        except KeyboardInterrupt:
            pass
    stopped = True

    print("\n===== 摘要 =====")
    for p in probes:
        print(f"{p.name}　{p.rows} 拍　回朔 {p.n_rollback} 次"
              f"　發呆(≥{STALL_SECS:.0f}s) {p.n_stall} 段"
              f"　斷線 {p.n_conn_drop} 次")
        for ev in p.events:
            if ev["ev"] == "rollback":
                back = ev.get("back_secs")
                print(f"  ⛔ t={ev['t']:.0f}s 回朔 {ev['jump']} 格 "
                      f"{ev['from']}→{ev['to']}"
                      f"（落點{'＝%.0f 秒前的位置' % back if back else '不明'}，"
                      f"發呆中={ev['stalling']} 連線={ev['conn']}）")
    print(f"\n逐拍紀錄：{path}")
    print("→ 把這個檔案路徑（和 farm_debug_*.log，如果有開）拿回來分析。")
    for p in probes:
        p.sc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
