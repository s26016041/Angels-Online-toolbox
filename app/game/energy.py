"""能量晶化：呼叫遊戲自己的泛用送包函式。

    energy.roll(mover)      # 按一次「能量晶化」

原理跟 `channel.py` 一樣 —— 同一個泛用送包函式，只差種類碼：

    0x5D3D97(0x0C, 目標實體ID)    ← 選定怪物（attack.select）
    0x5D3D97(0x47, 分流編號)      ← 切換分流（channel.switch）
    0x5D3D97(0x38, 1)             ← 能量晶化   ★

`0x5D3D97` 就是 `attack.SELECT_FN`（會被 `locate.warm()` 自動重新定位）。
cdecl、兩個整數、**沒有 this**，加解密與送出都由客戶端自己做。

## 怎麼找到的

使用者提供按下遊戲裡「能量晶化」按鈕時的封包擷取，呼叫鏈：

    0x5D3DC6　參數 (1, …)
    0x589B65　參數 (0x38, 1, …)      ← 這一層才是重點

反組譯 `0x589B60` 那道 `call` → 打到 `0x5D3D97`，`push ebx; push edi`
＝參數 `(0x38, 1)`。**看呼叫鏈上的參數，不要看封包內容**（內文是加密的）。

`0x589B27` 那個處理常式是**通用的 UI 送包指令**：從指令參數取出第 1、2 個
token 當代碼與參數再送。所以 `(0x38, 1)` 是視窗定義帶進去的常數。

遊戲裡的按鈕定義（`SETTING/BASE/WND04.XML`）：

    id=24653 「能量晶化」    <OnCommand>OnClickEnergyRoll</OnCommand>
    id=24654 「我要晶能加倍」<OnCommand>OnClickEnergyDouble</OnCommand>
    id=24655 「存入晶能」    <OnCommand>OnClickEnergySave</OnCommand>

提示文字：「每 1 點能量可進行 1 次能量晶化。按下能量晶化，隨機選取屬性。」

⚠ 另外兩顆按鈕（加倍／存入）的代碼**還不知道** —— 要各自擷取一次封包才有。
  不要用猜的（`0x39`、`0x3A` 之類），這個專案已經因為「看到數字連成一線就下
  結論」錯過兩次（見 [[channel-switch]] 的紀錄）。

⚠ 這支只是「按一下那顆按鈕」。晶化要花能量、結果隨機，**要不要按、按幾次
  是使用者的決定**，所以這裡不做任何自動連按。
"""
from __future__ import annotations

from app.game import attack

# 泛用送包函式的「能量晶化」種類碼與參數。函式本身沿用 attack.SELECT_FN。
ROLL_CODE = 0x38
ROLL_ARG = 1


def roll(mover) -> bool:
    """送一次「能量晶化」。排不進指令槽時回 False。

    mover: 已 start() 的 move.Mover（借它的跳板讓遊戲主執行緒替我們呼叫）。
    """
    if not (mover and mover.active):
        return False
    return attack._send(mover, ((attack.SELECT_FN, (ROLL_CODE, ROLL_ARG)),))
