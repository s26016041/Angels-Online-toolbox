"""自動登入：全程寫記憶體＋呼叫遊戲自己的函式，不占鍵盤滑鼠。

    login.sign_in(mover, scanner, "s26011034", "s26016041")   # 帳密登入
    login.enter_game(mover, scanner, slot=0)                   # 進入遊戲（第 1 個角色）

使用者硬要求：自動登入**全程背景、完全不可占用實體鍵鼠**（見 memory 的
auto-login-findings）。舊做法用 pyautogui 打字，那會搶走使用者當下的鍵盤，
而且背景滑鼠點擊在這遊戲根本無效。這裡改成兩件事都交給遊戲自己做。

## 一、帳密登入

登入按鈕的完整動作就是一支 **thiscall、零參數**的函式 `0x538959`：

    選中的伺服器 = 伺服器清單控制項(0xA92) 的選取索引   ← 沒選就直接 return
    if [0x890997] != 0 或 [0x8909B9] != 0:
        跳過讀 UI，直接用現成的全域帳密        ← ★ 我們要的就是這個分支
    else:
        帳號框(0xA8E).text → 0x890980（20 bytes）
        密碼框(0xA90).text → 0x890998（32 bytes）
    關掉舊連線 → [這+0x1038] = 0 → 建 socket → connect（非同步）

連線接上之後，**送登入包的是連線物件自己的 tick** `0x538EEB`：

    if [這+0x1038] == 1 且 socket 已連上:
        if [0x8909B9] != 0:  0x537C8C(帳號, token@0x890FCC)   代號 0x14
        elif [0x890997] != 0: 0x537C78(帳號, blob@0x890D30)    代號 0x13
        else:                 0x537C64(帳號, 密碼@0x890998)    代號 0x02  ★一般帳密
        [這+0x1038] += 1        ← 所以只會送一次

⚠⚠ **兩個旗標同時決定「跳不跳過 UI」和「送哪一種登入包」**，這是本檔最彆扭
   的地方：要讓 `0x538959` 不去讀（空的）輸入框，就得舉一支旗標；但旗標舉著
   的話 tick 就不會走一般帳密那條。所以做法是**舉起 → 呼叫 → 馬上放下**：

     1. 寫 0x890980 / 0x890998
     2. FLAG_TOKEN=0、FLAG_BLOB=1      （讓 0x538959 跳過讀 UI）
     3. call 0x538959(this)
     4. FLAG_BLOB=0                    （讓 tick 走一般帳密）

   中間有沒有可能被 tick 插隊？第 3 步回來時 socket 還在**三向交握**，
   tick 的前提「socket 已連上」至少要一個網路來回（毫秒級），而第 4 步在
   微秒內就做完了。理論上仍是競態，實務上差三個數量級。
   ⚠ 這條是靜態反組譯推出來的，**還沒實機驗證**。

`this` 怎麼來：登入畫面物件用 vtable 掃出來（`find_screen()`），
再交叉驗證 +0x10/+0x14/+0x18/+0x2C 四個副 vtable —— 五個指標同時對上，
不會誤認。進遊戲之後這個物件會被釋放，掃不到就是「現在不在登入畫面」。

## 二、選頻道（分流）—— 不能省的一步

登入成功後畫面會停在「請選擇分流遊戲世界」。⚠⚠ 點那一列**做的是兩件事**：

    ① `[0x890810] = 該列編號`（控制項 id 0x9E3，選取變更的訊息）
    ② `0x537353(登入畫面物件)`（同一個 id、**另一種訊息**，`0x539E40` 那個 call）
       → 跳出「與伺服器連線中」，跟伺服器要角色清單，畫面推進到選角色

只做 ① 的話畫面會一直停在分流清單，接著送「進入遊戲」就卡在「與伺服器連線中」
—— 這正是使用者回報的症狀。2026-08-07 在使用者開放的測試分身上實跑：
寫 `[0x890810]=2` 再 `0x537353(obj)` → 1.5 秒後截圖已是選角色畫面（白狐 LV80），
登入畫面物件隨即被釋放。

## 三、進入遊戲（頻道值也在這一包裡）

角色選好之後按「進入遊戲」送的是 `0x50F880(角色格號)`（stdcall，代號 6、
內文 0x25），走**登入連線**。它組包的內容是：

    [封包+2] = 角色格號（byte，參數帶進來的）
    [封包+3] = [0x890810]      ← ★ **頻道（分流），0 起算**
    [封包+4] = 0x890814 起 0x20 bytes（連線用的識別字串）

★★ **選頻道不是獨立的一包**，就是上面那個位元組 —— 所以「選頻道」對我們來說
只是**寫一個 byte**，不必碰那個頻道選擇畫面。三處交叉印證：

  1. 五台分身實測對照：雅典娜-2 → `1`、雅典娜-3 → `2`、雅典娜-5 → `4`。
  2. `0x591B0B`（Lua 的換頻道函式）拿 `[0x890810]` 當「目前頻道」比對，
     而且前面 `cmp ecx, 8` 把上限框在 8。
  3. 跟遊戲中換分流 `0x5D3D97(0x47, 分流−1)` 同樣是 0 起算（見 [[channel-switch]]）。

⚠ 使用者原本以為卡住的是「選伺服器」，實測是**選頻道 1~5**。伺服器
（雅典娜／維納斯／邱比特）在登入畫面就選好了，`0x538959` 用的是那個
下拉選單的選取值，我們沒有碰。

呼叫端在送之前還會先寫 `[0x890BE8] = 角色格號`，我們照做 —— 因為接下來那包
「進遊戲伺服器」(`0x5D5FB7`) 是讀這個全域的。

## 伺服器清單怎麼讀（純讀，不讀檔案）

`[[0x89096C]+0x500]` ~ `+0x504` 是一個等距陣列，記錄 0x178 bytes：

    +0x00 名稱（UTF-8 內嵌）   +0x40 ip 字串   +0x50 port
    +0x54 分流數   +0x58 伺服器編號   +0x5C 分流數（同一個值，兩格）

跟 [[channel-switch]] 的 `channel.count()` 讀的是同一份表，但那邊是**全記憶體
掃描**（0.3~1 秒）而且要靠視窗標題的伺服器名 —— 登入畫面根本還沒有標題，
所以這裡改成直接從陣列邊界走訪，微秒級而且登入畫面也讀得到。
`[0x890CA8]` 是登入時選中的伺服器索引（實測 2 = 雅典娜）。

★ 使用者擷取的「進入遊戲」有 5 包，但**我們只需要送第 1 包**：
    ① 0x50F880(格號)   代號 6    ← 只有這包是「按鈕按下去」送的
    ② 0x5D5FB7()       代號 2    ┐
    ③ 0x5D604F()       代號 3    │ 都在 0x5AA3xx 的**伺服器訊息處理常式**裡，
    ④ 0x602EC9(byte)   代號 0x15E│ 是客戶端收到伺服器回應後自己送的
    ⑤ 0x5D3D97(0x1E,1) 代號 0x16 ┘
  ⚠ 順帶更正 memory：④ 不是安全密碼 —— 它設的 Lua 全域是
    `WND_PET_EQUIP_SUIT_SELECT`（寵物裝備套裝），值是從伺服器封包讀出來的。

送出前會確認 `[0x89097C]`（登入連線編號）不是 0；是 0 代表根本還沒連上
登入伺服器，送了也只是丟進 `0x711130` 的空槽，直接擋下比較好交代。

相關：[[login-packet-chain]]、[[auto-login-findings]]、[[packet-opcode-table]]、
[[aob-auto-locate]]（下面每個位址都在 locate.SIGS，改版會自動重新定位）
"""
from __future__ import annotations

