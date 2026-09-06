"""公會（社團）倉庫：把「存公會倉庫」清單裡的東西存進去 —— **全部分身共用同一張清單**。

使用者 2026-09-06 要的：掛機頁一顆鈕 → 小視窗列這台背包裡**能存公會倉庫**的東西、
打字子字串過濾、勾選＝清單（存 config，所有分身都照這張存）；回程補給到銀行時
順手存；公會倉庫滿了**安靜關窗**不通知；綁定／不可交易的東西也不能存。

已實機驗證（黑狐 永夜城 銀行小姐艾寶 1890，2026-09-06，memory guild-bank）：
  · 開法＝跟銀行講話 →「我要用倉庫」(talkaction 11) → 子選單第 2 項「社團的倉庫」(11)
    → 2.46 秒 `WND_BANK` 非 0 且 Lua 全域 **`CUR_BANK_TYPE` == 1**（BANK_TYPE_GUILD）。
    ⚠ `CUR_BANK_TYPE` 關窗後**不歸零**（殘值）→ 只有 `WND_BANK≠0` 時才有意義，
      `is_open()` 兩個一起看。**沒確認是公會倉就絕不送存入包**（不然會存進個人倉）。
  · 存入封包**跟個人倉庫同一包**：`game.usecharitem` 的 C 函式（0x593C6C）存入分支就是
    `0x5D25B5(0x11, 格號, 0)`（代號 0x2F），**沒有看倉庫種類的分支** —— 哪個倉由伺服器
    依「目前開著的倉庫」決定 → `supply.deposit_slot` 直接沿用，零新位址。
  · 關窗同 `supply._bank_close`（0x22 離開包＋closebank＋DestroyBankWnd）。
  · 能不能存＝資源包 item.xml 的三個旗標（`itemflags`：不可存倉庫／不可交易／裝備綁定），
    表裡沒那筆一律當不能存（CLAUDE.md 第 0 條：表是權威，查不到就少做事）。
  · 滿了／被拒收的判法沿用 run_bank：送包後 poll 背包，序號還在＝沒進去。
    ⚠ 「拒收一件」跟「倉庫滿」在客戶端**分不出來**（都是東西沒走、伺服器只丟一句
      系統字串 581「無法存入倉庫。」）→ 連續 `FAIL_STREAK` 件都沒進去才當滿；
      單件沒進去記成「跳過」換下一件。滿了就關窗、把結果寫進訊息，**不通知**（使用者定）。
"""
from __future__ import annotations

import time

from app.config import config
from app.game import bag, itemflags, itemname, lua, scene, supply

CFG_KEY = "guildbank.items"      # 清單：物品種類 ID 的 list（全部分身共用）

TALK_GUILD = 11                  # 「我要用倉庫」之後的子選單第 2 項＝社團的倉庫（實測）
BANK_TYPE_GUILD = 1              # Lua 全域 BANK_TYPE_GUILD（dump_lua_globals 實讀）
BANK_TYPE_KEY = "CUR_BANK_TYPE"  # 開窗時 CreateBankWnd 寫的（關窗不歸零）
FAIL_STREAK = 2                  # 連續幾件送了沒進去＝倉庫滿了（單件＝被拒收，跳過）


# ---------------------------------------------------------------------------
# 清單（config）
# ---------------------------------------------------------------------------
def wanted() -> set[int]:
    """要存公會倉庫的物品種類 ID（config 讀出來；壞值一律丟掉）。"""
    raw = config.get(CFG_KEY, []) or []
    out: set[int] = set()
    for x in raw if isinstance(raw, (list, tuple, set)) else []:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def set_wanted(ids) -> None:
    """寫回清單。⚠ config.set 不寫檔，要接 save()（memory config-set-needs-save）。"""
    config.set(CFG_KEY, sorted({int(x) for x in ids}))
    config.save()


# ---------------------------------------------------------------------------
# 背包裡有什麼能存
# ---------------------------------------------------------------------------
def eligible(it: bag.Item) -> bool:
    """這件東西能不能放進公會倉庫（純查表，見 itemflags）。"""
    return itemflags.guild_bankable(it.type_id)


def candidates(scanner) -> tuple[list[bag.Item], list[bag.Item], bool]:
    """(能存的, 不能存的, 整袋讀完整了嗎)。給小視窗列清單用。"""
    items, complete = bag.scan(scanner)
    ok = [it for it in items if eligible(it)]
    no = [it for it in items if not eligible(it)]
    return ok, no, complete


def pending(scanner, ids: set[int]) -> list[bag.Item] | None:
    """背包裡「在清單上而且能存」的東西；背包讀不到回 None（⚠ None ≠ 空）。"""
    if not ids:
        return []
    items, complete = bag.scan(scanner)
    if not complete:
        return None
    return [it for it in items if it.type_id in ids and eligible(it)]


