# -*- coding: utf-8 -*-
"""「哪幾隻怪正在打我」—— 找出**準確**訊號的純唯讀探針。

    py tools\attacker_probe.py            # 只有一個分身時自動找
    py tools\attacker_probe.py --pid 1234
    py tools\attacker_probe.py --secs 60

背景（2026-09-12）：使用者指出官方「天使守護精靈 → 戰鬥」有一格
「被 __ 隻怪同時攻擊時使用技能」（wnd01.xml id=10051 圍毆按鈕），
＝**客戶端自己算得出「有幾隻怪在打我」**，所以這件事一定有準確來源。

我們現有的判斷全是弱訊號（交戰槽 +0x4D8/4E0/4E8 在怪出手當下是空的；
動畫 Att+距離分不出牠在打誰），見 memory `foe-field-unreliable`。
2026-08-08 那次掃描**漏掉了一個候選**：怪身上可能存的是「伺服器空間的
實體編號」（＝`[實體+0x1D0]`，2026-08-18 才發現），當時根本沒拿它去比對。

這支做的事（**全程只讀記憶體，不寫、不送封包、不掛勾**）：
  ① 記下我自己的各種身分：實體位址／實體編號／伺服器編號／玩家物件位址。
  ② 每 0.1 秒把附近每隻怪的前 0x800 bytes 讀回來，逐 dword 找有沒有等於
     我那些身分的值 —— 命中的偏移就是「這隻怪的目標是我」的候選欄位。
  ③ 反過來，把我自己（實體／玩家物件／狀態物件）前 0x1200 bytes 也掃一遍，
     找有沒有等於某隻怪的身分 —— 那就是「打我的人清單」的候選。
  ④ 同時記錄我的 HP 有沒有掉、每隻怪的動畫狀態，好判斷命中的欄位
     是不是真的跟「正在打我」同步。

怎麼跑：站在怪堆裡讓牠們打你（先一隻、再多隻最好），跑完看
`reports/attacker_probe.txt`。
"""
from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.core import charname, window as win                    # noqa: E402
from app.core.memory import MemoryScanner                      # noqa: E402
from app.game import bag, castwatch, entity, locate, player     # noqa: E402
from app.game import quickbar                                   # noqa: E402

OUT = os.path.join("reports", "attacker_probe.txt")
MON_SPAN = 0x800          # 每隻怪往後掃多少 bytes
ME_SPAN = 0x1200          # 我自己往後掃多少 bytes
HZ = 0.1                  # 取樣間隔


def clients():
    out, seen = [], set()
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
            continue
        seen.add(w.pid)
        out.append((w.pid, charname.account_from_title(w.title)))
    return out


def u32(sc, addr):
    raw = sc._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else 0


def scene_obj(sc, eid):
    """由實體編號拿到**場景實體物件**（bag.player_entity 用的那個空間：
    +0xBC 是編號、+0x1D0 是伺服器編號）。認不出來回 0。"""
    mgr = u32(sc, quickbar.MGR_PTR)
    if not mgr or not eid:
        return 0
    scene = u32(sc, mgr + bag.OFF_SCENE_MGR)
    if not scene:
        return 0
    table = u32(sc, scene + bag.OFF_ENT_TABLE)
    cap = u32(sc, scene + bag.OFF_ENT_CAP)
    idx = eid & 0xFFFF
    if not table or not 0 < cap <= 0x10000 or idx >= cap:
        return 0
    ptr = u32(sc, table + idx * 4)
    if not ptr or u32(sc, ptr + bag.OFF_ENT_ID) != eid:
        return 0
    return ptr


