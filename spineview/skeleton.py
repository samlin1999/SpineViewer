"""骨架執行期：骨骼世界座標、約束、插槽狀態。

逐行照 spine-cpp 3.8（Cocos2d-x 內附的那一份）移植，包含它的已知怪癖——
例如 TransformConstraint 絕對局部模式的 scale 寫法、shearY 寫回錯的欄位。
目的不是「正確的 Spine」，而是「跟遊戲裡跑出來的一樣」。
"""
from __future__ import annotations

import math

import numpy as np

from .model import (ClippingAttachment, MeshAttachment, PathAttachment,
                    RegionAttachment, SkeletonData, Skin, VertexAttachment,
                    PM_PERCENT, RM_CHAIN, RM_CHAIN_SCALE, RM_TANGENT,
                    SM_LENGTH, SM_PERCENT, TM_NO_ROTATION, TM_NO_SCALE,
                    TM_NO_SCALE_OR_REFLECTION, TM_NORMAL, TM_ONLY_TRANSLATION)

RAD_DEG = 180.0 / math.pi
DEG_RAD = math.pi / 180.0
PI = math.pi
PI2 = math.pi * 2


def _cos_deg(d: float) -> float:
    return math.cos(d * DEG_RAD)


def _sin_deg(d: float) -> float:
    return math.sin(d * DEG_RAD)


def _wrap_deg(r: float) -> float:
    """spine 的 `r - (16384 - (int)(16384.499999999996 - r / 360)) * 360`。"""
    if r != r or r in (math.inf, -math.inf):
        return r
    return r - (16384 - int(16384.499999999996 - r / 360)) * 360


def _fdiv(a: float, b: float) -> float:
    """C++ 的浮點除法：除以 0 得到 inf／nan 而不是丟例外。

    引擎遇到退化的骨架（長度 0 的骨骼、重疊的 IK 目標）時就是靠這個不當掉，
    只是那幾根骨頭會變成 NaN、附件畫不出來。這裡照樣處理才會跟遊戲一致。
    """
    try:
        return a / b
    except ZeroDivisionError:
        if a == 0 or a != a:
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)


# ---------------------------------------------------------------- Bone

class Bone:
    __slots__ = ("data", "skeleton", "parent", "children", "x", "y", "rotation",
                 "scaleX", "scaleY", "shearX", "shearY", "ax", "ay", "arotation",
                 "ascaleX", "ascaleY", "ashearX", "ashearY", "appliedValid",
                 "a", "b", "c", "d", "worldX", "worldY", "sorted", "active")

    def __init__(self, data, skeleton, parent):
        self.data = data
        self.skeleton = skeleton
        self.parent = parent
        self.children: list[Bone] = []
        self.a, self.b, self.c, self.d = 1.0, 0.0, 0.0, 1.0
        self.worldX = self.worldY = 0.0
        self.sorted = False
        self.active = False
        self.appliedValid = False
        self.ax = self.ay = self.arotation = self.ashearX = self.ashearY = 0.0
        self.ascaleX = self.ascaleY = 1.0
        self.set_to_setup_pose()

    def set_to_setup_pose(self) -> None:
        d = self.data
        self.x, self.y, self.rotation = d.x, d.y, d.rotation
        self.scaleX, self.scaleY = d.scaleX, d.scaleY
        self.shearX, self.shearY = d.shearX, d.shearY

    def update(self) -> None:
        self.update_world_transform(self.x, self.y, self.rotation, self.scaleX,
                                    self.scaleY, self.shearX, self.shearY)

    def update_world_transform(self, x, y, rotation, scaleX, scaleY, shearX, shearY) -> None:
        self.ax, self.ay, self.arotation = x, y, rotation
        self.ascaleX, self.ascaleY = scaleX, scaleY
        self.ashearX, self.ashearY = shearX, shearY
        self.appliedValid = True
        sk = self.skeleton
        parent = self.parent
        if parent is None:
            rotation_y = rotation + 90 + shearY
            sx, sy = sk.scaleX, sk.scaleY
            self.a = _cos_deg(rotation + shearX) * scaleX * sx
            self.b = _cos_deg(rotation_y) * scaleY * sx
            self.c = _sin_deg(rotation + shearX) * scaleX * sy
            self.d = _sin_deg(rotation_y) * scaleY * sy
            self.worldX = x * sx + sk.x
            self.worldY = y * sy + sk.y
            return

        pa, pb, pc, pd = parent.a, parent.b, parent.c, parent.d
        self.worldX = pa * x + pb * y + parent.worldX
        self.worldY = pc * x + pd * y + parent.worldY

        mode = self.data.transformMode
        if mode == TM_NORMAL:
            rotation_y = rotation + 90 + shearY
            la = _cos_deg(rotation + shearX) * scaleX
            lb = _cos_deg(rotation_y) * scaleY
            lc = _sin_deg(rotation + shearX) * scaleX
            ld = _sin_deg(rotation_y) * scaleY
            self.a = pa * la + pb * lc
            self.b = pa * lb + pb * ld
            self.c = pc * la + pd * lc
            self.d = pc * lb + pd * ld
            return
        if mode == TM_ONLY_TRANSLATION:
            rotation_y = rotation + 90 + shearY
            self.a = _cos_deg(rotation + shearX) * scaleX
            self.b = _cos_deg(rotation_y) * scaleY
            self.c = _sin_deg(rotation + shearX) * scaleX
            self.d = _sin_deg(rotation_y) * scaleY
        elif mode == TM_NO_ROTATION:
            s = pa * pa + pc * pc
            if s > 0.0001:
                s = abs(pa * pd - pb * pc) / s
                pa /= sk.scaleX
                pc /= sk.scaleY
                pb = pc * s
                pd = pa * s
                prx = math.atan2(pc, pa) * RAD_DEG
            else:
                pa = 0.0
                pc = 0.0
                prx = 90 - math.atan2(pd, pb) * RAD_DEG
            rx = rotation + shearX - prx
            ry = rotation + shearY - prx + 90
            la = _cos_deg(rx) * scaleX
            lb = _cos_deg(ry) * scaleY
            lc = _sin_deg(rx) * scaleX
            ld = _sin_deg(ry) * scaleY
            self.a = pa * la - pb * lc
            self.b = pa * lb - pb * ld
            self.c = pc * la + pd * lc
            self.d = pc * lb + pd * ld
        else:  # TM_NO_SCALE / TM_NO_SCALE_OR_REFLECTION
            cos = _cos_deg(rotation)
            sin = _sin_deg(rotation)
            za = (pa * cos + pb * sin) / sk.scaleX
            zc = (pc * cos + pd * sin) / sk.scaleY
            s = math.sqrt(za * za + zc * zc)
            if s > 0.00001:
                s = 1 / s
            za *= s
            zc *= s
            s = math.sqrt(za * za + zc * zc)
            if (mode == TM_NO_SCALE
                    and (pa * pd - pb * pc < 0) != ((sk.scaleX < 0) != (sk.scaleY < 0))):
                s = -s
            r = PI / 2 + math.atan2(zc, za)
            zb = math.cos(r) * s
            zd = math.sin(r) * s
            la = _cos_deg(shearX) * scaleX
            lb = _cos_deg(90 + shearY) * scaleY
            lc = _sin_deg(shearX) * scaleX
            ld = _sin_deg(90 + shearY) * scaleY
            self.a = za * la + zb * lc
            self.b = za * lb + zb * ld
            self.c = zc * la + zd * lc
            self.d = zc * lb + zd * ld
        self.a *= sk.scaleX
        self.b *= sk.scaleX
        self.c *= sk.scaleY
        self.d *= sk.scaleY

    def update_applied_transform(self) -> None:
        self.appliedValid = True
        parent = self.parent
        a, b, c, d = self.a, self.b, self.c, self.d
        if parent is None:
            self.ax = self.worldX
            self.ay = self.worldY
            self.arotation = math.atan2(c, a) * RAD_DEG
            self.ascaleX = math.sqrt(a * a + c * c)
            self.ascaleY = math.sqrt(b * b + d * d)
            self.ashearX = 0.0
            self.ashearY = math.atan2(a * b + c * d, a * d - b * c) * RAD_DEG
            return
        pa, pb, pc, pd = parent.a, parent.b, parent.c, parent.d
        det = pa * pd - pb * pc
        pid = 1 / det if det != 0 else 0.0
        dx = self.worldX - parent.worldX
        dy = self.worldY - parent.worldY
        self.ax = dx * pd * pid - dy * pb * pid
        self.ay = dy * pa * pid - dx * pc * pid
        ia = pid * pd
        id_ = pid * pa
        ib = pid * pb
        ic = pid * pc
        ra = ia * a - ib * c
        rb = ia * b - ib * d
        rc = id_ * c - ic * a
        rd = id_ * d - ic * b
        self.ashearX = 0.0
        self.ascaleX = math.sqrt(ra * ra + rc * rc)
        if self.ascaleX > 0.0001:
            det = ra * rd - rb * rc
            self.ascaleY = det / self.ascaleX
            self.ashearY = math.atan2(ra * rb + rc * rd, det) * RAD_DEG
            self.arotation = math.atan2(rc, ra) * RAD_DEG
        else:
            self.ascaleX = 0.0
            self.ascaleY = math.sqrt(rb * rb + rd * rd)
            self.ashearY = 0.0
            self.arotation = 90 - math.atan2(rd, rb) * RAD_DEG

    def local_to_world(self, lx: float, ly: float) -> tuple[float, float]:
        return (lx * self.a + ly * self.b + self.worldX,
                lx * self.c + ly * self.d + self.worldY)

    @property
    def world_rotation_x(self) -> float:
        return math.atan2(self.c, self.a) * RAD_DEG

    @property
    def world_scale_x(self) -> float:
        return math.sqrt(self.a * self.a + self.c * self.c)

    @property
    def world_scale_y(self) -> float:
        return math.sqrt(self.b * self.b + self.d * self.d)


