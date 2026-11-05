"""把 agent 說明裝進別的專案（`spineview init`／設定AI助手.bat）。

不同專案放 agent 說明的方式不一樣，硬把一大段文字塞進 CLAUDE.md 會違反人家的規矩。
所以先看專案的慣例再決定怎麼裝：

| 模式 | 什麼時候用 | 寫到哪裡 |
| --- | --- | --- |
| skill | 專案有 `.claude/skills/` | `.claude/skills/spineview/SKILL.md`，用到時才載入，不碰 CLAUDE.md |
| rule | CLAUDE.md 只有 `@` 引用、規則放在 `.claude/rules/` | `.claude/rules/spineview.md`，CLAUDE.md 加一行 `@` 引用 |
| claude-md | 都不是（一般專案） | 直接寫進 CLAUDE.md，用標記框起來，重跑只更新那一段 |

舊版一律用 claude-md 模式；改用 skill／rule 時會把 CLAUDE.md 裡那一段舊的移除。

規則：
- 改寫既有檔案時保留原本的換行格式（CRLF／LF）。舊版會把整份檔案改成 LF，
  移除舊段落時改用同專案其他檔案的換行格式，把它還原回去。
- 不是 spineview 產生的檔案（沒有產生標記）不覆蓋，除非 --force。
- 專案有自己的同步腳本（例如 Inanna 的 Tools/sync-ai-config.ps1 把 .claude 鏡像到 .cursor）
  時只提示使用者去跑，不代跑——那支腳本會一併同步別人還沒準備好的檔案。
"""
from __future__ import annotations

import glob
import os
import tempfile

from .locking import replace_with_retry

MARK_BEGIN = "<!-- spineview:begin -->"
MARK_END = "<!-- spineview:end -->"
GENERATED = "<!-- 由 spineview init 產生，重跑會整份覆蓋；要改內容請改 spineview 的 dropin/CLAUDE.md -->"
SKILL_NAME = "spineview"
RULE_FILE = "spineview.md"
MODES = ("auto", "skill", "rule", "claude-md")

SKILL_DESCRIPTION = (
    "用 spineview 檢視、渲染與檢查 Spine 骨架動畫（.skel／Spine .json + .atlas + 貼圖），"
    "算法照 Cocos2d-x 內附的 spine-cpp 3.8，畫出遊戲裡實際的樣子。"
    "使用者提到 Spine、骨架動畫、.skel、.atlas、看某段 Spine 動作長怎樣、Spine 缺圖、"
    "Spine 載入失敗（Spine Create Failed、Error reading attachment）、插槽、骨骼、skin、"
    "Spine 事件時間點、比對 Spine 截圖時使用。"
)


# ---------------------------------------------------------------- 檔案讀寫（保留換行格式）

def _read(path: str) -> tuple[str, str]:
    """回傳 (以 \\n 換行的內容, 檔案原本的換行字元)。"""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        raw = fh.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def _write(path: str, text: str, newline: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    data = text.replace("\r\n", "\n")
    if newline != "\n":
        data = data.replace("\n", newline)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".spineview-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        # Windows：CLAUDE.md 正被編輯器或 agent 讀著時換檔會暫時失敗，重試幾次
        replace_with_retry(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise


def _project_newline(directory: str, *patterns: str) -> str:
    """同專案其他檔案用的換行字元：多個樣本取多數。

    不能只看第一個：Inanna 的 .claude/rules 裡排第一的 CLAUDE.md 是 csdview 寫的 LF 檔，
    其餘六個都是 CRLF，只看第一個就會猜錯。
    """
    crlf = lf = 0
    seen = set()
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.join(directory, pattern))):
            if path in seen or len(seen) >= 20:
                continue
            seen.add(path)
            try:
                with open(path, "rb") as fh:
                    head = fh.read(4096)
            except OSError:
                continue
            if b"\r\n" in head:
                crlf += 1
            elif b"\n" in head:
                lf += 1
    return "\r\n" if crlf > lf else "\n"


