# spineview

檢視 Spine 骨架動畫（`.skel`／`.json` + `.atlas` + 貼圖）。有網頁介面給人看，也有指令列給
AI agent 用——作法沿用 [CsdViewer](../CsdViewer/)（csdview）：雙擊批次檔就能用、`--json`
輸出穩定、檔案被改動時畫面自動更新。

骨架怎麼擺、附件畫在哪、什麼顏色、怎麼混色、clipping 怎麼裁——全部照 Cocos2d-x 內附的
spine-cpp 3.8 逐行移植（包含這個專案分支額外支援的 3.5 讀檔路徑），所以算出來的是
**遊戲裡實際會出現的樣子**，不是 Spine 編輯器的樣子（兩者在 tint black 等細節上不同）。

不需要 Spine 編輯器、Cocos2d-x 或任何引擎執行檔，相依套件只有 Pillow 與 numpy。

## 給團隊使用：兩個批次檔

| 雙擊這個 | 用途 |
| --- | --- |
| **`開啟介面.bat`** | 開啟網頁檢視器，人自己看動畫 |
| **`設定AI助手.bat`** | 讓某個專案的 AI agent 學會用 spineview（第一次做一次） |

兩個都會在首次執行時自動安裝（`pip install -e .`），不需要先手動裝任何東西。

`開啟介面.bat` 第一次會請你指定資料夾——把放 Spine 檔的資料夾（或單一個 `.skel`）拖進視窗即可。
開過的資料夾會記在 `~/.spineview_ui`（保留 8 個），下次啟動可以用編號切換。
也可以直接把資料夾拖到 `開啟介面.bat` 的圖示上。

**只想看單一份 Spine**：啟動時直接按 Enter（或在清單選 `0`）就不開資料夾，到網頁上按
「📂 開啟」選那個 `.skel` 即可（見下方「開啟單一檔案」）。也可以直接把 `.skel` 拖到 `開啟介面.bat` 的圖示上。

## 網頁介面

```bash
spineview serve <資料夾>          # 或：python spineview.py serve <資料夾>
```

瀏覽器會開 <http://127.0.0.1:8780/>（埠被占用會自動往上找，並告訴你換成哪個）。

- **左側**：資料夾底下所有骨架檔，可篩選，<kbd>↑</kbd><kbd>↓</kbd> 逐一切換；右上角「📂 開啟」只選一個 `.skel` 就能看
  （不在資料夾裡也行），「⬆ 上傳」把檔案傳上來看
- **中間**：動畫畫面。滾輪縮放、拖曳平移、<kbd>F</kbd> 回到適合大小。
  滑鼠停在畫面上，左下角會顯示游標下是哪個插槽／附件與世界座標；點一下選取該插槽
- **下方**：播放、動畫、造型（skin）、時間軸（黃色刻度是事件，點了跳過去；下方的藍色細條是已經載入的影格）、
  逐格按鈕與輸入、循環、速度；「💾 存成 PNG」把目前這一格存成透明背景、原尺寸的 PNG（和 CLI 的 `render` 同一份程式畫的，
  隱藏的插槽不畫）
- **右側**：
  - 插槽：這一格的繪製順序（上＝最上層）、附件名稱、混色模式、狀態；可以用名稱篩選（插槽或附件名稱都行）。
    點眼睛隱藏該插槽，<kbd>Alt</kbd>+點眼睛＝只看這個插槽（clipping 遮罩保留，再按一次恢復）
  - 屬性：選取插槽的完整屬性（在畫面上點一下就會開；插槽列表雙擊或點「屬性 ›」也行），
    拖時間軸會跟著更新——
    - 插槽：設定色與目前顏色、dark color、混色、setup 附件、被哪個 clipping 裁切、世界外框
    - 附件：類型、所屬造型、附件色與最終顏色（骨架色 × 插槽色 × 附件色）、region 的 x/y/旋轉/縮放/尺寸、
      mesh 的頂點／三角形數、加權骨骼、linked mesh、deform 是否作用中
    - 圖集區塊：貼圖預覽（已轉正）、所在頁、位置、尺寸、裁邊與 offset
    - 骨骼：世界與區域的位置／旋轉／縮放、setup 值、繼承模式（點骨骼名稱跳到骨骼分頁）
    - 這段動畫的關鍵影格：換附件、顏色、deform、繪製順序、骨骼的各軌——點了跳到那一格
    - 這個插槽在各造型裡可以放的附件
  - 骨骼：階層樹，可以用名稱篩選；點了在畫面上標出位置與世界座標
  - 事件：這段動畫的所有事件與時間，點了跳到那一格
  - 資訊：版本、fps、圖集與貼圖、警告；圖集缺 region 會用紅字標出（遊戲端會載入失敗）

