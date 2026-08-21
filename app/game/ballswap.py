"""兩個分頁共用的「換球插曲」：先讓精靈停手，換完再交還。

為什麼要停精靈
--------------
⚠⚠ **精靈正在採集／製作的時候，遊戲不讓你換裝**（使用者 2026-08-21 實機
   說明：「正在生產或製作的時候會卡住不給換，要按 ESC 取消，換完再繼續」）。
   採集狀態同一回事 —— 那也是 `_escape_gather` 早就在做的事（不按 ESC 連
   天使之翼都用不了）。

所以只要**精靈主開關是開的**（自動練技、自動採集都是），換球前就要：

    關主開關 → 按 ESC 退出目前狀態 → 換球 → 主開關開回去

⭑ 只動「主開關」這一個旋鈕，其他（自動採集、練習技能、中心點、目標資源
  清單）**一律不碰** —— 各分頁自己的看門狗每一拍都在把它們往回推，
  我們多做一次只會跟它打架。交還主開關之後下一拍它們就自己接上了。
⭑ 純掛機（精靈本來就沒開）走這條路等於什麼都沒做，所以兩邊共用同一支。

⚠ 這支會 sleep 好幾秒到幾十秒，**只能在背景執行緒上跑**；跑的期間分頁要
  用自己的 `_ball_busy` 讓看門狗讓路，不然它會在中途把精靈又打開。
"""
from __future__ import annotations

import time

from app.core import window as win
from app.game import balls, robot

# ★ 送鍵走 win.send_key（它會按住 40ms；不按住只有 26.7% 會中，
#   見 memory 的 key-send-hold）。
VK_ESCAPE = 0x1B
# 按完 ESC 等遊戲真的退出採集／製作狀態再動手。
# ⚠ 別設太短：produce_tab 那邊「按 ESC 再用翼」實測也要等 1.2 秒。
ESC_SETTLE = 1.5
# 交還主開關之後等一下，讓精靈自己接回去（分頁的看門狗下一拍會補齊其他旗標）。
RESUME_SETTLE = 0.5


def swap_with_pause(mover, scanner, cur, pool, say=None, on_buy=None,
                    hwnd: int = 0) -> tuple[bool, str]:
    """把飾品欄的球全部換掉；**精靈開著的話先請它停手，換完交還**。

    回 `(成功?, 給人看的訊息)`。`say` / `on_buy` 直接傳給 `balls.run_swap`。
    """
    was_run = False
    try:
        was_run = bool(robot.is_run(scanner))
    except Exception:                              # noqa: BLE001
        was_run = False                            # 讀不到就當它沒開（不亂關）

    if was_run:
        if say:
            say("精靈正在跑 → 先請它停手（不然遊戲不給換裝）")
        try:
            robot.set_run(mover, scanner, False)
        except Exception:                          # noqa: BLE001
            pass                                   # 關不掉就照樣試，頂多換不成
        if hwnd:
            try:
                win.send_key(hwnd, VK_ESCAPE)      # 退出採集／製作狀態
            except Exception:                      # noqa: BLE001
                pass
        time.sleep(ESC_SETTLE)

    try:
        ok, msg = balls.run_swap(mover, scanner, cur, pool,
                                 say=say, on_buy=on_buy)
    finally:
        # ⚠⚠ **一定要在 finally 裡交還**：中間任何一步丟例外都不能把使用者的
        #   精靈留在關掉的狀態 —— 那等於安靜地把他的掛機停了。
        if was_run:
            if say:
                say("換完了 → 把精靈主開關開回去")
            try:
                robot.set_run(mover, scanner, True)
            except Exception:                      # noqa: BLE001
                pass
            time.sleep(RESUME_SETTLE)
    return ok, msg
