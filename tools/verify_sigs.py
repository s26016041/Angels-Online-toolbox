"""對真遊戲驗證 locate.SIGS 的特徵碼品質（純讀，不寫入、不注入）。

改了 `locate.SIGS` 或 `_auto_mask` 之後**一定要跑這個**：
開著遊戲，在專案根目錄執行 `py tools/verify_sigs.py`。

逐段檢查三件事：
  1. 唯一性 —— auto-mask（遮掉內嵌絕對位址）之後，整個映像必須恰好命中 1 次。
     多命中＝模糊定位，warm() 會直接放棄該段（data 保舊值、fn 清 0）。
  2. 讀回值 —— data 類從 imm_at 讀回的值；在「跟 SIGS 同版」的遊戲上
     應該等於 known（fn 類則是命中位址本身等於 known）。
  3. 交叉驗證 —— 同段裡多個「目標位址視窗」必須指向同一個值。

結果全量寫 `reports/sig_verify.txt`，主控台只印每段一行＋總結。
結束碼：0 = 全部通過、1 = 有壞的、2 = 找不到遊戲。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import window as win                    # noqa: E402
from app.core.memory import MemoryScanner             # noqa: E402
from app.game import locate                           # noqa: E402

REPORT = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "reports", "sig_verify.txt")


def _find_pid() -> int | None:
    for w in win.enumerate_windows(title_contains="Angels Online"):
        if "_MIDAGEONL_" in w.class_name:
            return w.pid
    return None


def _all_hits(img: bytes, sig: bytes, mask: bytes) -> tuple[list[int], int]:
    """跟 locate._find_unique 同一套比對，但**收集全部命中**（診斷用）。"""
    seed, at = locate._seed(sig, mask)
    hits: list[int] = []
    if not seed:
        return hits, 0
    i = img.find(seed)
    while i >= 0:
        start = i - at
        if start >= 0 and start + len(sig) <= len(img):
            if all(not mask[k] or img[start + k] == sig[k]
                   for k in range(len(sig))):
                hits.append(start)
        i = img.find(seed, i + 1)
    return hits, len(seed)


def main() -> int:
    pid = _find_pid()
    if pid is None:
        print("找不到遊戲視窗 —— 先開遊戲再跑。")
        return 2
    sc = MemoryScanner()
    sc.open(pid)
    try:
        base = sc.module_base(locate.GAME_MODULE)
        info = next((m for m in sc.list_modules()
                     if m.name.lower() == locate.GAME_MODULE), None)
        if not base or info is None:
            print("讀不到 angel.dat 模組。")
            return 2
        img = sc._read_bytes(base, info.size)
    finally:
        sc.close()
    if not img:
        print("讀不到映像。")
        return 2

    lines: list[str] = [f"pid={pid} base={base:#x} size={info.size:#x}", ""]
    bad = 0
    for s in locate.SIGS:
        sig, mask = locate._parse(s.pattern)
        m_full, m_only, targets = locate._auto_mask(
            sig, mask, s.known, base + 0x1000, base + info.size)
        # 跟 warm() 同一套策略：keep_imm 例外段直接用原 mask；
        # 其餘全遮優先，不唯一才退回只遮目標。
        if s.keep_imm:
            hits, seedlen = _all_hits(img, sig, mask)
            tier, used, targets = "keep-imm", mask, ()
        else:
            hits, seedlen = _all_hits(img, sig, m_full)
            tier, used = "full", m_full
            if len(hits) != 1:
                hits2, seedlen2 = _all_hits(img, sig, m_only)
                if len(hits2) == 1:
                    hits, seedlen = hits2, seedlen2
                    tier, used = "only-target", m_only
        masked = sum(1 for a, b in zip(mask, used) if a and not b)
        name = f"{s.module}.{s.attr}"
        row = (f"{name:<24} {s.kind:<4} {tier:<11} hits={len(hits)} "
               f"seed={seedlen}B masked={masked}B targets={list(targets)}")
        ok = len(hits) == 1
        if ok:
            off = hits[0]
            if s.kind == "fn":
                got = base + off
            else:
                k = off + (s.imm_at or 0)
                got = int.from_bytes(img[k:k + 4], "little")
                if not all(int.from_bytes(
                        img[off + t:off + t + 4], "little") == got
                        for t in targets):
                    ok = False
                    row += " ✗交叉驗證失敗（多個目標視窗不同值）"
            row += f" got={got:#x} known={s.known:#x}"
            if got != s.known:
                ok = False
                row += " ✗讀回值 ≠ known（同版遊戲上不該發生）"
        else:
            row += (" ✗多重命中（模糊）：" +
                    ", ".join(f"{base + h:#x}" for h in hits[:8])
                    if hits else " ✗沒命中")
        if not ok:
            bad += 1
        lines.append(("OK   " if ok else "BAD  ") + row)

    lines += ["", f"共 {len(locate.SIGS)} 段，壞 {bad} 段。", "",
              "--- 模擬改版：把映像副本裡的目標位址改掉，看還定不定得到 ---",
              "（⚠ 純記憶體副本運算，不碰遊戲。舊的『模擬改版 17/17』是改 Python",
              "  常數、映像沒動，測不到特徵把答案寫死的問題 —— 這裡才測得到。）"]
    sim_bad = 0
    SHIFT = 0x2000
    for s in locate.SIGS:
        if s.kind != "data":
            continue
        name = f"{s.module}.{s.attr}"
        sig, mask = locate._parse(s.pattern)
        m_full, m_only, targets = locate._auto_mask(
            sig, mask, s.known, base + 0x1000, base + info.size)
        if s.keep_imm:
            lines.append(f"SKIP {name:<24} keep_imm（位址即錨，已知會定位失敗）")
            continue
        off = locate._find_unique(img, sig, m_full)
        used = m_full
        if off is None:
            off = locate._find_unique(img, sig, m_only)
            used = m_only
        if off is None:
            sim_bad += 1
            lines.append(f"BAD  {name:<24} 原始映像就定位不到")
            continue
        # 把這一段裡所有「等於 known」的視窗都改成 known+SHIFT
        patched = bytearray(img)
        new_val = s.known + SHIFT
        for t in (targets or (s.imm_at or 0,)):
            k = off + t
            if int.from_bytes(patched[k:k + 4], "little") == s.known:
                patched[k:k + 4] = new_val.to_bytes(4, "little")
        pimg = bytes(patched)
        off2 = locate._find_unique(pimg, sig, used)
        got2 = None
        if off2 is not None:
            k = off2 + (s.imm_at or 0)
            got2 = int.from_bytes(pimg[k:k + 4], "little")
        ok2 = got2 == new_val
        if not ok2:
            sim_bad += 1
        lines.append(("OK   " if ok2 else "BAD  ") +
                     f"{name:<24} 位址 {s.known:#x} → {new_val:#x}：" +
                     ("重新定位成功" if ok2 else
                      f"失敗（找到 {got2 if got2 is None else hex(got2)}）"))
    lines += ["", f"模擬改版：壞 {sim_bad} 段。"]
    bad += sim_bad
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    for ln in lines:
        print(ln)
    print(f"\n報告：{REPORT}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
