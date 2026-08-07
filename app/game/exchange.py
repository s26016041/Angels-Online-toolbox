"""兌換系統：把材料換成獎賞，不必開視窗、不必在清單上勾。

    ent = exchange.find(scanner, reward=25832, material=5346)   # 翔宇聖翼／兌換券
    exchange.open_shop(mover, ent.group)                        # 先開那個兌換商店
    exchange.confirm(mover, scanner, ent.id, 次數)              # 再送出兌換

兩包
----
    0x5D3D97(0x49, 群組)     ← 開兌換商店（泛用送包，見 generic-send-fn）
    封包 0x148　內文 10：代號(u16) + 兌換編號(u32) + 次數(u32)

## 怎麼找到的（2026-08-07）

使用者提供「打開兌換介面 → 換取」的擷取。6 包裡真正有關的只有兩包
（另外是心跳、還有他當下自己在動作／放技能的 0x07 與 0x06）：

  · 步驟 1　`0x589B65　參數 (0x49, 0x244)` → `0x5D3D97(0x49, 580)`。
    0x244 = 580 正是 `exchangegroup.xml` 第 8 個兌換商店的群組。
    Lua `OnClickABExchange` 的 bytecode 也是這樣：
    `d = window.getappdata(btn); if 0 < d then CloseActBoardWnd(); netcommand(73, d)`。

  · 步驟 5　返回位址 `0x5D415A` 就在 `game.exchangeconfirm`(0x599E90) 叫的
    組包函式 `0x5D40C9` 裡面。反組譯那段：

        push 0xA / push 0x148 / call 0x50DF6E   ; 建封包（內文 10）
        mov [資料+2], [節點+0x10]               ; 兌換編號
        mov [資料+6], [節點+0x14]               ; 次數
        call 0x711130                           ; 送出

    節點是兌換視窗 `+0x198` 那個 std::map 的節點（key=編號、value=次數），
    key 抄自「列 +0x8C 指到的兌換記錄的第一個欄位」（`0x642950` 那段）。

## ★ 編號不寫死：直接讀客戶端載進來的兌換表

那個「第一個欄位」是什麼，是**在記憶體裡驗出來的**，不是照 xml 猜的：
全記憶體找「+0x0C 是獎賞、+0x2C 是材料」的記錄，整個行程只命中一筆 ——

    0x2881F630: 編號=39 群組=580 獎賞=25832×5 材料=5346×1 排序=41

跟 `exchange.xml` 的 `<exchange 編號="39" 群組="580" 獎賞1="25832"
獎賞1數量="5" 材料1="5346" 材料1數量="1" 排序="41"/>` 一字不差。

★ 所以 `find()` 是**用「換得到什麼、要花什麼」去找編號**，改版把編號重排也
  照樣對；找不到就回 None，呼叫端要**拒絕送出**（見下面那條）。

⚠⚠ **找不到就不要送**。群組 580 裡有幾十筆同樣「1 張券」的兌換
  （20級靈光、各種藥水…），編號猜錯不會失敗，只會換到別的東西 ——
  券就這樣沒了。所以寧可不送。

⚠ 記錄的**開頭 4 bytes 就是編號**，這點是靠上面那次比對確立的；欄位表：

    +0x00 編號   +0x04 群組   +0x08 字串指標
    +0x0C 獎賞×4 +0x1C 獎賞數量×4  +0x2C 材料×4  +0x3C 材料數量×4
    +0x4C 排序                                   （一筆 0x60，陣列間距 104）

⚠ 掃描要 1.3 秒（全記憶體）—— **不要放在 GUI 執行緒上一次跑完**，
  用 `finder()` 這個產生器分次跑（見 daily_tab）。表是資源包來的，
  每台分身內容都一樣，所以整個工具箱**只要掃一台**就夠了。
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from app.game import attack

# 泛用送包的「開啟兌換商店」代碼（參數＝群組）。函式沿用 attack.SELECT_FN。
OPEN_CODE = 0x49

# 兌換確認封包。組包／送出／連線指標跟 jumpmap 共用已定位好的那組（見 _fns）。
OPCODE = 0x148
BODY = 10                    # 代號(u16) + 編號(u32) + 次數(u32)
SCRATCH_OFF = 0x180          # 相對 mover.scratch()；避開 jumpmap 0x100、sell 0x140
CALL_TIMEOUT = 1.0

# --- 兌換記錄的版面 ------------------------------------------------------
REC_ID = 0x00
REC_GROUP = 0x04
REC_REWARD = 0x0C            # 獎賞 4 格
REC_REWARD_N = 0x1C          # 獎賞數量 4 格
REC_MATERIAL = 0x2C          # 材料 4 格
REC_MATERIAL_N = 0x3C        # 材料數量 4 格
REC_SORT = 0x4C
REC_SPAN = 0x50              # 讀到排序就夠了
SLOTS = 4


@dataclass(frozen=True)
class Entry:
    """一筆兌換：花 materials 換 rewards。"""

    id: int                  # 兌換編號 —— 封包填的就是這個
    group: int               # 兌換商店群組 —— 開商店那包填的
    addr: int                # 記錄在記憶體裡的位址（除錯用）
    rewards: tuple[tuple[int, int], ...]     # ((道具, 數量), …)
    materials: tuple[tuple[int, int], ...]

    @property
    def reward(self) -> tuple[int, int]:
        return self.rewards[0] if self.rewards else (0, 0)

    @property
    def material(self) -> tuple[int, int]:
        return self.materials[0] if self.materials else (0, 0)

    def times_for(self, have: int) -> int:
        """手上有 have 個材料，最多能換幾次。材料數量不合理就回 0。"""
        need = self.material[1]
        return have // need if need > 0 else 0


def _parse(raw: bytes, off: int) -> Entry | None:
    """把一段記憶體當成兌換記錄解析；不像記錄就回 None（不猜）。"""
    if off < 0 or off + REC_SPAN > len(raw):
        return None
    rid, group = struct.unpack_from("<II", raw, off + REC_ID)
    if not (1 <= rid <= 60000 and 1 <= group <= 60000):
        return None
    rw = struct.unpack_from(f"<{SLOTS}I", raw, off + REC_REWARD)
    rn = struct.unpack_from(f"<{SLOTS}I", raw, off + REC_REWARD_N)
    mt = struct.unpack_from(f"<{SLOTS}I", raw, off + REC_MATERIAL)
    mn = struct.unpack_from(f"<{SLOTS}I", raw, off + REC_MATERIAL_N)
    return Entry(
        id=rid, group=group, addr=0,
        rewards=tuple((a, b) for a, b in zip(rw, rn) if a),
        materials=tuple((a, b) for a, b in zip(mt, mn) if a),
    )


def finder(scanner, reward: int, material: int, budget_ms: float = 25.0):
    """產生器：分次全掃找那筆兌換。

    每跑 `budget_ms` 毫秒就 `yield None` 讓出去（GUI 才不會凍住），找到就
    `yield Entry` 然後結束；整個掃完沒找到就直接結束（呼叫端收到 StopIteration）。

    reward / material 都要對得上才算 —— 只對一邊很容易撞到別筆兌換。
    """
    pat = struct.pack("<I", reward)
    t0 = time.perf_counter()
    for base, size in scanner._iter_regions(writable_only=True):
        if base + size > 0xFFFFFFFF:
            continue
        raw = scanner._read_region(base, size)
        if raw:
            # ⚠⚠ 這行 bytes() **不能拿掉來省複製**：這是產生器，下面的
            #   `yield` 會把控制權交回 GUI 執行緒，期間別人會再讀記憶體 ——
            #   而 _read_region 回的是共用緩衝的視圖，會被蓋掉。
            #   （順帶：memoryview 也沒有 .find。）
            raw = bytes(raw)
            i = raw.find(pat)
            while i >= 0:
                # 命中的是「獎賞1」的話，記錄開頭在它前面 REC_REWARD
                got = _parse(raw, i - REC_REWARD)
                if (got is not None
                        and got.reward[0] == reward and got.reward[1] > 0
                        and got.material[0] == material
                        and got.material[1] > 0):
                    yield Entry(id=got.id, group=got.group,
                                addr=base + i - REC_REWARD,
                                rewards=got.rewards, materials=got.materials)
                    return
                i = raw.find(pat, i + 4)
        if (time.perf_counter() - t0) * 1000 >= budget_ms:
            yield None
            t0 = time.perf_counter()


def find(scanner, reward: int, material: int) -> Entry | None:
    """一次掃完（約 1.3 秒）。**GUI 執行緒請改用 `finder()`**。"""
    for got in finder(scanner, reward, material, budget_ms=1e9):
        if got is not None:
            return got
    return None


# --- 送封包 --------------------------------------------------------------
def _fns():
    """組包／送出／連線指標 —— 跟 jumpmap 共用同一組已定位的值。"""
    from app.game import jumpmap
    return jumpmap.BUILD_FN, jumpmap.SEND_FN, jumpmap.CONN_PTR


def _u32(scanner, addr: int) -> int:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else 0


def open_shop(mover, group: int) -> bool:
    """開啟某個兌換商店（伺服器會回清單，客戶端跟著開視窗）。

    ⚠ 兌換之前一定要先送這包 —— 跟賣東西前要先跟商人說話同一個道理
      （見 [[npc-sell-packet]]），使用者的擷取也是這個順序。
    """
    if not (mover and mover.active and group > 0):
        return False
    return attack._send(mover, ((attack.SELECT_FN, (OPEN_CODE, group)),))


def close_window(mover, scanner) -> tuple[bool, str]:
    """關掉兌換商店視窗（送了開商店那包之後客戶端會自己開起來）。

    叫遊戲自己的 `CreateExchangeWnd` —— 它是**開關**：

        if window.isexist(WND_EXCHANGE) then destroy; WND_EXCHANGE = 0; return
        else 建一個新的

    ⚠⚠ 所以**一定要先確認 WND_EXCHANGE 不是 0**，不然會反而開出一個空視窗。
      這裡先用 `lua.get_global` 讀那個全域，是 0 就什麼都不做。

    ⚠ 碰 UI 要小心（[[jumpmap-teleport]] 記過：查那類視窗的清單內容 4/4 卡死）。
      這支**只叫遊戲自己的開關函式**、不查詢任何清單，等於替使用者按一下
      那顆按鈕；關不掉也只是視窗留著，不影響已經送出的兌換。
    """
    from app.game import lua
    handle = lua.get_global(mover, scanner, "WND_EXCHANGE")
    if not handle:
        return False, "視窗沒開著"
    ok, val = lua.call(mover, scanner, "CreateExchangeWnd")
    return (True, "") if ok else (False, str(val))


def confirm(mover, scanner, entry_id: int, times: int) -> tuple[bool, str]:
    """送出兌換：`entry_id` 換 `times` 次。回傳 (送出了嗎, 說明)。

    只保證「封包送出去了」；材料不足、沒開商店之類由伺服器擋下來。
    """
    if not (mover and mover.active):
        return False, "跳板沒裝好"
    if entry_id <= 0 or times <= 0:
        return False, "編號或次數不合理"
    build_fn, send_fn, conn_ptr = _fns()

    with mover.lock:
        buf = mover.scratch() + SCRATCH_OFF
        mover.write(buf, b"\0" * 16)
        if mover.call_sync(build_fn, OPCODE, BODY, ecx=buf,
                           timeout=CALL_TIMEOUT) is None:
            return False, "建封包排不進去（指令槽忙碌，再按一次）"
        data = _u32(scanner, buf + 4)
        if not 0x10000 < data < 0x7FFF0000:
            return False, "封包資料指標不合理"
        # 代號已由建構函式寫在 +0；我們接著寫編號與次數，剛好填滿內文 10 bytes。
        # ⚠ 寫失敗就不要送 —— 半成品送出去等於叫伺服器解讀垃圾。
        if not mover.write(data + 2, struct.pack("<II", entry_id, times)):
            return False, "寫封包內容失敗"
        # ⚠⚠ 連線與封包指標送出去之前先擋掉 0：重連／載地圖時 [conn] 是 0，
        #   而送出函式是拿它算完位址才檢查 NULL（跟 jumpmap、sell 同一個坑）。
        conn, pkt = _u32(scanner, conn_ptr), _u32(scanner, buf + 0xC)
        if not conn:
            return False, "還沒連上線 —— 可能正在重連"
        if not 0x10000 < pkt < 0x7FFF0000:
            return False, "封包指標不合理"
        if mover.call_sync(send_fn, conn, pkt,
                           timeout=CALL_TIMEOUT) is None:
            return False, "送出排不進去（指令槽忙碌，再按一次）"
    return True, ""
