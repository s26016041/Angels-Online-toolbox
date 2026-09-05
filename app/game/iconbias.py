"""遊戲圖示的「換色」（basebias／basecolor／baserange）—— 照抄 angel.dat 的算法。

    A, B = iconbias.tables()                     # RGB565→HSV、HSV→RGB565 兩張查表（各 64K 項）
    out = iconbias.remap(colors, bias, color, rng)   # 一批 RGB565 顏色 → 換色後的 RGB565

## 為什麼要有這支（2026-09-06 使用者：「同等寶石你好像都用同一個圖片」）

`itemicon*.xml` 一筆 `<icon id=… normal="i0254" basebias="0x50a08" basecolor="0x4aed"
baserange="132">`：**33316 個道具圖示裡有 21564 個**是同一張 .SHP 換個顏色印出來的
（寶石五種顏色共用 i0250~i0254 五個形狀、裝備顏色版本…）。以前的圖包只照 `normal`
取圖，所有顏色版本都畫成原色 —— 黃寶石、綠寶石、橙寶石看起來一模一樣。

## 算法出處（全部反組譯自 angel.dat，⛔ 沒有一個數字是猜的）

* **parser** `0x649660`（itemicon 的 `basebias`→dword、`basecolor`→u16、`baserange`→u8；
  .obd 的 `BaseBias = 0x20505, 0xfe53, 130` 同一個版面 `0x679ac0`：物件 +0x40/+0x44/+0x46）。
* **SetBias** `0x6819f0`（thunk `0x671bd0`，畫圖前呼叫 `SetBias(0, bias, color, range)`）：
      slot = which<<4
      [0xa02e20+slot] = bias；[0xa02e24+slot] = range >> 7        ← 旗標：range 最高位
      if bias: base = A[color] & 0x3f；tol = range & 0x3f
               lo = base − tol；hi = base + tol；lo < 0 → lo += 64、hi += 64（色相環 64 格）
* **兩張表** `0x67ce70(A, B, is565)` 開機建一次（`0x67d2fc`／`0x67d378`，看顯示模式）：
      B[(v<<5|s)<<6|h] = rgb565(hsv2rgb(h*4, s*8, v*8))      h 0..63、s 0..31、v 0..31
      A[c] = (v>>3)<<11 | (s>>3)<<6 | (h>>2)   其中 (h,s,v)=rgb2hsv(r,g,b of c)，0..255
  `rgb2hsv` `0x682cf0`、`hsv2rgb` `0x682b40` 都是**單精度**浮點（SSE divss/mulss），
  最後 `cvtps2pd → addsd 0.5 → cvttsd2si` 四捨五入；下面用 numpy float32 逐步照抄，
  運算順序不能換（換了最後一位會不一樣）。
* **換色本體** `0x682230`（調色盤逐格）＝ `0x682462`（直接色逐點），兩處同一段算式：
      hsv = A[c]；h = hsv & 0x3f；不在 [lo,hi]（或 h+64 在）→ 原色不動
      h' = 旗標 ? bias & 0xff : (h + bias) & 0x3f
      s' = clamp(s + int8(bias>>8), 0, 31)；v' = clamp(v + int8(bias>>16), 0, 31)
      out = B[(v'<<5|s')<<6|h']
  bias == 0 的圖示遊戲根本不進這段（`0x67c438`），照原色。
* 顯示模式：表照 **RGB565** 建（`0x67d378` 那條，`.SHP` 調色盤本身就是 565，圖包一直
  這樣解、顏色對得上）。⚠ 555 模式（`[0xa014f8]==1`）的表沒做 —— 現代顯示都是 565。
* ✅ 驗證方法：`tools/icon_bias_probe.py` 在遊戲開著時把 `[0xa02e08]`／`[0xa02e0c]`
  兩張表整包讀出來跟這裡算的逐位元比對（兩張表一樣＝換色結果必然一樣）。

⚠ 這支只做純數學，不碰 Qt、不讀遊戲；圖檔解碼在 itemicon.py。
"""
from __future__ import annotations

import threading

