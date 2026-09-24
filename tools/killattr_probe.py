# -*- coding: utf-8 -*-
r"""「這隻怪是不是我殺的」—— 找 100% 歸屬訊號的**純唯讀**探針。

    py tools\killattr_probe.py                # 列出各分身、hook 狀態
    py tools\killattr_probe.py --pid 1234 --secs 120

背景（2026-09-23）：掛機的「已擊殺 N 隻」現在只認「鎖定的怪出現 Dead／物件
被回收」，被搶、走出視野、消失都會算 → 數字灌水。使用者要 100% 的歸屬。
100% 只有一種來源：伺服器明講（怪死亡廣播／經驗入帳包）。

這支做的事（**不裝 hook、不寫記憶體、不送封包**）：
  ① 讀 INBOUND_FN 開頭 7 bytes：是工具箱自己的 castwatch hook（E9…9090、
     目標第一 byte=pushad）才做 —— 從 stub 裡解出它的環狀緩衝位址，
     **借讀**工具箱正在記的入向封包（每包前 16 bytes）。
     ⛔ 沒 hook 的分身不裝（memory `opener-first-strike-gate`：另開行程
       acquire 會把工具箱的 hook 還原 → 探針結束＝遊戲崩潰）。
  ② 每 20ms 記：新封包、我選的目標(eid/血量)、每隻怪的動畫狀態、我的經驗值。
  ③ 事件：經驗上漲、目標變 Dead、非目標變 Dead、怪物消失。
     對每個事件列出前後 0.6 秒內的封包（op/sub/hex，標出含目標 eid 的）。
  ④ 統計：每種 (op,sub) 在「我的擊殺(經驗漲)」「別人殺的」「平時」各出現幾次
     → 只在我的擊殺出現、而且帶那隻怪 ID 的，就是候選。

結果寫 reports/killattr_probe.txt，主控台只印摘要。
"""
from __future__ import annotations

import argparse
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

OUT = os.path.join("reports", "killattr_probe.txt")
HZ = 0.02
WIN_BEFORE = 0.6
WIN_AFTER = 0.6
FULL_EVERY = 3.0


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
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) == 4 else 0


def hook_ring(sc):
    """工具箱 castwatch hook 在不在；在的話回 (wcnt位址, ring位址)。"""
    fn = castwatch.INBOUND_FN
    if not fn:
        return None, "INBOUND_FN 定位失敗"
    cur = bytes(sc._read_bytes(fn, castwatch.STOLEN) or b"")
    if cur == castwatch._EXPECT_PROLOGUE:
        return None, "沒 hook（原始 prologue）"
    if len(cur) != 7 or cur[0] != 0xE9 or cur[5:] != b"\x90\x90":
        return None, f"別人的 patch {cur.hex()}"
    target = fn + 5 + int.from_bytes(cur[1:5], "little", signed=True)
    head = bytes(sc._read_bytes(target, 16) or b"")
    # stub 開頭：60 (pushad) / 8B 3D imm32 (mov edi,[wcnt]) …
    if len(head) < 7 or head[0] != 0x60 or head[1:3] != b"\x8b\x3d":
        return None, f"stub 開頭認不得 {head.hex()}"
    wcnt = struct.unpack_from("<I", head, 3)[0]
    ring = wcnt + 64
    # 交叉驗證：stub 裡 `add eax, ring`（05 imm32）要對得上
    body = bytes(sc._read_bytes(target, 64) or b"")
    if struct.pack("<I", ring) not in body:
        return None, "stub 裡找不到 ring 位址"
    return (wcnt, ring), "工具箱 hook 在，借讀"


OWN_N = 1024
OWN_CAP = 64
OWN_SLOT = 8 + OWN_CAP      # seq(4) + len(4) + data