# ---------------------------------------------------------------- Slot

class Slot:
    __slots__ = ("data", "bone", "skeleton", "color", "darkColor", "attachment", "deform")

    def __init__(self, data, bone, skeleton):
        self.data = data
        self.bone = bone
        self.skeleton = skeleton
        self.color = data.color
        self.darkColor = data.darkColor
        self.attachment = None
        self.deform: list = []

    def set_attachment(self, attachment) -> None:
        current = self.attachment
        if current is attachment:
            return
        if (attachment is None or current is None
                or not isinstance(attachment, VertexAttachment)
                or not isinstance(current, VertexAttachment)
                or attachment.deformAttachment is not current.deformAttachment):
            self.deform = []
        self.attachment = attachment

    def set_to_setup_pose(self) -> None:
        self.color = self.data.color
        self.darkColor = self.data.darkColor
        name = self.data.attachmentName
        # spine-cpp 在 setup 沒有附件時（set_attachment(None) 對 None 直接 return）會留著上一格的
        # deform；那份資料看不到（之後放任何附件都會先清掉），但明確清掉才能保證
        # 「重複使用的骨架」和「每格新建的骨架」狀態完全相同
        self.deform = []
        self.attachment = None
        if name:
            self.set_attachment(self.skeleton.get_attachment(self.data.index, name))
        else:
            self.set_attachment(None)


# ---------------------------------------------------------------- 約束

class IkConstraint:
    def __init__(self, data, skeleton):
        self.data = data
        self.bones = [skeleton.bones[b.index] for b in data.bones]
        self.target = skeleton.bones[data.target.index]
        self.active = False
        self.set_to_setup_pose()

    def set_to_setup_pose(self) -> None:
        d = self.data
        self.mix, self.softness = d.mix, d.softness
        self.bendDirection = d.bendDirection
        self.compress, self.stretch = d.compress, d.stretch

    def update(self) -> None:
        t = self.target
        if len(self.bones) == 1:
            _ik_apply1(self.bones[0], t.worldX, t.worldY, self.compress, self.stretch,
                       self.data.uniform, self.mix)
        elif len(self.bones) == 2:
            _ik_apply2(self.bones[0], self.bones[1], t.worldX, t.worldY,
                       self.bendDirection, self.stretch, self.softness, self.mix)


