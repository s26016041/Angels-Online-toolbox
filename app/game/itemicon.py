"""道具圖示：圖示編號 → 圖（QPixmap／PNG bytes）。

    itemicon.pixmap(item.icon_id)      → QPixmap，查不到回 None
    itemicon.has(icon_id) / count()    → 圖包裡有沒有這個編號／共幾個編號

圖從哪來
--------
`assets/item_icons.zip`（`tools/build_item_icons.py` 從遊戲資源包打包）：
    index.tsv     編號 <TAB> 圖檔名 <TAB> basebias <TAB> basecolor <TAB> baserange
    i####.shp     遊戲自己的 TLHS 圖檔**原封不動**（4575 個、8.9MB，zip 壓完約一半）
⚠ **官方改版新增道具要重跑那支**，不然新道具沒有圖示。
★ **圖示編號本身是從記憶體讀的**（範本 `+0x00`，見 `bag.TMPL_ICON`），所以改版換了某個
  道具的圖，編號會自動跟上；這支只負責「編號 → 圖」那一段。

★★ 為什麼 2026-09-06 改成執行時解 .SHP、不再存 PNG：**33316 個圖示有 21564 個是同一張
  .SHP 換個顏色**（`basebias`／`basecolor`／`baserange`，寶石五種顏色共用五個形狀）。
  存 PNG 的話每個顏色版本都要一張（1.6 萬張、幾十 MB）；存原始 .SHP＋在這裡照遊戲的
  算法（`iconbias.py`，反組譯 angel.dat 抄的）換調色盤，圖包反而更小、顏色跟遊戲一樣。
  使用者原話：「同等寶石你好像都用同一個圖片」—— 以前全部畫成原色，就是這個坑。

.SHP 格式（`TLHS`，2026-08-28 自己解的，4568/4568 通過「段長度總和」自我驗證）
    +0x08 型別 2／4　+0x10 透明色　+0x14 寬　+0x18 高　+0x28 像素數
    +0x40 調色盤起點（256 × RGB565；0 ＝ 沒有）　+0x50 每列一個 dword → 段清單
    段 8 bytes ＝ [(起始x<<16)|長度, 像素資料位移]，一列可以好幾段，0xFFFF0000 結束
    型別2＋調色盤 → 1 byte 索引；型別2 沒調色盤 → 2 bytes RGB565；型別4＋調色盤 → (索引, alpha)

查不到就回 None —— 呼叫端自己決定要不要改顯示文字，**絕不拿別張圖頂替**。
"""
from __future__ import annotations

import struct
import threading
import zipfile

import numpy as np

from app.game import iconbias
from app.paths import resource

DATA_FILE = "assets/item_icons.zip"
INDEX_NAME = "index.tsv"

MAGIC = b"TLHS"
ROW_END = 0xFFFF0000
KIND_INDEX = 2
KIND_INDEX_ALPHA = 4

_lock = threading.Lock()
_zip: zipfile.ZipFile | None = None
_index: dict[int, tuple[str, int, int, int]] | None = None   # 編號 → (圖檔名, bias, color, range)
_shp_cache: dict[str, object] = {}                             # 圖檔名 → 解好的圖（或 None）
_rgba_cache: dict[int, np.ndarray | None] = {}
_pixmap_cache: dict[int, object] = {}


class Shp:
    """解好的一張圖：`pix` 是索引（uint8）或直接色（uint16），`alpha` 0/255（型別 4 是真 alpha）。"""

    __slots__ = ("w", "h", "pal", "pix", "alpha")

    def __init__(self, w: int, h: int, pal, pix, alpha) -> None:
        self.w, self.h, self.pal, self.pix, self.alpha = w, h, pal, pix, alpha


