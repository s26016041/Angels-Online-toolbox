"""AOB 自動定位：開工具箱時掃一次程式碼段，把所有寫死的遊戲位址換成當下正確的。

## 為什麼需要

**遊戲改版會讓寫死的位址整批位移。** 2026-08-04 那次實際發生過，症狀是
「工具箱突然掃不到怪物」，當時是一個一個手動重新定位的。而且位移量**不一致**
（實測 +0x10 / +0x18 / −0x10F / −0x11E），不能用同一個 delta 推。

只要函式本體沒被重寫（那次只是整體位移），特徵碼就能自動跟上。

## 怎麼運作

    locate.warm(scanner)        ← 有 scanner 的地方呼叫一次（分頁接上分身時）

它掃 angel.dat 的映像找每一段特徵，命中後**把值寫回各模組的常數**
（`attack.SELECT_FN`、`entity.VT_PLAYER`…）。所以那 60 幾個使用點一行都不用改，
也不必到處傳 scanner。

★ 一次就夠，五台共用：多開的分身載的是**同一份 angel.dat、同一個基底**
  （這遊戲無 ASLR，固定 0x400000），所以位址對每個分身都一樣。
  快取掛在**映像身分**（基底＋PE 表頭的 CRC）上 —— 遊戲更新後重開，
  表頭一定變，會自動重掃；工具箱一直開著也不會拿舊位址用在新版遊戲上。

★ 定位失敗的處理分兩種（2026-08-07 起）：
  * `data` 找不到／多命中 → 保留舊值。讀取側都有 vtable／範圍驗證，
    值錯了只會「讀不到、掃不到」，不會炸。
  * `fn` 找不到／多命中 → **清成 0**。改版後舊的函式位址多半是別支函式
    的中段，跳板 call 進去＝在遊戲主執行緒上當場崩潰 ——「交給遊戲的位址
    一律當場重驗」這條鐵則在這裡的落實就是：驗不過就不給用。
    `Mover.call()` 開頭會把 fn=0 擋下來，功能**大聲停用**（`failed()`
    列得出來、分頁的定位警示看得到），而不是亂呼叫。

## 兩種目標

* `fn`   —— 函式進入點。直接對那個位址的頭幾道指令建特徵。
* `data` —— vtable／全域指標這種**資料位址**。AOB 不能直接掃資料（內容會變），
  改成找「程式碼裡把這個位址當立即值用」的地方，對那段程式碼建特徵，
  命中後從 `imm_at` 把立即值讀回來。

## 遮罩規則（決定改版後還準不準）

* `call`/`jmp` 的 rel32 一律遮掉 —— 呼叫點與目標的位移量不一定相同，rel32 會變。
* **內嵌的絕對位址（落在模組範圍內的立即值）也一律遮掉** —— 由 `_auto_mask()`
  在掃描前自動處理，pattern 原文保留當年的位元組當文件。
  ⚠⚠ 這是 2026-08-07 的方向修正：舊規則「模組內位址保留當錨」看似聰明，
  實際等於**把答案寫死在特徵裡** —— .data 只要位移（多一個全域變數就會），
  這些特徵整批不命中、整批安靜保留舊值。之前「模擬改版 17/17 救回」的測法
  是把 Python 常數打歪、遊戲映像沒動，所以測不到這件事。
  遮掉之後靠**指令骨架**（opcode＋暫存器＋結構偏移）當錨；data 類再加一道
  交叉驗證：同一段裡出現多次的目標位址，命中後必須全部指向**同一個**新值。

每一段的唯一性用 `tools/verify_sigs.py` 對真遊戲驗證（純讀，開著遊戲就能跑）。

純讀記憶體，不寫入、不注入。
"""
from __future__ import annotations

import importlib
import threading
import zlib
from dataclasses import dataclass

GAME_MODULE = "angel.dat"


@dataclass(frozen=True)
class Sig:
    """一個要自動定位的位址。

    module/attr: 要寫回哪個模組的哪個常數
    kind:        'fn' = 命中處就是答案；'data' = 從命中處 +imm_at 讀 4 bytes
    known:       2026-08-04 當下的絕對位址。定位失敗時保留它，也用來報告有沒有移動。
    as_rva:      該常數存的是 RVA（相對模組基底）而不是絕對位址
    """

    module: str
    attr: str
    kind: str
    imm_at: int | None
    pattern: str
    known: int
    as_rva: bool = False
    keep_imm: bool = False   # 例外：只有位址本身能區分這一段，見 monsters


