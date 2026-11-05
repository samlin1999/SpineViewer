# dropin

這個資料夾裡的檔案是**要裝到別的專案**去用的，不是給 spineview 自己用的。

## CLAUDE.md

給 AI agent 看的 spineview 用法說明。不要手動複製——雙擊 spineview 資料夾裡的
**`設定AI助手.bat`**、把目標專案資料夾拖進去，或用指令：

```bash
spineview init <你的專案>            # 可一次給多個
spineview init --list                # 看設定過哪些專案
spineview init --all                 # 全部更新成最新說明
```

`init` 會先看專案的慣例再決定怎麼裝，不會硬把內容塞進 `CLAUDE.md`：

| 專案長這樣 | 安裝方式 | 寫到哪裡 |
| --- | --- | --- |
| 有 `.claude/skills/` | skill | `.claude/skills/spineview/SKILL.md`（加上 `name`／`description` frontmatter），不改 `CLAUDE.md` |
| `CLAUDE.md` 只有 `@` 引用、有 `.claude/rules/` | rule | `.claude/rules/spineview.md`（不帶 frontmatter），`CLAUDE.md` 加一行 `@.claude/rules/spineview.md` |
| 其他 | claude-md | `CLAUDE.md`，用 `<!-- spineview:begin -->`／`<!-- spineview:end -->` 框起來，重跑只更新那一段 |

- 要強制某種方式：`--as skill`、`--as rule`、`--as claude-md`（`--name AGENTS.md` 只用在 claude-md）
- 改寫既有檔案時保留原本的換行格式（CRLF／LF）；讀不懂的檔案（非 UTF-8）一律跳過、不改寫
- 目標位置已有**不是 spineview 產生的**同名檔案時不覆蓋，要覆蓋加 `--force`
- 舊版一律寫進 `CLAUDE.md`；改裝成 skill／rule 時會把那段移除，舊版改掉的換行格式也一起還原
- 專案有自己的同步腳本（例如 Inanna 的 `Tools/sync-ai-config.ps1` 把 `.claude` 鏡像到 `.cursor`）時，
  只提示使用者去跑，不代跑——那支腳本會連同其他還沒準備好的檔案一起同步

設定過的專案記在 `~/.spineview_projects`（`SPINEVIEW_PROJECTS` 環境變數可改位置）。`--all` 會
重新判斷每個專案的慣例，所以專案改了結構，重跑就會跟著改裝法。

前置：要先讓 `spineview` 變成可用的指令（批次檔第一次執行時會自動做，等效於在 spineview
資料夾執行 `pip install -e .`）。說明內容刻意不寫任何絕對路徑，換機器或搬資料夾都不用改。