import struct

# --- 函式（locate.warm() 會重新定位；定位失敗會被清成 0，Mover.call 擋下）---
LOGIN_FN = 0x00538959       # thiscall(this)：登入按鈕的完整動作
PICK_CHANNEL_FN = 0x00537353  # thiscall(this)：確定分流，畫面推進到選角色
ENTER_FN = 0x0050F880       # stdcall(角色格號)：進入遊戲

# --- 資料位址（定位失敗會保留下面寫死的值，讀錯只會「做不到」不會崩潰）---
VT_LOGIN = 0x007D6C94       # 登入畫面／登入連線物件的主 vtable
ACCOUNT = 0x00890980        # 帳號緩衝區，20 bytes
PASSWORD = 0x00890998       # 密碼緩衝區，32 bytes
FLAG_BLOB = 0x00890997      # ≠0 → 用 0x890D30 的 512-byte 憑證
FLAG_TOKEN = 0x008909B9     # ≠0 → 用 0x890FCC 的 token（啟動器社群登入）
CHAR_SLOT = 0x00890BE8      # 選中的角色格號（0 起算）
CONN_ID = 0x0089097C        # 登入連線編號；0 = 還沒連上
CHANNEL = 0x00890810        # 頻道／分流，**1 byte、0 起算**
APP_PTR = 0x0089096C        # 應用程式主物件；伺服器陣列掛在它 +0x500/+0x504
SERVER_INDEX = 0x00890CA8   # 登入時選中的伺服器索引（進陣列用）

