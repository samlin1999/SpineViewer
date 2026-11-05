"""單一插槽的完整屬性（網頁的「屬性」分頁與 CLI 的 spec --slot 共用）。

分成六塊：插槽本身、目前的附件、圖集裡的貼圖區塊、所屬骨骼、這個插槽在各造型裡能放哪些附件、
以及動畫裡哪些關鍵影格會動到它。數值全部是「這一格」擺好姿勢之後的狀態。
"""
from __future__ import annotations

import io
import math
from typing import Optional

from .animation import (AttachmentTimeline, ColorTimeline, DeformTimeline,
                        DrawOrderTimeline, RotateTimeline, ScaleTimeline,
                        ShearTimeline, TranslateTimeline, TwoColorTimeline)
from .model import (BLEND_MODES, BoundingBoxAttachment, ClippingAttachment,
                    MeshAttachment, PathAttachment, PointAttachment,
                    RegionAttachment, TRANSFORM_MODES)
from .render import Renderer, _collect


def _hex(color) -> Optional[str]:
    if color is None:
        return None
    parts = [max(0, min(255, int(round(c * 255)))) for c in color]
    return "#" + "".join(f"{p:02x}" for p in parts)


def _r(value, digits: int = 3):
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return round(value, digits)
    return value


def _frame(time: float, fps: float) -> int:
    return int(round(time * fps))


def find_slot(doc, name_or_index) -> int:
    slots = doc.data.slots
    if isinstance(name_or_index, int) or (isinstance(name_or_index, str) and name_or_index.isdigit()
                                          and not any(s.name == name_or_index for s in slots)):
        index = int(name_or_index)
        if 0 <= index < len(slots):
            return index
    for s in slots:
        if s.name == name_or_index:
            return s.index
    lowered = str(name_or_index).lower()
    matches = [s for s in slots if lowered in s.name.lower()]
    if len(matches) == 1:
        return matches[0].index
    hint = "、".join(s.name for s in matches[:10]) if matches else "（沒有名稱相近的）"
    raise ValueError(f"找不到插槽 {name_or_index!r}；名稱包含它的有：{hint}")


