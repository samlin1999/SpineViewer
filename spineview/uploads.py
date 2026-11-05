"""網頁介面的「上傳」：不必把檔案放進瀏覽的資料夾，也能直接看單一份 Spine。

上傳的檔案放在這次伺服器專用的暫存資料夾（`spineview-uploads-*`），伺服器結束時整個刪掉。
每次上傳一個子資料夾，保留資料夾結構（拖整個資料夾進來時，圖集與貼圖的相對位置不會亂）。

網頁端用 `@upload/<上傳 id>/<相對路徑>` 指稱這些檔案，其餘 API（info、poses、slot…）
完全不用改：ViewerState.resolve 會把這個前綴對應到暫存資料夾。

安全：伺服器只聽 127.0.0.1；檔名一律清理（不允許 ..、絕對路徑、磁碟代號），
只收 Spine 會用到的副檔名，單次上傳有大小與檔數上限，最多保留最近 20 次。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict

from .loader import iter_skeletons

PREFIX = "@upload/"
ALLOWED = (".skel", ".bytes", ".json", ".atlas", ".txt", ".png", ".jpg", ".jpeg", ".webp")
MAX_BYTES = 512 * 1024 * 1024      # 單次上傳總量
MAX_FILES = 4000
KEEP = 20
_BAD_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)}


def safe_relative(name: str) -> str:
    """把瀏覽器送來的相對路徑清成安全的形式；不合法就丟 ValueError。"""
    name = (name or "").replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError("檔名是空的")
    for part in parts:
        if part == ".." or any(c in _BAD_CHARS for c in part) or part.endswith((" ", ".")):
            raise ValueError(f"不合法的檔名：{name}")
    return os.path.join(*parts)


class UploadStore:
    def __init__(self):
        self._base: str | None = None
        self._items: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def _base_dir(self) -> str:
        if self._base is None:
            self._base = tempfile.mkdtemp(prefix="spineview-uploads-")
            atexit.register(shutil.rmtree, self._base, True)
        return self._base

    # ---- 建立／寫入／完成／刪除
    def create(self) -> str:
        with self._lock:
            upload_id = uuid.uuid4().hex[:10]
            folder = os.path.join(self._base_dir(), upload_id)
            os.makedirs(folder)
            self._items[upload_id] = {"dir": folder, "bytes": 0, "files": 0,
                                      "created": time.time(), "label": ""}
            while len(self._items) > KEEP:
                _old, item = self._items.popitem(last=False)
                shutil.rmtree(item["dir"], ignore_errors=True)
            return upload_id

    def _item(self, upload_id: str) -> dict:
        with self._lock:
            item = self._items.get(upload_id or "")
        if item is None:
            raise ValueError("這次上傳已經不存在（伺服器重開過，或被較新的上傳擠掉），請重新上傳")
        return item

    def add_file(self, upload_id: str, name: str, stream, length: int) -> dict:
        """把請求本文寫成檔案。不合法時仍把本文讀完再丟例外，連線才不會壞掉。"""
        def drain():
            remaining = length
            while remaining > 0:
                chunk = stream.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                remaining -= len(chunk)

        try:
            item = self._item(upload_id)
            rel = safe_relative(name)
        except ValueError:
            drain()
            raise
        if not rel.lower().endswith(ALLOWED):
            drain()
            return {"name": rel.replace("\\", "/"), "stored": False, "reason": "不是 Spine 會用到的檔案，略過"}
        with self._lock:
            if item["files"] + 1 > MAX_FILES or item["bytes"] + length > MAX_BYTES:
                over = True
            else:
                over = False
                item["files"] += 1
                item["bytes"] += length
        if over:
            drain()
            raise ValueError(f"一次上傳最多 {MAX_FILES} 個檔案、{MAX_BYTES // (1024 * 1024)} MB")
        target = os.path.join(item["dir"], rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        remaining = length
        with open(target, "wb") as fh:
            while remaining > 0:
                chunk = stream.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining:
            raise ValueError(f"{rel} 沒有傳完（少了 {remaining} bytes）")
        return {"name": rel.replace("\\", "/"), "stored": True, "bytes": length}

    def finish(self, upload_id: str) -> list[dict]:
        item = self._item(upload_id)
        files = self._entries(upload_id, item)
        names = sorted({f["name"] for f in files})
        item["label"] = names[0] if len(names) == 1 else (f"{names[0]} 等 {len(names)} 個" if names else "")
        return files

    def remove(self, upload_id: str) -> None:
        with self._lock:
            item = self._items.pop(upload_id or "", None)
        if item is not None:
            shutil.rmtree(item["dir"], ignore_errors=True)

    # ---- 查詢
    def _entries(self, upload_id: str, item: dict) -> list[dict]:
        out = []
        stamp = time.strftime("%H:%M:%S", time.localtime(item["created"]))
        for full in iter_skeletons(item["dir"]):
            rel = os.path.relpath(full, item["dir"]).replace("\\", "/")
            sub = os.path.dirname(rel)
            out.append({"path": PREFIX + upload_id + "/" + rel, "name": os.path.basename(full),
                        "dir": f"⬆ 上傳 {stamp}" + (f" · {sub}" if sub else ""),
                        "upload": upload_id})
        return out

    def entries(self) -> list[dict]:
        """所有上傳的骨架檔，最新的在前面。"""
        with self._lock:
            items = list(self._items.items())
        out = []
        for upload_id, item in reversed(items):
            out.extend(self._entries(upload_id, item))
        return out

    def resolve(self, path: str) -> str:
        rest = path[len(PREFIX):]
        upload_id, _, rel = rest.partition("/")
        item = self._item(upload_id)
        full = os.path.abspath(os.path.join(item["dir"], safe_relative(rel)))
        if os.path.commonpath([item["dir"], full]) != os.path.abspath(item["dir"]):
            raise ValueError("路徑超出上傳資料夾範圍")
        if not os.path.isfile(full):
            raise FileNotFoundError(path)
        return full

    def folder_of(self, full: str) -> str | None:
        """這個檔案若是上傳的，回傳它所屬的上傳資料夾。"""
        full = os.path.abspath(full)
        with self._lock:
            folders = [item["dir"] for item in self._items.values()]
        for folder in folders:
            try:
                if os.path.commonpath([folder, full]) == os.path.abspath(folder):
                    return folder
            except ValueError:
                continue
        return None
