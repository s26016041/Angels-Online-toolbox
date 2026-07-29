"""玩家物件定位器：把你在遊戲畫面上看到的數值一次餵進來，找出它們同住的那塊結構。

為什麼要這樣找
--------------
單獨掃「等級 87」會中幾十萬筆，毫無用處。但如果要求「**金幣、經驗、等級、HP
必須落在同一塊 4KB 記憶體內**」，巧合幾乎不可能發生 —— 剩下的那個地方就是
玩家物件（player struct）。找到它，之後只要用角色名當錨點定位一次，所有欄位
都能靠固定偏移一次讀齊，不必每個值分開掃。

這推翻不了「值本身無靜態指標路徑」的舊結論，但它要解決的是另一件事：
**把每次啟動的定位從「手動掃好幾輪」變成「一鍵掃一次、全部欄位到手」。**

兩階段掃描（避免被小數字的海量命中淹沒）
----------------------------------------
第一階段：只掃「高鑑別度錨點」= 角色名 + 數值 >= --rare-min 的欄位（金幣、經驗
          常是七八位數）。這種樣式在整個記憶體裡通常只有個位數命中。
第二階段：把第一階段的命中就近合併成幾個候選區塊，只在這些小窗口裡搜「所有」
          錨點（含等級、HP 這種小數字）。窗口很小，小數字再常見也不會爆。
最後依「命中幾個不同錨點」排序 —— 對得越齊，越可能是真正的玩家物件。

每個數值都會同時試 int32 / int16 / int64 / float / double，因為事前不知道遊戲
用哪種存（已知 HP 是 float，其他未定）。純讀記憶體，不寫入、不注入、不掛除錯器。

用法（在專案根目錄、遊戲已登入進場後執行）
------------------------------------------
    py tools/find_player_struct.py --name 角色名 --level 87 --exp 12345678 ^
        --gold 8642150 --hp 24500 --maxhp 24500 --mp 12300 --maxmp 12300

    只給部分欄位也行，但**至少要有一個高鑑別度錨點**（--name 或一個大數字）。
    其他欄位用 --extra 自己加，例如：--extra 經驗球1:1746 --extra 背包球:4

    --pid N        指定分身（多開時；省略則自動找，只有一個就直接用）
    --window 4096  判定「住在一起」的距離，預設 4KB
    --top 5        最多印幾個候選
    --dump         把最佳候選的前後記憶體存成 .bin，供後續分析

重要：跑之前讓角色**站著不動**，數值才不會在掃描途中變掉（經驗、HP 尤其）。
掃描要花幾秒到幾十秒，中途值變了那個錨點就會沒命中 —— 沒關係，工具會列出
哪些錨點沒中，其他錨點照樣能定位。

驗證（這步不能省）：找到偏移後，**換另一個分身、另一個角色再跑一次**，
如果兩邊算出來的相對偏移一模一樣，這組偏移才是真的可以寫死進產品的。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 主控台是 cp950，中文輸出要先把 stdout 轉成 utf-8（見記憶：env-run-commands）。
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from app.core import window as win
from app.core.memory import MemoryScanner

# 數值大於等於這個門檻，才算「高鑑別度」、能拿來當第一階段的錨點。
# 七八位數的金幣/經驗遠比「等級 87」稀有，用它們開路才不會掃出幾十萬筆。
DEFAULT_RARE_MIN = 10000

# 字串錨點要試的編碼（遊戲文字多為 UTF-16LE；mbcs 在繁中系統即 Big5）。
NAME_ENCODINGS = ("utf-16-le", "mbcs", "utf-8")

# 單一錨點在第一階段命中超過這個數量就放棄它（代表它根本不稀有，留著只會拖慢）。
MAX_RARE_HITS = 4000

# 第一階段只用這麼長以上的樣式。2 bytes 的 int16 在全記憶體會命中幾十萬次，
# 光是收集就先把記憶體吃光；這種短樣式留到第二階段的小窗口裡再比對。
MIN_RARE_PATTERN = 4


def _pad(text: str, width: int) -> str:
    """靠左補到指定顯示寬度。中文是全形佔兩格，用 len() 對齊會歪掉。"""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - w)


class Anchor:
    """一個要找的欄位：標籤 + 它可能的位元組樣式（同一個值有多種存法）。"""

    def __init__(self, label: str, patterns: list[tuple[bytes, str]], rare: bool):
        self.label = label
        self.patterns = patterns  # [(位元組樣式, 型別名), ...]
        self.rare = rare          # 是否夠稀有、可當第一階段錨點


def _int_patterns(v: int) -> list[tuple[bytes, str]]:
    """一個整數在記憶體裡可能的所有存法。事前不知道遊戲用哪種，全部都試。"""
    out: list[tuple[bytes, str]] = []
    if -(2**31) <= v < 2**31:
        out.append((struct.pack("<i", v), "int32"))
    if -(2**15) <= v < 2**15:
        out.append((struct.pack("<h", v), "int16"))
    if -(2**63) <= v < 2**63:
        out.append((struct.pack("<q", v), "int64"))
    out.append((struct.pack("<f", float(v)), "float"))
    out.append((struct.pack("<d", float(v)), "double"))
    return out


def _name_patterns(name: str) -> list[tuple[bytes, str]]:
    out: list[tuple[bytes, str]] = []
    for enc in NAME_ENCODINGS:
        try:
            pat = name.encode(enc, errors="strict")
        except Exception:
            continue
        if pat and (pat, enc) not in out:
            out.append((pat, enc))
    return out


def build_anchors(args) -> list[Anchor]:
    """把命令列參數轉成錨點清單。"""
    anchors: list[Anchor] = []
    if args.name:
        pats = _name_patterns(args.name)
        if pats:
            anchors.append(Anchor("角色名", pats, rare=True))

    fields = [
        ("等級", args.level),
        ("經驗", args.exp),
        ("金幣", args.gold),
        ("HP", args.hp),
        ("HP上限", args.maxhp),
        ("MP", args.mp),
        ("MP上限", args.maxmp),
    ]
    for label, value in fields:
        if value is None:
            continue
        anchors.append(
            Anchor(label, _int_patterns(value), rare=abs(value) >= args.rare_min)
        )

    for item in args.extra or []:
        if ":" not in item:
            print(f"⚠ --extra 格式應為 標籤:數值，已略過：{item}")
            continue
        label, _, val_s = item.partition(":")
        try:
            value = int(val_s)
        except ValueError:
            print(f"⚠ --extra 的數值不是整數，已略過：{item}")
            continue
        anchors.append(
            Anchor(label.strip(), _int_patterns(value), rare=abs(value) >= args.rare_min)
        )
    return anchors


def find_game_pids(account: str | None = None) -> list[tuple[int, str]]:
    """找出所有天使之戀分身，回傳 [(pid, 視窗標題), ...]。

    給 account 時只留標題含該帳號的分身（多開時免去自己查 PID）。
    """
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if w.pid in seen:
            continue
        if account and account.lower() not in w.title.lower():
            continue
        seen.add(w.pid)
        out.append((w.pid, w.title))
    return out


# ---------------------------------------------------------------------------
# 第一階段：全記憶體掃「高鑑別度錨點」
# ---------------------------------------------------------------------------
def scan_rare(sc: MemoryScanner, anchors: list[Anchor], writable_only: bool):
    """單趟掃過所有區段，找出稀有錨點的命中。回傳 {標籤: [(位址, 型別), ...]}。"""
    rare = [a for a in anchors if a.rare]
    hits: dict[str, list[tuple[int, str]]] = {a.label: [] for a in rare}
    dropped: set[str] = set()
    # 只留夠長的樣式；某錨點的樣式全被濾掉就沒得掃了，直接排除。
    pats = {a.label: [p for p in a.patterns if len(p[0]) >= MIN_RARE_PATTERN] for a in rare}
    for a in rare:
        if not pats[a.label]:
            dropped.add(a.label)

    regions = sc._iter_regions(writable_only)
    total = len(regions) or 1
    scanned_mb = 0.0
    for i, (base, size) in enumerate(regions):
        raw = sc._read_region(base, size)
        if raw:
            scanned_mb += len(raw) / (1024 * 1024)
            for a in rare:
                if a.label in dropped:
                    continue
                bucket = hits[a.label]
                for pat, kind in pats[a.label]:
                    start = 0
                    while len(bucket) <= MAX_RARE_HITS:  # 超量就停手，別把記憶體吃光
                        j = raw.find(pat, start)
                        if j < 0:
                            break
                        bucket.append((base + j, kind))
                        start = j + 1
                if len(bucket) > MAX_RARE_HITS:
                    dropped.add(a.label)
                    hits[a.label] = []
        if i % 40 == 0 or i == total - 1:
            pct = (i + 1) / total * 100
            print(f"\r  掃描中… {pct:5.1f}%（已讀 {scanned_mb:.0f} MB）", end="", flush=True)
    print()
    for label in dropped:
        print(
            f"  ⚠「{label}」樣式太短或命中過多（>{MAX_RARE_HITS}），不夠稀有，"
            "已排除出第一階段（第二階段仍會比對）。"
        )
    return hits


def cluster(addrs: list[int], window: int) -> list[tuple[int, int]]:
    """把相近的位址合併成區塊，回傳 [(起, 迄), ...]。相距在 window 內的算同一塊。"""
    if not addrs:
        return []
    ordered = sorted(set(addrs))
    out: list[tuple[int, int]] = []
    lo = hi = ordered[0]
    for a in ordered[1:]:
        if a - hi <= window:
            hi = a
        else:
            out.append((lo, hi))
            lo = hi = a
    out.append((lo, hi))
    return out


# ---------------------------------------------------------------------------
# 第二階段：在候選區塊裡找「全部」錨點
# ---------------------------------------------------------------------------
def probe_cluster(sc: MemoryScanner, lo: int, hi: int, anchors: list[Anchor], window: int):
    """讀出區塊前後各一個 window 的內容，在裡面搜所有錨點。

    回傳 (區塊起始位址, {標籤: [(位址, 型別), ...]}, 原始位元組)。
    """
    start = max(0, lo - window)
    size = (hi - lo) + window * 2
    raw = sc._read_bytes(start, size)
    if raw is None:
        return start, {}, b""
    found: dict[str, list[tuple[int, str]]] = {}
    for a in anchors:
        for pat, kind in a.patterns:
            j = 0
            while True:
                j = raw.find(pat, j)
                if j < 0:
                    break
                found.setdefault(a.label, []).append((start + j, kind))
                j += 1
    return start, found, raw


def main() -> int:
    ap = argparse.ArgumentParser(
        description="玩家物件定位器：用畫面上的數值找出它們同住的結構"
    )
    ap.add_argument("--pid", type=int, help="指定分身 PID（多開時；省略則自動找）")
    ap.add_argument(
        "--account",
        help="用帳號指定分身（比對視窗標題，例如 s26016041），免去自己查 PID",
    )
    ap.add_argument("--name", help="角色名（最強的錨點，強烈建議給）")
    ap.add_argument("--level", type=int, help="等級")
    ap.add_argument("--exp", type=int, help="經驗值（畫面上的整數，不要填百分比）")
    ap.add_argument("--gold", type=int, help="金幣")
    ap.add_argument("--hp", type=int, help="目前 HP")
    ap.add_argument("--maxhp", type=int, help="HP 上限")
    ap.add_argument("--mp", type=int, help="目前 MP")
    ap.add_argument("--maxmp", type=int, help="MP 上限")
    ap.add_argument(
        "--extra", action="append", metavar="標籤:數值", help="自訂欄位，可重複給"
    )
    ap.add_argument(
        "--window", type=int, default=4096, help="判定『住在一起』的距離（預設 4096）"
    )
    ap.add_argument(
        "--rare-min",
        type=int,
        default=DEFAULT_RARE_MIN,
        help=f"數值多大才算高鑑別度錨點（預設 {DEFAULT_RARE_MIN}）",
    )
    ap.add_argument("--top", type=int, default=5, help="最多印幾個候選（預設 5）")
    ap.add_argument(
        "--all-regions",
        action="store_true",
        help="連唯讀區段也掃（預設只掃可寫區段，玩家資料通常在可寫的堆積）",
    )
    ap.add_argument("--dump", action="store_true", help="把最佳候選的記憶體存成 .bin")
    args = ap.parse_args()

    anchors = build_anchors(args)
    if not anchors:
        ap.print_help()
        print("\n請至少給一個欄位，例如 --name 角色名 --gold 8642150。")
        return 1
    if not any(a.rare for a in anchors):
        print(
            "沒有任何高鑑別度錨點。請至少給 --name，或一個大數字欄位"
            f"（>= {args.rare_min}，例如金幣或經驗）。\n"
            "只給等級、HP 這種小數字的話，全記憶體會有幾十萬筆命中，無法定位。"
        )
        return 1

    # --- 選定分身 ---
    if args.pid:
        pid = args.pid
    else:
        found = find_game_pids(args.account)
        if not found:
            who = f"（帳號含 {args.account}）" if args.account else ""
            print(
                f"找不到天使之戀視窗{who}。請確認遊戲已登入進場；"
                "若遊戲以系統管理員執行，本工具也要。"
            )
            return 1
        if len(found) > 1:
            print("偵測到多個分身，請用 --account 或 --pid 指定要掃哪一個：")
            for p, t in found:
                print(f"  --pid {p}   {t}")
            return 1
        pid, title = found[0]
        print(f"目標分身：PID {pid}｜{title}")

    sc = MemoryScanner()
    try:
        sc.open(pid)
    except Exception as e:
        print(f"開啟程序失敗：{e}")
        return 1

    try:
        rare_labels = [a.label for a in anchors if a.rare]
        other_labels = [a.label for a in anchors if not a.rare]
        print(f"高鑑別度錨點：{'、'.join(rare_labels)}")
        if other_labels:
            print(f"輔助錨點（只在候選區塊內比對）：{'、'.join(other_labels)}")

        print("\n[第一階段] 全記憶體掃高鑑別度錨點…")
        hits = scan_rare(sc, anchors, writable_only=not args.all_regions)
        for label, lst in hits.items():
            print(f"  {label}：{len(lst)} 筆命中")
        all_addrs = [addr for lst in hits.values() for addr, _ in lst]
        if not all_addrs:
            print(
                "\n第一階段完全沒命中。可能原因：\n"
                "  1. 數值填錯，或掃描途中值變了（經驗/HP 會跳動）→ 讓角色站著不動再試。\n"
                "  2. 遊戲把值加密/編碼後才存 → 那就得換策略（先確認角色名有沒有命中）。\n"
                "  3. 值在唯讀區段 → 加 --all-regions 再試一次。"
            )
            return 1

        blocks = cluster(all_addrs, args.window)
        print(f"\n[第二階段] 合併成 {len(blocks)} 個候選區塊，逐一比對全部錨點…")

        scored = []
        for lo, hi in blocks:
            start, found, raw = probe_cluster(sc, lo, hi, anchors, args.window)
            if not found:
                continue
            scored.append((len(found), start, found, raw))
        scored.sort(key=lambda x: -x[0])

        total_anchors = len(anchors)
        print(f"\n{'='*66}")
        print(f"結果：共 {len(scored)} 個候選，依對齊的錨點數排序（滿分 {total_anchors}）")
        print("=" * 66)

        for rank, (n, start, found, raw) in enumerate(scored[: args.top], 1):
            # 基準點：優先用角色名的位置，沒有就用最小位址。
            if "角色名" in found:
                ref = found["角色名"][0][0]
                ref_note = "（以角色名為基準）"
            else:
                ref = min(a for lst in found.values() for a, _ in lst)
                ref_note = "（以最前面的命中為基準）"
            missing = [a.label for a in anchors if a.label not in found]

            print(f"\n### 候選 #{rank}：對齊 {n}/{total_anchors} 個錨點｜基準 0x{ref:X} {ref_note}")
            print(f"{_pad('錨點', 12)}{_pad('型別', 22)}{_pad('絕對位址', 18)}相對偏移")
            print("-" * 62)
            # 同一個位址常會同時吻合 int16/int32/int64（小數字後面接零），
            # 併成一列並列出所有型別，比一個位址印三遍好讀。
            merged: dict[tuple[str, int], list[str]] = {}
            for label, lst in found.items():
                for addr, kind in lst:
                    merged.setdefault((label, addr), []).append(kind)
            rows = sorted(
                ((addr - ref, label, "/".join(kinds), addr)
                 for (label, addr), kinds in merged.items()),
                key=lambda r: (r[0], r[1]),
            )
            for off, label, kinds, addr in rows:
                sign = "+" if off >= 0 else "-"
                print(
                    f"{_pad(label, 12)}{_pad(kinds, 22)}"
                    f"{_pad(f'0x{addr:08X}', 18)}{sign}0x{abs(off):X}"
                )
            if missing:
                print(f"未命中：{'、'.join(missing)}")

            if args.dump and rank == 1 and raw:
                path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scratchpad",
                    f"player_struct_{pid}_{start:X}.bin",
                )
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(raw)
                print(f"→ 已存 {len(raw)} bytes 到 {path}（起始位址 0x{start:X}）")

        best = scored[0][0] if scored else 0
        print(f"\n{'='*66}")
        if best >= 3:
            print("看起來有戲：有候選同時對上 3 個以上的欄位，這很難是巧合。")
            print("下一步（必做）：換另一個分身／另一個角色，用它的數值再跑一次。")
            print("　　　　　　　　兩邊的相對偏移若完全一致 → 這組偏移可以寫死進產品。")
        else:
            print("對齊的錨點偏少，還不能下定論。可以試：")
            print("  --window 16384   放寬『住在一起』的距離（結構可能比較鬆散）")
            print("  --all-regions    連唯讀區段一起掃")
            print("  確認數值沒填錯、掃描途中角色沒動。")
        return 0
    finally:
        sc.close()


if __name__ == "__main__":
    sys.exit(main())
