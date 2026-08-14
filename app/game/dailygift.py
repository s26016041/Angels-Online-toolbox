"""領取在線獎勵：呼叫遊戲自己的泛用送包函式，一格一包。

    dailygift.claim(mover, 3)     # 領第 3 格（在線 20 分鐘那格）

原理跟 `channel.py`／`energy.py` 一樣 —— 同一個泛用送包函式，只差種類碼：

    0x5D3D97(0x47, 分流編號)      ← 切換分流（channel.switch）
    0x5D3D97(0x38, 1)             ← 能量晶化（energy.roll）
    0x5D3D97(0x48, 獎勵編號)      ← 領取在線獎勵   ★

`0x5D3D97` 就是 `attack.SELECT_FN`（會被 `locate.warm()` 自動重新定位）。
cdecl、兩個整數、**沒有 this**，加解密與送出都由客戶端自己做。

## 怎麼找到的（2026-08-07）

使用者提供按「領取」時的封包擷取：8 包裡 6 包走泛用送包（另外 2 包是心跳，
`0x5D95FD` 那條鏈），呼叫鏈 `0x589B65　參數 (0x48, 1, …)`。

**參數沒有用猜的**（memory 鐵則：代碼有規律不代表參數有規律）——
用 `tools/dump_lua_fn.py` 倒出按鈕處理常式的 bytecode 確認：

    OnClickABOnlineRaward(btn):
        d = window.getappdata(btn)     -- 該列按鈕的 appdata
        if 0 < d then
            game.netcommand(72, d)     -- 72 = 0x48
        end

appdata 是 C++（`game.updateABOnlineRawardData`，0x59AD26）填的**獎勵編號**：
`setting/base/onlinegift.xml` 正好 6 筆（編號 1~6，在線 0/10/20/30/40/60 分鐘），
跟擷取到的 6 包一一對上，而且第一包參數就是 1。

## 為什麼 6 格全送、不先判斷領不領得到

遊戲自己的守門只有「appdata > 0 才送」（還沒到時間的格子 appdata 是 0）。
我們沒有那份 appdata（在 UI 物件裡，讀它要碰視窗 —— 見 [[jumpmap-teleport]]
碰那類清單 4/4 卡死的教訓），所以 6 格全送：

  · 送的跟使用者手點「可領的格子」是**一字不差的同一包**；
  · 還沒到時間／已領過的格子，判定本來就在伺服器 —— 客戶端不送只是省流量，
    伺服器收到不合法的領取就忽略（跟連點兩下同一格一樣）。

★ 2026-08-14：獎勵格編號改**讀遊戲載進記憶體的 OnlineGift 表**（`reward_ids()`），
  改版增減格數自動跟上；REWARD_IDS 降級成「表讀不到時的安全退路」。
"""
from __future__ import annotations

import struct

from app.game import attack

# ★ 泛用送包的「領取在線獎勵」種類碼，函式沿用 attack.SELECT_FN。出處：攔包
#   呼叫鏈 (0x48, 編號)＋OnClickABOnlineRaward 的 bytecode `netcommand(72, d)`（見檔頭）。
CLAIM_CODE = 0x48

# 獎勵格編號的**安全退路**（onlinegift.xml 的「編號」欄，1 起算）。
# 正路是 reward_ids() 現場讀記憶體的 OnlineGift 表；讀不到才用這份。
REWARD_IDS = (1, 2, 3, 4, 5, 6)

# ★ OnlineGift 表的全域指標（跟怪物/技能表同一族查表函式）。
#   出處：反組譯 0x548365 那支 `lea ecx,[esi-1] / cmp ecx,9 / ja 錯誤 /
#   mov eax,[0x98FD9C] / mov eax,[eax+esi*4]`＋錯誤訊息表名 "OnlineGift"。
#   ⚠ 改版會位移 —— locate.py 有 AOB（dailygift.GIFT_TAB，表名字串當錨）。
GIFT_TAB = 0x0098FD9C
# ⚠ 查表本體的邊界：(id-1) <= 9 ＝ 編號 1~10（同上那段 `cmp ecx,9`）。
#   陣列只配到這裡，掃超過就是隔壁堆積的垃圾（2026-08-14 五台實測：
#   界內乾淨一致、界外各台隨機出現「長得像指標」的雜訊）。
GIFT_MAX = 10


def reward_ids(scanner) -> tuple[int, ...]:
    """現在遊戲裡真的存在的獎勵格編號（讀 OnlineGift 表）；讀不到回 REWARD_IDS。

    ⚠ 只在遊戲自己的邊界（1~GIFT_MAX）內認指標，不多讀一格；空表／讀失敗
      一律退回寫死的 REWARD_IDS —— 跟今天的行為一模一樣（安全退化）。
    """
    try:
        raw = scanner._read_bytes(GIFT_TAB, 4)
        tab = struct.unpack("<I", bytes(raw))[0] if raw else 0
        if not 0x10000 < tab < 0x7FFF0000:
            return REWARD_IDS
        body = scanner._read_bytes(tab + 4, GIFT_MAX * 4)
        if not body or len(body) < GIFT_MAX * 4:
            return REWARD_IDS
        ids = tuple(i for i, p in enumerate(
            struct.unpack(f"<{GIFT_MAX}I", bytes(body)), start=1)
            if 0x10000 < p < 0x7FFF0000)
        return ids or REWARD_IDS
    except Exception:                                      # noqa: BLE001
        return REWARD_IDS


def claim(mover, reward_id: int) -> bool:
    """領一格。排不進指令槽時回 False（跟遊戲按鈕同一包，可安全重按）。"""
    if not (mover and mover.active):
        return False
    return attack._send(mover, ((attack.SELECT_FN, (CLAIM_CODE, reward_id)),))
