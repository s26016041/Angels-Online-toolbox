"""施放廣播監聽：inline hook 遊戲的「入向訊息入佇列」函式，攔到伺服器回的
每一包解密後明文，篩出**自己的**施放廣播 —— 這是唯一能在滿速輪迴齊放時
逐發、100% 確定「哪一招真的被伺服器受理」的訊號（SP/MP 扣款糊在一起分不出、
最近技能欄位對地技能不寫、+0x418 清單邊走邊放會漏，全部否決）。

## 為什麼是這個 hook 點（2026-08-18 反組譯定案，見 memory inbound-packet-cast-broadcast）

    recv(0x71236b) → 表頭 XOR 0x1357 解混淆 → 內文解密/解壓
      → 配置明文訊息拷貝 → **0x7122b0(this, 訊息緩衝, 長度)** 入佇列給主執行緒派發

`0x7122b0` 是每一包解密後明文的**唯一咽喉**。在它入口 hook，訊息緩衝開頭就是
opcode。施放廣播的版面（實測 944/945/946/947，跨兩台不同施法者）：

    [op:u16=0x1d @0][施法者ID:u32 @2][子類型:u16=0x0301 @6][技能ID:u32 @8] …

- **子類型 0x0301 = 主施放廣播**（0x0001/0x0401/0x2001 是別的事件，別誤收）。
- **施法者ID = `[玩家實體 + 0x1D0]`**（開機直讀，不必試放；重登換 session 自動更新）。
  伺服器受理才回、拒收不回；每台的施法者ID相異，過濾自己零誤判（同場兩台實測）。

## 風險與紀律

- **熱路徑**（每包一次），stub 任何 bug 立刻崩 → 只在有人要用時才裝
  （「掛機＋首次攻擊」或「自動分身要確認補放」，見 farm_tab._sync_castwatch），
  沒人用就卸；auto-login 會接崩潰。裝前**驗 prologue 位元組**，AOB 對不上就
  **拒裝**（呼叫端退回「送出就算」），絕不對錯位址寫 jmp。
  例外：認得是**自家殘留的 jmp**（上次工具箱沒收乾淨）就先寫回原 prologue
  再重裝（`_repair_stale`，三道認定）；配套是 locate.SIGS 的特徵把會被蓋的
  前 7 bytes 遮成 ??（hook 裝著也定位得到），與 farm_tab 關閉/收分身時
  無條件 release（_teardown/_client_gone）。
- 位址 `INBOUND_FN` 登記進 `locate.SIGS`，改版自動跟上；定位失敗 fn=0 → 拒裝。
- 純攔讀：stub 只把每包前 16 bytes 抄進自己的環狀緩衝，不改遊戲任何狀態、
  執行完偷來的 7 bytes 再跳回原函式，對遊戲完全透明。
"""
from __future__ import annotations

import ctypes
import struct
import threading
from ctypes import wintypes

# ⚠ 改版會位移；locate.SIGS 的 castwatch/INBOUND_FN 會自動定位並寫回這裡。
#   known 值是 2026-08-18 的位址。定位失敗 → 清 0 → start() 拒裝（大聲停用）。
INBOUND_FN = 0x007122B0
# 偷的 prologue：push ebp / mov ebp,esp / push ebx / mov bl,[ebp+0x10]
STOLEN = 7
_EXPECT_PROLOGUE = bytes((0x55, 0x8B, 0xEC, 0x53, 0x8A, 0x5D, 0x10))

CAST_OP = 0x1D           # 施放廣播 opcode（offset 0，u16）
CAST_SUB = 0x0301        # 主施放子類型（offset 6，u16）——只認這個
OFF_CASTER = 2           # 施法者伺服器ID（u32）
OFF_SUB = 6              # 子類型（u16）
OFF_SKILL = 8            # 技能ID（u32）

SRV_ID_OFF = 0x1D0       # 玩家實體 + 這個 = 我的施法者伺服器ID

_N = 512                 # 環槽數（2 的次方）
_CAP = 16                # 每包記前幾 bytes（夠讀 op/caster/sub/skill）
_SLOT = 4 + _CAP         # seq(4) + data
_k32 = ctypes.windll.kernel32


def own_server_id(scanner, player_entity: int) -> int | None:
    """我的施法者伺服器ID = `[玩家實體 + 0x1D0]`；讀不到／不合理回 None。

    ⚠ 換地圖/重連會重建玩家物件 → 要用**當下**的 player_entity 重讀，別快取。
    """
    if not player_entity:
        return None
    raw = scanner._read_bytes(player_entity + SRV_ID_OFF, 4)
    if not raw or len(raw) < 4:
        return None
    v = struct.unpack("<I", bytes(raw[:4]))[0]
    return v if v else None


