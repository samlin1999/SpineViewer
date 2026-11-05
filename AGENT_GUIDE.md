# spineview — AI agent 使用指南

這份文件寫給會呼叫 spineview 的 AI agent。常見情境：看懂一份 Spine 動畫在做什麼、
確認美術資源能不能在遊戲裡正常載入、把 Spine 動畫移植到別的引擎後比對畫面。

所有指令都支援 `--json`：stdout 是乾淨的 JSON，進度與提醒（例如 `--frame` 超出範圍被夾到最後一格）走 stderr；
出錯時 stdout 是空的、原因寫在 stderr、結束碼不是 0。圖片一律寫成檔案，
**請一律使用 JSON 回報的 `image` 路徑，不要自己組檔名**（預設檔名含參數雜湊，
不同選項一定是不同檔案；設 `SPINEVIEW_OUT=<目錄>` 可完全隔離）。

---

## 1. 先看懂這個檔案有什麼

```bash
spineview info <檔案.skel> --json
```

- `animations[]`：每段動畫的 `name`、`duration`（秒）、`frames`（格數，含第 0 格）、`events[]`
- `fps`：影格率。檔案有記錄就用檔案的，沒有就是 30（`fpsFromFile` 告訴你是哪一種）
- `skins[]`：造型與各自的附件數量
- `skinUsedByDefault`：沒給 `--skin` 時實際會用哪個造型（`null` = 只有 default）
- `constraints`：IK／Transform／Path 數量
- `missingRegions`：圖集缺幾個 region——**大於 0 代表遊戲端整份骨架會載入失敗**，先跑 `doctor`

## 2. 看畫面

```bash
spineview render <檔案> --anim <動畫> --frame <N> --json     # 單一影格
spineview sheet  <檔案> --anim <動畫> --count 12 --json      # 一張圖看完整段動作（最快）
```

- `--frame` 是「這段動畫的第幾格」（依 `fps`，從 0 起算，`-1` 是最後一格）；也可用 `--time <秒>`。
- 不給 `--anim` 就是 setup pose。
- `--skin <名稱>` 指定造型；多個造型用 `+` 組合（`--skin body+hat`）。
- `--bones` 疊上骨骼與世界原點（紅十字），`--annotate` 疊上每個插槽的外框與名稱。
- `--hide <插槽>`／`--only <插槽>` 可重複，用來單獨檢視某些部位。

**取景**：有 `--anim` 時預設取「整段動畫所有影格的聯集外框」，所以同一段動畫的每一格
圖片尺寸與原點都一樣、可以直接逐格比對。`--bounds frame` 只看這一格、`--bounds setup`
用 setup pose、`--viewport x,y,w,h` 直接指定世界座標範圍。被 clipping 裁掉看不到的部分不算進取景。

**看圖之後一定要用 Read 讀進來**——指令只是把 PNG 寫成檔案。

## 3. 座標

| 欄位 | 座標系 |
| --- | --- |
| `screenRect` | 圖片像素，原點左上、Y 向下（與截圖一致） |
| `worldBounds`、`world` | Spine 世界座標，原點是骨架原點、Y 向上（與 Spine 編輯器一致） |
| `origin` | 世界原點 (0,0) 在圖片上的位置 = **遊戲裡 SkeletonAnimation 節點擺放的位置** |
| `scale` | 圖片像素／世界單位；`clamped: true` 代表畫布太大被自動降尺度過 |

換算：`screenX = (worldX - viewport[0]) * scale + pad`，`screenY = (viewport[3] - worldY) * scale + pad`。

## 4. 插槽與骨骼的規格

```bash
spineview spec <檔案> --anim <動畫> --frame <N> --json          # 這一格每個插槽
spineview spec <檔案> --anim <動畫> --frame <N> --slot head --json
spineview tree <檔案> --anim <動畫> --frame <N> --depth 2       # 骨骼階層與世界座標
spineview find <檔案> --name eye --json                         # 依名稱找
```

`spec` 的 `slots[]` 依**繪製順序**排列（`order` 小的先畫、在下層），每筆：

| 欄位 | 意義 |
| --- | --- |
| `attachment`／`attachmentType` | 這一格掛的附件與類型（region／mesh／clipping…） |
| `state` | `drawn` 有畫、`empty` 沒有附件、`alpha0` 透明、`hidden` 被 `--hide`、`inactive` 骨骼未啟用（skin 限定骨骼）、`missingRegion` 圖集缺圖、`clipping` 遮罩本身、`nonVisual` bounding box 等不畫的附件 |
| `blend` | normal／additive／multiply／screen |
| `color` | 只在不是純白時出現（插槽色 × 附件色前的插槽色） |
| `clippedBy` | 被哪個 clipping 插槽裁切 |

整份很長，知道要找什麼就用 `--slot` 或 `find`，不要整份倒出來。

**`--slot <名稱>` 會輸出該插槽的完整屬性**（JSON 在 `slots[].detail`，名稱可給部分名稱或索引）：

