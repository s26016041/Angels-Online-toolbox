"""AOB 自動定位：開工具箱時掃一次程式碼段，把所有寫死的遊戲位址換成當下正確的。

## 為什麼需要

**遊戲改版會讓寫死的位址整批位移。** 2026-08-04 那次實際發生過，症狀是
「工具箱突然掃不到怪物」，當時是一個一個手動重新定位的。而且位移量**不一致**
（實測 +0x10 / +0x18 / −0x10F / −0x11E），不能用同一個 delta 推。

只要函式本體沒被重寫（那次只是整體位移），特徵碼就能自動跟上。

## 怎麼運作

    locate.warm(scanner)        ← 有 scanner 的地方呼叫一次（分頁接上分身時）

它掃 angel.dat 的映像找每一段特徵，命中後**把值寫回各模組的常數**
（`attack.SELECT_FN`、`entity.VT_PLAYER`…）。所以那 60 幾個使用點一行都不用改，
也不必到處傳 scanner。

★ 一次就夠，五台共用：多開的分身載的是**同一份 angel.dat、同一個基底**
  （這遊戲無 ASLR，固定 0x400000），所以位址對每個分身都一樣。

★ 失敗不會讓功能消失：找不到或命中多筆就**保留原本寫死的值**，
  行為跟沒有這支模組時完全一樣。`warm()` 會回報哪些變了、哪些失敗。

## 兩種目標

* `fn`   —— 函式進入點。直接對那個位址的頭幾道指令建特徵。
* `data` —— vtable／全域指標這種**資料位址**。AOB 不能直接掃資料（內容會變），
  改成找「程式碼裡把這個位址當立即值用」的地方，對那段程式碼建特徵，
  命中後從 `imm_at` 把立即值讀回來。

## 遮罩規則（決定改版後還準不準）

* `call`/`jmp` 的 rel32 一律遮掉 —— 呼叫點與目標的位移量不一定相同，rel32 會變。
* **指向模組內資料的絕對位址保留** —— 那次改版全域位址一個都沒動，是很好的錨
  （例如 `move.WALK_FN` 的特徵裡就有 `A1 64 5F 9B 00` = `mov eax,[0x9B5F64]`）。

特徵是用 `scratchpad/sigbuild.py` 產的，每一段都驗過**在整個模組裡唯一**。

純讀記憶體，不寫入、不注入。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass

GAME_MODULE = "angel.dat"


@dataclass(frozen=True)
class Sig:
    """一個要自動定位的位址。

    module/attr: 要寫回哪個模組的哪個常數
    kind:        'fn' = 命中處就是答案；'data' = 從命中處 +imm_at 讀 4 bytes
    known:       2026-08-04 當下的絕對位址。定位失敗時保留它，也用來報告有沒有移動。
    as_rva:      該常數存的是 RVA（相對模組基底）而不是絕對位址
    """

    module: str
    attr: str
    kind: str
    imm_at: int | None
    pattern: str
    known: int
    as_rva: bool = False


SIGS: tuple[Sig, ...] = (
    Sig("attack", "ACTION_FN", "fn", None,
        "55 8B EC 83 EC 10 6A 03 6A 07 8D 4D F0 E8 ?? ?? ?? ?? 8B 45 F4 8A 4D 0C",
        0x005DA8E5),
    Sig("attack", "SELECT_FN", "fn", None,
        "55 8B EC 83 EC 10 6A 07 6A 16 8D 4D F0 E8 ?? ?? ?? ?? 8B 4D F4 8A 45 08",
        0x005D3D97),
    Sig("attack", "CAST_FN", "fn", None,
        "55 8B EC 83 EC 10 56 8B 75 08 85 F6 7E 3D 6A 12 6A 06 8D 4D F0 E8 ?? ?? ?? ??",
        0x00559EDA),
    Sig("move", "MOVE_FN", "fn", None,
        "55 8B EC 83 EC 10 53 8B 5D 10 8D 4D F0 56 8B F3 C1 E6 02 8D 46 09 50 6A 04",
        0x00559F28),
    Sig("move", "WALK_FN", "fn", None,
        "55 8B EC A1 64 5F 9B 00 53 8B 58 10 8B 45 0C 99 F7 FB 8B C8 8B 45 14 99",
        0x005D7C87),
    Sig("move", "PATHFIND_FN", "fn", None,
        "55 8B EC 83 EC 0C 56 57 FF 75 0C 8B F9 33 F6 FF 75 08 89 7D F4 E8 ?? ?? ?? ??",
        0x00549A63),
    Sig("entity", "VT_ENTITY", "data", 3,
        "C7 40 08 24 2A 7D 00 E8 ?? ?? ?? ?? 8D 8B 94 03 00 00 C6 45 FC 04 E8 ?? ?? ?? ??",
        0x007D2A24),
    Sig("entity", "VT_ENTITY2", "data", 2,
        "C7 00 D8 29 7D 00 8D 8B FC 02 00 00 C7 40 08 24 2A 7D 00 E8 ?? ?? ?? ??",
        0x007D29D8),
    Sig("entity", "VT_STATE", "data", 2,
        "C7 03 50 3E 7E 00 C7 43 10 68 3E 7E 00 C7 43 14 80 3E 7E 00 C7 43 38 98 3E 7E 00",
        0x007E3E50),
    Sig("entity", "VT_PLAYER", "data", 10,
        "01 C7 07 AC 8B 7D 00 C7 47 08 F8 8B 7D 00 C7 87 10 02 00 00 00 8C 7D 00",
        0x007D8BF8),
    Sig("entity", "VT_MAP_MOBS", "data", 1,
        "68 88 8E 7D 00 E8 ?? ?? ?? ?? 83 C4 0C C3 FF 71 0C 0F B6 41 08 50 FF 71 04",
        0x007D8E88),
    Sig("player", "VTABLE_RVA", "data", 6,
        "C7 87 68 CB 00 00 1C 3E 7E 00 8D 8F 80 F2 00 00 C6 45 FC 24 E8 ?? ?? ?? ??",
        0x007E3E1C, as_rva=True),
    Sig("scene", "VTABLE_RVA", "data", 6,
        "C7 83 3C 10 00 00 3C CF 7D 00 A1 6C 09 89 00 89 1D 64 5F 9B 00 6A 1F 59",
        0x007DCF3C, as_rva=True),
    Sig("scene", "SCENE_PTR_RVA", "data", 1,
        "0D 48 09 89 00 8B 75 FC 8B 45 0C 03 C3 8B 11 FF 76 20 50 FF 52 18 8B 0D 48 09 89 00",
        0x00890948, as_rva=True),
    Sig("monsters", "INDEX_PTR_RVA", "data", 1,
        "A1 88 BC 98 00 8B 04 B0 EB 3B 68 FF 01 00 00 8D 85 FD FD FF FF C6 85 FC FD FF FF 00",
        0x0098BC88, as_rva=True),
    Sig("move", "WAYPOINTS", "data", 1,
        "68 84 66 9B 00 FF 75 0C 8B C8 FF 75 08 E8 ?? ?? ?? ?? 33 C9 85 C0 0F 9F C1",
        0x009B6684),
    Sig("move", "MGR_PTR", "data", 1,
        "35 38 E6 96 00 8B CE FF B6 90 2A 00 00 E8 ?? ?? ?? ?? 85 C0 74 2F FF 75 08",
        0x0096E638),
)

# 掃過就不再掃：同一份 angel.dat，五台分身結果一樣。
_done = False
_report: list[tuple[str, int, int | None]] = []


def _parse(pattern: str) -> tuple[bytes, bytes]:
    toks = pattern.split()
    sig = bytearray(len(toks))
    mask = bytearray(len(toks))
    for i, t in enumerate(toks):
        if t != "??":
            sig[i] = int(t, 16)
            mask[i] = 1
    return bytes(sig), bytes(mask)


def _seed(sig: bytes, mask: bytes) -> tuple[bytes, int]:
    """挑最長的一段固定位元組當粗篩用的種子（用 bytes.find 很快）。"""
    best_i = best_n = cur_i = cur_n = 0
    for i, m in enumerate(mask):
        if m:
            if cur_n == 0:
                cur_i = i
            cur_n += 1
            if cur_n > best_n:
                best_i, best_n = cur_i, cur_n
        else:
            cur_n = 0
    return sig[best_i:best_i + best_n], best_i


def _find_unique(img: bytes, sig: bytes, mask: bytes) -> int | None:
    """回傳唯一命中的位移；找不到或不只一個都回 None（寧可保留舊值）。"""
    seed, at = _seed(sig, mask)
    if not seed:
        return None
    hit = None
    i = img.find(seed)
    while i >= 0:
        start = i - at
        if start >= 0 and start + len(sig) <= len(img):
            if all(not mask[k] or img[start + k] == sig[k]
                   for k in range(len(sig))):
                if hit is not None:
                    return None            # 不只一個 → 特徵不夠強，別亂改
                hit = start
        i = img.find(seed, i + 1)
    return hit


def warm(scanner, force: bool = False) -> list[tuple[str, int, int | None]]:
    """掃一次並把結果寫回各模組。回傳 [(名稱, 舊值, 新值或 None)]。

    新值 None = 這一項定位失敗，**保留原本寫死的值**（功能不會因此消失）。
    """
    global _done
    if _done and not force:
        return _report
    base = scanner.module_base(GAME_MODULE)
    if not base:
        return []
    info = next((m for m in scanner.list_modules()
                 if m.name.lower() == GAME_MODULE), None)
    if info is None:
        return []
    img = scanner._read_bytes(base, info.size)
    if not img:
        return []
    img = bytes(img)

    out: list[tuple[str, int, int | None]] = []
    for s in SIGS:
        sig, mask = _parse(s.pattern)
        off = _find_unique(img, sig, mask)
        found = None
        if off is not None:
            if s.kind == "fn":
                found = base + off
            else:
                k = off + (s.imm_at or 0)
                if k + 4 <= len(img):
                    v = int.from_bytes(img[k:k + 4], "little")
                    # 資料位址一定落在模組內，不然就是抓錯了
                    if base <= v < base + info.size:
                        found = v
        if found is not None:
            mod = importlib.import_module(f"app.game.{s.module}")
            setattr(mod, s.attr, found - base if s.as_rva else found)
        out.append((f"{s.module}.{s.attr}", s.known, found))
    _done = True
    _report[:] = out
    return out


def moved(report=None) -> list[tuple[str, int, int]]:
    """報告裡「位址跟寫死的不一樣」的項目 —— 改版之後這裡就會有東西。"""
    rep = _report if report is None else report
    return [(n, old, new) for n, old, new in rep
            if new is not None and new != old]


def failed(report=None) -> list[str]:
    """定位失敗的項目（保留了寫死的值）。"""
    rep = _report if report is None else report
    return [n for n, _, new in rep if new is None]
