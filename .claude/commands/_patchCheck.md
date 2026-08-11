# _patchCheck（改版體檢）— 遊戲更新後，自動找出哪裡壞了並修好

天使之戀官方改版之後跑這一支。使用者不必再解釋一次背景，照下面做。

## 五層，全綠才算過

| 層 | 工具 | 驗什麼 | 要不要進遊戲 |
|---|---|---|---|
| ① 位址 | `patch_doctor.py` | 特徵唯一性、模擬改版、名字交叉驗證、寫死位址稽核 | 登入畫面就夠 |
| ② 偏移 | **`verify_offsets.py`**＋`patch_doctor.py`（`kind="off"` 那幾段） | 物件版面有沒有搬家 | 要 |
| ③ 讀取 | **`tab_check.py`** | **逐分頁**驗它實際會讀的每樣東西 | 要 |
| ④ 查表 | **`recheck_tables.py`** | 寫死表 vs 記憶體；順便回答 setting 要不要重解包 | 要 |
| ⑤ 動作 | ⚠ **還沒有工具** | 真的打一隻怪／走一步 | 要，且有副作用 |

⚠⚠ **①②③④ 全綠 ≠ 功能正常。** 2026-08-11 那次 selfcheck 13/13 全綠、位址
全綠，掛機還是完全廢的 —— 因為真兇是「`usequickkey` 第三個參數從 0 變成 1」
（呼叫慣例的**語意**變了，靜態工具永遠看不到）。所以最後一定要**實際打一隻怪**
確認，或請使用者掛五分鐘回報。

---

## 0. 先確認真的改版了

```
Get-Item "D:\AngelsOnline\Angels Online Global\angel.dat" | Select LastWriteTime
Get-Content "D:\AngelsOnline\Angels Online Global\patch.log" -Tail 6
```

⚠ `patch.log` 寫 `no update needed` **不能當作沒改版** —— 要看 angel.dat 的檔案時間。
⚠ 順便看 `UPDATE.PAK` 的時間：它也變了 → 從資源包抄來的寫死表要重新核對（見第 6 步）。

## 1. 跑體檢

遊戲開著（登入畫面就夠）：

```
$env:PYTHONUTF8=1; py tools\patch_doctor.py --dump
```

`--dump` 會把映像存到 `reports/angel_image.bin`，**之後遊戲關了也能離線查**
（`--image reports\angel_image.bin`）。這很重要：診斷常常要來回好幾趟，
不要每次都叫使用者開遊戲。

主控台只印結論；全量在 `reports/patch_doctor.txt`，可貼的修法在
`reports/patch_doctor_fix.md`。

## 2. 讀懂它的四種判定

| 判定 | 意思 | 要做什麼 |
|---|---|---|
| **位移** | 特徵自己跟上了，值變了 | **正常，不用管**。改版當天大部分都長這樣 |
| **壞：沒中** | 函式被重新編譯／程式碼改寫 | 看 fix 檔的「最接近」候選＋反組譯，套新 pattern |
| **壞：模糊** | 特徵不夠獨特，撞到別人 | **先挑對是哪一個**（見下），再照建議往後延伸 |
| **⚠ 只有『只遮目標』那層唯一** | 還沒壞，但**下次改版一定壞** | 順手修掉，fix 檔有現成建議 |

★ 「模糊」最危險：`data` 類會**安靜地保留舊值**，症狀離現場很遠
（2026-08-11 就是這樣 → 自動登入卡在「等遊戲開好」）。

## 3. 「模糊」要怎麼挑對候選

報告裡每個候選都附反組譯。判斷順序：

1. **字串內容**當錨（最強）—— 附近有 `push "某字串"` 就用 `str_at`/`str_val`。
   怪物表 vs 寵物表（`Npc`/`Pet`）、技能表（`Magic`）、`lua.CTX_PTR`
   （`UpdateEquipPetNew`）都是這樣分的。字串位址會移，**內容不會**。
2. **名字定位**（Lua 綁定表）—— 體檢報告的「名字定位交叉驗證」那一段。
   遊戲的 Lua 綁定表是 `{名字, 函式}` 成對，20 支 fn 裡 8 支查得到。
3. **指令骨架**往後延伸 —— fix 檔會算好「多蓋幾 bytes 才唯一」。
   ⚠ 別延伸到跨過 `ret` 進到下一支函式（那不穩）。

⛔ **絕對不要**把要解出來的位址留在特徵裡當錨（tautology）——
那等於拿答案比對答案，`.data` 一位移就整段失敗。這條是硬規則。

## 4. 套用 → 驗證（一定要做完）

改 `app/game/locate.py` 的 SIGS。**如果新特徵的 pattern 位元組是照現在的映像
重抄的，`known` 也要一起改成現在的值**（`_auto_mask` 靠 `known` 認出「哪個
4-byte 視窗是目標」，兩者必須一致）。同一個模組裡寫死的那份退路常數
（例如 `lua.CTX_PTR = 0x...`）也一起更新。