def _attachment_info(att, slot, skeleton, doc) -> dict:
    out = {"name": att.name, "type": att.kind}
    data = doc.data
    if isinstance(att, (RegionAttachment, MeshAttachment)):
        out["path"] = att.path
        out["color"] = _hex(att.color)
    if isinstance(att, RegionAttachment):
        out.update({"x": _r(att.x), "y": _r(att.y), "rotation": _r(att.rotation),
                    "scaleX": _r(att.scaleX), "scaleY": _r(att.scaleY),
                    "width": _r(att.width), "height": _r(att.height)})
    if isinstance(att, MeshAttachment):
        out.update({"vertices": att.worldVerticesLength // 2,
                    "triangles": len(att.triangles) // 3,
                    "hullVertices": att.hullLength // 2})
        if att.width or att.height:
            out["size"] = [_r(att.width), _r(att.height)]
        if att.linked is not None:
            skin_name, parent, inherit = att.linked
            out["linkedMesh"] = {"parent": parent, "skin": skin_name or "default",
                                 "inheritDeform": bool(inherit)}
    if hasattr(att, "bones") and att.weighted:
        used = []
        i = 0
        bones = att.bones
        while i < len(bones):
            n = bones[i]
            for b in bones[i + 1:i + 1 + n]:
                if b not in used:
                    used.append(b)
            i += n + 1
        out["weightedBones"] = [data.bones[b].name for b in used]
    elif isinstance(att, (MeshAttachment, BoundingBoxAttachment, ClippingAttachment, PathAttachment)):
        out["weightedBones"] = []
    if isinstance(att, (MeshAttachment, BoundingBoxAttachment, ClippingAttachment, PathAttachment)):
        deform = slot.deform
        if deform:
            if att.weighted:
                mags = [math.hypot(deform[k], deform[k + 1]) for k in range(0, len(deform) - 1, 2)]
            else:
                base = att.vertices
                mags = [math.hypot(deform[k] - base[k], deform[k + 1] - base[k + 1])
                        for k in range(0, min(len(deform), len(base)) - 1, 2)]
            out["deform"] = {"active": True, "maxOffset": _r(max(mags) if mags else 0.0, 2)}
        else:
            out["deform"] = {"active": False}
    if isinstance(att, ClippingAttachment):
        out["endSlot"] = att.endSlot.name if att.endSlot is not None else None
        out["vertices"] = att.worldVerticesLength // 2
    if isinstance(att, PathAttachment):
        out.update({"closed": att.closed, "constantSpeed": att.constantSpeed,
                    "vertices": att.worldVerticesLength // 2})
    if isinstance(att, PointAttachment):
        out.update({"x": _r(att.x), "y": _r(att.y), "rotation": _r(att.rotation)})
    if isinstance(att, BoundingBoxAttachment):
        out["vertices"] = att.worldVerticesLength // 2
    return out


def _region_info(att, doc) -> Optional[dict]:
    region = getattr(att, "region", None)
    if region is None:
        if isinstance(att, (RegionAttachment, MeshAttachment)):
            return {"missing": True, "path": att.path,
                    "note": "圖集裡沒有這張圖——遊戲端整份骨架會載入失敗"}
        return None
    page = region.page
    pages = doc.atlas.pages if doc.atlas else []
    return {"name": region.name, "page": page.name,
            "pageIndex": pages.index(page) if page in pages else -1,
            "xy": [region.x, region.y], "size": [region.width, region.height],
            "orig": [region.originalWidth, region.originalHeight],
            "offset": [_r(region.offsetX), _r(region.offsetY)],
            "rotate": region.degrees, "index": region.index,
            "trimmed": (region.width, region.height) != (region.originalWidth, region.originalHeight)}


def _bone_info(bone) -> dict:
    d = bone.data
    return {
        "name": d.name, "index": d.index, "parent": bone.parent.data.name if bone.parent else None,
        "active": bone.active,
        "world": {"x": _r(bone.worldX, 2), "y": _r(bone.worldY, 2),
                  "rotation": _r(bone.world_rotation_x, 2),
                  "scaleX": _r(bone.world_scale_x), "scaleY": _r(bone.world_scale_y)},
        "local": {"x": _r(bone.x, 2), "y": _r(bone.y, 2), "rotation": _r(bone.rotation, 2),
                  "scaleX": _r(bone.scaleX), "scaleY": _r(bone.scaleY),
                  "shearX": _r(bone.shearX, 2), "shearY": _r(bone.shearY, 2)},
        "setup": {"x": _r(d.x, 2), "y": _r(d.y, 2), "rotation": _r(d.rotation, 2),
                  "scaleX": _r(d.scaleX), "scaleY": _r(d.scaleY)},
        "length": _r(d.length, 2),
        "transform": TRANSFORM_MODES[d.transformMode]
        if d.transformMode < len(TRANSFORM_MODES) else str(d.transformMode),
    }


def _slot_timelines(animation, slot_index: int, bone_index: int, fps: float,
                    names: list) -> dict:
    """這段動畫裡會動到這個插槽（與它的骨骼）的關鍵影格。"""
    out: dict = {}
    if animation is None:
        return out
    for tl in animation.timelines:
        if isinstance(tl, AttachmentTimeline) and tl.slotIndex == slot_index:
            out["attachment"] = [{"time": _r(t, 4), "frame": _frame(t, fps), "name": n}
                                 for t, n in zip(tl.times, tl.names)]
        elif isinstance(tl, ColorTimeline) and tl.slotIndex == slot_index:
            out["color"] = [{"time": _r(t, 4), "frame": _frame(t, fps), "color": _hex(c)}
                            for t, c in zip(tl.times, tl.colors)]
        elif isinstance(tl, TwoColorTimeline) and tl.slotIndex == slot_index:
            out["twoColor"] = [{"time": _r(t, 4), "frame": _frame(t, fps), "light": _hex(lc),
                                "dark": _hex(dc)} for t, lc, dc in zip(tl.times, tl.lights, tl.darks)]
        elif isinstance(tl, DeformTimeline) and tl.slotIndex == slot_index:
            out.setdefault("deform", []).append({
                "attachment": tl.attachment.name,
                "keys": [{"time": _r(t, 4), "frame": _frame(t, fps)} for t in tl.times]})
        elif isinstance(tl, DrawOrderTimeline):
            keys = []
            for t, order in zip(tl.times, tl.orders):
                position = order.index(slot_index) if order is not None else slot_index
                if position != slot_index:
                    keys.append({"time": _r(t, 4), "frame": _frame(t, fps), "order": position})
            if keys:
                out["drawOrder"] = keys
        elif isinstance(tl, (RotateTimeline, TranslateTimeline, ScaleTimeline, ShearTimeline)) \
                and tl.boneIndex == bone_index:
            out.setdefault("bone", {})[tl.kind] = [
                {"time": _r(t, 4), "frame": _frame(t, fps)} for t in tl.times]
    return out


def _animations_touching(data, slot_index: int) -> list[str]:
    out = []
    for anim in data.animations:
        for tl in anim.timelines:
            if getattr(tl, "slotIndex", None) == slot_index:
                out.append(anim.name)
                break
            if isinstance(tl, DrawOrderTimeline) and any(
                    o is not None and o.index(slot_index) != slot_index for o in tl.orders):
                out.append(anim.name)
                break
    return out


def slot_detail(renderer: Renderer, animation_name: Optional[str], time: float,
                skin_name: Optional[str], slot, found: Optional[list] = None) -> dict:
    """`found` 給一個 list 時，會把這一格的附件物件放進去（網頁要用它找貼圖預覽）。"""
    doc = renderer.doc
    data = doc.data
    index = find_slot(doc, slot)
    if skin_name == "__default__":
        skin_name = renderer.default_skin_name()
    animation = renderer.animation(animation_name)
    if animation is not None:
        time = max(0.0, min(time, animation.duration))
    skin = renderer.skin(skin_name)
    sk = renderer.pose(animation, time, skin)
    reports: list = []
    _collect(sk, set(), None, reports)
    report = next(r for r in reports if r.index == index)
    runtime = sk.slots[index]
    sd = runtime.data
    fps = doc.fps

    color = runtime.color
    att = runtime.attachment
    if found is not None:
        found.append(att)
    detail = {
        "slot": {
            "name": sd.name, "index": sd.index, "drawOrder": report.order,
            "bone": sd.bone.name, "blend": BLEND_MODES[sd.blendMode] if sd.blendMode < 4 else str(sd.blendMode),
            "state": report.state,
            "setupColor": _hex(sd.color), "color": _hex(color),
            "setupDarkColor": _hex(sd.darkColor), "darkColor": _hex(runtime.darkColor),
            "setupAttachment": sd.attachmentName,
            "clippedBy": report.clippedBy,
        },
        "frame": _frame(time, fps) if animation is not None else None,
        "time": _r(time, 4), "animation": getattr(animation, "name", None), "skin": skin_name,
    }
    if report.world is not None:
        detail["slot"]["worldBounds"] = [_r(v, 2) for v in report.world]
    if att is not None:
        info = _attachment_info(att, runtime, sk, doc)
        # 附件是哪個造型提供的、在造型裡登記的名稱（可能跟附件本身的名稱不同）
        for source in ([skin] if skin is not None else []) + [data.defaultSkin]:
            if source is None:
                continue
            for (si, key), value in source.attachments.items():
                if si == index and value is att:
                    info["skin"] = source.name
                    info["skinKey"] = key
                    break
            if "skin" in info:
                break
        # 最終顏色＝骨架色 × 插槽色 × 附件色（遊戲裡實際乘上去的顏色）
        if isinstance(att, (RegionAttachment, MeshAttachment)):
            final = tuple(sk.color[k] * color[k] * att.color[k] for k in range(4))
            info["finalColor"] = _hex(final)
        detail["attachment"] = info
        region = _region_info(att, doc)
        if region is not None:
            detail["region"] = region
    detail["bone"] = _bone_info(runtime.bone)

    available = []
    for s in data.skins:
        if s is None:
            continue
        for (si, key), value in s.attachments.items():
            if si == index:
                available.append({"skin": s.name, "name": key, "type": value.kind,
                                  "current": value is att})
    detail["availableAttachments"] = available
    detail["keyframes"] = _slot_timelines(animation, index, sd.bone.index, fps, [])
    detail["animationsTouching"] = _animations_touching(data, index)
    return detail


def region_image(doc, att_region) -> Optional[bytes]:
    """把圖集裡這個區塊切出來、轉正，回傳 PNG。"""
    from PIL import Image
    region = att_region
    if region is None:
        return None
    page = region.page
    try:
        with Image.open(page.path) as img:
            img = img.convert("RGBA")
            sx = img.width / (page.width or img.width)
            sy = img.height / (page.height or img.height)
            pw, ph = region.packed_size()
            box = (int(round(region.x * sx)), int(round(region.y * sy)),
                   int(round((region.x + pw) * sx)), int(round((region.y + ph) * sy)))
            crop = img.crop(box)
    except OSError:
        return None
    # 圖集裡 rotate 的圖是逆時針轉 90 度存的，順時針轉回來
    if region.degrees == 90:
        crop = crop.transpose(Image.Transpose.ROTATE_270)
    elif region.degrees == 180:
        crop = crop.transpose(Image.Transpose.ROTATE_180)
    elif region.degrees == 270:
        crop = crop.transpose(Image.Transpose.ROTATE_90)
    buffer = io.BytesIO()
    crop.save(buffer, "PNG")
    return buffer.getvalue()


def format_text(d: dict) -> list[str]:
    """CLI 文字模式的排版。"""
    s = d["slot"]
    lines = [f"插槽 {s['name']}（#{s['index']}，第 {s['drawOrder']} 個畫）  狀態 {s['state']}",
             f"  骨骼 {s['bone']}   混色 {s['blend']}   顏色 {s['color']}（setup {s['setupColor']}）"]
    if s.get("darkColor") or s.get("setupDarkColor"):
        lines.append(f"  dark color {s.get('darkColor')}（setup {s.get('setupDarkColor')}；"
                     "遊戲預設不套用）")
    lines.append(f"  setup 附件 {s.get('setupAttachment') or '（無）'}"
                 + (f"   被 {s['clippedBy']} 裁切" if s.get("clippedBy") else ""))
    if s.get("worldBounds"):
        lines.append(f"  世界座標外框 {s['worldBounds']}")
    a = d.get("attachment")
    if a:
        lines.append(f"附件 {a['name']}（{a['type']}）" + (f"  造型 {a['skin']}，登記名 {a['skinKey']}"
                                                         if a.get("skin") else ""))
        skip = {"name", "type", "skin", "skinKey"}
        for k, v in a.items():
            if k not in skip:
                lines.append(f"  {k}: {v}")
    else:
        lines.append("附件：（這一格沒有附件）")
    r = d.get("region")
    if r:
        if r.get("missing"):
            lines.append(f"圖集區塊：✖ 缺 {r['path']}（{r['note']}）")
        else:
            lines.append(f"圖集區塊 {r['name']}  頁 {r['page']}  xy {r['xy']}  size {r['size']}  "
                         f"orig {r['orig']}  offset {r['offset']}  rotate {r['rotate']}")
    b = d["bone"]
    w, loc = b["world"], b["local"]
    lines.append(f"骨骼 {b['name']}（父 {b['parent']}，{b['transform']}）  世界 ({w['x']}, {w['y']}) "
                 f"旋轉 {w['rotation']}° 縮放 ({w['scaleX']}, {w['scaleY']})")
    lines.append(f"  區域 ({loc['x']}, {loc['y']}) 旋轉 {loc['rotation']}° 縮放 "
                 f"({loc['scaleX']}, {loc['scaleY']})  長度 {b['length']}")
    avail = d.get("availableAttachments") or []
    if avail:
        lines.append("可用附件：" + "、".join(
            f"{x['name']}[{x['skin']}]" + ("◀" if x["current"] else "") for x in avail[:30]))
    kf = d.get("keyframes") or {}
    if kf:
        lines.append(f"這段動畫的關鍵影格（{d.get('animation')}）：")
        for kind, keys in kf.items():
            if kind == "bone":
                for bk, bkeys in keys.items():
                    lines.append(f"  骨骼 {bk}: " + ", ".join(f"#{k['frame']}" for k in bkeys[:40]))
            elif kind == "deform":
                for item in keys:
                    lines.append(f"  deform {item['attachment']}: "
                                 + ", ".join(f"#{k['frame']}" for k in item["keys"][:40]))
            else:
                lines.append(f"  {kind}: " + ", ".join(
                    f"#{k['frame']}" + (f"={k.get('name') or k.get('color') or k.get('light') or k.get('order')}")
                    for k in keys[:40]))
    if d.get("animationsTouching"):
        lines.append("會動到這個插槽的動畫：" + "、".join(d["animationsTouching"]))
    return lines
