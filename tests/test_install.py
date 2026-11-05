"""`spineview init` 的回歸測試：依專案慣例選安裝方式，而且不弄壞別人的檔案。

    python tests/test_install.py

全部在暫存資料夾裡做，不碰任何真的專案。涵蓋：
1. 有 .claude/skills → 裝成 skill，CLAUDE.md 不動
2. 舊版塞進 CLAUDE.md 的段落（而且舊版把整份改成 LF）→ 移除，並依同專案多數檔案還原成 CRLF，
   結果要和原檔逐位元組相同
3. CLAUDE.md 只有 @ 引用 + .claude/rules → 裝成 rule、加一行引用，重跑不重複
4. 一般專案 → 寫進 CLAUDE.md，保留原本的 CRLF，重跑只更新那一段
5. 非 UTF-8 的 CLAUDE.md → 跳過、一個位元組都不改
6. 手寫的同名 SKILL.md → 不覆蓋（除非 --force）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from spineview.install import MARK_BEGIN, install  # noqa: E402

SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dropin", "CLAUDE.md")
failures = 0


def check(ok: bool, label: str) -> None:
    global failures
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok:
        failures += 1


def write(path: str, text: str, newline: str = "\n", encoding: str = "utf-8") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding, newline=newline) as fh:
        fh.write(text)


def raw(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    with tempfile.TemporaryDirectory() as root:
        # 1 + 2：Inanna 型
        a = os.path.join(root, "inanna")
        original = "@.claude/rules/a.md\n@.claude/rules/b.md\n"
        write(os.path.join(a, ".claude", "rules", "a.md"), "# a\n", "\r\n")
        write(os.path.join(a, ".claude", "rules", "b.md"), "# b\n", "\r\n")
        write(os.path.join(a, ".claude", "rules", "CLAUDE.md"), "csdview 留下的 LF 檔\n", "\n")
        write(os.path.join(a, ".claude", "skills", "x", "SKILL.md"), "---\nname: x\n---\n", "\r\n")
        expected = original.replace("\n", "\r\n").encode()
        # 舊版行為：existing.rstrip() + "\n\n" + 段落，整份以 LF 寫出
        write(os.path.join(a, "CLAUDE.md"),
              original.rstrip() + "\n\n" + MARK_BEGIN + "\n\n舊內容\n\n<!-- spineview:end -->\n", "\n")
        r = install(SOURCE, a)
        skill = os.path.join(a, ".claude", "skills", "spineview", "SKILL.md")
        check(r["mode"] == "skill", "有 .claude/skills 時裝成 skill")
        check(os.path.isfile(skill) and raw(skill).startswith(b"---\r\nname: spineview\r\n"),
              "SKILL.md 有 frontmatter，換行格式跟其他 skill 一樣是 CRLF")
        check(raw(os.path.join(a, "CLAUDE.md")) == expected,
              "移除舊段落後 CLAUDE.md 與原檔逐位元組相同（CRLF 也還原了）")
        before = raw(os.path.join(a, "CLAUDE.md"))
        install(SOURCE, a)
        check(raw(os.path.join(a, "CLAUDE.md")) == before, "重跑不再碰 CLAUDE.md")

        # 3：rule 型
        b = os.path.join(root, "rules")
        write(os.path.join(b, ".claude", "rules", "x.md"), "# x\n")
        write(os.path.join(b, "CLAUDE.md"), "@.claude/rules/x.md\n")
        r = install(SOURCE, b)
        install(SOURCE, b)
        text = raw(os.path.join(b, "CLAUDE.md")).decode()
        check(r["mode"] == "rule", "CLAUDE.md 只有 @ 引用時裝成 rule")
        check(text == "@.claude/rules/x.md\n@.claude/rules/spineview.md\n", "加一行 @ 引用，重跑不重複")
        check(not raw(os.path.join(b, ".claude", "rules", "spineview.md")).startswith(b"---"),
              "rule 主檔不帶 frontmatter")

        # 4：一般專案
        c = os.path.join(root, "plain")
        write(os.path.join(c, "CLAUDE.md"), "# 我的專案\n\n自己寫的說明。\n", "\r\n")
        r = install(SOURCE, c)
        install(SOURCE, c)
        data = raw(os.path.join(c, "CLAUDE.md"))
        check(r["mode"] == "claude-md", "一般專案寫進 CLAUDE.md")
        check(data.startswith("# 我的專案\r\n\r\n自己寫的說明。\r\n".encode()), "保留使用者原本的內容")
        check(b"\n" not in data.replace(b"\r\n", b""), "保留原本的 CRLF")
        check(data.count(MARK_BEGIN.encode()) == 1, "重跑只更新那一段")

        # 5：非 UTF-8
        d = os.path.join(root, "big5")
        write(os.path.join(d, "CLAUDE.md"), "# 舊專案\n", "\r\n", "cp950")
        before = raw(os.path.join(d, "CLAUDE.md"))
        try:
            install(SOURCE, d)
            crashed = False
        except UnicodeDecodeError:
            crashed = True       # CLI 會接住並跳過；這裡確認的是「沒有寫壞」
        check(crashed and raw(os.path.join(d, "CLAUDE.md")) == before,
              "非 UTF-8 的 CLAUDE.md 不被改寫")

        # 6：手寫的 SKILL.md
        e = os.path.join(root, "handmade")
        write(os.path.join(e, ".claude", "skills", "spineview", "SKILL.md"), "# 手寫\n")
        r = install(SOURCE, e)
        check(bool(r["skipped"]) and raw(os.path.join(e, ".claude", "skills", "spineview", "SKILL.md"))
              == b"# \xe6\x89\x8b\xe5\xaf\xab\n", "手寫的同名 SKILL.md 不覆蓋")
        r = install(SOURCE, e, force=True)
        check(not r["skipped"], "--force 才覆蓋")
    print("全部通過" if not failures else f"{failures} 項失敗")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