def _stub_asm(wcnt: int, ring: int, cont: int) -> str:
    """inline hook stub（32 位元）：把每包 seq＋前 16 bytes 記進環狀緩衝，
    再執行偷來的 7 bytes prologue、跳回原函式 +7。

    入口：__thiscall ecx=this、[esp+4]=訊息緩衝、[esp+8]=長度。
    pushad 後偏移：訊息緩衝=[esp+0x24]、長度=[esp+0x28]（this=[esp+0x18] 不需要）。
    """
    return f"""
    pushad
    mov edi, dword ptr [{wcnt:#x}]
    mov eax, edi
    and eax, {_N - 1:#x}
    imul eax, eax, {_SLOT:#x}
    add eax, {ring:#x}
    mov ebx, eax
    mov dword ptr [ebx], edi
    mov edx, dword ptr [esp+0x24]
    mov ecx, dword ptr [esp+0x28]
    cmp ecx, {_CAP:#x}
    jbe cok
    mov ecx, {_CAP:#x}
    cok:
    test edx, edx
    jz done
    lea edi, [ebx+0x4]
    mov esi, edx
    cld
    rep movsb
    done:
    inc dword ptr [{wcnt:#x}]
    popad
    push ebp
    mov ebp, esp
    push ebx
    mov bl, byte ptr [ebp+0x10]
    push {cont:#x}
    ret
    """


