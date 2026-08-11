"""生產收尾的動作：**製作**與**捐公會**。

    ok, msg = produce.craft(mover, scanner, 配方ID)          # 做一個
    ok, msg = produce.craft_stop(mover, scanner)             # 停止製作
    ok, msg = produce.donate(mover, scanner, [(貢獻編號, 組數), …])

⛔ **存倉庫這條路已刪除**（2026-08-11 使用者決定）。封包本身是解出來的
   （代號 `0x2F`、動作碼 `0x11`、格號），但**開倉庫做不到**：要跟倉庫人員
   對話選選項，而選項編號是伺服器當下發的清單位置、湊不出來，泛用送包的
   代碼表裡也只有「關閉倉庫」沒有「開啟」。與其留一條永遠要人工開倉庫的
   半自動路徑，不如不做 —— 沒選捐公會就做完停著不動。細節留在 memory 的
   `craft-donate-storage-packets`，要撿回來再看那份。

兩包長什麼樣（都是反組譯遊戲自己那條路來的，沒有猜的欄位）
----------------------------------------------------------

### 製作　代號 0x36、內文 6

`makestart`（UI 指令表）→ `0x5D47AF`：

    push 6 / push 0x36 / call 建包
    eax = 製作清單控制項第 0 列的資料；sar eax,0x10
    mov [封包+2], eax                     ← u32 **配方 ID**

清單那一列存的是 `(配方ID << 16) | 數量`（`makeadd` 0x58F93E 拿
`>>16` 跟選到的配方比對），所以送出去的就是配方 ID。
**一包只做一個**：客戶端做完一個會再送一次（0x55701A 那支也是同一包）。

### 停止製作　代號 0x37、內文 2

`makestop` → `0x5D485F`：只有代號，沒有內容。

### 捐公會　代號 0x4B、內文 4 + 6×筆數

`guildcontrib` → `0x5D7BAC`：

    [封包+2] u8  筆數（遊戲自己上限 100）
    [封包+3] u8  溢出確認旗標
    每筆 6 bytes：u16 **貢獻編號**、u32 **組數**

★ 那個 u16 是**貢獻品表的編號**，不是物品種類、也不是背包格號 ——
  `updatecontriblist`（0x5FCDC1）每一列 new 12 bytes，`[列+0]` 存的是
  貢獻品表項的指標，送包時取 `word [表項+0]`＝表項自己的編號。
★ 數量的單位是**組**（一組幾個看 `recipes.Contrib.group`，實測 200）。
★ 旗標：`OnGuildContrib` 的 Lua bytecode 是 `game.guildcontrib(false)`，
  貢獻度會溢出時跳出來的確認鈕才是 `guildcontrib(true)`。所以平常送 0；
  伺服器覺得會溢出時會退我們一個訊息，那不是我們該自己決定的事。

送出的骨架跟 `sell.py` 完全一樣，連函式都共用 `jumpmap` 那組已定位的值。
"""
from __future__ import annotations

import struct

# ★ 下面每個代號與長度都是**遊戲自己 push 進建包函式的那兩個立即值**，
#   一律反組譯得來、沒有一個是推的。位址寫的是 2026-08-11 那版（改版會位移，
#   靠 verify_sigs 那套；代號與長度屬於通訊協定，只有大更新改協定才會變）。

