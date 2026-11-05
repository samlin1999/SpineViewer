"""讀 Spine JSON（照 spine-cpp 3.8 SkeletonJson，並相容 3.5～3.7 的寫法）。

3.8 與更早版本的差異主要在三處，這裡都兩種寫法都認：
- skins：3.8 是陣列（每個有 name），之前是 {造型名: {...}} 物件
- curve：3.8 是 curve 數字 + c2/c3/c4，之前是 [cx1, cy1, cx2, cy2] 陣列
- deform：3.8 叫 deform，更早叫 ffd
"""
from __future__ import annotations

import json

import numpy as np

from .animation import (AttachmentTimeline, ColorTimeline, DeformTimeline,
                        DrawOrderTimeline, Event, EventTimeline,
                        IkConstraintTimeline, PathConstraintMixTimeline,
                        PathConstraintPositionTimeline,
                        PathConstraintSpacingTimeline, RotateTimeline,
                        ScaleTimeline, ShearTimeline,
                        TransformConstraintTimeline, TranslateTimeline,
                        TwoColorTimeline)
from .model import (Animation, BLEND_MODES, BoneData, BoundingBoxAttachment,
                    ClippingAttachment, EventData, IkConstraintData,
                    MeshAttachment, PathAttachment, PathConstraintData,
                    POSITION_MODES, PointAttachment, ROTATE_MODES,
                    RegionAttachment, SPACING_MODES, SkeletonData, Skin,
                    SlotData, TRANSFORM_MODES, TransformConstraintData,
                    PM_FIXED, SM_FIXED, SM_LENGTH)
from .reader_binary import SkeletonFormatError


def _color(text, default=(1.0, 1.0, 1.0, 1.0)):
    if not text:
        return default
    text = text.strip().lstrip("#")
    try:
        r = int(text[0:2], 16) / 255.0
        g = int(text[2:4], 16) / 255.0
        b = int(text[4:6], 16) / 255.0
        a = int(text[6:8], 16) / 255.0 if len(text) >= 8 else 1.0
    except ValueError:
        return default
    return (r, g, b, a)


def _enum(value, names, default=0) -> int:
    if value is None:
        return default
    lowered = str(value).lower()
    for i, name in enumerate(names):
        if name.lower() == lowered:
            return i
    return default


