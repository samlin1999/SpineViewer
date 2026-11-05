"""doctor：找出「遊戲裡會出事、但看圖不一定看得出來」的問題。

嚴重度的定義很嚴格，因為 agent 會照著它決定要不要動手：
- error：遊戲端會載入失敗或必定畫錯（依據都是 Cocos2d-x 內附 spine runtime 的實際行為）
- warning：可能出事，要看專案怎麼用（例如大小寫只在 Android／iOS 上才會壞）
- info：值得知道，但不是錯誤（例如圖集裡有沒用到的圖）
"""
from __future__ import annotations

import os
from typing import Optional

from .atlas import texture_size
from .loader import Document, find_atlas, load
from .model import MeshAttachment, RegionAttachment
from .reader_binary import SkeletonFormatError


def _issue(severity: str, kind: str, message: str, **detail) -> dict:
    out = {"severity": severity, "kind": kind, "message": message}
    out.update({k: v for k, v in detail.items() if v is not None})
    return out


def _case_on_disk(path: str) -> Optional[str]:
    """回傳磁碟上的實際檔名；大小寫與 path 不同時才有意義。找不到檔案回傳 None。"""
    folder, name = os.path.split(path)
    try:
        for entry in os.listdir(folder or "."):
            if entry.lower() == name.lower():
                return entry
    except OSError:
        return None
    return None


def check(path: str, atlas_path: Optional[str] = None) -> dict:
    issues: list[dict] = []
    try:
        doc = load(path, atlas_path)
    except SkeletonFormatError as exc:
        issues.append(_issue("error", "loadError", f"讀不進來：{exc}"))
        return {"file": os.path.abspath(path), "issues": issues, "ok": False}
    except Exception as exc:
        issues.append(_issue("error", "loadError", f"讀不進來：{type(exc).__name__}: {exc}"))
        return {"file": os.path.abspath(path), "issues": issues, "ok": False}
    issues.extend(check_document(doc))
    return {"file": doc.path, "atlas": doc.atlas_path, "version": doc.data.version,
            "issues": issues, "ok": not any(i["severity"] == "error" for i in issues)}


