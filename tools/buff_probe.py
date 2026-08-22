"""身上生效中的輔助技能（buff）清單 —— 純唯讀探針，不送任何封包。

    py tools\\buff_probe.py            列一次就好
    py tools\\buff_probe.py 20         再做 20 秒時間軸取樣（找「剩餘時間」欄位）

## 為什麼相信 +0x420 是 buff 清單（這次不是取樣猜的，是遊戲自己的碼）

官方「天使精靈」的**自動輔助**（Lua 全域 `DATAID_USE_ASSIST_CHECK`=2051 /
`DATAID_USE_ASSIST_LIST`=2060）要判斷「這招我身上還有沒有」才知道要不要補，
那段判斷就在精靈指令表裡（`reports/robot_cmds.txt`）：

    cmd 0x1c0 → 0x554098(技能ID) -> bool     我身上有沒有這個 buff
    cmd 0x1c1 → 0x553f6d(實體ID, 技能ID)     指定對象身上有沒有
    cmd 0x1c2 → 0x5540e4(道具/魔法ID)        先查表 +0xEC 再比
    cmd 0x1c3 → 0x553fc7(實體ID, 魔法ID)

0x554098 反組譯（reports/supply_ai_disasm.txt）：

    ecx = [0x96E618]                     世界管理員
    push [ecx+0x2A90]                    ← 自己的實體 ID
    call 0x5045DE                        ← 依 ID 找實體，eax = 實體物件
    esi = [eax+0x420]                    ← **buff 清單**（哨兵指標）
    eax = [esi]                          ← 第一個節點（== esi 就是空的）
    loop: cmp [eax+0x10], edi            ← **節點 +0x10 = 技能 ID**
          je  → return 1
          call 0x507345                  ← ++it
          cmp eax, esi / jne loop
    return 0

→ 走訪法：`哨兵 = [實體+0x420]`、節點 `+0x00=next`、`+0x10=ID`，繞回哨兵為止。
  那支只回 bool，清單自己走就能列出**全部**生效中的效果。

⚠⚠ 兩條清單只差 8 bytes，基底搞錯就會把 A 當成 B：
  `+0x418` 是施放/動畫中的效果（存活＝後置時間，短命，[[skill-data-and-buff]]
  已否決它當狀態來源），`+0x420` 才是身上生效中的。所以這支**兩條一起倒**、
  基底固定用 `bag.player_entity()`（＝反組譯 `0x5045DE` 回傳的同一個物件），
  不做「哪個走得通就用哪個」那種猜法。

節點裡有沒有「剩餘時間」（舊結論：11 條死路都沒找到）—— 現在有節點位址了，
時間軸取樣直接看哪個欄位會動。
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.core import charname, window as win
from app.core.memory import MemoryScanner
from app.game import bag, buffs, locate, skills

# ⚠ 版面（哨兵／節點偏移、走訪法）**只住在 app/game/buffs.py**
#   （CLAUDE.md：同一個位址不准在第二個地方再寫一次）。這支只多做一件
#   產品不需要的事：把節點原始 bytes 倒出來找沒解讀的欄位。
OFF_CAST_LIST = 0x418        # 施放/動畫中的效果（短命，產品沒在用）
OFF_BUFF_LIST = buffs.OFF_TREE
NODE_SKILL = buffs.NODE_KEY
NODE_SPAN = 0x40             # 一個節點倒多少 bytes 出來看（產品只讀到 +0x18）
MAX_NODES = buffs.MAX_NODES
OUT = os.path.join("reports", "buff_probe.txt")
LISTS = ((OFF_CAST_LIST, "施放/動畫清單"), (OFF_BUFF_LIST, "身上 buff 樹"))

_ok = buffs._ok
u32 = buffs._u32


def walk(sc, ent: int, off: int):
    """走 [ent+off] 那棵紅黑樹，回 (哨兵, [(節點位址, ID, raw)], 說明)。

    走訪法本身借 `buffs` 的（中序後繼），這裡只是每顆節點多讀 NODE_SPAN
    bytes 出來看。⚠ `+0x418` 跟 `+0x420` 是**兩棵不同的樹**，差 8 bytes，
    基底搞錯就會把 A 當成 B —— 基底一律用 `bag.player_entity()`
    （＝反組譯 `0x5045DE` 回傳的同一個物件），不做「哪個走得通就用哪個」。
    """
    head = u32(sc, ent + off)
    if not _ok(head):
        return head, [], "哨兵指標不像位址"
    cur = u32(sc, head + buffs.NODE_LEFT)        # 哨兵 +0x00 ＝ begin()
    if cur is None:
        return head, [], "哨兵讀不到"
    if cur == head:
        return head, [], "空的"
    out, seen = [], set()
    while _ok(cur) and cur != head and len(out) < MAX_NODES:
        if cur in seen:
            return head, out, "節點繞圈（樹正在被改？）"
        seen.add(cur)
        blob = sc._read_bytes(cur, NODE_SPAN)
        if not blob or len(blob) < NODE_SPAN:
            return head, out, f"節點 {cur:#x} 讀不到"
        b = bytes(blob)
        out.append((cur, struct.unpack_from("<I", b, NODE_SKILL)[0], b))
        right = struct.unpack_from("<I", b, buffs.NODE_RIGHT)[0]
        if right != head and _ok(right):         # ++it：右子的最左
            cur = right
            while True:
                left = u32(sc, cur + buffs.NODE_LEFT)
                if left is None or left == head or not _ok(left):
                    break
                cur = left
        else:                                    # 往上爬到「自己是左子」
            while True:
                parent = u32(sc, cur + buffs.NODE_PARENT)
                if parent is None or not _ok(parent) or parent == head:
                    cur = head
                    break
                if u32(sc, parent + buffs.NODE_RIGHT) != cur:
                    cur = parent
                    break
                cur = parent
    if cur != head:
        return head, out, "沒走回哨兵（超過上限或位址壞掉）"
    return head, out, ""


def show(sid: int) -> str:
    name = skills.name_of(sid) or "?"
    info = skills.of(sid)
    dur = f"，表定持續 {info.secs:.0f} 秒" if info else "，表裡查不到持續時間"
    return f"{name}{dur}"


def tick() -> int:
    """Windows GetTickCount()（開機到現在的毫秒）。"""
    return ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF


def stamps(words, secs: float) -> list[str]:
    """哪些欄位「像時間戳」—— 三種時基各驗一次，命中才報。

    ★ 找「到期時間」最直接的一刀：`+X 秒`＝還剩幾秒、`−X 秒`＝幾秒前開始。
    ⚠ 不預設遊戲用哪一種時基：Unix 秒／Unix 毫秒／開機 tick 毫秒都比對。
    """
    now_u = int(time.time())
    now_t = tick()
    out = []
    for i, w in enumerate(words):
        tag = f"+{i * 4:#04x}"
        for base, unit, name in ((now_u, 1, "Unix秒"),
                                 (now_u * 1000 & 0xFFFFFFFF, 1000, "Unix毫秒"),
                                 (now_t, 1000, "開機tick")):
            d = (w - base) / unit
            if -86400 <= d <= 86400:
                note = ""
                if secs and 0 < d <= secs + 5:
                    note = f"　← 剩餘 ≤ 表定 {secs:.0f} 秒，**像到期時間**"
                elif secs and -secs - 5 <= d < 0:
                    note = f"　← 在表定 {secs:.0f} 秒內，**像開始時間**"
                out.append(f"{tag}({name}) 離現在 {d:+.1f} 秒{note}")
    return out


def dump(sc, write, ent: int, off: int, label: str) -> int:
    head, nodes, why = walk(sc, ent, off)
    write("")
    write(f"  == +{off:#05x} {label}　哨兵 {(head or 0):#x} ==")
    # ★ 哨兵自己也倒出來：+0x00(next) 與 +0x04/+0x08(prev) 指向**不同**節點，
    #   就是「清單不只一個」的鐵證；一樣＝真的只有一個，不是我走漏了。
    hb = sc._read_bytes(head, 16) if _ok(head) else None
    if hb and len(hb) == 16:
        h0, h4, h8, hc = struct.unpack("<4I", bytes(hb))
        same = "（三個都一樣＝單節點）" if h0 == h4 == h8 else "（指向不同 → 多節點）"
        write(f"   哨兵內容 +0x00={h0:#x} +0x04={h4:#x} +0x08={h8:#x} "
              f"+0x0c={hc}{same}")
    if why:
        write(f"   ⚠ {why}")
    for addr, sid, b in nodes:
        write(f"   節點 {addr:#x}  ID {sid:<6} {show(sid)}")
        words = struct.unpack_from("<16I", b, 0)
        write("     " + "  ".join(f"+{i * 4:#04x}={w}" for i, w in enumerate(words)))
        info = skills.of(sid)
        hit = stamps(words, float(info.secs) if info else 0.0)
        write("     時間戳候選：" + ("；".join(hit) if hit else "無"))
    if not nodes and not why:
        write("   （空）")
    return len(nodes)


def probe(sc, write, watch: float) -> int:
    ent = bag.player_entity(sc)
    if not ent:
        write("  讀不到自己的實體（沒進場？）")
        return 0
    write(f"  自己的實體 {ent:#x}（＝反組譯 0x5045DE 回傳的那個物件）")
    n = 0
    for off, label in LISTS:
        got = dump(sc, write, ent, off, label)
        if off == OFF_BUFF_LIST:
            n = got

    if watch > 0:
        # 時間軸取樣：哪條清單的節點活得久？欄位會不會動？
        # ⚠ 用 **ID** 當 key 對齊，不用節點位址 —— 節點會被配置器回收重用。
        write("")
        write(f"  == 時間軸取樣 {watch:.0f} 秒（每 0.5 秒一拍）==")
        for off, label in (LISTS[1],):        # 只盯 buff 清單（0x418 短命不必）
            series: dict[int, list[tuple[float, tuple]]] = {}
            t0 = time.monotonic()
            while time.monotonic() - t0 < watch:
                _, ns, _ = walk(sc, ent, off)
                now = time.monotonic() - t0
                for _a, sid, b in ns:
                    series.setdefault(sid, []).append(
                        (now, struct.unpack_from("<16I", b, 0)))
                time.sleep(0.5)
            write(f"   -- +{off:#05x} {label} --")
            if not series:
                write("      整段取樣都是空的")
                continue
            for sid, rows in series.items():
                write(f"      ID {sid} {show(sid)}　出現 {len(rows)} 拍"
                      f"（{rows[0][0]:.1f}s → {rows[-1][0]:.1f}s）")
                if len(rows) < 2:
                    continue
                first, last = rows[0][1], rows[-1][1]
                dt = rows[-1][0] - rows[0][0]
                moved = False
                for i in range(16):
                    vals = [r[1][i] for r in rows]
                    if len(set(vals)) == 1:
                        continue
                    moved = True
                    d = last[i] - first[i]
                    trend = ("單調遞減" if all(y <= x for x, y in zip(vals, vals[1:]))
                             else "單調遞增" if all(y >= x for x, y in zip(vals, vals[1:]))
                             else "跳動")
                    write(f"        +{i * 4:#04x}  {first[i]} → {last[i]}"
                          f"（差 {d}，{(d / dt if dt else 0):+.1f}/秒，{trend}）")
                if not moved:
                    write("        16 個欄位全程不動（到期戳記本來就不該動）")
                # ★ 到期戳記 +0x18 換算剩餘：應該**線性遞減**，斜率 −1.0/秒
                exp = first[6]
                r0 = exp - (time.time() - dt)
                r1 = exp - time.time()
                write(f"        +0x18={exp}（到期 Unix 秒）→ 剩餘 {r0:.0f} 秒"
                      f" → {r1:.0f} 秒（{(r1 - r0) / dt if dt else 0:+.2f}/秒）")
    return n


def clients():
    out, seen = [], set()
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" not in w.class_name or w.pid in seen:
            continue
        seen.add(w.pid)
        out.append((w.pid, charname.account_from_title(w.title)))
    return out


def main():
    watch = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    cs = clients()
    if not cs:
        print("找不到遊戲視窗")
        return
    os.makedirs("reports", exist_ok=True)
    lines: list[str] = []
    summary: list[str] = []

    def write(s=""):
        lines.append(s)

    for pid, acc in cs:
        sc = MemoryScanner()
        try:
            sc.open(pid)
            locate.warm(sc)
        except Exception as e:                              # noqa: BLE001
            write(f"### {acc} (pid {pid}) 接不上：{e}")
            continue
        write("")
        write(f"################ {acc} (pid {pid}) ################")
        try:
            n = probe(sc, write, watch)
            summary.append(f"{acc}：身上 {n} 個生效中的效果")
        except Exception as e:                              # noqa: BLE001
            write(f"  探測失敗：{e!r}")
            summary.append(f"{acc}：探測失敗 {e!r}")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    # 主控台只留結論，全量在檔案裡（CLAUDE.md 省 token 規定）
    for s in summary:
        print(s)
    print(f"全量寫到 {OUT}（{len(lines)} 行）")


if __name__ == "__main__":
    main()
