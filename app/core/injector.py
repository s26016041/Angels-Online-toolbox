"""在目標遊戲行程注入一個 send 攔截 stub，記錄每個送出的封包。

用途
----
找出「送出登入封包的那段遊戲程式（= 登入函式候選）」，並判斷封包是明文或加密。
每攔到一次 send，就把下列資訊記進目標行程裡一塊環狀緩衝：
  1. 封包內容（可能截斷到 CAP bytes）
  2. 沿 EBP 框架鏈走出來的各層返回位址 = 呼叫鏈
     （第三層開始就是「建構這種封包的函式」，登入 / 攻擊 / 移動各不相同）
     ⚠ 一定要走框架鏈，不能直接複製堆疊 —— 原因見 build_stub_asm 的說明。

原理
----
把 angel.dat 匯入表（IAT）裡 send 那一格改指向自寫的機器碼；stub 記錄完後跳回
真正的 send，對遊戲完全透明。純讀寫記憶體、不改遊戲邏輯、不搶焦點。

因為 angel.dat 是 32 位元、無 ASLR，IAT 位址固定；send 的 IAT 位址用 pefile 解析
執行檔匯入表取得，遊戲改版也能自動對應。

相依：pymem、keystone-engine、pefile（皆延遲載入；未安裝時 available() 會回報）。
"""
from __future__ import annotations

import ctypes
import math
import struct
from collections import Counter
from ctypes import wintypes
from dataclasses import dataclass, field


# angel.dat 程式碼(.text)大致範圍，用來從堆疊挑出「遊戲內的返回位址」= 呼叫鏈。
# base 0x400000、.text 從 0x401000 起約 3.8MB。無 ASLR，固定。
CODE_LO = 0x401000
CODE_HI = 0x7D0000

# 環狀緩衝參數
_N = 128            # 槽數（2 的次方，取模用 and）
_FRAMES = 12        # 沿 EBP 往上走幾層
# 每層記 6 個 dword：[ebp+4] 返回位址，加上 [ebp+8/+c/+10/+14/+18] 前五個參數。
# 記參數是為了看清楚「建構這種封包的函式」被傳了什麼 —— 那些函式是腳本 VM 的
# 原生函式，參數從腳本堆疊推進來，光看呼叫端的組語看不出語意。
# （記到第五個是因為 0x559FF8 有五個參數，第五個在 ebp+0x18。）
_FRAME_DWORDS = 6
_CAP = 200          # 每筆最多記幾 bytes payload
_PAYLOAD_OFF = 8 + _FRAMES * _FRAME_DWORDS * 4   # caller(4)+len(4)+frames
_SLOT = _PAYLOAD_OFF + _CAP


