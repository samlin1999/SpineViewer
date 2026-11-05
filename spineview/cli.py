"""spineview 指令列。每個子指令都支援 --json（stdout 是乾淨的 JSON，進度走 stderr）。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from . import __version__
from .animation import animation_events
from .loader import DEFAULT_FPS, iter_skeletons, load
from .model import (BLEND_MODES, ClippingAttachment, MeshAttachment,
                    PathAttachment, RegionAttachment, TRANSFORM_MODES)
from .reader_binary import SkeletonFormatError
from .render import Renderer, RenderOptions, RenderResult, frame_to_time

_REGISTRY = os.environ.get("SPINEVIEW_PROJECTS") or os.path.join(
    os.path.expanduser("~"), ".spineview_projects")
_UI_REMEMBER = os.path.join(os.path.expanduser("~"), ".spineview_ui")


# ---------------------------------------------------------------- 共用

def _add_common(p: argparse.ArgumentParser, render: bool = True, frame: bool = True) -> None:
    p.add_argument("file", help=".skel 或 Spine .json")
    p.add_argument("--atlas", help="指定圖集（預設找同主檔名的 .atlas）")
    p.add_argument("--json", action="store_true", help="輸出 JSON")
    p.add_argument("--pretty", action="store_true", help="JSON 加縮排（給人看）")
    if frame:
        p.add_argument("--anim", "-a", default="", help="動畫名稱（不給就是 setup pose）")
        p.add_argument("--frame", "-f", type=int, help="動畫的第幾格（依 fps，從 0 起算）")
        p.add_argument("--time", "-t", type=float, help="動畫的第幾秒（與 --frame 二選一）")
        p.add_argument("--skin", "-s", help="造型；多個用 + 組合，例如 body+hat")
        p.add_argument("--fps", type=float, help="影格率（預設用檔案記錄的，沒有就 30）")
        p.add_argument("--hide", action="append", default=[], help="隱藏插槽（可重複）")
        p.add_argument("--only", action="append", default=[], help="只畫這些插槽（可重複）")
    if render:
        p.add_argument("-o", "--out", help="輸出路徑（預設 out/ 底下自動命名）")
        p.add_argument("--scale", type=float, default=1.0, help="縮放倍率（1 = 骨架 1 單位 = 1 像素）")
        p.add_argument("--bg", default="checker", help="背景：checker / none / #rrggbb")
        p.add_argument("--pad", type=int, default=16, help="四周留白（像素）")
        p.add_argument("--bones", action="store_true", help="疊上骨骼與世界原點")
        p.add_argument("--annotate", action="store_true", help="疊上插槽外框與名稱")
        p.add_argument("--bboxes", action="store_true", help="疊上 bounding box／clipping 多邊形")
        p.add_argument("--ss", type=int, default=1, help="超取樣倍率，2 會讓邊緣平滑（慢約 4 倍）")
        p.add_argument("--tint-black", action="store_true",
                       help="套用 dark color（遊戲引擎預設不開 two color tint）")
        p.add_argument("--bounds", default="auto", choices=["auto", "anim", "frame", "setup", "data"],
                       help="取景：anim=整段動畫的聯集（預設，有 --anim 時）、frame=只看這一格、"
                            "setup=setup pose、data=編輯器記錄的尺寸")
        p.add_argument("--viewport", help="直接指定世界座標取景 x,y,w,h（Y 向上）")
        p.add_argument("--max-pixels", type=int, default=16_000_000, help="畫布像素上限，超過自動降尺度")
        p.add_argument("--threads", type=int, default=0,
                       help="光柵化執行緒數（0 = 自動：大畫布切成橫條平行畫，結果與單執行緒逐像素相同）")


def _options(args, **override) -> RenderOptions:
    viewport = None
    if getattr(args, "viewport", None):
        parts = [float(v) for v in args.viewport.split(",")]
        if len(parts) != 4:
            raise SystemExit("錯誤: --viewport 要給 x,y,w,h 四個數字")
        viewport = tuple(parts)
    opts = RenderOptions(
        scale=getattr(args, "scale", 1.0), background=getattr(args, "bg", "checker"),
        padding=getattr(args, "pad", 16), bones=getattr(args, "bones", False),
        annotate=getattr(args, "annotate", False), bboxes=getattr(args, "bboxes", False),
        ss=getattr(args, "ss", 1), tint_black=getattr(args, "tint_black", False),
        bounds=getattr(args, "bounds", "auto"), viewport=viewport,
        max_pixels=getattr(args, "max_pixels", 16_000_000), threads=getattr(args, "threads", 0))
    for k, v in override.items():
        setattr(opts, k, v)
    return opts


def _load(args):
    try:
        doc = load(args.file, getattr(args, "atlas", None))
    except FileNotFoundError:
        raise SystemExit(f"錯誤: 找不到檔案 {args.file}")
    except SkeletonFormatError as exc:
        raise SystemExit(f"錯誤: {args.file} 讀不進來：{exc}")
    if getattr(args, "fps", None):
        doc.data.fps = args.fps
    return doc


def _skin_arg(args):
    skin = getattr(args, "skin", None)
    return "__default__" if skin is None else skin


def _frame_time(args, doc, renderer) -> tuple[float, int | None]:
    """回傳 (秒, 影格)。--frame 是「這段動畫的第幾格」。"""
    if args.time is not None and args.frame is not None:
        # 兩個都給時以前會默默用 --time，agent 拿到的不是它以為的那一格
        raise SystemExit("錯誤: --frame 與 --time 只能給一個")
    anim = renderer.animation(args.anim or None)
    fps = doc.fps
    if anim is None:
        if args.frame or args.time:
            _note("setup pose 沒有影格，--frame／--time 不起作用（要看動畫請加 --anim）")
        return 0.0, None
    if args.time is not None:
        t = max(0.0, min(args.time, anim.duration))
        if t != args.time:
            _note(f"--time {args.time:g} 超出這段動畫的範圍（0–{anim.duration:g} 秒），改用 {t:g} 秒")
        return t, int(round(t * fps))
    frame = args.frame or 0
    last = int(round(anim.duration * fps))
    wanted = frame
    if frame < 0:
        frame = last + 1 + frame       # -1 = 最後一格
    frame = max(0, min(frame, last))
    if wanted is not None and not (-(last + 1) <= wanted <= last):
        _note(f"--frame {wanted} 超出這段動畫的範圍（0–{last}，負數從尾巴數），改用第 {frame} 格")
    return frame_to_time(frame, fps, anim.duration), frame


def _note(text: str) -> None:
    """給呼叫端的提醒：寫到 stderr，--json 時 stdout 仍是乾淨的 JSON。"""
    print(f"注意: {text}", file=sys.stderr)


def _slim(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _slim(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_slim(v) for v in value]
    return value


def _json_text(payload, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(_slim(payload), ensure_ascii=False, indent=2)
    return json.dumps(_slim(payload), ensure_ascii=False, separators=(",", ":"))


def _emit(payload: dict, as_json: bool, lines: list[str], pretty: bool = False) -> None:
    if as_json:
        print(_json_text(payload, pretty))
    else:
        print("\n".join(lines))


def _progress(done: int, total: int, label: str) -> None:
    if total <= 1:
        return
    sys.stderr.write(f"\r{label} {done}/{total}")
    if done >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


# ---- 輸出路徑（同 csdview：參數雜湊入檔名，多個 agent 並行不會互蓋）
_RENDER_ARGS = ("anim", "frame", "time", "skin", "fps", "hide", "only", "scale", "bg", "pad",
                "bones", "annotate", "bboxes", "ss", "tint_black", "bounds", "viewport",
                "max_pixels", "count", "cols", "step", "atlas", "labels")


def _out_dir() -> str:
    return os.environ.get("SPINEVIEW_OUT") or "out"


def _option_tag(args) -> str:
    parts = []
    for name in _RENDER_ARGS:
        if hasattr(args, name):
            value = getattr(args, name)
            if isinstance(value, list):
                value = ",".join(str(v) for v in value)
            parts.append(f"{name}={value}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def _default_out(path: str, animation: str, frame, args, suffix: str = "", ext: str = ".png") -> str:
    base = os.path.basename(path)
    for e in (".skel.bytes", ".skel", ".json"):
        if base.lower().endswith(e):
            base = base[: -len(e)]
            break
    parts = [base]
    if animation:
        parts.append(animation.replace(" ", "_").replace("/", "_"))
    if frame is not None:
        parts.append(f"f{frame}")
    parts.append(_option_tag(args))
    return os.path.join(_out_dir(), "_".join(parts) + suffix + ext)


def _atomic_save(save, out: str) -> None:
    """先寫暫存檔再改名：別的 agent 不會讀到寫到一半的圖。

    暫存檔的副檔名跟目標相同（PIL 依副檔名決定格式）。Windows 上目標若正被別的行程讀著，
    換檔會暫時失敗，由 replace_with_retry 重試。
    """
    from .locking import atomic_write
    atomic_write(out, save, suffix=os.path.splitext(out)[1])


def _save_image(image, out: str, **kwargs) -> None:
    if out.lower().endswith((".jpg", ".jpeg")):
        _atomic_save(lambda p: image.convert("RGB").save(p, quality=92, **kwargs), out)
    else:
        _atomic_save(lambda p: image.save(p, **kwargs), out)


def _result_payload(doc, result: RenderResult, out: str | None) -> dict:
    drawn = [s for s in result.slots if s.state == "drawn"]
    payload = {
        "file": doc.path, "animation": result.animation or None, "skin": result.skin,
        "frame": result.frame, "time": round(result.time, 4), "fps": doc.fps,
        "size": list(result.size), "scale": round(result.scale, 4), "clamped": result.clamped,
        # 世界原點在圖上的位置 = 遊戲裡 SkeletonAnimation 節點的位置
        "origin": list(result.origin),
        "viewport": [round(v, 2) for v in result.viewport],
        "slotsDrawn": len(drawn), "slotsTotal": len(result.slots),
        "eventsAtFrame": result.events,
        "missingRegions": len(result.missing),
    }
    if out:
        payload["image"] = os.path.abspath(out)
    return payload


# ---------------------------------------------------------------- info

def cmd_info(args) -> int:
    doc = _load(args)
    data = doc.data
    renderer = Renderer(doc, RenderOptions(layout_only=True))
    fps = doc.fps
    animations = []
    for a in data.animations:
        kinds: dict[str, int] = {}
        for tl in a.timelines:
            kinds[tl.kind] = kinds.get(tl.kind, 0) + 1
        animations.append({"name": a.name, "duration": round(a.duration, 4),
                           "frames": int(round(a.duration * fps)) + 1,
                           "events": [e.to_dict() for e in animation_events(a)],
                           "timelines": kinds})
    skins = []
    for s in data.skins:
        if s is None:
            continue
        kinds: dict[str, int] = {}
        for att in s.attachments.values():
            kinds[att.kind] = kinds.get(att.kind, 0) + 1
        skins.append({"name": s.name, "attachments": len(s.attachments), "types": kinds})
    blends: dict[str, int] = {}
    for slot in data.slots:
        name = BLEND_MODES[slot.blendMode] if slot.blendMode < 4 else str(slot.blendMode)
        blends[name] = blends.get(name, 0) + 1
    default_skin = renderer.default_skin_name()
    payload = {
        "file": doc.path, "version": data.version, "format": data.format,
        "fps": fps, "fpsFromFile": bool(data.fps),
        "editorSize": [round(data.width, 2), round(data.height, 2)],
        "editorOrigin": [round(data.x, 2), round(data.y, 2)],
        "bones": len(data.bones), "slots": len(data.slots), "slotBlendModes": blends,
        "constraints": {"ik": len(data.ikConstraints), "transform": len(data.transformConstraints),
                        "path": len(data.pathConstraints)},
        "skins": skins, "skinUsedByDefault": default_skin,
        "animations": animations,
        "events": [e.name for e in data.events],
        "atlas": doc.atlas_path, "atlasGuessed": doc.atlas_guessed,
        "pages": [{"name": p.name, "size": [p.width, p.height], "exists": os.path.isfile(p.path)}
                  for p in (doc.atlas.pages if doc.atlas else [])],
        "missingRegions": len(doc.missing_regions),
        "warnings": doc.warnings,
    }
    lines = [f"{doc.name}  Spine {data.version}（{data.format}）  {fps:g} fps",
             f"  骨骼 {len(data.bones)}、插槽 {len(data.slots)}、約束 IK {len(data.ikConstraints)} / "
             f"Transform {len(data.transformConstraints)} / Path {len(data.pathConstraints)}",
             f"  造型: " + ", ".join(f"{s['name']}({s['attachments']})" for s in skins),
             f"  圖集: {doc.atlas_path or '（找不到）'}"]
    if default_skin:
        lines.append(f"  沒指定 --skin 時用: {default_skin}（default 造型能放的附件太少，改用第一個具名造型）")
    lines.append(f"  動畫 {len(animations)} 段：")
    for a in animations:
        ev = f"，事件 {len(a['events'])}" if a["events"] else ""
        lines.append(f"    {a['name']:<24} {a['duration']:.3f}s  {a['frames']} 格{ev}")
    if doc.missing_regions:
        lines.append(f"  ✖ 圖集缺 {len(doc.missing_regions)} 個 region（跑 doctor 看細節）")
    for w in doc.warnings:
        lines.append(f"  ⚠ {w}")
    _emit(payload, args.json, lines, args.pretty)
    return 0


# ---------------------------------------------------------------- render

def cmd_render(args) -> int:
    doc = _load(args)
    renderer = Renderer(doc, _options(args))
    t, frame = _frame_time(args, doc, renderer)
    result = renderer.render(args.anim or None, t, _skin_arg(args), args.hide, args.only or None,
                             frame=frame)
    out = args.out or _default_out(doc.path, args.anim, frame, args)
    _save_image(result.image, out)
    payload = _result_payload(doc, result, out)
    lines = [f"已輸出 {os.path.abspath(out)}  ({result.size[0]}×{result.size[1]})",
             f"  動畫 {result.animation or '(setup pose)'}  第 {frame if frame is not None else 0} 格"
             f"（{result.time:.3f}s）  造型 {result.skin or '(default)'}",
             f"  畫了 {payload['slotsDrawn']}/{payload['slotsTotal']} 個插槽，世界原點在圖上 {payload['origin']}"]
    if result.clamped:
        lines.append(f"  ⚠ 畫布太大，已自動降到 scale {result.scale:.3f}")
    if result.missing:
        lines.append(f"  ✖ 圖集缺 {len(result.missing)} 個 region，那些附件沒畫出來（跑 doctor）")
    _emit(payload, args.json, lines, args.pretty)
    return 0


def _render_many(renderer, doc, frames: list[int], args, label: str, jobs: int | None):
    """平行渲染多格（numpy 的大部分運算會釋放 GIL）。"""
    anim = args.anim or None
    fps = doc.fps
    duration = renderer.animation(anim).duration if anim else 0.0
    # 先把整段動畫的取景算好，各執行緒才不會重複算
    if anim:
        renderer.content_bounds(renderer.animation(anim), renderer.default_skin_name()
                                if getattr(args, "skin", None) is None else args.skin,
                                set(args.hide), set(args.only) if args.only else None)
    if jobs is None:
        jobs = min(4, os.cpu_count() or 1)
    results: list = [None] * len(frames)
    done = [0]
    progress_lock = threading.Lock()

    def one(i):
        f = frames[i]
        results[i] = renderer.render(anim, frame_to_time(f, fps, duration), _skin_arg(args), args.hide,
                                     args.only or None, frame=f)
        with progress_lock:          # += 不是原子操作，多執行緒下會掉數
            done[0] += 1
            _progress(done[0], len(frames), label)

    if jobs <= 1 or len(frames) <= 1:
        for i in range(len(frames)):
            one(i)
    else:
        with ThreadPoolExecutor(jobs) as pool:
            list(pool.map(one, range(len(frames))))
    return results


def _anim_frames(args, doc, renderer) -> list[int]:
    anim = renderer.animation(args.anim or None)
    if anim is None:
        raise SystemExit("錯誤: 這個指令需要 --anim（用 info 看有哪些動畫）")
    last = int(round(anim.duration * doc.fps))
    return list(range(0, last + 1))


def cmd_sheet(args) -> int:
    from PIL import Image, ImageDraw
    doc = _load(args)
    renderer = Renderer(doc, _options(args))
    all_frames = _anim_frames(args, doc, renderer)
    count = max(1, min(args.count, len(all_frames)))
    if count == 1:
        picks = [all_frames[0]]
    else:
        picks = [all_frames[int(round(i * (len(all_frames) - 1) / (count - 1)))] for i in range(count)]
    results = _render_many(renderer, doc, picks, args, "sheet", args.jobs)
    w, h = results[0].size
    cols = args.cols or min(count, max(1, int(math.ceil(math.sqrt(count)))))
    rows = int(math.ceil(count / cols))
    label = 18 if args.labels else 0
    sheet = Image.new("RGBA", (cols * w, rows * (h + label)), (22, 22, 26, 255))
    draw = ImageDraw.Draw(sheet)
    for i, res in enumerate(results):
        x, y = (i % cols) * w, (i // cols) * (h + label)
        sheet.alpha_composite(res.image, (x, y + label))
        if label:
            draw.text((x + 4, y + 3), f"#{res.frame}  {res.time:.2f}s", fill=(150, 200, 255, 255))
    out = args.out or _default_out(doc.path, args.anim, None, args, "_sheet")
    _save_image(sheet, out)
    payload = {"file": doc.path, "animation": args.anim, "image": os.path.abspath(out),
               "frames": picks, "times": [round(r.time, 4) for r in results],
               "cellSize": [w, h], "grid": [cols, rows], "fps": doc.fps,
               "origin": list(results[0].origin)}
    _emit(payload, args.json, [f"已輸出 {os.path.abspath(out)}（{count} 格，{cols}×{rows}）"], args.pretty)
    return 0


def cmd_frames(args) -> int:
    doc = _load(args)
    renderer = Renderer(doc, _options(args))
    frames = _anim_frames(args, doc, renderer)[::max(1, args.step)]
    folder = args.out or os.path.join(_out_dir(), f"{doc.name}_{args.anim}_{_option_tag(args)}")
    results = _render_many(renderer, doc, frames, args, "frames", args.jobs)
    paths = []
    for res in results:
        path = os.path.join(folder, f"{doc.name}_{args.anim}_{res.frame:04d}.png")
        _save_image(res.image, path)
        paths.append(os.path.abspath(path))
    payload = {"file": doc.path, "animation": args.anim, "folder": os.path.abspath(folder),
               "frames": frames, "images": paths, "size": list(results[0].size), "fps": doc.fps}
    _emit(payload, args.json, [f"已輸出 {len(paths)} 張到 {os.path.abspath(folder)}"], args.pretty)
    return 0


def cmd_gif(args) -> int:
    doc = _load(args)
    renderer = Renderer(doc, _options(args, background=args.bg if args.bg != "checker" else "#202024"))
    frames = _anim_frames(args, doc, renderer)[::max(1, args.step)]
    results = _render_many(renderer, doc, frames, args, "gif", args.jobs)
    images = [r.image.convert("RGB") for r in results]
    delay = int(round(1000 * max(1, args.step) / doc.fps))
    out = args.out or _default_out(doc.path, args.anim, None, args, "", ".gif")
    _atomic_save(lambda p: images[0].save(p, save_all=True, append_images=images[1:],
                                          duration=delay, loop=0, disposal=2), out)
    payload = {"file": doc.path, "animation": args.anim, "image": os.path.abspath(out),
               "frames": len(images), "delayMs": delay,
               "note": "GIF 的延遲單位是 1/100 秒，播放速度是近似值；逐格比對請用 frames 或 sheet。"}
    _emit(payload, args.json, [f"已輸出 {os.path.abspath(out)}（{len(images)} 格，每格 {delay}ms）"], args.pretty)
    return 0


# ---------------------------------------------------------------- tree / spec / find

def cmd_tree(args) -> int:
    doc = _load(args)
    renderer = Renderer(doc, _options(args, layout_only=True))
    t, frame = _frame_time(args, doc, renderer)
    result = renderer.render(args.anim or None, t, _skin_arg(args), args.hide, args.only or None,
                             frame=frame)
    roots = [b for b in result.bones if b.parent is None]
    if args.bone:
        roots = [b for b in result.bones if b.name == args.bone]
        if not roots:
            raise SystemExit(f"錯誤: 找不到骨骼 {args.bone}")

    def cut(rep, depth):
        d = rep.to_dict()
        if args.depth is None or depth < args.depth:
            d["children"] = [cut(c, depth + 1) for c in rep.children]
        elif rep.children:
            d["childrenOmitted"] = len(rep.children)
        return d

    payload = {"file": doc.path, "animation": result.animation or None, "frame": frame,
               "time": round(result.time, 4), "size": list(result.size),
               "origin": list(result.origin), "bones": [cut(r, 0) for r in roots]}
    lines = []

    def walk(rep, prefix, last, depth):
        branch = "└─ " if last else "├─ "
        wx, wy = rep.world
        lines.append(f"{prefix}{branch}{rep.name}  世界 ({wx:.1f}, {wy:.1f})  旋轉 {rep.rotation:.1f}°"
                     + ("" if rep.active else "  （未啟用）"))
        if args.depth is not None and depth >= args.depth:
            if rep.children:
                lines.append(f"{prefix}{'   ' if last else '│  '}…（{len(rep.children)} 個子骨骼）")
            return
        for i, child in enumerate(rep.children):
            walk(child, prefix + ("   " if last else "│  "), i == len(rep.children) - 1, depth + 1)

    for i, r in enumerate(roots):
        walk(r, "", i == len(roots) - 1, 0)
    _emit(payload, args.json, lines, args.pretty)
    return 0


def cmd_spec(args) -> int:
    doc = _load(args)
    renderer = Renderer(doc, _options(args, layout_only=True))
    t, frame = _frame_time(args, doc, renderer)
    result = renderer.render(args.anim or None, t, _skin_arg(args), args.hide, args.only or None,
                             frame=frame)
    slots = result.slots
    if args.slot:
        # 指定插槽時輸出完整屬性：設定色、附件參數、圖集區塊、骨骼、可用附件、關鍵影格
        from .slotinfo import find_slot, format_text, slot_detail
        indexes = [find_slot(doc, name) for name in args.slot]
        details = [slot_detail(renderer, args.anim or None, t, _skin_arg(args), i) for i in indexes]
        by_index = {s.index: s for s in slots}
        entries = []
        for i, detail in zip(indexes, details):
            entry = by_index[i].to_dict()
            entry["detail"] = detail
            entries.append(entry)
        payload = {"file": doc.path, "animation": result.animation or None, "frame": frame,
                   "time": round(result.time, 4), "skin": result.skin, "size": list(result.size),
                   "scale": round(result.scale, 4), "origin": list(result.origin), "slots": entries}
        lines = []
        for entry, detail in zip(entries, details):
            lines += format_text(detail)
            if entry.get("screenRect"):
                lines.append(f"  畫面座標 {entry['screenRect']}（圖片像素，左上原點）")
            lines.append("")
        _emit(payload, args.json, lines, args.pretty)
        return 0
    if not args.all:
        slots = [s for s in slots if s.state not in ("empty",)]
    payload = {"file": doc.path, "animation": result.animation or None, "frame": frame,
               "time": round(result.time, 4), "skin": result.skin, "size": list(result.size),
               "scale": round(result.scale, 4), "origin": list(result.origin),
               "slots": [s.to_dict() for s in slots]}
    if args.bones:
        payload["bones"] = [b.to_dict() for b in result.bones]
    lines = [f"{'順序':>4}  {'插槽':<24} {'附件':<28} {'狀態':<13} 畫面座標"]
    for s in slots:
        rect = "" if s.rect is None else "({:.0f},{:.0f} {:.0f}×{:.0f})".format(*s.rect)
        blend = f" [{s.blend}]" if s.blend != "normal" else ""
        lines.append(f"{s.order:>4}  {s.name:<24} {(s.attachment or '-'):<28} {s.state:<13} {rect}{blend}")
    _emit(payload, args.json, lines, args.pretty)
    return 0


def cmd_find(args) -> int:
    doc = _load(args)
    data = doc.data
    needle = (args.name or "").lower()
    kinds = set(args.type or ["bone", "slot", "attachment", "animation", "event", "skin"])
    renderer = Renderer(doc, _options(args, layout_only=True))
    t, frame = _frame_time(args, doc, renderer)
    result = renderer.render(args.anim or None, t, _skin_arg(args), args.hide, args.only or None,
                             frame=frame)
    found = []
    if "bone" in kinds:
        for b in result.bones:
            if needle in b.name.lower():
                found.append({"type": "bone", **b.to_dict()})
    if "slot" in kinds:
        for s in result.slots:
            if needle in s.name.lower():
                found.append({"type": "slot", **s.to_dict()})
    if "attachment" in kinds:
        seen = set()
        for skin, slot_index, name, att in data.iter_attachments():
            if needle in name.lower() or needle in getattr(att, "path", "").lower():
                key = (skin.name, slot_index, name)
                if key in seen:
                    continue
                seen.add(key)
                found.append({"type": "attachment", "name": name, "kind": att.kind,
                              "skin": skin.name, "slot": data.slots[slot_index].name,
                              "region": getattr(att, "path", None),
                              "hasRegion": getattr(att, "region", None) is not None
                              if isinstance(att, (RegionAttachment, MeshAttachment)) else None})
    if "animation" in kinds:
        for a in data.animations:
            if needle in a.name.lower():
                found.append({"type": "animation", "name": a.name, "duration": round(a.duration, 4),
                              "frames": int(round(a.duration * doc.fps)) + 1})
    if "event" in kinds:
        for a in data.animations:
            for e in animation_events(a):
                if needle in e.data.name.lower():
                    found.append({"type": "event", "animation": a.name, **e.to_dict(),
                                  "frame": int(round(e.time * doc.fps))})
    if "skin" in kinds:
        for s in data.skins:
            if s is not None and needle in s.name.lower():
                found.append({"type": "skin", "name": s.name, "attachments": len(s.attachments)})
    payload = {"file": doc.path, "query": args.name, "frame": frame, "count": len(found),
               "results": found[:args.limit]}
    lines = [f"找到 {len(found)} 筆" + (f"（只列前 {args.limit} 筆）" if len(found) > args.limit else "")]
    for item in found[:args.limit]:
        extra = ""
        if item["type"] == "bone":
            extra = f"世界 {item['world']}"
        elif item["type"] == "slot":
            extra = f"{item.get('attachment') or '-'}  {item['state']}  {item.get('screenRect', '')}"
        elif item["type"] == "attachment":
            extra = f"{item['kind']}  插槽 {item['slot']}  造型 {item['skin']}"
        elif item["type"] == "animation":
            extra = f"{item['duration']}s"
        elif item["type"] == "event":
            extra = f"{item['animation']} @ {item['time']}s（#{item['frame']}）"
        lines.append(f"  [{item['type']}] {item.get('name', '')}  {extra}")
    _emit(payload, args.json, lines, args.pretty)
    return 0


# ---------------------------------------------------------------- doctor / list / compare

def cmd_doctor(args) -> int:
    from .doctor import check, summarize
    target = args.file
    if os.path.isdir(target):
        files = list(iter_skeletons(target))
        results = []
        for i, path in enumerate(files):
            results.append(check(path))
            _progress(i + 1, len(files), "doctor")
        summary = summarize(results)
        problems = [r for r in results if any(x["severity"] != "info" for x in r["issues"])]
        payload = {"folder": os.path.abspath(target), **summary,
                   "results": [r for r in results if r["issues"]] if args.all else problems}
        lines = [f"檢查 {summary['files']} 個骨架檔，{summary['filesWithErrors']} 個有 error。",
                 "各類問題出現在幾個檔案："]
        for key, n in summary["issueFiles"].items():
            lines.append(f"  {key:<28} {n}")
        if summary["errorFiles"]:
            lines.append("有 error 的檔案：")
            for path in summary["errorFiles"][:60]:
                r = next(x for x in results if x["file"] == path)
                kinds = sorted({i["kind"] for i in r["issues"] if i["severity"] == "error"})
                lines.append(f"  {os.path.relpath(path, target)}  ({', '.join(kinds)})")
            if len(summary["errorFiles"]) > 60:
                lines.append(f"  …另外還有 {len(summary['errorFiles']) - 60} 個（--json 看全部）")
        _emit(payload, args.json, lines, args.pretty)
        return 1 if summary["filesWithErrors"] else 0

    result = check(target, args.atlas)
    lines = [f"{result['file']}"]
    if not result["issues"]:
        lines.append("  沒有發現問題。")
    icon = {"error": "✖", "warning": "⚠", "info": "·"}
    for issue in result["issues"]:
        lines.append(f"  {icon[issue['severity']]} [{issue['kind']}] {issue['message']}")
    _emit(result, args.json, lines, args.pretty)
    return 0 if result["ok"] else 1


def cmd_list(args) -> int:
    from .reader_binary import peek_version
    files = list(iter_skeletons(args.directory))
    entries = []
    for i, path in enumerate(files):
        rel = os.path.relpath(path, args.directory).replace("\\", "/")
        item = {"path": rel}
        if args.detail:
            try:
                doc = load(path)
                item.update({"version": doc.data.version, "fps": doc.fps,
                             "atlas": os.path.basename(doc.atlas_path) if doc.atlas_path else None,
                             "skins": [s.name for s in doc.data.skins if s is not None],
                             "animations": [{"name": a.name, "duration": round(a.duration, 3)}
                                            for a in doc.data.animations],
                             "missingRegions": len(doc.missing_regions)})
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
            _progress(i + 1, len(files), "list")
        elif path.lower().endswith(".skel"):
            with open(path, "rb") as fh:
                item["version"] = peek_version(fh.read(256))
        entries.append(item)
    payload = {"directory": os.path.abspath(args.directory), "count": len(entries), "files": entries}
    lines = [f"{len(entries)} 個骨架檔："]
    for item in entries:
        if "error" in item:
            lines.append(f"  {item['path']}  ✖ {item['error']}")
        elif args.detail:
            anims = ", ".join(a["name"] for a in item["animations"][:6])
            more = "…" if len(item["animations"]) > 6 else ""
            bad = f"  ✖ 缺 {item['missingRegions']} 個 region" if item["missingRegions"] else ""
            lines.append(f"  {item['path']}  [{item['version']}]  {anims}{more}{bad}")
        else:
            lines.append(f"  {item['path']}  [{item.get('version') or 'json'}]")
    _emit(payload, args.json, lines, args.pretty)
    return 0


def cmd_compare(args) -> int:
    from PIL import Image
    from .compare import compare
    doc = _load(args)
    renderer = Renderer(doc, _options(args, background="none", bones=False, annotate=False,
                                      bboxes=False))
    t, frame = _frame_time(args, doc, renderer)
    result = renderer.render(args.anim or None, t, _skin_arg(args), args.hide, args.only or None,
                             frame=frame)
    try:
        reference = Image.open(args.reference)
        reference.load()
    except OSError as exc:
        raise SystemExit(f"錯誤: 讀不到參考圖 {args.reference}：{exc}")
    outcome = compare(result, reference, threshold=args.threshold, background=args.diff_bg)
    out = args.out or _default_out(doc.path, args.anim, frame, args, "_diff")
    _save_image(outcome.image, out)
    payload = {"file": doc.path, "animation": result.animation or None, "frame": frame,
               "image": os.path.abspath(out), **outcome.to_dict()}
    lines = [f"判定：{outcome.verdict}   差異像素 {outcome.diff_ratio * 100:.2f}%   "
             f"位移 {list(outcome.shift)}（信心 {outcome.shift_confidence:.0f}）",
             f"對照圖：{os.path.abspath(out)}"] + [f"  · {h}" for h in outcome.hints]
    _emit(payload, args.json, lines, args.pretty)
    return 1 if outcome.verdict == "different" else 0


# ---------------------------------------------------------------- serve / ui

def cmd_serve(args) -> int:
    from .server import serve
    root, initial = args.directory, None
    if args.upload_only:
        root = None
    elif os.path.isfile(root):
        initial = os.path.basename(root)
        root = os.path.dirname(os.path.abspath(root))
    serve(root=root, port=args.port, open_browser=not args.no_browser, initial=initial)
    return 0


def _remembered_dirs() -> list[str]:
    try:
        with open(_UI_REMEMBER, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(saved, list):
        saved = [saved]
    return [p for p in saved if isinstance(p, str) and os.path.isdir(p)]


def _remember_dir(path: str) -> None:
    path = os.path.abspath(path)
    keep = [path] + [p for p in _remembered_dirs()
                     if os.path.normcase(p) != os.path.normcase(path)]
    try:
        with open(_UI_REMEMBER, "w", encoding="utf-8") as fh:
            json.dump(keep[:8], fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _pick_directory(candidates: list[str]) -> str:
    print("  最近開過的資料夾：")
    for i, path in enumerate(candidates, 1):
        print(f"    {i}) {path}" + ("  <- 上次用的" if i == 1 else ""))
    print()
    print("    0) 不開資料夾，到網頁上按「📂 開啟」選單一個 .skel")
    print()
    print("  直接按 Enter 用上次那個；輸入編號切換；")
    print("  或把別的資料夾（含 .skel／.atlas 的）拖曳到這個視窗（也可以貼路徑）。")
    print()
    while True:
        try:
            answer = input("  選擇: ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            print()
            return candidates[0]
        if not answer:
            return candidates[0]
        if answer == "0":
            return "__upload_only__"
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        if os.path.isdir(answer) or os.path.isfile(answer):
            return os.path.abspath(answer)
        print("  這不是編號也不是存在的資料夾，再試一次（Enter 用上次那個）。")


def cmd_ui(args) -> int:
    """互動式啟動網頁介面，供 開啟介面.bat 呼叫（中文提示都在這裡，批次檔維持純 ASCII）。"""
    from .server import serve
    print()
    print("  spineview - Spine 骨架動畫檢視器")
    print("  ================================")
    print()
    target = args.directory or ""
    if not target:
        candidates = _remembered_dirs()
        if len(candidates) > 1:
            target = _pick_directory(candidates)
        elif candidates:
            target = candidates[0]
    while target != "__upload_only__" and (not target or not os.path.exists(target)):
        if target:
            print(f"  找不到：{target}")
            print()
        print("  請把放 Spine 檔的資料夾（或單一個 .skel）拖曳到這個視窗，或直接貼上路徑。")
        print(r"  例如：D:\Mangan_git\Inanna\InannaResource\Inanna")
        print("  直接按 Enter：不開資料夾，到網頁上按「📂 開啟」選單一個 .skel。")
        print()
        try:
            target = input("  資料夾: ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not target:
            target = "__upload_only__"
    if target == "__upload_only__":
        print("  不開資料夾。網頁開啟後按左上角「📂 開啟」選 .skel（圖集與貼圖從同一個資料夾讀），")
        print("  或把 .skel、.atlas、.png 拖進視窗上傳。")
        print()
        serve(root=None, port=args.port, open_browser=not args.no_browser)
        return 0
    target = os.path.abspath(target)
    initial = None
    if os.path.isfile(target):
        initial = os.path.basename(target)
        target = os.path.dirname(target)
    _remember_dir(target)
    print(f"  資料夾 : {target}")
    print("  正在掃描骨架檔…")
    count = sum(1 for _ in iter_skeletons(target))
    print(f"  找到   : {count} 個骨架檔（.skel／Spine .json）")
    if count == 0:
        print("  這個資料夾裡沒有骨架檔，確認一下是不是選錯層了。")
    print()
    serve(root=target, port=args.port, open_browser=not args.no_browser, initial=initial)
    return 0


# ---------------------------------------------------------------- init / setup

def _agent_doc() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(os.path.dirname(here), "dropin", "CLAUDE.md")
    return candidate if os.path.isfile(candidate) else None


def _load_registry() -> list[dict]:
    try:
        with open(_REGISTRY, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("directory")]


def _remember_project(directory: str, name: str, mode: str) -> None:
    """記住設定過的專案。mode 存的是使用者要求的方式（多半是 auto），
    之後 init --all 會重新判斷，所以專案改了慣例也會跟著改裝法。"""
    from .locking import LockTimeout, file_lock
    try:
        # 讀出來、改一筆、寫回去要在鎖裡做：兩個 init 同時跑時才不會蓋掉對方剛加的專案
        with file_lock(_REGISTRY):
            entries = [e for e in _load_registry()
                       if os.path.normcase(os.path.abspath(e["directory"]))
                       != os.path.normcase(os.path.abspath(directory))]
            entries.append({"directory": directory, "name": name, "mode": mode})

            def write(path):
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump(entries, fh, ensure_ascii=False, indent=2)
            _atomic_save(write, _REGISTRY)
    except (OSError, LockTimeout):
        pass          # 記不住只是下次 --all 少更新這個專案，不值得中斷


def _install(source: str, directory: str, name: str, mode: str, force: bool = False) -> dict:
    from .install import install
    try:
        result = install(source, directory, mode, name, force)
    except UnicodeDecodeError as exc:
        # 讀不懂的檔案絕不改寫（改寫等於把它弄壞），跳過這個專案、其他照常處理
        return {"directory": os.path.abspath(directory), "mode": mode, "reason": "",
                "actions": [], "followUp": [],
                "skipped": f"既有檔案不是 UTF-8 編碼（{exc.reason}），沒有修改任何檔案"}
    except OSError as exc:
        return {"directory": os.path.abspath(directory), "mode": mode, "reason": "",
                "actions": [], "followUp": [], "skipped": f"寫入失敗：{exc}"}
    if not result["skipped"]:
        _remember_project(result["directory"], name, mode)
    return result


def cmd_init(args) -> int:
    from .install import describe
    if args.list:
        entries = _load_registry()
        lines = [f"已設定過的專案 {len(entries)} 個："] if entries else ["還沒有設定過任何專案。"]
        for e in entries:
            gone = "" if os.path.isdir(e["directory"]) else "   （資料夾已不存在）"
            lines.append(f"  {e['directory']}  （{e.get('mode', 'auto')}）{gone}")
        _emit({"projects": entries, "count": len(entries)}, args.json, lines, args.pretty)
        return 0
    source = _agent_doc()
    if source is None:
        print("錯誤: 找不到 dropin/CLAUDE.md，請在 spineview 資料夾內執行或用 pip install -e . 安裝。",
              file=sys.stderr)
        return 2
    targets = [(os.path.abspath(d), args.name, args.mode) for d in (args.directory or [])]
    if args.all:
        seen = {os.path.normcase(d) for d, _n, _m in targets}
        for e in _load_registry():
            d = os.path.abspath(e["directory"])
            if os.path.normcase(d) not in seen:
                targets.append((d, e.get("name", "CLAUDE.md"), e.get("mode", "auto")))
    if not targets:
        print("錯誤: 請指定至少一個專案資料夾，或用 --all 更新所有設定過的專案。", file=sys.stderr)
        return 2
    done, missing = [], []
    for directory, name, mode in targets:
        if not os.path.isdir(directory):
            missing.append(directory)
            continue
        done.append(_install(source, directory, name, mode, args.force))
    lines = [f"處理 {len(done)} 個專案："]
    for result in done:
        lines += describe(result)
    if missing:
        lines += ["", f"略過 {len(missing)} 個找不到的資料夾："] + [f"  {d}" for d in missing]
    _emit({"installed": done, "missing": missing, "source": source}, args.json, lines, args.pretty)
    return 0


def _split_paths(raw: str) -> list[str]:
    out, current, quoted = [], "", False
    for ch in raw:
        if ch == '"':
            quoted = not quoted
            continue
        if ch == " " and not quoted:
            if current:
                out.append(current)
                current = ""
            continue
        current += ch
    if current:
        out.append(current)
    return out


def cmd_setup(args) -> int:
    """互動式安裝，供 設定AI助手.bat 呼叫。"""
    from .install import describe
    print()
    print("  spineview - 讓 AI agent 能檢視 Spine 動畫")
    print("  =========================================")
    print()
    source = _agent_doc()
    if source is None:
        print("  找不到 dropin/CLAUDE.md，請確認是在 spineview 資料夾內執行。")
        input("  按 Enter 關閉...")
        return 2
    added: list[str] = []
    for directory in args.directory or []:
        directory = os.path.abspath(directory.strip().strip('"'))
        if os.path.isdir(directory):
            print(chr(10).join(describe(_install(source, directory, args.name, "auto"))))
            added.append(directory)
        else:
            print(f"  找不到資料夾，略過：{directory}")
    known = [e for e in _load_registry()
             if os.path.isdir(e["directory"]) and os.path.abspath(e["directory"]) not in added]
    if known:
        print()
        print(f"  另外有 {len(known)} 個之前設定過的專案：")
        for e in known[:8]:
            print(f"     {e['directory']}")
        print()
        try:
            answer = input("  要一起更新成最新的說明嗎？(Y/n) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "y", "yes"):
            for e in known:
                result = _install(source, e["directory"], e.get("name", "CLAUDE.md"),
                                  e.get("mode", "auto"))
                print(chr(10).join(describe(result)))
                added.append(e["directory"])
    while not added:
        print()
        print("  請把要讓 AI agent 使用的專案資料夾拖曳到這個視窗，或貼上路徑（可一次拖多個）。")
        print("  （直接按 Enter 結束）")
        print()
        try:
            raw = input("  專案資料夾: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            return 0
        for piece in _split_paths(raw):
            directory = os.path.abspath(piece)
            if os.path.isdir(directory):
                print(chr(10).join(describe(_install(source, directory, args.name, "auto"))))
                added.append(directory)
            else:
                print(f"  找不到資料夾：{directory}")
    print()
    print(f"  共設定了 {len(added)} 個專案。之後在那些專案裡對 AI agent 說")
    print("  「用 spineview 看某個 .skel」即可。spineview 更新時再跑一次這個批次檔。")
    print("  （專案有 .claude/skills 時會裝成 skill、不改 CLAUDE.md；上面的 → 是要你自己做的後續步驟）")
    print()
    try:
        input("  按 Enter 關閉...")
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


# ---------------------------------------------------------------- 進入點

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="spineview",
        description="Spine 骨架動畫（.skel／.json + .atlas）檢視器：渲染畫面、拖曳時間軸、供 AI agent 讀取。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例：
  spineview info   Hero.skel
  spineview render Hero.skel --anim idle --frame 10 -o out/idle10.png
  spineview sheet  Hero.skel --anim attack --count 12      # 一張圖看完整段動畫
  spineview spec   Hero.skel --anim attack --frame 8 --json
  spineview doctor Hero.skel                                # 檢查缺圖、圖集不符
  spineview doctor D:/Game/Resource                         # 整個資料夾一起檢查
  spineview serve  D:/Game/Resource                         # 開啟網頁介面
""")
    ap.add_argument("--version", action="version", version=f"spineview {__version__}")
    subs = ap.add_subparsers(dest="command", required=True)

    p = subs.add_parser("info", help="版本、骨骼／插槽統計、造型、動畫清單與事件")
    _add_common(p, render=False, frame=False)
    p.add_argument("--fps", type=float, help="影格率（預設用檔案記錄的，沒有就 30）")

    p = subs.add_parser("render", help="渲染單一影格成 PNG")
    _add_common(p)

    p = subs.add_parser("sheet", help="把一段動畫平均取幾格平鋪成一張總覽圖")
    _add_common(p)
    p.add_argument("--count", type=int, default=12, help="取幾格（預設 12）")
    p.add_argument("--cols", type=int, help="每列幾格")
    p.add_argument("--no-labels", dest="labels", action="store_false", help="不標影格編號")
    p.add_argument("--jobs", type=int, help="並行數（預設自動）")

    p = subs.add_parser("frames", help="批次輸出每一格 PNG")
    _add_common(p)
    p.add_argument("--step", type=int, default=1, help="每幾格輸出一張")
    p.add_argument("--jobs", type=int, help="並行數（預設自動）")

    p = subs.add_parser("gif", help="輸出動畫 GIF（播放速度為近似值）")
    _add_common(p)
    p.add_argument("--step", type=int, default=1, help="每幾格取一張")
    p.add_argument("--jobs", type=int, help="並行數（預設自動）")

    p = subs.add_parser("tree", help="骨骼階層與每根骨骼的世界座標")
    _add_common(p, render=False)
    p.add_argument("--depth", type=int, help="只列到第幾層")
    p.add_argument("--bone", help="只列這根骨骼底下")

    p = subs.add_parser("spec", help="每個插槽的附件、狀態、畫面座標（繪製順序）")
    _add_common(p, render=False)
    p.add_argument("--slot", action="append",
                   help="只看這些插槽並輸出完整屬性（可重複；名稱、部分名稱或索引）")
    p.add_argument("--all", action="store_true", help="連沒有附件的插槽也列出")
    p.add_argument("--bones", action="store_true", help="一併輸出所有骨骼")
    for name, default in (("scale", 1.0), ("pad", 16)):
        p.add_argument(f"--{name}", type=type(default), default=default)
    p.add_argument("--bounds", default="auto", choices=["auto", "anim", "frame", "setup", "data"])

    p = subs.add_parser("find", help="依名稱搜尋骨骼／插槽／附件／動畫／事件／造型")
    _add_common(p, render=False)
    p.add_argument("--name", "-n", default="", help="名稱包含這段文字（不分大小寫）")
    p.add_argument("--type", action="append",
                   choices=["bone", "slot", "attachment", "animation", "event", "skin"])
    p.add_argument("--limit", type=int, default=100)
    for name, default in (("scale", 1.0), ("pad", 16)):
        p.add_argument(f"--{name}", type=type(default), default=default)

    p = subs.add_parser("compare", help="與參考截圖比對差異，指出哪個插槽畫錯")
    _add_common(p)
    p.add_argument("reference", help="參考圖（例如 Cocos Creator 的截圖）")
    p.add_argument("--threshold", type=float, default=0.05)
    p.add_argument("--diff-bg", default="#000000", help="比對時墊的底色（要與截圖底色一致）")

    p = subs.add_parser("doctor", help="檢查缺圖、圖集不符、遊戲端會載入失敗的問題（可給資料夾）")
    p.add_argument("file", help=".skel／.json，或整個資料夾")
    p.add_argument("--atlas")
    p.add_argument("--all", action="store_true", help="資料夾模式下連只有 info 的檔案也列出")
    p.add_argument("--json", action="store_true")
    p.add_argument("--pretty", action="store_true")

    p = subs.add_parser("list", help="列出資料夾底下所有骨架檔")
    p.add_argument("directory")
    p.add_argument("--detail", action="store_true", help="一併讀出版本、動畫、圖集")
    p.add_argument("--json", action="store_true")
    p.add_argument("--pretty", action="store_true")

    p = subs.add_parser("serve", help="啟動網頁檢視器")
    p.add_argument("directory", nargs="?", default=".", help="要瀏覽的資料夾（或單一骨架檔）")
    p.add_argument("--upload-only", action="store_true",
                   help="不開資料夾，只用網頁上的「開啟」／「上傳」看單一份 Spine")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--no-browser", action="store_true")

    p = subs.add_parser("ui", help="互動式啟動網頁檢視器（開啟介面.bat 用的進入點）")
    p.add_argument("directory", nargs="?")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--no-browser", action="store_true")

    p = subs.add_parser("init", help="把 agent 說明安裝到一個或多個專案（依專案慣例選 skill／rule／CLAUDE.md）")
    p.add_argument("directory", nargs="*")
    p.add_argument("--as", dest="mode", default="auto", choices=["auto", "skill", "rule", "claude-md"],
                   help="安裝方式：auto（預設，依專案慣例判斷）、skill（.claude/skills/spineview）、"
                        "rule（.claude/rules/spineview.md + CLAUDE.md 的 @ 引用）、claude-md（寫進 CLAUDE.md）")
    p.add_argument("--force", action="store_true", help="目標檔案不是 spineview 產生的也覆蓋")
    p.add_argument("--name", default="CLAUDE.md", help="claude-md 模式的檔名（例如 AGENTS.md）")
    p.add_argument("--all", action="store_true", help="一併更新所有設定過的專案")
    p.add_argument("--list", action="store_true", help="列出設定過的專案")
    p.add_argument("--json", action="store_true")
    p.add_argument("--pretty", action="store_true")

    p = subs.add_parser("setup", help="互動式安裝（設定AI助手.bat 用的進入點）")
    p.add_argument("directory", nargs="*")
    p.add_argument("--name", default="CLAUDE.md")
    return ap


COMMANDS = {
    "info": cmd_info, "render": cmd_render, "sheet": cmd_sheet, "frames": cmd_frames,
    "gif": cmd_gif, "tree": cmd_tree, "spec": cmd_spec, "find": cmd_find,
    "compare": cmd_compare, "doctor": cmd_doctor, "list": cmd_list, "serve": cmd_serve,
    "ui": cmd_ui, "init": cmd_init, "setup": cmd_setup,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args) or 0
    except ValueError as exc:
        # 找不到動畫／造型之類：訊息本身就列出可用的選項
        print(f"錯誤: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # 輸出被接到 head 之類提早關閉的管線，不算錯誤
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
