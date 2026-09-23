"""自動丟棄：把「自動丟棄」清單裡的東西從背包丟掉 —— **全部分身共用同一張清單**。

使用者 2026-09-23 要的：跟「存公會倉庫」同一套（小視窗左邊背包／右邊清單、存 config、
所有分身照同一張），差別是**不用走去哪裡**：掛機中每隔幾秒讀一次背包，清單上的東西當場丟。

丟棄封包（2026-09-23 從遊戲自己的 Lua 綁定 `game.destroyitemslot` 反組譯，這版 0x596D50）：
  · 它拿 (視窗資源 id, 格號)，把資源 id 換成背包種類；背包（ITEM_CHAR＝1）那條分支是
    找到那格的物品物件後叫 `0x5D30B3(格號, [物件+8])`：
        push 8 / push 0x13 → 建包（＝jumpmap.BUILD_FN，已 AOB）
        [內文+2] = u16 格號、[內文+4] = u32 物品序號（物件 +0x00 序號＝bag.Item.serial）
        送出 ＝ jumpmap.SEND_FN（[jumpmap.CONN_PTR]，已 AOB）
    → 代號 **0x13**、內文 **8 bytes**；建/送/連線完全沿用存倉庫那組，零新位址。
  · 非背包分支走 `0x5D2DC5(0x13, 種類, 格號)`（倉庫／娃娃欄那些）——我們只丟背包，不用。
  · ⚠ 遊戲 UI 丟東西前會先 `dosafeverify(4)`（安全鎖）；我們直接送包，伺服器要是有鎖住
    就會拒絕 → 序號還在背包 → 記成「丟不掉」跳過，不會安靜地做錯事。

規則（照 CLAUDE.md 鐵則）：
  · 格號／序號**送出前當場重讀**（每一輪 `pending()` 重掃，不用上一拍的）。
  · 送完 poll 背包確認序號消失才算丟掉；沒消失＝拒收 → 這個序號這一趟跳過。
  · 背包讀不完整（None）→ 這一輪什麼都不做（讀不到 ≠ 沒有，memory bag-false-empty-guards）。
"""
from __future__ import annotations

import struct
import time

from app.config import config
from app.game import bag, itemname, jumpmap, supply

CFG_KEY = "discard.items"        # 清單：物品種類 ID 的 list（全部分身共用）

# ★ 出處：0x5D30B3 反組譯 `push 8 / push 0x13 / call BUILD_FN`（檔頭）。
DISCARD_OPCODE = 0x13
DISCARD_BODY = 8
# ★ 出處同上：內文 +2 u16 格號、+4 u32 序號（+0 代號由建構函式寫）。
_OFF_SLOT = 2
_OFF_SERIAL = 4
SCRATCH_OFF = 0x1E0              # 相對 mover.scratch()；避開 jumpmap 0x100/sell 0x140/supply 0x180/team 0x1C0
CALL_TIMEOUT = 1.0
MAX_PER_RUN = 60                 # 一輪最多丟幾件（防呆上限；正常一批遠少於此）
GAP = 5.0                        # 掛機中每隔幾秒檢查一次背包（自家節奏，不是遊戲常數）


# ---------------------------------------------------------------------------
# 清單（config）
# ---------------------------------------------------------------------------
def wanted() -> set[int]:
    """要自動丟棄的物品種類 ID（config 讀出來；壞值一律丟掉）。"""
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
# 背包裡有什麼
# ---------------------------------------------------------------------------
def candidates(scanner) -> tuple[list[bag.Item], list[bag.Item], bool]:
    """(能丟的, 不能丟的, 整袋讀完整了嗎)。給小視窗列清單用。

    背包裡的東西全部都能丟（遊戲的丟棄不看綁定／交易旗標），第二個清單永遠空的 ——
    介面跟存公會倉庫共用同一個小視窗，形狀對齊。
    """
    items, complete = bag.scan(scanner)
    return list(items), [], complete


def pending(scanner, ids: set[int]) -> list[bag.Item] | None:
    """背包裡「在清單上」的東西；背包讀不到回 None（⚠ None ≠ 空）。"""
    if not ids:
        return []
    items, complete = bag.scan(scanner)
    if not complete:
        return None
    return [it for it in items if it.type_id in ids]