SIGS: tuple[Sig, ...] = (
    Sig("attack", "ACTION_FN", "fn", None,
        "55 8B EC 83 EC 10 6A 03 6A 07 8D 4D F0 E8 ?? ?? ?? ?? 8B 45 F4 8A 4D 0C",
        0x005DA8E5),
    Sig("attack", "SELECT_FN", "fn", None,
        "55 8B EC 83 EC 10 6A 07 6A 16 8D 4D F0 E8 ?? ?? ?? ?? 8B 4D F4 8A 45 08",
        0x005D3D97),
    Sig("attack", "CAST_FN", "fn", None,
        "55 8B EC 83 EC 10 56 8B 75 08 85 F6 7E 3D 6A 12 6A 06 8D 4D F0 E8 ?? ?? ?? ??",
        0x00559EDA),
    # 攻擊指令包（近戰物理技能的關鍵，見 attack.THIRD_FN）。錨在
    # push 8/push 5（封包長 8、代號 5）＋把目標寫進封包與全域那幾行；
    # 兩個 call 的 rel32 與兩個全域位址放萬用。known 是 2026-08-06 的位址
    # （= 8/3 的 0x559FBE 改版搬家後的位置）。
    Sig("attack", "THIRD_FN", "fn", None,
        "55 8B EC 83 EC 10 6A 08 6A 05 8D 4D F0 E8 ?? ?? ?? ?? 8B 4D F4"
        " 8B 45 08 FF 75 FC 89 41 02 A3 ?? ?? ?? ?? 66 8B 45 0C 66 89 41 06"
        " FF 35 ?? ?? ?? ?? E8",
        0x00559EA0),
    # 「用快捷鍵」的本體（= 按 F2）。錨在函式頭＋讀快捷欄全域＋取 +0x2A90，
    # 全域位址與 call 的 rel32 放萬用。見 quickbar.USE_FN。
    Sig("quickbar", "USE_FN", "fn", None,
        "55 8B EC 53 56 57 8B 3D ?? ?? ?? ?? 8B F1 8B 4F 08 FF B1 90 2A 00 00"
        " E8 ?? ?? ?? ?? 8B D8 8B 45 0C 85 C0 79 19",
        0x005B87C5),
    Sig("jumpmap", "BUILD_FN", "fn", None,
        "55 8B EC 56 8B F1 33 C0 57 8B 7D 0C 57 89 06 89 46 04 89 46 08"
        " 89 46 0C E8 ?? ?? ?? ?? 8B 4E 04 66 8B 45 08 66 89 01",
        0x0050DF6E),
    Sig("jumpmap", "SEND_FN", "fn", None,
        "55 8B EC 8B 45 08 8B 0C 85 60 60 A1 00 85 C9 74 08 FF 75 0C"
        " E8 ?? ?? ?? ?? 5D C3",
        0x00711130),
    # 連線物件的全域指標。錨在「送傳送包」那幾行（`mov [eax+2],esi` 起）——
    # 只用 `push [0x9B67D0]` 當特徵不夠獨特。
    Sig("jumpmap", "CONN_PTR", "data", 5,
        "89 70 02 FF 35 D0 67 9B 00 E8 ?? ?? ?? ?? 59 59 5E C9 C2 04 00",
        0x009B67D0),
    # 賣東西前那包「對話動作」（UI 指令表裡的 talkaction）。跟 attack.ACTION_FN
    # 長得幾乎一樣，只差 push 的代號（0x0B vs 0x07）—— 特徵一定要蓋到那兩個
    # push，不然兩支會互相命中。已驗過在模組內唯一。
    Sig("sell", "TALK_FN", "fn", None,
        "55 8B EC 83 EC 10 6A 03 6A 0B 8D 4D F0 E8 ?? ?? ?? ?? 8B 45 F4"
        " 8A 4D 08 FF 75 FC 88 48 02 FF 35 ?? ?? ?? ?? E8 ?? ?? ?? ??"
        " 59 59 C9 C2 04 00",
        0x005DA91E),
    Sig("lua", "GETFIELD_FN", "fn", None,
        "55 8B EC 83 EC 10 53 56 8B 75 08 57 FF 75 0C 56 E8 ?? ?? ?? ??"
        " 8B 55 10 83 C4 08 8B CA 8B F8 8D 59 01 8A 01 41 84 C0 75 F9"
        " 2B CB 51 52 56 E8 ?? ?? ?? ?? FF 76 08 89 45 F0",
        0x006A4290),
    # ⚠ 前 46 bytes 有另一支 Lua 函式（0x6A4DF0）長得**一模一樣**，
    #   連呼叫目標都相同，只有第二個 call 之後才分歧 —— 特徵一定要蓋過那裡。
    Sig("lua", "PCALL_FN", "fn", None,
        "55 8B EC 8B 45 14 83 EC 08 57 8B 7D 08 85 C0 75 04 33 D2 EB 0F"
        " 50 57 E8 ?? ?? ?? ?? 8B D0 83 C4",
        0x006A4740),
    Sig("lua", "CTX_PTR", "data", 2,
        "8B 0D 10 10 89 00 68 C4 3D 7D 00 8D 49 04 E8 ?? ?? ?? ??",
        0x00891010),
    Sig("robot", "RUN_FLAG", "data", 2,
        "C7 05 A4 FB 9C 00 FF FF FF FF C3 C7 05 A4 FB 9C 00 00 00 00 00 C3",
        0x009CFBA4),
    Sig("recall", "USE_ITEM_FN", "fn", None,
        "55 8B EC 83 EC 10 53 8B 5D 08 85 DB 78 4A 8D 4D F0 81 FB FF 00 00 00"
        " 7F 17 6A 07 6A 2E E8 ?? ?? ?? ??",
        0x005DB4E0),
    Sig("move", "MOVE_FN", "fn", None,
        "55 8B EC 83 EC 10 53 8B 5D 10 8D 4D F0 56 8B F3 C1 E6 02 8D 46 09 50 6A 04",
        0x00559F28),
    Sig("move", "WALK_FN", "fn", None,
        "55 8B EC A1 64 5F 9B 00 53 8B 58 10 8B 45 0C 99 F7 FB 8B C8 8B 45 14 99",
        0x005D7C87),
    Sig("move", "PATHFIND_FN", "fn", None,
        "55 8B EC 83 EC 0C 56 57 FF 75 0C 8B F9 33 F6 FF 75 08 89 7D F4 E8 ?? ?? ?? ??",
        0x00549A63),
    Sig("entity", "VT_ENTITY", "data", 3,
        "C7 40 08 24 2A 7D 00 E8 ?? ?? ?? ?? 8D 8B 94 03 00 00 C6 45 FC 04 E8 ?? ?? ?? ??",
        0x007D2A24),
    Sig("entity", "VT_ENTITY2", "data", 2,
        "C7 00 D8 29 7D 00 8D 8B FC 02 00 00 C7 40 08 24 2A 7D 00 E8 ?? ?? ?? ??",
        0x007D29D8),
    Sig("entity", "VT_STATE", "data", 2,
        "C7 03 50 3E 7E 00 C7 43 10 68 3E 7E 00 C7 43 14 80 3E 7E 00 C7 43 38 98 3E 7E 00",
        0x007E3E50),
    Sig("entity", "VT_PLAYER", "data", 10,
        "01 C7 07 AC 8B 7D 00 C7 47 08 F8 8B 7D 00 C7 87 10 02 00 00 00 8C 7D 00",
        0x007D8BF8),
    # ⛔ 拿掉了：entity.VT_MAP_MOBS（怪物類型表）已經沒有任何程式在讀，
    #    留著等於每次 warm() 都白掃一段特徵、還會多一個可能「定位失敗」的
    #    項目餵給自我監察。要復活的話特徵是：
    #    "68 88 8E 7D 00 E8 ?? ?? ?? ?? 83 C4 0C C3 FF 71 0C 0F B6 41 08 50 FF 71 04"
    #    imm_at=1、2026-08-04 的值 0x007D8E88。
    # 精靈設定的單例（那棵 std::map 掛在它 +4）。特徵取自
    # `0x54E068` 裡「還沒建就 new 一個」那段：`mov eax,[單例] / test / jne /
    # push 0x50 / call new …`。已驗證在模組內唯一。
    Sig("robot", "VAR_MGR_PTR", "data", 1,
        "A1 ?? ?? ?? ?? 85 C0 75 ?? 6A 50 E8 ?? ?? ?? ?? 59 8B C8 89 4D 08 "
        "33 C0 89 45 FC 85 C9",
        0x0096E630),
    Sig("player", "VTABLE_RVA", "data", 6,
        "C7 87 68 CB 00 00 1C 3E 7E 00 8D 8F 80 F2 00 00 C6 45 FC 24 E8 ?? ?? ?? ??",
        0x007E3E1C, as_rva=True),
    Sig("scene", "VTABLE_RVA", "data", 6,
        "C7 83 3C 10 00 00 3C CF 7D 00 A1 6C 09 89 00 89 1D 64 5F 9B 00 6A 1F 59",
        0x007DCF3C, as_rva=True),
    Sig("scene", "SCENE_PTR_RVA", "data", 1,
        "0D 48 09 89 00 8B 75 FC 8B 45 0C 03 C3 8B 11 FF 76 20 50 FF 52 18 8B 0D 48 09 89 00",
        0x00890948, as_rva=True),
    # ⚠⚠⚠ **全專案唯一保留 tautology 的一段**（keep_imm=True），原因是實測出來的：
    #   模組裡有 **28 支一模一樣**的「依 ID 查表」存取函式 —— 同樣的邊界檢查
    #   （`cmp ecx,0x88B7`）、同樣的錯誤訊息編號（`push 0x88B9`）、同樣的
    #   「push 0x1FF ／清 512 bytes 緩衝」慣用碼，是同一個樣板產生的，
    #   **彼此只差表位址**，也就是我們要解出來的那個值本身。
    #   把位址遮掉 → 28 個命中（模糊）；往前往後延伸都試過，區分不出來。
    #   所以這段的取捨是：位址留在特徵裡當錨。
    #   後果（已知、可接受）：.data 位移時這段會**定位失敗而不是抓錯**，
    #   failed() 會列出來、分頁警示會亮，monsters 退化成讀不到王／等級／滿血，
    #   不會崩潰。要根治得改用「找 28 個候選再讀表內容驗證哪個是怪物表」。
    Sig("monsters", "INDEX_PTR_RVA", "data", 16,
        "56 8B 75 08 8D 4E FF 81 F9 B7 88 00 00 77 0A"
        " A1 88 BC 98 00 8B 04 B0 EB 3B 68 FF 01 00 00 8D 85 FD FD FF FF"
        " C6 85 FC FD FF FF 00 6A 00 50 E8 ?? ?? ?? ?? 68 B9 88 00 00 56",
        0x0098BC88, as_rva=True, keep_imm=True),
    Sig("move", "WAYPOINTS", "data", 1,
        "68 84 66 9B 00 FF 75 0C 8B C8 FF 75 08 E8 ?? ?? ?? ?? 33 C9 85 C0 0F 9F C1",
        0x009B6684),
    Sig("move", "MGR_PTR", "data", 1,
        "35 38 E6 96 00 8B CE FF B6 90 2A 00 00 E8 ?? ?? ?? ?? 85 C0 74 2F FF 75 08",
        0x0096E638),
    # 快捷欄管理物件的全域指標。錨在 usequickkey（Lua 綁定）的函式頭 ——
    # 表就掛在它 +0x609C（見 quickbar.py）。
    Sig("quickbar", "MGR_PTR", "data", 40,
        "55 8B EC 51 51 8B 45 08 8D 4D F8 56 6A 01 89 45 FC C6 45 F8 00"
        " E8 ?? ?? ?? ?? 6A 02 8D 4D F8 8B F0 E8 ?? ?? ?? ?? 8B 0D AC 66 9B 00"
        " 6A 00 56 50",
        0x009B66AC),
)