class CastHook:
    """掛在單一遊戲行程上的施放廣播監聽。非執行緒安全，同一執行緒用。"""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._pm = None
        self._block = 0
        self._wcnt = 0
        self._ring = 0
        self._orig = b""
        self._active = False
        self._lock = threading.RLock()

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> bool:
        """裝 hook。**驗 prologue 對不上就拒裝**回 False（退舊行為）。裝成回 True。"""
        import keystone
        import pymem

        if not INBOUND_FN:                     # locate 定位失敗清成 0
            return False
        pm = pymem.Pymem()
        pm.open_process_from_id(self._pid)
        # ⚠ 裝前一定要驗：AOB 若命中別支函式、或遊戲改版，位元組會不一樣，
        #   對錯位址寫 jmp = 當場崩潰。對不上寧可不裝。
        cur = bytes(pm.read_bytes(INBOUND_FN, STOLEN))
        if cur != _EXPECT_PROLOGUE:
            cur = self._repair_stale(pm, cur)
        if cur != _EXPECT_PROLOGUE:
            return False

        block = pm.allocate(0x8000)
        wcnt = block
        ring = block + 64
        code = ring + _N * _SLOT
        pm.write_uint(wcnt, 0)
        ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_32)
        ks.syntax = keystone.KS_OPT_SYNTAX_INTEL
        shell, _ = ks.asm(_stub_asm(wcnt, ring, INBOUND_FN + STOLEN),
                          addr=code)
        pm.write_bytes(code, bytes(shell), len(shell))

        jmp = b"\xe9" + struct.pack("<i", code - (INBOUND_FN + 5))
        jmp += b"\x90" * (STOLEN - len(jmp))
        self._orig = cur
        self._patch(pm, jmp)

        self._pm = pm
        self._block = block
        self._wcnt = wcnt
        self._ring = ring
        self._active = True
        return True

    def _repair_stale(self, pm, cur: bytes) -> bytes:
        """認得「上一次工具箱沒收乾淨留下的自家 hook」就修復，回修復後的位元組。

        會發生在：工具箱崩潰、被強關，或 0.4.39 的關閉洩漏 bug（teardown 沒還
        castwatch → 遊戲裡留著 jmp → 下次開工具箱 AOB 掃不到、hook 也裝不回）。
        認定三道全過才動手：①開頭是 E9 rel32；②補位是我們寫的兩個 NOP；
        ③jmp 目標第一個 byte 是 pushad(0x60)＝我們 stub 的固定開頭。
        別人的 patch（樣式不同）一律不碰、照樣拒裝。
        位址身分不靠這 7 bytes —— AOB 特徵錨在後面沒被蓋的骨架（locate.SIGS
        已把這 7 bytes 遮成 ??），命中即確定是這支函式，寫回原始 prologue 安全。
        ⚠ 舊 session 在遊戲裡配的 stub 記憶體（32KB）救不回來，就留著（洩漏
        一次性、無害）；修復後照常走全新安裝。
        """
        if len(cur) != STOLEN or cur[0] != 0xE9 or cur[5:] != b"\x90\x90":
            return cur
        rel = int.from_bytes(cur[1:5], "little", signed=True)
        target = INBOUND_FN + 5 + rel
        try:
            head = bytes(pm.read_bytes(target, 1))
        except Exception:                      # noqa: BLE001
            return cur
        if head != b"\x60":                    # pushad —— 我們 stub 的第一個 byte
            return cur
        self._patch(pm, _EXPECT_PROLOGUE)
        return bytes(pm.read_bytes(INBOUND_FN, STOLEN))

    def _patch(self, pm, data: bytes) -> None:
        old = wintypes.DWORD()
        _k32.VirtualProtectEx(pm.process_handle, ctypes.c_void_p(INBOUND_FN),
                              STOLEN, 0x40, ctypes.byref(old))
        pm.write_bytes(INBOUND_FN, data, len(data))
        _k32.VirtualProtectEx(pm.process_handle, ctypes.c_void_p(INBOUND_FN),
                              STOLEN, old.value, ctypes.byref(wintypes.DWORD()))

    def write_count(self) -> int:
        """目前總攔包數（當「起算點」用：送出首發前記一次，之後只看新的）。"""
        if not self._active:
            return 0
        try:
            return self._pm.read_uint(self._wcnt)
        except Exception:                      # noqa: BLE001
            return 0

    def casts_since(self, since: int) -> list[tuple[int, int]]:
        """自 `since`（write_count 的值）以來的**施放廣播** [(施法者ID, 技能ID)]。

        只回子類型 0x0301 的（主施放），別的事件濾掉。讀壞就當沒有。
        """
        if not self._active:
            return []
        try:
            wc = self._pm.read_uint(self._wcnt)
        except Exception:                      # noqa: BLE001
            return []
        out: list[tuple[int, int]] = []
        start = max(since, wc - _N)
        for i in range(start, wc):
            s = self._ring + (i % _N) * _SLOT
            try:
                data = bytes(self._pm.read_bytes(s + 4, _CAP))
            except Exception:                  # noqa: BLE001
                continue
            if len(data) < OFF_SKILL + 4:
                continue
            if struct.unpack_from("<H", data, 0)[0] != CAST_OP:
                continue
            if struct.unpack_from("<H", data, OFF_SUB)[0] != CAST_SUB:
                continue
            caster = struct.unpack_from("<I", data, OFF_CASTER)[0]
            skill = struct.unpack_from("<I", data, OFF_SKILL)[0]
            out.append((caster, skill))
        return out

    def fired(self, since: int, server_id: int, skill_id: int) -> bool:
        """自 `since` 以來，**我**（server_id）有沒有放出 `skill_id`？

        這就是首發的 100% 確認：伺服器受理才會有這一包、拒收不會有。
        """
        if not (server_id and skill_id):
            return False
        for caster, skill in self.casts_since(since):
            if caster == server_id and skill == skill_id:
                return True
        return False

    def stop(self) -> None:
        """卸 hook、還原原始位元組。"""
        if not self._active:
            return
        try:
            self._patch(self._pm, self._orig)
        except Exception:                      # noqa: BLE001
            pass
        self._active = False


# ── 一個行程一份，比照 move.acquire ────────────────────────────────
_shared: dict[int, CastHook] = {}
_owners: dict[int, set] = {}
_shared_lock = threading.Lock()


def acquire(pid: int, owner) -> CastHook | None:
    """拿這個行程的施放廣播監聽；還沒裝就裝。**裝不起來回 None**（呼叫端退舊行為）。

    owner: 可雜湊物件（通常是分頁自己），用來記「誰還在用」。用完 release。
    """
    with _shared_lock:
        h = _shared.get(pid)
        if h is not None and h.active:
            _owners.setdefault(pid, set()).add(owner)
            return h
        h = CastHook(pid)
    try:
        ok = h.start()
    except Exception:                          # noqa: BLE001
        ok = False
    if not ok:
        return None
    with _shared_lock:
        cur = _shared.get(pid)
        if cur is not None and cur.active and cur is not h:
            h.stop()
            _owners.setdefault(pid, set()).add(owner)
            return cur
        _shared[pid] = h
        _owners.setdefault(pid, set()).add(owner)
    return h


def release(pid: int, owner) -> None:
    """還掉；最後一個人還完才真的卸 hook。"""
    with _shared_lock:
        owners = _owners.get(pid)
        if owners is not None:
            owners.discard(owner)
            if owners:
                return
            _owners.pop(pid, None)
        h = _shared.pop(pid, None)
    if h is not None:
        h.stop()