def decode_shp(data: bytes) -> Shp:
    """TLHS → Shp；格式不認得就丟 ValueError（呼叫端跳過該檔）。"""
    if data[:4] != MAGIC:
        raise ValueError("不是 TLHS 圖檔")
    kind = struct.unpack_from("<I", data, 0x08)[0]
    _key, w, h = struct.unpack_from("<3I", data, 0x10)
    if not (0 < w <= 512 and 0 < h <= 512):
        raise ValueError(f"寬高不合理 {w}x{h}")
    pal_off = struct.unpack_from("<I", data, 0x40)[0]
    pal = (np.frombuffer(data, dtype="<u2", count=256, offset=pal_off).astype(np.uint16)
           if pal_off else None)
    if kind == KIND_INDEX and pal is not None:
        mode, step = "index", 1
    elif kind == KIND_INDEX and pal is None:
        mode, step = "rgb565", 2
    elif kind == KIND_INDEX_ALPHA and pal is not None:
        mode, step = "index_alpha", 2
    else:
        raise ValueError(f"型別 {kind}＋{'有' if pal is not None else '沒'}調色盤 還沒解")
    rows = struct.unpack_from(f"<{h}I", data, 0x50)
    pix = np.zeros((h, w), dtype=np.uint16 if mode == "rgb565" else np.uint8)
    alpha = np.zeros((h, w), dtype=np.uint8)
    n_data = len(data)
    for y, roff in enumerate(rows):
        off = roff
        while True:
            head = struct.unpack_from("<I", data, off)[0]
            if head == ROW_END:
                break
            src = struct.unpack_from("<I", data, off + 4)[0]
            off += 8
            x0, n = head >> 16, head & 0xFFFF
            n = min(n, max(w - x0, 0), max((n_data - src) // step, 0))
            if n <= 0:
                continue
            if mode == "index":
                pix[y, x0:x0 + n] = np.frombuffer(data, dtype=np.uint8, count=n, offset=src)
                alpha[y, x0:x0 + n] = 255
            elif mode == "index_alpha":
                pair = np.frombuffer(data, dtype=np.uint8, count=n * 2, offset=src)
                pix[y, x0:x0 + n] = pair[0::2]
                alpha[y, x0:x0 + n] = pair[1::2]
            else:
                pix[y, x0:x0 + n] = np.frombuffer(data, dtype="<u2", count=n, offset=src)
                alpha[y, x0:x0 + n] = 255
    return Shp(w, h, pal, pix, alpha)


def compose(shp: Shp, bias: int = 0, color: int = 0, rng: int = 0) -> np.ndarray:
    """Shp（＋換色參數）→ RGBA uint8 [h, w, 4]。換色照遊戲：有調色盤換調色盤、直接色逐點換。"""
    if shp.pal is not None:
        pal = iconbias.remap(shp.pal, bias, color, rng) if bias else shp.pal
        c = pal[shp.pix]
    else:
        c = iconbias.remap(shp.pix.ravel(), bias, color, rng).reshape(shp.h, shp.w) if bias else shp.pix
    r, g, b = iconbias.rgb565_to_rgb8(c)
    return np.dstack([r, g, b, shp.alpha]).astype(np.uint8)


# --- 圖包 ---------------------------------------------------------------------
def _open() -> tuple[zipfile.ZipFile | None, dict[int, tuple[str, int, int, int]]]:
    """開檔＋讀索引（只做一次）。檔案缺了就整個功能退化成「都沒有圖」。"""
    global _zip, _index
    if _index is None:
        idx: dict[int, tuple[str, int, int, int]] = {}
        try:
            z = zipfile.ZipFile(resource(DATA_FILE))
            for line in z.read(INDEX_NAME).decode("utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) < 2 or not parts[1]:
                    continue
                try:
                    bias, color, rng = (int(parts[2]), int(parts[3]), int(parts[4])) \
                        if len(parts) >= 5 else (0, 0, 0)
                    idx[int(parts[0])] = (parts[1].lower(), bias, color, rng)
                except ValueError:
                    continue
            _zip = z
        except Exception:                                  # noqa: BLE001
            _zip = None
        _index = idx
    return _zip, _index


def _shp(name: str) -> Shp | None:
    if name in _shp_cache:
        return _shp_cache[name]                            # type: ignore[return-value]
    z, _idx = _open()
    shp = None
    if z is not None:
        try:
            shp = decode_shp(z.read(f"{name}.shp"))
        except Exception:                                  # noqa: BLE001
            shp = None
    _shp_cache[name] = shp
    return shp


def rgba(icon_id: int) -> np.ndarray | None:
    """圖示編號 → RGBA uint8 [h, w, 4]（已照遊戲換色）；查不到回 None。純 numpy，任何執行緒都能叫。"""
    icon_id = int(icon_id)
    with _lock:
        if icon_id in _rgba_cache:
            return _rgba_cache[icon_id]
        _z, idx = _open()
        entry = idx.get(icon_id)
        out = None
        if entry is not None:
            shp = _shp(entry[0])
            if shp is not None:
                try:
                    out = compose(shp, entry[1], entry[2], entry[3])
                except Exception:                          # noqa: BLE001
                    out = None
        _rgba_cache[icon_id] = out
        return out


def has(icon_id: int) -> bool:
    """有沒有這個編號的圖（不解圖、只查索引）。"""
    _, idx = _open()
    return int(icon_id) in idx


def entry(icon_id: int) -> tuple[str, int, int, int] | None:
    """索引裡這個編號的 (圖檔名, basebias, basecolor, baserange)；沒有回 None。"""
    _, idx = _open()
    return idx.get(int(icon_id))


def _qimage(arr: np.ndarray):
    from PySide6.QtGui import QImage             # 延後 import：無頭工具不必有 Qt
    h, w = arr.shape[:2]
    buf = np.ascontiguousarray(arr).tobytes()
    img = QImage(buf, w, h, w * 4, QImage.Format_RGBA8888)
    return img.copy()                            # 跟 buf 脫鉤


def png(icon_id: int) -> bytes | None:
    """圖示編號的 PNG bytes；查不到回 None。"""
    arr = rgba(icon_id)
    if arr is None:
        return None
    try:
        from PySide6.QtCore import QBuffer, QByteArray
        ba = QByteArray()
        qb = QBuffer(ba)
        qb.open(QBuffer.WriteOnly)
        _qimage(arr).save(qb, "PNG")
        return bytes(ba.data())
    except Exception:                                      # noqa: BLE001
        return None


def pixmap(icon_id: int):
    """QPixmap；查不到（或還沒有 QGuiApplication）回 None。

    ⚠ QPixmap 只能在 GUI 執行緒建立 —— 這支給分頁用，背景執行緒請改用 `rgba()`。
    """
    icon_id = int(icon_id)
    if icon_id in _pixmap_cache:
        return _pixmap_cache[icon_id]
    arr = rgba(icon_id)
    pm = None
    if arr is not None:
        try:
            from PySide6.QtGui import QPixmap
            pm = QPixmap.fromImage(_qimage(arr))
            if pm.isNull():
                pm = None
        except Exception:                                  # noqa: BLE001
            pm = None
    _pixmap_cache[icon_id] = pm
    return pm


def count() -> int:
    """索引裡有幾個編號（診斷用；0 ＝ 圖包沒載到）。"""
    return len(_open()[1])
