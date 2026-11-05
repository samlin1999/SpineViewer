"""讀 .skel（Spine binary）。

照 Cocos2d-x 內附的 spine-cpp SkeletonBinary.cpp 移植。那一份是改過的：
同一支讀檔器同時吃 3.8 與 3.5（版本字串含 "3.5" 就走舊格式），這裡兩條路都照搬，
所以遊戲讀得進去的檔這裡也讀得進去，遊戲拒收的（3.8.75）這裡同樣拒收。

讀完會檢查是否剛好讀到檔尾：任何一個欄位讀錯，幾乎不可能剛好停在結尾，
這是判斷「格式解析正確」最便宜也最可靠的訊號。
"""
from __future__ import annotations

import struct

import numpy as np

from .animation import (AttachmentTimeline, ColorTimeline, DeformTimeline,
                        DrawOrderTimeline, Event, EventTimeline,
                        IkConstraintTimeline, PathConstraintMixTimeline,
                        PathConstraintPositionTimeline,
                        PathConstraintSpacingTimeline, RotateTimeline,
                        ScaleTimeline, ShearTimeline,
                        TransformConstraintTimeline, TranslateTimeline,
                        TwoColorTimeline)
from .model import (Animation, BoneData, BoundingBoxAttachment,
                    ClippingAttachment, EventData, IkConstraintData,
                    MeshAttachment, PathAttachment, PathConstraintData,
                    PointAttachment, RegionAttachment, SkeletonData, Skin,
                    SlotData, TransformConstraintData, PM_FIXED, SM_FIXED,
                    SM_LENGTH)

_FLOAT = struct.Struct(">f")
_INT = struct.Struct(">i")

SLOT_ATTACHMENT, SLOT_COLOR, SLOT_TWO_COLOR = 0, 1, 2
BONE_ROTATE, BONE_TRANSLATE, BONE_SCALE, BONE_SHEAR = 0, 1, 2, 3
PATH_POSITION, PATH_SPACING, PATH_MIX = 0, 1, 2
CURVE_LINEAR, CURVE_STEPPED, CURVE_BEZIER = 0, 1, 2


class SkeletonFormatError(ValueError):
    """檔案格式不對或版本不支援。訊息會直接顯示給使用者／agent。"""


class _Input:
    __slots__ = ("data", "pos", "strings")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.strings: list[str] = []

    def byte(self) -> int:
        try:
            value = self.data[self.pos]
        except IndexError:
            raise SkeletonFormatError(f"檔案提前結束（位置 {self.pos}）") from None
        self.pos += 1
        return value

    def sbyte(self) -> int:
        value = self.byte()
        return value - 256 if value > 127 else value

    def boolean(self) -> bool:
        return self.byte() != 0

    def int(self) -> int:
        if self.pos + 4 > len(self.data):
            raise SkeletonFormatError(f"檔案提前結束（位置 {self.pos}）")
        value = _INT.unpack_from(self.data, self.pos)[0]
        self.pos += 4
        return value

    def float(self) -> float:
        if self.pos + 4 > len(self.data):
            raise SkeletonFormatError(f"檔案提前結束（位置 {self.pos}）")
        value = _FLOAT.unpack_from(self.data, self.pos)[0]
        self.pos += 4
        return value

    def varint(self, optimize_positive: bool = True) -> int:
        b = self.byte()
        value = b & 0x7F
        if b & 0x80:
            b = self.byte()
            value |= (b & 0x7F) << 7
            if b & 0x80:
                b = self.byte()
                value |= (b & 0x7F) << 14
                if b & 0x80:
                    b = self.byte()
                    value |= (b & 0x7F) << 21
                    if b & 0x80:
                        value |= (self.byte() & 0x7F) << 28
        value &= 0xFFFFFFFF
        if not optimize_positive:
            value = (value >> 1) ^ -(value & 1)
        elif value & 0x80000000:
            value -= 1 << 32
        return value

    def string(self):
        length = self.varint(True)
        if length == 0:
            return None
        end = self.pos + length - 1
        if end > len(self.data):
            raise SkeletonFormatError(f"字串長度超出檔案（位置 {self.pos}）")
        raw = self.data[self.pos:end]
        self.pos = end
        return raw.decode("utf-8", errors="replace")

    def string_ref(self):
        index = self.varint(True)
        if index == 0:
            return None
        if index - 1 >= len(self.strings):
            raise SkeletonFormatError(f"字串索引 {index} 超出字串表（{len(self.strings)}）")
        return self.strings[index - 1]

    def color(self) -> tuple:
        return (self.byte() / 255.0, self.byte() / 255.0, self.byte() / 255.0, self.byte() / 255.0)

    def float_array(self, n: int, scale: float = 1.0) -> list:
        end = self.pos + n * 4
        if end > len(self.data):
            raise SkeletonFormatError(f"檔案提前結束（位置 {self.pos}）")
        values = struct.unpack_from(f">{n}f", self.data, self.pos)
        self.pos = end
        if scale != 1:
            return [v * scale for v in values]
        return list(values)

    def short_array(self) -> list:
        n = self.varint(True)
        end = self.pos + n * 2
        if end > len(self.data):
            raise SkeletonFormatError(f"檔案提前結束（位置 {self.pos}）")
        values = struct.unpack_from(f">{n}H", self.data, self.pos)
        self.pos = end
        return list(values)