勾選項：骨骼（含世界原點紅十字＝遊戲裡節點的位置）、插槽外框、Tint black、循環、
**伺服器渲染**（見下）、自動更新；背景、縮放、播放速度。

快捷鍵（右下角「⌨ 快捷鍵」或按 <kbd>?</kbd> 看完整列表）：<kbd>空白鍵</kbd> 播放／暫停、
<kbd>←</kbd><kbd>→</kbd> 逐格（Shift 跳 10 格）、<kbd>Home</kbd>／<kbd>End</kbd> 頭尾、
<kbd>↑</kbd><kbd>↓</kbd> 上一個／下一個骨架檔、<kbd>[</kbd><kbd>]</kbd> 上一段／下一段動畫（播放中會接著播）、
<kbd>F</kbd> 或 <kbd>0</kbd> 適合大小、<kbd>1</kbd> 100%、<kbd>B</kbd> 骨骼、<kbd>Esc</kbd> 關閉訊息／取消選取。
用滑鼠操作過的按鈕和下拉選單不會留著焦點，接著按方向鍵一定是逐格，不會改到選單。

側欄的邊緣可以拖曳調整寬度（雙擊恢復）。側欄寬度、背景、播放速度、循環與各個勾選項會記在這台電腦的瀏覽器裡，
下次打開還在。

循環播放照遊戲引擎在動畫長度處繞回 0：時間＝長度的「最後一格」在循環動畫裡和第 0 格是同一個姿勢，
播放時不會出現（不然每圈接縫處會頓一格）；不循環時才會停在最後一格。

伺服器只接受這個檢視器自己的網頁發來的請求（用 `http://127.0.0.1:埠號/` 或 `http://localhost:埠號/` 開），
其他網站即使在同一台電腦的瀏覽器裡也打不進來（擋 DNS rebinding 與跨站請求）。

網址的 `#` 會記住目前的檔案、動畫、造型與影格（例如
`#file=Hero.skel&anim=attack&frame=12`），複製網址就能讓別人打開同一個畫面。

### 開啟單一檔案

按左上角 **📂 開啟**，在跳出的 Windows 開檔視窗只選 `.skel`（或 Spine 匯出的 `.json`），
圖集與貼圖會直接從它所在的資料夾讀。

- 瀏覽器的檔案選擇框不會告訴網頁檔案原本在哪個資料夾，所以這個視窗是由本機的 spineview 伺服器跳出來的；
  讀的是原檔、不必上傳，美術重新匯出時畫面會自動更新
- 也可以在篩選框貼上 `.skel` 的完整路徑按 Enter（例如檔案總管「複製路徑」的結果，前後的引號不用拿掉）；
  貼 `.atlas` 或貼圖的路徑也行，會找同資料夾的同名骨架檔
- 開過的檔案記在「📂 最近開啟」（`~/.spineview_recent`，最近 15 個），下次啟動還在；滑鼠移過去按 ✕ 從清單移除（不會刪檔）
- 開檔視窗偶爾會被瀏覽器擋在後面，按了沒反應時看一下工作列

### 上傳

檔案不在這台電腦、或想直接從別處拖進來時用上傳：按左上角 **⬆ 上傳**，一次選好 `.skel`（或 Spine 匯出的 `.json`）、
`.atlas` 和貼圖（`.png`），或直接把這些檔案、甚至整個資料夾拖進瀏覽器視窗。

- 上傳的檔案出現在列表最上面的「⬆ 上傳」群組並自動打開；圖集配對、自動更新、屬性分頁都跟一般檔案一樣
- 少了圖集或貼圖會直接說缺哪個檔（例如「少了：圖集 Hero.atlas」）
- 檔案存在這次伺服器專用的暫存資料夾，群組右邊的 ✕ 可以刪除，伺服器結束時全部刪掉；
  最多保留最近 20 次上傳