def _ik_apply1(bone: Bone, target_x, target_y, compress, stretch, uniform, alpha) -> None:
    p = bone.parent
    pa, pb, pc, pd = p.a, p.b, p.c, p.d
    rotation_ik = -bone.ashearX - bone.arotation
    if not bone.appliedValid:
        bone.update_applied_transform()
    mode = bone.data.transformMode
    sk = bone.skeleton
    if mode == TM_ONLY_TRANSLATION:
        tx = target_x - bone.worldX
        ty = target_y - bone.worldY
    else:
        if mode == TM_NO_ROTATION:
            s = _fdiv(abs(pa * pd - pb * pc), pa * pa + pc * pc)
            sa = pa / sk.scaleX
            sc = pc / sk.scaleY
            pb = -sc * s * sk.scaleX
            pd = sa * s * sk.scaleY
            rotation_ik += math.atan2(sc, sa) * RAD_DEG
        x = target_x - p.worldX
        y = target_y - p.worldY
        d = pa * pd - pb * pc
        tx = _fdiv(x * pd - y * pb, d) - bone.ax
        ty = _fdiv(y * pa - x * pc, d) - bone.ay
    rotation_ik += math.atan2(ty, tx) * RAD_DEG
    if bone.ascaleX < 0:
        rotation_ik += 180
    if rotation_ik > 180:
        rotation_ik -= 360
    elif rotation_ik < -180:
        rotation_ik += 360
    sx = bone.ascaleX
    sy = bone.ascaleY
    if compress or stretch:
        if mode in (TM_NO_SCALE, TM_NO_SCALE_OR_REFLECTION):
            tx = target_x - bone.worldX
            ty = target_y - bone.worldY
        b = bone.data.length * sx
        dd = math.sqrt(tx * tx + ty * ty)
        if ((compress and dd < b) or (stretch and dd > b)) and b > 0.0001:
            s = (_fdiv(dd, b) - 1) * alpha + 1
            sx *= s
            if uniform:
                sy *= s
    bone.update_world_transform(bone.ax, bone.ay, bone.arotation + rotation_ik * alpha,
                                sx, sy, bone.ashearX, bone.ashearY)


def _ik_apply2(parent: Bone, child: Bone, target_x, target_y, bend_dir, stretch,
               softness, alpha) -> None:
    if alpha == 0:
        child.update()
        return
    if not parent.appliedValid:
        parent.update_applied_transform()
    if not child.appliedValid:
        child.update_applied_transform()
    px, py = parent.ax, parent.ay
    psx = parent.ascaleX
    sx = psx
    psy = parent.ascaleY
    csx = child.ascaleX
    if psx < 0:
        psx = -psx
        o1 = 180
        s2 = -1
    else:
        o1 = 0
        s2 = 1
    if psy < 0:
        psy = -psy
        s2 = -s2
    if csx < 0:
        csx = -csx
        o2 = 180
    else:
        o2 = 0
    r = psx - psy
    cx = child.ax
    u = abs(r) <= 0.0001
    if not u:
        cy = 0.0
        cwx = parent.a * cx + parent.worldX
        cwy = parent.c * cx + parent.worldY
    else:
        cy = child.ay
        cwx = parent.a * cx + parent.b * cy + parent.worldX
        cwy = parent.c * cx + parent.d * cy + parent.worldY
    pp = parent.parent
    a, b, c, d = pp.a, pp.b, pp.c, pp.d
    id_ = _fdiv(1.0, a * d - b * c)
    x = cwx - pp.worldX
    y = cwy - pp.worldY
    dx = (x * d - y * b) * id_ - px
    dy = (y * a - x * c) * id_ - py
    l1 = math.sqrt(dx * dx + dy * dy)
    l2 = child.data.length * csx
    if l1 < 0.0001:
        _ik_apply1(parent, target_x, target_y, False, stretch, False, alpha)
        child.update_world_transform(cx, cy, 0, child.ascaleX, child.ascaleY,
                                     child.ashearX, child.ashearY)
        return
    x = target_x - pp.worldX
    y = target_y - pp.worldY
    tx = (x * d - y * b) * id_ - px
    ty = (y * a - x * c) * id_ - py
    dd = tx * tx + ty * ty
    if softness != 0:
        softness *= psx * (csx + 1) / 2
        td = math.sqrt(dd)
        sd = td - l1 - l2 * psx + softness
        if sd > 0:
            p = min(1.0, _fdiv(sd, softness * 2)) - 1
            p = _fdiv(sd - softness * (1 - p * p), td)
            tx -= p * tx
            ty -= p * ty
            dd = tx * tx + ty * ty

    a1 = a2 = 0.0
    solved = False
    if u:
        l2 *= psx
        cosine = _fdiv(dd - l1 * l1 - l2 * l2, 2 * l1 * l2)
        if cosine < -1:
            cosine = -1.0
        elif cosine > 1:
            cosine = 1.0
            if stretch:
                sx *= (_fdiv(math.sqrt(dd), l1 + l2) - 1) * alpha + 1
        a2 = math.acos(cosine) * bend_dir
        a = l1 + l2 * cosine
        b = l2 * math.sin(a2)
        a1 = math.atan2(ty * a - tx * b, tx * a + ty * b)
        solved = True
    else:
        a = psx * l2
        b = psy * l2
        aa, bb, ll = a * a, b * b, l1 * l1
        ta = math.atan2(ty, tx)
        c0 = bb * ll + aa * dd - aa * bb
        c1 = -2 * bb * l1
        c2 = bb - aa
        d = c1 * c1 - 4 * c2 * c0
        if d >= 0:
            q = math.sqrt(d)
            if c1 < 0:
                q = -q
            q = -(c1 + q) / 2
            r0 = _fdiv(q, c2)
            r1 = _fdiv(c0, q)
            r = r0 if abs(r0) < abs(r1) else r1
            if r * r <= dd:
                y = math.sqrt(dd - r * r) * bend_dir
                a1 = ta - math.atan2(y, r)
                a2 = math.atan2(_fdiv(y, psy), _fdiv(r - l1, psx))
                solved = True
        if not solved:
            min_angle, min_x, min_y = PI, l1 - a, 0.0
            min_dist = min_x * min_x
            max_angle, max_x, max_y = 0.0, l1 + a, 0.0
            max_dist = max_x * max_x
            denom = aa - bb
            c0 = _fdiv(-a * l1, denom)
            if -1 <= c0 <= 1:
                c0 = math.acos(c0)
                x = a * math.cos(c0) + l1
                y = b * math.sin(c0)
                d = x * x + y * y
                if d < min_dist:
                    min_angle, min_dist, min_x, min_y = c0, d, x, y
                if d > max_dist:
                    max_angle, max_dist, max_x, max_y = c0, d, x, y
            if dd <= (min_dist + max_dist) / 2:
                a1 = ta - math.atan2(min_y * bend_dir, min_x)
                a2 = min_angle * bend_dir
            else:
                a1 = ta - math.atan2(max_y * bend_dir, max_x)
                a2 = max_angle * bend_dir

    os_ = math.atan2(cy, cx) * s2
    a1 = (a1 - os_) * RAD_DEG + o1 - parent.arotation
    if a1 > 180:
        a1 -= 360
    elif a1 < -180:
        a1 += 360
    parent.update_world_transform(px, py, parent.arotation + a1 * alpha, sx,
                                  parent.ascaleY, 0, 0)
    a2 = ((a2 + os_) * RAD_DEG - child.ashearX) * s2 + o2 - child.arotation
    if a2 > 180:
        a2 -= 360
    elif a2 < -180:
        a2 += 360
    child.update_world_transform(cx, cy, child.arotation + a2 * alpha, child.ascaleX,
                                 child.ascaleY, child.ashearX, child.ashearY)


