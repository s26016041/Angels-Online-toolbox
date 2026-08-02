"""共用的警報通知：Telegram 有填就送，否則響警報 + 跳視窗。

原本這套只在收益監控分頁裡。自動掛機也需要（例如武器壞掉要停下來通知），
與其複製一份，不如抽出來共用 —— 兩邊的行為才不會慢慢走鐘。

★ 設定沿用收益監控那一份（`profit.notify_method` / `profit.telegram_id`），
  使用者只要在監控分頁設定一次，掛機這邊自動跟著走。

★ 通知真的送得出去就到此為止：**不出聲、也不跳視窗**。
  選 Telegram 的情境是「人不在電腦前」，跳一個要人按掉的視窗只會擋住畫面，
  而且電腦前如果是別人，他根本不知道那是什麼、也不該幫忙按。
  （這條是使用者明確要求的：「如果有用 TG通知 就不需要跳出警報給別人按」。）
"""
from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QObject, Signal

from app.config import config
from app.core import notify
from app.core.alarm import Alarm, AlarmDialog

KEY_METHOD = "profit.notify_method"      # "sound" | "telegram"
KEY_ROOM = "profit.telegram_id"


def telegram_room() -> str:
    """目前設定的 Telegram 群組/房間 ID；沒設定回空字串。"""
    if config.get(KEY_METHOD, "sound") != "telegram":
        return ""
    return str(config.get(KEY_ROOM, "") or "").strip()


class Notifier(QObject):
    """一個分頁一份。fire() 送通知，stop() 收掉聲音與視窗。"""

    failed = Signal(str)                 # Telegram 送失敗（在 UI 執行緒顯示）

    def __init__(self, parent, title: str = "⚠ 警報") -> None:
        super().__init__(parent)
        self._parent = parent
        self._title = title
        self._alarm = Alarm(parent)
        self._dialog: AlarmDialog | None = None
        self._pending: list[tuple[str, str]] = []

    def fire(self, who: str, msg: str) -> str:
        """送一則通知。回傳一句可以放進狀態列的說明。"""
        room = telegram_room()
        if room:
            self._send_async(room, [(who, msg)])
            return "已送出 Telegram 通知"

        self._pending.append((who, msg))
        if config.get(KEY_METHOD, "sound") == "telegram":
            # 選了 Telegram 卻沒填 ID：送不出去，維持原本行為（跳視窗但不出聲）
            note = "⚠ 未填 Telegram 群組/房間 ID，改用視窗提示"
        else:
            self._alarm.start()
            note = "已響警報"
        self._popup()
        return note

    def _popup(self) -> None:
        if self._dialog is None:
            self._dialog = AlarmDialog(self._parent, self.stop, self._title)
        body = "\n".join(f"　• {w} — {m}" for w, m in self._pending)
        self._dialog.set_text(
            "以下分身觸發了警報：\n\n" + body
            + "\n\n按「停止警報」關閉聲音並清除這份清單。")
        self._dialog.popup()

    def _send_async(self, room: str, events) -> None:
        payload = list(events)

        def worker():
            for who, msg in payload:
                ok, info = notify.send_telegram(room, who, "⚠ " + msg)
                if not ok:
                    sys.stderr.write(f"[telegram] 送出失敗 {who}: {info}\n")
                    self.failed.emit(f"⚠ Telegram 送出失敗（{who}）：{info}")

        threading.Thread(target=worker, daemon=True).start()

    def stop(self) -> None:
        self._alarm.stop()
        if self._dialog:
            self._dialog.hide()
        self._pending = []