# 出處：反組譯 makestart → 0x5D47AF 的 `push 6 / push 0x36`
OP_CRAFT = 0x36
# 出處：同一支的 `push 6` —— 內文 ＝ 代號(u16) + 配方ID(u32)
BODY_CRAFT = 6
# 出處：反組譯 makestop → 0x5D485F 的 `push 2 / push 0x37`
OP_CRAFT_STOP = 0x37
# 出處：同一支的 `push 2` —— 內文只有代號(u16)，沒有參數
BODY_CRAFT_STOP = 2
# 出處：反組譯 guildcontrib → 0x5D7BAC 的 `push [esi+4] / push 0x4B`，
# 長度是 `imul esi,ebx,6` ＋ `lea eax,[esi+4]` 算出來的（4 + 6×筆數）
OP_CONTRIB = 0x4B
# 出處：同上的 `lea eax,[esi+4]` —— 代號(u16) + 筆數(u8) + 旗標(u8)
CONTRIB_HEAD = 4
# 出處：同上的 `imul esi,ebx,6` —— 每筆 貢獻編號(u16) + 組數(u32)
CONTRIB_ENTRY = 6
# 出處：0x5D7C58 的 `cmp ebx,0x64` —— 遊戲自己寫死的上限，超過的直接不寫進包
CONTRIB_MAX = 100
# 出處：OnGuildContrib 的 Lua bytecode 是 `guildcontrib(false)`（實機倒出來的）
CONTRIB_OVERFLOW_OK = 0
# ⚠ 避開別人用的暫存區：lua 0、jumpmap 0x100、sell 0x140、exchange 0x180、
#   team 0x1C0。（scratch 從 mover 區塊 +0x800 起，還有 0x800 可用。）
SCRATCH_OFF = 0x200
CALL_TIMEOUT = 1.0
_PTR_LO, _PTR_HI = 0x10000, 0x7FFF0000


def _fns():
    """建包／送出函式與連線 —— 跟 jumpmap、sell 共用同一組已定位的值。"""
    from app.game import jumpmap
    return jumpmap.BUILD_FN, jumpmap.SEND_FN, jumpmap.CONN_PTR


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw and len(raw) >= 4 else 0


