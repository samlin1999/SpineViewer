"""動畫時間軸：給定時間，把骨架擺成那一格的姿勢。

只實作「單一動畫、從 setup pose 開始、alpha = 1」這一種套用方式（MixBlend.setup）。
遊戲裡 track 0 播第一段動畫時就是這個情況，跟 AnimationState 算出來的結果一致；
多段動畫交叉淡入（mix）不在範圍內。

曲線求值照 spine-cpp 3.8 CurveTimeline：貝茲曲線先用前向差分攤成 10 段折線，
再在折線上線性內插——不是解析解，跟引擎才會一模一樣。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .model import VertexAttachment
from .skeleton import _wrap_deg

BEZIER_SIZE = 10 * 2 - 1
_STEPPED = "stepped"


class CurveTimeline:
    """曲線存在每個「前一格」上：curves[i] 描述第 i 格到第 i+1 格之間怎麼過渡。"""

    def __init__(self, frame_count: int):
        self.times: list[float] = []
        self.curves: list = [None] * max(frame_count - 1, 0)

    def set_stepped(self, i: int) -> None:
        if i < len(self.curves):
            self.curves[i] = _STEPPED

    def set_curve(self, i: int, cx1: float, cy1: float, cx2: float, cy2: float) -> None:
        if i >= len(self.curves):
            return
        tmpx = (-cx1 * 2 + cx2) * 0.03
        tmpy = (-cy1 * 2 + cy2) * 0.03
        dddfx = ((cx1 - cx2) * 3 + 1) * 0.006
        dddfy = ((cy1 - cy2) * 3 + 1) * 0.006
        ddfx = tmpx * 2 + dddfx
        ddfy = tmpy * 2 + dddfy
        dfx = cx1 * 0.3 + tmpx + dddfx * 0.16666667
        dfy = cy1 * 0.3 + tmpy + dddfy * 0.16666667
        x, y = dfx, dfy
        points = []
        for _ in range((BEZIER_SIZE - 1) // 2):
            points.append(x)
            points.append(y)
            dfx += ddfx
            dfy += ddfy
            ddfx += dddfx
            ddfy += dddfy
            x += dfx
            y += dfy
        self.curves[i] = points

    def curve_percent(self, i: int, percent: float) -> float:
        percent = 0.0 if percent < 0 else (1.0 if percent > 1 else percent)
        curve = self.curves[i] if i < len(self.curves) else None
        if curve is None:
            return percent
        if curve is _STEPPED:
            return 0.0
        x = 0.0
        n = len(curve)
        j = 0
        while j < n:
            x = curve[j]
            if x >= percent:
                if j == 0:
                    return curve[1] * percent / x if x else 0.0
                prev_x, prev_y = curve[j - 2], curve[j - 1]
                return prev_y + (curve[j + 1] - prev_y) * (percent - prev_x) / (x - prev_x)
            j += 2
        y = curve[n - 1]
        return y + (1 - y) * (percent - x) / (1 - x) if x != 1 else y

    def locate(self, time: float):
        """回傳 (前一格索引, 進度)；時間在最後一格之後回傳 (最後一格, None)。"""
        t = self.times
        if time >= t[-1]:
            return len(t) - 1, None
        i = bisect_right(t, time)
        span = t[i] - t[i - 1]
        raw = (time - t[i - 1]) / span if span > 0 else 0.0
        return i - 1, self.curve_percent(i - 1, raw)

    @property
    def keyed_times(self) -> list[float]:
        return self.times


def _lerp(a: float, b: float, p: float) -> float:
    return a + (b - a) * p


# ---------------------------------------------------------------- 骨骼

class RotateTimeline(CurveTimeline):
    kind = "rotate"

    def __init__(self, frame_count: int, bone_index: int):
        super().__init__(frame_count)
        self.boneIndex = bone_index
        self.values: list[float] = []

    def apply(self, sk, time: float) -> None:
        bone = sk.bones[self.boneIndex]
        if not bone.active:
            return
        if time < self.times[0]:
            bone.rotation = bone.data.rotation
            return
        i, p = self.locate(time)
        if p is None:
            bone.rotation = bone.data.rotation + self.values[-1]
            return
        prev = self.values[i]
        r = self.values[i + 1] - prev
        r = prev + _wrap_deg(r) * p
        bone.rotation = bone.data.rotation + _wrap_deg(r)


class TranslateTimeline(CurveTimeline):
    kind = "translate"

    def __init__(self, frame_count: int, bone_index: int):
        super().__init__(frame_count)
        self.boneIndex = bone_index
        self.xs: list[float] = []
        self.ys: list[float] = []

    def _value(self, time: float):
        i, p = self.locate(time)
        if p is None:
            return self.xs[-1], self.ys[-1]
        return _lerp(self.xs[i], self.xs[i + 1], p), _lerp(self.ys[i], self.ys[i + 1], p)

    def apply(self, sk, time: float) -> None:
        bone = sk.bones[self.boneIndex]
        if not bone.active:
            return
        if time < self.times[0]:
            bone.x, bone.y = bone.data.x, bone.data.y
            return
        x, y = self._value(time)
        bone.x = bone.data.x + x
        bone.y = bone.data.y + y


class ScaleTimeline(TranslateTimeline):
    kind = "scale"

    def apply(self, sk, time: float) -> None:
        bone = sk.bones[self.boneIndex]
        if not bone.active:
            return
        if time < self.times[0]:
            bone.scaleX, bone.scaleY = bone.data.scaleX, bone.data.scaleY
            return
        x, y = self._value(time)
        bone.scaleX = x * bone.data.scaleX
        bone.scaleY = y * bone.data.scaleY


class ShearTimeline(TranslateTimeline):
    kind = "shear"

    def apply(self, sk, time: float) -> None:
        bone = sk.bones[self.boneIndex]
        if not bone.active:
            return
        if time < self.times[0]:
            bone.shearX, bone.shearY = bone.data.shearX, bone.data.shearY
            return
        x, y = self._value(time)
        bone.shearX = bone.data.shearX + x
        bone.shearY = bone.data.shearY + y


# ---------------------------------------------------------------- 插槽

class ColorTimeline(CurveTimeline):
    kind = "color"

    def __init__(self, frame_count: int, slot_index: int):
        super().__init__(frame_count)
        self.slotIndex = slot_index
        self.colors: list[tuple] = []

    def apply(self, sk, time: float) -> None:
        slot = sk.slots[self.slotIndex]
        if not slot.bone.active:
            return
        if time < self.times[0]:
            slot.color = slot.data.color
            return
        i, p = self.locate(time)
        if p is None:
            slot.color = self.colors[-1]
            return
        a, b = self.colors[i], self.colors[i + 1]
        slot.color = tuple(_lerp(a[k], b[k], p) for k in range(4))


class TwoColorTimeline(CurveTimeline):
    kind = "twoColor"

    def __init__(self, frame_count: int, slot_index: int):
        super().__init__(frame_count)
        self.slotIndex = slot_index
        self.lights: list[tuple] = []
        self.darks: list[tuple] = []

    def apply(self, sk, time: float) -> None:
        slot = sk.slots[self.slotIndex]
        if not slot.bone.active:
            return
        if time < self.times[0]:
            slot.color = slot.data.color
            slot.darkColor = slot.data.darkColor
            return
        i, p = self.locate(time)
        if p is None:
            slot.color = self.lights[-1]
            slot.darkColor = self.darks[-1]
            return
        la, lb = self.lights[i], self.lights[i + 1]
        da, db = self.darks[i], self.darks[i + 1]
        slot.color = tuple(_lerp(la[k], lb[k], p) for k in range(4))
        slot.darkColor = tuple(_lerp(da[k], db[k], p) for k in range(3))


class AttachmentTimeline:
    kind = "attachment"

    def __init__(self, frame_count: int, slot_index: int):
        self.slotIndex = slot_index
        self.times: list[float] = []
        self.names: list[Optional[str]] = []

    def apply(self, sk, time: float) -> None:
        slot = sk.slots[self.slotIndex]
        if not slot.bone.active:
            return
        if time < self.times[0]:
            name = slot.data.attachmentName
        else:
            i = len(self.times) - 1 if time >= self.times[-1] else bisect_right(self.times, time) - 1
            name = self.names[i]
        slot.set_attachment(None if not name else sk.get_attachment(self.slotIndex, name))

    @property
    def keyed_times(self) -> list[float]:
        return self.times


class DeformTimeline(CurveTimeline):
    kind = "deform"

    def __init__(self, frame_count: int, slot_index: int, attachment: VertexAttachment):
        super().__init__(frame_count)
        self.slotIndex = slot_index
        self.attachment = attachment
        self.vertices: list[np.ndarray] = []

    def apply(self, sk, time: float) -> None:
        slot = sk.slots[self.slotIndex]
        if not slot.bone.active:
            return
        att = slot.attachment
        if not isinstance(att, VertexAttachment) or att.deformAttachment is not self.attachment:
            return
        if time < self.times[0]:
            slot.deform = []
            return
        i, p = self.locate(time)
        if p is None:
            slot.deform = self.vertices[-1].tolist()
            return
        a, b = self.vertices[i], self.vertices[i + 1]
        slot.deform = (a + (b - a) * p).tolist()


class DrawOrderTimeline:
    kind = "drawOrder"

    def __init__(self, frame_count: int):
        self.times: list[float] = []
        self.orders: list[Optional[list[int]]] = []

    def apply(self, sk, time: float) -> None:
        if time < self.times[0]:
            sk.drawOrder = list(sk.slots)
            return
        i = len(self.times) - 1 if time >= self.times[-1] else bisect_right(self.times, time) - 1
        order = self.orders[i]
        if order is None:
            sk.drawOrder = list(sk.slots)
        else:
            sk.drawOrder = [sk.slots[index] for index in order]

    @property
    def keyed_times(self) -> list[float]:
        return self.times


@dataclass
class Event:
    time: float
    data: object
    intValue: int = 0
    floatValue: float = 0.0
    stringValue: Optional[str] = None
    volume: float = 1.0
    balance: float = 0.0

    def to_dict(self) -> dict:
        out = {"time": round(self.time, 4), "name": self.data.name}
        if self.intValue:
            out["int"] = self.intValue
        if self.floatValue:
            out["float"] = round(self.floatValue, 4)
        if self.stringValue:
            out["string"] = self.stringValue
        if getattr(self.data, "audioPath", None):
            out["audio"] = self.data.audioPath
        return out


class EventTimeline:
    kind = "event"

    def __init__(self, frame_count: int):
        self.times: list[float] = []
        self.events: list[Event] = []

    def apply(self, sk, time: float) -> None:
        pass          # 事件不影響畫面，只在 info／時間軸上列出

    @property
    def keyed_times(self) -> list[float]:
        return self.times


# ---------------------------------------------------------------- 約束

class IkConstraintTimeline(CurveTimeline):
    kind = "ik"

    def __init__(self, frame_count: int, index: int):
        super().__init__(frame_count)
        self.index = index
        self.mix: list[float] = []
        self.softness: list[float] = []
        self.bend: list[int] = []
        self.compress: list[bool] = []
        self.stretch: list[bool] = []

    def apply(self, sk, time: float) -> None:
        c = sk.ikConstraints[self.index]
        if not c.active:
            return
        if time < self.times[0]:
            c.set_to_setup_pose()
            return
        i, p = self.locate(time)
        if p is None:
            c.mix, c.softness = self.mix[-1], self.softness[-1]
            c.bendDirection, c.compress, c.stretch = self.bend[-1], self.compress[-1], self.stretch[-1]
            return
        c.mix = _lerp(self.mix[i], self.mix[i + 1], p)
        c.softness = _lerp(self.softness[i], self.softness[i + 1], p)
        c.bendDirection, c.compress, c.stretch = self.bend[i], self.compress[i], self.stretch[i]


class TransformConstraintTimeline(CurveTimeline):
    kind = "transform"

    def __init__(self, frame_count: int, index: int):
        super().__init__(frame_count)
        self.index = index
        self.values: list[tuple] = []   # (rotate, translate, scale, shear)

    def apply(self, sk, time: float) -> None:
        c = sk.transformConstraints[self.index]
        if not c.active:
            return
        if time < self.times[0]:
            c.set_to_setup_pose()
            return
        i, p = self.locate(time)
        if p is None:
            v = self.values[-1]
        else:
            a, b = self.values[i], self.values[i + 1]
            v = tuple(_lerp(a[k], b[k], p) for k in range(4))
        c.rotateMix, c.translateMix, c.scaleMix, c.shearMix = v


class PathConstraintPositionTimeline(CurveTimeline):
    kind = "pathPosition"
    field = "position"

    def __init__(self, frame_count: int, index: int):
        super().__init__(frame_count)
        self.index = index
        self.values: list[float] = []

    def apply(self, sk, time: float) -> None:
        c = sk.pathConstraints[self.index]
        if not c.active:
            return
        if time < self.times[0]:
            setattr(c, self.field, getattr(c.data, self.field))
            return
        i, p = self.locate(time)
        value = self.values[-1] if p is None else _lerp(self.values[i], self.values[i + 1], p)
        setattr(c, self.field, value)


class PathConstraintSpacingTimeline(PathConstraintPositionTimeline):
    kind = "pathSpacing"
    field = "spacing"


class PathConstraintMixTimeline(CurveTimeline):
    kind = "pathMix"

    def __init__(self, frame_count: int, index: int):
        super().__init__(frame_count)
        self.index = index
        self.values: list[tuple] = []   # (rotate, translate)

    def apply(self, sk, time: float) -> None:
        c = sk.pathConstraints[self.index]
        if not c.active:
            return
        if time < self.times[0]:
            c.rotateMix, c.translateMix = c.data.rotateMix, c.data.translateMix
            return
        i, p = self.locate(time)
        if p is None:
            v = self.values[-1]
        else:
            a, b = self.values[i], self.values[i + 1]
            v = (_lerp(a[0], b[0], p), _lerp(a[1], b[1], p))
        c.rotateMix, c.translateMix = v


def apply_animation(animation, sk, time: float) -> None:
    for timeline in animation.timelines:
        if timeline.times:
            timeline.apply(sk, time)


def animation_events(animation) -> list[Event]:
    out: list[Event] = []
    for timeline in animation.timelines:
        if isinstance(timeline, EventTimeline):
            out.extend(timeline.events)
    out.sort(key=lambda e: e.time)
    return out