| 區塊 | 內容 |
| --- | --- |
| `slot` | `setupColor`／`color`、`setupDarkColor`／`darkColor`、`blend`、`setupAttachment`、`drawOrder`、`clippedBy`、`worldBounds` |
| `attachment` | 類型、`skin`（哪個造型提供）與 `skinKey`（造型裡的登記名）、`color`、`finalColor`（骨架色 × 插槽色 × 附件色）；region 的 `x/y/rotation/scaleX/scaleY/width/height`；mesh 的 `vertices/triangles/hullVertices`、`weightedBones`、`linkedMesh`、`deform` |
| `region` | 圖集區塊：`page`、`xy`、`size`、`orig`、`offset`、`rotate`、`trimmed`；缺圖時 `missing: true` |
| `bone` | `world`／`local`／`setup` 的位置、旋轉、縮放，`transform`、`length`、`active` |
| `keyframes` | 這段動畫裡動到它的關鍵影格（`attachment`、`color`、`twoColor`、`deform`、`drawOrder`、`bone.*`），每筆有 `frame` 與 `time` |
| `availableAttachments` | 這個插槽在各造型裡能放的附件，`current` 標出目前這個 |
| `animationsTouching` | 所有會動到這個插槽的動畫 |

例：「某個部位在第幾格換圖」看 `keyframes.attachment`；「為什麼這塊顏色比較暗」看 `color`／`finalColor`；
「這張圖在圖集哪裡」看 `region`。

## 5. 資源檢查（`doctor`）

```bash
spineview doctor <檔案> --json
spineview doctor <資料夾> --json        # 整批，summary 會列出有 error 的檔案
```

嚴重度的定義很嚴格，依據都是 Cocos2d-x 內附 spine runtime 的實際行為：

| 嚴重度 | kind | 遊戲端會怎樣 |
| --- | --- | --- |
| error | `missingRegion` | 附件要的圖不在圖集裡 → AtlasAttachmentLoader 回傳空 → **整份骨架載入失敗** |
| error | `pageMissing` | 圖集指向的貼圖檔不存在 |
| error | `atlasMissing` | 找不到圖集 |
| error | `loadError` | 讀不進來（例如 3.8.75 會被引擎拒收、4.x 引擎讀不了） |
| error | `pageSizeMismatch`／`regionOutOfBounds` | 貼圖與圖集不是同一次匯出的，畫出來會錯位 |
| warning | `caseMismatch` | 檔名大小寫與磁碟不同：Windows 正常，Android／iOS 找不到 |
| warning | `atlasGuessed` | 同資料夾多個圖集，請確認程式載入的是哪一個 |
| info | `darkColorIgnored` | 設了 tint black，但引擎預設不開 two color tint，遊戲裡看不到 |
| info | `unusedRegions` | 圖集裡有沒用到的圖（只是浪費貼圖空間） |
| info | `zeroDuration` | 長度 0 的動畫 |
| info | `pageScaled` | 貼圖被等比縮放過（UV 仍正確） |

**畫面缺東西時先跑 doctor**，多半是資源問題，不是渲染錯誤。

## 6. 與截圖比對（`compare`）

```bash
spineview compare <檔案> <截圖.png> --anim <動畫> --frame <N> --json
```

方法沿用 csdview：先讀 **`hints[]`**（依實際數字算出的判讀建議，前提失效時會直接停止）。

| 欄位 | 意義 |
| --- | --- |
| `verdict` | `match`／`shifted`／`close`／`different` |
| `shiftToAlignReference` | 截圖要平移多少才對齊——整張差一個偏移是最常見的移植錯 |
| `afterShiftCompensation.worstRegions[]` | 扣掉位移後差異最大的插槽 |
| `worstRegions[].kind` | `content` 要查、`additive` 光暈類（顏色取決於底下畫了什麼）、`clipped` 被遮罩的 |
| `referenceAspectDelta` | 長寬比落差，> 0.02 代表取景不同，其餘數字不可信 |

截圖背景要和 `--diff-bg`（預設黑色）一致。取景要一致：用 `render --json` 回報的 `viewport`
與 `origin` 對齊另一邊的畫面。

## 7. 批次

```bash
spineview list <資料夾> --detail --json
spineview frames <檔案> --anim <動畫> --step 2
spineview gif <檔案> --anim <動畫>
```

`frames`／`gif`／`sheet` 會並行渲染並把進度寫到 stderr——看到進度代表還在跑。
GIF 的播放速度是近似值（延遲單位 1/100 秒），逐格比對請用 `frames` 或 `sheet`。

---

## 注意事項

- 只模擬**單一動畫從 setup pose 播放**；多 track 疊加與動畫間的 mix 不模擬。
- 引擎預設不套用 dark color，spineview 也一樣；要看開啟 two color tint 的樣子用 `--tint-black`。
- 沒給 `--skin` 時，若 default 造型能放的附件太少，會改用第一個具名造型（JSON 的 `skin` 欄位會寫）。
  **對照遊戲畫面時請指定遊戲裡實際用的造型。**
- numpy 渲染器沒有抗鋸齒；邊緣鋸齒不是錯誤，要平滑用 `--ss 2`。
- 退化的骨架（長度 0 的骨骼配 IK 等）會讓某些附件座標變成 NaN 而畫不出來——引擎也一樣。
