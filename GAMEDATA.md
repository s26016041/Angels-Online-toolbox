# GAMEDATA — 解開的遊戲資源包說明

> 來源：`D:\RPGViewer\天使之戀封包解開`（把遊戲的 `.pak` 完整解開的資源，
> `.pak` 本身是加密的，見 memory `pak-and-item-id-dead-ends`）。
> **建議把這包放進專案叫 `GAMEDATA/`**：目前的 `GAMEDATA/setting/` 只是它 `setting/` 子夾的
> 部分抽取；這包多了 **`map/`（NPC 擺放座標）** 與 **`script/`（遊戲 Lua 邏輯）**。
>
> ⚠ 這份是「目前已確認」的檔案用途，後續有查證就補。**先查這裡，不要每次從頭讀。**

## 抓數值鐵則對照

依專案 CLAUDE.md：**優先讀執行時記憶體**；記憶體沒有、確認過才抄資源包，且要用
`tools/build_*.py` 自動抽、登記到 memory `items-table-maintenance`、蓋 `table_stamp`。
資源包只當「記憶體查不到」的來源與交叉驗證。

---

## 資料夾總覽

| 資料夾 | 是什麼 |
|---|---|
| `setting/` | 遊戲設定與資料表（= 目前專案的 `GAMEDATA/setting/`，這裡是完整版） |
| `map/` | **地圖檔 `MAP<場景>.MPC`：地形 + NPC/物件擺放座標**（★本次關鍵發現） |
| `script/` | 遊戲邏輯 Lua：`*.L`＝bytecode（讀不到源碼）、`*.so`＝編譯模組 |
| `shape/` `bmp/` `brush/` `font/` | 美術素材（sprite/圖/字型） |
| `sound/` `music/` | 音效音樂 |
| `make/` | 製作/配方相關素材 |

---

## setting/（資料表；名字在 big5/string/str_*.xml，編號 = 內容編號 + 前綴）

| 檔案 | 內容 | 關鍵欄位/備註 |
|---|---|---|
| `base/npc.xml` | **NPC 範本** | `編號`→`圖號`(外觀=記憶體實體 +0xB4)、等級、陣營、戰鬥屬性 |
| `shop.xml` | **商店販售清單** | `<商店 編號=N>` → item1..itemN 物品id。**★商店 35 = 補給店（唯一賣天使之翼 1905）** |
| `base/item*.xml`(item.xml~item9) | 物品定義 | 名字在 str_item*.xml；種類id + 各種欄位 |
| `base/magic.xml` | 技能/魔法表（射程/範圍/MP…） | 見 memory `skill-template-table` |
| `base/make.xml` | 製作配方 | 見 produce |
| `base/exchange.xml` | 兌換 | |
| `base/JumpMap.xml` / `JumpMapClass.xml` | 趴趴GO 傳送點（含 x,y 與分類） | jumpmap.py 用 |
| `base/monster.xml` | 怪物範本 | |
| `big5/string/str_npc.xml` | **NPC 名字** | `編號` = NPC編號 **+ 1200000000**（例：1878→1200001878「藥水雜貨商人」） |
| `big5/string/str_item*.xml` | 物品名字 | 編號 = 物品id + 1140000000（見 build_item_names.py） |
| `npc.obd` / `mapobj.obd` / `m01~m03.obd` | NPC/地圖物件的 **sprite 視覺定義**（[OBJECT] 段：Sequence=圖號、動畫） | ⚠ 只是外觀，**沒有座標、沒有 NPC↔商店綁定** |

---

## map/MAP&lt;場景編號&gt;.MPC — ★NPC 擺放座標（本次破解）

二進位，`MAP\0` 開頭，含地形 + 物件擺放段。每筆 NPC 記錄（2026-08-14 用邱比特1862、
藥水雜貨商人1878 校準）：

```
圖號  @ 編號位址 − 24  (u32 高 16 位)
X     @ 編號位址 − 20  (16.16 定點；高字 ÷ 32 = tile_x)   ✅實測邱比特214、商人92
Y     @ 編號位址 − 16  (16.16 定點；高字 ÷ 32 = rawY)
編號  @ 編號位址        (u32；對到 npc.xml / str_npc)
```

⚠ **Y 是上下翻的**：`tile_y = 地圖高度 − rawY`（地圖高度＝地形圖 Grid.h，永夜城=200）。
  邱比特 200−151.7=48✓、藥水雜貨商人 200−138.5=61.5✓（跟記憶體量的一致）。

抽法：掃全檔，找 u32 == 有效 NPC 編號、且其 −24 的高字 ∈ 有效圖號集，就是一筆 NPC。
✅ 已做成 **`tools/build_supply_merchants.py`**：抽各圖「藥水雜貨商人」→ `assets/supply_merchants.json`
   （補給用）。要抽別種 NPC（修裝「維修奴隸」、銀行「銀行小姐」）照這支改名字即可。

### 永夜城(26) 幾個重點 NPC（抽出來的）
- **藥水雜貨商人 1878**  X≈92, tile_y≈61 —— **補給商（賣天使之翼，商店35）**
- **維修奴隸 1683**    X≈65, rawY≈128 —— 修裝 NPC
- **銀行小姐 1684/1889/1890** X≈130~140 —— 銀行 NPC

---

## script/（遊戲邏輯 Lua）

`*.L` 是 **Lua bytecode**（不是源碼，讀不到邏輯，但字串/DATAID 撈得到）；`*.so` 是編譯模組。
相關的：
- `AUTOSUPPLY.L` / `autosupply.so` — **官方補給**（我們要取代的那塊）
- `sale.l` — NPC 買賣
- `bank.l` — 銀行
- `AUTOFIGHT.L` `AUTOPET.L` `AUTOOTHER.L` `farm.l` `make.l` … — 各系統

---

## 相關 memory
`self-supply-buy`（自寫補給全流程）、`game-loaded-tables`、`items-table-maintenance`、
`pak-and-item-id-dead-ends`、`jumpmap-teleport`、`skill-template-table`。