import numpy as np

_F = np.float32
_lock = threading.Lock()
_tables: tuple[np.ndarray, np.ndarray] | None = None

# 遊戲自己那兩張表的**指標**所在（全域變數，開機由 `0x67d2aa`／`0x67d2c4` 填）。
# 產品不讀它們（圖示是資源檔的事）；只給 `tools/icon_bias_probe.py` 逐位元對表用。
# 由 locate.warm() 用 AOB 寫回（錨在換色本體 `0x682234` 那幾行）。
TABLE_A_PTR = 0x00A02E08     # [這裡] → RGB565→HSV 表（64K × u16）
TABLE_B_PTR = 0x00A02E0C     # [這裡] → HSV→RGB565 表


def _round_half_up(x32: np.ndarray) -> np.ndarray:
    """`cvtps2pd → addsd 0.5 → cvttsd2si`：轉雙精度、加 0.5、往零截斷（值都 ≥ 0）。
    NaN（0/0 那些沒用到的格）先換成 0，免得轉整數時噴警告。"""
    x = np.nan_to_num(x32.astype(np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    return np.trunc(x + 0.5).astype(np.int32)


def rgb2hsv(r: np.ndarray, g: np.ndarray, b: np.ndarray):
    """`0x682cf0`：(r,g,b) 0..255 → (h,s,v) 0..255，單精度逐步照抄。"""
    R = r.astype(_F) / _F(255)
    G = g.astype(_F) / _F(255)
    B = b.astype(_F) / _F(255)
    mx = np.maximum(np.maximum(R, G), B)          # maxss(maxss(R,G),B)
    mn = np.minimum(np.minimum(R, G), B)
    v = _round_half_up(mx * _F(255))
    delta = (mx - mn).astype(_F)
    with np.errstate(divide="ignore", invalid="ignore"):
        s_raw = (delta * _F(255)) / mx
        hr = (G - B) / delta                                  # R 是最大
        hg = ((B - R) / delta).astype(_F) + _F(2)             # G 是最大
        hb = ((R - G) / delta).astype(_F) + _F(4)             # 其餘（B 最大）
    s = _round_half_up(np.where(mx > 0, s_raw, _F(0)))
    h6 = np.where(R == mx, hr, np.where(G == mx, hg, hb)).astype(_F)
    h6 = (h6 / _F(6)).astype(_F)
    h6 = np.where(h6 < 0, (h6 + _F(1)).astype(_F), h6)
    h = _round_half_up((h6 * _F(255)).astype(_F))
    zero = (mx == 0) | (s == 0)
    h = np.where(zero, 0, h)
    s = np.where(mx == 0, 0, s)
    return h.astype(np.int32), s.astype(np.int32), v.astype(np.int32)


def hsv2rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray):
    """`0x682b40`：(h,s,v) 0..255 → (r,g,b) 0..255。⚠ h==1 被當成 0（遊戲就是這樣寫）。"""
    hh = np.where(h == 1, 0, h)
    V = v.astype(_F) / _F(255)
    H = (hh.astype(_F) / _F(255)).astype(_F) * _F(6)
    i = np.trunc(H).astype(np.int32)                          # cvttss2si
    S = s.astype(_F) / _F(255)
    vv = _round_half_up((V * _F(255)).astype(_F))
    f = (H - i.astype(_F)).astype(_F)
    p = _round_half_up((((_F(1) - S).astype(_F) * V).astype(_F) * _F(255)).astype(_F))
    q = _round_half_up((((_F(1) - (f * S).astype(_F)).astype(_F) * V).astype(_F) * _F(255)).astype(_F))
    t1 = ((_F(1) - f).astype(_F) * S).astype(_F)
    t = _round_half_up((((_F(1) - t1).astype(_F) * V).astype(_F) * _F(255)).astype(_F))
    i = np.clip(i, 0, 5)
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [vv, q, p, p, t], vv)
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [t, vv, vv, q, p], p)
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [p, p, t, vv, vv], q)
    grey = s == 0
    r = np.where(grey, vv, r)
    g = np.where(grey, vv, g)
    b = np.where(grey, vv, b)
    black = v == 0
    return (np.where(black, 0, r).astype(np.int32), np.where(black, 0, g).astype(np.int32),
            np.where(black, 0, b).astype(np.int32))


