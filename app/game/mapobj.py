"""場景物件的外觀編號 → 中文名字（＋圖檔名）。

    mapobj.name_of(60049)   → '惡魔系雕像01'
    mapobj.label(60049)     → '惡魔系雕像01（60049）'
    mapobj.label(69999)     → '外觀 69999'
    mapobj.hittest(60049)   → True   （資源包標了「點得到」）

表從哪來
--------
`assets/mapobj_names.tsv.gz`（13894 筆、131KB），`tools/build_mapobj.py` 從
`GAMEDATA/setting/*.obd` 抽出來的 —— 那是**文字**的物件資料庫，一筆有
`Name` / `Sequence` / `Flags` / `Directory` / `Sprite`。`Sequence` 就是實體
`+0xB4` 讀到的外觀編號（`scenery.Prop.model`）。

為什麼要有它（2026-09-02）
--------------------------
副本腳本製作那頁本來只印得出編號，使用者看不出那是什麼東西。實測吞噬之間 1
的「解謎」是繞 **8 座雕像**（人型／動物／能量／惡魔各 01、02），沒有名字
根本看不出來：

    60039 人型系雕像01   60040 人型系雕像02
    60041 動物系雕像01   60042 動物系雕像02
    60045 能量系雕像01   60046 能量系雕像02
    60049 惡魔系雕像01   60050 惡魔系雕像02
    60272 靜態-資料片門01關   60299 門開關火炬   60097 墓碑
    60005 TAG01   60034 TAG02   60038 TAG06      ← 場景標記點

⚠ 官方改版新增物件要重跑 `tools/build_mapobj.py`；查不到就**退回顯示編號**
  （安全退化），絕不猜一個名字給它。
★ 只在第一次用到時才載入，之後整支程式共用同一份。
"""
from __future__ import annotations

import gzip
import threading
import zipfile

from app.paths import resource

DATA_FILE = "assets/mapobj_names.tsv.gz"

_table: dict[int, tuple[str, bool, str]] | None = None


def _load() -> dict[int, tuple[str, bool, str]]:
    global _table
    if _table is None:
        out: dict[int, tuple[str, bool, str]] = {}
        try:
            with gzip.open(resource(DATA_FILE), "rt", encoding="utf-8") as f:
                next(f, None)                  # 標題列
                for line in f:
                    cols = line.rstrip("\n").split("\t")
                    if len(cols) < 2 or not cols[0].isdigit():
                        continue
                    flags = cols[2] if len(cols) > 2 else ""
                    spr = cols[3] if len(cols) > 3 else ""
                    out[int(cols[0])] = (cols[1], flags, spr)
        except Exception:                                  # noqa: BLE001
            out = {}          # 檔案缺了就退化成只顯示編號，不要爆掉
        _table = out
    return _table


def name_of(model: int) -> str:
    """外觀編號的中文名字；查不到回空字串（呼叫端自己決定怎麼退化）。"""
    rec = _load().get(int(model))
    return rec[0] if rec else ""


def hittest(model: int) -> bool:
    """資源包有沒有標「點得到」（`SP_ATTRIB_HITTEST`）。查不到當 False。"""
    rec = _load().get(int(model))
    return bool(rec and "HITTEST" in rec[1])


def hidden(model: int) -> bool:
    """這個外觀在遊戲裡是**看不見**的嗎（`SP_ATTRIB_HIDE`）。

    ★ 場景標記點（TAG01/TAG02/TAG06…）就是這種：伺服器拿來標位置用，畫面上
      根本沒東西。它們照樣有合法的互動 id，所以會混進 `scenery.nearby()` 的
      結果裡 —— 實測吞噬之間 1 站著掃到 45 個「可互動物件」，絕大多數是 TAG。
      介面預設把它們收起來，不然真正的機關會被淹掉。
    ⚠ 查不到旗標回 False（＝當成看得見）—— 寧可多列，不要把真的機關藏起來。
    """
    rec = _load().get(int(model))
    return bool(rec and "HIDE" in rec[1])


def sprite_of(model: int) -> str:
    """這個外觀的圖檔（`stage/24/24-012-5U0`）；查不到回空字串。"""
    rec = _load().get(int(model))
    return rec[2] if rec else ""


def label(model: int) -> str:
    """給人看的標示：有名字就「名字（編號）」，沒有就「外觀 編號」。"""
    nm = name_of(model)
    return f"{nm}（{model}）" if nm else f"外觀 {model}"


# ---------------------------------------------------------------------------
# 縮圖（`tools/build_mapobj_icons.py` 做的，只收「點得到」的物件）
# ---------------------------------------------------------------------------
# ⚠ 涵蓋率只有 969/3598 —— 專案裡那份 GAMEDATA 的 `shape/stage` 沒有那麼多檔
#   （server.obd 引用到 24-274，資源包只到 24-203；stage/28 整個沒有）。
#   查不到就**不顯示圖**，不要拿別的圖頂替。
ICON_FILE = "assets/mapobj_icons.zip"
INDEX_NAME = "index.tsv"

_lock = threading.Lock()
_zip: "zipfile.ZipFile | None" = None
_icon_index: dict[int, str] | None = None
_png_cache: dict[int, bytes | None] = {}
_pixmap_cache: dict[int, object] = {}


def _open() -> tuple["zipfile.ZipFile | None", dict[int, str]]:
    global _zip, _icon_index
    if _icon_index is None:
        idx: dict[int, str] = {}
        try:
            z = zipfile.ZipFile(resource(ICON_FILE))
            for line in z.read(INDEX_NAME).decode("utf-8").splitlines():
                key, _, name = line.partition("\t")
                if name:
                    idx[int(key)] = name
            _zip = z
        except Exception:                                  # noqa: BLE001
            _zip = None                # 圖包缺了 → 整個退化成「都沒有圖」
        _icon_index = idx
    return _zip, _icon_index


def png(model: int) -> bytes | None:
    """這個外觀的縮圖 PNG bytes；沒有就回 None。"""
    model = int(model)
    with _lock:
        if model in _png_cache:
            return _png_cache[model]
        z, idx = _open()
        data: bytes | None = None
        name = idx.get(model)
        if z is not None and name:
            try:
                data = z.read(f"{name}.png")
            except Exception:                              # noqa: BLE001
                data = None
        _png_cache[model] = data
        return data


def has_icon(model: int) -> bool:
    """有沒有這個外觀的圖（只查索引，不解圖）。"""
    return int(model) in _open()[1]


def pixmap(model: int):
    """QPixmap；沒有圖（或還沒有 QGuiApplication）回 None。

    ⚠ QPixmap 只能在 GUI 執行緒建立 —— 背景執行緒請改用 `png()`。
    """
    model = int(model)
    if model in _pixmap_cache:
        return _pixmap_cache[model]
    data = png(model)
    pm = None
    if data:
        try:
            from PySide6.QtGui import QPixmap    # 延後 import：無頭工具不必有 Qt
            pm = QPixmap()
            if not pm.loadFromData(data, "PNG"):
                pm = None
        except Exception:                                  # noqa: BLE001
            pm = None
    _pixmap_cache[model] = pm
    return pm


def icon_count() -> int:
    """圖包裡涵蓋幾個外觀編號（診斷用；0 ＝ 圖包沒載到）。"""
    return len(_open()[1])
