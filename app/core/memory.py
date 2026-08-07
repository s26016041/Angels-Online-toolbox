"""記憶體掃描核心（Windows 專用）。

提供 Cheat Engine 風格的數值搜尋，用來找出遊戲中某個數值（經驗值、
HP、金錢…）真正存放的記憶體位址：

  1. open(pid)              以 OpenProcess 取得程序控制代碼。
  2. first_scan(...)        掃描所有可讀（預設只掃「可寫入」）記憶體區段，
                           找出符合條件的所有位址。
  3. next_scan(...)         對上一輪的候選位址重新讀值，依條件（等於 / 大於 /
                           增加 / 減少 / 改變 / 未改變…）篩選，逐步縮小範圍。
  4. read_value/write_value 讀寫單一位址（給觀察清單用）。

全部透過 ctypes 直接呼叫 Win32 API（VirtualQueryEx / ReadProcessMemory /
WriteProcessMemory），只讀取本機上、由使用者自己選定的程序，不搶焦點、
不動遊戲畫面，屬單機自動化用途。

實作重點：
- 掃描以 numpy 向量化比對，單一區段一次比完，速度夠快。
- 為了兼顧「速度、記憶體、可持續篩選」，內部有兩種候選表示法：
    dense（區段快照）：未知初始值搜尋用，保留整個區段的值以便日後做
                       「增加 / 減少」這種相對比較；記憶體上限≈掃描到的位元組。
    sparse（位址清單）：候選數量夠少時自動切換，只留位址與值，省記憶體。
- 掃描一律對齊到型別大小（等同 Cheat Engine 的「快速掃描」），足以應付
  絕大多數遊戲數值。
"""
from __future__ import annotations

import bisect
import ctypes
import os
import struct
import threading
from ctypes import wintypes
from dataclasses import dataclass, field

import numpy as np

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

# --- 程序存取權限 ---------------------------------------------------------
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

# --- 記憶體狀態與保護旗標 -------------------------------------------------
MEM_COMMIT = 0x1000

PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80

_READABLE = (
    PAGE_READONLY
    | PAGE_READWRITE
    | PAGE_WRITECOPY
    | PAGE_EXECUTE_READ
    | PAGE_EXECUTE_READWRITE
    | PAGE_EXECUTE_WRITECOPY
)
_WRITABLE = (
    PAGE_READWRITE
    | PAGE_WRITECOPY
    | PAGE_EXECUTE_READWRITE
    | PAGE_EXECUTE_WRITECOPY
)

# 使用者位址空間上限（64 位元）。32 位元目標會在更早處讓 VirtualQueryEx
# 回傳 0 而自然結束。
_MAX_ADDR = 0x7FFFFFFF0000

# 候選數少於此值就切換成 sparse（位址清單）表示法。
_COMPACT_THRESHOLD = 100_000

# 一次讀取的最大分塊，避免超大區段一次配置過多記憶體。
_READ_CHUNK = 16 * 1024 * 1024

# --- _read_region 的重用緩衝 -------------------------------------------------
# 每次讀區段都新配一塊「跟區段一樣大」的 ctypes 緩衝，配置＋歸零就吃掉整段
# 時間的一半以上（實測 4MB：整段 2.0ms，其中 RPM 本身只有 0.3ms）。
# 這裡改成重用：只在第一次或不夠大時才重新配置。
# ⚠⚠ 一定要 threading.local —— 同一顆 MemoryScanner 會被掃描/攻擊/按鍵/GUI
#   多條執行緒同時呼叫 _read_region，共用一塊緩衝會互相蓋掉對方讀到的內容
#   （症狀：拿到別的位址的資料 → 用錯的怪物 ID 去寫記憶體）。
_scratch = threading.local()
_SCRATCH_MAX = 32 * 1024 * 1024   # 超過就照舊當場配置，不長期佔住記憶體


def _region_buffer(size: int):
    """拿一塊至少 size bytes 的 ctypes 緩衝（本執行緒專用、跨呼叫重用）。

    回傳的緩衝**可能比 size 大且含上一次的殘留資料** —— 呼叫端只能用
    自己這次實際讀到的前段（_read_region 正是如此：迴圈以 size 為界、
    回傳時只切 buf[:off]）。
    """
    if size > _SCRATCH_MAX:
        return (ctypes.c_char * size)()
    buf = getattr(_scratch, "buf", None)
    if buf is None or len(buf) < size:
        buf = (ctypes.c_char * max(size, 1 << 20))()
        _scratch.buf = buf
    return buf


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class MODULEINFO(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", ctypes.c_void_p),
        ("SizeOfImage", wintypes.DWORD),
        ("EntryPoint", ctypes.c_void_p),
    ]


LIST_MODULES_ALL = 0x03


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
    ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t

kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL

kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL

psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
    wintypes.DWORD,
]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

kernel32.IsWow64Process.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.BOOL),
]
kernel32.IsWow64Process.restype = wintypes.BOOL

psapi.EnumProcessModulesEx.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.HMODULE),
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.DWORD,
]
psapi.EnumProcessModulesEx.restype = wintypes.BOOL

psapi.GetModuleInformation.argtypes = [
    wintypes.HANDLE,
    wintypes.HMODULE,
    ctypes.POINTER(MODULEINFO),
    wintypes.DWORD,
]
psapi.GetModuleInformation.restype = wintypes.BOOL

psapi.GetModuleBaseNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.HMODULE,
    wintypes.LPWSTR,
    wintypes.DWORD,
]
psapi.GetModuleBaseNameW.restype = wintypes.DWORD