def check_document(doc: Document) -> list[dict]:
    issues: list[dict] = []
    data = doc.data

    if doc.atlas is None:
        issues.append(_issue("error", "atlasMissing",
                             "找不到對應的 .atlas。遊戲端載入時需要圖集，沒有就無法建立骨架。"))
    else:
        if doc.atlas.format_hint == "4.x":
            issues.append(_issue("error", "atlasFormat",
                                 "圖集是 Spine 4.x 的格式，遊戲用的 3.8 runtime 讀不進來。"))
        if doc.atlas_guessed:
            issues.append(_issue("warning", "atlasGuessed",
                                 f"同資料夾有多個 .atlas，猜測用 {os.path.basename(doc.atlas_path)}。"
                                 "遊戲端是程式指定圖集路徑，請確認程式用的是哪一個。"))
        actual = _case_on_disk(doc.atlas_path)
        if actual and actual != os.path.basename(doc.atlas_path):
            issues.append(_issue("warning", "caseMismatch",
                                 f"圖集檔名大小寫與磁碟不同：{os.path.basename(doc.atlas_path)} vs {actual}",
                                 file=doc.atlas_path))
        for page in doc.atlas.pages:
            if not os.path.isfile(page.path):
                issues.append(_issue("error", "pageMissing",
                                     f"圖集第 {doc.atlas.pages.index(page) + 1} 頁的貼圖不存在：{page.name}",
                                     file=page.path))
                continue
            actual = _case_on_disk(page.path)
            if actual and actual != os.path.basename(page.path):
                issues.append(_issue("warning", "caseMismatch",
                                     f"圖集寫的貼圖檔名是 {page.name}，磁碟上是 {actual}。"
                                     "Windows 不分大小寫所以看起來正常，Android／iOS 上會找不到圖。",
                                     file=page.path))
            size = texture_size(page.path)
            if size and page.width and page.height and size != (page.width, page.height):
                ratio_w = size[0] / page.width
                ratio_h = size[1] / page.height
                if abs(ratio_w - ratio_h) > 0.01:
                    issues.append(_issue("error", "pageSizeMismatch",
                                         f"{page.name} 實際尺寸 {size[0]}×{size[1]}，圖集寫 "
                                         f"{page.width}×{page.height}，長寬比不同——貼圖與圖集不是同一次匯出的。",
                                         file=page.path))
                else:
                    issues.append(_issue("info", "pageScaled",
                                         f"{page.name} 實際尺寸 {size[0]}×{size[1]}，圖集寫 "
                                         f"{page.width}×{page.height}（等比縮放過，UV 仍然正確）。",
                                         file=page.path))
        for region in doc.atlas.regions:
            page = region.page
            w, h = region.packed_size()
            if page.width and page.height and (region.x + w > page.width or region.y + h > page.height
                                               or region.x < 0 or region.y < 0):
                issues.append(_issue("error", "regionOutOfBounds",
                                     f"region {region.name} 超出貼圖範圍"
                                     f"（{region.x},{region.y} {w}×{h}，頁面 {page.width}×{page.height}）。"
                                     "圖集多半是舊的。", region=region.name))

    for m in doc.missing_regions:
        issues.append(_issue(
            "error", "missingRegion",
            f"附件 {m['attachment']}（插槽 {m['slot']}，造型 {m['skin']}）要的圖 {m['path']} 不在圖集裡。"
            "遊戲端的 AtlasAttachmentLoader 找不到 region 時整份骨架會載入失敗"
            "（Error reading attachment）。",
            slot=m["slot"], attachment=m["attachment"], region=m["path"], skin=m["skin"]))

    if doc.atlas is not None:
        used = set()
        for _skin, _slot, _name, att in data.iter_attachments():
            if isinstance(att, (RegionAttachment, MeshAttachment)):
                used.add(att.path)
        unused = sorted({r.name for r in doc.atlas.regions} - used)
        if unused:
            issues.append(_issue("info", "unusedRegions",
                                 f"圖集裡有 {len(unused)} 張圖沒有任何附件用到（佔貼圖空間）。",
                                 regions=unused[:50]))

    dark = [s.name for s in data.slots if s.darkColor is not None]
    if dark:
        issues.append(_issue("info", "darkColorIgnored",
                             f"{len(dark)} 個插槽設了 tint black（dark color），但 Cocos2d-x 的 "
                             "SkeletonRenderer 預設不開 two color tint，除非程式呼叫 setTwoColorTint(true)，"
                             "遊戲裡看不到這個效果。spineview 預設也不套用，用 --tint-black 看開啟後的樣子。",
                             slots=dark[:30]))

    empty = [a.name for a in data.animations if a.duration <= 0]
    if empty:
        issues.append(_issue("info", "zeroDuration",
                             f"{len(empty)} 段動畫長度為 0（只有第 0 格的關鍵影格）：{', '.join(empty[:10])}"))

    if data.version and data.version.startswith("3.5"):
        issues.append(_issue("info", "oldFormat",
                             "這是 Spine 3.5 的檔案。引擎的讀檔器有特別支援，但 3.5 沒有 clipping、"
                             "point 附件與 two color tint。"))
    for w in doc.warnings:
        if "找不到" in w or "4.x" in w or "猜測" in w:
            continue          # 上面已經各自列成具體的 issue
        issues.append(_issue("warning", "loader", w))
    return issues


def summarize(results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    files_with_errors = []
    for r in results:
        kinds = set()
        for issue in r["issues"]:
            key = f"{issue['severity']}:{issue['kind']}"
            if key not in kinds:
                counts[key] = counts.get(key, 0) + 1
                kinds.add(key)
        if not r["ok"]:
            files_with_errors.append(r["file"])
    return {"files": len(results), "filesWithErrors": len(files_with_errors),
            "issueFiles": dict(sorted(counts.items())), "errorFiles": files_with_errors}


__all__ = ["check", "check_document", "summarize", "find_atlas"]