class JsonReader:
    def __init__(self, scale: float = 1.0):
        self.scale = scale
        self.linked: list = []

    def read(self, text: str) -> SkeletonData:
        try:
            root = json.loads(text)
        except ValueError as exc:
            raise SkeletonFormatError(f"JSON 格式錯誤：{exc}") from None
        if not isinstance(root, dict) or "bones" not in root:
            raise SkeletonFormatError("這不是 Spine 的 JSON（找不到 bones）")
        s = self.scale
        sd = SkeletonData(format="json")
        head = root.get("skeleton") or {}
        sd.hash = head.get("hash")
        sd.version = head.get("spine")
        version = sd.version or ""
        if version.split(".")[0].isdigit() and int(version.split(".")[0]) >= 4:
            raise SkeletonFormatError(
                f"版本 {version}：這是 Spine 4.x 的檔案，遊戲用的 3.8 runtime 讀不進來。")
        sd.x = float(head.get("x", 0))
        sd.y = float(head.get("y", 0))
        sd.width = float(head.get("width", 0))
        sd.height = float(head.get("height", 0))
        sd.fps = float(head.get("fps", 0) or 0)
        sd.imagesPath = head.get("images")
        sd.audioPath = head.get("audio")

        for i, b in enumerate(root.get("bones") or []):
            parent = sd.find_bone(b["parent"]) if b.get("parent") else None
            if b.get("parent") and parent is None:
                raise SkeletonFormatError(f"骨骼 {b.get('name')} 的父骨骼不存在：{b['parent']}")
            bone = BoneData(i, b.get("name", ""), parent)
            bone.length = b.get("length", 0) * s
            bone.x = b.get("x", 0) * s
            bone.y = b.get("y", 0) * s
            bone.rotation = b.get("rotation", 0)
            bone.scaleX = b.get("scaleX", 1)
            bone.scaleY = b.get("scaleY", 1)
            bone.shearX = b.get("shearX", 0)
            bone.shearY = b.get("shearY", 0)
            if "transform" in b:
                bone.transformMode = _enum(b["transform"], TRANSFORM_MODES)
            else:
                # 3.4 以前用 inheritRotation／inheritScale
                inherit_rotation = b.get("inheritRotation", True)
                inherit_scale = b.get("inheritScale", True)
                if not inherit_rotation:
                    bone.transformMode = 2
                elif not inherit_scale:
                    bone.transformMode = 3
            bone.skinRequired = bool(b.get("skin", False))
            sd.bones.append(bone)

        for i, sl in enumerate(root.get("slots") or []):
            bone = sd.find_bone(sl.get("bone", ""))
            if bone is None:
                raise SkeletonFormatError(f"插槽 {sl.get('name')} 的骨骼不存在：{sl.get('bone')}")
            slot = SlotData(i, sl.get("name", ""), bone)
            slot.color = _color(sl.get("color"))
            if sl.get("dark"):
                slot.darkColor = _color(sl["dark"])[:3]
            slot.attachmentName = sl.get("attachment")
            slot.blendMode = _enum(sl.get("blend"), BLEND_MODES)
            sd.slots.append(slot)

        for c in root.get("ik") or []:
            d = IkConstraintData(c.get("name", ""))
            d.order = c.get("order", 0)
            d.skinRequired = bool(c.get("skin", False))
            d.bones = [self._bone(sd, n) for n in c.get("bones", [])]
            d.target = self._bone(sd, c.get("target"))
            d.mix = c.get("mix", 1)
            d.softness = c.get("softness", 0) * s
            d.bendDirection = 1 if c.get("bendPositive", True) else -1
            d.compress = bool(c.get("compress", False))
            d.stretch = bool(c.get("stretch", False))
            d.uniform = bool(c.get("uniform", False))
            sd.ikConstraints.append(d)

        for c in root.get("transform") or []:
            d = TransformConstraintData(c.get("name", ""))
            d.order = c.get("order", 0)
            d.skinRequired = bool(c.get("skin", False))
            d.bones = [self._bone(sd, n) for n in c.get("bones", [])]
            d.target = self._bone(sd, c.get("target"))
            d.local = bool(c.get("local", False))
            d.relative = bool(c.get("relative", False))
            d.offsetRotation = c.get("rotation", 0)
            d.offsetX = c.get("x", 0) * s
            d.offsetY = c.get("y", 0) * s
            d.offsetScaleX = c.get("scaleX", 0)
            d.offsetScaleY = c.get("scaleY", 0)
            d.offsetShearY = c.get("shearY", 0)
            d.rotateMix = c.get("rotateMix", 1)
            d.translateMix = c.get("translateMix", 1)
            d.scaleMix = c.get("scaleMix", 1)
            d.shearMix = c.get("shearMix", 1)
            sd.transformConstraints.append(d)

        for c in root.get("path") or []:
            d = PathConstraintData(c.get("name", ""))
            d.order = c.get("order", 0)
            d.skinRequired = bool(c.get("skin", False))
            d.bones = [self._bone(sd, n) for n in c.get("bones", [])]
            d.target = sd.find_slot(c.get("target", ""))
            if d.target is None:
                raise SkeletonFormatError(f"路徑約束 {d.name} 的目標插槽不存在：{c.get('target')}")
            d.positionMode = _enum(c.get("positionMode"), POSITION_MODES, 1)
            d.spacingMode = _enum(c.get("spacingMode"), SPACING_MODES, 0)
            d.rotateMode = _enum(c.get("rotateMode"), ROTATE_MODES, 0)
            d.offsetRotation = c.get("rotation", 0)
            d.position = c.get("position", 0)
            if d.positionMode == PM_FIXED:
                d.position *= s
            d.spacing = c.get("spacing", 0)
            if d.spacingMode in (SM_LENGTH, SM_FIXED):
                d.spacing *= s
            d.rotateMix = c.get("rotateMix", 1)
            d.translateMix = c.get("translateMix", 1)
            sd.pathConstraints.append(d)

        skins = root.get("skins") or []
        if isinstance(skins, dict):
            skins = [{"name": name, "attachments": body} for name, body in skins.items()]
        for entry in skins:
            skin = Skin(entry.get("name", ""))
            skin.bones = [self._bone(sd, n) for n in entry.get("bones", [])]
            for key, pool in (("ik", sd.ikConstraints), ("transform", sd.transformConstraints),
                              ("path", sd.pathConstraints)):
                for name in entry.get(key, []):
                    for c in pool:
                        if c.name == name:
                            skin.constraints.append(c)
            for slot_name, atts in (entry.get("attachments") or {}).items():
                slot = sd.find_slot(slot_name)
                if slot is None:
                    raise SkeletonFormatError(f"造型 {skin.name} 指向不存在的插槽：{slot_name}")
                for att_name, body in atts.items():
                    att = self._attachment(body, skin, slot.index, att_name, sd)
                    if att is not None:
                        skin.set_attachment(slot.index, att_name, att)
            sd.skins.append(skin)
            if skin.name == "default":
                sd.defaultSkin = skin

        for mesh, skin_name, slot_index, parent_name, inherit in self.linked:
            skin = sd.defaultSkin if not skin_name else sd.find_skin(skin_name)
            if skin is None:
                raise SkeletonFormatError(f"linked mesh 指向不存在的造型：{skin_name}")
            parent = skin.get_attachment(slot_index, parent_name)
            if parent is None:
                raise SkeletonFormatError(f"linked mesh 的父 mesh 不存在：{parent_name}")
            mesh.deformAttachment = parent if inherit else mesh
            mesh.set_parent_mesh(parent)
        self.linked = []

        for name, body in (root.get("events") or {}).items():
            e = EventData(name)
            e.intValue = body.get("int", 0)
            e.floatValue = body.get("float", 0.0)
            e.stringValue = body.get("string")
            e.audioPath = body.get("audio")
            if e.audioPath:
                e.volume = body.get("volume", 1)
                e.balance = body.get("balance", 0)
            sd.events.append(e)

        for name, body in (root.get("animations") or {}).items():
            sd.animations.append(self._animation(body, name, sd))
        return sd

    @staticmethod
    def _bone(sd, name):
        bone = sd.find_bone(name or "")
        if bone is None:
            raise SkeletonFormatError(f"找不到骨骼：{name}")
        return bone

    # ---- 附件
    def _attachment(self, m: dict, skin, slot_index: int, name: str, sd):
        s = self.scale
        name = m.get("name", name)
        kind = (m.get("type") or "region").lower()
        if kind == "skinnedmesh":
            kind = "mesh"
        if kind == "region":
            att = RegionAttachment(name, m.get("path", name))
            att.x = m.get("x", 0) * s
            att.y = m.get("y", 0) * s
            att.scaleX = m.get("scaleX", 1)
            att.scaleY = m.get("scaleY", 1)
            att.rotation = m.get("rotation", 0)
            att.width = m.get("width", 32) * s
            att.height = m.get("height", 32) * s
            att.color = _color(m.get("color"))
            return att
        if kind == "boundingbox":
            att = BoundingBoxAttachment(name)
            self._vertices(m, att, m.get("vertexCount", 0) << 1)
            return att
        if kind in ("mesh", "linkedmesh"):
            att = MeshAttachment(name, m.get("path", name))
            att.color = _color(m.get("color"))
            att.width = m.get("width", 0) * s
            att.height = m.get("height", 0) * s
            parent = m.get("parent")
            if parent:
                inherit = bool(m.get("deform", True))
                att.linked = (m.get("skin"), parent, inherit)
                self.linked.append((att, m.get("skin"), slot_index, parent, inherit))
                return att
            uvs = m.get("uvs", [])
            self._vertices(m, att, len(uvs))
            att.triangles = list(m.get("triangles", []))
            att.regionUVs = list(uvs)
            att.hullLength = m.get("hull", 0) * 2
            att.edges = list(m.get("edges", []))
            return att
        if kind == "path":
            att = PathAttachment(name)
            att.closed = bool(m.get("closed", False))
            att.constantSpeed = bool(m.get("constantSpeed", True))
            count = m.get("vertexCount", 0)
            self._vertices(m, att, count << 1)
            att.lengths = [v * s for v in m.get("lengths", [])]
            return att
        if kind == "point":
            att = PointAttachment(name)
            att.x = m.get("x", 0) * s
            att.y = m.get("y", 0) * s
            att.rotation = m.get("rotation", 0)
            return att
        if kind == "clipping":
            att = ClippingAttachment(name)
            end = m.get("end")
            if end:
                att.endSlot = sd.find_slot(end)
            self._vertices(m, att, m.get("vertexCount", 0) << 1)
            return att
        return None

    def _vertices(self, m: dict, att, vertices_length: int) -> None:
        s = self.scale
        values = list(m.get("vertices", []))
        att.worldVerticesLength = vertices_length
        if vertices_length == len(values):
            att.vertices = [v * s for v in values]
            att.bones = None
            return
        bones, weights = [], []
        i = 0
        while i < len(values):
            count = int(values[i])
            i += 1
            bones.append(count)
            end = i + count * 4
            while i < end:
                bones.append(int(values[i]))
                weights.append(values[i + 1] * s)
                weights.append(values[i + 2] * s)
                weights.append(values[i + 3])
                i += 4
        att.bones = bones
        att.vertices = weights

    # ---- 動畫
    @staticmethod
    def _curve(entry: dict, timeline, frame: int) -> None:
        curve = entry.get("curve")
        if curve is None:
            return
        if curve == "stepped":
            timeline.set_stepped(frame)
        elif isinstance(curve, list):
            if len(curve) >= 4:
                timeline.set_curve(frame, curve[0], curve[1], curve[2], curve[3])
        elif isinstance(curve, (int, float)):
            timeline.set_curve(frame, float(curve), entry.get("c2", 0),
                               entry.get("c3", 1), entry.get("c4", 1))

    def _animation(self, m: dict, name: str, sd) -> Animation:
        s = self.scale
        timelines: list = []

        for slot_name, body in (m.get("slots") or {}).items():
            slot = sd.find_slot(slot_name)
            if slot is None:
                raise SkeletonFormatError(f"動畫 {name} 指向不存在的插槽：{slot_name}")
            for kind, frames in body.items():
                if not frames:
                    continue
                count = len(frames)
                if kind == "attachment":
                    tl = AttachmentTimeline(count, slot.index)
                    for f in frames:
                        tl.times.append(f.get("time", 0))
                        tl.names.append(f.get("name"))
                elif kind == "color":
                    tl = ColorTimeline(count, slot.index)
                    for i, f in enumerate(frames):
                        tl.times.append(f.get("time", 0))
                        tl.colors.append(_color(f.get("color")))
                        self._curve(f, tl, i)
                elif kind == "twoColor":
                    tl = TwoColorTimeline(count, slot.index)
                    for i, f in enumerate(frames):
                        tl.times.append(f.get("time", 0))
                        tl.lights.append(_color(f.get("light")))
                        tl.darks.append(_color(f.get("dark"))[:3])
                        self._curve(f, tl, i)
                else:
                    continue
                timelines.append(tl)

        for bone_name, body in (m.get("bones") or {}).items():
            bone = self._bone(sd, bone_name)
            for kind, frames in body.items():
                if not frames:
                    continue
                count = len(frames)
                if kind == "rotate":
                    tl = RotateTimeline(count, bone.index)
                    for i, f in enumerate(frames):
                        tl.times.append(f.get("time", 0))
                        tl.values.append(f.get("angle", 0))
                        self._curve(f, tl, i)
                elif kind in ("translate", "scale", "shear"):
                    cls = {"translate": TranslateTimeline, "scale": ScaleTimeline,
                           "shear": ShearTimeline}[kind]
                    tl = cls(count, bone.index)
                    default = 1 if kind == "scale" else 0
                    ts = s if kind == "translate" else 1
                    for i, f in enumerate(frames):
                        tl.times.append(f.get("time", 0))
                        tl.xs.append(f.get("x", default) * ts)
                        tl.ys.append(f.get("y", default) * ts)
                        self._curve(f, tl, i)
                else:
                    continue
                timelines.append(tl)

        for c_name, frames in (m.get("ik") or {}).items():
            index = next((i for i, c in enumerate(sd.ikConstraints) if c.name == c_name), None)
            if index is None or not frames:
                continue
            data = sd.ikConstraints[index]
            tl = IkConstraintTimeline(len(frames), index)
            for i, f in enumerate(frames):
                tl.times.append(f.get("time", 0))
                tl.mix.append(f.get("mix", 1))
                tl.softness.append(f.get("softness", 0) * s)
                tl.bend.append(1 if f.get("bendPositive", True) else -1)
                tl.compress.append(bool(f.get("compress", False)))
                tl.stretch.append(bool(f.get("stretch", False)))
                self._curve(f, tl, i)
            del data
            timelines.append(tl)

        for c_name, frames in (m.get("transform") or {}).items():
            index = next((i for i, c in enumerate(sd.transformConstraints) if c.name == c_name), None)
            if index is None or not frames:
                continue
            tl = TransformConstraintTimeline(len(frames), index)
            for i, f in enumerate(frames):
                tl.times.append(f.get("time", 0))
                tl.values.append((f.get("rotateMix", 1), f.get("translateMix", 1),
                                  f.get("scaleMix", 1), f.get("shearMix", 1)))
                self._curve(f, tl, i)
            timelines.append(tl)

        for c_name, body in (m.get("path") or m.get("paths") or {}).items():
            index = next((i for i, c in enumerate(sd.pathConstraints) if c.name == c_name), None)
            if index is None:
                continue
            data = sd.pathConstraints[index]
            for kind, frames in body.items():
                if not frames:
                    continue
                if kind in ("position", "spacing"):
                    if kind == "spacing":
                        tl = PathConstraintSpacingTimeline(len(frames), index)
                        ts = s if data.spacingMode in (SM_LENGTH, SM_FIXED) else 1
                    else:
                        tl = PathConstraintPositionTimeline(len(frames), index)
                        ts = s if data.positionMode == PM_FIXED else 1
                    for i, f in enumerate(frames):
                        tl.times.append(f.get("time", 0))
                        tl.values.append(f.get(kind, 0) * ts)
                        self._curve(f, tl, i)
                elif kind == "mix":
                    tl = PathConstraintMixTimeline(len(frames), index)
                    for i, f in enumerate(frames):
                        tl.times.append(f.get("time", 0))
                        tl.values.append((f.get("rotateMix", 1), f.get("translateMix", 1)))
                        self._curve(f, tl, i)
                else:
                    continue
                timelines.append(tl)

        for skin_name, slots in (m.get("deform") or m.get("ffd") or {}).items():
            skin = sd.find_skin(skin_name)
            if skin is None:
                continue
            for slot_name, atts in slots.items():
                slot = sd.find_slot(slot_name)
                if slot is None:
                    continue
                for att_name, frames in atts.items():
                    att = skin.get_attachment(slot.index, att_name)
                    if att is None or not frames:
                        continue
                    weighted = bool(att.bones)
                    vertices = att.vertices
                    length = len(vertices) // 3 * 2 if weighted else len(vertices)
                    base = None if weighted else np.asarray(vertices, dtype=np.float64)
                    tl = DeformTimeline(len(frames), slot.index, att)
                    for i, f in enumerate(frames):
                        tl.times.append(f.get("time", 0))
                        values = f.get("vertices")
                        if values is None:
                            deform = np.zeros(length) if weighted else base.copy()
                        else:
                            deform = np.zeros(length)
                            start = f.get("offset", 0)
                            deform[start:start + len(values)] = np.asarray(values) * s
                            if not weighted:
                                deform += base
                        tl.vertices.append(deform)
                        self._curve(f, tl, i)
                    timelines.append(tl)

        draw = m.get("drawOrder") or m.get("draworder")
        if draw:
            tl = DrawOrderTimeline(len(draw))
            slot_count = len(sd.slots)
            for f in draw:
                tl.times.append(f.get("time", 0))
                offsets = f.get("offsets")
                if not offsets:
                    tl.orders.append(None)
                    continue
                order = [-1] * slot_count
                unchanged = [0] * (slot_count - len(offsets))
                original = unchanged_index = 0
                for item in offsets:
                    slot = sd.find_slot(item.get("slot", ""))
                    if slot is None:
                        raise SkeletonFormatError(f"drawOrder 指向不存在的插槽：{item.get('slot')}")
                    while original != slot.index:
                        unchanged[unchanged_index] = original
                        unchanged_index += 1
                        original += 1
                    order[original + item.get("offset", 0)] = original
                    original += 1
                while original < slot_count:
                    unchanged[unchanged_index] = original
                    unchanged_index += 1
                    original += 1
                for ii in range(slot_count - 1, -1, -1):
                    if order[ii] == -1:
                        unchanged_index -= 1
                        order[ii] = unchanged[unchanged_index]
                tl.orders.append(order)
            timelines.append(tl)

        events = m.get("events")
        if events:
            tl = EventTimeline(len(events))
            for f in events:
                data = next((e for e in sd.events if e.name == f.get("name")), None)
                if data is None:
                    raise SkeletonFormatError(f"動畫 {name} 用到不存在的事件：{f.get('name')}")
                ev = Event(f.get("time", 0), data)
                ev.intValue = f.get("int", data.intValue)
                ev.floatValue = f.get("float", data.floatValue)
                ev.stringValue = f.get("string", data.stringValue)
                if data.audioPath:
                    ev.volume = f.get("volume", 1)
                    ev.balance = f.get("balance", 0)
                tl.times.append(ev.time)
                tl.events.append(ev)
            timelines.append(tl)

        duration = 0.0
        for tl in timelines:
            if tl.times:
                duration = max(duration, max(tl.times))
        return Animation(name, timelines, duration)


def read_json(text: str, scale: float = 1.0) -> SkeletonData:
    return JsonReader(scale).read(text)


def looks_like_spine_json(head: bytes) -> bool:
    """只看檔頭判斷是不是 Spine JSON，避免把一般設定檔列進清單。"""
    return b'"skeleton"' in head and (b'"spine"' in head or b'"bones"' in head) \
        or (head.lstrip().startswith(b"{") and b'"bones"' in head and b'"slots"' in head)
