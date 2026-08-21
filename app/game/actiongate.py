"""動作節流：照遊戲自己的規則排隊，被擋下來就再送一次。

為什麼需要
----------
伺服器對「同一個帳號連續做動作」有間隔限制，太快就擋下並回「操作太快」。
客戶端自己也有一份同樣的檢查（所以正常玩不會踩到），但**我們是自己建包直送、
繞過了客戶端那道關**，於是連送必被伺服器擋 —— 2026-08-21 使用者實機回報
「買完立刻領」整條流程卡住，就是這個。

間隔是**從遊戲自己挖出來的，不是猜的**（反組譯 `0x6112B7`，thiscall）：

    now = time()                  ; 0x746358 ＝ Unix 秒（GetSystemTimeAsFileTime 換算）
    elapsed = now - [this+8]      ; 64-bit
    if elapsed <= 5:  顯示字串 2369「操作太快。」→ 回 0（擋下）
    else:             [this+8] = now → 回 1（放行）

而 `0x6115AF` 就是「節流過了才送 `0x5D2A93`」的包裝 —— `0x5D2A93` 正是
**0x2F 那一族**（存倉／領商城倉庫）的送包函式。

⚠ 「大於 5」＋時鐘只有秒解析度 → 取 **6 秒**才穩（差 1 秒就被擋）。

★★ 計時是**每個遊戲行程一份、跨模組共用**的：伺服器是對帳號算的，
   商城購買、領取、換裝……全部共用同一條隊伍。所以這份計時表放在這裡，
   `mall.py` 與 `balls.py` 都來借，**不准各留一份**（各留一份就等於沒排隊）。

重試為什麼安全
--------------
`retry()` 在**每一次送出之前都先驗一次結果**：上一發其實成功、只是我們沒等到
的話就直接收工。沒有這道檢查，重送就是重複動作 —— 買東西會再扣一次點、
換裝會把剛換好的又換回去。
"""
from __future__ import annotations

import time

# ★ 官方是「必須大於 5 秒」（見檔頭反組譯），秒解析度所以取 6。
ACTION_GAP = 6.0
# 沒生效時最多送幾次。
TRIES = 3
# 驗結果的輪詢間隔。
POLL = 0.25

# 每個遊戲行程各自記「上一次送動作的時刻」（伺服器是對帳號算的）。
_last: dict[int, float] = {}


def gate(scanner, gap: float = ACTION_GAP, say=None) -> None:
    """等到離「這台上一次動作」滿 `gap` 秒。

    ⚠ 這支會 sleep，只能在背景執行緒上呼叫。
    """
    pid = getattr(scanner, "pid", 0) or 0
    wait = gap - (time.monotonic() - _last.get(pid, -999.0))
    if wait > 0:
        if say:
            say(f"等冷卻 {wait:.0f} 秒（官方限制動作要隔 {ACTION_GAP:.0f} 秒）")
        time.sleep(wait)
    _last[pid] = time.monotonic()


def forget(scanner) -> None:
    """把這台的節流計時忘掉（測試／重連後用）。"""
    _last.pop(getattr(scanner, "pid", 0) or 0, None)


# `build()` 的三種結果。★ 使用者 2026-08-21 定：「每個步驟都要確認，避免因為
#   遊戲指令忙線等等不明原因沒吃到指令」—— 所以「**沒送出去**」也要再試，
#   不能跟「沒救」混為一談。
SENT = "sent"     # 送出去了 → 接著驗結果
RETRY = "retry"   # 這次沒送成（指令槽忙碌、暫時讀不到…）→ 等一下重來
STOP = "stop"     # 沒救（跳板沒接上、位址定位失敗…）→ 立刻回報，重試沒意義


def retry(scanner, done, build, say=None, what: str = "",
          wait: float = 8.0, gaps=None, tries: int = TRIES
          ) -> tuple[bool, str]:
    """「排隊 → 送 → 驗結果 → 沒成就再送」。

    · `done()`  → True ／ False ／ **None（讀不到，不下結論）**
    · `build()` → `(狀態, 說明, 成功訊息的 callable)`，狀態見上面三個常數。
      **`RETRY` 跟 `SENT` 一樣會再繞一輪** —— 指令槽忙碌那種「遊戲根本沒
      收到」的情況，放棄等於功能無聲失敗。
    · `gaps`    → 每一次送出前要隔多久（不給就每次都用 `ACTION_GAP`）。
      第一次通常可以短一點（前一個動作可能已經隔很久了），重送就一定要
      隔滿 —— 會走到重送就代表**很可能就是被節流擋掉的**。

    ★ 每次送出前先 `done()`：這是重試安全的前提，見檔頭。
    """
    gaps = list(gaps or ())
    rounds = max(tries, len(gaps))
    last = ""
    for i in range(rounds):
        if done() is True:
            return True, f"{what}已完成"
        gate(scanner, gaps[i] if i < len(gaps) else ACTION_GAP, say)
        if say:
            say(f"{what}（第 {i + 1} 次）")
        state, why, good = build()
        if state == STOP:
            return False, why           # 沒救 → 別白等
        if state != SENT:               # 沒送出去（指令槽忙…）→ 下一輪重送
            last = why
            if say:
                say(f"{what}沒送出去（{why}）→ 等一下重送")
            continue
        end = time.monotonic() + wait
        while time.monotonic() < end:
            time.sleep(POLL)
            if done() is True:
                return True, good()
        last = f"{what}沒有生效"
    return False, (f"{last}（試了 {rounds} 次都不成"
                   " —— 被伺服器擋？條件不符？）")
