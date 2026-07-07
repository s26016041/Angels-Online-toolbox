"""設定管理模組。

設定以 JSON 儲存在使用者的 AppData 目錄下（Windows），
避免與程式碼混在一起，打包成 exe 後也能正常讀寫。

使用方式：
    from app.config import config
    config.get("login.account", "")
    config.set("login.account", "myname")
    config.save()
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any


APP_DIR_NAME = "AngelsOnlineToolbox"
CONFIG_FILENAME = "config.json"


def _config_dir() -> Path:
    """取得跨平台的設定目錄。"""
    base = os.environ.get("APPDATA")  # Windows
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")  # 其他系統後備
    path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class Config:
    """簡單的巢狀設定管理，支援 "a.b.c" 形式的鍵。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (_config_dir() / CONFIG_FILENAME)
        self._data: dict[str, Any] = {}
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # 設定檔損毀時不讓程式崩潰，改用空設定。
                self._data = {}
        else:
            self._data = {}

    def save(self) -> None:
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError(f"設定鍵衝突：{key}")
        node[parts[-1]] = value

    # --- 密碼的輕度混淆 ---------------------------------------------------
    # 注意：這只是「避免明碼直視」的混淆，不是真正的加密。
    # 任何拿到設定檔的人都能還原。若要安全儲存，建議改用 keyring 函式庫。
    @staticmethod
    def obfuscate(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    @staticmethod
    def deobfuscate(text: str) -> str:
        try:
            return base64.b64decode(text.encode("ascii")).decode("utf-8")
        except Exception:
            return ""


# 全域單一實例，供各分頁共用。
config = Config()
