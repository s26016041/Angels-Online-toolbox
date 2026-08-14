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

⚠ REWARD_IDS 是照 onlinegift.xml 寫死的。改版增減獎勵格要跟著改
  （目前 6 格已經涵蓋在線 60 分鐘的全部獎勵）。
"""
from __future__ import annotations

from app.game import attack

# ★ 泛用送包的「領取在線獎勵」種類碼，函式沿用 attack.SELECT_FN。出處：攔包
#   呼叫鏈 (0x48, 編號)＋OnClickABOnlineRaward 的 bytecode `netcommand(72, d)`（見檔頭）。
CLAIM_CODE = 0x48

# 獎勵格編號（onlinegift.xml 的「編號」欄，1 起算）。
REWARD_IDS = (1, 2, 3, 4, 5, 6)


def claim(mover, reward_id: int) -> bool:
    """領一格。排不進指令槽時回 False（跟遊戲按鈕同一包，可安全重按）。"""
    if not (mover and mover.active):
        return False
    return attack._send(mover, ((attack.SELECT_FN, (CLAIM_CODE, reward_id)),))