# 掃過就不再掃：同一份 angel.dat，五台分身結果一樣。
# ⚠ 快取 key 是「映像身分」（基底＋PE 表頭 4KB 的 CRC），不是單純的 bool ——
#   工具箱常常一直開著，遊戲更新後重開的話表頭一定不同，要自動重掃；
#   以前是永久快取，改版＋重開遊戲會整批沿用舊位址（最危險的安靜壞掉）。
_ran = False
_img_key: tuple[int, int] | None = None
_report: list[tuple[str, int, int | None]] = []
# ⚠ warm() 會被三個地方呼叫：GUI 執行緒（分頁接上分身）、預讀執行緒、
#   自我監察執行緒。沒有鎖的話兩邊可以同時通過 `_done` 檢查，各自把
#   6.4MB 映像讀一遍、25 段特徵掃一遍（白花 0.2~0.5 秒）。
#   寫回的值一樣，所以不會壞，但那是浪費。
_lock = threading.Lock()


def _parse(pattern: str) -> tuple[bytes, bytes]:
    toks = pattern.split()
    sig = bytearray(len(toks))
    mask = bytearray(len(toks))
    for i, t in enumerate(toks):
        if t != "??":
            sig[i] = int(t, 16)
            mask[i] = 1
    return bytes(sig), bytes(mask)