class TransformConstraint:
    def __init__(self, data, skeleton):
        self.data = data
        self.bones = [skeleton.bones[b.index] for b in data.bones]
        self.target = skeleton.bones[data.target.index]
        self.active = False
        self.set_to_setup_pose()

    def set_to_setup_pose(self) -> None:
        d = self.data
        self.rotateMix, self.translateMix = d.rotateMix, d.translateMix
        self.scaleMix, self.shearMix = d.scaleMix, d.shearMix

    def update(self) -> None:
        if self.data.local:
            if self.data.relative:
                self._relative_local()
            else:
                self._absolute_local()
        else:
            if self.data.relative:
                self._relative_world()
            else:
                self._absolute_world()

    def _absolute_world(self) -> None:
        rm, tm, sm, shm = self.rotateMix, self.translateMix, self.scaleMix, self.shearMix
        t = self.target
        ta, tb, tc, td = t.a, t.b, t.c, t.d
        reflect = DEG_RAD if ta * td - tb * tc > 0 else -DEG_RAD
        data = self.data
        offset_rotation = data.offsetRotation * reflect
        offset_shear_y = data.offsetShearY * reflect
        for bone in self.bones:
            modified = False
            if rm != 0:
                a, b, c, d = bone.a, bone.b, bone.c, bone.d
                r = math.atan2(tc, ta) - math.atan2(c, a) + offset_rotation
                if r > PI:
                    r -= PI2
                elif r < -PI:
                    r += PI2
                r *= rm
                cos, sin = math.cos(r), math.sin(r)
                bone.a = cos * a - sin * c
                bone.b = cos * b - sin * d
                bone.c = sin * a + cos * c
                bone.d = sin * b + cos * d
                modified = True
            if tm != 0:
                tx, ty = t.local_to_world(data.offsetX, data.offsetY)
                bone.worldX += (tx - bone.worldX) * tm
                bone.worldY += (ty - bone.worldY) * tm
                modified = True
            if sm > 0:
                s = math.sqrt(bone.a * bone.a + bone.c * bone.c)
                if s > 0.00001:
                    s = (s + (math.sqrt(ta * ta + tc * tc) - s + data.offsetScaleX) * sm) / s
                bone.a *= s
                bone.c *= s
                s = math.sqrt(bone.b * bone.b + bone.d * bone.d)
                if s > 0.00001:
                    s = (s + (math.sqrt(tb * tb + td * td) - s + data.offsetScaleY) * sm) / s
                bone.b *= s
                bone.d *= s
                modified = True
            if shm > 0:
                b, d = bone.b, bone.d
                by = math.atan2(d, b)
                r = math.atan2(td, tb) - math.atan2(tc, ta) - (by - math.atan2(bone.c, bone.a))
                if r > PI:
                    r -= PI2
                elif r < -PI:
                    r += PI2
                r = by + (r + offset_shear_y) * shm
                s = math.sqrt(b * b + d * d)
                bone.b = math.cos(r) * s
                bone.d = math.sin(r) * s
                modified = True
            if modified:
                bone.appliedValid = False

    def _relative_world(self) -> None:
        rm, tm, sm, shm = self.rotateMix, self.translateMix, self.scaleMix, self.shearMix
        t = self.target
        ta, tb, tc, td = t.a, t.b, t.c, t.d
        reflect = DEG_RAD if ta * td - tb * tc > 0 else -DEG_RAD
        data = self.data
        offset_rotation = data.offsetRotation * reflect
        offset_shear_y = data.offsetShearY * reflect
        for bone in self.bones:
            modified = False
            if rm != 0:
                a, b, c, d = bone.a, bone.b, bone.c, bone.d
                r = math.atan2(tc, ta) + offset_rotation
                if r > PI:
                    r -= PI2
                elif r < -PI:
                    r += PI2
                r *= rm
                cos, sin = math.cos(r), math.sin(r)
                bone.a = cos * a - sin * c
                bone.b = cos * b - sin * d
                bone.c = sin * a + cos * c
                bone.d = sin * b + cos * d
                modified = True
            if tm != 0:
                tx, ty = t.local_to_world(data.offsetX, data.offsetY)
                bone.worldX += tx * tm
                bone.worldY += ty * tm
                modified = True
            if sm > 0:
                s = (math.sqrt(ta * ta + tc * tc) - 1 + data.offsetScaleX) * sm + 1
                bone.a *= s
                bone.c *= s
                s = (math.sqrt(tb * tb + td * td) - 1 + data.offsetScaleY) * sm + 1
                bone.b *= s
                bone.d *= s
                modified = True
            if shm > 0:
                r = math.atan2(td, tb) - math.atan2(tc, ta)
                if r > PI:
                    r -= PI2
                elif r < -PI:
                    r += PI2
                b, d = bone.b, bone.d
                r = math.atan2(d, b) + (r - PI / 2 + offset_shear_y) * shm
                s = math.sqrt(b * b + d * d)
                bone.b = math.cos(r) * s
                bone.d = math.sin(r) * s
                modified = True
            if modified:
                bone.appliedValid = False

    def _absolute_local(self) -> None:
        rm, tm, sm, shm = self.rotateMix, self.translateMix, self.scaleMix, self.shearMix
        t = self.target
        data = self.data
        if not t.appliedValid:
            t.update_applied_transform()
        for bone in self.bones:
            if not bone.appliedValid:
                bone.update_applied_transform()
            rotation = bone.arotation
            if rm != 0:
                r = t.arotation - rotation + data.offsetRotation
                r = _wrap_deg(r)
                rotation += r * rm
            x, y = bone.ax, bone.ay
            if tm != 0:
                x += (t.ax - x + data.offsetX) * tm
                y += (t.ay - y + data.offsetY) * tm
            scale_x, scale_y = bone.ascaleX, bone.ascaleY
            if sm != 0:
                # 與 spine-cpp 3.8 相同（結果是比例而非實際縮放，屬原版行為）
                if scale_x > 0.00001:
                    scale_x = (scale_x + (t.ascaleX - scale_x + data.offsetScaleX) * sm) / scale_x
                if scale_y > 0.00001:
                    scale_y = (scale_y + (t.ascaleY - scale_y + data.offsetScaleY) * sm) / scale_y
            shear_y = bone.ashearY
            if shm != 0:
                r = t.ashearY - shear_y + data.offsetShearY
                r = _wrap_deg(r)
                bone.shearY += r * shm     # 原版寫到 shearY 而非區域變數
            bone.update_world_transform(x, y, rotation, scale_x, scale_y, bone.ashearX, shear_y)

    def _relative_local(self) -> None:
        rm, tm, sm, shm = self.rotateMix, self.translateMix, self.scaleMix, self.shearMix
        t = self.target
        data = self.data
        if not t.appliedValid:
            t.update_applied_transform()
        for bone in self.bones:
            if not bone.appliedValid:
                bone.update_applied_transform()
            rotation = bone.arotation
            if rm != 0:
                rotation += (t.arotation + data.offsetRotation) * rm
            x, y = bone.ax, bone.ay
            if tm != 0:
                x += (t.ax + data.offsetX) * tm
                y += (t.ay + data.offsetY) * tm
            scale_x, scale_y = bone.ascaleX, bone.ascaleY
            if sm != 0:
                if scale_x > 0.00001:
                    scale_x *= ((t.ascaleX - 1 + data.offsetScaleX) * sm) + 1
                if scale_y > 0.00001:
                    scale_y *= ((t.ascaleY - 1 + data.offsetScaleY) * sm) + 1
            shear_y = bone.ashearY
            if shm != 0:
                shear_y += (t.ashearY + data.offsetShearY) * shm
            bone.update_world_transform(x, y, rotation, scale_x, scale_y, bone.ashearX, shear_y)


