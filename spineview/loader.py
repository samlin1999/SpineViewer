"""把骨架檔與它的圖集配成一份 Document，並把貼圖區塊接到附件上。

圖集怎麼找（依序）：
1. 呼叫端用 --atlas 指定
2. 同資料夾、同主檔名的 .atlas（Hero.skel → Hero.atlas）——遊戲程式幾乎都是這樣載入
3. 同資料夾只有一個 .atlas
4. 同資料夾有好幾個：挑涵蓋最多附件路徑的那個，並在 warnings 註明是猜的
"""
from __future__ import annotations

import copy
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from .atlas import Atlas, fill_page_sizes, parse_atlas, texture_size
from .model import MeshAttachment, RegionAttachment, SkeletonData
from .reader_binary import SkeletonFormatError, read_binary
from .reader_json import looks_like_spine_json, read_json

SKIP_DIRS = {"node_modules", ".git", ".svn", "__pycache__", "out", "build", "temp",
             "library", "local"}
DEFAULT_FPS = 30.0


@dataclass
class Document:
    path: str
    data: SkeletonData
    atlas: Optional[Atlas]
    atlas_path: Optional[str]
    atlas_guessed: bool = False
    missing_regions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # 每個相依檔「讀取前」的指紋（見 watch_paths）。版本號一律用它，不用事後重新問到的指紋：
    # 讀到一半檔案被換掉時，事後的指紋是新的、內容卻是舊的，會把舊內容標成新版本，
    # 之後就再也不會重讀（csdview 踩過的坑）。
    read_stamp: tuple = ()

    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]

    @property
    def fps(self) -> float:
        return self.data.fps if self.data.fps and self.data.fps > 0 else DEFAULT_FPS

    def files(self) -> list[str]:
        """畫面相依的所有檔案（骨架、圖集、每一頁貼圖）。"""
        out = [self.path]
        if self.atlas_path:
            out.append(self.atlas_path)
        if self.atlas is not None:
            out.extend(page.path for page in self.atlas.pages)
        return out

    def watch_paths(self) -> list[str]:
        """自動更新要盯的路徑：相依檔，加上「圖集還可能換人」時的資料夾本身。

        找不到圖集、或是從好幾個裡面猜的時候，資料夾裡新增一個 .atlas 就可能改變結果；
        只盯檔案的話，把漏掉的 .atlas 補進來後永遠不會重讀（按重新整理也一樣）。
        資料夾的 mtime 在裡面新增／刪除檔案時會變。
        """
        out = self.files()
        if self.atlas is None or self.atlas_guessed:
            out.append(os.path.dirname(self.path))
        return out

    @property
    def version(self) -> str:
        """給網頁與快取用的版本字串（讀取前的指紋）。"""
        return "-".join(f"{m}.{s}" for m, s in self.read_stamp)


def is_skeleton_file(path: str) -> bool:
    lower = path.lower()
    if lower.endswith(".skel") or lower.endswith(".skel.bytes"):
        return True
    if lower.endswith(".json"):
        try:
            with open(path, "rb") as fh:
                head = fh.read(512)
        except OSError:
            return False
        return looks_like_spine_json(head)
    return False


def iter_skeletons(root: str):
    """遞迴列出資料夾底下的骨架檔（.skel 與 Spine JSON）。"""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS)
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            if is_skeleton_file(full):
                yield full


