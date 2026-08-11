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

## 三種目標

* `fn`   —— 函式進入點。直接對那個位址的頭幾道指令建特徵。
* `data` —— vtable／全域指標這種**資料位址**。AOB 不能直接掃資料（內容會變），
  改成找「程式碼裡把這個位址當立即值用」的地方，對那段程式碼建特徵，
  命中後從 `imm_at` 把立即值讀回來。
* `off`  —— **結構偏移**（`[edi+0xCB08]` 那個 0xCB08）。跟 data 同一套，只是
  讀回來的值不是位址、不做模組範圍檢查。2026-08-11 那次改版就是它變了
  （狀態物件裡角色屬性從 +0xCB68 搬到 +0xCB08），本來只會**安靜變慢**
  （捷徑驗不過 → 退回 0.4~1 秒全掃），現在跟著自動定位。
  ⚠ 偏移不落在模組範圍內 → `_auto_mask` 不會自動遮，pattern 裡要**自己寫 `??`**，
  不然又是「拿答案當錨」。

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
    # ★ 字串內容當錨（2026-08-11 加）：命中後從 str_at 讀 4 bytes 當指標，
    #   那裡的 NUL 結尾字串必須等於 str_val，不然這個命中不算。
    #   用途：兩支「同一個模子印出來」的函式只差一個字串（`Npc` / `Pet`），
    #   位址會位移但**字串內容不會變** —— 這樣就不必像以前那樣把表位址
    #   寫死在特徵裡當錨（那等於拿答案比對答案，改版必定定位失敗）。
    str_at: int | None = None
    str_val: bytes = b""


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
    # ⚠ 2026-08-11 改版重編譯過，暫存器配置變了（舊：`8B 3D 全域 / 8B F1 /
    #   8B 4F 08`＝edi 拿全域、esi=this；新：`8B 35 全域 / 8B F9 / 8B 4E 08`
    #   ＝反過來）。骨架其他部分一字未改，所以只換了那三處。
    #   ★ 找回它的方法：quickbar.MGR_PTR 那段特徵就是 Lua 綁定 usequickkey
    #     的函式頭，它結尾那個 call 的目標就是這一支（0x58E71F → 0x5B76C4）。
    Sig("quickbar", "USE_FN", "fn", None,
        "55 8B EC 53 56 8B 35 ?? ?? ?? ?? 57 8B F9 8B 4E 08 FF B1 90 2A 00 00"
        " E8 ?? ?? ?? ?? 8B D8 8B 45 0C 85 C0 79 19",
        0x005B76C4),
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
    # Lua 狀態的全域（`[CTX_PTR]+8` = lua_State）。
    # ⚠ 這段的指令骨架**完全不獨特** —— 全遮之後 445 個命中（`mov ecx,[全域] /
    #   push 字串 / lea ecx,[ecx+4] / call` 是叫 Lua 函式的固定慣用碼，滿地都是）。
    #   以前只有「只遮目標」那層唯一，而那層拿舊位址當錨 —— 位址一移就沒了，
    #   跟 2026-08-11 弄壞自動登入的 VT_LOGIN 是同一種病（`patch_doctor` 抓到的）。
    # ★ 改錨在後面 push 的**字串內容** `UpdateEquipPetNew`：445 → 1，
    #   而且模擬改版也定位得回來。
    Sig("lua", "CTX_PTR", "data", 2,
        "8B 0D F0 0F 89 00 68 C4 3D 7D 00 8D 49 04 E8 ?? ?? ?? ??",
        0x00890FF0, str_at=7, str_val=b"UpdateEquipPetNew"),
    Sig("robot", "RUN_FLAG", "data", 2,
        "C7 05 A4 FB 9C 00 FF FF FF FF C3 C7 05 A4 FB 9C 00 00 00 00 00 C3",
        0x009CFBA4),
    # 組隊：同意／踢人／升隊長／退組／解散／拒絕 共用的封包函式。
    # 跟 attack.SELECT_FN、sell.TALK_FN 是同一個模子（`6A 長度 / 6A 代號 /
    # call 建封包`），差別只在代號 0x18 —— 特徵一定要蓋到那兩個 push
    # 與後面「把動作寫進 +2、參數寫進 +3」那兩行，不然三支會互相命中。
    Sig("team", "ACTION_FN", "fn", None,
        "55 8B EC 83 EC 10 6A 07 6A 18 8D 4D F0 E8 ?? ?? ?? ?? 8B 4D F4"
        " 8A 45 08 FF 75 FC 88 41 02 8B 45 0C 89 41 03 FF 35 D0 67 9B 00"
        " E8 ?? ?? ?? ?? 59 59 C9 C2 08 00",
        0x005D5355),
    # 組隊邀請。函式頭是 SEH 序言（`push 0x2C / mov eax,例外處理表`），
    # 那個表位址是模組內立即值，會被 _auto_mask 遮掉；靠 `6A 24 6A 17`
    # （內文 36、代號 0x17）與後面取名字、取分配方式那幾行當骨架。
    Sig("team", "INVITE_FN", "fn", None,
        "6A 2C B8 72 6A 7C 00 E8 ?? ?? ?? ?? 8B 45 08 8D 4D D8 50"
        " E8 ?? ?? ?? ?? 6A 24 6A 17 8D 4D C8 C7 45 FC 00 00 00 00"
        " E8 ?? ?? ?? ?? 83 7D EC 0F",
        0x005D538A),
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
    # 角色屬性物件的 vtable，寫在狀態物件建構函式裡（`mov [edi+偏移], vtable`）。
    # ⚠⚠ 2026-08-11 改版**兩個結構偏移都變了**（0xCB68→0xCB08、0xF280→0xF220），
    #   舊特徵把它們當骨架 → 整段不命中。現在那兩個 disp32 與 SEH 序號一律放
    #   萬用（實測全遮之後仍然唯一），只靠 `mov [edi+disp],imm / lea ecx,[edi+disp]
    #   / mov byte [ebp-4],序號 / call` 這串骨架。
    Sig("player", "VTABLE_RVA", "data", 6,
        "C7 87 ?? ?? 00 00 F4 3D 7E 00 8D 8F ?? ?? 00 00 C6 45 FC ?? E8 ?? ?? ?? ??",
        0x007E3DF4, as_rva=True),
    # ★ 同一道指令的 disp32 ＝ 角色屬性物件在狀態物件裡的位置（見 player.py
    #   的 VT_OFF_FROM_MGR）。以前寫死成 0xCB68+0x20，改版後捷徑就驗不過、
    #   每次都退回全掃（只變慢、不會算錯）。現在跟著自動定位。
    Sig("player", "VT_OFF_FROM_MGR", "off", 2,
        "C7 87 ?? ?? 00 00 F4 3D 7E 00 8D 8F ?? ?? 00 00 C6 45 FC ?? E8 ?? ?? ?? ??",
        0x0000CB08),
    Sig("scene", "VTABLE_RVA", "data", 6,
        "C7 83 3C 10 00 00 3C CF 7D 00 A1 6C 09 89 00 89 1D 64 5F 9B 00 6A 1F 59",
        0x007DCF3C, as_rva=True),
    Sig("scene", "SCENE_PTR_RVA", "data", 1,
        "0D 48 09 89 00 8B 75 FC 8B 45 0C 03 C3 8B 11 FF 76 20 50 FF 52 18 8B 0D 48 09 89 00",
        0x00890948, as_rva=True),
    # 怪物／NPC 範本表（[這裡]+種類ID*4 → 範本；見 app/game/monsters.py）。
    # ⚠ 這是那 28 支「同一個模子印出來」的查表函式之一。連邊界值 0x88B7 與
    #   錯誤訊息編號 0x88B9 都跟另一支（寵物表）**完全相同** —— 全遮之後
    #   剩下兩個候選 0x508D89（Npc）與 0x5AC245（Pet）。
    # ★★ 2026-08-11 起靠**字串內容**分辨：那句錯誤訊息是
    #   `Get %s Data Error, ID:%d >= MAX:%d`，%s 帶進去的常數字串一支是
    #   `Npc`、一支是 `Pet`。字串位址會位移，**內容不會**，所以拿它當錨
    #   完全不必把表位址寫死（以前那段 keep_imm 的 tautology 就是被這個取代的
    #   —— 2026-08-11 改版 .data 位移 −0x20，舊寫法如預期整段定位失敗）。
    Sig("monsters", "INDEX_PTR_RVA", "data", 16,
        "56 8B 75 08 8D 4E FF 81 F9 B7 88 00 00 77 0A"
        " A1 68 BC 98 00 8B 04 B0 EB 3B 68 FF 01 00 00 8D 85 FD FD FF FF"
        " C6 85 FC FD FF FF 00 6A 00 50 E8 ?? ?? ?? ?? 68 B9 88 00 00 56"
        " 68 C0 29 7D 00",
        0x0098BC68, as_rva=True, str_at=58, str_val=b"Npc"),
    # 技能範本表（[這裡]+技能ID*4 → 範本；見 app/game/skillcost.py）。
    # ⚠ 跟怪物表是**同一種**查表函式（模組裡 28 支長得一模一樣）。以前以為
    #   「邊界值 0x61A7 與錯誤訊息編號 0x61A9 是它自己的、可以當錨」——
    #   **錯了**：`_auto_mask` 會把 `F9 A7 61 00`(=0x61A7F9) 與 `68 A9 61 00`
    #   (=0x61A968) 當成模組內位址遮掉，全遮之後那兩個常數根本不在特徵裡，
    #   28 支又撞成一團。它一直是靠「只遮目標」那層才唯一的（＝拿舊位址當錨，
    #   改版必失效）。2026-08-11 `patch_doctor` 抓到的預防性項目。
    # ★ 跟 monsters 一樣改用**字串內容**當錨：`Magic`（怪物表那支是 `Npc`）。
    #   28 → 1，模擬改版也定位得回來。
    Sig("skillcost", "TABLE_PTR", "data", 16,
        "56 8B 75 08 8D 4E FF 81 F9 A7 61 00 00 77 0A"
        " A1 90 BC 98 00 8B 04 B0 EB 3B 68 FF 01 00 00 8D 85 FD FD FF FF"
        " C6 85 FC FD FF FF 00 6A 00 50 E8 ?? ?? ?? ?? 68 A9 61 00 00 56"
        " 68 74 29 7D 00",
        0x0098BC90, str_at=58, str_val=b"Magic"),
    Sig("move", "WAYPOINTS", "data", 1,
        "68 84 66 9B 00 FF 75 0C 8B C8 FF 75 08 E8 ?? ?? ?? ?? 33 C9 85 C0 0F 9F C1",
        0x009B6684),
    Sig("move", "MGR_PTR", "data", 1,
        "35 38 E6 96 00 8B CE FF B6 90 2A 00 00 E8 ?? ?? ?? ?? 85 C0 74 2F FF 75 08",
        0x0096E638),
    # 地圖物件（可走格；見 app/game/terrain.py）。錨在遊戲自己的
    # 「走到座標」常式 0x507D87 的序幕：取地圖物件 → 讀 +0x10（每格幾單位）
    # → 讀自己的世界座標 +0xC6。
    Sig("terrain", "MAP_PTR", "data", 5,
        "55 8B EC 51 A1 64 5F 9B 00 53 56 57 8B 70 10 8B F9"
        " 0F BF 8F C6 00 00 00",
        0x009B5F64),
    # --- 自動登入（見 app/game/login.py）-----------------------------------
    # 登入按鈕的完整動作（thiscall，零參數）。錨在 /GS 序幕＋「取伺服器清單
    # 控制項 0xA92」那幾行；0x878240（/GS cookie）由 _auto_mask 遮掉。
    Sig("login", "LOGIN_FN", "fn", None,
        "55 8B EC 83 EC 28 A1 40 82 87 00 33 C5 89 45 FC 56 8B F1"
        " 68 92 0A 00 00 FF B6 64 10 00 00 8B 4E 0C E8",
        0x00538959),
    # 「進入遊戲」那一包（stdcall(角色格號)，代號 6、內文 0x25）。
    Sig("login", "ENTER_FN", "fn", None,
        "55 8B EC 83 EC 10 6A 25 6A 06 8D 4D F0 E8 ?? ?? ?? ??"
        " 8B 4D F4 8A 45 08 6A 20 68 14 08 89 00 88 41 02"
        " A0 10 08 89 00 88 41 03 8D 41 04 50 E8",
        0x0050F880),
    # 登入畫面物件的主 vtable（`find_screen()` 靠它掃出登入畫面物件）。
    # ⚠⚠ 2026-08-11 這一段是**整個自動登入卡在「等遊戲開好」的元凶**：
    #   舊特徵只蓋到 +0x2C 那行，全遮之後跟另一支建構函式（0x608B52，
    #   vtable 0x7E71B4）撞在一起 → 模糊命中 → data 類保留舊值 0x7D6C94
    #   → 掃不到登入畫面物件 → 一直等下去。以前靠「只遮目標」那層才唯一，
    #   而那層是拿舊位址當錨的，位址一移就沒了 —— 等於沒有退路。
    # ★ 現在往後多蓋兩行（+0x30、+0x34）：那兩行只有登入畫面這一支有，
    #   全遮之後就唯一，跟位址完全無關。
    Sig("login", "VT_LOGIN", "data", 4,
        "8B F9 C7 07 74 6C 7D 00 C7 47 10 8C 6C 7D 00 C7 47 14 A4 6C 7D 00"
        " C7 47 18 B0 6C 7D 00 C7 47 2C BC 6C 7D 00 C7 47 30 C8 6C 7D 00"
        " C7 47 34 D4 6C 7D 00 E8",
        0x007D6C74),
    # 帳號／密碼緩衝區。錨在登入按鈕裡「把輸入框的字抄進全域」那兩段。
    Sig("login", "ACCOUNT", "data", 7,
        "6A 14 8D 45 E4 50 68 80 09 89 00 E8 ?? ?? ?? ?? 8B 03 83 C4 1C 8B CB",
        0x00890980),
    Sig("login", "PASSWORD", "data", 7,
        "6A 20 FF 50 40 50 68 98 09 89 00 E8 ?? ?? ?? ?? 8B 7D DC 83 C4 0C",
        0x00890998),
    # 兩支「用哪一種憑證」的旗標。BLOB 錨在 0x537041（帶帳號+512bytes 憑證
    # 登入）裡把它設 1 那行；TOKEN 錨在登入按鈕判斷「要不要讀 UI」那行。
    Sig("login", "FLAG_BLOB", "data", 7,
        "6A 14 8D 45 D8 C6 05 97 09 89 00 01 50 68 80 09 89 00 E8 ?? ?? ?? ??",
        0x00890997),
    Sig("login", "FLAG_TOKEN", "data", 2,
        "80 3D B9 09 89 00 00 0F 85 D1 00 00 00",
        0x008909B9),
    # 選中的角色格號。錨在「進入遊戲」的呼叫端：寫全域、再送包那兩行。
    Sig("login", "CHAR_SLOT", "data", 9,
        "8B 86 50 10 00 00 8B CE A3 E8 0B 89 00 FF B6 50 10 00 00",
        0x00890BE8),
    # 「確定分流」——點分流清單那一列時，除了寫頻道位元組還會叫這支
    # （同一個控制項 id 0x9E3 在另一種訊息下走 0x539E40 的 call）。
    # 它跳出「與伺服器連線中」並把畫面推進到選角色。thiscall、零參數。
    Sig("login", "PICK_CHANNEL_FN", "fn", None,
        "55 8B EC 83 EC 10 53 56 57 33 C0 C7 45 F0 44 73 53 00 50 83 EC 10"
        " 89 45 F8 8B FC 89 45 FC C7 45 F4 E8 FF FF FF 8D 75 F0 8B D9 6A 1E"
        " A5 8D 4B 18 A5 A5 A5 E8 ?? ?? ?? ?? 8D 8B 40 10 00 00"
        " 89 83 3C 10 00 00 E8 ?? ?? ?? ?? 5F 5E 5B C9 C3",
        0x00537353),
    # 「依 id 取控制項」。stdcall(視窗id, 控制項id)，ecx = UI 管理者。
    # 純查表、不抽訊息，所以拿來取伺服器清單控制項很安全。
    Sig("login", "GETWIDGET_FN", "fn", None,
        "55 8B EC 83 7D 08 00 53 56 57 8B F9 74 47 FF 75 08 E8 ?? ?? ?? ??"
        " 8B D8 85 DB 74 61 8B B3 C8 00 00 00 EB 27",
        0x00624D17),
    # 「授權合約已同意」旗標。==1 就整段跳過 EULA（唯一的讀在 0x539B5A、
    # 唯一的寫在 0x539BA7）。自己開 angel.dat 時先寫 1 就不會跳合約視窗。
    Sig("login", "EULA_OK", "data", 2,
        "80 3D FC 0F 89 00 01 56 8B F1 74 47 57 68 5C 6D 7D 00 E8 ?? ?? ?? ??",
        0x00890FFC),
    # 角色清單裡「這一格有沒有角色」那個欄位（0x8909DF + 格號*0xB7）。
    # 錨在遊戲自己的「進入遊戲」按鈕 0x5103E8 送包前的兩道檢查。
    Sig("login", "CHAR_HAS", "data", 8,
        "69 C0 B7 00 00 00 83 B8 DF 09 89 00 00 74 37"
        " F7 80 CF 09 89 00 00 00 00 40 75 2B",
        0x008909DF),
    # 頻道（分流）位元組，0 起算。錨在 ENTER_FN 裡「把它塞進封包 +3」那行。
    Sig("login", "CHANNEL", "data", 11,
        "6A 20 68 14 08 89 00 88 41 02 A0 10 08 89 00 88 41 03 8D 41 04 50 E8",
        0x00890810),
    # 保護密碼的 MD5（32 字十六進位）。同一段特徵的另一個立即值 ——
    # ENTER_FN 就是把它 strncpy 0x20 bytes 進封包 +4。
    Sig("login", "PROTECT_HASH", "data", 3,
        "6A 20 68 14 08 89 00 88 41 02 A0 10 08 89 00 88 41 03 8D 41 04 50 E8",
        0x00890814),
    # 應用程式主物件的全域指標（伺服器清單掛在它 +0x500/+0x504）。
    # 錨在「送選頻道那包」的函式頭（0x539C7E）—— 它整支就是讀 +0x51C 送出去。
    Sig("login", "APP_PTR", "data", 4,
        "55 8B EC A1 6C 09 89 00 83 EC 10 83 B8 1C 05 00 00 00 76 2D"
        " 6A 06 6A 11 8D 4D F0 E8",
        0x0089096C),
    # 登入連線編號（0 = 還沒連上）。⚠ 不能錨在「建完 socket 存起來」那行 ——
    # 模組裡有好幾段一樣的建連線慣用碼，全遮之後撞在一起。改錨在
    # ENTER_FN 尾巴「push 連線編號 → 送出」那兩行（那段本來就唯一）。
    Sig("login", "CONN_ID", "data", 14,
        "8D 41 04 50 E8 ?? ?? ?? ?? FF 75 FC FF 35 7C 09 89 00"
        " E8 ?? ?? ?? ?? 83 C4 14 C9 C2 04 00",
        0x0089097C),
    # 登入時選中的伺服器索引。⚠ 2026-08-11 稽核抓到的縫：它以前**沒有特徵**，
    # 是 login.py 裡唯一寫死又沒被 AOB 蓋住的位址（改版位移 −0x20 之後
    # `server_info()` 會拿別的東西當索引 —— 這正是「安靜地做錯事」）。
    # 錨在遊戲自己用它進伺服器陣列那行：`imul esi,[索引],0x178`（0x178 = 一筆
    # 伺服器記錄的大小）＋後面 `mov edx,[ecx+0x500] / cmp [edx+esi+0x5C],0`
    # —— 順便就是 SRV_STRIDE / SRV_BEGIN / SRV_SUBSET_B 三個版面常數的出處。
    Sig("login", "SERVER_INDEX", "data", 2,
        "69 35 88 0C 89 00 78 01 00 00 FF 35 F4 0B 89 00"
        " 8B 91 00 05 00 00 83 7C 32 5C 00 74 3D",
        0x00890C88),
    # 快捷欄管理物件的全域指標。錨在 usequickkey（Lua 綁定）的函式頭 ——
    # 表就掛在它 +0x609C（見 quickbar.py）。
    # 快捷欄表在管理物件裡的位置（結構偏移；2026-08-11 從 0x609C 變 0x603C）。
    # 錨在 USE_FN 裡算格子位址那兩行：`imul ecx,eax,9 / movzx eax,byte
    # [ecx+esi+表偏移] / movzx edx,word [ecx+edi+表偏移+1]`。
    # ⚠ 偏移不在模組範圍內 → 不會被 _auto_mask 遮，pattern 裡自己寫 `??`。
    Sig("quickbar", "TABLE_OFF", "off", 7,
        "6B C8 09 0F B6 84 31 ?? ?? ?? ?? 0F B7 94 39 ?? ?? ?? ??",
        0x0000603C),
    Sig("quickbar", "MGR_PTR", "data", 40,
        "55 8B EC 51 51 8B 45 08 8D 4D F8 56 6A 01 89 45 FC C6 45 F8 00"
        " E8 ?? ?? ?? ?? 6A 02 8D 4D F8 8B F0 E8 ?? ?? ?? ?? 8B 0D AC 66 9B 00"
        " 6A 00 56 50",
        0x009B66AC),
    # ★★★「目前選定的目標」在狀態物件裡的位置（見 entity.OFF_TARGET）。
    #   2026-08-11 改版從 0x2D8 搬到 0x270（−0x68），而**寫錯完全不會報錯**：
    #   施放路徑讀到 0 就走失敗分支，不出手也不扣 MP —— 掛機整個廢掉、
    #   而且我們還每 20ms 往一個不明成員寫 4 bytes。所以它一定要自動跟上。
    # 錨在快捷鍵施放分支 0x5B7565 取目標那一段：
    #     push [esi+自己ID偏移] / call 取實體 / push [edi+目標偏移]
    #     / mov ecx,esi / mov [ebp+8],eax / call ID→索引 / push eax / mov ecx,esi
    # ⚠ 兩個 disp32 都是結構偏移（不在模組範圍，`_auto_mask` 不會遮）→
    #   自己寫 `??`，只留指令骨架當錨。
    Sig("entity", "OFF_TARGET", "off", 13,
        "FF B6 ?? ?? 00 00 E8 ?? ?? ?? ?? FF B7 ?? ?? ?? ??"
        " 8B CE 89 45 08 E8 ?? ?? ?? ?? 50 8B CE",
        0x00000270),
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


