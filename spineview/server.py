"""本機網頁檢視器。

分工：
- 骨架姿勢、每個附件的世界座標頂點、顏色、混色、clipping、繪製順序——全部由 Python 算
  （跟 CLI 渲染 PNG 用的是同一段 `_collect`），以 /api/poses 送給瀏覽器。
- 瀏覽器用 WebGL 把三角形畫出來，混色函式與預乘 alpha 照 Cocos2d-x 的設定，
  clipping 用 stencil。這一步就是遊戲裡 GPU 做的事，所以可以即時播放、任意縮放。
- /api/frame 另外提供 numpy 渲染器畫的 PNG，介面上的「伺服器渲染」開關切過去就能
  對照兩邊是否一致。
"""
from __future__ import annotations

import base64
import io
import json
import os
import socket
import threading
import time
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

from .animation import animation_events
from .loader import DocumentCache, failure_watch_paths, file_stamp, iter_skeletons
from .model import BLEND_MODES, ClippingAttachment, MeshAttachment, RegionAttachment, TRANSFORM_MODES
from .render import Renderer, RenderOptions, _collect, SlotReport, frame_to_time
from .uploads import PREFIX as UPLOAD_PREFIX, UploadStore
from .opened import PREFIX as OPEN_PREFIX, OpenedStore

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATES = ["drawn", "hidden", "empty", "alpha0", "inactive", "missingRegion",
           "clipping", "nonVisual"]


def _b64(arr: np.ndarray, dtype) -> str:
    return base64.b64encode(np.ascontiguousarray(arr, dtype=dtype).tobytes()).decode("ascii")


class _Cache:
    """PNG／JSON 回應的 LRU，同時限張數與總位元組（作法同 csdview）。"""

    def __init__(self, limit: int = 400, limit_bytes: int = 192 * 1024 * 1024):
        self.limit = limit
        self.limit_bytes = limit_bytes
        self._data: OrderedDict = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key, value: bytes) -> None:
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            self._data[key] = value
            self._bytes += len(value)
            while len(self._data) > 1 and (len(self._data) > self.limit
                                           or self._bytes > self.limit_bytes):
                _, dropped = self._data.popitem(last=False)
                self._bytes -= len(dropped)


class ViewerState:
    def __init__(self, root: str | None):
        # 開機序號放進所有版本參數：更新程式後重啟 serve，瀏覽器快取就全部失效
        self.boot = int(time.time())
        # root 可以是 None：不開資料夾、只用上傳功能
        self.root = os.path.abspath(root) if root else None
        self.uploads = UploadStore()
        self.opened = OpenedStore()
        self.docs = DocumentCache()
        self.cache = _Cache()
        self._renderers: dict = {}
        self._att_ids: dict = {}
        self._lock = threading.Lock()
        self._render_slots = threading.Semaphore(3)
        self._files_cache = None
        self._files_gen = 0
        self._files_lock = threading.Lock()

    def list_files(self, fresh: bool = False) -> list[dict]:
        now = time.monotonic()
        with self._files_lock:
            if not fresh and self._files_cache and now - self._files_cache[0] < 5.0:
                return self._files_cache[1]
            gen = self._files_gen
        entries = self.uploads.entries() + self.opened.entries()
        if self.root:
            for full in iter_skeletons(self.root):
                rel = os.path.relpath(full, self.root).replace("\\", "/")
                folder = os.path.dirname(rel) or "."
                name = os.path.basename(full)
                entries.append({"path": rel, "name": name, "dir": folder})
        with self._files_lock:
            # 掃描途中有人上傳／開啟／刪除（invalidate 過）時不寫回：這份清單已經過時了，
            # 寫回去的話接下來 5 秒內的請求都會拿到少了新檔案的清單
            if gen == self._files_gen:
                self._files_cache = (time.monotonic(), entries)
        return entries

    def invalidate_files(self) -> None:
        with self._files_lock:
            self._files_cache = None
            self._files_gen += 1

    def resolve(self, rel: str) -> str:
        if rel.startswith(UPLOAD_PREFIX):
            return self.uploads.resolve(rel)
        if rel.startswith(OPEN_PREFIX):
            return self.opened.resolve(rel)
        if not self.root:
            raise ValueError("沒有開啟資料夾，只能看上傳的檔案")
        full = os.path.abspath(os.path.join(self.root, rel))
        try:
            inside = os.path.commonpath([self.root, full]) == self.root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("路徑超出根目錄範圍")
        if not os.path.isfile(full):
            raise FileNotFoundError(rel)
        return full

    def document(self, full: str):
        return self.docs.get(full)

    def renderer(self, doc, options: RenderOptions) -> Renderer:
        key = (id(doc), options.key())
        with self._lock:
            hit = self._renderers.get(key)
            if hit is not None and hit.doc is doc:
                return hit
            if len(self._renderers) > 48:
                self._renderers.clear()
            made = Renderer(doc, options)
            self._renderers[key] = made
            return made

    def attachment_ids(self, doc) -> dict:
        """每個可畫附件一個整數 id；網頁端用它對應靜態的 UV 與三角形。"""
        with self._lock:
            hit = self._att_ids.get(id(doc))
            if hit is not None and hit[0] is doc:
                return hit[1]
        ids: dict = {}
        for _skin, _slot, _name, att in doc.data.iter_attachments():
            if isinstance(att, (RegionAttachment, MeshAttachment)) and id(att) not in ids:
                ids[id(att)] = (len(ids), att)
        with self._lock:
            if len(self._att_ids) > 64:
                self._att_ids.clear()
            self._att_ids[id(doc)] = (doc, ids)
        return ids


