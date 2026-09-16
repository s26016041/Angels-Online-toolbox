"""**離線**地形圖：不必站在那張地圖上，也能算路。

    mapfile.grid_of(7)            → terrain.Grid（向日葵平原的可走格）
    mapfile.grid_of(721006)       → 分流會自動折成本流（map_key）

跟 `terrain.py` 的差別
----------------------
`terrain.load(scanner)` 讀的是**記憶體裡當下那張圖**——人沒站上去就沒有。
這支讀的是資源包抽出來的表 `assets/map_grids.bin.gz`，**任何一張都拿得到**。

為什麼需要它（2026-09-16 使用者提的需求）
    「趴趴GO 回巡邏點時，一張圖有好幾個傳送點，要挑**走過去最短**的那個。」
    要比「從傳點 A 走到目標」和「從傳點 B 走到目標」，就得有**目標那張圖**的
    地形，而人還在別張圖上、記憶體只有當下這張。

⛔ **這不是拿來取代 `terrain.py` 的**。記憶體那張是「當下這張圖」的事實
  （節慶換檔、動態空間都吃得到），而且零維護；這張表是改版要重跑的靜態表。
  規矩：**人在哪張圖就用記憶體那張，要算別張圖才用這張**，這張讀不到就回
  `None`，呼叫端安全退化（見 `jumpmap.nearest`）。

表怎麼來
--------
`tools/build_map_grids.py` 從 `GAMEDATA/map/*.mpc` ＋ `stage.xml` 自動抽，
版面與對帳證據寫在那支的檔頭。⚠ 官方改版要重跑（登記在 memory
`items-table-maintenance`）。

✅ 對帳：2026-09-16 五台實機，千夜魔宮(126) 380×250、無限塔(110) 320×300 跟
   `terrain.load()` 逐格比對 191,000 格、不同 0。
"""
from __future__ import annotations

import gzip
import struct

from app.paths import resource

DATA_FILE = "assets/map_grids.bin.gz"
MAGIC = b"AOMG1"
CACHE_SIZE = 4                   # 同時攤開的地圖張數（一張 380×250 約 95 KB）

# 索引（第一次用到才載入）：檔案索引 → (寬, 高, bitmap 在 _raw 裡的位置)
_files: list[tuple[int, int, int]] | None = None
_scenes: dict[int, int] = {}     # 場景編號 → 檔案索引
_raw: bytes = b""
_grids: dict[int, object] = {}   # 檔案索引 → Grid（LRU，最多 CACHE_SIZE 張）
_order: list[int] = []
_fail = ""                       # 載入失敗的原因（給診斷看）


def reload() -> None:
    """丟掉已載入的表，下次用到再讀（build 工具重跑完、測試用）。"""
    global _files, _scenes, _raw, _grids, _order, _fail
    _files, _scenes, _raw, _grids, _order, _fail = None, {}, b"", {}, [], ""


def _load() -> bool:
    """解析索引；**不**攤開任何一張地圖。失敗回 False（安全退化）。"""
    global _files, _scenes, _raw, _fail
    if _files is not None:
        return bool(_files)
    _files, _scenes, _fail = [], {}, ""
    try:
        _raw = gzip.decompress(resource(DATA_FILE).read_bytes())
    except Exception as e:                       # 檔案缺了／壞了都當沒有
        _fail = f"讀不到 {DATA_FILE}：{e}"
        return False
    try:
        if _raw[:len(MAGIC)] != MAGIC:
            _fail = "檔頭不對"
            _files = []
            return False
        p = len(MAGIC)
        n, = struct.unpack_from("<H", _raw, p)
        p += 2
        for _ in range(n):
            ln = _raw[p]
            p += 1 + ln
            w, h = struct.unpack_from("<HH", _raw, p)
            p += 4
            _files.append((w, h, p))
            p += ((w + 7) // 8) * h
        m, = struct.unpack_from("<H", _raw, p)
        p += 2
        for _ in range(m):
            sid, idx = struct.unpack_from("<IH", _raw, p)
            p += 6
            if idx < len(_files):
                _scenes[sid] = idx
    except Exception as e:
        _fail = f"表壞了：{e}"
        _files = []
        _scenes = {}
        return False
    return bool(_files)


def _expand(idx: int):
    """把第 idx 張攤成 terrain.Grid（bitmap → 每格一個 byte）。"""
    from app.game import terrain                  # 避免載入時循環相依

    w, h, off = _files[idx]
    stride = (w + 7) // 8
    rows: list[bytearray] = []
    for y in range(h):
        line = _raw[off + y * stride: off + (y + 1) * stride]
        rows.append(bytearray((line[x >> 3] >> (x & 7)) & 1 for x in range(w)))
    return terrain.Grid(w, h, 0, rows)            # obj=0：這張不是記憶體來的


def grid_of(scene_id: int | None):
    """那張地圖的可走格；表裡沒有／讀不到一律回 `None`（呼叫端安全退化）。

    ⚠ 分流編號（高 16 位是分流序號）會先折成本流 —— 分流地形一樣、座標互通，
      這點跟 `jumpmap.by_scene()` 的處理一致（見 `scene.map_key`）。
    """
    if scene_id is None or not _load():
        return None
    from app.game import scene                    # 避免載入時循環相依

    idx = _scenes.get(scene.map_key(scene_id) or -1)
    if idx is None:
        idx = _scenes.get(scene_id)               # 本流編號直接命中
    if idx is None:
        return None

    got = _grids.get(idx)
    if got is None:
        try:
            got = _expand(idx)
        except Exception:
            return None
        _grids[idx] = got
        _order.append(idx)
        while len(_order) > CACHE_SIZE:
            _grids.pop(_order.pop(0), None)
    return got


def has(scene_id: int | None) -> bool:
    """表裡有沒有這張圖（不攤開，便宜）。"""
    if scene_id is None or not _load():
        return False
    from app.game import scene

    return (scene.map_key(scene_id) in _scenes) or (scene_id in _scenes)


def status() -> str:
    """給診斷／自我檢查看的一行字。"""
    if not _load():
        return f"離線地形表：沒有（{_fail or '表是空的'}）"
    return f"離線地形表：{len(_files)} 張地圖、{len(_scenes)} 個場景"