def _seed(sig: bytes, mask: bytes) -> tuple[bytes, int]:
    """挑最長的一段固定位元組當粗篩用的種子（用 bytes.find 很快）。"""
    best_i = best_n = cur_i = cur_n = 0
    for i, m in enumerate(mask):
        if m:
            if cur_n == 0:
                cur_i = i
            cur_n += 1
            if cur_n > best_n:
                best_i, best_n = cur_i, cur_n
        else:
            cur_n = 0
    return sig[best_i:best_i + best_n], best_i


def _auto_mask(sig: bytes, mask: bytes, known: int,
               lo: int, hi: int) -> tuple[bytes, bytes, tuple[int, ...]]:
    """算出兩層遮罩：把 pattern 裡「內嵌的絕對位址」換成萬用字元。

    判定方式：固定位元組區裡任何一個 4-byte 視窗，小端解碼後落在
    [lo, hi)（＝模組映像範圍，跳過表頭）就當成內嵌位址，整組遮掉，
    並跳到視窗後面繼續掃（位址不會互相重疊）。

    回傳 (全遮, 只遮目標, targets)：
    * **全遮** —— 所有內嵌位址都放萬用。最抗改版，但有些段遮完只剩
      編譯器慣用碼（清 512 bytes 緩衝、跳表），會變成模糊命中。
    * **只遮目標** —— 只遮「原值等於 known」的視窗，也就是我們要解出來的
      那個位址；其餘內嵌位址留著當錨。退路用。
    ⚠ 目標視窗**一定要遮**，兩層都是 —— 不遮的話 pattern 等於把答案寫死在
      特徵裡（found 恆等於 known），改版位移必定整段不命中，`moved()` 對
      data 類也永遠是空的。這是 2026-08-07 稽核抓到的主要缺陷。

    targets 給交叉驗證用：同一段裡出現多次的目標位址（例如 RUN_FLAG 的
    開／關兩行寫的是同一個全域），命中後**必須全部指向同一個新值**，
    對不齊就是抓錯段，寧可不採用。
    """
    full = bytearray(mask)
    only = bytearray(mask)
    targets: list[int] = []
    i, n = 0, len(sig)
    while i + 4 <= n:
        if full[i] and full[i + 1] and full[i + 2] and full[i + 3]:
            v = int.from_bytes(sig[i:i + 4], "little")
            if lo <= v < hi:
                full[i:i + 4] = b"\x00\x00\x00\x00"
                if v == known:
                    only[i:i + 4] = b"\x00\x00\x00\x00"
                    targets.append(i)
                i += 4
                continue
        i += 1
    return bytes(full), bytes(only), tuple(targets)