psapi.GetModuleFileNameExW.argtypes = [
    wintypes.HANDLE,
    wintypes.HMODULE,
    wintypes.LPWSTR,
    wintypes.DWORD,
]
psapi.GetModuleFileNameExW.restype = wintypes.DWORD


# ---------------------------------------------------------------------------
# 數值型別與搜尋條件
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ValueType:
    key: str
    label: str
    dtype: np.dtype
    size: int
    is_float: bool
    struct_fmt: str


# 顯示順序即為 UI 下拉選單順序。
VALUE_TYPES: dict[str, ValueType] = {
    "int32": ValueType("int32", "4 位元組整數", np.dtype("<i4"), 4, False, "<i"),
    "int64": ValueType("int64", "8 位元組整數", np.dtype("<i8"), 8, False, "<q"),
    "int16": ValueType("int16", "2 位元組整數", np.dtype("<i2"), 2, False, "<h"),
    "int8": ValueType("int8", "1 位元組整數", np.dtype("<i1"), 1, False, "<b"),
    "float": ValueType("float", "浮點數 float", np.dtype("<f4"), 4, True, "<f"),
    "double": ValueType("double", "雙精度 double", np.dtype("<f8"), 8, True, "<d"),
}

# 字串搜尋支援的編碼：codec 名稱 -> 顯示名稱。
# 遊戲文字多為 UTF-16LE；帳號等有時是 ASCII/ANSI。
STRING_ENCODINGS: dict[str, str] = {
    "utf-16-le": "UTF-16",
    "mbcs": "ASCII/ANSI",
    "utf-8": "UTF-8",
}

# 搜尋條件的顯示名稱在 app/tabs/memory_tab.py 那邊（SCAN_TYPE_ORDER），
# 這裡只留「哪些條件在什麼時候可用」。
# 首次搜尋只能用這些（其餘需要「上一輪的值」才能比較）。
FIRST_SCAN_TYPES = ("exact", "bigger", "smaller", "unknown")
# 需要在輸入框填數值的條件。
VALUE_REQUIRED = ("exact", "bigger", "smaller")


class ScanCancelled(Exception):
    """使用者中途按下停止時拋出，用來提早結束掃描。"""


@dataclass
class _Region:
    """dense 模式下的單一區段快照。"""

    base: int
    values: np.ndarray  # 對齊後每個位置的值（上一輪讀到的）
    mask: np.ndarray  # 布林遮罩：哪些位置仍是候選


@dataclass
class ModuleInfo:
    name: str
    base: int
    size: int
    path: str = ""

    @property
    def end(self) -> int:
        return self.base + self.size


@dataclass
class PointerPath:
    """一條指標路徑：以「模組名 + 基底偏移」為起點，順著 offsets 逐層解址。

    解析方式（見 MemoryScanner.resolve_path）：
        addr = 目前模組基底 + base_offset
        若 offsets 為空       -> 位址本身就是 addr（靜態值）
        否則 p = *addr; 對 offsets[:-1] 逐一 p = *(p + off); 最後 addr = p + offsets[-1]

    因為起點用「模組名」在執行時查當下基底，所以遊戲重開、位址位移也照樣解得出來。
    """

    module: str
    base_offset: int
    offsets: list[int] = field(default_factory=list)

    @property
    def levels(self) -> int:
        return len(self.offsets)

    @property
    def is_static(self) -> bool:
        return len(self.offsets) == 0

    def expr(self, module_base: int | None = None) -> str:
        """路徑文字。傳入 module_base 時，會在模組名後標出它當下的基底位址，
        例如 "angel.dat(0x400000)"+0x618F3C，方便對照絕對位址。"""
        if module_base is None:
            head = f'"{self.module}"+0x{self.base_offset:X}'
        else:
            head = f'"{self.module}(0x{module_base:X})"+0x{self.base_offset:X}'
        if not self.offsets:
            return head
        chain = " ".join(f"+0x{o:X}" for o in self.offsets)
        return f"[{head}] {chain}"

    @staticmethod
    def from_dict(d: dict) -> "PointerPath":
        return PointerPath(
            d["module"], int(d["base_offset"]), [int(o) for o in d["offsets"]]
        )


def working_set_mb(pid: int) -> float | None:
    """回傳指定 PID 的工作集記憶體（MB）；失敗回傳 None。"""
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not handle:
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return counters.WorkingSetSize / (1024 * 1024)
    finally:
        kernel32.CloseHandle(handle)