def _base(path: str) -> str:
    name = os.path.basename(path)
    for ext in (".skel.bytes", ".skel", ".json"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return os.path.splitext(name)[0]


def atlas_candidates(path: str) -> list[str]:
    folder = os.path.dirname(os.path.abspath(path))
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(os.path.join(folder, n) for n in names
                  if n.lower().endswith((".atlas", ".atlas.txt")))


def _needed_paths(data: SkeletonData) -> set[str]:
    need = set()
    for _skin, _slot, _name, att in data.iter_attachments():
        if isinstance(att, (RegionAttachment, MeshAttachment)):
            need.add(att.path)
    return need


def find_atlas(path: str, data: Optional[SkeletonData] = None) -> tuple[Optional[str], bool]:
    """回傳 (圖集路徑, 是否為猜測)。"""
    candidates = atlas_candidates(path)
    if not candidates:
        return None, False
    base = _base(path).lower()
    for cand in candidates:
        name = os.path.basename(cand).lower()
        if name in (base + ".atlas", base + ".atlas.txt"):
            return cand, False
    if len(candidates) == 1:
        return candidates[0], False
    if data is None:
        return None, False
    need = _needed_paths(data)
    best, best_score = None, -1
    for cand in candidates:
        try:
            atlas = parse_atlas(cand)
        except Exception:
            continue
        score = sum(1 for p in need if atlas.find_region(p) is not None)
        if score > best_score:
            best, best_score = cand, score
    return best, True


def read_skeleton(path: str, scale: float = 1.0) -> SkeletonData:
    with open(path, "rb") as fh:
        raw = fh.read()
    if path.lower().endswith(".json"):
        return read_json(raw.decode("utf-8-sig", errors="replace"), scale)
    head = raw[:64].lstrip()
    if head.startswith(b"{"):
        return read_json(raw.decode("utf-8-sig", errors="replace"), scale)
    return read_binary(raw, scale)


def attach_regions(data: SkeletonData, atlas: Optional[Atlas]) -> list[dict]:
    """把貼圖區塊接到附件上，回傳找不到的清單。

    遊戲端（AtlasAttachmentLoader）找不到 region 時會讓整份骨架載入失敗，
    這裡則照樣載入、那個附件不畫，並把它列出來交給 doctor 判定嚴重度。
    """
    missing: list[dict] = []
    seen = set()
    for skin, slot_index, name, att in data.iter_attachments():
        if not isinstance(att, (RegionAttachment, MeshAttachment)):
            continue
        if id(att) in seen:
            continue
        seen.add(id(att))
        region = atlas.find_region(att.path) if atlas is not None else None
        att.region = region
        if region is None:
            missing.append({"skin": skin.name, "slot": data.slots[slot_index].name,
                            "attachment": name, "path": att.path, "type": att.kind})
        if isinstance(att, RegionAttachment):
            att.update_offset()
        else:
            att.update_uvs()
    return missing


def load(path: str, atlas_path: Optional[str] = None, scale: float = 1.0) -> Document:
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    # 每個檔案在「讀它之前」記下指紋（理由見 Document.read_stamp）
    stamps: dict[str, tuple] = {}
    folder_before = file_stamp([os.path.dirname(path)])[0]
    stamps[path] = file_stamp([path])[0]
    data = read_skeleton(path, scale)
    warnings: list[str] = []
    guessed = False
    if atlas_path:
        atlas_path = os.path.abspath(atlas_path)
    else:
        atlas_path, guessed = find_atlas(path, data)
    atlas = None
    if atlas_path and os.path.isfile(atlas_path):
        stamps[atlas_path] = file_stamp([atlas_path])[0]
        atlas = parse_atlas(atlas_path)
        if atlas.format_hint == "4.x":
            warnings.append("圖集是 Spine 4.x 的格式，遊戲用的 3.8 runtime 讀不進來。")
        sizes = {}
        for page in atlas.pages:
            stamps[page.path] = file_stamp([page.path])[0]
            if not page.width or not page.height:
                size = texture_size(page.path)
                if size:
                    sizes[page.path] = size
        if sizes:
            fill_page_sizes(atlas, sizes)
        if guessed:
            warnings.append(f"同資料夾有多個 .atlas，依附件涵蓋率猜測用 "
                            f"{os.path.basename(atlas_path)}；不對的話用 --atlas 指定。")
    elif atlas_path:
        warnings.append(f"找不到圖集檔：{atlas_path}")
        atlas_path = None
    else:
        warnings.append("找不到對應的 .atlas，只能畫出骨骼與外框。")
    missing = attach_regions(data, atlas)
    doc = Document(path=path, data=data, atlas=atlas, atlas_path=atlas_path,
                   atlas_guessed=guessed, missing_regions=missing, warnings=warnings)
    stamps[os.path.dirname(path)] = folder_before
    for p in doc.watch_paths():
        if p not in stamps:              # 不存在的檔案（缺的貼圖）也要有指紋：補進來時才會變
            stamps[p] = file_stamp([p])[0]
    doc.read_stamp = tuple(stamps[p] for p in doc.watch_paths())
    return doc


def file_stamp(paths: list[str]) -> tuple:
    """一組檔案的指紋（奈秒 mtime + 大小）。任何一個改了指紋就會變。"""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((0, -1))
    return tuple(out)


def failure_watch_paths(path: str) -> list[str]:
    """讀檔失敗時要盯的路徑：骨架檔本身，加上它的資料夾。

    失敗時沒有 Document，不知道相依檔有哪些；骨架檔被改（例如改用 3.8 重新匯出）、
    或資料夾裡換檔（匯出工具常是寫暫存檔再改名），任何一個都可能讓它讀得進來了。
    """
    return [path, os.path.dirname(os.path.abspath(path))]


class DocumentCache:
    """依檔案指紋快取 Document；骨架、圖集或貼圖任何一個被換掉就重讀。

    - 版本用 Document.read_stamp（讀取前的指紋），不是讀完才問的指紋
    - 同一個檔案同時只讀一次（single-flight）：網頁一打開會同時發好幾個請求，
      不擋的話大檔會被平行解析好幾遍，而且各請求拿到不同的 Document 物件
    - 讀不進來也記下來（指紋沒變就直接回報同一個錯）：網頁在檔案壞掉時仍會每 1.5 秒輪詢，
      不記的話壞檔每次都會被整個重新解析一遍。例外上的 spineview_stamp 是讀取前的指紋，
      網頁靠它判斷檔案之後有沒有被修好
    """

    def __init__(self, limit: int = 32):
        self.limit = limit
        self._docs: OrderedDict = OrderedDict()
        self._failed: OrderedDict = OrderedDict()   # key → (讀取前的指紋, 例外)
        self._lock = threading.Lock()
        self._loading: dict = {}          # key → 這個檔案專用的鎖

    def _fresh(self, key):
        with self._lock:
            hit = self._docs.get(key)
        if hit is not None and file_stamp(hit.watch_paths()) == hit.read_stamp:
            with self._lock:
                if key in self._docs:
                    self._docs.move_to_end(key)
            return hit
        return None

    def _known_failure(self, key, path: str):
        with self._lock:
            hit = self._failed.get(key)
        if hit is not None and file_stamp(failure_watch_paths(path)) == hit[0]:
            return hit[1]
        return None

    @staticmethod
    def _raise_copy(exc: BaseException):
        # 每次丟一個新的複本：同一個例外物件被好幾個執行緒、好幾次 raise，traceback 會越疊越長
        try:
            fresh = copy.copy(exc)
        except Exception:
            fresh = exc
        # 自訂 __reduce__ 的例外（例如 json.JSONDecodeError）複製時會掉屬性；指紋要補回去
        stamp = getattr(exc, "spineview_stamp", None)
        if stamp is not None:
            try:
                fresh.spineview_stamp = stamp
            except AttributeError:
                fresh = exc
        raise fresh.with_traceback(None)

    def get(self, path: str, atlas_path: Optional[str] = None) -> Document:
        key = (os.path.normcase(os.path.abspath(path)), atlas_path or "")
        doc = self._fresh(key)
        if doc is not None:
            return doc
        failed = self._known_failure(key, path)
        if failed is not None:
            self._raise_copy(failed)
        with self._lock:
            gate = self._loading.setdefault(key, threading.Lock())
        with gate:
            doc = self._fresh(key)           # 排隊期間別人可能已經讀好了
            if doc is not None:
                return doc
            failed = self._known_failure(key, path)
            if failed is not None:
                self._raise_copy(failed)
            before = file_stamp(failure_watch_paths(path))
            try:
                doc = load(path, atlas_path)
            except OSError:
                raise                        # 檔案不見、被鎖住：不記，下次直接再試（很便宜）
            except Exception as exc:
                exc.spineview_stamp = before
                with self._lock:
                    self._failed[key] = (before, exc)
                    self._failed.move_to_end(key)
                    while len(self._failed) > self.limit:
                        self._failed.popitem(last=False)
                raise
            with self._lock:
                self._failed.pop(key, None)
                self._docs[key] = doc
                self._docs.move_to_end(key)
                while len(self._docs) > self.limit:
                    self._docs.popitem(last=False)
            return doc


__all__ = ["Document", "load", "find_atlas", "iter_skeletons", "is_skeleton_file",
           "SkeletonFormatError", "DocumentCache", "file_stamp", "failure_watch_paths", "DEFAULT_FPS"]