def _strip_block(text: str) -> tuple[str, bool]:
    """移除舊版寫進去的 spineview 段落。"""
    if MARK_BEGIN not in text or MARK_END not in text:
        return text, False
    head, rest = text.split(MARK_BEGIN, 1)
    tail = rest.split(MARK_END, 1)[1]
    head = head.rstrip()
    tail = tail.strip("\n")
    out = head
    if tail.strip():
        out = (out + "\n\n" if out else "") + tail
    return (out + "\n") if out else "", True


def _imports_only(text: str) -> bool:
    """CLAUDE.md 是不是只放 `@` 引用（Inanna 這種把內容全拆到 .claude/rules 的寫法）。"""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    lines = [ln for ln in lines if not (ln.startswith("<!--") and ln.endswith("-->"))]
    return bool(lines) and all(ln.startswith("@") for ln in lines)


# ---------------------------------------------------------------- 判斷與安裝

def detect_mode(directory: str) -> tuple[str, str]:
    claude_dir = os.path.join(directory, ".claude")
    if os.path.isdir(os.path.join(claude_dir, "skills")):
        return "skill", "專案有 .claude/skills/，裝成 skill（用到時才載入，不改 CLAUDE.md）"
    claude_md = os.path.join(directory, "CLAUDE.md")
    if os.path.isdir(os.path.join(claude_dir, "rules")) and os.path.isfile(claude_md):
        text, _ = _read(claude_md)
        text, _ = _strip_block(text)
        if _imports_only(text):
            return "rule", ("CLAUDE.md 只放 @ 引用、內容放在 .claude/rules/，"
                            "裝成 rule 並在 CLAUDE.md 加一行 @ 引用")
    return "claude-md", "一般專案，直接寫進 CLAUDE.md（用標記框起來，重跑只更新那一段）"


def _sync_hint(directory: str, mode: str) -> list[str]:
    script = os.path.join(directory, "Tools", "sync-ai-config.ps1")
    if os.path.isfile(script):
        hints = []
        if mode == "rule":
            hints.append("在 Tools/sync-ai-config.ps1 的 $FrontMatter 補一筆 'spineview' 的 description"
                         "（沒補會用檔名當描述）")
        hints.append("這個專案用 Tools/sync-ai-config.ps1 把 .claude 同步到 .cursor，請自己執行一次"
                     "（spineview 不代跑，因為它會連同其他尚未進版控的檔案一起同步）：\n"
                     "      powershell -NoProfile -ExecutionPolicy Bypass -File Tools\\sync-ai-config.ps1 -Check")
        return hints
    if os.path.isdir(os.path.join(directory, ".cursor")):
        return ["這個專案也有 .cursor 設定；若 .cursor 是由 .claude 同步產生的，請用專案自己的方式同步，"
                "不要手動複製。"]
    return []


def _owned(path: str) -> bool:
    try:
        text, _ = _read(path)
    except OSError:
        return False
    return GENERATED in text or MARK_BEGIN in text