# ---------------------------------------------------------------------------
# 丟
# ---------------------------------------------------------------------------
def discard_slot(mover, scanner, slot: int, serial: int) -> tuple[bool, str]:
    """送一包「丟棄」。slot = 物品格號（bag.Item.slot）、serial = 物品序號（bag.Item.serial）。

    封包＝代號 0x13、內文 8：u16 格號 + u32 序號。建/送同存倉庫（supply.deposit_slot）。
    """
    if not (jumpmap.BUILD_FN and jumpmap.SEND_FN):
        return False, "送包位址還沒定位（改版？先跑 patch_doctor）"
    if not (0 <= slot <= 0xFFFF) or not (0 < serial < 0xFFFFFFFF):
        return False, "格號／序號不合理，不送"
    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(jumpmap.BUILD_FN, DISCARD_OPCODE, DISCARD_BODY, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return False, "建封包排不進去（指令槽忙碌）"
        data = supply._u32(scanner, buf + 4)
        if not 0x10000 < data < 0x7FFF0000:
            return False, "封包資料指標不合理"
        # data+0 代號已由建構函式寫；我們填 +2 格號、+4 序號。
        payload = struct.pack("<HI", slot & 0xFFFF, serial & 0xFFFFFFFF)
        if not mover.write(data + _OFF_SLOT, payload):
            return False, "寫封包內容失敗"
        conn = supply._u32(scanner, jumpmap.CONN_PTR)
        pkt = supply._u32(scanner, buf + 0xC)
        if not conn:
            return False, "還沒連上線 —— 可能正在重連"
        if not 0x10000 < pkt < 0x7FFF0000:
            return False, "封包指標不合理"
        if mover.call_sync(jumpmap.SEND_FN, conn, pkt,
                           timeout=CALL_TIMEOUT) is None:
            return False, "送出排不進去（指令槽忙碌）"
    return True, ""


def run(mover, scanner, ids: set[int] | None = None, say=None,
        should_stop=None) -> tuple[bool, str]:
    """把背包裡清單上的東西一件一件丟掉。回 (接得起來嗎, 訊息)。

    每一件送之前都重掃背包（格號／序號當場重讀）；送完 poll 到序號消失才算。
    沒消失＝伺服器不讓丟（安全鎖／不可丟棄的東西）→ 這個序號跳過，訊息裡點名。
    """
    def note(m):
        if say:
            say(m)

    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if ids is None:
        ids = wanted()
    if not ids:
        return True, "清單是空的（先在小視窗勾要丟的東西）"
    dropped = 0
    refused: set[int] = set()
    skipped: list[str] = []
    bag_lost = False
    for _ in range(MAX_PER_RUN):
        if should_stop and should_stop():
            break
        pend = pending(scanner, ids)
        if pend is None:
            bag_lost = True
            break
        pend = [it for it in pend if it.serial not in refused]
        if not pend:
            break
        it = pend[0]                 # 剛讀到的（格號／序號送出前當場重讀，鐵則）
        note(f"丟棄 {itemname.label(it.type_id, it.count)}（格 {it.slot}）…")
        ok, msg = discard_slot(mover, scanner, it.slot, it.serial)
        if not ok:
            return (dropped > 0), f"丟棄送不出去（{msg}）；已丟 {dropped} 件"
        gone = False
        for _ in range(supply.DEPOSIT_POLL):
            time.sleep(supply.DEPOSIT_WAIT)
            if supply._item_gone(scanner, it.serial):
                gone = True
                break
        if not gone:
            refused.add(it.serial)
            skipped.append(itemname.label(it.type_id))
            continue
        dropped += 1
    tail = ""
    if skipped:
        tail = f"（{'、'.join(skipped)} 丟不掉，跳過）"
    if bag_lost:
        tail += "（⚠ 背包讀不到，提前停手）"
    if dropped == 0 and not tail:
        return True, "背包沒有清單上的東西"
    return True, f"丟了 {dropped} 件{tail}"
