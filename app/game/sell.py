"""賣東西給 NPC 商人：照抄遊戲「確定賣出」那顆按鈕送的封包。

    ok, msg = sell.sell_items(mover, scanner, [(序號, 取得時間, 數量), …])

⚠ 送出前角色**必須已經在跟商人對話**（商店視窗開著）。伺服器是靠先前那包
  「點 NPC」記住你在跟誰交易的，這支不會、也不該替你去點商人。

封包長什麼樣（反組譯 `npcsalesellconfirm` → `0x5D70AE`）
--------------------------------------------------------
    imul eax, [件數], 0xC / add eax, 6
    push eax / push 0x28 / call 0x50DF6E    ; 代號 0x28、內文 6 + 12×件數
    mov word  [資料+0], 0x28
    mov dword [資料+2], 件數
    每件 12 bytes 從 +6 起：
        +0 = 物品 +0x00（唯一序號）
        +4 = 物品 +0x04（取得時間）
        +8 = 要賣幾個　　★ 遊戲對 <= 0 的那一筆直接跳過不寫

    push [封包] / push [0x9B67D0] / call 0x711130   ; 送出

那兩個欄位是**直接從物品結構抄過去的**（`0x6023AD` 那段把 `[item+0]`、
`[item+4]` 寫進清單列，送包時再原封不動複製進封包），所以我們也照抄，
不必知道伺服器怎麼解讀它們。見 `app/game/bag.py`。

線材長度可以自己驗算：`6 + 上取整16(內文)`。使用者那份擷取賣出那包是 86 bytes
→ 內文落在 65~80 → 件數 5 或 6，跟「賣掉幾件不要的裝備」對得上。

為什麼還要先送 talkaction
-------------------------
擷取裡「確定賣出」是**兩包**：先 `talkaction 10`（代號 0x0B、內文 3、
就一個動作碼），緊接著才是賣出包。同一個 Lua 處理常式送的。
不確定伺服器有沒有在檢查，但照著客戶端的順序送最安全，成本也只有一包。

⚠⚠ 客戶端在建包前會先檢查「賣完之後的錢會不會超過 20 億」（`0x5D717A` 那段
  比 `0x77359400`），超過就不送、跳訊息。我們**不做這個檢查** —— 我們送的是
  伺服器要的東西，錢的上限由伺服器判定；在這裡自己算反而可能算錯而擋掉正常交易。
"""
from __future__ import annotations

import struct

# ⚠ 這幾個值會被 locate.warm() 依 AOB 重新定位，不要在別處複製。
#   前三個跟 jumpmap 用的是同一支遊戲函式，直接借它定位好的值（見 _fns()）。
TALK_FN = 0x005DA91E         # talkaction(動作碼)：代號 0x0B、內文 3

OPCODE = 0x28                # 賣東西給 NPC
BODY_HEAD = 6                # 代號(u16) + 件數(u32)
BODY_ENTRY = 12              # 序號 + 取得時間 + 數量
TALK_SELL = 10               # 擷取到的動作碼（確定賣出那一下送的）

# ★ 一次最多送幾件。遊戲那邊沒有明文上限（清單有幾列就送幾列），但我們手上
#   唯一的實例是使用者那份擷取：線材 86 bytes ＝ 5 或 6 件。所以分批送，
#   每批的長度停在那個量級的兩倍以內（8 件 → 內文 102、線材 118）。
#   分批是安全的：每一筆認的是物品自己的序號，不是格號，賣掉幾件之後
#   剩下那些的序號不會變。
BATCH = 8
SCRATCH_OFF = 0x140          # 相對 mover.scratch()；避開 jumpmap 的 0x100 與 lua 的字串區
CALL_TIMEOUT = 1.0


def _fns():
    """取封包建構／送出函式與連線指標 —— 跟 jumpmap 共用同一組已定位的值。"""
    from app.game import jumpmap
    return jumpmap.BUILD_FN, jumpmap.SEND_FN, jumpmap.CONN_PTR


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else 0


def _send_batch(mover, scanner, batch) -> tuple[bool, str]:
    """送出一包賣出封包。batch = [(序號, 取得時間, 數量), …]，長度 >= 1。"""
    build_fn, send_fn, conn_ptr = _fns()
    body = BODY_HEAD + BODY_ENTRY * len(batch)
    payload = struct.pack("<I", len(batch))
    for serial, stamp, qty in batch:
        payload += struct.pack("<III", serial & 0xFFFFFFFF,
                               stamp & 0xFFFFFFFF, qty)

    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(build_fn, OPCODE, body, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return False, "建封包排不進去（指令槽忙碌，再按一次）"
        data = _u32(scanner, buf + 4)
        if not 0x10000 < data < 0x7FFF0000:
            return False, "封包資料指標不合理"
        # 代號已經由建構函式寫在 +0，我們從 +2 接著寫件數與各件明細。
        mover.write(data + 2, payload)
        # ⚠⚠ 連線與封包指標送出去之前先擋掉 0：重連／載地圖時 [conn] 會是 0，
        #   而送出函式是拿它去算位址之後才檢查 NULL 的（跟 jumpmap 同一個坑）。
        conn, pkt = _u32(scanner, conn_ptr), _u32(scanner, buf + 0xC)
        if not conn:
            return False, "還沒連上線 —— 可能正在重連"
        if not 0x10000 < pkt < 0x7FFF0000:
            return False, "封包指標不合理"
        if mover.call_sync(send_fn, conn, pkt,
                           timeout=CALL_TIMEOUT) is None:
            return False, "送出排不進去（指令槽忙碌，再按一次）"
    return True, ""


def talk(mover, code: int = TALK_SELL) -> bool:
    """送一包「對話動作」（talkaction）。"""
    if not (mover and mover.active):
        return False
    with mover.lock:
        return mover.call_sync(TALK_FN, code,
                               timeout=CALL_TIMEOUT) is not None


def sell_items(mover, scanner, entries) -> tuple[bool, str]:
    """把 entries 賣掉。entries = [(序號, 取得時間, 數量), …]。

    回傳 (有沒有全部送出去, 說明)。只保證「封包送出去了」——
    真的賣不賣得掉由伺服器決定（沒在跟商人對話、東西不能賣都會被它擋下來）。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    rows = [(int(s), int(t), int(q)) for s, t, q in entries if int(q) > 0]
    if not rows:
        return False, "沒有要賣的東西"

    talk(mover)          # 照客戶端的順序：先 talkaction，再賣出
    sent = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        ok, msg = _send_batch(mover, scanner, batch)
        if not ok:
            done = f"（已送出 {sent} 件）" if sent else ""
            return False, f"{msg}{done}"
        sent += len(batch)
    return True, f"已送出賣出封包：{sent} 件"