```
py tools\patch_doctor.py --image reports\angel_image.bin   # 應該 0 壞
py tools\verify_sigs.py                                    # 遊戲開著；唯一性＋模擬改版
```

## 5. 結構偏移（版面）壞掉的話　★ 這一層最會咬人

症狀是特徵好好的、功能卻不對。這類**不是位址位移**，是官方在物件裡加減成員，
而且**壞了完全沒有錯誤訊息**：舊偏移照樣讀得到（讀到別的成員）、照樣寫得進去。

```
py tools\verify_offsets.py        # 進遊戲後跑；每個偏移用不變量驗一次
```

⚠ 掛機開著跑最好 —— 「目標欄位」那一項要有選定目標才驗得到（沒有就顯示 `–`）。

2026-08-11 那次**同一個物件裡不同成員的位移量不一樣**，不能用同一個 delta 推：

| 偏移 | 舊 → 新 | 位移 |
|---|---|---|
| `quickbar.TABLE_OFF` | 0x609C → 0x603C | −0x60 |
| `player.VT_OFF_FROM_MGR` | 0xCB68 → 0xCB08 | −0x60 |
| `robot.ROBOT_READY_OFF` | 0xF35C → 0xF2FC | −0x60 |
| `team.MEMBERS_OFF` | 0x31A0 → 0x3140 | −0x60 |
| `team.PENDING_OFF` | 0x338C → 0x332C | −0x60 |
| **`entity.OFF_TARGET`** | 0x2D8 → **0x270** | **−0x68** |
| **`energy.OFF_ENERGY`** | 0xB8 → **0x54** | **−0x64** |

⛔ 所以**不准拿別的欄位的位移量去推**，一個一個驗。

做法：找一處遊戲自己用那個偏移的程式碼，把 disp32 讀回來 →

```
py tools\find_off.py 0x31A0            # 誰在用這個偏移
py tools\find_off.py --at 0x58c70d 0x1a   # 把那一段直接印成 pattern 位元組
```

★ 找不到使用點（像 `team.MEMBERS_OFF` 舊值）本身就是強烈訊號：**它已經搬家了**。
★ 純 Lua 的介面（晶化那組）C++ 沒有錨可掛 → 改用「送一發同步請求，看**哪一格
會變**」定位，再跨五台交叉比對語意（−1 哨兵值、固定 10、因人而異的值）。
★ 定完一律加進 `locate.SIGS`（`kind="off"`）讓它下次自動跟上。
⚠ `off` 類的目標值不在模組範圍內 → `_auto_mask` 不會自動遮，**pattern 裡要
自己寫 `??`**，不然又是拿答案當錨。

## 6. 進遊戲驗「每個分頁都讀得對」（③ 讀取層）

位址對了不代表**版面**對了，更不代表分頁讀得到它要的東西。登入一台再跑：

```
py tools\tab_check.py        # 逐分頁；有幾台就驗幾台
py tools\selfcheck.py        # 13 項共用底層（tab_check 已涵蓋大半，快就順手跑）
```

`tab_check` 的對照表是用 AST 掃 `app/tabs/*_tab.py` 的相依關係列出來的
（分頁 → 它呼叫哪些讀取函式），不是憑印象寫的。目前涵蓋：

* **收益監控**：角色屬性（等級/HP/MP/經驗/金幣合理性）、經驗球分類
* **自動掛機**：座標、場景、地形圖、周圍怪＋範本表交叉比對、**目標欄位**、
  快捷欄 4 頁、技能範本＋射程表、SP、背包／金幣、身上裝備耐久、精靈設定樹、
  補給判斷、分流／伺服器名
* **自動登入**：伺服器清單、角色清單
* **分身總控**：隊伍成員、趴趴GO 表
* **能量晶化**：晶能欄位、可分解清單
* **販賣裝備**：裝備品質／耐久

⚠ 每一項都要有**能分辨對錯的證據**（範圍檢查或兩個來源交叉比對），
不是「有回值就算過」。加新檢查時照這個標準。
⚠⚠ 跨來源比對的值**一定要當場重讀**：`sp_now` 是拿「子物件的 MP 等不等於
角色屬性那份 MP」認基準的，用幾秒前的快照會因為 MP 自然回復而假失敗
（實際踩到，五台裡一台紅燈）。量測工具自己把數據弄假是老坑。

