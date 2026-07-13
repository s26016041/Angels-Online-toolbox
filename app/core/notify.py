"""通知後端：把警報送到使用者自製的 Telegram 後端。

POST https://telegram-backend-…/sendMessage
Content-Type: application/json; charset=UTF-8
Body: {"id": <群組/房間ID>, "name": <帳號+角色名>, "content": <訊息>}

只用標準庫 urllib（不加相依）。打包後若缺系統憑證導致 SSL 驗證失敗，會退回不驗證
（打的是使用者自己的後端，可接受）。
"""
from __future__ import annotations

import json
import ssl
import urllib.request

TELEGRAM_URL = "https://telegram-backend-494287458629.asia-east1.run.app/sendMessage"


def send_telegram(room_id, name: str, content: str, timeout: float = 10.0):
    """送一則通知。回傳 (成功?, 狀態碼或錯誤訊息)。"""
    try:
        rid = int(str(room_id).strip())      # id 後端範例是數字，盡量轉 int
    except (TypeError, ValueError):
        rid = room_id
    body = json.dumps(
        {"id": rid, "name": name, "content": content}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        TELEGRAM_URL,
        data=body,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.status
    except ssl.SSLError:
        # 打包後可能缺系統 CA → 退回不驗證憑證再送一次。
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return True, resp.status
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
