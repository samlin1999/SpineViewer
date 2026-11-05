"""「📂 開啟」伺服器端邏輯的測試（不會真的跳出視窗）。

    python tests/test_opened.py

用假的 tkinter 取代真的，測：開檔視窗回傳的路徑會被登記、取消回傳 None、
同時只能開一個視窗、找骨架檔的規則（給 .atlas 也找得到同名骨架）、最近開啟清單會存檔。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
failures = 0


def check(ok: bool, label: str) -> None:
    global failures
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok:
        failures += 1


def fake_tkinter(answer, gate=None):
    """回傳一個假的 tkinter 模組；askopenfilename 回傳 answer（gate 給了就先等它）。"""
    tk = types.ModuleType("tkinter")
    fd = types.ModuleType("tkinter.filedialog")

    class Tk:
        def withdraw(self): pass
        def attributes(self, *a): pass
        def update(self): pass
        def destroy(self): pass

    def ask(**kwargs):
        if gate is not None:
            gate.wait(5)
        return answer
    tk.Tk = Tk
    fd.askopenfilename = ask
    tk.filedialog = fd
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.filedialog"] = fd


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    work = tempfile.mkdtemp(prefix="spineview-opened-")
    os.environ["SPINEVIEW_RECENT"] = os.path.join(work, "recent.json")
    import importlib
    import spineview.opened as opened
    importlib.reload(opened)          # 讓它讀到上面設的 SPINEVIEW_RECENT
    try:
        folder = os.path.join(work, "assets")
        os.makedirs(folder)
        skel = os.path.join(folder, "Hero.skel")
        with open(skel, "wb") as fh:
            fh.write(b"\x00")
        with open(os.path.join(folder, "Hero.atlas"), "w") as fh:
            fh.write("Hero.png\n")
        with open(os.path.join(folder, "Hero.png"), "wb") as fh:
            fh.write(b"\x89PNG")

        store = opened.OpenedStore()
        fake_tkinter(skel)
        chosen = store.ask()
        check(chosen == skel, "開檔視窗選的路徑原樣回傳")
        entry = store.add(chosen)
        check(entry["path"].startswith("@open/") and entry["title"] == skel, "登記成 @open/ 路徑，提示是完整路徑")
        check(store.resolve(entry["path"]) == skel, "@open/ 路徑解得回原檔")
        fake_tkinter("")
        check(store.ask() is None, "取消開檔視窗回傳 None")

        gate = threading.Event()
        fake_tkinter(skel, gate)
        first = threading.Thread(target=store.ask)
        first.start()
        import time
        time.sleep(0.2)
        try:
            store.ask()
            second_blocked = False
        except ValueError:
            second_blocked = True
        gate.set()
        first.join()
        check(second_blocked, "同時只能開一個開檔視窗")

        check(opened.find_skeleton(os.path.join(folder, "Hero.atlas")) == skel, "給 .atlas 會找到同名骨架檔")
        check(opened.find_skeleton(os.path.join(folder, "Hero.png")) == skel, "給貼圖也會找到同名骨架檔")
        check(opened.find_skeleton(f'"{skel}"') == skel, "路徑前後的引號會去掉（從檔案總管複製的路徑）")
        for bad, label in ((folder, "資料夾"), (os.path.join(folder, "none.skel"), "找不到")):
            try:
                opened.find_skeleton(bad)
                check(False, f"不合法的路徑要報錯：{label}")
            except ValueError as exc:
                check(label in str(exc), f"不合法的路徑要報錯：{label}")

        again = opened.OpenedStore()
        check(any(e["title"] == skel for e in again.entries()), "最近開啟清單存檔，重新建立後還在")
        again.remove(entry["opened"])
        check(not opened.OpenedStore().entries(), "移除後也寫回清單")
        check(os.path.isfile(skel), "移除不會刪檔案")
    finally:
        for name in ("tkinter", "tkinter.filedialog"):
            sys.modules.pop(name, None)
        shutil.rmtree(work, ignore_errors=True)
    print("全部通過" if not failures else f"{failures} 項失敗")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
