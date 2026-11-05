"""網頁介面的「開啟」：只選一個 .skel，圖集與貼圖直接從它所在的資料夾讀。

瀏覽器的檔案選擇框不會告訴網頁檔案原本在哪個資料夾（安全限制），所以「上傳」只拿得到
選了的那幾個檔案。但 spineview 的伺服器就跑在同一台電腦上，由伺服器跳出 Windows 的開檔視窗，
拿到的就是完整路徑——不必上傳，讀的是原檔，美術重新匯出時畫面也會自動更新。

網頁端用 `@open/<id>/<檔名>` 指稱這些檔案；id 是完整路徑的雜湊，伺服器重開後網址仍然有效。
開過的檔案記在 `~/.spineview_recent`（最近 15 個），下次啟動還在列表上。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict

from .loader import is_skeleton_file
from .locking import LockTimeout, atomic_write, file_lock

PREFIX = "@open/"
KEEP = 15
_RECENT = os.environ.get("SPINEVIEW_RECENT") or os.path.join(os.path.expanduser("~"), ".spineview_recent")
_dialog_lock = threading.Lock()


def _key(path: str) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:10]


def find_skeleton(path: str) -> str:
    """使用者給的路徑 → 骨架檔。給了同資料夾的 .atlas／貼圖也幫忙找同名骨架檔。"""
    path = (path or "").strip().strip('"').strip("'")
    if not path:
        raise ValueError("沒有給路徑")
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(path):
        raise ValueError("這是資料夾。請選資料夾裡的 .skel（或 Spine 的 .json），"
                         "或用 開啟介面.bat 開整個資料夾")
    if not os.path.isfile(path):
        raise ValueError(f"找不到檔案：{path}")
    if is_skeleton_file(path):
        return path
    folder, name = os.path.split(path)
    base = name.split(".")[0]
    for ext in (".skel", ".skel.bytes", ".json"):
        candidate = os.path.join(folder, base + ext)
        if os.path.isfile(candidate) and is_skeleton_file(candidate):
            return candidate
    raise ValueError(f"{name} 不是 Spine 骨架檔（.skel 或 Spine 匯出的 .json），同資料夾也找不到同名的骨架檔")


def _read_recent() -> list[str]:
    try:
        with open(_RECENT, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return []
    return [p for p in saved if isinstance(p, str)] if isinstance(saved, list) else []


class OpenedStore:
    """「最近開啟」清單。

    同時開好幾個 spineview 時，大家共用 `~/.spineview_recent`。所以每次增刪都是
    「鎖住 → 讀檔案上最新的清單 → 改一筆 → 寫回」，而不是把自己記憶體裡的整份蓋上去——
    後者會把另一個視窗剛開的檔案洗掉。列清單時也會在檔案變動後重讀。
    """

    def __init__(self, remember: bool = True):
        self._items: OrderedDict[str, str] = OrderedDict()     # id → 完整路徑，最新的在後面
        self._lock = threading.Lock()
        self._remember = remember
        self._seen = None
        self.last_dir: str | None = None
        if remember:
            self._load(_read_recent())
            if self._items:
                self.last_dir = os.path.dirname(next(reversed(self._items.values())))

    def _load(self, paths: list[str]) -> None:
        self._items = OrderedDict((_key(p), p) for p in paths[-KEEP:])
        try:
            st = os.stat(_RECENT)
            self._seen = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._seen = None

    def _refresh(self) -> None:
        """別的 spineview 改過清單就重讀。"""
        if not self._remember:
            return
        try:
            st = os.stat(_RECENT)
            now = (st.st_mtime_ns, st.st_size)
        except OSError:
            now = None
        if now != self._seen:
            self._load(_read_recent())

    def _update(self, change) -> None:
        """在跨行程的鎖裡讀最新清單、套用 change(list)、寫回；同步更新記憶體。"""
        if not self._remember:
            paths = list(self._items.values())
            change(paths)
            self._items = OrderedDict((_key(p), p) for p in paths[-KEEP:])
            return
        try:
            with file_lock(_RECENT):
                paths = _read_recent()
                change(paths)
                paths = paths[-KEEP:]

                def write(temp):
                    with open(temp, "w", encoding="utf-8") as fh:
                        json.dump(paths, fh, ensure_ascii=False, indent=2)
                atomic_write(_RECENT, write)
                self._load(paths)
        except (OSError, LockTimeout):
            # 寫不進去（唯讀的家目錄等）：至少這次執行期間記得
            paths = list(self._items.values())
            change(paths)
            self._items = OrderedDict((_key(p), p) for p in paths[-KEEP:])

    def add(self, path: str) -> dict:
        full = find_skeleton(path)
        key = _key(full)

        def change(paths):
            paths[:] = [p for p in paths if _key(p) != key] + [full]
        with self._lock:
            self._update(change)
            self.last_dir = os.path.dirname(full)
        return self._entry(key, full)

    def remove(self, key: str) -> None:
        def change(paths):
            paths[:] = [p for p in paths if _key(p) != key]
        with self._lock:
            self._update(change)

    def _entry(self, key: str, full: str) -> dict:
        return {"path": f"{PREFIX}{key}/{os.path.basename(full)}", "name": os.path.basename(full),
                "dir": "📂 最近開啟", "opened": key, "title": full}

    def entries(self) -> list[dict]:
        with self._lock:
            self._refresh()
            items = list(self._items.items())
        return [self._entry(k, p) for k, p in reversed(items) if os.path.isfile(p)]

    def resolve(self, path: str) -> str:
        key = path[len(PREFIX):].split("/", 1)[0]
        with self._lock:
            full = self._items.get(key)
            if full is None:
                self._refresh()          # 可能是另一個 spineview 剛開的
                full = self._items.get(key)
        if full is None:
            raise ValueError("這個檔案不在「最近開啟」裡了，請重新用「📂 開啟」選一次")
        if not os.path.isfile(full):
            raise FileNotFoundError(full)
        return full

    def ask(self) -> str | None:
        """在這台電腦上跳出開檔視窗，回傳選了的路徑；取消回傳 None。

        網頁伺服器只聽 127.0.0.1，所以這個視窗就出現在使用者自己的桌面上。
        同時只開一個（重複按「開啟」不會疊出好幾個視窗）。
        """
        if not _dialog_lock.acquire(blocking=False):
            raise ValueError("開檔視窗已經開著了（可能被瀏覽器擋在後面），請先完成或取消那一個")
        try:
            try:
                import tkinter
                from tkinter import filedialog
            except ImportError:
                raise ValueError("這台電腦的 Python 沒有 tkinter，無法跳出開檔視窗。"
                                 "請在左上角的篩選框貼上 .skel 的完整路徑後按 Enter") from None
            root = tkinter.Tk()
            try:
                root.withdraw()
                root.attributes("-topmost", True)     # 不然視窗常被瀏覽器蓋住，看起來像沒反應
                root.update()
                chosen = filedialog.askopenfilename(
                    parent=root, title="選一個 Spine 骨架檔（圖集與貼圖會從同一個資料夾讀）",
                    initialdir=self.last_dir or os.path.expanduser("~"),
                    filetypes=[("Spine 骨架", "*.skel *.json *.bytes"), ("所有檔案", "*.*")])
            finally:
                root.destroy()
            return chosen or None
        finally:
            _dialog_lock.release()
