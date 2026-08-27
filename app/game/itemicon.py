"""道具圖示：圖示編號 → 圖（PNG bytes 或 QPixmap）。

    itemicon.pixmap(item.icon_id)      → QPixmap，查不到回 None

圖從哪來
--------
`assets/item_icons.zip`（4560 張 PNG、8.5MB），是 `tools/build_item_icons.py`
把遊戲資源包 `GAMEDATA\\shape\\item\\*.SHP` 解出來打包的。
⚠ **官方改版新增道具要重跑那支**，不然新道具沒有圖示。

★ **圖示編號本身是從記憶體讀的**（範本 `+0x00`，見 `bag.TMPL_ICON`），所以
  改版換了某個道具的圖，編號會自動跟上；這支只負責「編號 → 圖」那一段。

查不到就回 None —— 呼叫端自己決定要不要改顯示文字，**絕不拿別張圖頂替**
（有 4 個編號在資源包裡自己就前後矛盾，build 工具已經整個丟掉不收）。
"""
from __future__ import annotations

import threading
import zipfile

from app.paths import resource

DATA_FILE = "assets/item_icons.zip"
INDEX_NAME = "index.tsv"

_lock = threading.Lock()
_zip: zipfile.ZipFile | None = None
_index: dict[int, str] | None = None
_png_cache: dict[int, bytes | None] = {}
_pixmap_cache: dict[int, object] = {}


def _open() -> tuple[zipfile.ZipFile | None, dict[int, str]]:
    """開檔＋讀索引（只做一次）。檔案缺了就整個功能退化成「都沒有圖」。"""
    global _zip, _index
    if _index is None:
        idx: dict[int, str] = {}
        try:
            z = zipfile.ZipFile(resource(DATA_FILE))
            for line in z.read(INDEX_NAME).decode("utf-8").splitlines():
                key, _, name = line.partition("\t")
                if name:
                    idx[int(key)] = name
            _zip = z
        except Exception:                                  # noqa: BLE001
            _zip = None
        _index = idx
    return _zip, _index


def png(icon_id: int) -> bytes | None:
    """圖示編號的 PNG bytes；查不到回 None。"""
    icon_id = int(icon_id)
    with _lock:
        if icon_id in _png_cache:
            return _png_cache[icon_id]
        z, idx = _open()
        data: bytes | None = None
        name = idx.get(icon_id)
        if z is not None and name:
            try:
                data = z.read(f"{name}.png")
            except Exception:                              # noqa: BLE001
                data = None
        _png_cache[icon_id] = data
        return data


def has(icon_id: int) -> bool:
    """有沒有這個編號的圖（不解圖、只查索引）。"""
    _, idx = _open()
    return int(icon_id) in idx


def pixmap(icon_id: int):
    """QPixmap；查不到（或還沒有 QGuiApplication）回 None。

    ⚠ QPixmap 只能在 GUI 執行緒建立 —— 這支給分頁用，背景執行緒請改用 `png()`。
    """
    icon_id = int(icon_id)
    if icon_id in _pixmap_cache:
        return _pixmap_cache[icon_id]
    data = png(icon_id)
    pm = None
    if data:
        try:
            from PySide6.QtGui import QPixmap        # 延後 import：無頭工具不必有 Qt
            pm = QPixmap()
            if not pm.loadFromData(data, "PNG"):
                pm = None
        except Exception:                                  # noqa: BLE001
            pm = None
    _pixmap_cache[icon_id] = pm
    return pm


def count() -> int:
    """索引裡有幾個編號（診斷用；0 ＝ 圖包沒載到）。"""
    return len(_open()[1])
