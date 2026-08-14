"""回程道具 + 天使補助精靈：都是呼叫遊戲自己的函式，不必解密任何東西。

    slot, _, n = inventory.find_by_type(sc, head, recall.RECALL_ITEM)
    recall.use_item(mover, slot)      # 用掉一個回程道具 → 約 1.5 秒後回到城裡

## 使用背包物品

    0x5DB4E0(格號, 參數2)      ← 參數2 實測為 0

反組譯（位址是 2026-08-04 改版後的）：

    mov  ebx,[ebp+8]          ; 參數1 = 格號
    test ebx,ebx / js 跳掉     ; 負數不送
    cmp  ebx,0xff / jg 另一條  ; > 255 走別條（封包 0x132，格號寫成 u32）
    push 7 / push 0x2e        ; 封包 0x2E、內文 7 bytes
    mov  byte  [ecx+2],bl     ; 格號寫成 1 個位元組
    mov  dword [ecx+3],eax    ; 參數2 寫 4 個位元組

**回程不是專用封包，就是「使用第 N 格的物品」**，所以這一支也可以拿來自動吃藥、
用任何消耗品。⚠ 不能把種類 ID 塞進去 —— 那個欄位只有 1 個 byte。

## ⚠⚠ 格號一定要用 `inventory.find_by_type()` 現找

背包會重排，格號**不能寫死**；而且**不能用陣列索引** ——
`inventory.locate()` 的表頭實測偏過 6 格，用索引會叫到別的東西。
`find_by_type()` 讀的是物品自己記的格號（`+0x25`），不受表頭影響。

回程道具 = 種類 **1905**（五台都有）。先前記成 3966 是表頭偏格讀錯的。

## ⛔⛔ 天使補助精靈：**沒有可重放的封包**（2026-08-05 定案）

使用者要的是「掛機時自動回家修裝買水」，也就是官方的天使守護精靈。
兩份獨立擷取（都是「觸發 → 它自己跑完一趟」）逐包比對後結論是：

**那顆按鈕不送任何「開始」封包。** 擷取裡最前面那三包
（返回位址 `0x596154 / 0x59615D / 0x596166`）全部出自同一支函式 `0x596131`，
而它在 UI 指令表裡的名字是 **`automallrequestmalldata`**
—— 補給流程的第一步「跟伺服器要商城資料」，不是開關。

實測（雪狐）：原封不動呼叫 `0x596131()`，三包確實送出去，
**角色 30 秒完全沒動**。之後擷取到的打怪／移動／用回程，都是精靈
**已經在跑之後**它自己做的事。

同樣被排除的還有 `0x5D3D97(0x1E, 1)`：它在擷取裡出現兩次，都在跑起來之後；
單獨送回傳成功但背包沒變、角色沒動。

→ 真正的開關是**純客戶端**的，攔封包永遠看不到，要靠記憶體差分找。

### 相關線索（留給下次接手）

* UI 指令表在 **`0x7DD040`，632 個 `[名字, 函式]`**（見 [[ui-command-table]]），
  裡面有 `setrobotisrun` / `repairall` / `autosupply*` / `robotvar_*`。
* `setrobotisrun` 拆到底只是寫一個全域：`[0x9CFBA4] = 0`（開）/ `-1`（關），
  那其實是設定字串 `"AutoFight"` 對應的旗標。對黑狐寫 0 沒有任何效果。
* 觀察到的相關性：正在跑的三台都是 `0`，站著不動的黑狐是 `-1`。

## 回程道具本身是通的

`use_item()` 已實測可用（黑狐 隱者溼地 → 永夜城）。就算精靈叫不動，
「自己做一套回家修裝」也有現成的基礎。位址由 `app/game/locate.py` 自動定位。
"""
from __future__ import annotations

from app.game import attack

# 使用背包物品。⚠ 這個值會被 locate.warm() 依 AOB 重新定位，不要在別處複製。
USE_ITEM_FN = 0x005DB4E0

# ★ 回程道具（天使之翼）的種類 ID。出處：五台背包實測對照
#   （⚠ 舊記的 3966 是表頭偏格讀錯，見 memory recall-item-packet）。
RECALL_ITEM = 1905


def use_item(mover, slot: int) -> bool:
    """用掉第 slot 格的東西。slot 要是**物品自己記的格號**。"""
    if not (mover and mover.active) or not 0 <= slot <= 0xFF:
        return False
    return attack._send(mover, ((USE_ITEM_FN, (slot, 0)),))