def _send(mover, scanner, opcode: int, body_len: int,
          payload: bytes) -> tuple[bool, str]:
    """建包 → 把 payload 寫進內文 +2 → 送出。payload 不含代號那 2 bytes。

    ⚠ 每一步都擋在前面：寫失敗就不送（半成品封包＝叫伺服器解讀垃圾）、
      連線是 0 就不送（重連／換地圖時送出函式會先算位址才檢查 NULL）。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if len(payload) != body_len - 2:
        return False, f"內文長度對不上（{len(payload)} vs {body_len - 2}）"
    build_fn, send_fn, conn_ptr = _fns()
    if not build_fn or not send_fn:
        return False, "封包函式定位失敗（遊戲改版？）—— 這個功能停用"
    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(build_fn, opcode, body_len, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return False, "建封包排不進去（指令槽忙碌）"
        data = _u32(scanner, buf + 4)
        if not _PTR_LO < data < _PTR_HI:
            return False, "封包資料指標不合理"
        if payload and not mover.write(data + 2, payload):
            return False, "寫封包內容失敗"
        conn, pkt = _u32(scanner, conn_ptr), _u32(scanner, buf + 0xC)
        if not conn:
            return False, "還沒連上線 —— 可能正在重連"
        if not _PTR_LO < pkt < _PTR_HI:
            return False, "封包指標不合理"
        if mover.call_sync(send_fn, conn, pkt,
                           timeout=CALL_TIMEOUT) is None:
            return False, "送出排不進去（指令槽忙碌）"
    return True, ""


# ---------------------------------------------------------------------------
# 點檯子（開製作面板）與「面板到底開了沒」
# ---------------------------------------------------------------------------
# 製作面板開著伺服器才受理 0x36（2026-08-12 使用者 A/B 實測：同一台、同一個
# 配方、同樣站在檯子旁，面板沒開送出去 20 秒毫無反應；面板開著 3 秒後材料
# −1、產物 +1）。面板是伺服器開的，我們這邊唯一能做的就是**跟遊戲一樣點它**。
#
# 點一個東西 ＝ 封包 0x05(實體ID, 動作碼)，函式就是 `attack.THIRD_FN`
# （反組譯 0x559FE0：`push 8 / push 5` → 建包 → `mov [封包+2],實體ID`
#  → `mov [0x9B6664],實體ID` → `mov [封包+6],動作碼` → 送出）。
# ★ 不必再登記一個位址：那支已經在 `locate.SIGS` 裡（attack.THIRD_FN），
#   打怪的第二包用的就是它，點檯子只是動作碼給 0。
CLICK_ACTION = 0                # 點場景物件時遊戲自己送的動作碼就是 0
# 製作面板的 Lua 全域：開著是執行期代號、關著是 0（跟 WND_BANK 同一招）。
WND_MAKE = "WND_MAKE"


def panel_open(scanner) -> bool | None:
    """製作面板開著嗎？**讀不到回 None**（不是 False）。

    ⚠ 「讀不到」跟「沒開」分開 —— 這個專案把讀不到講成「沒有」已經復發過
      六次（memory 的 bag-false-empty-guards）。讀不到時呼叫端該做的是
      「照原計畫送送看」，不是「認定面板沒開」。
    """
    from app.game import lua
    g = lua.globals_of(scanner, (WND_MAKE,))
    if g is None:
        return None
    return bool(g.get(WND_MAKE))


def click(mover, scanner, prop) -> tuple[bool, str]:
    """點一個場景物件（＝送 0x05）。`prop` 是 `scenery.Prop`。

    ⚠ **送出前當場重驗那個 ID 還指著同一個東西**（CLAUDE.md 的鐵則）：
      物件會被回收、表格那一格會被別的東西佔走，上一拍讀到的 ID 不能信。
    """
    from app.game import attack, scenery
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if not attack.THIRD_FN:
        return False, "點選函式定位失敗（遊戲改版？）—— 這個功能停用"
    if not scenery.still_there(scanner, prop):
        return False, "那個東西已經不在了（換地圖／走出視野？）"
    if mover.call_sync(attack.THIRD_FN, prop.oid, CLICK_ACTION,
                       timeout=CALL_TIMEOUT) is None:
        return False, "點選排不進去（指令槽忙碌）"
    return True, f"已點 ({prop.x:.0f},{prop.y:.0f})"


# ---------------------------------------------------------------------------
def craft(mover, scanner, recipe_id: int) -> tuple[bool, str]:
    """做**一個**。要做很多個就等做完再叫一次（客戶端自己也是這樣）。"""
    if not 1 <= recipe_id < 0x10000:
        return False, f"配方編號不合理（{recipe_id}）"
    ok, msg = _send(mover, scanner, OP_CRAFT, BODY_CRAFT,
                    struct.pack("<I", recipe_id))
    return (True, f"已送出製作（配方 {recipe_id}）") if ok else (False, msg)


def craft_stop(mover, scanner) -> tuple[bool, str]:
    """停止製作。內文只有代號，沒有參數。"""
    ok, msg = _send(mover, scanner, OP_CRAFT_STOP, BODY_CRAFT_STOP, b"")
    return (True, "已送出停止製作") if ok else (False, msg)


def donate(mover, scanner, entries) -> tuple[bool, str]:
    """捐公會。entries = [(貢獻編號, 組數), …]，組數是**組**不是個。

    ⚠ 送出前**不會**幫你確認背包真的有那麼多 —— 呼叫端要拿背包當真相
      自己算好（`bag` 是唯一可信的來源）。
    """
    rows = [(int(c), int(n)) for c, n in entries if int(n) > 0]
    if not rows:
        return False, "沒有要捐的東西"
    # 遊戲自己就是「超過 100 筆的後面直接不寫」，我們照抄 —— 但**要說出來**。
    # 安靜地少捐幾種跟「捐完了」長得一模一樣（CLAUDE.md 禁止的安靜做錯事）。
    dropped = max(0, len(rows) - CONTRIB_MAX)
    rows = rows[:CONTRIB_MAX]
    payload = struct.pack("<BB", len(rows), CONTRIB_OVERFLOW_OK)
    for cid, groups in rows:
        payload += struct.pack("<HI", cid & 0xFFFF, groups)
    body = CONTRIB_HEAD + CONTRIB_ENTRY * len(rows)
    ok, msg = _send(mover, scanner, OP_CONTRIB, body, payload)
    if not ok:
        return False, msg
    total = sum(n for _c, n in rows)
    tail = f"　⚠ 一包最多 {CONTRIB_MAX} 種，剩下 {dropped} 種要再捐一次" \
        if dropped else ""
    return True, f"已送出捐獻：{len(rows)} 種、共 {total} 組{tail}"
