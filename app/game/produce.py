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
# 開一整批製作：選配方 → 設數量 → makeadd → makestart，讓**客戶端自己續做整批**
# ---------------------------------------------------------------------------
# 2026-08-12 傍晚實機攔包＋實測定案（見 memory craft-donate-storage-packets）：
# 面板開著時，照這步驟做，客戶端就自己每 ~2.9 秒送一包 0x36 做完整批（續做函式
# 0x5C32E3），做好一個把製作清單那列數量 −1、到 0 移除該列、清單空了自然停。
# ★ **不能靠工具箱自送 0x36**：只做得出第 1 個，之後伺服器拒收（跳「製作物品
#   失敗」對話框），還會卡住遊戲訊息迴圈。要走這條「填清單→makestart→放手」。
#
# GET_CTRL(ecx=[0x9B669C+0xC], window, 控制項id) → 控制項指標（已 AOB 登記）。
GET_CTRL = 0x00624FCC
WORLD_PTR = 0x009B669C
# 三個控制項 id（UI 層常數，跟封包代號一樣屬「大更新改協定才會變」）：
CTRL_RECIPES = 0xAFE          # 配方清單（選要做的配方）
CTRL_QTY = 0xB03              # 數量輸入框
CTRL_MAKELIST = 0xB0F         # 製作清單（makestart 讀它、客戶端續做也讀它）
# 控制項裡的結構偏移（反組譯出處見下）：
#   0x625487：item = [ctrl+0x150 + index*4]；資料 = [item+0x8c]
CTRL_VEC_FIRST, CTRL_VEC_LAST = 0x150, 0x154
ITEM_DATA_OFF = 0x8C          # 配方清單：=配方ID；製作清單：=配方<<16|剩餘數量
#   0x625252：找第一個 [item+6]!=0 的列＝選中的那一列
ITEM_SEL_OFF = 0x06
#   數量框取值虛擬函式 0x625E9D＝`mov eax,[ctrl+0xf8]` → 字串緩衝指標，
#   makeadd 對它做**窄字元 atoi**（0x757F96）→ 要寫**窄 ASCII 數字**。
QTY_STR_OFF = 0xF8
_CALL_T = 0.6
# make_batch 專用回傳：開到的面板裡**沒有**這個配方（多半是別種生產的檯子）。
# 呼叫端看到它就該「關掉這個面板、換下一個站台點」，而不是在原面板一直重試。
WRONG_PANEL = "面板裡沒有這個配方（可能是別種生產的檯子）"


def _ctrl(mover, scanner, wnd: int, ctrl_id: int):
    """取製作面板裡某個控制項的指標。拿不到回 None。"""
    if not GET_CTRL:
        return None
    thisptr = _u32(scanner, _u32(scanner, WORLD_PTR) + 0xC)
    if not _PTR_LO < (thisptr or 0) < _PTR_HI:
        return None
    ptr = mover.call_sync(GET_CTRL, wnd, ctrl_id, ecx=thisptr, timeout=_CALL_T)
    return ptr if ptr and _PTR_LO < ptr < _PTR_HI else None


def _rows(scanner, ctrl: int) -> list[int]:
    """控制項的項目指標陣列（vector）。"""
    first = _u32(scanner, ctrl + CTRL_VEC_FIRST)
    last = _u32(scanner, ctrl + CTRL_VEC_LAST)
    if not first or last is None or last < first:
        return []
    n = (last - first) // 4
    return [_u32(scanner, first + i * 4) for i in range(min(n, 4096))]