- 只收 Spine 會用到的副檔名（.skel、.json、.atlas、.png、.jpg、.webp），檔名不能跳出暫存資料夾
- 不開資料夾、只用上傳：`spineview serve --upload-only`（`開啟介面.bat` 啟動時直接按 Enter 也是）

### 畫面是怎麼算出來的

| 步驟 | 誰做 |
| --- | --- |
| 讀檔、擺姿勢、算每個附件的世界座標頂點、顏色、混色、clipping、繪製順序 | Python（與 CLI 同一段程式） |
| 把三角形畫到螢幕上 | 瀏覽器的 WebGL |

WebGL 的設定照 Cocos2d-x：貼圖載入時預乘 alpha、混色函式 normal `(ONE, 1-SRC_ALPHA)`、
additive `(ONE, ONE)`、multiply `(DST_COLOR, 1-SRC_ALPHA)`、screen `(ONE, 1-SRC_COLOR)`，
clipping 用 stencil。也就是說光柵化這一步跟遊戲一樣交給 GPU，所以能即時播放、任意縮放。

勾選 **伺服器渲染** 會改顯示 Python 端 numpy 渲染器畫的 PNG——就是 CLI `render` 給 agent 的那張圖。
兩邊對不上時可以用它判斷是哪一邊的問題。

### 自動更新

骨架、圖集、貼圖任何一個檔案被改動（美術重新匯出、agent 改檔），畫面會自動重新載入。
作法同 csdview：每 1.5 秒問一次奈秒級指紋，偵測到改動後**等檔案寫完（指紋連續兩次相同）才重載**，
避免讀到寫到一半的檔。分頁在背景時不輪詢。

檔案讀不進來時（例如用錯 Spine 版本匯出）會顯示原因、停用播放相關的控制項，但**仍會繼續盯著這個檔案**：
修好（重新匯出）後自動恢復，接回原本的動畫與造型，不用按「重新整理」。伺服器會記住這次失敗，
檔案沒變之前輪詢不會把壞檔重新解析一遍。檔案只是暫時被匯出工具或防毒鎖住、讀不到時，鎖放開就會自己再試。

重新載入時：原本在播就接著播；隱藏與選取的插槽、骨骼依**名稱**接回去（重新匯出時插槽增減或換了順序也不會指錯），
新檔裡沒有的就放掉。

## 指令列（給 AI agent）

每個子指令都支援 `--json`（緊湊輸出，`--pretty` 加縮排）。圖片一律寫成檔案、JSON 回報絕對路徑。
預設輸出到 `out/`，檔名含參數雜湊，多個 agent 並行不會互相覆蓋；`SPINEVIEW_OUT` 可改輸出根目錄。

| 指令 | 用途 |
| --- | --- |
| `info` | 版本、fps、骨骼／插槽／約束數量、造型、動畫清單（長度、格數、事件） |
| `render` | 渲染單一影格成 PNG |
| `sheet` | 一段動畫平均取幾格平鋪成一張總覽圖 |
| `frames` | 批次輸出每一格 PNG |
| `gif` | 輸出 GIF（速度為近似值） |
| `tree` | 骨骼階層與每根骨骼的世界座標 |
| `spec` | 每個插槽的附件、狀態、畫面座標（繪製順序） |
| `find` | 依名稱找骨骼／插槽／附件／動畫／事件／造型 |
| `compare` | 與參考截圖比對，指出哪個插槽畫錯 |
| `doctor` | 檢查遊戲端會載入失敗或畫錯的問題（可給整個資料夾） |
| `list` | 列出資料夾底下所有骨架檔 |

```bash
spineview info   Hero.skel
spineview render Hero.skel --anim attack --frame 12 -o out/a12.png --json
spineview sheet  Hero.skel --anim attack --count 12
spineview spec   Hero.skel --anim attack --frame 12 --json
spineview doctor D:/Game/Resource            # 整個資料夾，最後列出有 error 的檔案
```

細節見 [AGENT_GUIDE.md](AGENT_GUIDE.md)。要讓某個專案的 agent 自動知道這個工具，
跑 `設定AI助手.bat`（等效：`spineview init <專案資料夾>`），它會**依那個專案的慣例**安裝
[dropin/CLAUDE.md](dropin/CLAUDE.md) 的內容：