class MemoryScanner:
    """對單一程序做數值搜尋與讀寫。

    執行緒安全的界線（掛機分頁實際就是 4~5 條執行緒共用同一顆）：
    ★ 純讀寫路徑（_read_bytes / _read_region / write_value）可以跨執行緒
      共用 —— 它們只讀 self._handle，其餘全是區域變數（重用緩衝也是
      threading.local，見 _region_buffer）。
    ⚠ 搜尋／指標掃描狀態（first_scan / next_scan / build_pointer_map /
      refresh_modules 會動 _regions/_addrs/_values/_mods_sorted…）**不可**
      跨執行緒併用 —— 要加共用快取之前先想這一段。
    """

    def __init__(self) -> None:
        self._handle: int | None = None
        self._pid: int | None = None
        self._can_write = False
        self._psize = 8  # 目標指標大小（位元組）：32 位元程序為 4

        self._vt: ValueType | None = None
        self._mode: str | None = None  # "dense" | "sparse" | None
        self._regions: list[_Region] = []
        self._addrs: np.ndarray = np.empty(0, np.uint64)
        self._values: np.ndarray = np.empty(0, np.dtype("<i4"))

        # 指標掃描相關狀態
        self._ptr_vals: np.ndarray = np.empty(0, np.uint64)  # 依值排序
        self._ptr_addrs: np.ndarray = np.empty(0, np.uint64)  # 對應的槽位址
        self._mods_sorted: list[ModuleInfo] = []
        self._mod_starts: list[int] = []
        self._mod_ends: list[int] = []
        self._module_by_name: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 程序開關
    # ------------------------------------------------------------------
    def open(self, pid: int) -> None:
        self.close()
        full = (
            PROCESS_QUERY_INFORMATION
            | PROCESS_VM_READ
            | PROCESS_VM_WRITE
            | PROCESS_VM_OPERATION
        )
        handle = kernel32.OpenProcess(full, False, pid)
        self._can_write = bool(handle)
        if not handle:
            # 退而求其次：至少能讀（例如寫入權限被拒）。
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
            )
        if not handle:
            err = ctypes.get_last_error()
            raise OSError(
                f"無法開啟程序 (PID {pid})，錯誤碼 {err}。"
                "若目標是以系統管理員身分執行的遊戲，請同樣以系統管理員身分執行本工具。"
            )
        self._handle = handle
        self._pid = pid
        self._psize = 4 if self._is_wow64(handle) else 8
        self.reset()
        self._clear_pointer_state()
        self.refresh_modules()

    @staticmethod
    def _is_wow64(handle: int) -> bool:
        """目標是否為 32 位元程序（於 64 位元 Windows 上以 WOW64 執行）。"""
        flag = wintypes.BOOL()
        if kernel32.IsWow64Process(handle, ctypes.byref(flag)):
            return bool(flag.value)
        return False

    def close(self) -> None:
        if self._handle:
            kernel32.CloseHandle(self._handle)
        self._handle = None
        self._pid = None
        self._can_write = False
        self.reset()
        self._clear_pointer_state()
        self._mods_sorted = []
        self._mod_starts = []
        self._mod_ends = []
        self._module_by_name = {}

    def _clear_pointer_state(self) -> None:
        self._ptr_vals = np.empty(0, np.uint64)
        self._ptr_addrs = np.empty(0, np.uint64)

    @property
    def pointer_size(self) -> int:
        return self._psize

    def reset(self) -> None:
        """清掉目前的搜尋結果，回到「尚未搜尋」狀態。"""
        self._vt = None
        self._mode = None
        self._regions = []
        self._addrs = np.empty(0, np.uint64)
        self._values = np.empty(0, np.dtype("<i4"))

    @property
    def attached(self) -> bool:
        return self._handle is not None

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def can_write(self) -> bool:
        return self._can_write

    @property
    def has_results(self) -> bool:
        return self._mode is not None

    # ------------------------------------------------------------------
    # 底層讀寫
    # ------------------------------------------------------------------
    def _require_handle(self) -> int:
        if not self._handle:
            raise RuntimeError("尚未選定程序。")
        return self._handle

    def _read_bytes(self, addr: int, size: int) -> bytes | None:
        handle = self._require_handle()
        buf = (ctypes.c_char * size)()
        read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            handle, addr, buf, size, ctypes.byref(read)
        )
        if not ok or read.value != size:
            return None
        return bytes(buf)

    def read_value(self, addr: int, vt: ValueType):
        raw = self._read_bytes(addr, vt.size)
        if raw is None:
            return None
        return struct.unpack(vt.struct_fmt, raw)[0]

    def write_value(self, addr: int, vt: ValueType, value) -> None:
        handle = self._require_handle()
        data = struct.pack(vt.struct_fmt, value)
        written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            handle, addr, data, len(data), ctypes.byref(written)
        )
        if not ok or written.value != len(data):
            err = ctypes.get_last_error()
            raise OSError(
                f"寫入失敗（錯誤碼 {err}）。可能沒有寫入權限，"
                "請以系統管理員身分執行本工具再試。"
            )

    # ------------------------------------------------------------------
    # 字串搜尋
    # ------------------------------------------------------------------
    def search_string(
        self,
        text: str,
        enc_keys,
        writable_only: bool = True,
        progress=None,
    ) -> list[tuple[int, str, int]]:
        """在記憶體搜尋字串的位元組樣式（可同時試多種編碼）。

        回傳 [(位址, 編碼 codec 名, 位元組長度), ...]，依位址排序。
        不會動到數值搜尋的狀態，是獨立的一次性查找。
        """
        self._require_handle()
        patterns: list[tuple[str, bytes]] = []
        for key in enc_keys:
            try:
                pat = text.encode(key, errors="strict")
            except Exception:
                continue
            if pat:
                patterns.append((key, pat))
        if not patterns:
            return []

        regions = self._iter_regions(writable_only)
        total = len(regions) or 1
        hits: list[tuple[int, str, int]] = []
        for i, (base, size) in enumerate(regions):
            raw = self._read_region(base, size)
            if raw:
                # `_read_region` 回的是 memoryview（沒有 .find）。這是冷路徑
                # （使用者按一下才跑），複製一次的成本跟以前完全一樣 ——
                # 以前那一份複製是在 _read_region 裡面做的。
                raw = bytes(raw)
                for key, pat in patterns:
                    start = 0
                    while True:
                        j = raw.find(pat, start)
                        if j < 0:
                            break
                        hits.append((base + j, key, len(pat)))
                        start = j + 1
            if progress:
                progress((i + 1) / total)
        hits.sort(key=lambda h: h[0])
        return hits

    def filter_string_hits(
        self, hits, text: str, progress=None
    ) -> list[tuple[int, str, int]]:
        """字串「再次搜尋」：在既有命中位址中，只保留『現在的內容仍等於 text』的。

        每個命中沿用它當初的編碼，把 text 用該編碼編碼後、讀相同長度的位元組比對。
        典型用法：先搜一個字串得到很多命中 → 在遊戲裡把該欄位改成新文字 → 輸入新
        文字再次搜尋 → 只有你真正在改的那個位址會留下來（等同數值搜尋的逐步縮小）。

        hits 與回傳皆為 [(位址, 編碼 codec 名, 位元組長度), ...]，依位址排序。
        """
        self._require_handle()
        out: list[tuple[int, str, int]] = []
        total = len(hits) or 1
        for i, (addr, enc, _blen) in enumerate(hits):
            try:
                pat = text.encode(enc, errors="strict")
            except Exception:
                pat = b""
            if pat:
                raw = self._read_bytes(addr, len(pat))
                if raw is not None and raw == pat:
                    out.append((addr, enc, len(pat)))
            if progress:
                progress((i + 1) / total)
        out.sort(key=lambda h: h[0])
        return out

    def read_string(self, addr: int, byte_len: int, enc_key: str) -> str | None:
        """從位址讀出指定位元組數並依編碼解碼；失敗回傳 None。

        C 字串以 null 結尾，顯示時截到第一個 null（比較乾淨）。
        """
        raw = self._read_bytes(addr, byte_len)
        if raw is None:
            return None
        try:
            s = raw.decode(enc_key, errors="replace")
        except Exception:
            return None
        nul = s.find("\x00")
        return s if nul < 0 else s[:nul]

    def write_string(
        self, addr: int, text: str, enc_key: str, null_terminate: bool = True
    ) -> int:
        """把字串以指定編碼寫入位址；預設補上結尾 null。回傳寫入的位元組數。

        ⚠ 呼叫端要自行判斷長度：若新字串比原本長，會覆蓋後面相鄰的記憶體。
        """
        handle = self._require_handle()
        data = text.encode(enc_key, errors="strict")
        if null_terminate:
            data += b"\x00\x00" if enc_key == "utf-16-le" else b"\x00"
        written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            handle, addr, data, len(data), ctypes.byref(written)
        )
        if not ok or written.value != len(data):
            err = ctypes.get_last_error()
            raise OSError(
                f"寫入失敗（錯誤碼 {err}）。可能沒有寫入權限，"
                "請以系統管理員身分執行本工具再試。"
            )
        return len(data)

    # ------------------------------------------------------------------
    # 區段列舉與讀取
    # ------------------------------------------------------------------
    def _iter_regions(self, writable_only: bool) -> list[tuple[int, int]]:
        """回傳 [(base, size), ...]，只含已提交且可讀（或可寫）的區段。"""
        handle = self._require_handle()
        regions: list[tuple[int, int]] = []
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        size_mbi = ctypes.sizeof(mbi)
        want = _WRITABLE if writable_only else _READABLE
        while addr < _MAX_ADDR:
            ret = kernel32.VirtualQueryEx(
                handle, addr, ctypes.byref(mbi), size_mbi
            )
            if ret == 0:
                break
            base = mbi.BaseAddress
            region_size = mbi.RegionSize
            if region_size == 0:
                break
            protect = mbi.Protect
            if (
                mbi.State == MEM_COMMIT
                and not (protect & PAGE_GUARD)
                and not (protect & PAGE_NOACCESS)
                and (protect & want)
            ):
                regions.append((base, region_size))
            addr = base + region_size
        return regions

    def _read_region(self, base: int, size: int) -> memoryview | None:
        """讀取整個區段；部分可讀時回傳已成功讀到的前段。

        ⚠⚠⚠ **回傳的是指向共用緩衝的 memoryview（零複製），不是 bytes。**
          規則只有兩條，違反了會靜默拿到錯的資料：

          1. **用完就丟，不可跨呼叫持有。** 同一條執行緒下一次 `_read_region`
             就會把內容蓋掉。要留著（存進 self、append 進 list、跨 yield／
             跨 signal 傳出去）一律先 `bytes(raw)` 或 numpy `.copy()`。
          2. **要用 bytes 的方法就先 `bytes(raw)`。** memoryview 沒有
             `.find/.count/.split/.decode/.startswith`。

          可以直接用的：`len()`、`if not raw`、切片、整數索引、切片與 bytes
          比較、`np.frombuffer()`、`struct.unpack_from()`、`re` 模組。

        ★ 為什麼值得：配置＋複製本來佔整段時間的 8 成以上（實測讀 17MB
          9.2ms，其中 ReadProcessMemory 本身只有 2ms）。掃描執行緒每秒
          要讀好幾十 MB，這是全專案最熱的一條路。
        ★ `.cast("B")` 不能省 —— ctypes 的 c_char 陣列 format 是 `<c`，
          那種 memoryview 整數索引會 NotImplementedError，而且
          **切片跟 bytes 比較永遠是 False（不報錯，靜默錯）**。
        ★ `.toreadonly()` 不能省 —— 讓 `np.frombuffer()` 產生的陣列維持
          唯讀（跟以前餵 bytes 時一樣）。誤寫會當場 ValueError，
          而不是安靜汙染共用緩衝。
        """
        handle = self._require_handle()
        buf = _region_buffer(size)
        read = ctypes.c_size_t(0)
        off = 0
        while off < size:
            n = min(_READ_CHUNK, size - off)
            # ⚠ Win32 失敗時不保證會寫 lpNumberOfBytesRead —— 不歸零的話
            #   read.value 會殘留上一輪的計數，off 就多跳一整個 chunk，
            #   回傳的尾巴變成沒讀過的全零緩衝（掃描會靜默漏判）。
            read.value = 0
            ok = kernel32.ReadProcessMemory(
                handle,
                base + off,
                ctypes.byref(buf, off),
                n,
                ctypes.byref(read),
            )
            if not ok:
                if read.value:  # 部分成功：用已讀到的前段即可
                    off += read.value
                break
            if read.value == 0:
                break
            off += read.value
        if off == 0:
            return None
        return memoryview(buf).cast("B")[:off].toreadonly()

    def _region_values(self, base: int, size: int) -> np.ndarray | None:
        """區段內容當成數值陣列。

        ⚠ 回傳的是**指向共用緩衝的視圖**（唯讀），下一次 `_read_region`
          就會被蓋掉 —— 呼叫端要留著就得先 `.copy()`（目前三個呼叫端
          都有做：first_scan 的 dense／sparse 分支與 _next_scan_dense）。
        """
        raw = self._read_region(base, size)
        if raw is None:
            return None
        itemsize = self._vt.size
        n = len(raw) // itemsize
        if n == 0:
            return None
        return np.frombuffer(raw, dtype=self._vt.dtype, count=n)

    # ------------------------------------------------------------------
    # 比較
    # ------------------------------------------------------------------
    def _compare(
        self, cur: np.ndarray, prev: np.ndarray | None, scan_type: str, value
    ) -> np.ndarray:
        is_float = self._vt.is_float
        if scan_type == "exact":
            if is_float:
                return np.isclose(cur, value, rtol=1e-5, atol=1e-2)
            return cur == value
        if scan_type == "bigger":
            return cur > value
        if scan_type == "smaller":
            return cur < value
        if scan_type == "increased":
            return cur > prev
        if scan_type == "decreased":
            return cur < prev
        if scan_type == "changed":
            if is_float:
                return ~np.isclose(cur, prev)
            return cur != prev
        if scan_type == "unchanged":
            if is_float:
                return np.isclose(cur, prev)
            return cur == prev
        if scan_type == "unknown":
            return np.ones(len(cur), dtype=bool)
        raise ValueError(f"未知的搜尋條件：{scan_type}")

    # ------------------------------------------------------------------
    # 首次搜尋
    # ------------------------------------------------------------------
    def first_scan(
        self,
        vt: ValueType,
        scan_type: str,
        value,
        writable_only: bool = True,
        progress=None,
    ) -> int:
        """開始一輪全新搜尋，回傳找到的候選數量。

        scan_type 只能是 exact / bigger / smaller / unknown。
        """
        if scan_type not in FIRST_SCAN_TYPES:
            raise ValueError(
                "首次搜尋只能用「等於 / 大於 / 小於 / 未知初始值」。"
            )
        self.reset()
        self._vt = vt

        regions = self._iter_regions(writable_only)
        total = len(regions) or 1

        if scan_type == "unknown":
            # dense：保留每個區段的完整快照，之後才能做增加/減少比較。
            kept: list[_Region] = []
            for i, (base, size) in enumerate(regions):
                values = self._region_values(base, size)
                if values is not None:
                    kept.append(
                        _Region(
                            base=base,
                            values=values.copy(),
                            mask=np.ones(len(values), dtype=bool),
                        )
                    )
                if progress:
                    progress((i + 1) / total)
            self._regions = kept
            self._mode = "dense"
            self._compact_if_small()
        else:
            # exact/bigger/smaller：直接收集符合的位址（sparse）。
            addr_parts: list[np.ndarray] = []
            val_parts: list[np.ndarray] = []
            for i, (base, size) in enumerate(regions):
                values = self._region_values(base, size)
                if values is not None:
                    mask = self._compare(values, None, scan_type, value)
                    idx = np.nonzero(mask)[0]
                    if len(idx):
                        addr_parts.append(
                            np.uint64(base) + idx.astype(np.uint64) * vt.size
                        )
                        val_parts.append(values[idx].copy())
                if progress:
                    progress((i + 1) / total)
            self._addrs = (
                np.concatenate(addr_parts)
                if addr_parts
                else np.empty(0, np.uint64)
            )
            self._values = (
                np.concatenate(val_parts)
                if val_parts
                else np.empty(0, vt.dtype)
            )
            self._mode = "sparse"

        return self.count()

    # ------------------------------------------------------------------
    # 再次搜尋
    # ------------------------------------------------------------------
    def next_scan(self, scan_type: str, value, progress=None) -> int:
        """對現有候選再篩選一輪，回傳剩餘數量。"""
        if self._mode is None:
            raise RuntimeError("請先做一次首次搜尋。")

        if self._mode == "sparse":
            self._next_scan_sparse(scan_type, value, progress)
        else:
            self._next_scan_dense(scan_type, value, progress)
            self._compact_if_small()
        return self.count()

    def _next_scan_sparse(self, scan_type: str, value, progress) -> None:
        cur, valid = self._read_many(self._addrs, progress)
        step = self._compare(cur, self._values, scan_type, value)
        keep = valid & step
        self._addrs = self._addrs[keep]
        self._values = cur[keep]

    def _next_scan_dense(self, scan_type: str, value, progress) -> None:
        total = len(self._regions) or 1
        survivors: list[_Region] = []
        for i, region in enumerate(self._regions):
            size = len(region.values) * self._vt.size
            cur = self._region_values(region.base, size)
            if cur is not None and len(cur) == len(region.values):
                step = self._compare(cur, region.values, scan_type, value)
                new_mask = region.mask & step
                if new_mask.any():
                    survivors.append(
                        _Region(base=region.base, values=cur.copy(), mask=new_mask)
                    )
            if progress:
                progress((i + 1) / total)
        self._regions = survivors

    def _read_many(self, addrs: np.ndarray, progress=None):
        """讀取多個位址的值，回傳 (值陣列, 有效遮罩)。

        以「區段為單位」批次讀取：把候選依所在區段分組，每個區段整塊讀一次後
        向量化取值。避免逐一 ReadProcessMemory —— 候選達數百萬時（例如關掉
        「只搜尋可寫入區段」）逐一呼叫會慢到像當機。
        """
        n = len(addrs)
        itemsize = self._vt.size
        out = np.zeros(n, dtype=self._vt.dtype)
        valid = np.zeros(n, dtype=bool)
        if n == 0:
            return out, valid

        order = np.argsort(addrs, kind="stable")
        saddr = addrs[order].astype(np.uint64)

        regions = sorted(self._iter_regions(writable_only=False))
        for base, size in regions:
            # 用二分找出落在此區段 [base, base+size) 的候選（saddr 已排序）
            lo = int(np.searchsorted(saddr, np.uint64(base), "left"))
            hi = int(np.searchsorted(saddr, np.uint64(base + size), "left"))
            if hi <= lo:
                continue
            raw = self._read_region(base, size)
            # ⚠ progress 移到「raw 用完之後」才叫（以前夾在讀取與使用之間）。
            #   raw 現在是指向共用緩衝的視圖，回呼若哪天塞了會讀記憶體的東西
            #   進來，就會在我們用它之前把緩衝蓋掉。順序對了就不必擔心。
            if raw:
                rl = len(raw)
                nval = rl // itemsize
                if nval:
                    arr = np.frombuffer(raw, dtype=self._vt.dtype, count=nval)
                    offs = saddr[lo:hi] - np.uint64(base)
                    ok = (offs + itemsize <= rl) & (offs % itemsize == 0)
                    vidx = (offs // itemsize).astype(np.int64)
                    sel = order[lo:hi]
                    good = np.nonzero(ok)[0]
                    if len(good):
                        # fancy index → 新陣列（複製），不是視圖
                        out[sel[good]] = arr[vidx[good]]
                        valid[sel[good]] = True
            if progress:
                progress(min(1.0, hi / n))
        if progress:
            progress(1.0)
        return out, valid

    # ------------------------------------------------------------------
    # dense -> sparse 壓縮
    # ------------------------------------------------------------------
    def _compact_if_small(self) -> None:
        if self._mode != "dense":
            return
        total = sum(int(r.mask.sum()) for r in self._regions)
        if total > _COMPACT_THRESHOLD:
            return
        addr_parts: list[np.ndarray] = []
        val_parts: list[np.ndarray] = []
        for r in self._regions:
            idx = np.nonzero(r.mask)[0]
            if len(idx):
                addr_parts.append(
                    np.uint64(r.base) + idx.astype(np.uint64) * self._vt.size
                )
                val_parts.append(r.values[idx].copy())
        self._addrs = (
            np.concatenate(addr_parts) if addr_parts else np.empty(0, np.uint64)
        )
        self._values = (
            np.concatenate(val_parts)
            if val_parts
            else np.empty(0, self._vt.dtype)
        )
        self._regions = []
        self._mode = "sparse"

    # ------------------------------------------------------------------
    # 結果查詢
    # ------------------------------------------------------------------
    def count(self) -> int:
        if self._mode == "sparse":
            return int(len(self._addrs))
        if self._mode == "dense":
            return sum(int(r.mask.sum()) for r in self._regions)
        return 0

    def results(self, limit: int = 2000) -> list[tuple[int, object]]:
        """回傳 [(address, value), ...]，最多 limit 筆，依位址排序。"""
        out: list[tuple[int, object]] = []
        if self._mode == "sparse":
            order = np.argsort(self._addrs, kind="stable")
            for i in order[:limit]:
                out.append((int(self._addrs[i]), self._values[i].item()))
        elif self._mode == "dense":
            for r in sorted(self._regions, key=lambda x: x.base):
                idx = np.nonzero(r.mask)[0]
                for i in idx:
                    out.append(
                        (r.base + int(i) * self._vt.size, r.values[i].item())
                    )
                    if len(out) >= limit:
                        return out
        return out

    # ==================================================================
    # 模組列舉（把動態位址表達成「模組名 + 偏移」，重開才能還原）
    # ==================================================================
    def list_modules(self) -> list[ModuleInfo]:
        """列出目標程序目前載入的所有模組（含 32 位元 DLL）。"""
        handle = self._require_handle()
        count = 1024
        arr = (wintypes.HMODULE * count)()
        needed = wintypes.DWORD(0)
        if not psapi.EnumProcessModulesEx(
            handle, arr, ctypes.sizeof(arr), ctypes.byref(needed), LIST_MODULES_ALL
        ):
            return []
        n = min(needed.value // ctypes.sizeof(wintypes.HMODULE), count)
        mods: list[ModuleInfo] = []
        for i in range(n):
            hmod = arr[i]
            name_buf = ctypes.create_unicode_buffer(260)
            psapi.GetModuleBaseNameW(handle, hmod, name_buf, 260)
            path_buf = ctypes.create_unicode_buffer(260)
            psapi.GetModuleFileNameExW(handle, hmod, path_buf, 260)
            info = MODULEINFO()
            if psapi.GetModuleInformation(
                handle, hmod, ctypes.byref(info), ctypes.sizeof(info)
            ):
                mods.append(
                    ModuleInfo(
                        name=name_buf.value,
                        base=info.lpBaseOfDll or 0,
                        size=info.SizeOfImage,
                        path=path_buf.value,
                    )
                )
        return mods

    def main_module_dir(self) -> str | None:
        """回傳主程式（.exe）所在資料夾；用來判斷哪些模組是「遊戲自己的」。"""
        handle = self._require_handle()
        buf = ctypes.create_unicode_buffer(260)
        if psapi.GetModuleFileNameExW(handle, None, buf, 260) and buf.value:
            return os.path.dirname(buf.value)
        return None

    def main_module_base(self) -> int | None:
        """回傳主程式（.exe）的載入基底位址；無 ASLR 時固定（如 angel.dat=0x400000）。"""
        handle = self._require_handle()
        buf = ctypes.create_unicode_buffer(260)
        if psapi.GetModuleFileNameExW(handle, None, buf, 260) and buf.value:
            return self.module_base(os.path.basename(buf.value))
        return None

    def game_module_names(self) -> set[str]:
        """回傳「遊戲自己的」模組名集合（主程式 + 與它同資料夾的 DLL）。

        用來把指標路徑的起點限制在遊戲模組，排除系統 / 顯卡驅動等第三方 DLL
        （例如 nvwgf2um.dll），避免掃到重開就失效的假路徑。
        """
        game_dir = self.main_module_dir()
        names: set[str] = set()
        if not game_dir:
            return names
        prefix = os.path.normcase(os.path.join(game_dir, ""))
        for m in self._mods_sorted:
            if m.path and os.path.normcase(m.path).startswith(prefix):
                names.add(m.name.lower())
        return names

    def refresh_modules(self) -> list[ModuleInfo]:
        """重新抓取模組表並建立查詢索引（重開後基底會變，需重抓）。"""
        mods = self.list_modules()
        mods_sorted = sorted(mods, key=lambda m: m.base)
        self._mods_sorted = mods_sorted
        self._mod_starts = [m.base for m in mods_sorted]
        self._mod_ends = [m.end for m in mods_sorted]
        self._module_by_name = {}
        for m in mods:
            # 同名以第一個（通常是主模組）為準
            self._module_by_name.setdefault(m.name.lower(), m.base)
        return mods

    def module_base(self, name: str) -> int | None:
        """回傳指定模組當下的載入基底位址；未載入 / 未選定程序時為 None。"""
        return self._module_by_name.get(name.lower())

    def module_for_address(self, addr: int) -> tuple[ModuleInfo, int] | None:
        """位址若落在某模組內，回傳 (模組, 相對偏移)，否則 None。"""
        if not self._mod_starts:
            return None
        i = bisect.bisect_right(self._mod_starts, addr) - 1
        if i >= 0 and addr < self._mod_ends[i]:
            mod = self._mods_sorted[i]
            return mod, addr - mod.base
        return None

    # ==================================================================
    # 指標對照表與指標掃描
    # ==================================================================
    def build_pointer_map(self, progress=None, should_cancel=None) -> int:
        """掃描可寫入區段裡所有「看起來是指標」的值，建立 值->槽位址 對照表。

        指標路徑掃描的地基：之後要反向找「誰指向目標」全靠這張表。
        回傳收集到的指標槽數量。should_cancel 為可選的取消判斷函式。
        """
        self._require_handle()
        psize = self._psize
        ptr_dtype = np.uint32 if psize == 4 else np.uint64

        # 有效指標的判定範圍：所有已提交可讀區段。
        commit = sorted(self._iter_regions(writable_only=False))
        if commit:
            starts = np.array([b for b, _ in commit], dtype=np.uint64)
            ends = np.array([b + s for b, s in commit], dtype=np.uint64)
        else:
            starts = np.empty(0, np.uint64)
            ends = np.empty(0, np.uint64)

        regions = self._iter_regions(writable_only=True)
        total = len(regions) or 1
        addr_parts: list[np.ndarray] = []
        val_parts: list[np.ndarray] = []
        for i, (base, size) in enumerate(regions):
            if should_cancel and should_cancel():
                raise ScanCancelled()
            raw = self._read_region(base, size)
            if raw:
                n = len(raw) // psize
                if n:
                    vals = np.frombuffer(raw, dtype=ptr_dtype, count=n).astype(
                        np.uint64
                    )
                    if len(starts):
                        idx = np.searchsorted(starts, vals, side="right") - 1
                        idxc = np.clip(idx, 0, len(starts) - 1)
                        valid = (idx >= 0) & (vals < ends[idxc]) & (vals != 0)
                    else:
                        valid = np.zeros(len(vals), dtype=bool)
                    if valid.any():
                        sidx = np.nonzero(valid)[0]
                        slot_addrs = (
                            np.uint64(base) + sidx.astype(np.uint64) * psize
                        )
                        addr_parts.append(slot_addrs)
                        val_parts.append(vals[sidx])
            if progress:
                progress((i + 1) / total)

        if val_parts:
            all_addr = np.concatenate(addr_parts)
            all_val = np.concatenate(val_parts)
            order = np.argsort(all_val, kind="stable")
            self._ptr_vals = all_val[order]
            self._ptr_addrs = all_addr[order]
        else:
            self._clear_pointer_state()
        self.refresh_modules()
        return int(len(self._ptr_vals))

    def pointer_scan(
        self,
        target: int,
        max_level: int = 4,
        max_offset: int = 0x600,
        max_results: int = 8000,
        max_nodes: int = 4_000_000,
        max_slots_per_node: int = 2048,
        allowed_modules: set[str] | None = None,
        progress=None,
        should_cancel=None,
    ) -> list[PointerPath]:
        """反向搜尋能解到 target 的指標路徑（起點必為某模組內的靜態位址）。

        需先呼叫 build_pointer_map()。回傳依「層數少、偏移小」排序的路徑清單。

        allowed_modules：若提供（小寫模組名集合），只接受起點落在這些模組的路徑，
        其餘模組（系統 / 驅動 DLL）視為死路，不採用也不繼續往下找。用來排除像
        nvwgf2um.dll 這種以顯卡驅動為起點的假路徑。
        max_slots_per_node：單一節點最多展開幾個候選指標（取偏移最小、最接近的）。
        真實遊戲某些位址周圍有成千上萬個相近指標，不封頂會分支爆炸、慢到像當機。
        should_cancel：可選的取消判斷函式，回傳 True 時提早結束並回傳已找到的路徑。
        """
        if len(self._ptr_vals) == 0:
            raise RuntimeError("請先建立指標對照表（build_pointer_map）。")

        vals = self._ptr_vals
        addrs = self._ptr_addrs
        mod_starts = self._mod_starts
        mod_ends = self._mod_ends
        mods = self._mods_sorted
        results: list[PointerPath] = []
        seen: set[tuple] = set()
        visited: set[int] = set()
        node_count = [0]

        def _allowed(name: str) -> bool:
            return allowed_modules is None or name.lower() in allowed_modules

        # 靜態直達：目標本身就在模組內（0 層）。
        direct = self.module_for_address(target)
        if direct is not None and _allowed(direct[0].name):
            mod, off = direct
            results.append(PointerPath(mod.name, off, []))
            seen.add((mod.name.lower(), off, ()))

        def dfs(wanted: int, offs: list[int], level: int) -> None:
            if len(results) >= max_results or node_count[0] >= max_nodes:
                return
            if should_cancel and should_cancel():
                raise ScanCancelled()
            # 關鍵：搜尋值一定要轉成和陣列同型別（uint64）。若傳 Python int，
            # numpy 每次都會把整個指標陣列重新轉型，等於每次呼叫都是 O(N)，
            # 在大遊戲上會慢到像當機。轉成 np.uint64 才會走快速二分搜尋。
            lo = int(np.searchsorted(vals, np.uint64(max(0, wanted - max_offset)), "left"))
            hi = int(np.searchsorted(vals, np.uint64(wanted), "right"))
            if hi <= lo:
                return
            # 分支封頂：只保留最接近 wanted（偏移最小）的前 N 個，
            # 否則遇到指標陣列這種密集區會爆炸。vals 遞增，靠近 hi 者偏移最小。
            if hi - lo > max_slots_per_node:
                lo = hi - max_slots_per_node
            # 一次把整段切片轉成 Python int 清單，避免逐格 numpy 取值的高昂開銷。
            slot_list = addrs[lo:hi].tolist()
            val_list = vals[lo:hi].tolist()
            node_count[0] += len(slot_list)
            for slot, v in zip(slot_list, val_list):
                if len(results) >= max_results:
                    return
                off = wanted - v  # 0 <= off <= max_offset
                # 內嵌 module_for_address（熱路徑，省一次函式呼叫）。
                mi = bisect.bisect_right(mod_starts, slot) - 1
                if mi >= 0 and slot < mod_ends[mi]:
                    # 落在某模組內 = 靜態。允許的模組才採用；不允許的當死路，
                    # 不採用也不遞迴（避免把路徑「洗」過驅動記憶體）。
                    mod = mods[mi]
                    if _allowed(mod.name):
                        base_off = slot - mod.base
                        chain = tuple(reversed(offs + [off]))
                        key = (mod.name.lower(), base_off, chain)
                        if key not in seen:
                            seen.add(key)
                            results.append(
                                PointerPath(mod.name, base_off, list(chain))
                            )
                elif level < max_level and slot not in visited:
                    visited.add(slot)
                    dfs(slot, offs + [off], level + 1)
            if progress:
                progress(min(1.0, node_count[0] / max_nodes))

        try:
            dfs(target, [], 1)
        except ScanCancelled:
            pass  # 回傳目前已找到的路徑
        results.sort(key=lambda p: (p.levels, sum(p.offsets)))
        if progress:
            progress(1.0)
        return results

    # ==================================================================
    # 指標路徑解析（重開後用這個把路徑還原成當下的真實位址）
    # ==================================================================
    def _read_pointer(self, addr: int) -> int | None:
        raw = self._read_bytes(addr, self._psize)
        if raw is None:
            return None
        return int.from_bytes(raw, "little")

    def resolve_path(self, path: PointerPath) -> int | None:
        """把指標路徑解析成目前的真實位址；模組不存在或斷鏈時回傳 None。"""
        mod_base = self._module_by_name.get(path.module.lower())
        if mod_base is None:
            return None
        addr = mod_base + path.base_offset
        if path.is_static:
            return addr
        ptr = self._read_pointer(addr)
        if ptr is None:
            return None
        for off in path.offsets[:-1]:
            ptr = self._read_pointer(ptr + off)
            if ptr is None:
                return None
        return ptr + path.offsets[-1]

    def read_path(self, path: PointerPath, vt: ValueType):
        """解析路徑並讀出該位址的值；失敗回傳 None。"""
        addr = self.resolve_path(path)
        if addr is None:
            return None
        return self.read_value(addr, vt)