def _own_stub(wcnt: int, ring: int, cont: int) -> str:
    """跟 castwatch._stub_asm 同款，但記 64 bytes＋封包長度。
    ★ 第一個 byte 故意是 nop（不是 pushad）：工具箱若中途想裝 castwatch，
      會認成「別人的 patch」而拒裝，不會來「修復」我們的 hook。"""
    return f"""
    nop
    pushad
    mov edi, dword ptr [{wcnt:#x}]
    mov eax, edi
    and eax, {OWN_N - 1:#x}
    imul eax, eax, {OWN_SLOT:#x}
    add eax, {ring:#x}
    mov ebx, eax
    mov dword ptr [ebx], edi
    mov edx, dword ptr [esp+0x24]
    mov ecx, dword ptr [esp+0x28]
    mov dword ptr [ebx+4], ecx
    cmp ecx, {OWN_CAP:#x}
    jbe cok
    mov ecx, {OWN_CAP:#x}
    cok:
    test edx, edx
    jz done
    lea edi, [ebx+8]
    mov esi, edx
    cld
    rep movsb
    done:
    inc dword ptr [{wcnt:#x}]
    popad
    push ebp
    mov ebp, esp
    push ebx
    mov bl, byte ptr [ebp+0x10]
    push {cont:#x}
    ret
    """


def install_own(pid: int):
    """裝自己的 64-byte 錄包 hook。回 (pm, wcnt, ring, orig)；呼叫端 finally 還原。"""
    import keystone
    import pymem
    fn = castwatch.INBOUND_FN
    pm = pymem.Pymem()
    pm.open_process_from_id(pid)
    cur = bytes(pm.read_bytes(fn, castwatch.STOLEN))
    if cur != castwatch._EXPECT_PROLOGUE:
        raise RuntimeError(f"prologue 對不上 {cur.hex()}，不裝")
    block = pm.allocate(0x20000)
    wcnt = block
    ring = block + 64
    code = ring + OWN_N * OWN_SLOT
    pm.write_uint(wcnt, 0)
    ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
    ks.syntax = keystone.KS_OPT_SYNTAX_INTEL
    shell, _ = ks.asm(_own_stub(wcnt, ring, fn + castwatch.STOLEN), addr=code)
    pm.write_bytes(code, bytes(shell), len(shell))
    jmp = b"\xe9" + struct.pack("<i", code - (fn + 5))
    jmp += b"\x90" * (castwatch.STOLEN - len(jmp))
    _patch(pm, jmp)
    return pm, wcnt, ring, cur


def _patch(pm, data: bytes) -> None:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    fn = castwatch.INBOUND_FN
    old = wintypes.DWORD()
    k32.VirtualProtectEx(pm.process_handle, ctypes.c_void_p(fn),
                         castwatch.STOLEN, 0x40, ctypes.byref(old))
    pm.write_bytes(fn, data, len(data))
    k32.VirtualProtectEx(pm.process_handle, ctypes.c_void_p(fn),
                         castwatch.STOLEN, old.value, ctypes.byref(wintypes.DWORD()))


def scene_srv(sc, eid):
    """怪的場景實體 +0x1D0（伺服器編號候選）。認不出回 0。"""
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
    return u32(sc, ptr + castwatch.SRV_ID_OFF)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--secs", type=float, default=120.0)
    args = ap.parse_args()

    cs = clients()
    if not cs:
        print("⛔ 找不到遊戲視窗"); return 2
    if not args.pid:
        for pid, name in cs:
            sc = MemoryScanner(); sc.open(pid); locate.warm(sc)
            _, why = hook_ring(sc)
            print(f"  pid={pid:<6} {name:<12} {why}")
        print("用 --pid 挑一台「工具箱 hook 在」的")
        return 0

    pid = args.pid
    name = dict(cs).get(pid, "?")
    sc = MemoryScanner()
    sc.open(pid)
    locate.warm(sc)
    ring_info, why = hook_ring(sc)
    own = None
    if ring_info:
        wcnt_addr, ring = ring_info
        N, SLOT, CAP, DOFF = castwatch._N, castwatch._SLOT, castwatch._CAP, 8
    elif why.startswith("沒 hook"):
        own = install_own(pid)
        _pm, wcnt_addr, ring, _orig = own
        N, SLOT, CAP, DOFF = OWN_N, OWN_SLOT, OWN_CAP, 8
        why = "裝自己的 64-byte hook（結束還原）"
    else:
        print(f"⛔ {why}"); return 2
    try:
        return _run(sc, pid, name, args, wcnt_addr, ring, N, SLOT, CAP, DOFF, why)
    finally:
        if own is not None:
            _patch(own[0], own[3])
            back = bytes(own[0].read_bytes(castwatch.INBOUND_FN, castwatch.STOLEN))
            print("hook 已還原" if back == own[3] else f"⛔ 還原失敗 {back.hex()}")