class PathConstraint:
    EPSILON = 0.00001
    NONE, BEFORE, AFTER = -1, -2, -3

    def __init__(self, data, skeleton):
        self.data = data
        self.skeleton = skeleton
        self.bones = [skeleton.bones[b.index] for b in data.bones]
        self.target = skeleton.slots[data.target.index]
        self.active = False
        self.set_to_setup_pose()

    def set_to_setup_pose(self) -> None:
        d = self.data
        self.position, self.spacing = d.position, d.spacing
        self.rotateMix, self.translateMix = d.rotateMix, d.translateMix

    def update(self) -> None:
        attachment = self.target.attachment
        if not isinstance(attachment, PathAttachment):
            return
        rotate_mix, translate_mix = self.rotateMix, self.translateMix
        translate = translate_mix > 0
        rotate = rotate_mix > 0
        if not translate and not rotate:
            return
        data = self.data
        percent_spacing = data.spacingMode == SM_PERCENT
        rotate_mode = data.rotateMode
        tangents = rotate_mode == RM_TANGENT
        scale = rotate_mode == RM_CHAIN_SCALE
        bone_count = len(self.bones)
        spaces_count = bone_count if tangents else bone_count + 1
        spaces = [0.0] * spaces_count
        lengths = [0.0] * bone_count if scale else None
        spacing = self.spacing
        eps = self.EPSILON
        if scale or not percent_spacing:
            length_spacing = data.spacingMode == SM_LENGTH
            i = 0
            n = spaces_count - 1
            while i < n:
                bone = self.bones[i]
                setup_length = bone.data.length
                if setup_length < eps:
                    if scale:
                        lengths[i] = 0.0
                    i += 1
                    spaces[i] = 0.0
                elif percent_spacing:
                    if scale:
                        x = setup_length * bone.a
                        y = setup_length * bone.c
                        lengths[i] = math.sqrt(x * x + y * y)
                    i += 1
                    spaces[i] = spacing
                else:
                    x = setup_length * bone.a
                    y = setup_length * bone.c
                    length = math.sqrt(x * x + y * y)
                    if scale:
                        lengths[i] = length
                    i += 1
                    spaces[i] = (setup_length + spacing if length_spacing else spacing) \
                        * length / setup_length
        else:
            for i in range(1, spaces_count):
                spaces[i] = spacing

        positions = self._compute_world_positions(
            attachment, spaces, spaces_count, tangents,
            data.positionMode == PM_PERCENT, percent_spacing)
        bone_x, bone_y = positions[0], positions[1]
        offset_rotation = data.offsetRotation
        if offset_rotation == 0:
            tip = rotate_mode == RM_CHAIN
        else:
            tip = False
            p = self.target.bone
            offset_rotation *= DEG_RAD if p.a * p.d - p.b * p.c > 0 else -DEG_RAD
        p = 3
        for i in range(bone_count):
            bone = self.bones[i]
            bone.worldX += (bone_x - bone.worldX) * translate_mix
            bone.worldY += (bone_y - bone.worldY) * translate_mix
            x, y = positions[p], positions[p + 1]
            dx, dy = x - bone_x, y - bone_y
            if scale:
                length = lengths[i]
                if length >= eps:
                    s = (_fdiv(math.sqrt(dx * dx + dy * dy), length) - 1) * rotate_mix + 1
                    bone.a *= s
                    bone.c *= s
            bone_x, bone_y = x, y
            if rotate:
                a, b, c, d = bone.a, bone.b, bone.c, bone.d
                if tangents:
                    r = positions[p - 1]
                elif spaces[i + 1] < eps:
                    r = positions[p + 2]
                else:
                    r = math.atan2(dy, dx)
                r -= math.atan2(c, a)
                if tip:
                    cos, sin = math.cos(r), math.sin(r)
                    length = bone.data.length
                    bone_x += (length * (cos * a - sin * c) - dx) * rotate_mix
                    bone_y += (length * (sin * a + cos * c) - dy) * rotate_mix
                else:
                    r += offset_rotation
                if r > PI:
                    r -= PI2
                elif r < -PI:
                    r += PI2
                r *= rotate_mix
                cos, sin = math.cos(r), math.sin(r)
                bone.a = cos * a - sin * c
                bone.b = cos * b - sin * d
                bone.c = sin * a + cos * c
                bone.d = sin * b + cos * d
            bone.appliedValid = False
            p += 3

    def _compute_world_positions(self, path, spaces, spaces_count, tangents,
                                 percent_position, percent_spacing):
        target = self.target
        position = self.position
        out = [0.0] * (spaces_count * 3 + 2)
        closed = path.closed
        vertices_length = path.worldVerticesLength
        curve_count = vertices_length // 6
        prev_curve = self.NONE
        eps = self.EPSILON

        if not path.constantSpeed:
            lengths = path.lengths
            curve_count -= 1 if closed else 2
            path_length = lengths[curve_count]
            if percent_position:
                position *= path_length
            if percent_spacing:
                for i in range(1, spaces_count):
                    spaces[i] *= path_length
            world = [0.0] * 8
            curve = 0
            o = 0
            for i in range(spaces_count):
                space = spaces[i]
                position += space
                p = position
                skip = False
                if closed:
                    p = math.fmod(p, path_length)
                    if p < 0:
                        p += path_length
                    curve = 0
                elif p < 0:
                    if prev_curve != self.BEFORE:
                        prev_curve = self.BEFORE
                        compute_path_world_vertices(path, target, 2, 4, world, 0)
                    _add_before(p, world, 0, out, o)
                    skip = True
                elif p > path_length:
                    if prev_curve != self.AFTER:
                        prev_curve = self.AFTER
                        compute_path_world_vertices(path, target, vertices_length - 6, 4, world, 0)
                    _add_after(p - path_length, world, 0, out, o)
                    skip = True
                if not skip:
                    while True:
                        length = lengths[curve]
                        if p > length:
                            curve += 1
                            continue
                        if curve == 0:
                            p = _fdiv(p, length)
                        else:
                            prev = lengths[curve - 1]
                            p = _fdiv(p - prev, length - prev)
                        break
                    if curve != prev_curve:
                        prev_curve = curve
                        if closed and curve == curve_count:
                            compute_path_world_vertices(path, target, vertices_length - 4, 4, world, 0)
                            compute_path_world_vertices(path, target, 0, 4, world, 4)
                        else:
                            compute_path_world_vertices(path, target, curve * 6 + 2, 8, world, 0)
                    _add_curve(p, world[0], world[1], world[2], world[3], world[4], world[5],
                               world[6], world[7], out, o,
                               tangents or (i > 0 and space < eps))
                o += 3
            return out

        if closed:
            vertices_length += 2
            world = [0.0] * vertices_length
            compute_path_world_vertices(path, target, 2, vertices_length - 4, world, 0)
            compute_path_world_vertices(path, target, 0, 2, world, vertices_length - 4)
            world[vertices_length - 2] = world[0]
            world[vertices_length - 1] = world[1]
        else:
            curve_count -= 1
            vertices_length -= 4
            world = [0.0] * vertices_length
            compute_path_world_vertices(path, target, 2, vertices_length, world, 0)

        curves = [0.0] * curve_count
        path_length = 0.0
        x1, y1 = world[0], world[1]
        cx1 = cy1 = cx2 = cy2 = x2 = y2 = 0.0
        w = 2
        for i in range(curve_count):
            cx1, cy1, cx2, cy2, x2, y2 = world[w:w + 6]
            tmpx = (x1 - cx1 * 2 + cx2) * 0.1875
            tmpy = (y1 - cy1 * 2 + cy2) * 0.1875
            dddfx = ((cx1 - cx2) * 3 - x1 + x2) * 0.09375
            dddfy = ((cy1 - cy2) * 3 - y1 + y2) * 0.09375
            ddfx = tmpx * 2 + dddfx
            ddfy = tmpy * 2 + dddfy
            dfx = (cx1 - x1) * 0.75 + tmpx + dddfx * 0.16666667
            dfy = (cy1 - y1) * 0.75 + tmpy + dddfy * 0.16666667
            path_length += math.sqrt(dfx * dfx + dfy * dfy)
            dfx += ddfx
            dfy += ddfy
            ddfx += dddfx
            ddfy += dddfy
            path_length += math.sqrt(dfx * dfx + dfy * dfy)
            dfx += ddfx
            dfy += ddfy
            path_length += math.sqrt(dfx * dfx + dfy * dfy)
            dfx += ddfx + dddfx
            dfy += ddfy + dddfy
            path_length += math.sqrt(dfx * dfx + dfy * dfy)
            curves[i] = path_length
            x1, y1 = x2, y2
            w += 6

        if percent_position:
            position *= path_length
        else:
            position *= _fdiv(path_length, path.lengths[curve_count - 1])
        if percent_spacing:
            for i in range(1, spaces_count):
                spaces[i] *= path_length

        segments = [0.0] * 10
        curve_length = 0.0
        curve = 0
        segment = 0
        o = 0
        for i in range(spaces_count):
            space = spaces[i]
            position += space
            p = position
            if closed:
                p = math.fmod(p, path_length)
                if p < 0:
                    p += path_length
                curve = 0
            elif p < 0:
                _add_before(p, world, 0, out, o)
                o += 3
                continue
            elif p > path_length:
                _add_after(p - path_length, world, vertices_length - 4, out, o)
                o += 3
                continue
            while True:
                length = curves[curve]
                if p > length:
                    curve += 1
                    continue
                if curve == 0:
                    p = _fdiv(p, length)
                else:
                    prev = curves[curve - 1]
                    p = _fdiv(p - prev, length - prev)
                break
            if curve != prev_curve:
                prev_curve = curve
                ii = curve * 6
                x1, y1, cx1, cy1, cx2, cy2, x2, y2 = world[ii:ii + 8]
                tmpx = (x1 - cx1 * 2 + cx2) * 0.03
                tmpy = (y1 - cy1 * 2 + cy2) * 0.03
                dddfx = ((cx1 - cx2) * 3 - x1 + x2) * 0.006
                dddfy = ((cy1 - cy2) * 3 - y1 + y2) * 0.006
                ddfx = tmpx * 2 + dddfx
                ddfy = tmpy * 2 + dddfy
                dfx = (cx1 - x1) * 0.3 + tmpx + dddfx * 0.16666667
                dfy = (cy1 - y1) * 0.3 + tmpy + dddfy * 0.16666667
                curve_length = math.sqrt(dfx * dfx + dfy * dfy)
                segments[0] = curve_length
                for ii in range(1, 8):
                    dfx += ddfx
                    dfy += ddfy
                    ddfx += dddfx
                    ddfy += dddfy
                    curve_length += math.sqrt(dfx * dfx + dfy * dfy)
                    segments[ii] = curve_length
                dfx += ddfx
                dfy += ddfy
                curve_length += math.sqrt(dfx * dfx + dfy * dfy)
                segments[8] = curve_length
                dfx += ddfx + dddfx
                dfy += ddfy + dddfy
                curve_length += math.sqrt(dfx * dfx + dfy * dfy)
                segments[9] = curve_length
                segment = 0
            p *= curve_length
            while True:
                length = segments[segment]
                if p > length:
                    segment += 1
                    continue
                if segment == 0:
                    p = _fdiv(p, length)
                else:
                    prev = segments[segment - 1]
                    p = segment + _fdiv(p - prev, length - prev)
                break
            _add_curve(p * 0.1, x1, y1, cx1, cy1, cx2, cy2, x2, y2, out, o,
                       tangents or (i > 0 and space < eps))
            o += 3
        return out