# ---------------------------------------------------------------------------
# 開的是不是公會倉
# ---------------------------------------------------------------------------
def is_open(scanner) -> bool | None:
    """公會倉庫視窗現在開著嗎：WND_BANK 非 0 **而且** CUR_BANK_TYPE == 1。
    全域讀不到回 None（呼叫端當「不確定」＝不送）。"""
    try:
        g = lua.globals_of(scanner, (supply.BANK_WND, BANK_TYPE_KEY))
    except Exception:                                      # noqa: BLE001
        return None
    if not g or supply.BANK_WND not in g or BANK_TYPE_KEY not in g:
        return None
    return bool(g[supply.BANK_WND]) and int(g[BANK_TYPE_KEY]) == BANK_TYPE_GUILD


# ---------------------------------------------------------------------------
# 存
# ---------------------------------------------------------------------------
def run(mover, scanner, npc_id: int, fallback, ids: set[int]) -> tuple[bool, str]:
    """走到銀行 → 開**社團**倉庫 → 把清單上的東西一件件存進去 → 關窗。

    假設角色已在有銀行的城裡（走近由 `supply._engage_npc` 負責，跟個人倉庫同一支）。
    回 (這段算不算成功, 說明)。滿了／拒收都寫進說明，由呼叫端決定要不要講。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    pend = pending(scanner, ids)
    if pend is None:
        return True, "背包讀不到，跳過公會倉庫"
    if not pend:
        return True, "沒有要存公會倉庫的東西"

    # 上一步（個人倉庫）的窗要是還開著，_engage_npc 第一句就會當「已經開了」跳過對話
    # → 存進個人倉。先收乾淨再講話。
    if supply._wnd_open(mover, scanner, supply.BANK_WND):
        supply._bank_close(mover, scanner)
        time.sleep(0.5)
    if not supply._engage_npc(mover, scanner, npc_id, fallback,
                              [supply.TALK_BANK_USE, TALK_GUILD], supply.BANK_WND):
        supply._bank_close(mover, scanner)
        return False, (f"公會倉庫開不起來（停在離銀行約 "
                       f"{supply._dist_to_npc(scanner, npc_id)} 格）")
    opened = is_open(scanner)
    if opened is not True:
        # ⛔ 開到的不是公會倉（或讀不到種類）→ 一件都不送，不然會存進個人倉庫
        supply._bank_close(mover, scanner)
        return False, ("開到的視窗不是公會倉庫（CUR_BANK_TYPE 不是 1）→ 關窗不存"
                       if opened is False else "讀不到倉庫種類 → 不敢存，關窗")

    deposited = 0
    skipped: list[str] = []
    refused: set[int] = set()        # 送了沒進去的序號（別再挑到它）
    streak = 0
    full = False
    bag_lost = False
    for _ in range(supply.MAX_DEPOSIT):
        pend = pending(scanner, ids)
        if pend is None:
            bag_lost = True
            break
        pend = [it for it in pend if it.serial not in refused]
        if not pend:
            break
        it = pend[0]                 # 每一件都是剛讀到的（格號送出前當場重讀，鐵則）
        ok, msg = supply.deposit_slot(mover, scanner, it.slot)
        if not ok:
            supply._bank_close(mover, scanner)
            return (deposited > 0), f"存款送不出去（{msg}）；已存 {deposited} 件到公會倉庫"
        left = False
        for _ in range(supply.DEPOSIT_POLL):
            time.sleep(supply.DEPOSIT_WAIT)
            if supply._item_gone(scanner, it.serial):
                left = True
                break
        if not left:
            refused.add(it.serial)
            skipped.append(itemname.label(it.type_id))
            streak += 1
            if streak >= FAIL_STREAK:
                full = True
                break
            continue
        streak = 0
        deposited += 1

    supply._bank_close(mover, scanner)
    tail = ""
    if full:
        tail = f"（公會倉庫滿了：{skipped[-1]} 存不進去，關窗離開）"
    elif skipped:
        tail = f"（{'、'.join(skipped)} 存不進去，跳過）"
    if bag_lost:
        tail += "（⚠ 背包讀不到，提前停手）"
    return True, f"存了 {deposited} 件到公會倉庫{tail}"


def run_here(mover, scanner, say=None, ids: set[int] | None = None) -> tuple[bool, str]:
    """**就地**測：讀目前地圖的銀行 NPC → 走過去 → 開社團倉庫 → 存清單上的東西。
    不回城、不修不買（給小視窗的測試鈕用）。假設角色已在有銀行的城裡。"""
    def note(m):
        if say:
            say(m)

    if not (mover and mover.active):
        return False, "跳板沒裝好"
    here = scene.current_id(scanner)
    if here is None:
        return False, "讀不到目前地圖"
    entry = supply.NPC_TABLE.get(here)
    bank_npc = entry.get("bank") if entry else None
    if not bank_npc:
        return False, f"{scene.scene_name(here)} 沒有銀行 NPC（不在名單內，或這張圖沒銀行）"
    ids = wanted() if ids is None else ids
    if not ids:
        return True, "清單是空的（先在小視窗勾要存的東西）"
    pend = pending(scanner, ids)
    if pend is None:
        return False, "背包讀不到，先不動（避免把「讀不到」當成「沒有」）"
    if not pend:
        return True, "背包沒有清單上的東西"
    bkid, bkx, bky = bank_npc
    note(f"背包有 {len(pend)} 件要存，走去銀行 ({bkx},{bky}) 開公會倉庫…")
    return run(mover, scanner, bkid, (bkx, bky), ids)