def _rgba_int(value: int) -> tuple:
    value &= 0xFFFFFFFF
    return (((value >> 24) & 0xFF) / 255.0, ((value >> 16) & 0xFF) / 255.0,
            ((value >> 8) & 0xFF) / 255.0, (value & 0xFF) / 255.0)


class BinaryReader:
    def __init__(self, scale: float = 1.0):
        self.scale = scale
        self.linked: list = []

    def read(self, data: bytes) -> SkeletonData:
        inp = _Input(data)
        sd = SkeletonData(format="binary")
        self.linked = []
        sd.hash = inp.string()
        sd.version = inp.string()
        if sd.version == "3.8.75":
            raise SkeletonFormatError(
                "版本 3.8.75：遊戲引擎會拒絕這份檔案（Unsupported skeleton data, "
                "please export with a newer version of Spine）。請用較新的 Spine 重新匯出。")
        version = sd.version or ""
        # 引擎的判斷方式：版本字串含 "3.5" 走舊格式，其餘一律當 3.8 讀
        old = "3.5" in version
        if not old and not version.startswith("3.8"):
            major = version.split(".")[0]
            if version.startswith(("3.6", "3.7")):
                raise SkeletonFormatError(
                    f"版本 {version}：引擎的讀檔器只認得 3.8 與 3.5 的 binary 格式，"
                    "3.6／3.7 匯出的 .skel 在遊戲裡也讀不進來。")
            if major.isdigit() and int(major) >= 4:
                raise SkeletonFormatError(
                    f"版本 {version}：這是 Spine 4.x 的檔案，遊戲用的 3.8 runtime 讀不進來，"
                    "需要用 Spine 3.8 重新匯出。")
            raise SkeletonFormatError(
                f"讀不出版本字串（{version!r}），這可能不是 Spine 的 .skel，"
                "或是 4.x 以後的格式。")
        self.old = old
        s = self.scale

        if not old:
            sd.x = inp.float()
            sd.y = inp.float()
        sd.width = inp.float()
        sd.height = inp.float()
        nonessential = inp.boolean()
        if nonessential:
            sd.fps = inp.float()
            sd.imagesPath = inp.string()
            if not old:
                sd.audioPath = inp.string()
        if not old:
            inp.strings = [inp.string() for _ in range(inp.varint(True))]

        # 骨骼
        for i in range(inp.varint(True)):
            name = inp.string()
            parent = None if i == 0 else sd.bones[inp.varint(True)]
            b = BoneData(i, name, parent)
            b.rotation = inp.float()
            b.x = inp.float() * s
            b.y = inp.float() * s
            b.scaleX = inp.float()
            b.scaleY = inp.float()
            b.shearX = inp.float()
            b.shearY = inp.float()
            b.length = inp.float() * s
            b.transformMode = inp.varint(True)
            if not old:
                b.skinRequired = inp.boolean()
            if nonessential:
                inp.int()
            sd.bones.append(b)

        # 插槽
        for i in range(inp.varint(True)):
            name = inp.string()
            bone = sd.bones[inp.varint(True)]
            slot = SlotData(i, name, bone)
            slot.color = inp.color()
            if not old:
                r, g, b, a = inp.byte(), inp.byte(), inp.byte(), inp.byte()
                if not (r == 0xFF and g == 0xFF and b == 0xFF and a == 0xFF):
                    slot.darkColor = (r / 255.0, g / 255.0, b / 255.0)
                slot.attachmentName = inp.string_ref()
            else:
                slot.attachmentName = inp.string()
            slot.blendMode = inp.varint(True)
            sd.slots.append(slot)

        # IK
        for _ in range(inp.varint(True)):
            c = IkConstraintData(inp.string())
            c.order = inp.varint(True)
            if not old:
                c.skinRequired = inp.boolean()
            c.bones = [sd.bones[inp.varint(True)] for _ in range(inp.varint(True))]
            c.target = sd.bones[inp.varint(True)]
            c.mix = inp.float()
            if not old:
                c.softness = inp.float() * s
            c.bendDirection = inp.sbyte()
            if not old:
                c.compress = inp.boolean()
                c.stretch = inp.boolean()
                c.uniform = inp.boolean()
            sd.ikConstraints.append(c)

        # Transform
        for _ in range(inp.varint(True)):
            c = TransformConstraintData(inp.string())
            c.order = inp.varint(True)
            if not old:
                c.skinRequired = inp.boolean()
            c.bones = [sd.bones[inp.varint(True)] for _ in range(inp.varint(True))]
            c.target = sd.bones[inp.varint(True)]
            if not old:
                c.local = inp.boolean()
                c.relative = inp.boolean()
            c.offsetRotation = inp.float()
            c.offsetX = inp.float() * s
            c.offsetY = inp.float() * s
            c.offsetScaleX = inp.float()
            c.offsetScaleY = inp.float()
            c.offsetShearY = inp.float()
            c.rotateMix = inp.float()
            c.translateMix = inp.float()
            c.scaleMix = inp.float()
            c.shearMix = inp.float()
            sd.transformConstraints.append(c)

        # Path
        for _ in range(inp.varint(True)):
            c = PathConstraintData(inp.string())
            c.order = inp.varint(True)
            if not old:
                c.skinRequired = inp.boolean()
            c.bones = [sd.bones[inp.varint(True)] for _ in range(inp.varint(True))]
            c.target = sd.slots[inp.varint(True)]
            c.positionMode = inp.varint(True)
            c.spacingMode = inp.varint(True)
            c.rotateMode = inp.varint(True)
            c.offsetRotation = inp.float()
            c.position = inp.float()
            if c.positionMode == PM_FIXED:
                c.position *= s
            c.spacing = inp.float()
            if c.spacingMode in (SM_LENGTH, SM_FIXED):
                c.spacing *= s
            c.rotateMix = inp.float()
            c.translateMix = inp.float()
            sd.pathConstraints.append(c)

        # 造型
        if not old:
            default = self._read_skin(inp, sd, True, None, nonessential)
        else:
            default = self._read_skin(inp, sd, True, "default", nonessential)
        if default is not None:
            sd.defaultSkin = default
            sd.skins.append(default)
        n = inp.varint(True)
        if old and default is not None:
            n += 1          # 3.5 的造型數量含 default
        for _ in range(1 if old and default is not None else 0, n):
            if not old:
                sd.skins.append(self._read_skin(inp, sd, False, None, nonessential))
            else:
                name = inp.string()
                skin = self._read_skin(inp, sd, False, name, nonessential)
                sd.skins.append(skin if skin is not None else Skin(name or ""))

        # linked mesh
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

        # 事件
        for _ in range(inp.varint(True)):
            name = inp.string_ref() if not old else inp.string()
            e = EventData(name or "")
            e.intValue = inp.varint(False)
            e.floatValue = inp.float()
            e.stringValue = inp.string()
            if not old:
                e.audioPath = inp.string()
                if e.audioPath:
                    e.volume = inp.float()
                    e.balance = inp.float()
            sd.events.append(e)

        # 動畫
        for _ in range(inp.varint(True)):
            name = inp.string()
            sd.animations.append(self._read_animation(inp, sd, name or ""))

        if inp.pos != len(data):
            raise SkeletonFormatError(
                f"讀完後還剩 {len(data) - inp.pos} bytes 沒讀到（共 {len(data)}）。"
                "檔案格式與版本字串不符，或檔案已損壞。")
        return sd

    # ---- 造型與附件
    def _read_skin(self, inp: _Input, sd: SkeletonData, default: bool, name,
                   nonessential: bool):
        old = self.old
        if old:
            slot_count = inp.varint(True)
            if slot_count == 0:
                return None
            skin = Skin(name or "default")
        elif default:
            slot_count = inp.varint(True)
            if slot_count == 0:
                return None
            skin = Skin("default")
        else:
            skin = Skin(inp.string_ref() or "")
            skin.bones = [sd.bones[inp.varint(True)] for _ in range(inp.varint(True))]
            for _ in range(inp.varint(True)):
                skin.constraints.append(sd.ikConstraints[inp.varint(True)])
            for _ in range(inp.varint(True)):
                skin.constraints.append(sd.transformConstraints[inp.varint(True)])
            for _ in range(inp.varint(True)):
                skin.constraints.append(sd.pathConstraints[inp.varint(True)])
            slot_count = inp.varint(True)
        for _ in range(slot_count):
            slot_index = inp.varint(True)
            for _ in range(inp.varint(True)):
                att_name = inp.string() if old else inp.string_ref()
                att = self._read_attachment(inp, sd, slot_index, att_name or "", nonessential)
                if att is not None:
                    skin.set_attachment(slot_index, att_name or "", att)
        return skin

    def _read_attachment(self, inp: _Input, sd: SkeletonData, slot_index: int,
                         attachment_name: str, nonessential: bool):
        old = self.old
        s = self.scale
        ref = inp.string if old else inp.string_ref
        name = ref() or attachment_name
        kind = inp.byte()
        if kind == 0:      # region
            path = ref() or name
            att = RegionAttachment(name, path)
            att.rotation = inp.float()
            att.x = inp.float() * s
            att.y = inp.float() * s
            att.scaleX = inp.float()
            att.scaleY = inp.float()
            att.width = inp.float() * s
            att.height = inp.float() * s
            att.color = inp.color()
            return att
        if kind == 1:      # bounding box
            count = inp.varint(True)
            att = BoundingBoxAttachment(name)
            self._read_vertices(inp, att, count)
            if nonessential:
                inp.int()
            return att
        if kind == 2:      # mesh
            path = ref() or name
            att = MeshAttachment(name, path)
            att.color = inp.color()
            count = inp.varint(True)
            att.regionUVs = inp.float_array(count << 1)
            att.triangles = inp.short_array()
            self._read_vertices(inp, att, count)
            att.hullLength = inp.varint(True) << 1
            if nonessential:
                att.edges = inp.short_array()
                att.width = inp.float() * s
                att.height = inp.float() * s
            return att
        if kind == 3:      # linked mesh
            path = ref() or name
            att = MeshAttachment(name, path)
            att.color = inp.color()
            skin_name = ref()
            parent = ref()
            inherit = inp.boolean()
            if nonessential:
                att.width = inp.float() * s
                att.height = inp.float() * s
            att.linked = (skin_name, parent, inherit)
            self.linked.append((att, skin_name, slot_index, parent, inherit))
            return att
        if kind == 4:      # path
            att = PathAttachment(name)
            att.closed = inp.boolean()
            att.constantSpeed = inp.boolean()
            count = inp.varint(True)
            self._read_vertices(inp, att, count)
            att.lengths = [inp.float() * s for _ in range(count // 3)]
            if nonessential:
                inp.int()
            return att
        if kind == 5 and not old:      # point
            att = PointAttachment(name)
            att.rotation = inp.float()
            att.x = inp.float() * s
            att.y = inp.float() * s
            if nonessential:
                inp.int()
            return att
        if kind == 6 and not old:      # clipping
            end_slot = inp.varint(True)
            count = inp.varint(True)
            att = ClippingAttachment(name)
            self._read_vertices(inp, att, count)
            att.endSlot = sd.slots[end_slot]
            if nonessential:
                inp.int()
            return att
        raise SkeletonFormatError(f"未知的附件類型 {kind}（{name}）")

    def _read_vertices(self, inp: _Input, att, vertex_count: int) -> None:
        s = self.scale
        att.worldVerticesLength = vertex_count << 1
        if not inp.boolean():
            att.vertices = inp.float_array(vertex_count << 1, s)
            att.bones = None
            return
        vertices, bones = [], []
        for _ in range(vertex_count):
            count = inp.varint(True)
            bones.append(count)
            for _ in range(count):
                bones.append(inp.varint(True))
                vertices.append(inp.float() * s)
                vertices.append(inp.float() * s)
                vertices.append(inp.float())
        att.vertices = vertices
        att.bones = bones

    # ---- 動畫
    def _curve(self, inp: _Input, timeline, frame: int) -> None:
        kind = inp.byte()
        if kind == CURVE_STEPPED:
            timeline.set_stepped(frame)
        elif kind == CURVE_BEZIER:
            timeline.set_curve(frame, inp.float(), inp.float(), inp.float(), inp.float())

    def _read_animation(self, inp: _Input, sd: SkeletonData, name: str) -> Animation:
        old = self.old
        s = self.scale
        timelines: list = []
        duration = 0.0

        for _ in range(inp.varint(True)):
            slot_index = inp.varint(True)
            for _ in range(inp.varint(True)):
                kind = inp.byte()
                count = inp.varint(True)
                if kind == SLOT_ATTACHMENT:
                    tl = AttachmentTimeline(count, slot_index)
                    for _ in range(count):
                        tl.times.append(inp.float())
                        tl.names.append(inp.string() if old else inp.string_ref())
                elif kind == SLOT_COLOR:
                    tl = ColorTimeline(count, slot_index)
                    for f in range(count):
                        tl.times.append(inp.float())
                        tl.colors.append(inp.color() if old else _rgba_int(inp.int()))
                        if f < count - 1:
                            self._curve(inp, tl, f)
                elif kind == SLOT_TWO_COLOR and not old:
                    tl = TwoColorTimeline(count, slot_index)
                    for f in range(count):
                        tl.times.append(inp.float())
                        tl.lights.append(_rgba_int(inp.int()))
                        dark = inp.int() & 0xFFFFFFFF
                        tl.darks.append((((dark >> 16) & 0xFF) / 255.0,
                                         ((dark >> 8) & 0xFF) / 255.0, (dark & 0xFF) / 255.0))
                        if f < count - 1:
                            self._curve(inp, tl, f)
                else:
                    raise SkeletonFormatError(f"未知的插槽時間軸類型 {kind}（動畫 {name}）")
                timelines.append(tl)
                duration = max(duration, tl.times[-1] if tl.times else 0.0)

        for _ in range(inp.varint(True)):
            bone_index = inp.varint(True)
            for _ in range(inp.varint(True)):
                kind = inp.byte()
                count = inp.varint(True)
                if kind == BONE_ROTATE:
                    tl = RotateTimeline(count, bone_index)
                    for f in range(count):
                        tl.times.append(inp.float())
                        tl.values.append(inp.float())
                        if f < count - 1:
                            self._curve(inp, tl, f)
                elif kind in (BONE_TRANSLATE, BONE_SCALE, BONE_SHEAR):
                    cls = {BONE_TRANSLATE: TranslateTimeline, BONE_SCALE: ScaleTimeline,
                           BONE_SHEAR: ShearTimeline}[kind]
                    tl = cls(count, bone_index)
                    ts = s if kind == BONE_TRANSLATE else 1.0
                    for f in range(count):
                        tl.times.append(inp.float())
                        tl.xs.append(inp.float() * ts)
                        tl.ys.append(inp.float() * ts)
                        if f < count - 1:
                            self._curve(inp, tl, f)
                else:
                    raise SkeletonFormatError(f"未知的骨骼時間軸類型 {kind}（動畫 {name}）")
                timelines.append(tl)
                duration = max(duration, tl.times[-1] if tl.times else 0.0)

        for _ in range(inp.varint(True)):
            index = inp.varint(True)
            count = inp.varint(True)
            tl = IkConstraintTimeline(count, index)
            for f in range(count):
                tl.times.append(inp.float())
                tl.mix.append(inp.float())
                if old:
                    tl.softness.append(0.0)
                    tl.bend.append(inp.sbyte())
                    tl.compress.append(False)
                    tl.stretch.append(False)
                else:
                    tl.softness.append(inp.float() * s)
                    tl.bend.append(inp.sbyte())
                    tl.compress.append(inp.boolean())
                    tl.stretch.append(inp.boolean())
                if f < count - 1:
                    self._curve(inp, tl, f)
            timelines.append(tl)
            duration = max(duration, tl.times[-1] if tl.times else 0.0)

        for _ in range(inp.varint(True)):
            index = inp.varint(True)
            count = inp.varint(True)
            tl = TransformConstraintTimeline(count, index)
            for f in range(count):
                tl.times.append(inp.float())
                tl.values.append((inp.float(), inp.float(), inp.float(), inp.float()))
                if f < count - 1:
                    self._curve(inp, tl, f)
            timelines.append(tl)
            duration = max(duration, tl.times[-1] if tl.times else 0.0)

        for _ in range(inp.varint(True)):
            index = inp.varint(True)
            data = sd.pathConstraints[index]
            for _ in range(inp.varint(True)):
                kind = inp.sbyte()
                count = inp.varint(True)
                if kind in (PATH_POSITION, PATH_SPACING):
                    if kind == PATH_SPACING:
                        tl = PathConstraintSpacingTimeline(count, index)
                        ts = s if data.spacingMode in (SM_LENGTH, SM_FIXED) else 1.0
                    else:
                        tl = PathConstraintPositionTimeline(count, index)
                        ts = s if data.positionMode == PM_FIXED else 1.0
                    for f in range(count):
                        tl.times.append(inp.float())
                        tl.values.append(inp.float() * ts)
                        if f < count - 1:
                            self._curve(inp, tl, f)
                elif kind == PATH_MIX:
                    tl = PathConstraintMixTimeline(count, index)
                    for f in range(count):
                        tl.times.append(inp.float())
                        tl.values.append((inp.float(), inp.float()))
                        if f < count - 1:
                            self._curve(inp, tl, f)
                else:
                    raise SkeletonFormatError(f"未知的路徑時間軸類型 {kind}（動畫 {name}）")
                timelines.append(tl)
                duration = max(duration, tl.times[-1] if tl.times else 0.0)

        for _ in range(inp.varint(True)):
            skin = sd.skins[inp.varint(True)]
            for _ in range(inp.varint(True)):
                slot_index = inp.varint(True)
                for _ in range(inp.varint(True)):
                    att_name = inp.string() if old else inp.string_ref()
                    att = skin.get_attachment(slot_index, att_name) if skin else None
                    if att is None:
                        raise SkeletonFormatError(
                            f"動畫 {name} 的 deform 指向不存在的附件：{att_name}")
                    weighted = bool(att.bones)
                    vertices = att.vertices
                    length = len(vertices) // 3 * 2 if weighted else len(vertices)
                    base = None if weighted else np.asarray(vertices, dtype=np.float64)
                    count = inp.varint(True)
                    tl = DeformTimeline(count, slot_index, att)
                    for f in range(count):
                        tl.times.append(inp.float())
                        end = inp.varint(True)
                        if end == 0:
                            deform = np.zeros(length) if weighted else base.copy()
                        else:
                            deform = np.zeros(length)
                            start = inp.varint(True)
                            end += start
                            deform[start:end] = inp.float_array(end - start, s)
                            if not weighted:
                                deform += base
                        tl.vertices.append(deform)
                        if f < count - 1:
                            self._curve(inp, tl, f)
                    timelines.append(tl)
                    duration = max(duration, tl.times[-1] if tl.times else 0.0)

        count = inp.varint(True)
        if count > 0:
            tl = DrawOrderTimeline(count)
            slot_count = len(sd.slots)
            for _ in range(count):
                tl.times.append(inp.float())
                offset_count = inp.varint(True)
                order = [-1] * slot_count
                unchanged = [0] * (slot_count - offset_count)
                original = unchanged_index = 0
                for _ in range(offset_count):
                    slot_index = inp.varint(True)
                    while original != slot_index:
                        unchanged[unchanged_index] = original
                        unchanged_index += 1
                        original += 1
                    order[original + inp.varint(True)] = original
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
            duration = max(duration, tl.times[-1])

        count = inp.varint(True)
        if count > 0:
            tl = EventTimeline(count)
            for _ in range(count):
                time = inp.float()
                data = sd.events[inp.varint(True)]
                ev = Event(time, data)
                ev.intValue = inp.varint(False)
                ev.floatValue = inp.float()
                ev.stringValue = inp.string() if inp.boolean() else data.stringValue
                if not old and data.audioPath:
                    ev.volume = inp.float()
                    ev.balance = inp.float()
                tl.times.append(time)
                tl.events.append(ev)
            timelines.append(tl)
            duration = max(duration, tl.times[-1])

        return Animation(name, timelines, duration)


def read_binary(data: bytes, scale: float = 1.0) -> SkeletonData:
    return BinaryReader(scale).read(data)


def peek_version(data: bytes) -> str | None:
    """不完整解析，只讀版本字串（list／doctor 掃整個資料夾時用）。"""
    try:
        inp = _Input(data[:256])
        inp.string()
        return inp.string()
    except Exception:
        return None