def _add_before(p, temp, i, out, o):
    x1, y1 = temp[i], temp[i + 1]
    dx, dy = temp[i + 2] - x1, temp[i + 3] - y1
    r = math.atan2(dy, dx)
    out[o] = x1 + p * math.cos(r)
    out[o + 1] = y1 + p * math.sin(r)
    out[o + 2] = r


def _add_after(p, temp, i, out, o):
    x1, y1 = temp[i + 2], temp[i + 3]
    dx, dy = x1 - temp[i], y1 - temp[i + 1]
    r = math.atan2(dy, dx)
    out[o] = x1 + p * math.cos(r)
    out[o + 1] = y1 + p * math.sin(r)
    out[o + 2] = r


def _add_curve(p, x1, y1, cx1, cy1, cx2, cy2, x2, y2, out, o, tangents):
    if p < PathConstraint.EPSILON or math.isnan(p):
        out[o] = x1
        out[o + 1] = y1
        out[o + 2] = math.atan2(cy1 - y1, cx1 - x1)
        return
    tt = p * p
    ttt = tt * p
    u = 1 - p
    uu = u * u
    uuu = uu * u
    ut = u * p
    ut3 = ut * 3
    uut3 = u * ut3
    utt3 = ut3 * p
    x = x1 * uuu + cx1 * uut3 + cx2 * utt3 + x2 * ttt
    y = y1 * uuu + cy1 * uut3 + cy2 * utt3 + y2 * ttt
    out[o] = x
    out[o + 1] = y
    if tangents:
        if p < 0.001:
            out[o + 2] = math.atan2(cy1 - y1, cx1 - x1)
        else:
            out[o + 2] = math.atan2(y - (y1 * uu + cy1 * ut * 2 + cy2 * tt),
                                    x - (x1 * uu + cx1 * ut * 2 + cx2 * tt))