def _image_key(scanner) -> tuple[int, int] | None:
    """映像身分：(基底, PE 表頭 4KB 的 CRC)。讀不到（遊戲剛關）回 None。

    表頭裡有 TimeDateStamp / SizeOfImage / 各節表 —— 改版必變，
    同版重開必同。一次 4KB 讀取，微秒級，每次 warm() 都付得起。
    """
    base = scanner.module_base(GAME_MODULE)
    if not base:
        return None
    head = scanner._read_bytes(base, 0x1000)
    if not head:
        return None
    return (base, zlib.crc32(bytes(head)))


def _find_unique(img: bytes, sig: bytes, mask: bytes) -> int | None:
    """回傳唯一命中的位移；找不到或不只一個都回 None（寧可保留舊值）。"""
    seed, at = _seed(sig, mask)
    if not seed:
        return None
    hit = None
    i = img.find(seed)
    while i >= 0:
        start = i - at
        if start >= 0 and start + len(sig) <= len(img):
            if all(not mask[k] or img[start + k] == sig[k]
                   for k in range(len(sig))):
                if hit is not None:
                    return None            # 不只一個 → 特徵不夠強，別亂改
                hit = start
        i = img.find(seed, i + 1)
    return hit


def warm(scanner, force: bool = False) -> list[tuple[str, int, int | None]]:
    """掃一次並把結果寫回各模組。回傳 [(名稱, 舊值, 新值或 None)]。

    新值 None = 這一項定位失敗：`data` 保留原本寫死的值；`fn` 清成 0、
    由 `Mover.call()` 擋下（理由見檔頭「定位失敗的處理」）。
    同一份映像只掃一次；映像換了（遊戲更新後重開）會自動重掃。
    """
    global _ran, _img_key
    key = _image_key(scanner)
    if _ran and not force and key is not None and key == _img_key:
        return _report
    with _lock:
        # 進到鎖裡再確認一次：等鎖的期間別人可能已經掃完了。
        if _ran and not force and key is not None and key == _img_key:
            return _report
        base = scanner.module_base(GAME_MODULE)
        if not base:
            return []
        info = next((m for m in scanner.list_modules()
                     if m.name.lower() == GAME_MODULE), None)
        if info is None:
            return []
        img = scanner._read_bytes(base, info.size)
        if not img:
            return []

        out: list[tuple[str, int, int | None]] = []
        for s in SIGS:
            sig, mask = _parse(s.pattern)
            m_full, m_only, targets = _auto_mask(
                sig, mask, s.known, base + 0x1000, base + info.size)
            if s.keep_imm:
                # 例外段：位址本身就是唯一的區分點（見 monsters 的說明）。
                off, targets = _find_unique(img, sig, mask), ()
            else:
                # ★ 先用全遮（最抗改版）；那樣不唯一才退回「只遮目標」，
                #   讓其他內嵌位址當錨把它區分開來（見 _auto_mask）。
                #   兩層都要求**唯一命中** —— 模糊命中一律當定位失敗。
                off = _find_unique(img, sig, m_full)
                if off is None:
                    off = _find_unique(img, sig, m_only)
            found = None
            if off is not None:
                if s.kind == "fn":
                    found = base + off
                else:
                    k = off + (s.imm_at or 0)
                    if k + 4 <= len(img):
                        v = int.from_bytes(img[k:k + 4], "little")
                        # ★ 交叉驗證：pattern 裡每個「當年等於 known」的視窗，
                        #   現在也必須全部等於同一個新值 —— 對不齊＝抓錯段。
                        same = all(
                            off + t + 4 <= len(img)
                            and int.from_bytes(
                                img[off + t:off + t + 4], "little") == v
                            for t in targets)
                        # 資料位址一定落在模組內，不然就是抓錯了
                        if same and base <= v < base + info.size:
                            found = v
            mod = importlib.import_module(f"app.game.{s.module}")
            if found is not None:
                setattr(mod, s.attr, found - base if s.as_rva else found)
            elif s.kind == "fn":
                # ⚠⚠ 函式位址驗不過就不給用（清 0 → Mover.call 擋下）。
                #   沿用舊值的話，改版後那裡是別支函式的中段，call 進去
                #   ＝在遊戲主執行緒上當場崩潰。
                setattr(mod, s.attr, 0)
            out.append((f"{s.module}.{s.attr}", s.known, found))
        _ran = True
        _img_key = key
        _report[:] = out
        return out


def moved(report=None) -> list[tuple[str, int, int]]:
    """報告裡「位址跟寫死的不一樣」的項目 —— 改版之後這裡就會有東西。"""
    rep = _report if report is None else report
    return [(n, old, new) for n, old, new in rep
            if new is not None and new != old]


def failed(report=None) -> list[str]:
    """定位失敗的項目（data 保留了寫死的值；fn 已被清 0 停用）。

    ⚠ 「還沒掃過」跟「全部正常」以前對外長得一模一樣（都回空清單）——
      warm() 拿不到模組、讀不到映像時提早 return，自我監察就誤判沒問題。
      現在沒真的掃完過就回一個明確的項目，讓警示亮起來。
    """
    if report is None and not _ran:
        return ["（定位還沒執行過 —— 讀不到遊戲模組，全部位址未經驗證）"]
    rep = _report if report is None else report
    return [n for n, _, new in rep if new is None]
