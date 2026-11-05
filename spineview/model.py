"""Spine 骨架資料模型（對應 spine-cpp 3.8 的 *Data 類別）。

只放「讀檔後就不再變動」的資料；每一格動畫的姿勢放在 skeleton.py 的執行期物件裡。
欄位名稱刻意沿用 spine runtime 的命名，對照 C++ 原始碼時一眼就能找到對應。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# 列舉值的順序與 spine-cpp 完全一致：binary 檔直接存的是這些索引
TRANSFORM_MODES = ["normal", "onlyTranslation", "noRotationOrReflection",
                   "noScale", "noScaleOrReflection"]
BLEND_MODES = ["normal", "additive", "multiply", "screen"]
POSITION_MODES = ["fixed", "percent"]
SPACING_MODES = ["length", "fixed", "percent"]
ROTATE_MODES = ["tangent", "chain", "chainScale"]
ATTACHMENT_TYPES = ["region", "boundingbox", "mesh", "linkedmesh", "path",
                    "point", "clipping"]

TM_NORMAL, TM_ONLY_TRANSLATION, TM_NO_ROTATION, TM_NO_SCALE, TM_NO_SCALE_OR_REFLECTION = range(5)
PM_FIXED, PM_PERCENT = range(2)
SM_LENGTH, SM_FIXED, SM_PERCENT = range(3)
RM_TANGENT, RM_CHAIN, RM_CHAIN_SCALE = range(3)

Color = tuple  # (r, g, b, a)，0~1


# ---------------------------------------------------------------- 骨骼／插槽／約束

@dataclass(eq=False)
class BoneData:
    index: int
    name: str
    parent: Optional["BoneData"]
    length: float = 0.0
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    scaleX: float = 1.0
    scaleY: float = 1.0
    shearX: float = 0.0
    shearY: float = 0.0
    transformMode: int = TM_NORMAL
    skinRequired: bool = False


@dataclass(eq=False)
class SlotData:
    index: int
    name: str
    bone: BoneData
    color: Color = (1.0, 1.0, 1.0, 1.0)
    darkColor: Optional[tuple] = None        # (r, g, b)；None 代表沒有設定 tint black
    attachmentName: Optional[str] = None
    blendMode: int = 0


@dataclass(eq=False)
class IkConstraintData:
    name: str
    order: int = 0
    skinRequired: bool = False
    bones: list = field(default_factory=list)
    target: Optional[BoneData] = None
    mix: float = 1.0
    softness: float = 0.0
    bendDirection: int = 1
    compress: bool = False
    stretch: bool = False
    uniform: bool = False


@dataclass(eq=False)
class TransformConstraintData:
    name: str
    order: int = 0
    skinRequired: bool = False
    bones: list = field(default_factory=list)
    target: Optional[BoneData] = None
    local: bool = False
    relative: bool = False
    offsetRotation: float = 0.0
    offsetX: float = 0.0
    offsetY: float = 0.0
    offsetScaleX: float = 0.0
    offsetScaleY: float = 0.0
    offsetShearY: float = 0.0
    rotateMix: float = 1.0
    translateMix: float = 1.0
    scaleMix: float = 1.0
    shearMix: float = 1.0


@dataclass(eq=False)
class PathConstraintData:
    name: str
    order: int = 0
    skinRequired: bool = False
    bones: list = field(default_factory=list)
    target: Optional[SlotData] = None
    positionMode: int = PM_PERCENT
    spacingMode: int = SM_LENGTH
    rotateMode: int = RM_TANGENT
    offsetRotation: float = 0.0
    position: float = 0.0
    spacing: float = 0.0
    rotateMix: float = 1.0
    translateMix: float = 1.0


@dataclass(eq=False)
class EventData:
    name: str
    intValue: int = 0
    floatValue: float = 0.0
    stringValue: Optional[str] = None
    audioPath: Optional[str] = None
    volume: float = 1.0
    balance: float = 0.0


# ---------------------------------------------------------------- 附件

class Attachment:
    kind = "attachment"

    def __init__(self, name: str):
        self.name = name


class RegionAttachment(Attachment):
    """矩形貼圖。四個角的區域座標在 update_offset() 算好，照 spine-cpp 的公式。"""
    kind = "region"

    def __init__(self, name: str, path: str):
        super().__init__(name)
        self.path = path
        self.x = self.y = self.rotation = 0.0
        self.scaleX = self.scaleY = 1.0
        self.width = self.height = 0.0
        self.color: Color = (1.0, 1.0, 1.0, 1.0)
        self.region = None            # atlas.AtlasRegion；找不到就是 None
        self.offset = np.zeros((4, 2))   # 骨骼座標系下的 bl, ul, ur, br
        self.uvs = np.zeros((4, 2))      # 與 offset 同順序，0~1

    def update_offset(self) -> None:
        r = self.region
        if r is None:
            # 沒有貼圖仍算出外框，讓 doctor／標註看得到它應該在哪
            rw, rh, ow, oh, ox, oy = self.width, self.height, self.width, self.height, 0, 0
        else:
            rw, rh = r.width, r.height
            ow, oh = r.originalWidth or rw, r.originalHeight or rh
            ox, oy = r.offsetX, r.offsetY
        ow = ow or 1.0
        oh = oh or 1.0
        region_sx = self.width / ow * self.scaleX
        region_sy = self.height / oh * self.scaleY
        local_x = -self.width / 2 * self.scaleX + ox * region_sx
        local_y = -self.height / 2 * self.scaleY + oy * region_sy
        local_x2 = local_x + rw * region_sx
        local_y2 = local_y + rh * region_sy
        cos = np.cos(np.radians(self.rotation))
        sin = np.sin(np.radians(self.rotation))
        x, y = self.x, self.y
        lxc, lxs = local_x * cos + x, local_x * sin
        lyc, lys = local_y * cos + y, local_y * sin
        lx2c, lx2s = local_x2 * cos + x, local_x2 * sin
        ly2c, ly2s = local_y2 * cos + y, local_y2 * sin
        self.offset = np.array([
            [lxc - lys, lyc + lxs],        # bl
            [lxc - ly2s, ly2c + lxs],      # ul
            [lx2c - ly2s, ly2c + lx2s],    # ur
            [lx2c - lys, lyc + lx2s],      # br
        ])
        if r is not None:
            u, v, u2, v2 = r.u, r.v, r.u2, r.v2
            if r.rotate:
                self.uvs = np.array([[u2, v2], [u, v2], [u, v], [u2, v]])
            else:
                self.uvs = np.array([[u, v2], [u, v], [u2, v], [u2, v2]])


class VertexAttachment(Attachment):
    """有頂點的附件（mesh、bounding box、path、clipping）。

    `bones` 為 None 是未加權頂點：vertices 就是骨骼座標系下的 x, y。
    加權頂點沿用 spine 的扁平格式：bones = [數量, 骨骼索引...]，vertices = [x, y, 權重...]。
    prepare() 會另外展開成 numpy 陣列，算每一格頂點時才不必跑 Python 迴圈。
    """
    kind = "vertex"

    def __init__(self, name: str):
        super().__init__(name)
        self.bones: Optional[list] = None
        self.vertices: list = []
        self.worldVerticesLength = 0
        self.deformAttachment: "VertexAttachment" = self
        self._prepared = None

    @property
    def weighted(self) -> bool:
        return bool(self.bones)

    def prepare(self):
        if self._prepared is not None:
            return self._prepared
        if not self.bones:
            local = np.asarray(self.vertices, dtype=np.float64).reshape(-1, 2)
            self._prepared = ("plain", local)
        else:
            ebone, ex, ey, ew, evert = [], [], [], [], []
            v = 0
            b = 0
            vertex = 0
            bones = self.bones
            verts = self.vertices
            while v < len(bones):
                n = bones[v]
                v += 1
                for _ in range(n):
                    ebone.append(bones[v])
                    ex.append(verts[b])
                    ey.append(verts[b + 1])
                    ew.append(verts[b + 2])
                    evert.append(vertex)
                    v += 1
                    b += 3
                vertex += 1
            self._prepared = ("weighted", np.asarray(ebone, dtype=np.int64),
                              np.asarray(ex), np.asarray(ey), np.asarray(ew),
                              np.asarray(evert, dtype=np.int64), vertex)
        return self._prepared


class MeshAttachment(VertexAttachment):
    kind = "mesh"

    def __init__(self, name: str, path: str):
        super().__init__(name)
        self.path = path
        self.color: Color = (1.0, 1.0, 1.0, 1.0)
        self.regionUVs: list = []
        self.triangles: list = []
        self.hullLength = 0
        self.edges: list = []
        self.width = self.height = 0.0
        self.region = None
        self.uvs = np.zeros((0, 2))
        self.parentMesh: Optional["MeshAttachment"] = None
        self.linked: Optional[tuple] = None      # (skin 名稱, parent 名稱, inheritDeform)
        self._tri_array = None

    def triangle_array(self) -> np.ndarray:
        """三角形索引的 (N, 3) 陣列，第一次用到時建好、之後共用（唯讀）。

        讀檔完成後 triangles 就不再變；多個執行緒同時第一次呼叫只會各建一份相同的陣列，
        誰的留下來都一樣，所以不需要上鎖。
        """
        arr = self._tri_array
        if arr is None:
            arr = np.asarray(self.triangles, dtype=np.int64).reshape(-1, 3)
            arr.setflags(write=False)
            self._tri_array = arr
        return arr

    def set_parent_mesh(self, parent: "MeshAttachment") -> None:
        self.parentMesh = parent
        self.bones = parent.bones
        self.vertices = parent.vertices
        self.worldVerticesLength = parent.worldVerticesLength
        self.regionUVs = parent.regionUVs
        self.triangles = parent.triangles
        self.hullLength = parent.hullLength
        self.edges = parent.edges
        self.width, self.height = parent.width, parent.height
        self._prepared = None
        self._tri_array = None

    def update_uvs(self) -> None:
        """照 spine-cpp 3.8 MeshAttachment::updateUVs，含裁邊（whitespace strip）補償。"""
        ruv = np.asarray(self.regionUVs, dtype=np.float64).reshape(-1, 2)
        r = self.region
        if r is None:
            self.uvs = ruv.copy()
            return
        u, v = r.u, r.v
        du = (r.u2 - r.u) or 1e-12
        dv = (r.v2 - r.v) or 1e-12
        ow = r.originalWidth or r.width
        oh = r.originalHeight or r.height
        out = np.empty_like(ruv)
        if r.degrees == 90:
            tw = r.height / du
            th = r.width / dv
            u -= (oh - r.offsetY - r.height) / tw
            v -= (ow - r.offsetX - r.width) / th
            w, h = oh / tw, ow / th
            out[:, 0] = u + ruv[:, 1] * w
            out[:, 1] = v + (1 - ruv[:, 0]) * h
        elif r.degrees == 180:
            tw = r.width / du
            th = r.height / dv
            u -= (ow - r.offsetX - r.width) / tw
            v -= r.offsetY / th
            w, h = ow / tw, oh / th
            out[:, 0] = u + (1 - ruv[:, 0]) * w
            out[:, 1] = v + (1 - ruv[:, 1]) * h
        elif r.degrees == 270:
            th = r.height / dv
            tw = r.width / du
            u -= r.offsetY / tw
            v -= r.offsetX / th
            w, h = oh / tw, ow / th
            out[:, 0] = u + (1 - ruv[:, 1]) * w
            out[:, 1] = v + ruv[:, 0] * h
        else:
            tw = r.width / du
            th = r.height / dv
            u -= r.offsetX / tw
            v -= (oh - r.offsetY - r.height) / th
            w, h = ow / tw, oh / th
            out[:, 0] = u + ruv[:, 0] * w
            out[:, 1] = v + ruv[:, 1] * h
        self.uvs = out


class BoundingBoxAttachment(VertexAttachment):
    kind = "boundingbox"


class PathAttachment(VertexAttachment):
    kind = "path"

    def __init__(self, name: str):
        super().__init__(name)
        self.closed = False
        self.constantSpeed = True
        self.lengths: list = []


class PointAttachment(Attachment):
    kind = "point"

    def __init__(self, name: str):
        super().__init__(name)
        self.x = self.y = self.rotation = 0.0


class ClippingAttachment(VertexAttachment):
    kind = "clipping"

    def __init__(self, name: str):
        super().__init__(name)
        self.endSlot: Optional[SlotData] = None


# ---------------------------------------------------------------- 造型、動畫、整份資料

class Skin:
    def __init__(self, name: str):
        self.name = name
        self.attachments: dict[tuple[int, str], Attachment] = {}
        self.bones: list = []
        self.constraints: list = []

    def set_attachment(self, slot_index: int, name: str, attachment: Attachment) -> None:
        self.attachments[(slot_index, name)] = attachment

    def get_attachment(self, slot_index: int, name: str):
        return self.attachments.get((slot_index, name))

    def add_skin(self, other: "Skin") -> None:
        """spine 3.8 的 Skin::addSkin：合併骨骼、約束與附件（後者覆蓋前者）。"""
        for bone in other.bones:
            if bone not in self.bones:
                self.bones.append(bone)
        for constraint in other.constraints:
            if constraint not in self.constraints:
                self.constraints.append(constraint)
        self.attachments.update(other.attachments)


class Animation:
    def __init__(self, name: str, timelines: list, duration: float):
        self.name = name
        self.timelines = timelines
        self.duration = duration


@dataclass
class SkeletonData:
    hash: Optional[str] = None
    version: Optional[str] = None
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    fps: float = 0.0
    imagesPath: Optional[str] = None
    audioPath: Optional[str] = None
    bones: list = field(default_factory=list)
    slots: list = field(default_factory=list)
    skins: list = field(default_factory=list)
    defaultSkin: Optional[Skin] = None
    events: list = field(default_factory=list)
    animations: list = field(default_factory=list)
    ikConstraints: list = field(default_factory=list)
    transformConstraints: list = field(default_factory=list)
    pathConstraints: list = field(default_factory=list)
    format: str = "binary"     # binary / json

    def find_skin(self, name: str) -> Optional[Skin]:
        for skin in self.skins:
            if skin is not None and skin.name == name:
                return skin
        return None

    def find_animation(self, name: str) -> Optional[Animation]:
        for anim in self.animations:
            if anim.name == name:
                return anim
        return None

    def find_bone(self, name: str) -> Optional[BoneData]:
        for bone in self.bones:
            if bone.name == name:
                return bone
        return None

    def find_slot(self, name: str) -> Optional[SlotData]:
        for slot in self.slots:
            if slot.name == name:
                return slot
        return None

    def iter_attachments(self):
        """(skin, slotIndex, 名稱, 附件)，給 doctor／info 統計用。"""
        for skin in self.skins:
            if skin is None:
                continue
            for (slot_index, name), att in skin.attachments.items():
                yield skin, slot_index, name, att