def compute_path_world_vertices(att: VertexAttachment, slot: Slot, start: int, count: int,
                                out: list, offset: int, stride: int = 2) -> None:
    """VertexAttachment::computeWorldVertices 的逐點版本（path 約束需要指定區段）。"""
    count = offset + (count >> 1) * stride
    deform = slot.deform
    bones = att.bones
    if not bones:
        vertices = deform if deform else att.vertices
        bone = slot.bone
        x, y, a, b, c, d = bone.worldX, bone.worldY, bone.a, bone.b, bone.c, bone.d
        vv = start
        w = offset
        while w < count:
            vx, vy = vertices[vv], vertices[vv + 1]
            out[w] = vx * a + vy * b + x
            out[w + 1] = vx * c + vy * d + y
            vv += 2
            w += stride
        return
    v = 0
    skip = 0
    i = 0
    while i < start:
        n = bones[v]
        v += n + 1
        skip += n
        i += 2
    sk_bones = slot.skeleton.bones
    vertices = att.vertices
    w = offset
    b = skip * 3
    f = skip << 1
    while w < count:
        wx = wy = 0.0
        n = bones[v]
        v += 1
        n += v
        while v < n:
            bone = sk_bones[bones[v]]
            vx = vertices[b]
            vy = vertices[b + 1]
            if deform:
                vx += deform[f]
                vy += deform[f + 1]
            weight = vertices[b + 2]
            wx += (vx * bone.a + vy * bone.b + bone.worldX) * weight
            wy += (vx * bone.c + vy * bone.d + bone.worldY) * weight
            v += 1
            b += 3
            f += 2
        out[w] = wx
        out[w + 1] = wy
        w += stride


# ---------------------------------------------------------------- Skeleton

