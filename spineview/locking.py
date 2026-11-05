"""跨行程的檔案鎖與原子換檔（移植自 csdview 的同名模組）。

會被好幾個行程同時寫的檔案：
- `~/.spineview_projects`（設定AI助手.bat、spineview init，可能同時跑好幾個）
- `~/.spineview_recent`（同時開好幾個 spineview 網頁檢視器時，都會寫「最近開啟」）
- agent 輸出的圖片（多個 agent 可能寫同一個路徑）

規則：
1. 寫入一律「暫存檔 → os.replace」，別人永遠讀到完整的舊檔或新檔。
2. Windows 上目標若正被別的程式開著（例如另一個 agent 正在讀那張圖、防毒正在掃），
   os.replace 會丟 PermissionError；那是暫時的，等一下重試即可（replace_with_retry）。
3. 「讀出來、改一筆、寫回去」要在 file_lock 裡做，不然兩個行程會互相蓋掉對方剛加的那一筆。
   鎖的是 `<path>.lock`，不是目標本身（目標會被 os.replace 換掉）。
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import time

if os.name == "nt":
    import msvcrt

    def _acquire(handle) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _release(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _acquire(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _release(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


REPLACE_ATTEMPTS = 8


class ReplaceBusy(PermissionError):
    pass


def replace_with_retry(temp: str, target: str, attempts: int = REPLACE_ATTEMPTS) -> None:
    for attempt in range(attempts):
        try:
            os.replace(temp, target)
            return
        except PermissionError as exc:
            if attempt == attempts - 1:
                raise ReplaceBusy(f"{os.path.basename(target)} 一直被別的程式開著，"
                                  f"重試 {attempts} 次都換不進去。") from exc
            time.sleep(0.02 * (attempt + 1))


def atomic_write(target: str, write, suffix: str = ".tmp") -> None:
    """在目標同一個資料夾寫暫存檔（write(暫存檔路徑)），寫完再換上去。失敗時不留暫存檔。"""
    folder = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".spineview-", suffix=suffix)
    os.close(handle)
    try:
        write(temp)
        replace_with_retry(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 10.0):
    """鎖住 `<path>.lock`（跨行程互斥）。"""
    lock_path = path + ".lock"
    folder = os.path.dirname(lock_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    deadline = time.monotonic() + timeout
    handle = open(lock_path, "a+b")
    try:
        while not _acquire(handle):
            if time.monotonic() > deadline:
                raise LockTimeout(f"等 {timeout:g} 秒還拿不到 {os.path.basename(path)} 的鎖。")
            time.sleep(0.05)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()