def _run(sc, pid, name, args, wcnt_addr, ring, N, SLOT, CAP, DOFF, why) -> int:

    my_ent = bag.player_entity(sc)
    my_srv = castwatch.own_server_id(sc, my_ent) or 0
    my_eid = u32(sc, my_ent + bag.OFF_ENT_ID) if my_ent else 0
    base = player.locate_fast(sc)
    st0 = player.read(sc, base) if base else None
    if st0 is None:
        print("⛔ 讀不到角色屬性"); return 2
    state, player_obj, ents, hot, _ = entity.snapshot(sc)
    if not state:
        state = entity.locate_state(sc)
    print(f"[{pid}] {name}  我 srv={my_srv:#x} eid={my_eid:#x} exp={st0.exp}  {why}")
    print(f"錄 {args.secs:.0f} 秒 …… 照常掛機就好")

    # 封包紀錄：[(t, seq, bytes16)]
    pkts: list[tuple[float, int, bytes]] = []
    wc_prev = u32(sc, wcnt_addr)
    # 事件：[(t, kind, eid, note)]
    events: list[tuple[float, str, int, str]] = []
    mon_state: dict[int, str] = {}
    mon_name: dict[int, str] = {}
    mon_srv: dict[int, int] = {}
    exp_prev = st0.exp
    tgt_prev = 0
    t_start = time.monotonic()
    t_end = t_start + args.secs
    full_t = 0.0
    live_t = 0.0
    ticks = 0

    while time.monotonic() < t_end:
      try:
        t0 = time.monotonic()
        ticks += 1
        # ── 新封包 ──
        wc = u32(sc, wcnt_addr)
        if wc != wc_prev:
            lo = max(wc_prev, wc - N)
            for i in range(lo, wc):
                s = ring + (i % N) * SLOT
                raw = sc._read_bytes(s, SLOT)
                if not raw or len(raw) < SLOT:
                    continue
                raw = bytes(raw)
                seq = struct.unpack_from("<I", raw, 0)[0]
                plen = struct.unpack_from("<I", raw, 4)[0] if DOFF == 8 else CAP
                pkts.append((t0, seq, raw[DOFF:DOFF + min(CAP, plen)]))
            wc_prev = wc
        # ── 我的經驗 ──
        st = player.read(sc, base) if base else None
        if st is not None and st.exp != exp_prev:
            events.append((t0, "EXP", 0, f"{exp_prev}→{st.exp} (+{st.exp - exp_prev})"))
            exp_prev = st.exp
        # ── 目標 ──
        tid = 0
        if state:
            ok, tid, thp = entity.read_target_checked(sc, state)
            if not ok:
                tid = 0
        if tid != tgt_prev:
            events.append((t0, "TGT", tid, f"目標 {tgt_prev:#x}→{tid:#x}"))
            tgt_prev = tid
        # ── 怪的狀態 ──
        if t0 - full_t > FULL_EVERY:
            state, player_obj, ents, hot, _ = entity.snapshot(sc)
            full_t = t0
        else:
            state, player_obj, ents, hot, _ = entity.snapshot(sc, regions=hot)
        seen = set()
        for m in ents:
            if not m.is_monster:
                continue
            seen.add(m.eid)
            alive, stt, _p, flag = entity.read_live(sc, m)
            if m.eid not in mon_state:
                mon_name[m.eid] = m.name
                mon_srv[m.eid] = scene_srv(sc, m.eid)
            prev = mon_state.get(m.eid, "")
            now_dead = entity.looks_dead(stt, alive, flag)
            if now_dead and prev != "Dead":
                kind = "DIE_TGT" if m.eid == tid else "DIE_OTHER"
                events.append((t0, kind, m.eid,
                               f"{m.name} st={stt!r} flag={flag} srv={mon_srv.get(m.eid, 0):#x}"))
                mon_state[m.eid] = "Dead"
            elif not now_dead:
                mon_state[m.eid] = stt or "?"
        for eid in list(mon_state):
            if eid not in seen:
                events.append((t0, "GONE", eid,
                               f"{mon_name.get(eid, '?')} 上次狀態={mon_state[eid]}"))
                del mon_state[eid]
        if t0 - live_t >= 2.0:
            live_t = t0
            print(f"  {t0 - t_start:5.0f}s 封包{len(pkts):<5} 事件{len(events):<4} "
                  f"exp={exp_prev} 目標={tid & 0xFFFF:#x}")
        left = HZ - (time.monotonic() - t0)
        if left > 0:
            time.sleep(left)
      except KeyboardInterrupt:
        break

    # ── 分析 ──
    def ids_in(data: bytes, eid: int) -> str:
        tags = []
        cands = {eid: "eid", eid & 0xFFFF: "eid16"}
        if mon_srv.get(eid):
            cands[mon_srv[eid]] = "srv"
        if my_srv:
            cands[my_srv] = "我srv"
        if my_eid:
            cands[my_eid] = "我eid"
        for off in range(0, len(data) - 3):
            v = struct.unpack_from("<I", data, off)[0]
            if v in cands and v:
                tags.append(f"@{off}={cands[v]}")
        for off in range(0, len(data) - 1):
            v = struct.unpack_from("<H", data, off)[0]
            if v and v == (eid & 0xFFFF):
                tags.append(f"@{off}=eid16")
        return " ".join(tags)

    exp_ts = [t for t, k, _, _ in events if k == "EXP"]
    die_tgt = [(t, e) for t, k, e, _ in events if k == "DIE_TGT"]
    die_oth = [(t, e) for t, k, e, _ in events if k == "DIE_OTHER"]

    def near(ts, t, w=WIN_AFTER):
        return any(abs(t - x) <= w for x in ts)

    stat = defaultdict(lambda: [0, 0, 0, 0])   # (op,sub) → [總, 近EXP, 近我目標死, 近別人死]
    for t, seq, d in pkts:
        if len(d) < 8:
            continue
        op = struct.unpack_from("<H", d, 0)[0]
        sub = struct.unpack_from("<H", d, 6)[0]
        k = (op, sub)
        stat[k][0] += 1
        if near(exp_ts, t):
            stat[k][1] += 1
        if near([x for x, _ in die_tgt], t):
            stat[k][2] += 1
        if near([x for x, _ in die_oth], t):
            stat[k][3] += 1

    os.makedirs("reports", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        def w(s=""):
            f.write(s + "\n")
        w(f"# killattr_probe pid={pid} {name} {time.strftime('%Y-%m-%d %H:%M:%S')}")
        w(f"我 srv={my_srv:#x} eid={my_eid:#x}  取樣 {ticks} 拍  封包 {len(pkts)}  "
          f"EXP漲 {len(exp_ts)} 次  目標死 {len(die_tgt)}  別隻死 {len(die_oth)}")
        w()
        w("## (op,sub) 統計：總次數 / 距EXP漲±0.6s / 距我目標死±0.6s / 距別隻死±0.6s")
        for (op, sub), (n, a, b, c) in sorted(stat.items(), key=lambda kv: (-kv[1][1], -kv[1][0])):
            w(f"  op={op:#06x} sub={sub:#06x}  總={n:<5} EXP={a:<4} 目標死={b:<4} 別隻死={c}")
        w()
        w("## 事件時間線（每個事件列前後 0.6 秒的封包）")
        for t, kind, eid, note in events:
            w(f"[{t - t_start:8.3f}] {kind:<9} {eid:#x} {note}")
            if kind in ("TGT",):
                continue
            for pt, seq, d in pkts:
                if -WIN_BEFORE <= pt - t <= WIN_AFTER:
                    op = struct.unpack_from("<H", d, 0)[0] if len(d) >= 2 else -1
                    sub = struct.unpack_from("<H", d, 6)[0] if len(d) >= 8 else -1
                    w(f"      {pt - t:+.3f}s #{seq} op={op:#06x} sub={sub:#06x} "
                      f"{d.hex(' ')}  {ids_in(d, eid)}")
    print(f"寫好了：{OUT}  封包{len(pkts)} EXP漲{len(exp_ts)} 目標死{len(die_tgt)} 別隻死{len(die_oth)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
