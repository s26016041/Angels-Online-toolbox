"""道具圖示（.SHP 解碼＋換色）離線回歸：不必開遊戲。

    py tools\\icon_check.py

驗的東西：
  · 圖包索引讀得到、帶換色參數；每一張 .SHP 都解得開、尺寸合理。
  · 換色算法（iconbias）：色相窗、旗標、clamp 都照 angel.dat 那段反組譯的規則走；
    同一個形狀不同顏色的寶石（橙 5445／黃 5450／綠 5455 共用 i0254）要畫出**不同**的圖，
    而且橙的紅比綠多、綠的綠比橙多（顏色方向對）。
  · bias == 0 的圖示一個像素都不能動。
⚠ 演算法**逐位元**的驗證要開遊戲：`py tools\\icon_bias_probe.py`（讀遊戲自己建的兩張表比對）。
"""
from __future__ import annotations

import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")              # type: ignore[attr-defined]
except Exception:                                          # noqa: BLE001
    pass

import numpy as np                                         # noqa: E402

from app.game import iconbias, itemicon                    # noqa: E402
from app.paths import resource                             # noqa: E402

PASS = FAIL = 0


def ck(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}　{note}")


def main() -> int:
    print("圖包")
    n = itemicon.count()
    ck("索引讀得到（> 1 萬個編號）", n > 10000, str(n))
    z = zipfile.ZipFile(resource(itemicon.DATA_FILE))
    names = [x for x in z.namelist() if x.endswith(".shp")]
    ck("包裡是原始 .SHP（> 4000 張）", len(names) > 4000, str(len(names)))
    bad = []
    sizes = []
    for name in names:
        try:
            shp = itemicon.decode_shp(z.read(name))
            sizes.append((shp.w, shp.h))
            if not (0 < shp.w <= 512 and 0 < shp.h <= 512):
                bad.append(name)
        except Exception as exc:                           # noqa: BLE001
            bad.append(f"{name}:{exc}")
    ck("每一張都解得開", not bad, str(bad[:5]))
    e = itemicon.entry(5450)
    ck("5450（完美的黃寶石）索引帶換色參數 i0254 / 0x50a08 / 0x4aed / 132",
       e == ("i0254", 0x50A08, 0x4AED, 132), str(e))

    print("\n換色算法（iconbias）")
    A, B = iconbias.tables()
    ck("兩張表各 65536 項 uint16", A.shape == (65536,) and B.shape == (65536,)
       and A.dtype == np.uint16 and B.dtype == np.uint16)
    hsv = A[np.array([0xF800, 0x07E0, 0x001F, 0xFFFF, 0x0000], dtype=np.uint16)].astype(int)
    h = hsv & 0x3F
    s = (hsv >> 6) & 0x1F
    v = hsv >> 11
    ck("純紅／純綠／純藍的色相 0／21／42（64 格色相環三等分）", list(h[:3]) == [0, 21, 42], str(list(h)))
    ck("白＝飽和 0 亮 31、黑＝全 0", s[3] == 0 and v[3] == 31 and (hsv[4] == 0), str(list(hsv)))
    ck("紅綠藍飽和／亮度全滿", all(s[:3] == 31) and all(v[:3] == 31))
    # B 是 A 的反向（量化後：飽和 31 ＝ 248/255，所以不是完全純色，但主通道要全滿、其他很小）
    back = [int(x) for x in B[hsv[:3]]]
    r5 = [(x >> 11) & 0x1F for x in back]
    g6 = [(x >> 5) & 0x3F for x in back]
    b5 = [x & 0x1F for x in back]
    # 飽和最高只到 248/255 → 其他通道會留 ~7/255（5 位 ≤ 2、6 位 ≤ 4），主通道要幾乎全滿
    ck("HSV→RGB 表把純色轉回（幾乎）純色：主通道 ≥ 30/31（62/63）、其他通道 ≤ 7/255",
       r5[0] >= 30 and g6[0] <= 4 and b5[0] <= 2
       and g6[1] >= 62 and r5[1] <= 2 and b5[1] <= 2
       and b5[2] >= 30 and r5[2] <= 2 and g6[2] <= 4,
       [hex(x) for x in back])
    lo, hi, flag = iconbias.window(0x4AED, 132)
    ck("色相窗：basecolor 0x4aed（色相 35）± 4、range 132 的最高位＝絕對色相旗標",
       (lo, hi, flag) == (31, 39, 1), str((lo, hi, flag)))
    lo2, hi2, _f = iconbias.window(0xF800, 130)
    ck("色相 0 減 2 會繞到 62～66（lo<0 就整組 +64）", (lo2, hi2) == (62, 66), str((lo2, hi2)))
    same = iconbias.remap(np.array([0x1234, 0xF800], dtype=np.uint16), 0, 0, 0)
    ck("bias 0 → 原樣", list(same) == [0x1234, 0xF800])
    outside = iconbias.remap(np.array([0x001F], dtype=np.uint16), 0x50A08, 0x4AED, 132)
    ck("色相不在窗內（純藍 42 vs 31～39）→ 不動", int(outside[0]) == 0x001F, hex(int(outside[0])))
    # 旗標＝絕對色相：窗內的顏色統統換成色相 bias&0xff（8＝橙黃）、飽和 +10、亮度 +5
    base = int(A[0x4AED])
    bh, bs, bv = base & 0x3F, (base >> 6) & 0x1F, base >> 11
    ck("basecolor 0x4aed 的 HSV ＝ (35, 9, 13)", (bh, bs, bv) == (35, 9, 13), str((bh, bs, bv)))
    inside = iconbias.remap(np.array([0x4AED], dtype=np.uint16), 0x50A08, 0x4AED, 132)
    want = int(B[((min(bv + 5, 31) << 5) | min(bs + 10, 31)) << 6 | 8])
    ck("窗內的顏色 → B[色相 8、飽和 9+10、亮度 13+5]", int(inside[0]) == want,
       f"{int(inside[0]):#x} vs {want:#x}")
    # 相對色相（旗標 0）：色相 +bias；飽和／亮度不動
    rel = iconbias.remap(np.array([0xF800], dtype=np.uint16), 0x0A, 0xF800, 0)
    rh = int(A[0xF800])
    want = int(B[((rh >> 11) << 5 | ((rh >> 6) & 0x1F)) << 6 | ((rh & 0x3F) + 10) & 0x3F])
    ck("旗標 0 → 色相相對加（紅 0 + 10 → 10）", int(rel[0]) == want, f"{int(rel[0]):#x} vs {want:#x}")
    # 負的飽和／亮度偏移：bias 0xfe0805 ＝ 色相 5、飽和 +8、亮度 −2（int8）
    neg = iconbias.remap(np.array([0x4AED], dtype=np.uint16), 0xFE0805, 0x4AED, 132)
    want = int(B[((max(bv - 2, 0) << 5) | min(bs + 8, 31)) << 6 | 5])
    ck("bias 高位元組是 int8（亮度 −2）", int(neg[0]) == want, f"{int(neg[0]):#x} vs {want:#x}")

    print("\n寶石：同一個形狀不同顏色要畫出不同的圖")
    imgs = {i: itemicon.rgba(i) for i in (5445, 5450, 5455)}
    ck("三張都畫得出來", all(x is not None for x in imgs.values()))
    if all(x is not None for x in imgs.values()):
        ck("橙 5445 ≠ 黃 5450", bool((imgs[5445] != imgs[5450]).any()))
        ck("黃 5450 ≠ 綠 5455", bool((imgs[5450] != imgs[5455]).any()))
        ck("alpha 一樣（只換顏色不換形狀）",
           bool((imgs[5445][..., 3] == imgs[5455][..., 3]).all()))

        def mean(a, ch):
            m = a[..., 3] > 0
            return float(a[..., ch][m].mean())
        ck("綠寶石的綠 > 紅；橙寶石的紅 > 綠（顏色方向對）",
           mean(imgs[5455], 1) > mean(imgs[5455], 0) and mean(imgs[5445], 0) > mean(imgs[5445], 1),
           f"綠 r={mean(imgs[5455], 0):.0f} g={mean(imgs[5455], 1):.0f}；"
           f"橙 r={mean(imgs[5445], 0):.0f} g={mean(imgs[5445], 1):.0f}")
    plain = [i for i, e in itemicon._open()[1].items() if e[1] == 0][:3]
    ok = True
    for i in plain:
        e = itemicon.entry(i)
        shp = itemicon._shp(e[0])
        if shp is None:
            continue
        a = itemicon.rgba(i)
        b = itemicon.compose(shp)
        ok = ok and a is not None and bool((a == b).all())
    ck("bias 0 的圖示＝原圖一個像素都沒動", ok)
    pm = itemicon.png(5450)
    ck("png() 出得來（PNG 檔頭）", pm is not None and pm[:8] == b"\x89PNG\r\n\x1a\n")

    print(f"\n通過 {PASS}　失敗 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
