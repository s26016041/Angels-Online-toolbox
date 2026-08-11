"""改版體檢：遊戲更新之後，一支指令告訴你「哪裡壞了、新值是多少、特徵怎麼改」。

    py tools\\patch_doctor.py            # 開著遊戲（登入畫面就夠）
    py tools\\patch_doctor.py --image reports\\angel_image.bin   # 離線用抓下來的映像
    py tools\\patch_doctor.py --dump     # 順便把映像存起來（遊戲關了還能查）

## 為什麼要這支

2026-08-11 那次改版（見 memory `patch-2026-08-11`）花掉的時間，九成不是「修」，
是「找出到底哪裡壞了」——症狀只有一句「自動登入卡在等遊戲開好」，而真正的原因
是 `login.VT_LOGIN` 的特徵跟另一支建構函式撞號。這支把那段診斷全部自動化：

* 逐段特徵：OK / 改版位移（正常）/ **模糊命中** / **沒命中**
* 沒命中 → 自動找「最接近的那段程式碼」，列出哪幾個 byte 變了、反組譯給你看，
  並且**產生改好的 pattern**（已驗過唯一才會給）
* 模糊命中 → 列出所有候選的反組譯，算出「往後多蓋幾 bytes 才唯一」
* **名字定位當第二來源**：遊戲的 Lua 綁定表是 `{名字, 函式}` 成對，名字不會因為
  重編譯或位移而改變。20 支函式裡 8 支查得到，拿來交叉驗證／直接補答案。
* 位址稽核：專案裡有沒有「寫死了但不在 SIGS 裡」的遊戲位址（2026-08-11 就是
  這樣抓到 `login.SERVER_INDEX` 的漏網）
* 資料表戳記：UPDATE.PAK 換了要重新解包重跑 build 工具
* 進遊戲的話再跑一次執行時體檢（等同 selfcheck）

## 輸出

主控台只印結論與待辦；全量寫 `reports/patch_doctor.txt`，
可以直接貼進 locate.SIGS 的建議寫在 `reports/patch_doctor_fix.md`。

⚠ 純讀取：不寫遊戲記憶體、不呼叫遊戲函式、不注入。
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win                    # noqa: E402
from app.core.memory import MemoryScanner             # noqa: E402
from app.game import locate, tablestamp               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "reports", "patch_doctor.txt")
FIXFILE = os.path.join(ROOT, "reports", "patch_doctor_fix.md")
IMGFILE = os.path.join(ROOT, "reports", "angel_image.bin")

MODULE_LO, MODULE_HI = 0x400000, 0xA00000     # 位址稽核用的合理範圍

try:
    from capstone import CS_ARCH_X86, CS_MODE_32, Cs
    _md = Cs(CS_ARCH_X86, CS_MODE_32)
except Exception:                                          # noqa: BLE001
    _md = None


# ---------------------------------------------------------------------------
# 基本工具
# ---------------------------------------------------------------------------
def disasm(img: bytes, base: int, addr: int, n: int = 8) -> list[str]:
    """反組譯 n 道指令；沒裝 capstone 就退成十六進位傾印。"""
    off = addr - base
    if _md is None:
        return [f"  {addr:#010x}  " + img[off:off + 24].hex(" ")]
    out = []
    for ins in _md.disasm(img[off:off + n * 16], addr, count=n):
        out.append(f"  {ins.address:#010x}  {ins.bytes.hex():<20} "
                   f"{ins.mnemonic} {ins.op_str}")
    return out


def runs(mask: bytes, min_len: int = 4) -> list[tuple[int, int]]:
    """pattern 裡所有「連續固定位元組」的區段（起點, 長度），長度由大到小。"""
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if j - i >= min_len:
                out.append((i, j - i))
            i = j
        else:
            i += 1
    return sorted(out, key=lambda r: -r[1])


def near_misses(img: bytes, sig: bytes, mask: bytes,
                limit: int = 6) -> list[tuple[int, list[int]]]:
    """找「差幾個 byte 就中」的位置。

    做法：拿 pattern 裡每一段連續固定位元組當種子去找，對每個對齊點算固定位置
    有幾個不合。**改版重新編譯通常只動幾個 byte**（暫存器換了），所以總會有
    某一段種子完整存活 —— 這就是找回它的鉤子。
    回傳 [(位移, [不合的位置…])]，依不合數量排序。
    """
    seen: dict[int, list[int]] = {}
    for at, ln in runs(mask):
        seed = sig[at:at + ln]
        i = img.find(seed)
        while i >= 0:
            start = i - at
            if 0 <= start and start + len(sig) <= len(img) and start not in seen:
                bad = [k for k in range(len(sig))
                       if mask[k] and img[start + k] != sig[k]]
                if len(bad) <= max(6, len(sig) // 4):
                    seen[start] = bad
            i = img.find(seed, i + 1)
    return sorted(seen.items(), key=lambda kv: len(kv[1]))[:limit]


def to_pattern(sig: bytes, mask: bytes) -> str:
    """位元組＋遮罩 → `55 8B EC ?? ??` 這種字串（每 12 個換行方便貼）。"""
    toks = [f"{b:02X}" if m else "??" for b, m in zip(sig, mask)]
    lines = [" ".join(toks[i:i + 12]) for i in range(0, len(toks), 12)]
    return '"' + '"\n        " '.join(lines) + '"'


# ---------------------------------------------------------------------------
# 名字定位（Lua 綁定表）——第二個獨立來源
# ---------------------------------------------------------------------------
def _is_ident(b: bytes) -> bool:
    return (2 <= len(b) <= 40 and
            all(0x30 <= c <= 0x39 or 0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A
                or c == 0x5F for c in b))


def lua_bindings(img: bytes, base: int) -> dict[str, int]:
    """掃出 `{名字字串指標, 函式指標}` 成對的 Lua 綁定表。

    ★ 這是**不靠任何位元組樣式**的定位方式：名字是遊戲自己的 Lua API，
      它的腳本靠這些名字吃飯，改版重編譯也不會改名。
    """
    out: dict[str, int] = {}
    lo, hi = base, base + len(img)
    for i in range(0, len(img) - 8, 4):
        p1 = int.from_bytes(img[i:i + 4], "little")
        if not lo < p1 < hi:
            continue
        p2 = int.from_bytes(img[i + 4:i + 8], "little")
        if not lo < p2 < hi or img[p2 - base:p2 - base + 2] != b"\x55\x8b":
            continue
        s = img[p1 - base:p1 - base + 48].split(b"\x00")[0]
        if _is_ident(s):
            out.setdefault(s.decode(), p2)
    return out


def last_call(img: bytes, base: int, fn: int) -> int | None:
    """綁定函式裡**最後一個 call** 的目標＝真正做事的那支。

    前面那些 call 是「跟 Lua 取參數」的固定慣用碼（0x5339F6 那支）。
    2026-08-11 實測：usequickkey→USE_FN、talkaction→TALK_FN、
    useextrasaleslot→USE_ITEM_FN 都吻合。
    """
    if _md is None:
        return None
    off = fn - base
    tgt = None
    for ins in _md.disasm(img[off:off + 0x400], fn):
        if ins.mnemonic == "call" and ins.op_str.startswith("0x"):
            tgt = int(ins.op_str, 16)
        elif ins.mnemonic == "jmp" and ins.op_str.startswith("0x"):
            # 尾呼叫（`jmp 目標` 取代 call+ret）也算 —— setrobotisrun 就是這樣寫的
            t = int(ins.op_str, 16)
            if not fn <= t < fn + 0x400:
                return t
        elif ins.mnemonic == "ret":
            break              # ⚠ 一定要在這裡停：不停就會讀進下一支函式的 call
    return tgt


# 名字 → SIGS 裡的哪一段（2026-08-11 實測對出來的，不是猜的）。
# ⚠ 有些名字看起來不相干（giveup01 → 送包函式），那是因為那支 Lua 綁定最後
#   呼叫的正好是**共用的**泛用函式 —— 當錨一樣有效，但語意別會錯意。
# ⚠ 只收「取參數 → call → ret」那種**直線**綁定。有分支的（例如
#   adtradeselectchange）「最後一個 call」會隨走哪條路而變，試過、拿掉了。
# ⚠ 這裡是**第二個獨立來源**，不是取代 AOB：兩邊對不上就大聲報，讓人去看。
NAME_HINTS = {
    "quickbar.USE_FN": "usequickkey",
    "sell.TALK_FN": "talkaction",
    "recall.USE_ITEM_FN": "useextrasaleslot",
    "attack.SELECT_FN": "netcommand",
    "jumpmap.SEND_FN": "giveup01",
    "team.ACTION_FN": "grouppromote",
    "team.INVITE_FN": "groupinvite",
}


# ---------------------------------------------------------------------------
# 逐段診斷
# ---------------------------------------------------------------------------
class Finding:
    def __init__(self, sig, status, value=None, note="", detail=None,
                 suggest=None):
        self.sig, self.status, self.value = sig, status, value
        self.note, self.detail, self.suggest = note, detail or [], suggest

    @property
    def name(self) -> str:
        return f"{self.sig.module}.{self.sig.attr}"


def check_sig(img, base, size, s, names) -> Finding:
    sig, mask = locate._parse(s.pattern)
    m_full, m_only, targets = locate._auto_mask(
        sig, mask, s.known, base + 0x1000, base + size)

    def hits(m):
        h = locate._find_all(img, sig, m)
        return [x for x in h if locate.str_ok(img, base, x, s)]

    got = hits(m_full)
    tier = "全遮"
    if len(got) != 1:
        alt = hits(m_only)
        if len(alt) == 1:
            got, tier = alt, "只遮目標"

    if len(got) == 1:
        off = got[0]
        if s.kind == "fn":
            val = base + off
        else:
            k = off + (s.imm_at or 0)
            val = int.from_bytes(img[k:k + 4], "little")
        status = "OK" if val == s.known else "位移"
        if tier == "全遮":
            return Finding(s, status, val)
        # ⚠ 只有第二層唯一 = 這一段**下次改版一定壞**（第二層是拿舊位址當錨的，
        #   位址一移就沒了）。2026-08-11 的 login.VT_LOGIN 就是死在這裡。
        #   順手算出「往後多蓋幾 bytes 全遮就唯一」，當預防性修法給出去。
        ext = extend_until_unique(img, base, size, s, sig, mask, off)
        return Finding(s, status, val,
                       "　⚠ 只有『只遮目標』那層唯一 —— 那層拿舊位址當錨，"
                       "**下次改版必失效**",
                       suggest=("harden", [(base + off, *ext)] if ext else []))

    # --- 壞掉了：模糊命中 --------------------------------------------------
    if len(got) > 1:
        detail = [f"  {len(got)} 個候選，全遮之後分不出來："]
        for h in got[:6]:
            detail.append(f"  ── 候選 {base + h:#x}")
            detail += disasm(img, base, base + h, 6)
        # 往後延伸多少才唯一？（每個候選各算一次）
        sugg = []
        for h in got[:6]:
            ext = extend_until_unique(img, base, size, s, sig, mask, h)
            if ext:
                n, pat = ext
                sugg.append((base + h, n, pat))
        return Finding(s, "模糊", None, "特徵不夠獨特", detail,
                       ("extend", sugg))

    # --- 壞掉了：完全沒命中 ------------------------------------------------
    detail = ["  一個都沒中 —— 函式被重新編譯，或這段程式碼被改寫了。"]
    cands = near_misses(img, sig, mask)
    sugg = []
    for off, bad in cands:
        detail.append(f"  ── 最接近：{base + off:#x}，固定位元組有 {len(bad)} 個不同")
        for k in bad[:8]:
            detail.append(f"       第 {k} 個 byte：特徵寫 {sig[k]:02X}、"
                          f"現在是 {img[off + k]:02X}")
        detail += disasm(img, base, base + off, 8)
        # 依現況重新產生 pattern（保留原本的 ?? 位置），驗過唯一才給
        new_sig = bytearray(sig)
        for k in bad:
            new_sig[k] = img[off + k]
        new_sig = bytes(new_sig)
        val = (base + off if s.kind == "fn" else
               int.from_bytes(img[off + (s.imm_at or 0):
                                  off + (s.imm_at or 0) + 4], "little"))
        nf, no, _t = locate._auto_mask(new_sig, mask, val,
                                       base + 0x1000, base + size)
        uniq = len(locate._find_all(img, new_sig, nf)) == 1
        if uniq:
            sugg.append((base + off, val, to_pattern(new_sig, mask)))
    # 名字定位能不能直接給答案
    hint = NAME_HINTS.get(f"{s.module}.{s.attr}")
    named = None
    if hint and hint in names:
        named = last_call(img, base, names[hint])
        detail.append(f"  ★ 名字定位：lua『{hint}』→ 綁定 {names[hint]:#x} "
                      f"→ 最後一個 call = {named:#x}" if named else
                      f"  ★ 名字定位：找到 lua『{hint}』但讀不出目標")
    return Finding(s, "沒命中", None, "", detail, ("regen", sugg, named))


def extend_until_unique(img, base, size, s, sig, mask, hit, cap=48):
    """從某個候選往後多蓋幾 bytes 才唯一？回 (幾 bytes, 新 pattern)。

    ★ 這就是 2026-08-11 修 `login.VT_LOGIN` 的手法自動化：舊特徵只蓋到跟別人
      一樣的那幾行，往後多蓋兩行（只有它有）就唯一了。
    """
    for n in range(4, cap + 1, 2):
        end = hit + len(sig) + n
        if end > len(img):
            return None
        new_sig = sig + img[hit + len(sig):end]
        new_mask = mask + b"\x01" * n
        val = (base + hit if s.kind == "fn" else
               int.from_bytes(img[hit + (s.imm_at or 0):
                                  hit + (s.imm_at or 0) + 4], "little"))
        nf, _no, _t = locate._auto_mask(new_sig, new_mask, val,
                                        base + 0x1000, base + size)
        if len(locate._find_all(img, new_sig, nf)) == 1:
            return n, to_pattern(new_sig, new_mask)
    return None


# ---------------------------------------------------------------------------
# 位址稽核：專案裡有沒有寫死了卻不在 SIGS 裡的遊戲位址
# ---------------------------------------------------------------------------
def audit_addresses() -> list[str]:
    covered = {(s.module, s.attr) for s in locate.SIGS}
    # 這幾個是「範圍界線」不是位址，不需要定位
    allow = {("injector", "CODE_LO"), ("injector", "CODE_HI")}
    out = []
    app = os.path.join(ROOT, "app")
    for dirpath, _d, files in os.walk(app):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            mod = fn[:-3]
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), path)
            except SyntaxError:
                continue
            for node in tree.body:
                if not (isinstance(node, ast.Assign)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, int)
                        and MODULE_LO <= node.value.value < MODULE_HI):
                    continue
                for t in node.targets:
                    if not isinstance(t, ast.Name):
                        continue
                    key = (mod, t.id)
                    if key in covered or key in allow:
                        continue
                    out.append(f"{mod}.{t.id} = {node.value.value:#x}   "
                               f"({os.path.relpath(path, ROOT)}:{node.lineno})")
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def find_client():
    """回傳 (pid, 有沒有進遊戲)。沒有就 (None, False)。"""
    best = None
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" not in w.class_name:
            continue
        ingame = " - " in w.title
        if best is None or ingame:
            best = (w.pid, ingame)
        if ingame:
            break
    return best or (None, False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="離線用的映像檔（--dump 存下來的）")
    ap.add_argument("--dump", action="store_true", help="順便把映像存起來")
    args = ap.parse_args()

    base, size, img, pid, ingame = 0x400000, 0, b"", None, False
    if args.image:
        img = open(args.image, "rb").read()
        size = len(img)
        print(f"離線映像：{args.image}（{size} bytes，假設基底 {base:#x}）")
    else:
        pid, ingame = find_client()
        if pid is None:
            print("⛔ 找不到遊戲。開起來（登入畫面就夠）再跑一次，"
                  "或用 --image 指定先前 --dump 存下來的映像。")
            return 2
        sc = MemoryScanner()
        sc.open(pid)
        try:
            base = sc.module_base(locate.GAME_MODULE)
            info = next((m for m in sc.list_modules()
                         if m.name.lower() == locate.GAME_MODULE), None)
            if not base or info is None:
                print("⛔ 讀不到 angel.dat 模組。")
                return 2
            size = info.size
            img = bytes(sc._read_bytes(base, size) or b"")
        finally:
            sc.close()
        if not img:
            print("⛔ 讀不到映像。")
            return 2
        if args.dump:
            os.makedirs(os.path.dirname(IMGFILE), exist_ok=True)
            open(IMGFILE, "wb").write(img)

    crc = zlib.crc32(img[:0x1000])
    lines = [f"映像 base={base:#x} size={size:#x} 表頭CRC={crc:#010x}"
             f"{'' if pid is None else f'  pid={pid}'}"
             f"{'（在遊戲裡）' if ingame else '（還沒進遊戲）'}", ""]

    names = lua_bindings(img, base)
    lines.append(f"Lua 綁定表：{len(names)} 支（名字定位可用）")
    lines.append("")

    findings = [check_sig(img, base, size, s, names) for s in locate.SIGS]
    broken = [f for f in findings if f.status in ("模糊", "沒命中")]
    moved = [f for f in findings if f.status == "位移"]
    weak = [f for f in findings if f.note.startswith("　⚠")]

    lines.append("=== 逐段特徵 ===")
    for f in findings:
        mark = {"OK": "OK   ", "位移": "位移 ", "模糊": "壞：模糊",
                "沒命中": "壞：沒中"}[f.status]
        val = "" if f.value is None else f"{f.value:#x}"
        lines.append(f"{mark} {f.name:<26} {val:<12} "
                     f"{'' if f.status != '位移' else f'(舊 {f.sig.known:#x})'}"
                     f"{f.note}")
        lines += f.detail

    # 名字定位交叉驗證
    lines += ["", "=== 名字定位交叉驗證（Lua 綁定表；名字不會因改版而變）==="]
    cross_bad = []
    for f in findings:
        hint = NAME_HINTS.get(f.name)
        if not hint:
            continue
        if hint not in names:
            lines.append(f"?    {f.name:<26} 找不到 lua『{hint}』"
                         "（官方改了 API 名字？）")
            continue
        tgt = last_call(img, base, names[hint])
        if f.value is not None and tgt == f.value:
            lines.append(f"OK   {f.name:<26} lua『{hint}』→ {tgt:#x} 一致")
        else:
            cross_bad.append((f.name, hint, tgt, f.value))
            lines.append(f"⚠    {f.name:<26} lua『{hint}』→ "
                         f"{'?' if tgt is None else hex(tgt)}"
                         f"　特徵給的是 {'(壞了)' if f.value is None else hex(f.value)}")

    # 位址稽核
    gaps = audit_addresses()
    lines += ["", "=== 位址稽核（寫死但不在 SIGS 裡）==="]
    lines += ["  （沒有）"] if not gaps else ["  ⚠ " + g for g in gaps]

    # 資料表戳記
    stamp = tablestamp.read_stamp()
    lines += ["", "=== 寫死資料表 ==="]
    if stamp is None:
        lines.append("  還沒蓋過章（assets/table_stamp.json 不存在）")
    elif stamp == crc:
        lines.append("  ✔ 已對這一版遊戲核對過")
    else:
        lines.append(f"  ⚠ 戳記 {stamp:#010x} ≠ 這版 {crc:#010x}"
                     " —— 表**可能**過期。先進遊戲跑 py tools\\recheck_tables.py"
                     "（會做錯事的那兩張可以直接跟記憶體對帳），全對就只要"
                     " py tools\\stamp_tables.py 蓋章，不必重新解包。")

    # 執行時體檢
    runtime_bad: list[str] = []
    if ingame:
        lines += ["", "=== 執行時體檢 ==="]
        try:
            from app.core import health
            rep = health.check()
            for c in rep.clients:
                lines.append(f"  ── {c.name}（{c.account}）")
                for k, (ok, detail) in c.checks.items():
                    lines.append(f"     {'✔' if ok else '✘'} {k}　{detail}")
            bad_feats = rep.broken
            if bad_feats:
                lines.append("  ⛔ 在所有分身上都失敗：" + "、".join(bad_feats))
                runtime_bad.extend(bad_feats)
        except Exception as exc:                           # noqa: BLE001
            lines.append(f"  （跑不起來：{type(exc).__name__}: {exc}）"
                         " —— 直接跑 py tools\\selfcheck.py")
    else:
        lines += ["", "=== 執行時體檢 ===",
                  "  跳過（還沒進遊戲）。位址修好之後登入一台，再跑 "
                  "py tools\\selfcheck.py 驗欄位。"]

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    open(REPORT, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    # --- 可以直接貼的修法 --------------------------------------------------
    fix = ["# patch_doctor 產生的修法建議",
           "",
           "⚠ 每一條都已經驗過「全遮之後在整個映像唯一」才會列出來。",
           "貼進 `app/game/locate.py` 的 SIGS 之後，**一定要**再跑：",
           "",
           "    py tools\\verify_sigs.py     # 唯一性＋模擬改版",
           "    py tools\\patch_doctor.py    # 這支自己再跑一次",
           ""]
    for f in broken:
        fix.append(f"## {f.name}（{f.status}）")
        fix.append("")
        if f.suggest and f.suggest[0] == "regen":
            _k, sugg, named = f.suggest
            if named:
                fix.append(f"★ 名字定位說答案是 **{named:#x}**"
                           f"（lua『{NAME_HINTS.get(f.name)}』綁定裡最後一個 call）"
                           " —— 兩個來源對得起來才採用。")
                fix.append("")
            for addr, val, pat in sugg:
                fix += [f"位址 **{val:#x}**（原本 {f.sig.known:#x}），"
                        f"命中處 {addr:#x}。改成：", "",
                        "```python",
                        f'    Sig("{f.sig.module}", "{f.sig.attr}", '
                        f'"{f.sig.kind}", {f.sig.imm_at},',
                        f"        {pat},",
                        f"        {val:#010x}"
                        f"{', as_rva=True' if f.sig.as_rva else ''}),",
                        "```", ""]
            if not sugg:
                fix.append("⛔ 找不到夠接近的候選 —— 這段程式碼被**改寫**了，"
                           "不只是重新編譯。要人工反組譯重做特徵。")
                fix.append("")
        elif f.suggest and f.suggest[0] == "extend":
            _k, sugg = f.suggest
            fix.append("候選不只一個 —— **要先挑對哪一個**（看報告裡的反組譯，"
                       "或用別的線索：字串內容、名字定位、旁邊的欄位）。"
                       "挑好之後照下面延伸就唯一了：")
            fix.append("")
            for addr, n, pat in sugg:
                fix += [f"候選 {addr:#x}：往後多蓋 {n} bytes 就唯一 →", "",
                        "```python", f"        {pat},", "```", ""]
        fix.append("")

    # 預防性：還沒壞、但只有第二層唯一的段（下次改版一定壞）
    if weak:
        fix += ["---", "",
                "# 預防性：這幾段還沒壞，但**下次改版一定會壞**", "",
                "它們只有在『只遮目標』那層才唯一 —— 而那層是拿舊位址當錨的，",
                "位址一移就沒了（2026-08-11 的 `login.VT_LOGIN` 就是這樣死的）。",
                "往後多蓋幾 bytes 讓它在『全遮』那層就唯一：", ""]
        for f in weak:
            fix.append(f"## {f.name}")
            fix.append("")
            if f.suggest and f.suggest[1]:
                addr, n, pat = f.suggest[1][0]
                fix += [f"命中處 {addr:#x}，往後多蓋 {n} bytes 就唯一 →", "",
                        "```python", f"        {pat},", "```", ""]
            else:
                fix += ["⛔ 往後延伸 48 bytes 都還不唯一 —— 要換個錨"
                        "（字串內容、別的欄位、或另找一處取用點）。", ""]
    open(FIXFILE, "w", encoding="utf-8").write("\n".join(fix) + "\n")

    # --- 主控台：只印結論 --------------------------------------------------
    print(f"特徵 {len(locate.SIGS)} 段：壞 {len(broken)}、改版位移 {len(moved)}、"
          f"其餘正常")
    for f in broken:
        print(f"  ⛔ {f.name}（{f.status}）")
    for f in weak:
        print(f"  ⚠ {f.name} 只有『只遮目標』那層唯一 —— 下次改版會壞")
    for name, hint, tgt, val in cross_bad:
        print(f"  ⚠ 名字定位對不上：{name} vs lua『{hint}』"
              f"→ {'?' if tgt is None else hex(tgt)}")
    for g in gaps:
        print(f"  ⚠ 寫死但沒有特徵：{g}")
    for b in runtime_bad:
        print(f"  ⛔ 執行時體檢失敗（所有分身）：{b}")
    if not (broken or gaps or cross_bad or runtime_bad or weak):
        print("  ✔ 位址與特徵全部正常"
              + ("（執行時欄位也驗過）" if ingame else
                 "（還沒進遊戲，欄位版面沒驗到）"))
    if stamp is not None and stamp != crc:
        print("  ⚠ 另外：寫死資料表還沒對這版核對 —— 進遊戲跑 "
              "py tools\\recheck_tables.py（多半不必重新解包）")
    print(f"\n報告：{REPORT}")
    if broken or weak:
        print(f"修法：{FIXFILE}")
    return 1 if (broken or gaps or cross_bad or runtime_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