# 伺服器記錄的版面（見檔頭）
SRV_BEGIN = 0x500
SRV_END = 0x504
SRV_STRIDE = 0x178
SRV_NAME = 0x00
SRV_PORT = 0x50
SRV_SUBSET_A = 0x54
SRV_ID = 0x58
SRV_SUBSET_B = 0x5C
MAX_SUBSET = 8              # 上限跟遊戲一致：0x591B03 的 `cmp ecx, 8`
MAX_SERVERS = 64            # 合理性上限，避免版面變了就跑一個天文數字的迴圈

# 掃到 VT_LOGIN 之後還要對上的副 vtable（建構函式 0x5363A0 一口氣寫的那組）。
# ⚠ 只取建構函式**不會再改寫**的四個：+0x30/+0x34 後面會被覆蓋成別的值。
SUB_VTABLES = {0x10: 0x007D6CAC, 0x14: 0x007D6CC4,
               0x18: 0x007D6CD0, 0x2C: 0x007D6CDC}

ACCOUNT_LEN = 20            # strncpy 0x14
PASSWORD_LEN = 32           # strncpy 0x20

# 登入那一下要建 socket、關舊連線、動 UI，比純送包的函式重得多。
CALL_TIMEOUT = 1.0
ENTER_TIMEOUT = 0.3


def _pad(text: str, size: int) -> bytes:
    """把字串補成固定長度的 C 字串；太長就截掉（遊戲自己也是 strncpy）。"""
    raw = text.encode("ascii", errors="ignore")[:size - 1]
    return raw + b"\x00" * (size - len(raw))


def _u32(scanner, addr: int) -> int | None:
    raw = scanner._read_bytes(addr, 4)
    return struct.unpack("<I", bytes(raw))[0] if raw else None


def server_info(scanner) -> tuple[str, int] | None:
    """(目前選中的伺服器名, 它有幾個分流)；讀不到／版面對不上回 None。

    ★ 純讀、微秒級，登入畫面與遊戲中都讀得到 —— 陣列邊界在主物件裡，
      不必像 [[channel-switch]] 的 `channel.count()` 那樣全記憶體掃描，
      也不必靠視窗標題（登入畫面還沒有標題）。

    ⚠ 合理性條件全部是**結構性的**（不看特定數值），所以改版增減伺服器、
      改分流數都照樣成立：兩格分流數要一致且在範圍內、port 要像 port。
      對不上就回 None，讓呼叫端退回安全的預設，而不是拿一個錯的數字用。
    """
    app = _u32(scanner, APP_PTR)
    if not app:
        return None
    beg, end = _u32(scanner, app + SRV_BEGIN), _u32(scanner, app + SRV_END)
    if not beg or not end or end <= beg:
        return None
    count = (end - beg) // SRV_STRIDE
    if not (1 <= count <= MAX_SERVERS):
        return None
    idx = _u32(scanner, SERVER_INDEX)
    if idx is None or not (0 <= idx < count):
        return None
    raw = scanner._read_bytes(beg + idx * SRV_STRIDE, 0x60)
    if not raw:
        return None
    raw = bytes(raw)
    port, sub_a, _sid, sub_b = struct.unpack_from("<IIII", raw, SRV_PORT)
    if not (1024 <= port <= 65535):
        return None
    if sub_a != sub_b or not (1 <= sub_a <= MAX_SUBSET):
        return None
    name = raw[SRV_NAME:0x40].split(b"\x00")[0].decode("utf-8", "replace")
    return name, sub_a