def _find_all(img: bytes, sig: bytes, mask: bytes) -> list[int]:
    """所有命中的位移（給字串錨過濾用；一般情況直接看 `_find_unique`）。"""
    seed, at = _seed(sig, mask)
    if not seed:
        return []
    hits: list[int] = []
    i = img.find(seed)
    while i >= 0:
        start = i - at
        if start >= 0 and start + len(sig) <= len(img):
            if all(not mask[k] or img[start + k] == sig[k]
                   for k in range(len(sig))):
                hits.append(start)
        i = img.find(seed, i + 1)
    return hits


def str_ok(img: bytes, base: int, off: int, s: Sig) -> bool:
    """字串錨：命中處 +str_at 的指標指到的字串是不是 str_val。

    沒設 str_at 的段一律通過。指標指到模組外、或字串不吻合 → 這個命中不算。
    """
    if s.str_at is None:
        return True
    k = off + s.str_at
    if k + 4 > len(img):
        return False
    ptr = int.from_bytes(img[k:k + 4], "little") - base
    if not 0 <= ptr < len(img):
        return False
    end = ptr + len(s.str_val)
    return img[ptr:end] == s.str_val and end < len(img) and img[end] == 0


def _find_unique(img: bytes, sig: bytes, mask: bytes,
                 s: Sig | None = None, base: int = 0) -> int | None:
    """回傳唯一命中的位移；找不到或不只一個都回 None（寧可保留舊值）。

    s/base 有給的話，先用字串錨把命中過濾一輪再判斷唯一性。
    """
    hits = _find_all(img, sig, mask)
    if s is not None and s.str_at is not None:
        hits = [h for h in hits if str_ok(img, base, h, s)]
    return hits[0] if len(hits) == 1 else None


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
            # ★ 先用全遮（最抗改版）；那樣不唯一才退回「只遮目標」，
            #   讓其他內嵌位址當錨把它區分開來（見 _auto_mask）。
            #   兩層都要求**唯一命中** —— 模糊命中一律當定位失敗。
            off = _find_unique(img, sig, m_full, s, base)
            if off is None:
                off = _find_unique(img, sig, m_only, s, base)
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
                        # data 位址一定落在模組內；off 是結構偏移，只要求
                        # 非 0 且小得合理（物件再大也不會到 1MB）。
                        ok = (base <= v < base + info.size if s.kind == "data"
                              else 0 < v < 0x100000)
                        if same and ok:
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