class Skeleton:
    def __init__(self, data: SkeletonData):
        self.data = data
        self.bones: list[Bone] = []
        for bd in data.bones:
            parent = self.bones[bd.parent.index] if bd.parent is not None else None
            bone = Bone(bd, self, parent)
            if parent is not None:
                parent.children.append(bone)
            self.bones.append(bone)
        self.slots = [Slot(sd, self.bones[sd.bone.index], self) for sd in data.slots]
        self.drawOrder = list(self.slots)
        self.ikConstraints = [IkConstraint(d, self) for d in data.ikConstraints]
        self.transformConstraints = [TransformConstraint(d, self) for d in data.transformConstraints]
        self.pathConstraints = [PathConstraint(d, self) for d in data.pathConstraints]
        self.skin: Skin | None = None
        self.color = (1.0, 1.0, 1.0, 1.0)
        self.x = self.y = 0.0
        self.scaleX = self.scaleY = 1.0
        self._cache: list = []
        self._cache_reset: list = []
        self.update_cache()

    # ---- 造型與附件
    def set_skin(self, skin: Skin | None) -> None:
        if skin is self.skin:
            return
        if skin is not None and self.skin is None:
            for i, slot in enumerate(self.slots):
                name = slot.data.attachmentName
                if name:
                    att = skin.get_attachment(i, name)
                    if att is not None:
                        slot.set_attachment(att)
        self.skin = skin
        self.update_cache()

    def get_attachment(self, slot_index: int, name: str):
        if self.skin is not None:
            att = self.skin.get_attachment(slot_index, name)
            if att is not None:
                return att
        if self.data.defaultSkin is not None:
            return self.data.defaultSkin.get_attachment(slot_index, name)
        return None

    def set_to_setup_pose(self) -> None:
        for bone in self.bones:
            bone.set_to_setup_pose()
        for c in self.ikConstraints:
            c.set_to_setup_pose()
        for c in self.transformConstraints:
            c.set_to_setup_pose()
        for c in self.pathConstraints:
            c.set_to_setup_pose()
        self.drawOrder = list(self.slots)
        for slot in self.slots:
            slot.set_to_setup_pose()

    # ---- 更新順序（Skeleton::updateCache）
    def update_cache(self) -> None:
        cache: list = []
        reset: list = []
        self._cache, self._cache_reset = cache, reset
        for bone in self.bones:
            bone.sorted = bone.data.skinRequired
            bone.active = not bone.sorted
        if self.skin is not None:
            for bd in self.skin.bones:
                bone = self.bones[bd.index]
                while bone is not None:
                    bone.sorted = False
                    bone.active = True
                    bone = bone.parent

        count = len(self.ikConstraints) + len(self.transformConstraints) + len(self.pathConstraints)
        for i in range(count):
            done = False
            for c in self.ikConstraints:
                if c.data.order == i:
                    self._sort_ik(c)
                    done = True
                    break
            if done:
                continue
            for c in self.transformConstraints:
                if c.data.order == i:
                    self._sort_transform(c)
                    done = True
                    break
            if done:
                continue
            for c in self.pathConstraints:
                if c.data.order == i:
                    self._sort_path(c)
                    break
        for bone in self.bones:
            self._sort_bone(bone)

    def _skin_has(self, data) -> bool:
        return self.skin is not None and data in self.skin.constraints

    def _sort_ik(self, c: IkConstraint) -> None:
        c.active = c.target.active and (not c.data.skinRequired or self._skin_has(c.data))
        if not c.active:
            return
        self._sort_bone(c.target)
        constrained = c.bones
        parent = constrained[0]
        self._sort_bone(parent)
        if len(constrained) > 1:
            child = constrained[-1]
            if child not in self._cache:
                self._cache_reset.append(child)
        self._cache.append(c)
        self._sort_reset(parent.children)
        constrained[-1].sorted = True

    def _sort_path(self, c: PathConstraint) -> None:
        c.active = c.target.bone.active and (not c.data.skinRequired or self._skin_has(c.data))
        if not c.active:
            return
        slot = c.target
        slot_index = slot.data.index
        slot_bone = slot.bone
        if self.skin is not None:
            self._sort_path_skin(self.skin, slot_index, slot_bone)
        default = self.data.defaultSkin
        if default is not None and default is not self.skin:
            self._sort_path_skin(default, slot_index, slot_bone)
        self._sort_path_attachment(slot.attachment, slot_bone)
        for bone in c.bones:
            self._sort_bone(bone)
        self._cache.append(c)
        for bone in c.bones:
            self._sort_reset(bone.children)
        for bone in c.bones:
            bone.sorted = True

    def _sort_path_skin(self, skin: Skin, slot_index: int, slot_bone: Bone) -> None:
        for (index, _name), att in skin.attachments.items():
            if index == slot_index:
                self._sort_path_attachment(att, slot_bone)

    def _sort_path_attachment(self, att, slot_bone: Bone) -> None:
        if not isinstance(att, PathAttachment):
            return
        path_bones = att.bones
        if not path_bones:
            self._sort_bone(slot_bone)
            return
        i = 0
        n = len(path_bones)
        while i < n:
            bone_count = path_bones[i]
            i += 1
            end = i + bone_count
            while i < end:
                self._sort_bone(self.bones[path_bones[i]])
                i += 1

    def _sort_transform(self, c: TransformConstraint) -> None:
        c.active = c.target.active and (not c.data.skinRequired or self._skin_has(c.data))
        if not c.active:
            return
        self._sort_bone(c.target)
        constrained = c.bones
        if c.data.local:
            for child in constrained:
                self._sort_bone(child.parent)
                if child not in self._cache:
                    self._cache_reset.append(child)
        else:
            for bone in constrained:
                self._sort_bone(bone)
        self._cache.append(c)
        for bone in constrained:
            self._sort_reset(bone.children)
        for bone in constrained:
            bone.sorted = True

    def _sort_bone(self, bone: Bone | None) -> None:
        if bone is None or bone.sorted:
            return
        if bone.parent is not None:
            self._sort_bone(bone.parent)
        bone.sorted = True
        self._cache.append(bone)

    def _sort_reset(self, bones: list) -> None:
        for bone in bones:
            if not bone.active:
                continue
            if bone.sorted:
                self._sort_reset(bone.children)
            bone.sorted = False

    def update_world_transform(self) -> None:
        for bone in self._cache_reset:
            bone.ax, bone.ay, bone.arotation = bone.x, bone.y, bone.rotation
            bone.ascaleX, bone.ascaleY = bone.scaleX, bone.scaleY
            bone.ashearX, bone.ashearY = bone.shearX, bone.shearY
            bone.appliedValid = True
        for item in self._cache:
            item.update()

    # ---- 頂點
    def bone_arrays(self):
        """所有骨骼的世界矩陣，(a, b, c, d, worldX, worldY) 各一條 numpy 陣列。"""
        m = np.array([(b.a, b.b, b.c, b.d, b.worldX, b.worldY) for b in self.bones],
                     dtype=np.float64).reshape(-1, 6)
        return m[:, 0], m[:, 1], m[:, 2], m[:, 3], m[:, 4], m[:, 5]


def world_vertices(att, slot: Slot, bone_arrays=None) -> np.ndarray:
    """附件在世界座標下的頂點，(N, 2)。region 回傳 bl, ul, ur, br 四點。"""
    bone = slot.bone
    if isinstance(att, RegionAttachment):
        o = att.offset
        x = o[:, 0] * bone.a + o[:, 1] * bone.b + bone.worldX
        y = o[:, 0] * bone.c + o[:, 1] * bone.d + bone.worldY
        return np.stack([x, y], axis=1)
    prepared = att.prepare()
    deform = slot.deform
    if prepared[0] == "plain":
        local = prepared[1]
        if deform and len(deform) == local.size:
            local = np.asarray(deform, dtype=np.float64).reshape(-1, 2)
        x = local[:, 0] * bone.a + local[:, 1] * bone.b + bone.worldX
        y = local[:, 0] * bone.c + local[:, 1] * bone.d + bone.worldY
        return np.stack([x, y], axis=1)
    _kind, ebone, ex, ey, ew, evert, count = prepared
    if bone_arrays is None:
        bone_arrays = slot.skeleton.bone_arrays()
    A, B, C, D, WX, WY = bone_arrays
    vx, vy = ex, ey
    if deform and len(deform) == ex.size * 2:
        d = np.asarray(deform, dtype=np.float64).reshape(-1, 2)
        vx = ex + d[:, 0]
        vy = ey + d[:, 1]
    wx = (vx * A[ebone] + vy * B[ebone] + WX[ebone]) * ew
    wy = (vx * C[ebone] + vy * D[ebone] + WY[ebone]) * ew
    return np.stack([np.bincount(evert, wx, minlength=count),
                     np.bincount(evert, wy, minlength=count)], axis=1)


def is_drawable(att) -> bool:
    return isinstance(att, (RegionAttachment, MeshAttachment))


__all__ = ["Bone", "Slot", "Skeleton", "world_vertices", "is_drawable",
           "ClippingAttachment", "compute_path_world_vertices"]