def read_channel(scanner) -> int | None:
    """目前的頻道（**1 起算**，給介面顯示用）；讀不到回 None。"""
    raw = scanner._read_bytes(CHANNEL, 1)
    if not raw:
        return None
    got = bytes(raw)[0] + 1
    return got if 1 <= got <= MAX_SUBSET else None


def find_screen(scanner) -> int | None:
    """登入畫面物件的位址；不在登入畫面（已進遊戲）就回 None。

    ★ 用 vtable 掃：主 vtable 在 +0，另外四個副 vtable 在固定偏移 ——
      五個指標同時對上才算數，誤判機率可以忽略。掃描約 0.6 秒。

    ⚠ VT_LOGIN 定位失敗時 locate 會保留寫死的值。那種情況下這裡多半掃不到，
      回 None（功能停用）而不是掃到別的東西。
    """
    if not VT_LOGIN:
        return None
    pat = struct.pack("<I", VT_LOGIN)
    for base, size in scanner._iter_regions(writable_only=True):
        raw = scanner._read_region(base, size)
        if not raw:
            continue
        raw = bytes(raw)
        i = raw.find(pat)
        while i >= 0:
            if all(i + off + 4 <= len(raw)
                   and struct.unpack_from("<I", raw, i + off)[0] == vt
                   for off, vt in SUB_VTABLES.items()):
                return base + i
            i = raw.find(pat, i + 1)
    return None


def sign_in(mover, scanner, account: str, password: str) -> str:
    """帳密登入。成功回空字串，失敗回一句「為什麼不行」。

    mover:   已 start() 的 move.Mover（借它的跳板讓遊戲主執行緒替我們呼叫）
    scanner: 同一個分身的 MemoryScanner（只用來掃登入畫面物件）

    ⚠ 前提是遊戲停在**登入畫面**、而且伺服器清單已經有選取（預設就選好第一個）。
      沒選的話 `0x538959` 自己會直接 return，我們這邊看起來就是「按了沒反應」。
    """
    if not (mover and mover.active):
        return "跳板還沒裝好（遊戲剛開或被防毒擋住）。"
    if not account or not password:
        return "請先填帳號與密碼。"
    obj = find_screen(scanner)
    if obj is None:
        return "找不到登入畫面 —— 這台分身可能已經進遊戲了。"

    if not mover.write(ACCOUNT, _pad(account, ACCOUNT_LEN)):
        return "寫入帳號失敗。"
    if not mover.write(PASSWORD, _pad(password, PASSWORD_LEN)):
        return "寫入密碼失敗。"
    # ⚠ 順序有意義：先把 token 那條路關掉，再舉「跳過讀 UI」的旗標。
    #   反過來的話，中間那一瞬間兩支旗標都舉著 → tick 會挑 token。
    mover.write(FLAG_TOKEN, b"\x00")
    mover.write(FLAG_BLOB, b"\x01")
    try:
        ok = mover.call_sync(LOGIN_FN, ecx=obj, timeout=CALL_TIMEOUT) is not None
    finally:
        # ⚠⚠ 一定要放下，而且要在 finally 裡 —— 留著的話 tick 會拿
        #   0x890D30（空的 512-byte 憑證）去登入，帳密白寫。
        mover.write(FLAG_BLOB, b"\x00")
    if not ok:
        return "指令槽排不進去或逾時（遊戲主執行緒正忙），等一下再按。"
    return ""