def located(module: str, attr: str) -> bool:
    """這一段這次真的定位成功了嗎？（沒掃過、或那段失敗 → False）

    ⚠⚠ **要寫進遊戲記憶體的全域，寫之前一定要問這個。**
    讀錯位址頂多讀到垃圾（讀取端都有驗證擋著）；**寫錯位址是直接破壞遊戲的
    記憶體**。2026-08-11 實際踩到：一支沒先 `warm()` 的腳本拿舊版的
    `login.EULA_OK`（0x890FFC）去寫 1，而那裡在新版落在 Lua 全域區
    （新的 `lua.CTX_PTR` 就是 0x890FF0）—— 登入畫面的 Lua UI 當場壞掉，
    分流清單點下去不動，症狀跟位址完全扯不上關係。

    `data` 類定位失敗會**保留舊值**（那對讀取是對的取捨），所以「值看起來
    正常」完全不代表它有被驗證過 —— 只能問這裡。
    """
    name = f"{module}.{attr}"
    return any(n == name and new is not None for n, _old, new in _report)


def image_identity() -> tuple[int, int] | None:
    """最後一次 warm() 認得的映像身分 (基底, PE 表頭 4KB 的 CRC)。

    還沒掃完過回 None。給 tablestamp 比對「寫死資料表是對哪一版遊戲核對的」
    用 —— 那邊不必再讀一次記憶體。
    """
    return _img_key if _ran else None