class Handler(BaseHTTPRequestHandler):
    state: ViewerState = None            # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",
                         "public, max-age=604800, immutable" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, payload, code: int = 200, cache: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", cache)

    def _from_this_page(self) -> bool:
        """只接受這個檢視器自己的網頁（或 curl 之類不帶 Origin 的本機程式）發來的請求。

        伺服器只聽 127.0.0.1，但瀏覽器裡的其他網站仍然打得到它：
        - DNS rebinding：惡意網域改解析成 127.0.0.1，瀏覽器就當成同源、讀得到回應
          （檔案清單、任意開啟本機骨架並讀貼圖）。這種請求的 Host 是對方的網域 → 擋 Host
        - 跨站 POST：任何網頁都能對 127.0.0.1 發 POST（讀不到回應，但會跳開檔視窗、塞上傳檔案）。
          瀏覽器發 POST 一定帶 Origin → Origin 不是自己就擋
        """
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if (self.headers.get("Host") or "").strip().lower() not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.strip().lower() in {"http://" + h for h in allowed}

    def _reject(self) -> None:
        # 請求本文沒讀；關掉連線，免得沒讀的本文被當成下一個請求
        self.close_connection = True
        self._json({"error": "拒絕：請求不是來自這個檢視器的網頁（請用 http://127.0.0.1:埠號/ 開啟）"}, 403)

    def do_GET(self) -> None:
        if not self._from_this_page():
            self._reject()
            return
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if route in ("/", "/index.html"):
                with open(os.path.join(_HERE, "web", "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            elif route == "/api/files":
                self._json({"root": self.state.root,
                            "files": self.state.list_files(query.get("fresh") == "1")})
            elif route == "/api/info":
                self._json(self._info(query))
            elif route == "/api/stat":
                self._json(self._stat(query))
            elif route == "/api/texture":
                self._texture(query)
            elif route == "/api/meshes":
                self._meshes(query)
            elif route == "/api/poses":
                self._poses(query)
            elif route == "/api/frame":
                self._frame(query)
            elif route == "/api/slot":
                self._json(self._slot(query))
            elif route == "/api/region":
                self._region(query)
            else:
                self._json({"error": "not found"}, 404)
        except FileNotFoundError as exc:
            self._json({"error": f"找不到檔案: {exc}"}, 404)
        except ValueError as exc:
            self._json(self._error_payload(exc, str(exc)), 400)
        except Exception as exc:          # 讀檔失敗也要回 JSON，前端才能把原因顯示出來
            self._json(self._error_payload(exc, f"{type(exc).__name__}: {exc}"), 500)

    def _error_payload(self, exc, message: str) -> dict:
        """錯誤回應；骨架讀不進來時附上讀取前的指紋（與 /api/stat 同一種格式），
        網頁才能繼續盯著檔案、修好後自動重新載入。"""
        payload = {"error": message}
        stamp = getattr(exc, "spineview_stamp", None)
        if stamp is not None:
            payload["stamp"] = _format_stamp(stamp)
            payload["boot"] = self.state.boot
        return payload

    def do_POST(self) -> None:
        if not self._from_this_page():
            self._reject()
            return
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        store = self.state.uploads
        try:
            if route == "/api/upload/file":
                self._json(store.add_file(query.get("id", ""), query.get("name", ""), self.rfile, length))
                return
            self._drain(length)
            if route == "/api/open/dialog":
                chosen = self.state.opened.ask()
                if chosen is None:
                    self._json({"cancelled": True})
                else:
                    entry = self.state.opened.add(chosen)
                    self.state.invalidate_files()
                    self._json({"file": entry["path"], "title": entry["title"]})
            elif route == "/api/open/path":
                entry = self.state.opened.add(query.get("path", ""))
                self.state.invalidate_files()
                self._json({"file": entry["path"], "title": entry["title"]})
            elif route == "/api/open/remove":
                self.state.opened.remove(query.get("id", ""))
                self.state.invalidate_files()
                self._json({"ok": True})
            elif route == "/api/upload/new":
                self._json({"id": store.create()})
            elif route == "/api/upload/done":
                files = store.finish(query.get("id", ""))
                self.state.invalidate_files()
                self._json({"files": files})
            elif route == "/api/upload/remove":
                store.remove(query.get("id", ""))
                self.state.invalidate_files()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _drain(self, length: int) -> None:
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            remaining -= len(chunk)

    # ------------------------------------------------------------ API
    def _doc(self, query):
        full = self.state.resolve(query.get("file", ""))
        return full, self.state.document(full)

    def _stamp(self, doc) -> str:
        # 讀取前的指紋（見 Document.read_stamp）。不能在這裡重新問檔案系統：
        # 拿到 doc 之後檔案若被換掉，重新問到的是新指紋、doc 卻是舊內容，
        # 舊內容會被標成新版本快取起來，網頁也會以為自己是最新的而不再重載。
        return doc.version

    def _check_version(self, query, doc) -> bool:
        """網址帶的版本（v=版本-開機序號）跟目前的 doc 不同時回 409。

        這幾個回應被標成可長期快取，網址就是版本；用新內容回應舊版本的網址，
        瀏覽器會把錯的內容記在那個網址底下。回 409 讓網頁知道要重新載入。
        """
        want = query.get("v")
        current = f"{doc.version}-{self.state.boot}"
        if want is not None and want != current:
            self._json({"error": "檔案在載入途中被更新了，請重新載入", "stale": True,
                        "stamp": doc.version}, 409)
            return False
        return True

    def _info(self, query) -> dict:
        full, doc = self._doc(query)
        data = doc.data
        renderer = self.state.renderer(doc, RenderOptions(layout_only=True))
        fps = doc.fps
        anims = []
        for a in data.animations:
            anims.append({
                "name": a.name, "duration": round(a.duration, 4),
                "frames": int(round(a.duration * fps)) + 1,
                "events": [e.to_dict() for e in animation_events(a)],
            })
        # 任何造型裡放過 clipping 附件的插槽：網頁「只看這個插槽」時不能把這些遮罩藏掉
        clip_slots = {slot for skin in [*data.skins, data.defaultSkin] if skin is not None
                      for (slot, _name), att in skin.attachments.items()
                      if isinstance(att, ClippingAttachment)}
        pages = []
        if doc.atlas is not None:
            for i, page in enumerate(doc.atlas.pages):
                pages.append({"index": i, "name": page.name, "width": page.width,
                              "height": page.height, "pma": page.pma,
                              "exists": os.path.isfile(page.path)})
        return {
            "file": query.get("file", ""), "name": doc.name, "path": full,
            "version": data.version, "format": data.format, "fps": fps,
            "size": [round(data.width, 2), round(data.height, 2)],
            "animations": anims,
            "skins": [s.name for s in data.skins if s is not None],
            "defaultSkin": renderer.default_skin_name(),
            "bones": [{"name": b.name, "parent": b.parent.index if b.parent else -1,
                       "length": round(b.length, 2),
                       "transform": TRANSFORM_MODES[b.transformMode]
                       if b.transformMode < len(TRANSFORM_MODES) else str(b.transformMode)}
                      for b in data.bones],
            "slots": [{"name": s.name, "bone": s.bone.index,
                       "blend": BLEND_MODES[s.blendMode] if s.blendMode < 4 else str(s.blendMode),
                       "dark": s.darkColor is not None, "clip": s.index in clip_slots}
                      for s in data.slots],
            "constraints": {"ik": len(data.ikConstraints),
                            "transform": len(data.transformConstraints),
                            "path": len(data.pathConstraints)},
            "events": [e.name for e in data.events],
            "atlas": self._display_path(doc.atlas_path, doc.path),
            "uploaded": query.get("file", "").startswith(UPLOAD_PREFIX),
            "opened": query.get("file", "").startswith(OPEN_PREFIX),
            "pages": pages,
            "warnings": doc.warnings,
            "missingRegions": doc.missing_regions,
            "stamp": self._stamp(doc), "boot": self.state.boot,
        }

    def _display_path(self, path, skeleton=None):
        if not path:
            return None
        base = self.state.uploads.folder_of(path) or self.state.root
        if skeleton and (not base or not os.path.abspath(skeleton).startswith(os.path.abspath(base))):
            base = os.path.dirname(skeleton)     # 「開啟」的檔案：以骨架檔所在資料夾為基準
        try:
            return os.path.relpath(path, base).replace("\\", "/") if base else os.path.basename(path)
        except ValueError:
            return os.path.basename(path)

    def _stat(self, query) -> dict:
        """只回指紋給前端輪詢：骨架、圖集、貼圖任何一個被換掉都算改動。

        不重新解析——用上次解析時記下的相依檔清單，再加上骨架檔本身。
        """
        full = self.state.resolve(query.get("file", ""))
        try:
            files = self.state.document(full).watch_paths()
        except Exception:
            # 讀不進來：盯的路徑要和讀檔失敗時記下的指紋（錯誤回應裡的 stamp）一致，才比得出有沒有修好。
            # 失敗有快取，這裡不會每次輪詢都重新解析壞檔
            files = failure_watch_paths(full)
        return {"stamp": _format_stamp(file_stamp(files)), "boot": self.state.boot}

    def _texture(self, query) -> None:
        _full, doc = self._doc(query)
        if not self._check_version(query, doc):
            return
        index = int(query.get("page", 0))
        if doc.atlas is None or not 0 <= index < len(doc.atlas.pages):
            raise FileNotFoundError(f"page {index}")
        path = doc.atlas.pages[index].path
        # 只送圖片：貼圖檔名來自圖集內容（可能是上傳的），寫成 ../../別的檔案 時不能把那個檔案原樣送出去。
        # 不擋 ../ 本身——真的專案偶爾會把貼圖放在圖集的上一層
        ext = os.path.splitext(path)[1].lower()
        kind = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp"}.get(ext)
        if kind is None:
            raise ValueError(f"貼圖不是圖片檔：{os.path.basename(path)}")
        with open(path, "rb") as fh:
            body = fh.read()
        self._send(200, body, kind, cache=True)

    def _meshes(self, query) -> None:
        """所有可畫附件的靜態資料：在哪一頁貼圖、UV、三角形。整份骨架只要抓一次。"""
        _full, doc = self._doc(query)
        if not self._check_version(query, doc):
            return
        pages = {id(p): i for i, p in enumerate(doc.atlas.pages)} if doc.atlas else {}
        out = []
        for _key, (n, att) in sorted(self.state.attachment_ids(doc).items(),
                                     key=lambda kv: kv[1][0]):
            region = att.region
            if isinstance(att, RegionAttachment):
                tris = np.array([3, 0, 1, 1, 2, 3])
            else:
                tris = np.asarray(att.triangles)
            out.append({"id": n, "name": att.name,
                        "page": pages.get(id(region.page), -1) if region is not None else -1,
                        "uv": _b64(att.uvs, "<f4"), "tri": _b64(tris, "<u2")})
        self._json({"stamp": self._stamp(doc), "attachments": out}, cache=True)

    def _poses(self, query) -> None:
        """一段影格的幾何資料。frames=起-迄（含）。"""
        _full, doc = self._doc(query)
        stamp = self._stamp(doc)
        key = ("poses", query.get("file"), stamp, tuple(sorted(query.items())))
        cached = self.state.cache.get(key)
        if cached is None:
            renderer = self.state.renderer(doc, RenderOptions(layout_only=True))
            anim_name = query.get("anim") or None
            animation = renderer.animation(anim_name)
            skin_name = query.get("skin", "__default__")
            if skin_name == "__default__":
                skin_name = renderer.default_skin_name()
            skin_name = skin_name or None
            skin = renderer.skin(skin_name)
            hidden = set(filter(None, (query.get("hide") or "").split("|")))
            start, _, end = (query.get("frames") or "0-0").partition("-")
            start = int(start or 0)
            end = int(end or start)
            fps = doc.fps
            last = int(round(animation.duration * fps)) if animation is not None else 0
            end = min(end, last)
            start = max(0, min(start, end))
            ids = self.state.attachment_ids(doc)
            viewport = renderer.content_bounds(animation, skin_name, set(), None)
            events = animation_events(animation) if animation is not None else []
            frames = []
            sk = renderer.new_skeleton(skin)     # 這一段影格共用同一個骨架（只在這個請求裡）
            lengths = np.array([b.length for b in doc.data.bones], dtype=np.float64)
            for f in range(start, end + 1):
                t = frame_to_time(f, fps, animation.duration) if animation is not None else 0.0
                renderer.pose(animation, t, skin, sk)
                reports: list[SlotReport] = []
                items = _collect(sk, hidden, None, reports, bounds=False)
                clips, clip_index, draws = [], {}, []
                for it in items:
                    k = -1
                    if it.clip is not None:
                        pid = id(it.clip[1])
                        if pid not in clip_index:
                            clip_index[pid] = len(clips)
                            clips.append(_b64(it.clip[1], "<f4"))
                        k = clip_index[pid]
                    entry = ids.get(id(it.att))
                    if entry is None:
                        continue
                    r, g, b, a = it.color
                    draws.append([entry[0], it.slot.data.index, it.blend,
                                  [round(r * a, 4), round(g * a, 4), round(b * a, 4), round(a, 4)],
                                  [round(c, 4) for c in it.dark] if it.dark is not None else None,
                                  k, _b64(it.xy, "<f4")])
                # 骨骼：每根 [x, y, 末端x, 末端y]，float32 base64（和頂點同一種格式）；
                # 一次用 numpy 算完，比逐根 round() 成 JSON 數字快得多，精度也更高
                ba, _bb, bc, _bd, wx, wy = sk.bone_arrays()
                bone_xy = np.stack([wx, wy, lengths * ba + wx, lengths * bc + wy], axis=1)
                inactive = [i for i, bone in enumerate(sk.bones) if not bone.active]
                slots = [[rep.index, rep.attachment, _STATES.index(rep.state)]
                         for rep in reports]
                half = 0.5 / fps
                fired = [e.to_dict() for e in events if abs(e.time - t) <= half + 1e-6]
                frames.append({"frame": f, "time": round(t, 4), "draws": draws,
                               "clips": clips, "bones": _b64(bone_xy, "<f4"),
                               "inactiveBones": inactive, "slots": slots, "events": fired})
            payload = {"stamp": stamp, "viewport": viewport, "states": _STATES,
                       "skin": skin_name, "frames": frames}
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.state.cache.put(key, body)
            cached = body
        self._send(200, cached, "application/json; charset=utf-8")

    def _slot(self, query) -> dict:
        """一個插槽在這一格的完整屬性（與 CLI 的 spec --slot 同一段程式）。"""
        from .slotinfo import slot_detail
        _full, doc = self._doc(query)
        renderer = self.state.renderer(doc, RenderOptions(layout_only=True))
        frame = int(query.get("frame", 0))
        skin = query.get("skin", "__default__")
        found: list = []
        # 網頁用 index 指定（插槽名稱可能剛好是數字，例如叫 "3"，用名字找會和第 3 個插槽搞混）
        if "index" in query:
            target = int(query["index"])
            if not 0 <= target < len(doc.data.slots):
                raise ValueError(f"沒有第 {target} 個插槽（共 {len(doc.data.slots)} 個）")
        else:
            target = query.get("slot", "")
        detail = slot_detail(renderer, query.get("anim") or None, frame_to_time(frame, doc.fps),
                             skin if skin != "" else None, target, found)
        # 網頁要用 id 去抓貼圖預覽
        att = found[0] if found else None
        entry = self.state.attachment_ids(doc).get(id(att)) if att is not None else None
        detail["attachmentId"] = entry[0] if entry else None
        detail["stamp"] = self._stamp(doc)
        return detail

    def _region(self, query) -> None:
        """附件在圖集裡的那一塊，切出來轉正的 PNG。"""
        from .slotinfo import region_image
        _full, doc = self._doc(query)
        if not self._check_version(query, doc):
            return
        want = int(query.get("att", -1))
        att = next((a for n, a in self.state.attachment_ids(doc).values() if n == want), None)
        body = region_image(doc, getattr(att, "region", None)) if att is not None else None
        if body is None:
            raise FileNotFoundError(f"附件 {want} 沒有圖集區塊")
        self._send(200, body, "image/png", cache=True)

    def _frame(self, query) -> None:
        """numpy 渲染器畫的 PNG（與 CLI 的 render 完全相同的程式）。"""
        full, doc = self._doc(query)
        stamp = self._stamp(doc)
        key = ("png", full, stamp, tuple(sorted(query.items())))
        cached = self.state.cache.get(key)
        if cached is None:
            with self.state._render_slots:
                cached = self.state.cache.get(key)
                if cached is None:
                    options = RenderOptions(
                        scale=float(query.get("scale", 1.0)),
                        background=query.get("bg", "checker"),
                        bones=query.get("bones") == "1",
                        annotate=query.get("annotate") == "1",
                        bboxes=query.get("bboxes") == "1",
                        tint_black=query.get("tint") == "1",
                        ss=int(query.get("ss", 1)),
                        padding=int(query.get("pad", 16)),
                        max_pixels=int(query.get("maxpx", 6_000_000)))
                    renderer = self.state.renderer(doc, options)
                    anim = query.get("anim") or None
                    skin = query.get("skin", "__default__")
                    hidden = set(filter(None, (query.get("hide") or "").split("|")))
                    frame = int(query.get("frame", 0))
                    time_ = frame_to_time(frame, doc.fps)
                    result = renderer.render(anim, time_, skin if skin != "" else None,
                                             hidden=hidden, frame=frame)
                    buffer = io.BytesIO()
                    result.image.save(buffer, "PNG")
                    cached = buffer.getvalue()
                    self.state.cache.put(key, cached)
        self._send(200, cached, "image/png", cache=True)


def _format_stamp(stamp: tuple) -> str:
    """指紋 → 字串（和 Document.version 同一種寫法）。"""
    return "-".join(f"{m}.{s}" for m, s in stamp)


class _Server(ThreadingHTTPServer):
    """獨佔埠號的 HTTP server（Windows 用 SO_EXCLUSIVEADDRUSE，理由同 csdview）。

    預設的 SO_REUSEADDR 在 Windows 上允許兩個行程綁同一個埠：第二個不會報錯，
    請求卻全被第一個接走，畫面變成別人的專案。所以讓第二次綁定直接失敗、往上換埠。
    """

    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _bind(port: int, tries: int = 20):
    last = None
    for offset in range(tries):
        try:
            return _Server(("127.0.0.1", port + offset), Handler), port + offset
        except OSError as exc:
            last = exc
    raise SystemExit(f"錯誤: {port}~{port + tries - 1} 都被佔用了（{last}）。用 --port 指定別的埠。")


def serve(root: str | None = ".", port: int = 8780, open_browser: bool = True,
          initial: str | None = None) -> None:
    Handler.state = ViewerState(root)
    server, actual = _bind(port)
    url = f"http://127.0.0.1:{actual}/"
    if initial:
        from urllib.parse import quote
        url += "#" + quote(initial)
    print(f"spineview 網頁檢視器已啟動：http://127.0.0.1:{actual}/")
    if actual != port:
        print(f"（{port} 已經有人在用了，改用 {actual}）")
    print(f"瀏覽目錄：{Handler.state.root or '（未指定，只用網頁上的「上傳」）'}")
    print("按 Ctrl+C 結束。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