需要實際登入時：帳密在設定檔（`app.config.config` 的 `accounts`，
`config.deobfuscate()` 解），流程照 `app/game/login.py`
（`sign_in` → `pick_channel` → 等 `character()` → `enter_game`）。
⚠ 動使用者的分身前要有授權；沒說過就先問。
⚠⚠ 任何碰遊戲記憶體的腳本，**第一件事是 `locate.warm(sc)`** ——
沒 warm 就寫全域＝拿舊位址砸新版遊戲（見 memory `patch-doctor-command`）。

## 7. 查表正確 ＋ setting 要不要重新解包（④ 查表層）

**先別急著叫使用者重新解包。** `UPDATE.PAK` 換了不代表表變了；使用者的實務
經驗是**「通常只有大更新新增內容才要重做」**，而那句話現在可以**驗證**：

```
py tools\recheck_tables.py        # 要進遊戲，最後會直接給結論
```

它自己會印「setting 這次要／不必重新解包」。對帳項目：

| 項目 | 過期後果 | 對帳來源 |
|---|---|---|
| `skill_range.tsv.gz` | ⛔ 走位停太遠 → 零傷害，不報錯 | Magic 範本 +0x50/+0x54 |
| `jumpmap.tsv` | ⛔ 傳到錯的地方 | JumpMap 範本 +0x04 場景/+0x10 X/+0x14 Y |
| Item 表 ↔ 背包 | 驗表指標＋物品的種類 ID 欄位 | 每件物品都要查得到範本 |
| Npc 表 | 王／等級／滿血的來源 | 抽樣合理性＋場上的怪查得到 |
| 物品名稱 | 只影響顯示（查不到 → 顯示編號） | 只報涵蓋率，**不擋** |

* **全對** → 不必解包。`py tools\stamp_tables.py` 蓋章，掛機頁警示就熄。
* **對不上** → 才請使用者重新解包（`D:\RPGViewer` 是 GUI，我們代跑不了），
  然後 `build_item_names / build_jumpmap / build_skills / build_skill_names /
  build_skill_range` 重跑一輪再蓋章。

### ★★★ 41 張表都在記憶體裡（要寫死之前先看這個）

那 41 支查表函式共用同一句錯誤訊息 `Get %s Data Error, ID:%d >= MAX:%d`，
`%s` 帶進去的字串就是**表名**。`recheck_tables.find_tables()` 靠這個現場把
40+ 張表的指標全撈出來（不必寫死任何位址，改版自動跟上），清單印在
`reports/table_recheck.txt` 末尾：

```
Npc Magic JumpMap JumpMapClass Item OnlineGift Exchange ExchangeGroup Drop
Quest Shop Doll Stage Skill Pet Make Mall Mat Achievement Collection …
```

**在這張清單裡的資料就別抄資源包。** 要用哪張，照 `skillcost.TABLE_PTR` 的
做法進 `locate.SIGS`（錨在查表函式尾巴 push 的**表名字串**）。

## 8. ⑤ 動作層 —— 靜態工具的極限，一定要補這一刀

①~④ 全綠**還是可能整個功能是廢的**。2026-08-11 的實錄：位址全綠、
selfcheck 13/13、資料表全對，但掛機站在怪旁邊完全不出手 —— 真兇是
`usequickkey` 第三個參數從「0 可以」變成「0 一律失敗」。
**參數個數沒變、函式頭沒變、特徵完美命中**，只有真的打一隻怪才驗得出來。

目前沒有工具，要手動確認（⚠ 要先跟使用者要授權，會動到他的角色）：

```python
# 最小可信實驗：設目標 → 叫快捷鍵 → 看怪的血有沒有掉
state, me, ents, _r, _e = entity.snapshot(sc)
t = 最近的一隻怪
entity.set_target_id(sc, state, t.eid)
quickbar.use(mv, sc, slot, page)        # 回 True 只代表「送進指令槽」
# ★ 唯一算數的證據：entity.read_target_checked() 的血量%下降，或 MP 真的扣了
```

其他功能的最低驗證（都要授權，且部分有副作用）：

| 功能 | 硬證據 |
|---|---|
| 走位 | `WALK_FN` 後座標真的變 |
| 自動登入 | 登入畫面 → 進遊戲，視窗標題出現帳號 |
| 換頻道／趴趴GO | 標題分流變／場景編號變（**會把角色丟走**） |
| 賣裝備／兌換／領獎／分解 | 只能驗「送得出去、遊戲有反應」，**別真的跑**（會消耗東西） |

★ 最省事也最可靠的一招：位址層修好之後**請使用者自己掛五分鐘**回報。
他的回報（「站著不動」「不出手」）比任何自動檢查都準。

## 9. 收尾

* commit（訊息要寫清楚「哪一段、為什麼壞、怎麼修」，改版紀錄很值錢）
* 更新 memory `patch-2026-08-11` 那類的紀錄（新的改版就新開一份 `patch-YYYY-MM-DD`）
* 要發版的話走 `/_pushReleases`（⚠ 建 Release 前要先問過使用者）
