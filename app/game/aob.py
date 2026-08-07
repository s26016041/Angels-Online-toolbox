"""AOB 特徵碼定位：用「值周圍穩定的位元組樣式」在動態記憶體中定位一個值。

跟指標路徑不同 —— 不靠固定位址、也不靠指標鏈，而是靠一段 masked 位元組樣式
（?? = 萬用字元）去掃描記憶體，命中後加一個固定偏移就是目標值。適合那些「掛在
動態結構、沒有穩定指標路徑」的值（天使之戀的技能經驗球就是這種）。

用法：
    from app.game.aob import SKILL_EXP_BALL, scan
    from app.core.memory import MemoryScanner
    sc = MemoryScanner(); sc.open(pid)
    for addr in scan(sc, SKILL_EXP_BALL):
        print(sc.read_value(addr, SKILL_EXP_BALL.vt))

特徵怎麼來的（開發期，只做一次）：用多個分身比對某值周圍的位元組，相同→固定、
不同→萬用；再驗證它能唯一定位到目標（見 scratchpad/aob_sig.py、tools/build_signature.py）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.memory import VALUE_TYPES, ValueType


@dataclass(frozen=True)
class AOBSignature:
    """一段 AOB 特徵：masked 位元組樣式 + 「樣式起點到目標值」的偏移。"""

    pattern: str          # 例 "01 ?? ?? 09 ... FF FF"，?? = 萬用
    value_offset: int     # 從樣式起點到目標值的位元組偏移
    vt_key: str = "int32"
    label: str = ""

    @property
    def vt(self) -> ValueType:
        return VALUE_TYPES[self.vt_key]

    def parse(self) -> tuple[bytes, bytes]:
        """把樣式字串轉成 (sig, mask)：mask[i]=1 表示該位元組固定。"""
        toks = self.pattern.split()
        sig = bytearray(len(toks))
        mask = bytearray(len(toks))
        for i, t in enumerate(toks):
            if t in ("??", "?"):
                mask[i] = 0
            else:
                sig[i] = int(t, 16)
                mask[i] = 1
        return bytes(sig), bytes(mask)


def scan(scanner, aob: AOBSignature, writable_only: bool = True, limit: int = 64,
         should_stop=None) -> list[int]:
    """在程序記憶體找 AOB 樣式，回傳 [目標值位址, ...]。

    以「最長連續固定位元組」當搜尋錨點快速定位候選，再逐一驗證整段 masked 樣式。

    should_stop: 可選的 callable，每掃一個記憶體區塊前呼叫一次；回傳 True 就中止並
    回傳目前找到的（不完整）結果。全掃一個分身要 1～2.5 秒，沒有這個中止點的話，
    要求它停止的人（例如關閉程式、觸發警報）就得整整等它掃完。
    """
    sig, mask = aob.parse()
    n = len(sig)
    # 找最長連續固定當搜尋錨點
    best_off, best_len, run = 0, 0, None
    for k in range(n + 1):
        if k < n and mask[k]:
            run = k if run is None else run
        else:
            if run is not None and k - run > best_len:
                best_off, best_len = run, k - run
            run = None
    anchor = sig[best_off:best_off + best_len]
    if not anchor:
        return []

    hits: list[int] = []
    for base, size in scanner._iter_regions(writable_only=writable_only):
        if should_stop is not None and should_stop():
            return hits
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        # memoryview 沒有 .find（見 _read_region）。複製一次的成本跟以前
        # 一模一樣 —— 以前那份複製是在 _read_region 裡面做的。
        raw = bytes(raw)
        s = 0
        while len(hits) < limit:
            i = raw.find(anchor, s)
            if i < 0:
                break
            start = i - best_off
            if start >= 0 and start + n <= len(raw):
                seg = raw[start:start + n]
                if all(m == 0 or seg[k] == sig[k] for k, m in enumerate(mask)):
                    hits.append(base + start + aob.value_offset)
            s = i + 1
        if len(hits) >= limit:
            break
    return hits


# ---------------------------------------------------------------------------
# 已找到並驗證過的 AOB 特徵登錄表
# ---------------------------------------------------------------------------
# 技能經驗球（int32）：值前方 64 個位元組是一段固定的 FF…（+前面幾個固定值），
# 命中後 +0x80 = 經驗球位址。用 3 台分身生成、泛化到第 4 台驗證通過（2026-07-11）。
# 註：此特徵會命中「所有技能」的經驗球（每技能一顆）；監控端靠「正在增加者」選出在練的技能。
SKILL_EXP_BALL = AOBSignature(
    pattern=(
        "01 ?? ?? ?? ?? ?? ?? 01 ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? 04 ?? ?? ?? ?? ?? "
        "?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? "
        "?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? ?? "
        "FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF "
        "FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF "
        "FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF ?? ?? ?? ??"
    ),
    value_offset=0x80,
    vt_key="int32",
    label="技能經驗球",
)