def make_batch(mover, scanner, wnd: int, recipe_id: int,
               qty: int) -> tuple[bool, int, int, str]:
    """開一整批：在**已開著的**面板裡選配方、設數量、makeadd、makestart。

    回 (成功嗎, 真的排進製作清單的數量, 製作清單控制項指標, 訊息)。
    成功之後**客戶端會自己做完整批**（別再自送 0x36）。

    ★ 「真的排進去的數量」可能**比要求的少**（makeadd 自己會依材料／負重
      ／背包裁掉）—— 呼叫端要拿這個數字當真相，不能拿自己要求的 qty。
    ★ 控制項指標給呼叫端**監看進度**用：`make_left()` 純讀那列「還剩幾個」，
      那才是遊戲自己的進度（吃了製作加倍、被打斷都反映在上面）。

    ⚠ 面板要先開著（呼叫端用 `produce.click(站台)` 開）。`wnd` 傳 `WND_MAKE`
      的值（`panel_open` 讀得到）。
    """
    if not (mover and mover.active):
        return False, 0, 0, "跳板沒裝好"
    if not GET_CTRL:
        return False, 0, 0, "取控制項函式定位失敗（遊戲改版？）—— 這個功能停用"
    if not 1 <= recipe_id < 0x10000 or qty <= 0:
        return False, 0, 0, f"配方或數量不合理（{recipe_id}, {qty}）"
    rec_c = _ctrl(mover, scanner, wnd, CTRL_RECIPES)
    qty_c = _ctrl(mover, scanner, wnd, CTRL_QTY)
    mk_c = _ctrl(mover, scanner, wnd, CTRL_MAKELIST)
    if not (rec_c and qty_c and mk_c):
        return False, 0, 0, "面板控制項讀不到（面板還沒開好？）"

    # ① 選配方：清掉所有列的選取旗標，只把目標那列設 1（0x625252 認第一個 !=0）
    target = None
    for it in _rows(scanner, rec_c):
        if not (it and _PTR_LO < it < _PTR_HI):
            continue
        mover.write(it + ITEM_SEL_OFF, b"\x00")
        if _u32(scanner, it + ITEM_DATA_OFF) == recipe_id:
            target = it
    if target is None:
        # ★ 開到的面板不是這個配方的生產類別（點到別種站台）。回 WRONG_PANEL，
        #   呼叫端會關掉它、換下一個站台再點（見 produce_tab._craft_step）。
        return False, 0, mk_c, WRONG_PANEL
    if not mover.write(target + ITEM_SEL_OFF, b"\x01"):
        return False, 0, mk_c, "選配方寫入失敗"

    # ② 設數量：往數量框的字串緩衝寫窄 ASCII（makeadd 讀不到數字就不加）
    sbuf = _u32(scanner, qty_c + QTY_STR_OFF)
    if not (sbuf and _PTR_LO < sbuf < _PTR_HI):
        return False, 0, mk_c, "數量框讀不到"
    if not mover.write(sbuf, str(int(qty)).encode("ascii") + b"\0"):
        return False, 0, mk_c, "設數量寫入失敗"

    # ③ makeadd → 把（選中配方 + 數量）加進製作清單
    from app.game import lua
    ok, _v = lua.call(mover, scanner, "game.makeadd", wnd)
    if not ok:
        return False, 0, mk_c, f"makeadd 沒成功（{_v}）"
    added = 0
    for it in _rows(scanner, mk_c):
        data = _u32(scanner, it + ITEM_DATA_OFF) or 0
        if (data >> 16) == recipe_id:
            added = max(added, data & 0xFFFF)
    if added <= 0:
        # makeadd 有前置檢查（材料/等級/數量），不合就不加、也不報錯
        return False, 0, mk_c, \
            "makeadd 沒把配方排進去（材料不足／數量 0／等級不夠？）"

    # ④ makestart → 送首包、客戶端接手續做整批
    ok, _v = lua.call(mover, scanner, "game.makestart", wnd)
    if not ok:
        return False, added, mk_c, f"已排 {added} 個但 makestart 沒成功（{_v}）"
    return True, added, mk_c, ""


def make_left(scanner, mk_ctrl: int, recipe_id: int) -> int | None:
    """這個配方在製作清單裡**還剩幾個**（純讀，開一批之後拿來監看進度）。

    ★ 這是**遊戲自己的進度數字**：做好一個那列 −1、到 0 移除該列。拿它當
      進度比「數材料變少」準 —— 遊戲排得比要求少（負重／背包）、吃了製作
      加倍、被系統打斷，全都反映在這個數字上。
    ⚠ `mk_ctrl` 是開批那一刻拿的控制項指標，面板一關就懸空 —— 呼叫端要先
      確認 `panel_open()` 是 True 才來讀；這裡再驗一次指標與 vector 合理性，
      驗不過回 **None**（不是 0）：「讀不到」≠「做完了」
      （bag-false-empty-guards 那類誤報復發過六次）。回 0 ＝清單裡真的
      沒有這個配方＝這一批做完了。
    """
    if not (mk_ctrl and _PTR_LO < mk_ctrl < _PTR_HI):
        return None
    first = _u32(scanner, mk_ctrl + CTRL_VEC_FIRST)
    last = _u32(scanner, mk_ctrl + CTRL_VEC_LAST)
    if first == 0 and last == 0:
        return 0                       # 沒配置過的空 vector＝清單空
    if not (_PTR_LO < first < _PTR_HI and first <= last
            and last - first <= 4096 * 4):
        return None
    left = 0
    for i in range((last - first) // 4):
        it = _u32(scanner, first + i * 4)
        if not _PTR_LO < it < _PTR_HI:
            continue
        data = _u32(scanner, it + ITEM_DATA_OFF) or 0
        if (data >> 16) == recipe_id:
            left = max(left, data & 0xFFFF)
    return left


def make_stop(mover, scanner) -> tuple[bool, str]:
    """停止整批製作（送 0x37）。跟 `craft_stop` 同一包，換個看得懂的名字。"""
    return craft_stop(mover, scanner)


def close_panel(mover, scanner) -> bool:
    """關掉製作面板（`OnMakeClose`，也會送 makestop）。點錯站台時用來換一個再點。

    ⚠ 開關視窗在這個專案是安全操作（查清單內容才會卡死，見 lua-engine）。
    """
    if not (mover and mover.active):
        return False
    from app.game import lua
    ok, _v = lua.call(mover, scanner, "OnMakeClose")
    return bool(ok)


# ---------------------------------------------------------------------------
def craft(mover, scanner, recipe_id: int) -> tuple[bool, str]:
    """做**一個**。⚠ 只做得出第一個就會被拒（見 make_batch 的說明）——
    正常製作請走 `make_batch`。這支保留給診斷／單發用。"""
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