def available() -> tuple[bool, str]:
    """回傳 (是否可用, 缺套件的安裝提示)。"""
    missing = []
    for mod in ("pymem", "keystone", "pefile"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return True, ""
    pkg = {"keystone": "keystone-engine"}
    names = " ".join(pkg.get(m, m) for m in missing)
    return False, f"需要套件：{names}\n請執行： py -m pip install {names}"


def process_path(pid: int) -> str | None:
    """取得指定 PID 的執行檔完整路徑。"""
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(handle)


def _resolve_iat(exe_path: str, func: str) -> int | None:
    """從執行檔匯入表取出某函式的 IAT 位址（含 ImageBase 的 VA）。"""
    import pefile

    pe = pefile.PE(exe_path, fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
    )
    for entry in pe.DIRECTORY_ENTRY_IMPORT:
        for imp in entry.imports:
            if imp.name and imp.name.decode(errors="replace") == func:
                return imp.address
    return None


def build_stub_asm(wcnt: int, ring: int, origp: int) -> str:
    """產生 send 攔截 stub 的組合語言（32 位元）。

    ⚠ keystone 把無前綴數字當「十六進位」解析，所以所有數字一律用 0x 十六進位。
    stub 在每次 send 時：把 caller 返回位址、長度、呼叫鏈、payload 記進環狀緩衝，
    再跳回真正的 send（jmp，對遊戲完全透明）。

    ★ 呼叫鏈為什麼要沿 EBP 走，不能直接複製堆疊 ★
    ------------------------------------------------
    送封包的函式（實測 0x712840）開頭是
        mov eax, 0xfdf8 ; call __chkstk      ← 配置 64KB 區域變數
    所以**真正呼叫它的動作函式的返回位址在約 65,000 bytes 之外**。
    原本的做法是從 esp+0x20 複製 96 bytes 堆疊，永遠搆不到那裡 ——
    抓到的全是堆疊殘值（例如 0x712AB3，其實是那個函式自己的 mov esp,ebp）。
    結果就是「每種封包看起來都出自同一個地方」，完全找不到動作函式。

    這個函式有標準的 push ebp / mov ebp,esp，所以 send 當下：
        [ebp+4] = 呼叫它的人 = 建構這種封包的函式
        [ebp]   = 上一層的 ebp，可以一直往上走
    改走框架鏈之後，實測五種封包各自對應到不同的建構函式，馬上就分得出來。

    ⚠ 走鏈要解參考堆疊上的位址，讀到壞位址 = 存取違規 = 遊戲當場關閉。
    三道保護，任一不過就停止並把剩下的補 0：
      1. 下界：必須大於目前 esp，而且每層都要比前一層高（防止繞回去無限迴圈）
      2. 上界：TEB(fs:[0x18]) 的 StackBase，也就是這條執行緒堆疊真正的頂端
      3. 對齊：必須 4 對齊

    ★ 每層除了返回位址，還記 [ebp+8/+c/+10/+14] = 那一層函式的前四個參數。
      這樣就能直接看到「建構封包的函式」收到什麼 —— 那些是腳本 VM 的原生函式，
      參數從腳本堆疊推進來，看呼叫端的組語推不出語意。
      ⚠ 因為現在會讀到 [ebp+0x14]，上界必須從 StackBase-8 收緊到 **-0x20**，
        否則最後一層可能讀出堆疊頂端 —— 那就是當場崩潰。
    """
    return f"""
    pushad
    mov eax, dword ptr [{wcnt:#x}]
    and eax, {(_N - 1):#x}
    imul eax, eax, {_SLOT:#x}
    add eax, {ring:#x}
    mov ebx, eax
    mov ecx, dword ptr [esp+0x20]
    mov dword ptr [ebx], ecx
    mov ecx, dword ptr [esp+0x2c]
    mov dword ptr [ebx+0x4], ecx

    lea edi, [ebx+0x8]
    mov esi, esp
    mov edx, ebp
    mov eax, dword ptr fs:[0x18]
    mov ebp, dword ptr [eax+0x4]
    sub ebp, 0x20
    mov ecx, {_FRAMES:#x}
    walk:
    cmp edx, esi
    jbe fill
    cmp edx, ebp
    jae fill
    test dl, 0x3
    jnz fill
    mov eax, dword ptr [edx+0x4]
    mov dword ptr [edi], eax
    mov eax, dword ptr [edx+0x8]
    mov dword ptr [edi+0x4], eax
    mov eax, dword ptr [edx+0xc]
    mov dword ptr [edi+0x8], eax
    mov eax, dword ptr [edx+0x10]
    mov dword ptr [edi+0xc], eax
    mov eax, dword ptr [edx+0x14]
    mov dword ptr [edi+0x10], eax
    mov eax, dword ptr [edx+0x18]
    mov dword ptr [edi+0x14], eax
    add edi, 0x18
    mov esi, edx
    mov edx, dword ptr [edx]
    dec ecx
    jnz walk
    jmp payload
    fill:
    mov dword ptr [edi], 0x0
    mov dword ptr [edi+0x4], 0x0
    mov dword ptr [edi+0x8], 0x0
    mov dword ptr [edi+0xc], 0x0
    mov dword ptr [edi+0x10], 0x0
    mov dword ptr [edi+0x14], 0x0
    add edi, 0x18
    dec ecx
    jnz fill

    payload:
    mov ecx, dword ptr [esp+0x2c]
    cmp ecx, {_CAP:#x}
    jbe cap_ok
    mov ecx, {_CAP:#x}
    cap_ok:
    mov esi, dword ptr [esp+0x28]
    lea edi, [ebx+{_PAYLOAD_OFF:#x}]
    cld
    rep movsb
    inc dword ptr [{wcnt:#x}]
    popad
    jmp dword ptr [{origp:#x}]
    """


def entropy(data: bytes) -> float:
    """Shannon 亂度（0~8）。越接近 8 越像加密/壓縮。"""
    if not data:
        return 0.0
    c = Counter(data)
    n = len(data)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


@dataclass
class Packet:
    seq: int
    caller: int          # 直接呼叫 send 者的返回位址（多半是遊戲的 send wrapper，固定）
    length: int          # 實際送出長度
    data: bytes          # 內容（截斷到 CAP）
    frames: list[int]    # 沿 EBP 走出來的各層返回位址（0 = 走不下去了）
    # 各層函式的前四個參數，與 frames 同索引。
    # ⚠ 索引 i 的參數屬於「返回位址是 frames[i] 的那一層」，也就是**被呼叫的那個
    #   函式自己**的參數 —— 例如 frames[i] == 0x664608 時，args[i] 就是
    #   移動封包建構函式 0x55A046 收到的 (a1, a2, count, a4)。
    args: list[tuple[int, int, int, int, int]] = field(default_factory=list)

    @property
    def call_chain(self) -> list[int]:
        """呼叫鏈：落在 angel.dat 程式碼範圍的返回位址，由內而外。

        前兩層通常固定（送出佇列 wrapper、封包送出層），**第三層開始才是
        「建構這種封包的函式」**，不同動作會不一樣 —— 那才是要找的東西。
        """
        out: list[int] = []
        for v in self.frames:
            if CODE_LO <= v < CODE_HI and v not in out:
                out.append(v)
        return out

    @property
    def entropy(self) -> float:
        return entropy(self.data)

    def hexdump(self, width: int = 16, maxlen: int = 512) -> str:
        lines = []
        d = self.data[:maxlen]
        for i in range(0, len(d), width):
            chunk = d[i : i + width]
            h = " ".join(f"{b:02X}" for b in chunk)
            t = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{i:04X}  {h:<{width*3}} {t}")
        if self.length > len(d):
            lines.append(f"…（共 {self.length} bytes，只顯示前 {len(d)}）")
        return "\n".join(lines)


class SendCapture:
    """掛在單一遊戲行程上的 send 攔截器。非執行緒安全，請在同一執行緒使用。"""

    def __init__(self, pid: int, exe_path: str) -> None:
        self._pid = pid
        self._exe = exe_path
        self._pm = None
        self._iat = 0
        self._orig = 0
        self._block = 0
        self._read = 0
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def pid(self) -> int:
        return self._pid

    # ------------------------------------------------------------------
    def start(self) -> None:
        import keystone
        import pymem

        iat = _resolve_iat(self._exe, "send")
        if not iat:
            raise RuntimeError(
                "在執行檔匯入表找不到 send（可能不是預期的遊戲執行檔）。"
            )
        pm = pymem.Pymem()
        pm.open_process_from_id(self._pid)
        orig = pm.read_uint(iat)

        block = pm.allocate(0x10000)
        wcnt = block            # 寫入計數
        origp = block + 4       # 原 send 位址
        ring = block + 64       # 環狀緩衝起點
        code = ring + _N * _SLOT
        pm.write_uint(wcnt, 0)
        pm.write_uint(origp, orig)

        asm = build_stub_asm(wcnt, ring, origp)
        ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
        ks.syntax = keystone.KS_OPT_SYNTAX_INTEL
        shell, _ = ks.asm(asm, addr=code)
        pm.write_bytes(code, bytes(shell), len(shell))

        self._patch(pm, iat, code)
        self._pm = pm
        self._iat = iat
        self._orig = orig
        self._block = block
        self._read = 0
        self._active = True

    @staticmethod
    def _protect(pm, addr: int, size: int, prot: int) -> int:
        old = wintypes.DWORD()
        ctypes.windll.kernel32.VirtualProtectEx(
            pm.process_handle, ctypes.c_void_p(addr), size, prot, ctypes.byref(old)
        )
        return old.value

    def _patch(self, pm, iat: int, target: int) -> None:
        old = self._protect(pm, iat, 8, 0x40)  # PAGE_EXECUTE_READWRITE
        pm.write_uint(iat, target)
        self._protect(pm, iat, 8, old)

    # ------------------------------------------------------------------
    def read_new(self) -> list[Packet]:
        """讀出上次之後新攔到的封包。"""
        if not self._active:
            return []
        pm = self._pm
        ring = self._block + 64
        wc = pm.read_uint(self._block)
        start = self._read
        if wc - start > _N:      # 溢位，只保留最後 N 筆
            start = wc - _N
        out: list[Packet] = []
        for idx in range(start, wc):
            slot = ring + (idx % _N) * _SLOT
            head = pm.read_bytes(slot, _PAYLOAD_OFF)
            vals = struct.unpack(f"<II{_FRAMES * _FRAME_DWORDS}I", head)
            caller, length = vals[0], vals[1]
            rec = vals[2:]
            frames = [rec[i * _FRAME_DWORDS] for i in range(_FRAMES)]
            args = [tuple(rec[i * _FRAME_DWORDS + 1:i * _FRAME_DWORDS
                              + _FRAME_DWORDS])
                    for i in range(_FRAMES)]
            n = min(length, _CAP)
            data = bytes(pm.read_bytes(slot + _PAYLOAD_OFF, n)) if n > 0 else b""
            out.append(Packet(idx, caller, length, data, frames, args))
        self._read = wc
        return out

    def stop(self) -> None:
        """還原 IAT，停止攔截。"""
        if not self._active:
            return
        try:
            self._patch(self._pm, self._iat, self._orig)
        except Exception:
            pass
        self._active = False
