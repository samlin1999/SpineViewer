# 檢視 Spine 動畫：spineview

這個專案的 Spine 骨架動畫（`.skel`／`.json` + `.atlas` + 貼圖）可以用 `spineview` 渲染成圖片、
列出插槽與骨骼規格、檢查資源。算法照 Cocos2d-x 內附的 spine-cpp 3.8 移植，
畫出來的是**遊戲裡實際的樣子**。

## 規則（動手前先讀）

1. **圖產生後必須用 Read 讀進來。** `render`／`sheet`／`compare` 只是把 PNG 寫成檔案，
   只回報「已輸出」不算完成——你要真的看過那張圖。
2. **畫面缺東西、或懷疑資源有問題，先跑 `doctor`。** 它的 error 都對應到遊戲端真的會壞的情況：
   `missingRegion`（圖集缺圖 → 遊戲端整份骨架載入失敗）、`pageMissing`、`atlasMissing`、
   `loadError`（3.8.75 被引擎拒收、4.x 讀不了）、`pageSizeMismatch`／`regionOutOfBounds`（貼圖與圖集不同批）。
   `info` 等級不是錯誤，不要當成 bug 回報。
3. **對照遊戲畫面時要指定造型（`--skin`）。** 沒指定時工具可能自動挑第一個具名造型，
   JSON 的 `skin` 欄位會寫實際用了哪個。
4. **分清楚工具的已知限制和真正的問題**（見下表）。把已知限制當成 bug 回報，比漏報還糟。
5. **找不到 `spineview` 指令**就請使用者到 spineview 資料夾雙擊 `開啟介面.bat`（會自動安裝），
   或執行一次 `pip install -e .`。不要自行猜測工具的安裝路徑。

## 判斷準則

| 可以相信（差異即是問題） | 只是限制（差異屬正常） |
| --- | --- |
| 骨骼姿勢、附件位置與形狀、繪製順序 | 三角形邊緣有鋸齒（沒有抗鋸齒，用 `--ss 2`） |
| 混色模式、預乘 alpha、clipping | 多個動畫交叉淡入（mix）、多 track 疊加——不模擬 |
| 曲線求值（照引擎的 10 段折線近似） | tint black 預設不套用（引擎也預設不開；`--tint-black` 可看） |
| IK／Transform／Path 約束 | 事件只列出，不播音效 |

| 看到的現象 | 不要直接下的結論 | 先做這件事 |
| --- | --- | --- |
| 整張圖空白 | 「渲染器壞了」 | `doctor`；再用 `spec` 看插槽 `state`（`empty`／`alpha0`／`missingRegion`）。不少特效第 0 格本來就是空的，換中段的格子看 |
| 某個部位不見 | 「動畫做錯了」 | `spec --slot <名稱>` 看狀態；可能是造型不對（`--skin`）或骨骼 `inactive`（skin 限定骨骼） |
| 光暈顏色和遊戲不同 | 「混色錯了」 | additive／screen 的顏色取決於底下的背景，確認背景一致（`--bg`） |
| 和 Spine 編輯器看起來不同 | 「工具錯了」 | 編輯器預設開 tint black、遊戲預設不開；用 `--tint-black` 比對 |

## 指令

```
spineview <指令> <檔案.skel> [選項]
```

| 指令 | 用途 |
| --- | --- |
| `info` | 版本、fps、造型、動畫清單（長度、格數、事件） |
| `render` | 渲染單一影格 |
| `sheet` | 一段動畫平均取幾格平鋪成一張，最快看懂動作 |
| `spec` | 每個插槽的附件、狀態、混色、畫面座標（繪製順序）；`--slot <名稱>` 給完整屬性 |
| `tree` | 骨骼階層與世界座標 |
| `find` | 依名稱找骨骼／插槽／附件／動畫／事件 |
| `doctor` | 資源檢查（可給整個資料夾） |
| `compare` | 與截圖比對，指出哪個插槽畫錯 |
| `frames`／`gif`／`list` | 批次輸出、列出資料夾裡的骨架檔 |

```bash
spineview info   Hero.skel --json
spineview sheet  Hero.skel --anim attack --count 12 --json
spineview render Hero.skel --anim attack --frame 8 --bones --json
spineview spec   Hero.skel --anim attack --frame 8 --slot weapon --json
spineview doctor Hero.skel --json
```

- 想知道某個插槽的細節（設定色／目前顏色、附件參數、圖集區塊、骨骼數值、這段動畫在哪幾格換圖或變色、
  各造型可放的附件），用 `spec --slot <名稱> --json`，看 `slots[].detail`
- `--frame` 是這段動畫的第幾格（依 `fps`，從 0 起算，`-1` = 最後一格），也可用 `--time <秒>`
- 同一段動畫的每一格取景都相同（整段動畫的聯集外框），可以直接逐格比對
- 座標：`screenRect` 是圖片像素（左上原點、Y 向下）；`world` 是 Spine 世界座標（Y 向上）；
  `origin` 是世界原點在圖上的位置，也就是遊戲裡節點擺放的位置
- 所有指令支援 `--json`（stdout 乾淨、進度走 stderr）。**以 JSON 的 `image` 路徑為準，不要自己拼檔名**
- 多個 agent 同時用不會互相覆蓋（檔名含參數雜湊、原子寫入）；要完全隔離設 `SPINEVIEW_OUT=<目錄>`
- 人要自己看動畫：`spineview serve <資料夾>`（或雙擊 spineview 資料夾的 `開啟介面.bat`）

更詳細的說明見 spineview 專案裡的 `AGENT_GUIDE.md`。
