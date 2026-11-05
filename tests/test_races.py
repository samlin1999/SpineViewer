"""競態回歸測試：把每個修過的競態實際觸發一次，確認結果正確。

    python tests/test_races.py <骨架檔路徑>      （同資料夾要有 .atlas 與貼圖；會複製到暫存資料夾操作）

1. 同一個檔案被 8 個執行緒同時要：只解析一次，大家拿到同一個 Document
2. 解析途中檔案被換掉：快取的版本是「讀取前」的指紋，下一次一定會重讀（不會把舊內容標成新版本）
3. 找不到圖集時把 .atlas 補進來：下一次會重讀並找到它（以前要重開伺服器）
4. 網址帶的版本和目前內容不同：可長期快取的 API 回 409，不把新內容存進舊版本的網址
5. 檔案清單掃描途中有人上傳：過時的清單不會寫回快取
6. 兩個「最近開啟」（模擬兩個 spineview 視窗）各開一個檔：兩個都留在清單上
7. Windows 上目標檔正被別人開著：換檔會重試成功，不會直接失敗
8. 平行取景只算一次，結果與單執行緒相同
9. 批次渲染的進度計數在多執行緒下不掉數
10. 讀不進來的檔案：失敗也快取（輪詢不會一直重新解析壞檔），錯誤回應附指紋，修好後讀得到
11. 別的網站打來的請求（DNS rebinding、跨站 POST）要擋；圖集裡寫的貼圖只送圖片檔
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
failures = 0


def check(ok: bool, label: str, extra: str = "") -> None:
    global failures
    print(("PASS  " if ok else "FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not ok:
        failures += 1


def copy_skeleton(src: str, dest_dir: str, with_atlas: bool = True) -> str:
    folder = os.path.dirname(os.path.abspath(src))
    base = os.path.basename(src).rsplit(".", 1)[0]
    shutil.copy(src, dest_dir)
    if with_atlas:
        atlas = os.path.join(folder, base + ".atlas")
        shutil.copy(atlas, dest_dir)
        with open(atlas, encoding="utf-8", errors="replace") as fh:
            for line in fh.read().split("\n"):
                name = line.strip()
                if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    shutil.copy(os.path.join(folder, name), dest_dir)
    return os.path.join(dest_dir, os.path.basename(src))


def bump(path: str, seconds: float = 2.0) -> None:
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + int(seconds * 1e9)))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    src = sys.argv[1]
    work = tempfile.mkdtemp(prefix="spineview-races-")
    os.environ["SPINEVIEW_RECENT"] = os.path.join(work, "recent.json")
    import spineview.loader as loader
    real_load = loader.load
    try:
        # 1. single-flight
        a = os.path.join(work, "a")
        os.makedirs(a)
        skel = copy_skeleton(src, a)
        calls = []
        real_load = loader.load

        def counting_load(*args, **kwargs):
            calls.append(1)
            time.sleep(0.2)          # 拉長解析時間，讓其他執行緒一定撞上
            return real_load(*args, **kwargs)

        loader.load = counting_load
        cache = loader.DocumentCache()
        got = [None] * 8
        threads = [threading.Thread(target=lambda i=i: got.__setitem__(i, cache.get(skel))) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        loader.load = real_load
        check(len(calls) == 1, "8 個執行緒同時要同一個檔：只解析一次", f"解析了 {len(calls)} 次")
        check(all(g is got[0] for g in got), "大家拿到同一個 Document 物件")

        # 2. 解析途中檔案被換掉
        real_read = loader.read_skeleton

        def read_then_touch(path, scale=1.0):
            data = real_read(path, scale)
            bump(path)               # 讀完、Document 還沒建好之前，檔案被換掉
            return data

        loader.read_skeleton = read_then_touch
        cache = loader.DocumentCache()
        doc = cache.get(skel)
        loader.read_skeleton = real_read
        check(doc.read_stamp != loader.file_stamp(doc.watch_paths()),
              "解析途中被換掉時，快取記的是讀取前的版本（和現在的檔案不同）")
        again = cache.get(skel)
        check(again is not doc, "下一次會重讀，不會拿舊內容當新版本")
        check(again.read_stamp == loader.file_stamp(again.watch_paths()), "重讀之後版本與檔案一致")

        # 3. 圖集補進來
        b = os.path.join(work, "b")
        os.makedirs(b)
        lone = copy_skeleton(src, b, with_atlas=False)
        cache = loader.DocumentCache()
        first = cache.get(lone)
        check(first.atlas is None, "一開始沒有圖集")
        time.sleep(0.05)
        folder = os.path.dirname(os.path.abspath(src))
        base = os.path.basename(src).rsplit(".", 1)[0]
        shutil.copy(os.path.join(folder, base + ".atlas"), b)
        second = cache.get(lone)
        check(second is not first and second.atlas is not None, "把 .atlas 補進資料夾後，下一次會重讀並找到它")

        # 4. 409
        import spineview.server as server
        state = server.ViewerState(a)
        server.Handler.state = state

        import types

        class Fake(server.Handler):
            def __init__(self, headers=None):
                self.sent = None
                self.server = types.SimpleNamespace(server_address=("127.0.0.1", 8780))
                self.headers = headers if headers is not None else {"Host": "127.0.0.1:8780"}

            def _send(self, code, body, ctype, cache=False):
                self.sent = (code, body)

        h = Fake()
        name = os.path.basename(skel)
        h._meshes({"file": name, "v": "舊版本-0"})
        check(h.sent[0] == 409, "網址的版本跟目前內容不同：meshes 回 409")
        info = h._info({"file": name})
        h._meshes({"file": name, "v": f"{info['stamp']}-{info['boot']}"})
        check(h.sent[0] == 200, "版本相同：meshes 正常回應")
        h._texture({"file": name, "page": "0", "v": "舊版本-0"})
        check(h.sent[0] == 409, "網址的版本跟目前內容不同：texture 回 409")

        # 4b. 伺服器拿到 doc 之後、組回應之前，檔案被換掉（以前的版本會把舊內容標成新版本）
        held = state.document(skel)
        bump(skel, 3.0)
        real_document = state.document
        state.document = lambda full: held          # 讓這次請求用的是換檔前拿到的 doc
        info = h._info({"file": name})
        state.document = real_document
        now = h._stat({"file": name})["stamp"]
        check(info["stamp"] == held.version and info["stamp"] != now,
              "回應標的是內容實際的（舊）版本，與 /api/stat 不同，網頁因此會重新載入")

        # 5. 檔案清單快取
        real_iter = server.iter_skeletons

        def slow_iter(root):
            state.invalidate_files()     # 掃描途中有人上傳／開啟／刪除
            yield from real_iter(root)

        server.iter_skeletons = slow_iter
        state.list_files(fresh=True)
        server.iter_skeletons = real_iter
        check(state._files_cache is None, "掃描途中被 invalidate：過時的清單不寫回快取")

        # 6. 兩個「最近開啟」
        import importlib
        import spineview.opened as opened
        importlib.reload(opened)
        one, two = opened.OpenedStore(), opened.OpenedStore()
        one.add(skel)
        two.add(lone)
        listed = {e["title"] for e in opened.OpenedStore().entries()}
        check(skel in listed and lone in listed, "兩個 spineview 各開一個檔：兩個都留在清單上")
        check(any(e["title"] == lone for e in one.entries()), "另一個視窗開的檔，這邊的清單也看得到")

        # 7. 換檔重試（Windows：目標開著時 os.replace 會丟 PermissionError）
        from spineview.locking import atomic_write
        target = os.path.join(work, "busy.txt")
        with open(target, "w") as fh:
            fh.write("old")
        holder = open(target, "r")

        def release():
            time.sleep(0.15)
            holder.close()

        threading.Thread(target=release).start()

        def write(temp):
            with open(temp, "w") as fh:
                fh.write("new")
        try:
            atomic_write(target, write)
            ok = open(target).read() == "new"
        except Exception as exc:
            ok = False
            print("      ", exc)
        check(ok, "目標檔正被別人開著：換檔重試後成功" + ("" if os.name == "nt" else "（非 Windows 不會擋）"))
        leftovers = [n for n in os.listdir(work) if n.startswith(".spineview-")]
        check(not leftovers, "沒有留下暫存檔")

        # 8. 平行取景
        from spineview.render import Renderer, RenderOptions
        doc = loader.load(skel)
        anim = doc.data.animations[0] if doc.data.animations else None
        renderer = Renderer(doc, RenderOptions(layout_only=True))
        skin = renderer.default_skin_name()
        counted = []
        real_new = renderer.new_skeleton

        def counting_new(s):
            counted.append(1)
            time.sleep(0.05)
            return real_new(s)

        renderer.new_skeleton = counting_new
        results = [None] * 6
        threads = [threading.Thread(target=lambda i=i: results.__setitem__(
            i, renderer.content_bounds(anim, skin, set(), None))) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        check(len(counted) == 1, "6 個請求同時要同一個取景：只算一次", f"算了 {len(counted)} 次")
        single = Renderer(doc, RenderOptions(layout_only=True)).content_bounds(anim, skin, set(), None)
        check(all(r == single for r in results), "平行取景的結果和單執行緒相同")

        # 9. 進度計數
        import spineview.cli as cli
        out = io.StringIO()
        real_err, real_out = sys.stderr, sys.stdout
        sys.stderr, sys.stdout = out, io.StringIO()
        try:
            cli.main(["frames", skel, "--anim", anim.name, "--scale", "0.2", "--jobs", "8",
                      "--out", os.path.join(work, "frames"), "--json"]) if anim else None
        finally:
            sys.stderr, sys.stdout = real_err, real_out
        text = out.getvalue()
        total = len(os.listdir(os.path.join(work, "frames"))) if anim else 0
        check(not anim or f"{total}/{total}" in text, "多執行緒批次渲染的進度最後是 N/N（沒有掉數）",
              text.strip().split("\r")[-1] if text else "")
        # 10. 讀不進來的檔案：失敗也記下來（網頁在出錯時仍每 1.5 秒輪詢，不記的話壞檔每次都被整個重新解析）；
        #     錯誤回應附讀取前的指紋，和 /api/stat 同一種格式，網頁靠它知道檔案修好了
        c = os.path.join(work, "c")
        os.makedirs(c)
        broken = copy_skeleton(src, c)
        with open(broken, "rb") as fh:
            good = fh.read()
        with open(broken, "wb") as fh:
            fh.write(b"not a skeleton")
        parses = []

        def counting_plain(*args, **kwargs):
            parses.append(1)
            return real_load(*args, **kwargs)

        loader.load = counting_plain
        cache = loader.DocumentCache()
        errors = []
        for _ in range(4):
            try:
                cache.get(broken)
            except Exception as exc:
                errors.append(exc)
        check(len(errors) == 4 and len(parses) == 1, "壞掉的檔案只解析一次，之後直接回報同一個錯",
              f"解析 {len(parses)} 次、錯誤 {len(errors)} 次")
        check(len({type(e) for e in errors}) == 1 and all(str(e) == str(errors[0]) for e in errors),
              "每次回報的錯誤都一樣")
        check(getattr(errors[0], "spineview_stamp", None) == loader.file_stamp(loader.failure_watch_paths(broken)),
              "錯誤上帶著讀取前的指紋")
        with open(broken, "wb") as fh:
            fh.write(good)
        bump(broken)
        fixed = cache.get(broken)
        check(fixed is not None and len(parses) == 2, "檔案修好後下一次就重讀成功")
        loader.load = real_load

        state = server.ViewerState(c)
        server.Handler.state = state
        h = Fake()
        bad_name = os.path.basename(broken)
        with open(broken, "wb") as fh:
            fh.write(b"not a skeleton")
        bump(broken, 4.0)
        import json as _json
        h.path = "/api/info?file=" + bad_name
        h.do_GET()
        payload = _json.loads(h.sent[1])
        stat = h._stat({"file": bad_name})
        check("error" in payload and payload.get("stamp") == stat["stamp"] and payload.get("boot") == stat["boot"],
              "/api/info 的錯誤回應附上指紋，和 /api/stat 一致（沒修好時網頁不會一直重載）",
              _json.dumps({k: payload.get(k) for k in ("stamp", "boot")}, ensure_ascii=False))
        with open(broken, "wb") as fh:
            fh.write(good)
        bump(broken, 6.0)
        check(h._stat({"file": bad_name})["stamp"] != payload["stamp"], "檔案修好後 /api/stat 的指紋就變了（網頁據此重新載入）")
        h.do_GET()
        check("error" not in _json.loads(h.sent[1]), "修好後 /api/info 正常回應")
        # 11. 別的網站打過來的請求要擋（DNS rebinding 讀資料、跨站 POST 製造副作用）；貼圖只送圖片
        def status(headers, method="GET", path="/api/files"):
            f = Fake(headers)
            f.path = path
            (f.do_POST if method == "POST" else f.do_GET)()
            return f.sent[0]
        check(status({"Host": "127.0.0.1:8780"}) == 200, "自己的網頁（Host 127.0.0.1）可以用")
        check(status({"Host": "localhost:8780"}) == 200, "用 localhost 開也可以")
        check(status({"Host": "evil.example:8780"}) == 403, "Host 是別的網域（DNS rebinding）→ 擋")
        check(status({"Host": "127.0.0.1:9999"}) == 403, "Host 的埠號不是這個伺服器 → 擋")
        check(status({"Host": "127.0.0.1:8780", "Origin": "http://evil.example"}, "POST", "/api/open/dialog") == 403,
              "別的網站發來的 POST（例如跳開檔視窗）→ 擋")
        check(status({"Host": "127.0.0.1:8780", "Origin": "null"}, "POST", "/api/upload/new") == 403,
              "Origin 是 null（沙盒 iframe、本機 html 檔）→ 擋")
        check(status({"Host": "127.0.0.1:8780", "Origin": "http://127.0.0.1:8780"}, "POST", "/api/upload/new") == 200,
              "自己的網頁發的 POST 照常")
        d = os.path.join(work, "d")
        os.makedirs(d)
        trick = copy_skeleton(src, d)
        with open(os.path.join(work, "secret.txt"), "w", encoding="utf-8") as fh:
            fh.write("不該被送出去的內容")
        atlas_path = os.path.join(d, os.path.basename(trick).rsplit(".", 1)[0] + ".atlas")
        with open(atlas_path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        first_page = next(i for i, line in enumerate(lines) if line.strip().lower().endswith(".png"))
        lines[first_page] = "../secret.txt"
        with open(atlas_path, "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join(lines))
        server.Handler.state = server.ViewerState(d)
        f = Fake()
        f.path = "/api/texture?file=" + os.path.basename(trick) + "&page=0"
        f.do_GET()
        check(f.sent[0] != 200 and "不該被送出去" not in f.sent[1].decode("utf-8", "replace"),
              "圖集把貼圖寫成 ../secret.txt：不會把那個檔案的內容送出去", f"HTTP {f.sent[0]}")

        # 12. 屬性分頁用 index 指定插槽（名稱剛好是數字的插槽不會和第 N 個搞混）；超出範圍要報錯，不能退回去用名稱模糊比對
        server.Handler.state = server.ViewerState(a)
        f = Fake()
        doc_a = server.Handler.state.document(skel)
        last_slot = len(doc_a.data.slots) - 1
        detail = f._slot({"file": name, "index": str(last_slot)})
        check(detail["slot"]["index"] == last_slot and detail["slot"]["name"] == doc_a.data.slots[last_slot].name,
              "用 index 取插槽屬性拿到的就是那一個")
        try:
            f._slot({"file": name, "index": str(last_slot + 40)})
            out_of_range = False
        except ValueError:
            out_of_range = True
        check(out_of_range, "index 超出範圍時報錯（不會變成拿名稱裡含那串數字的插槽）")
    finally:
        loader.load = real_load
        shutil.rmtree(work, ignore_errors=True)
    print("全部通過" if not failures else f"{failures} 項失敗")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