| 專案長這樣 | 安裝方式 |
| --- | --- |
| 有 `.claude/skills/`（例如 Inanna） | 裝成 skill：`.claude/skills/spineview/SKILL.md`，用到 Spine 時才載入，**不改 CLAUDE.md** |
| `CLAUDE.md` 只放 `@` 引用、內容在 `.claude/rules/` | 裝成 rule：`.claude/rules/spineview.md`，`CLAUDE.md` 加一行 `@` 引用 |
| 其他 | 寫進 `CLAUDE.md`，用標記框起來，重跑只更新那一段 |

舊版一律塞進 `CLAUDE.md`；重跑新版會把那段移除（連舊版改掉的換行格式一起還原）。
專案若有自己的同步腳本（Inanna 的 `Tools/sync-ai-config.ps1` 會把 `.claude` 鏡像到 `.cursor`），
spineview 只提示、不代跑。要強制某種方式用 `spineview init <專案> --as skill|rule|claude-md`。

## 當作 Python 函式庫

```python
from spineview.render import render_file

doc, result = render_file("Hero.skel", animation="attack", time=0.4)
result.image.save("attack.png")
for slot in result.slots:
    print(slot.name, slot.attachment, slot.state, slot.rect)
```

## 支援的內容

| 項目 | 狀態 |
| --- | --- |
| 檔案格式 | binary：3.8（含 3.8.99）、3.5（照引擎的舊格式路徑）。JSON：3.5～3.8 |
| 附件 | region、mesh（含加權）、linked mesh（含 inherit deform）、clipping、bounding box／path／point（不畫，可疊外框） |
| 約束 | IK（1／2 骨、softness、stretch、compress）、Transform（世界／區域、絕對／相對）、Path（全部模式） |
| 時間軸 | rotate／translate／scale／shear、attachment、color、two color、deform、draw order、IK、transform、path、event |
| 曲線 | linear、stepped、bezier（照引擎用 10 段折線近似，不是解析解） |
| 骨骼繼承 | normal、onlyTranslation、noRotationOrReflection、noScale、noScaleOrReflection |
| 繪製 | 預乘 alpha、四種混色、clipping、tint black（選用）、多頁圖集、圖集旋轉與裁邊還原 |
| 造型 | 單一造型或用 `+` 組合多個（Skin::addSkin） |

本機 Mangan_git 底下 1106 個骨架檔（1098 個 `.skel` 與 8 個 JSON）全部能讀、能擺姿勢、能渲染
（`tests/test_corpus.py`）。

## 已知限制

- **只模擬單一動畫從 setup pose 開始播**（遊戲裡 track 0 播第一段動畫的情況）。
  多個 track 疊加、動畫之間的交叉淡入（mix）不模擬。
- 遊戲引擎預設**不開 two color tint**，所以 dark color 預設不套用；勾 Tint black（CLI `--tint-black`）
  看開啟後的樣子。`doctor` 會列出設了 dark color 的插槽（`darkColorIgnored`）。
- Spine 4.x 的檔案讀不進來——遊戲用的 3.8 runtime 一樣讀不進來，`doctor` 會明講。
- numpy 渲染器沒有抗鋸齒（三角形邊緣是硬邊），要平滑邊緣用 `--ss 2`。貼圖用雙線性取樣、
  沒有 mipmap，跟遊戲相同。
- 事件只列出、不播放音效。
- 沒指定造型時：default 造型能放的附件太少（角色本體都在具名造型裡）時，會改用第一個具名造型，
  JSON 的 `skin` 欄位一律回報實際用了哪個。要對照遊戲畫面時請用 `--skin` 指定遊戲裡用的那個。

## 效能與並行

- **網頁**：每 20 格一個請求，伺服器只算姿勢與頂點（重複使用同一個骨架物件，不每格重建），
  三角形交給 WebGL 畫。整段動畫的取景外框同時只算一次、結果快取。
- **numpy 渲染器**（CLI、伺服器渲染）：大畫布切成橫條平行畫；被 clipping 遮掉的像素、
  貼圖四個取樣點都是全透明的像素直接跳過。這些都不改變結果——`tests/golden.py shortcut`
  拿「單執行緒、不走捷徑」的參考版本逐像素比對過。`--threads 1` 可強制單執行緒。