def dwords(blob):
    n = len(blob) // 4
    return struct.unpack("<%dI" % n, blob[:n * 4])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--secs", type=float, default=45.0)
    args = ap.parse_args()

    cs = clients()
    if not cs:
        print("⛔ 找不到遊戲視窗"); return 2
    pid = args.pid or cs[0][0]
    name = dict(cs).get(pid, "?")
    sc = MemoryScanner()
    sc.open(pid)
    locate.warm(sc)

    my_ent = bag.player_entity(sc)
    if not my_ent:
        print("⛔ 讀不到我的實體（還沒進場？）"); return 2
    srv = castwatch.own_server_id(sc, my_ent) or 0
    my_eid = u32(sc, my_ent + bag.OFF_ENT_ID)
    state, player_obj, ents, hot, _ = entity.snapshot(sc)
    if not state:
        state = entity.locate_state(sc)
    base = player.locate_fast(sc)
    st0 = player.read(sc, base) if base else None
    hp0 = st0.hp if st0 else -1

    # 我的各種「身分」＝要在怪身上找的值
    me_vals = {
        my_ent: "我的場景實體位址",
        my_ent - 8: "我的場景實體-8",
        my_ent + 8: "我的場景實體+8",
        my_eid: "我的實體編號",
        srv: "我的伺服器編號(+0x1D0)",
    }
    if player_obj:
        me_vals[player_obj] = "玩家物件位址"
        me_vals[player_obj - 8] = "玩家物件位址-8"
    if state:
        me_vals[state] = "狀態物件位址"
    me_vals.pop(0, None)

    print(f"[{pid}] {name}  實體={my_ent:#x} 編號={my_eid:#x} "
          f"伺服器編號={srv:#x} 玩家物件={player_obj and hex(player_obj)}")
    print(f"取樣 {args.secs:.0f} 秒 …… 請站著讓怪打你（不要反擊也沒關係）")

    # 統計：mon_hit[(偏移, 身分)] = [總次數, 出手中次數, 我掉血那拍次數]
    mon_hit = defaultdict(lambda: [0, 0, 0])
    mon_seen = defaultdict(set)
    me_hit = defaultdict(lambda: [0, 0])
    # 即時顯示用：候選欄位（第一輪跑出來的）
    # ★ 2026-09-12 定案候選：怪的實體物件 +0x34C ＝ **牠正在打的對象的實體編號**
    #   （遊戲自己的存取器 0x557852：實體編號 → 物件 → 回 [物件+0x354]，
    #    我們的實體空間差 8 bytes ⇒ +0x34C）
    CAND_TGT_OFF = 0x34C
    CAND_MON_OFF = 0x20      # 舊候選：怪的實體物件 +0x20 == 我的玩家物件？
    live_t = 0.0
    ticks = hp_drops = 0
    max_pointing = 0          # 同一拍最多幾隻怪指著我
    point_hist = defaultdict(int)
    # 「以我為目標」的怪：動畫狀態 × 離我多遠（找「已經在打我」的硬分界）
    eng_tab = defaultdict(int)
    eng_state = defaultdict(int)
    eng_dist = defaultdict(lambda: [99.0, 0.0])   # eid → [最近, 最遠]
    all_tab = defaultdict(int)    # (動畫狀態, 目標是誰) → 拍次
    att_ticks = 0
    t_end = time.monotonic() + args.secs
    hp_prev = hp0
    full_t = 0.0

    while time.monotonic() < t_end:
        t0 = time.monotonic()
        if t0 - full_t > 5.0:
            state, player_obj, ents, hot, _ = entity.snapshot(sc)
            full_t = t0
        else:
            state, player_obj, ents, hot, _ = entity.snapshot(sc, regions=hot)
        st = player.read(sc, base) if base else None
        hp = st.hp if st else hp_prev
        dropped = hp_prev >= 0 and hp >= 0 and hp < hp_prev
        hp_prev = hp
        ticks += 1
        if dropped:
            hp_drops += 1

        mons = [e for e in ents if e.is_monster and not e.dead]
        scenes = {m.eid: scene_obj(sc, m.eid) for m in mons}
        # ① 怪身上有沒有「我」（兩種物件空間各掃一次）
        for m in mons:
            atk = entity.read_state(sc, m.addr) in entity.ATT_STATES
            if atk:
                att_ticks += 1
            for space, addr in (("實體", m.addr), ("場景", scenes.get(m.eid) or 0)):
                if not addr:
                    continue
                blob = sc._read_bytes(addr, MON_SPAN)
                if not blob or len(blob) < MON_SPAN:
                    continue
                for i, v in enumerate(dwords(bytes(blob))):
                    lab = me_vals.get(v)
                    if lab is None:
                        continue
                    k = (space, i * 4, lab)
                    mon_hit[k][0] += 1
                    if atk:
                        mon_hit[k][1] += 1
                    if dropped:
                        mon_hit[k][2] += 1
                    mon_seen[k].add(m.eid)

        # ② 我身上有沒有「某隻怪」
        mon_vals = {}
        for m in mons:
            sa = scenes.get(m.eid) or 0
            msrv = u32(sc, sa + castwatch.SRV_ID_OFF) if sa else 0
            for v, lab in ((m.addr, "怪的實體位址"), (m.addr - 8, "怪的實體位址-8"),
                           (sa, "怪的場景實體位址"), (m.eid, "怪的實體編號"),
                           (msrv, "怪的伺服器編號")):
                if v:
                    mon_vals[v] = lab
        for who, addr in (("我的場景實體", my_ent), ("玩家物件", player_obj),
                          ("狀態物件", state)):
            if not addr:
                continue
            blob = sc._read_bytes(addr, ME_SPAN)
            if not blob or len(blob) < ME_SPAN:
                continue
            for i, v in enumerate(dwords(bytes(blob))):
                lab = mon_vals.get(v)
                if lab is None:
                    continue
                k = (who, i * 4, lab)
                me_hit[k][0] += 1
                if dropped:
                    me_hit[k][1] += 1

        # 同一拍有幾隻怪的 +0x20 指著我（＝官方「圍毆」數量的候選）
        npoint = sum(1 for m in mons
                     if u32(sc, m.addr + CAND_TGT_OFF) == my_eid)
        point_hist[npoint] += 1
        if npoint > max_pointing:
            max_pointing = npoint

        # 以我為目標的怪：狀態 × 距離（純讀，幾隻而已）
        me_pos = entity.read_pos(sc, player_obj) if player_obj else None
        for m in mons:
            tv = u32(sc, m.addr + CAND_TGT_OFF)
            st = entity.read_state(sc, m.addr) or "?"
            who = "我" if tv == my_eid else ("空" if tv == 0 else "別人")
            all_tab[(st, who)] += 1
            if tv != my_eid:
                continue
            pos = entity.read_pos(sc, m.addr)
            d = (math.hypot(pos[0] - me_pos[0], pos[1] - me_pos[1])
                 if (pos and me_pos) else None)
            if d is None:
                bucket = "讀不到"
            elif d <= 1.5:
                bucket = "≤1.5"
            elif d <= 2.5:
                bucket = "≤2.5"
            elif d <= 3.5:
                bucket = "≤3.5"
            elif d <= 6.0:
                bucket = "≤6"
            elif d <= 12.0:
                bucket = "≤12"
            else:
                bucket = ">12"
            eng_tab[(st, bucket)] += 1
            eng_state[st] += 1
            if d is not None:
                lo, hi = eng_dist[m.eid & 0xFFFF]
                eng_dist[m.eid & 0xFFFF] = [min(lo, d), max(hi, d)]

        # ③ 每秒印一行「現在誰指著我」給使用者對畫面
        if t0 - live_t >= 1.0:
            live_t = t0
            marks = []
            for m in mons:
                tags = ""
                if u32(sc, m.addr + CAND_TGT_OFF) == my_eid:
                    tags += "◎"
                if player_obj and u32(sc, m.addr + CAND_MON_OFF) == player_obj:
                    tags += "★"
                if player_obj and entity.attacking(sc, m, player_obj):
                    tags += "槽"
                if entity.read_state(sc, m.addr) in entity.ATT_STATES:
                    tags += "揮"
                if tags:
                    marks.append(f"{m.name}({m.eid & 0xFFFF:#x}){tags}")
            tgt = ""
            if state:
                ok, tid, thp = entity.read_target_checked(sc, state)
                if ok and tid:
                    tgt = f" 我選的目標={tid & 0xFFFF:#x}({thp}%)"
            print(f"  HP={hp:<6} 怪{len(mons):<3} 打我{npoint}隻{tgt} "
                  + ("｜" + " ".join(marks) if marks else "｜（沒有）"))

        left = HZ - (time.monotonic() - t0)
        if left > 0:
            time.sleep(left)

    os.makedirs("reports", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        def w(s=""):
            f.write(s + "\n")
        w(f"# attacker_probe  pid={pid} {name}  {time.strftime('%Y-%m-%d %H:%M:%S')}")
        w(f"取樣 {ticks} 拍、我掉血 {hp_drops} 拍、怪出手 {att_ticks} 拍次")
        w(f"同一拍最多有 {max_pointing} 隻怪的 +0x34C 指著我；"
          f"分佈={dict(sorted(point_hist.items()))}")
        w(f"我的實體={my_ent:#x} 編號={my_eid:#x} 伺服器編號={srv:#x} "
          f"玩家物件={player_obj and hex(player_obj)} 狀態物件={state and hex(state)}")
        w()
        w("## ① 怪身上出現「我」的欄位（偏移 → 命中次數／其中出手中／其中我掉血那拍／幾隻怪）")
        if not mon_hit:
            w("（零命中）")
        for (space, off, lab), (n, a, d) in sorted(mon_hit.items(),
                                                   key=lambda kv: -kv[1][0]):
            w(f"  [{space}] +{off:#06x}  {lab:<20} 次數={n:<5} 出手中={a:<5} "
              f"掉血拍={d:<4} 怪數={len(mon_seen[(space, off, lab)])}")
        w()
        w("## ③ 以我為目標的怪：動畫狀態 × 離我多遠（拍次）")
        w("   狀態合計：" + "、".join(f"{k}={v}" for k, v in
                                  sorted(eng_state.items(), key=lambda kv: -kv[1])))
        for (st, b), n in sorted(eng_tab.items(), key=lambda kv: -kv[1]):
            w(f"  {st:<6} {b:<6} {n}")
        w("   每隻的距離範圍：" + "、".join(
            f"{eid:#x}[{lo:.1f}~{hi:.1f}]" for eid, (lo, hi) in eng_dist.items()))
        w()
        w("## ④ 所有怪：動畫狀態 × +0x34C 指著誰（拍次）")
        for (st2, who2), n in sorted(all_tab.items(), key=lambda kv: -kv[1]):
            w(f"  {st2:<6} 目標={who2:<3} {n}")
        w()
        w("## ② 我身上出現「某隻怪」的欄位")
        if not me_hit:
            w("（零命中）")
        for (who, off, lab), (n, d) in sorted(me_hit.items(),
                                              key=lambda kv: -kv[1][0]):
            w(f"  {who} +{off:#06x}  {lab:<16} 次數={n:<5} 掉血拍={d}")
    print(f"寫好了：{OUT}  （命中 ①{len(mon_hit)} 項 ②{len(me_hit)} 項）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