def install(source: str, directory: str, mode: str = "auto", name: str = "CLAUDE.md",
            force: bool = False) -> dict:
    """裝一個專案。回傳做了哪些事，給 CLI 顯示與 JSON 輸出。"""
    directory = os.path.abspath(directory)
    with open(source, encoding="utf-8") as fh:
        body = fh.read().replace("\r\n", "\n").strip()
    reason = "使用者指定"
    if mode == "auto":
        mode, reason = detect_mode(directory)
    result = {"directory": directory, "mode": mode, "reason": reason, "actions": [],
              "followUp": [], "skipped": None}
    actions = result["actions"]

    if mode == "skill":
        target = os.path.join(directory, ".claude", "skills", SKILL_NAME, "SKILL.md")
        if os.path.isfile(target) and not force and not _owned(target):
            result["skipped"] = f"{target} 已存在且不是 spineview 產生的，未覆蓋（要覆蓋加 --force）"
            return result
        newline = _project_newline(directory, ".claude/skills/*/SKILL.md", ".claude/rules/*.md")
        text = (f"---\nname: {SKILL_NAME}\ndescription: {SKILL_DESCRIPTION}\n---\n\n"
                f"{GENERATED}\n\n{body}\n")
        verb = "更新" if os.path.isfile(target) else "建立"
        _write(target, text, newline)
        actions.append([verb, target])
    elif mode == "rule":
        target = os.path.join(directory, ".claude", "rules", RULE_FILE)
        if os.path.isfile(target) and not force and not _owned(target):
            result["skipped"] = f"{target} 已存在且不是 spineview 產生的，未覆蓋（要覆蓋加 --force）"
            return result
        newline = _project_newline(directory, ".claude/rules/*.md")
        verb = "更新" if os.path.isfile(target) else "建立"
        # Inanna 的規則主檔不帶 frontmatter（frontmatter 由同步腳本加）
        _write(target, f"{GENERATED}\n\n{body}\n", newline)
        actions.append([verb, target])
    elif mode != "claude-md":
        raise ValueError(f"不認得的安裝模式 {mode!r}，可用：{', '.join(MODES)}")

    claude_md = os.path.join(directory, name if mode == "claude-md" else "CLAUDE.md")
    if mode == "claude-md":
        block = MARK_BEGIN + "\n\n" + body + "\n\n" + MARK_END + "\n"
        if os.path.isfile(claude_md):
            text, newline = _read(claude_md)
            if MARK_BEGIN in text and MARK_END in text:
                head = text.split(MARK_BEGIN)[0]
                tail = text.split(MARK_END, 1)[1]
                merged, verb = head + block + tail.lstrip("\n"), "更新"
            else:
                merged, verb = text.rstrip() + "\n\n" + block, "附加"
        else:
            merged, newline, verb = block, _project_newline(directory, "*.md"), "建立"
        _write(claude_md, merged, newline)
        actions.append([verb, claude_md])
    elif os.path.isfile(claude_md):
        text, newline = _read(claude_md)
        changed = False
        text, removed = _strip_block(text)
        if removed:
            changed = True
            # 舊版寫入時把整份檔案改成了 LF；用同專案其他檔案的換行格式還原
            newline = _project_newline(directory, ".claude/rules/*.md", ".claude/skills/*/SKILL.md",
                                       "*.md")
            actions.append(["移除舊段落", claude_md])
        if mode == "rule":
            ref = f"@.claude/rules/{RULE_FILE}"
            lines = text.split("\n")
            if ref not in (ln.strip() for ln in lines):
                last = max((i for i, ln in enumerate(lines) if ln.strip().startswith("@")), default=-1)
                if last >= 0:
                    lines.insert(last + 1, ref)
                else:
                    lines = [ref, ""] + lines
                text = "\n".join(lines)
                if not text.endswith("\n"):
                    text += "\n"
                changed = True
                actions.append(["加入引用", f"{claude_md}（{ref}）"])
        if changed:
            if not text.strip():
                os.remove(claude_md)          # 只剩舊段落的 CLAUDE.md 是 spineview 自己建的
                actions.append(["刪除（只剩舊段落）", claude_md])
            else:
                _write(claude_md, text, newline)
    result["followUp"] = _sync_hint(directory, mode)
    return result


def describe(result: dict) -> list[str]:
    lines = [f"  {result['directory']}", f"    方式：{result['mode']}（{result['reason']}）"]
    if result["skipped"]:
        lines.append(f"    略過：{result['skipped']}")
    for verb, path in result["actions"]:
        lines.append(f"    {verb}  {path}")
    for hint in result["followUp"]:
        lines.append(f"    → {hint}")
    return lines