def _build() -> tuple[np.ndarray, np.ndarray]:
    # B：HSV(6/5/5 位) → RGB565（`0x67ce90` 那三層迴圈，is565 那條）
    h = np.arange(64, dtype=np.int32)
    s = np.arange(32, dtype=np.int32)
    v = np.arange(32, dtype=np.int32)
    hh, ss, vv = np.meshgrid(h, s, v, indexing="ij")
    r, g, b = hsv2rgb(hh.ravel() * 4, ss.ravel() * 8, vv.ravel() * 8)
    rgb = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | ((b >> 3) & 0x1F)
    idx = ((vv.ravel() << 5) | ss.ravel()) << 6 | hh.ravel()
    B = np.zeros(65536, dtype=np.uint16)
    B[idx] = rgb
    # A：RGB565 → HSV(6/5/5 位)（`0x67cf42` 那圈，is565 那條）
    c = np.arange(65536, dtype=np.int32)
    r = (c >> 8) & 0xF8
    g = (c >> 3) & 0xFC
    b = (c & 0x1F) << 3
    h8, s8, v8 = rgb2hsv(r, g, b)
    A = (((v8 >> 3) << 11) | ((s8 >> 3) << 6) | (h8 >> 2)).astype(np.uint16)
    return A, B


def tables() -> tuple[np.ndarray, np.ndarray]:
    """(A: RGB565→HSV, B: HSV→RGB565)，第一次要用時才建（約 0.1 秒），之後共用。"""
    global _tables
    with _lock:
        if _tables is None:
            _tables = _build()
        return _tables


def window(color: int, rng: int) -> tuple[int, int, int]:
    """SetBias 的色相窗：(lo, hi, 旗標)。"""
    A, _B = tables()
    base = int(A[int(color) & 0xFFFF]) & 0x3F
    tol = int(rng) & 0x3F
    lo, hi = base - tol, base + tol
    if lo < 0:
        lo += 0x40
        hi += 0x40
    return lo, hi, (int(rng) >> 7) & 1


def remap(colors: np.ndarray, bias: int, color: int, rng: int) -> np.ndarray:
    """一批 RGB565（uint16 陣列）換色；bias == 0 原樣回。"""
    colors = np.asarray(colors, dtype=np.uint16)
    bias = int(bias) & 0xFFFFFFFF
    if bias == 0:
        return colors
    A, B = tables()
    lo, hi, flag = window(color, rng)
    hsv = A[colors].astype(np.int32)
    h = hsv & 0x3F
    inside = ((h >= lo) & (h <= hi)) | ((h + 0x40 >= lo) & (h + 0x40 <= hi))
    s = (hsv >> 6) & 0x1F
    v = hsv >> 11
    if flag:
        h2 = np.full_like(h, bias & 0xFF)
    else:
        h2 = (h + bias) & 0x3F
    ds = ((bias >> 8) & 0xFF) - (0x100 if (bias >> 8) & 0x80 else 0)
    dv = ((bias >> 16) & 0xFF) - (0x100 if (bias >> 16) & 0x80 else 0)
    s2 = np.clip(s + ds, 0, 31)
    v2 = np.clip(v + dv, 0, 31)
    out = B[(((v2 << 5) | s2) << 6) | h2]
    return np.where(inside, out, colors).astype(np.uint16)


def rgb565_to_rgb8(c: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RGB565 → 8 位三通道（跟舊圖包同一個展開法：高位補低位）。"""
    c = np.asarray(c, dtype=np.uint32)
    r = (c >> 11) & 0x1F
    g = (c >> 5) & 0x3F
    b = c & 0x1F
    return ((r << 3) | (r >> 2)).astype(np.uint8), ((g << 2) | (g >> 4)).astype(np.uint8), \
        ((b << 3) | (b >> 2)).astype(np.uint8)