def pick_channel(mover, scanner, channel: int) -> str:
    """在「請選擇分流遊戲世界」那一頁確定分流，把畫面推進到選角色。

    channel: 頻道／分流，**1 起算**。

    ⚠⚠ **這一步不能省**（2026-08-07 實機確認）：點分流清單那一列其實做**兩件事**
      —— 寫頻道位元組，以及叫 `0x537353` 跟伺服器要角色清單。只寫位元組的話
      畫面會一直停在分流清單，接著送「進入遊戲」就卡在「與伺服器連線中」。

    實測：寫 `[0x890810]=2` 再 `0x537353(登入畫面物件)` → 1.5 秒後畫面就到
    選角色（截圖確認），登入畫面物件隨即被釋放（`find_screen()` 回 None）。
    """
    if not (mover and mover.active):
        return "跳板還沒裝好（遊戲剛開或被防毒擋住）。"
    err = _check_channel(scanner, channel)
    if err:
        return err
    obj = find_screen(scanner)
    if obj is None:
        return "現在不在分流選擇畫面（可能已經到選角色了，直接按「進入遊戲」）。"
    # ⚠ 順序照使用者點那一列時的兩個動作：先寫頻道，再確定。
    if not mover.write(CHANNEL, bytes([channel - 1])):
        return "寫入頻道失敗。"
    if mover.call_sync(PICK_CHANNEL_FN, ecx=obj, timeout=CALL_TIMEOUT) is None:
        return "指令槽排不進去或逾時（遊戲主執行緒正忙），等一下再按。"
    return ""


def _check_channel(scanner, channel: int) -> str:
    """頻道合理性；沒問題回空字串。"""
    if not (1 <= channel <= MAX_SUBSET):
        return f"頻道要在 1~{MAX_SUBSET} 之間。"
    # ⚠ 上限**照這台伺服器實際的分流數**再檢一次；讀不到就只用上面那道
    #   （寧可放行也不要因為讀不到就整個功能不能用）。
    info = server_info(scanner)
    if info is not None and channel > info[1]:
        return f"{info[0]} 只有 {info[1]} 個分流，選不到第 {channel} 個。"
    return ""


def enter_game(mover, scanner, slot: int, channel: int) -> str:
    """選頻道 + 進入遊戲，一包送出。成功回空字串，失敗回原因。

    slot:    角色格號，**0 起算**（畫面上第 1 個角色 = 0）
    channel: 頻道／分流，**1 起算**（畫面上的「雅典娜-3」就填 3）

    ⚠ 頻道不是另一包 —— 它是這一包裡的一個位元組（見檔頭）。所以不必去點
      那個頻道選擇畫面，寫下去再送就好。
    """
    if not (mover and mover.active):
        return "跳板還沒裝好（遊戲剛開或被防毒擋住）。"
    if slot < 0:
        return "角色格號不對。"
    err = _check_channel(scanner, channel)
    if err:
        return err
    if not _u32(scanner, CONN_ID):
        return "還沒連上登入伺服器 —— 請先「帳密登入」。"
    # ⚠⚠ 只寫 **1 個 byte**：0x890811 是別的東西（實測五台都是 1），
    #   寫 4 bytes 會把它一起清掉。
    if not mover.write(CHANNEL, bytes([channel - 1])):
        return "寫入頻道失敗。"
    # ⚠ 先寫全域再送包，跟遊戲自己的呼叫端（0x51041F）同一個順序：
    #   下一包「進遊戲伺服器」是讀這個全域，不是讀封包。
    if not mover.write(CHAR_SLOT, struct.pack("<I", slot)):
        return "寫入角色格號失敗。"
    with mover.lock:
        if mover.call_sync(ENTER_FN, slot, timeout=ENTER_TIMEOUT) is None:
            return "指令槽排不進去或逾時，等一下再按。"
    return ""