- 實測（LightStick 411 骨骼／394 插槽、SVIPUpgrade 大量 mesh）：

  | | 優化前 | 優化後 |
  | --- | --- | --- |
  | LightStick 渲染 0.5x | 563 ms | 約 400 ms |
  | SVIPUpgrade 渲染 0.5x／1x | 2084／7708 ms | 約 1000／3800 ms |
  | 整段取景（LightStick） | 699 ms | 約 400 ms |
  | 網頁每 20 格（LightStick／SVIPUpgrade） | 186／64 ms | 111／40 ms |

### 執行緒安全的規則

伺服器每個請求一個執行緒，CLI 的批次指令也會平行渲染，改程式時請遵守：

- 快取一律走 `cache.memoize()` 或有鎖的結構；**快取鍵不用 `id()`**（物件回收後 id 會重複）。
  `id()` 只能當「同一個物件還活著」時的查表鍵，而且要像 `attachment_ids` 一樣存物件本身再比對 `is`
- **版本用「讀取前」的指紋**（`Document.read_stamp`／`doc.version`），不要在回應時重新問檔案系統：
  拿到 doc 之後檔案若被換掉，重新問到的是新指紋、內容卻是舊的，舊內容會被標成新版本快取起來
- 可長期快取的 API（meshes、texture、region）網址帶版本；版本對不上回 409，網頁收到就重新載入
- 同一個檔案同時只解析一次、同一個取景同時只算一次（single-flight）
- 骨架物件（`Skeleton`）不跨執行緒共用：每個請求／每個執行緒自己 `new_skeleton()`
- 寫檔一律「暫存檔 → `replace_with_retry`」；會被多個行程改的檔（`~/.spineview_projects`、
  `~/.spineview_recent`）在 `file_lock` 裡「讀最新的 → 改一筆 → 寫回」

## 圖集怎麼找

1. `--atlas` 指定
2. 同資料夾、同主檔名的 `.atlas`（`Hero.skel` → `Hero.atlas`）
3. 同資料夾只有一個 `.atlas`
4. 有好幾個：挑涵蓋最多附件的那個，並在警告中註明是猜的

## 測試

```bash
python tests/test_corpus.py <資料夾> [--render]     # 每個骨架檔都讀得進來、擺得出姿勢
python tests/test_install.py                       # init 依專案慣例安裝、不弄壞別人的檔案
python tests/test_races.py <骨架檔路徑>              # 把修過的競態實際觸發一次（含注入舊行為也抓得到）
python tests/golden.py capture <基準.pkl> <骨架檔...> # 改渲染或姿勢計算前先存基準
python tests/golden.py compare <基準.pkl> <骨架檔...> # 改完逐項比對（幾何、顏色、取景、逐像素）
python tests/golden.py shortcut <骨架檔...>          # 渲染捷徑與平行化的結果和參考版本逐像素相同
node   tests/test_ui.js <資料夾> <骨架檔相對路徑>    # 用無頭 Chrome 實際操作網頁介面
node   tests/test_upload.js <骨架檔路徑>               # 上傳：完整上傳、缺圖集、惡意檔名、刪除
node   tests/test_open.js <骨架檔路徑>                 # 開啟：只給 .skel、自動更新、最近開啟
node   tests/test_ux.js <資料夾> <骨架檔相對路徑>    # 快捷鍵、焦點、篩選、只看單一插槽、側欄寬度、記住設定、存成 PNG
python tests/test_opened.py                        # 開啟的伺服器端邏輯（用假的 tkinter，不跳視窗）
```

`test_ui.js` 透過 DevTools Protocol 操作真正的頁面：載入、確認 WebGL 畫布上確實有畫出內容、
播放、隱藏插槽、切換動畫、切伺服器渲染，最後確認 console 沒有任何錯誤。需要 Node 22+ 與 Chrome／Edge。
`test_ux.js` 全部用真的滑鼠與鍵盤事件操作（不直接呼叫頁面裡的函式）。

瀏覽器測試請**一次只跑一個**：每個都會開一個用 CPU 算 WebGL 的無頭 Chrome 和一個伺服器，同時跑好幾個可能把電腦拖到當住。
