"""把擺好姿勢的骨架畫成圖片（純 numpy 的軟體光柵化）。

人在網頁上看到的畫面與 agent 用 CLI 拿到的 PNG 都由這裡算出，兩邊不會有落差。

繪製規則照 Cocos2d-x 的 SkeletonRenderer：
- 依 drawOrder 逐插槽畫；附件為空、骨骼未啟用、插槽或附件 alpha 為 0 的不畫
- 顏色 = 骨架色 × 插槽色 × 附件色，預乘 alpha（setOpacityModifyRGB(true) 是預設）
- 貼圖載入時預乘 alpha（Cocos2d-x 載 PNG 的預設行為）
- 混色：normal (ONE, 1-SRC_A)、additive (ONE, ONE)、multiply (DST_COLOR, 1-SRC_A)、
  screen (ONE, 1-SRC_COLOR)
- clipping 附件把之後的插槽裁進多邊形，直到 end slot 為止
- 引擎預設不開 two color tint（tint black），所以 dark color 預設不套用

三角形用像素中心取樣、貼圖雙線性取樣，沒有抗鋸齒；需要平滑邊緣時用 ss（超取樣）。
"""
from __future__ import annotations

import math
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .animation import animation_events, apply_animation
from .atlas import load_texture_pair
from .loader import Document
from .model import (BLEND_MODES, BoundingBoxAttachment, ClippingAttachment,
                    MeshAttachment, PathAttachment, PointAttachment,
                    RegionAttachment, Skin)
from .skeleton import Skeleton, world_vertices

_QUAD = np.array([[3, 0, 1], [1, 2, 3]], dtype=np.int64)
# 只給測試用：False 時不套用「遮罩外的像素不算」的捷徑，tests/golden.py 用它證明捷徑不改變結果
CLIP_SHORTCUT = True


def frame_to_time(frame: int, fps: float, duration: Optional[float] = None) -> float:
    """第幾格 → 秒，跟 Spine 編輯器一樣用 float32 算。

    關鍵影格的時間在檔案裡是 float32（例如第 40 格 @30fps 存成 1.3333334），
    用 double 算 40/30 = 1.3333333 會剛好比它小一點，那一格的關鍵影格要到下一格才生效——
    點「#40 換附件」跳過去卻看到還沒換。用同樣的 float32 捨入就會剛好相等。
    """
    t = float(np.float32(frame / fps))
    if duration is not None:
        t = min(t, duration)
    return max(0.0, t)


@dataclass
class RenderOptions:
    scale: float = 1.0
    background: str = "checker"      # checker / none / #rrggbb
    padding: int = 16
    bones: bool = False              # 疊上骨骼
    annotate: bool = False           # 疊上插槽外框與名稱
    bboxes: bool = False             # 疊上 bounding box 附件
    ss: int = 1                      # 超取樣倍率
    tint_black: bool = False         # 套用 dark color（引擎預設關閉）
    bounds: str = "auto"             # auto / anim / frame / setup / data
    viewport: Optional[tuple] = None  # 世界座標 (x, y, w, h)，指定就不自動取景
    max_pixels: int = 16_000_000
    layout_only: bool = False        # 只算座標、不畫圖（tree／spec 用）
    threads: int = 0                 # 光柵化的執行緒數；0 = 自動（大畫布才平行）

    def key(self) -> tuple:
        return (self.scale, self.background, self.padding, self.bones, self.annotate,
                self.bboxes, self.ss, self.tint_black, self.bounds, self.viewport, self.threads,
                self.max_pixels, self.layout_only)


@dataclass
class SlotReport:
    order: int
    index: int
    name: str
    bone: str
    attachment: Optional[str]
    kind: Optional[str]
    blend: str
    color: tuple
    state: str                      # drawn / hidden / empty / alpha0 / inactive / missingRegion / clipping / nonVisual
    rect: Optional[tuple] = None    # 畫面座標 (x, y, w, h)，原點左上
    world: Optional[tuple] = None   # 世界座標 (minX, minY, maxX, maxY)，Y 向上
    clippedBy: Optional[str] = None
    path: Optional[str] = None
    vertices: int = 0

    def to_dict(self) -> dict:
        out = {"order": self.order, "name": self.name, "bone": self.bone,
               "attachment": self.attachment, "attachmentType": self.kind, "blend": self.blend,
               "state": self.state}
        if any(abs(c - 1.0) > 1e-4 for c in self.color):
            out["color"] = [round(c, 3) for c in self.color]
        if self.rect is not None:
            out["screenRect"] = [round(v, 1) for v in self.rect]
        if self.world is not None:
            out["worldBounds"] = [round(v, 2) for v in self.world]
        if self.clippedBy:
            out["clippedBy"] = self.clippedBy
        if self.path and self.path != self.attachment:
            out["region"] = self.path
        if self.vertices:
            out["vertices"] = self.vertices
        return out


@dataclass
class BoneReport:
    index: int
    name: str
    parent: Optional[str]
    world: tuple                    # (x, y) 世界座標
    screen: tuple                   # (x, y) 畫面座標
    rotation: float                 # 世界旋轉（度，逆時針）
    scale: tuple
    local: dict
    length: float
    active: bool
    children: list = field(default_factory=list)

    def to_dict(self, nested: bool = False) -> dict:
        out = {"name": self.name, "parent": self.parent,
               "world": [round(v, 2) for v in self.world],
               "screen": [round(v, 1) for v in self.screen],
               "rotation": round(self.rotation, 2),
               "scale": [round(v, 3) for v in self.scale],
               "local": {k: round(v, 3) for k, v in self.local.items()},
               "length": round(self.length, 2)}
        if not self.active:
            out["active"] = False
        if nested:
            out["children"] = [c.to_dict(True) for c in self.children]
        return out


@dataclass
class RenderResult:
    image: Optional[Image.Image]
    size: tuple
    animation: str
    time: float
    frame: Optional[int]
    skin: Optional[str]
    viewport: tuple                 # 世界座標 (minX, minY, maxX, maxY)
    scale: float
    padding: int
    clamped: bool
    slots: list
    bones: list
    missing: list
    events: list

    @property
    def origin(self) -> tuple:
        """世界原點 (0, 0) 在圖上的位置——遊戲裡 SkeletonAnimation 節點的座標就在這。"""
        return (round(float((0 - self.viewport[0]) * self.scale + self.padding), 1),
                round(float((self.viewport[3] - 0) * self.scale + self.padding), 1))


# ---------------------------------------------------------------- 繪製清單

@dataclass
class _Item:
    slot: object
    att: object
    xy: np.ndarray          # 世界座標頂點
    uv: np.ndarray          # 0~1
    tris: np.ndarray
    color: tuple            # 未預乘
    dark: Optional[tuple]
    blend: int
    texture: Optional[str]
    pma: bool
    clip: Optional[tuple]   # (clip 插槽名, 世界座標多邊形)


def _collect(sk: Skeleton, hidden: set, only: Optional[set],
             reports: Optional[list] = None, bounds: bool = True):
    """依 drawOrder 走一遍，回傳要畫的項目；同時填寫每個插槽的狀態報告。

    bounds=False 時報告不算世界座標外框（每個插槽要 5 次 numpy 呼叫，網頁的影格資料用不到）。
    """
    items: list[_Item] = []
    bone_arrays = sk.bone_arrays()
    clip_slot = None        # 正在作用的 clipping 插槽
    clip_end_index = -1
    clip_poly = None
    skc = sk.color

    def clip_end(slot) -> None:
        nonlocal clip_slot, clip_poly, clip_end_index
        if clip_slot is not None and clip_end_index == slot.data.index:
            clip_slot = None
            clip_poly = None
            clip_end_index = -1

    for order, slot in enumerate(sk.drawOrder):
        att = slot.attachment
        data = slot.data
        report = None
        if reports is not None:
            report = SlotReport(order=order, index=data.index, name=data.name,
                                bone=data.bone.name,
                                attachment=getattr(att, "name", None),
                                kind=getattr(att, "kind", None),
                                blend=BLEND_MODES[data.blendMode] if data.blendMode < 4 else str(data.blendMode),
                                color=tuple(slot.color), state="drawn",
                                path=getattr(att, "path", None))
            reports.append(report)
        is_hidden = data.name in hidden or (only is not None and data.name not in only
                                            and not isinstance(att, ClippingAttachment))
        if att is None:
            if report:
                report.state = "empty"
            clip_end(slot)
            continue
        if not slot.bone.active:
            if report:
                report.state = "inactive"
            clip_end(slot)
            continue
        if slot.color[3] == 0 or (isinstance(att, (RegionAttachment, MeshAttachment))
                                  and att.color[3] == 0):
            if report:
                report.state = "alpha0"
            clip_end(slot)
            continue
        if is_hidden:
            if report:
                report.state = "hidden"
            clip_end(slot)
            continue

        if isinstance(att, ClippingAttachment):
            if report:
                report.state = "clipping"
            if clip_slot is None:
                poly = world_vertices(att, slot, bone_arrays)
                clip_slot = data.name
                clip_end_index = att.endSlot.index if att.endSlot is not None else -1
                clip_poly = poly
                if report is not None and len(poly):
                    report.world = (float(poly[:, 0].min()), float(poly[:, 1].min()),
                                    float(poly[:, 0].max()), float(poly[:, 1].max()))
            continue
        if not isinstance(att, (RegionAttachment, MeshAttachment)):
            if report:
                report.state = "nonVisual"
            clip_end(slot)
            continue

        xy = world_vertices(att, slot, bone_arrays)
        color = (skc[0] * slot.color[0] * att.color[0], skc[1] * slot.color[1] * att.color[1],
                 skc[2] * slot.color[2] * att.color[2], skc[3] * slot.color[3] * att.color[3])
        if isinstance(att, RegionAttachment):
            uv, tris = att.uvs, _QUAD
        else:
            uv = att.uvs
            tris = att.triangle_array()
        region = att.region
        if report is not None:
            report.vertices = len(xy)
            if bounds:
                finite = xy[np.isfinite(xy).all(axis=1)]
                if len(finite):
                    report.world = (float(finite[:, 0].min()), float(finite[:, 1].min()),
                                    float(finite[:, 0].max()), float(finite[:, 1].max()))
            if clip_slot is not None:
                report.clippedBy = clip_slot
            if region is None:
                report.state = "missingRegion"
        if region is not None:
            items.append(_Item(slot=slot, att=att, xy=xy, uv=uv, tris=tris, color=color,
                               dark=slot.darkColor, blend=data.blendMode,
                               texture=region.page.path, pma=region.page.pma,
                               clip=(clip_slot, clip_poly) if clip_slot is not None else None))
        clip_end(slot)
    return items


def _item_bounds(items) -> Optional[tuple]:
    """看得到的內容外框。被 clipping 裁掉的部分不算——特效常把元件擺在遮罩外等著滑進來，
    算進去的話畫布會大到幾千像素、內容只佔中間一小塊。"""
    box = None
    clip_boxes: dict = {}
    for it in items:
        xy = it.xy
        finite = xy[np.isfinite(xy).all(axis=1)]
        if not len(finite):
            continue
        b = (finite[:, 0].min(), finite[:, 1].min(), finite[:, 0].max(), finite[:, 1].max())
        if it.clip is not None:
            key = id(it.clip[1])
            if key not in clip_boxes:
                poly = it.clip[1]
                poly = poly[np.isfinite(poly).all(axis=1)]
                clip_boxes[key] = ((poly[:, 0].min(), poly[:, 1].min(),
                                    poly[:, 0].max(), poly[:, 1].max()) if len(poly) else None)
            c = clip_boxes[key]
            if c is None:
                continue
            b = (max(b[0], c[0]), max(b[1], c[1]), min(b[2], c[2]), min(b[3], c[3]))
            if b[0] >= b[2] or b[1] >= b[3]:
                continue
        box = _union(box, b)
    return box


def _union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


# ---------------------------------------------------------------- 光柵化

def _blend(dst: np.ndarray, src: np.ndarray, mode: int) -> np.ndarray:
    """兩邊都是預乘 alpha 的 (K, 4)。"""
    sa = src[:, 3:4]
    if mode == 1:      # additive: ONE, ONE
        return np.minimum(src + dst, 1.0)
    if mode == 2:      # multiply: DST_COLOR, ONE_MINUS_SRC_ALPHA
        return src * dst + dst * (1.0 - sa)
    if mode == 3:      # screen: ONE, ONE_MINUS_SRC_COLOR
        out = np.empty_like(dst)
        out[:, :3] = src[:, :3] + dst[:, :3] * (1.0 - src[:, :3])
        out[:, 3:] = sa + dst[:, 3:] * (1.0 - sa)
        return out
    return src + dst * (1.0 - sa)     # normal: ONE, ONE_MINUS_SRC_ALPHA


def _rasterize(canvas: np.ndarray, xy: np.ndarray, uvpx: np.ndarray, tris: np.ndarray,
               tex: np.ndarray, nonzero: Optional[np.ndarray], color_pm: np.ndarray,
               dark: Optional[np.ndarray], blend: int, clip: Optional[tuple],
               rows_range: Optional[tuple] = None) -> None:
    """clip 是 _clip_mask 的結果 (遮罩, 遮罩外框)；nonzero 是貼圖的非零表（可為 None）。
    rows_range=(起, 迄) 時只畫這幾列（平行渲染用）。"""
    H, W = canvas.shape[:2]
    finite = np.isfinite(xy).all(axis=1)
    if not finite.any():
        return
    fx = xy[finite]
    x0 = max(int(math.floor(fx[:, 0].min())), 0)
    x1 = min(int(math.ceil(fx[:, 0].max())) + 1, W)
    y0 = max(int(math.floor(fx[:, 1].min())), 0)
    y1 = min(int(math.ceil(fx[:, 1].max())) + 1, H)
    if rows_range is not None:
        y0, y1 = max(y0, rows_range[0]), min(y1, rows_range[1])
    mask = None
    if clip is not None and not CLIP_SHORTCUT:
        mask = clip[0]
    elif clip is not None:
        # 遮罩外的像素乘上 0，對四種混色都完全不改變畫布（normal：0 + dst×1；additive：min(dst, 1)；
        # multiply：0×dst + dst×1；screen：0 + dst×1），所以直接不算——特效常把大量內容擺在遮罩外
        mask, box = clip
        if box is None:
            return
        x0, y0 = max(x0, box[0]), max(y0, box[1])
        x1, y1 = min(x1, box[2]), min(y1, box[3])
    if x0 >= x1 or y0 >= y1:
        return
    bw, bh = x1 - x0, y1 - y0
    U = np.zeros((bh, bw), dtype=np.float32)
    V = np.zeros((bh, bw), dtype=np.float32)
    M = np.zeros((bh, bw), dtype=bool)

    px = xy[:, 0]
    py = xy[:, 1]
    for i0, i1, i2 in tris:
        if not (finite[i0] and finite[i1] and finite[i2]):
            continue
        ax, ay, bx, by, cx, cy = px[i0], py[i0], px[i1], py[i1], px[i2], py[i2]
        area = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        if abs(area) < 1e-9:
            continue
        bx0 = int(math.floor(min(ax, bx, cx)))
        by0 = int(math.floor(min(ay, by, cy)))
        tx0 = max(bx0, x0)
        tx1 = min(int(math.ceil(max(ax, bx, cx))) + 1, x1)
        ty0 = max(by0, y0)
        ty1 = min(int(math.ceil(max(ay, by, cy))) + 1, y1)
        if tx0 >= tx1 or ty0 >= ty1:
            continue
        inv = 1.0 / area
        # 重心座標 λ = a·x + b·y + c（已除以面積），三個都 ≥ 0 就在三角形內。
        # 原點用三角形「未裁切」外框的左上角：座標小、float32 精度夠，而且同一個像素
        # 不論被畫布邊緣或 clipping 外框裁掉多少，算出來的值都一模一樣（裁切只決定算哪些像素）。
        ox, oy = bx0 + 0.5, by0 + 0.5
        a0, b0 = -(cy - by) * inv, (cx - bx) * inv
        c0 = ((cx - bx) * (oy - by) - (cy - by) * (ox - bx)) * inv
        a1, b1 = -(ay - cy) * inv, (ax - cx) * inv
        c1 = ((ax - cx) * (oy - cy) - (ay - cy) * (ox - cx)) * inv
        xs = np.arange(tx0 - bx0, tx1 - bx0, dtype=np.float32)
        ys = np.arange(ty0 - by0, ty1 - by0, dtype=np.float32)
        l0 = (np.float32(a0) * xs)[None, :] + (np.float32(b0) * ys + np.float32(c0))[:, None]
        l1 = (np.float32(a1) * xs)[None, :] + (np.float32(b1) * ys + np.float32(c1))[:, None]
        inside = (l0 >= 0) & (l1 >= 0) & (l0 + l1 <= 1)
        rr, cc = np.nonzero(inside)
        if rr.size == 0:
            continue
        # u、v 對畫面座標是仿射的，只對三角形內的像素求值
        du0, du1 = uvpx[i0, 0] - uvpx[i2, 0], uvpx[i1, 0] - uvpx[i2, 0]
        dv0, dv1 = uvpx[i0, 1] - uvpx[i2, 1], uvpx[i1, 1] - uvpx[i2, 1]
        X = xs[cc]
        Y = ys[rr]
        u = (np.float32(a0 * du0 + a1 * du1) * X + np.float32(b0 * du0 + b1 * du1) * Y
             + np.float32(uvpx[i2, 0] + c0 * du0 + c1 * du1))
        v = (np.float32(a0 * dv0 + a1 * dv1) * X + np.float32(b0 * dv0 + b1 * dv1) * Y
             + np.float32(uvpx[i2, 1] + c0 * dv0 + c1 * dv1))
        rr = rr + (ty0 - y0)
        cc = cc + (tx0 - x0)
        U[rr, cc] = u
        V[rr, cc] = v
        M[rr, cc] = True

    rows, cols = np.nonzero(M)
    if mask is not None and CLIP_SHORTCUT and rows.size:
        inside = mask[y0 + rows, x0 + cols] > 0       # 理由同上：遮罩值 0 的像素不影響結果
        rows, cols = rows[inside], cols[inside]
    if rows.size == 0:
        return
    th, tw = tex.shape[:2]
    flat = tex.reshape(-1, 4)
    u = U[rows, cols] - 0.5
    v = V[rows, cols] - 0.5
    iu = np.floor(u)
    iv = np.floor(v)
    fu = (u - iu)[:, None]
    fv = (v - iv)[:, None]
    iu = iu.astype(np.int64)
    iv = iv.astype(np.int64)
    u0 = np.clip(iu, 0, tw - 1)
    u1 = np.clip(iu + 1, 0, tw - 1)
    r0 = np.clip(iv, 0, th - 1) * tw
    r1 = np.clip(iv + 1, 0, th - 1) * tw
    i00, i01, i10, i11 = r0 + u0, r0 + u1, r1 + u0, r1 + u1
    if nonzero is not None:
        # 四個取樣點都是全 0 的貼圖像素時，雙線性內插的結果一定是 0、下面也會被丟掉；
        # 先用 1 byte 的非零表挑掉，省下 4 次 16 bytes 的取樣（特效貼圖大片透明）
        hit = nonzero[i00] | nonzero[i01] | nonzero[i10] | nonzero[i11]
        if not hit.all():
            rows, cols, fu, fv = rows[hit], cols[hit], fu[hit], fv[hit]
            i00, i01, i10, i11 = i00[hit], i01[hit], i10[hit], i11[hit]
            if rows.size == 0:
                return
    # 雙線性內插：運算與原本的
    #   ((t00*(1-fu) + t01*fu)*(1-fv) + (t10*(1-fu) + t11*fu)*fv)
    # 完全相同、順序相同（只是改成就地運算省掉暫存陣列），結果逐位元一樣
    gu = 1 - fu
    sample = flat[i00]
    sample *= gu
    t = flat[i01]
    t *= fu
    sample += t
    sample *= 1 - fv
    low = flat[i10]
    low *= gu
    t = flat[i11]
    t *= fu
    low += t
    low *= fv
    sample += low
    # 四個通道全是 0 的貼圖像素對四種混色（含 two color tint）都沒有影響，先丟掉——
    # 特效貼圖大多是透明的。用 max 而不是只看 alpha：已預乘的貼圖（pma）
    # 可能有 alpha 0、rgb 非 0 的加法發光像素，那些仍然要畫。
    keep = sample.max(axis=1) > 0
    if not keep.all():
        rows, cols, sample = rows[keep], cols[keep], sample[keep]
        if rows.size == 0:
            return
    if dark is not None:
        # two color tint 的 shader（預乘版本，dark.a = 1）
        src = np.empty_like(sample)
        ta = sample[:, 3:4]
        src[:, :3] = (ta - sample[:, :3]) * dark[None, :] + sample[:, :3] * color_pm[None, :3]
        src[:, 3:] = ta * color_pm[3]
    else:
        src = sample * color_pm[None, :]
    if mask is not None:
        src *= mask[y0 + rows, x0 + cols][:, None]
    region = canvas[y0:y1, x0:x1]
    region[rows, cols] = _blend(region[rows, cols], src, blend)


_POOL = None
_POOL_LOCK = threading.Lock()
_PARALLEL_MIN_PIXELS = 250_000


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            from concurrent.futures import ThreadPoolExecutor
            _POOL = ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1),
                                       thread_name_prefix="spineview-raster")
        return _POOL


def _paint(canvas: np.ndarray, jobs: list, threads: int) -> None:
    """依序把所有附件畫上畫布；大畫布切成橫條平行畫。

    為什麼平行之後結果完全相同：
    - 每個像素只受「畫在它上面的附件、依繪製順序」影響，跟別的像素無關；
      每一條各自依同樣的順序畫完所有附件，只寫自己那幾列
    - 像素的值只由三角形與像素位置決定（_rasterize 用未裁切的三角形外框當原點），
      跟它屬於哪一條、被裁掉多少無關
    tests/golden.py 用逐像素比對驗證過。
    """
    H, W = canvas.shape[:2]
    if threading.current_thread().name.startswith("spineview-raster"):
        threads = 1          # 已經在光柵化的執行緒裡：再丟工作給同一個池子等它，池子滿了會死結
    elif threads <= 0:
        threads = min(8, os.cpu_count() or 1) if H * W >= _PARALLEL_MIN_PIXELS else 1
    threads = max(1, min(threads, H // 16 or 1))
    if threads == 1 or not jobs:
        for job in jobs:
            _rasterize(canvas, *job)
        return
    edges = [H * k // threads for k in range(threads + 1)]

    def band(k):
        rows = (edges[k], edges[k + 1])
        for job in jobs:
            _rasterize(canvas, *job, rows_range=rows)

    list(_pool().map(band, range(threads)))


def _clip_mask(poly_screen: np.ndarray, width: int, height: int) -> tuple:
    """回傳 (遮罩, 遮罩非零區域的外框 (x0, y0, x1, y1))；遮罩全空時外框是 None。"""
    mask = Image.new("L", (width, height), 0)
    pts = [(float(x), float(y)) for x, y in poly_screen if math.isfinite(x) and math.isfinite(y)]
    if len(pts) >= 3:
        ImageDraw.Draw(mask).polygon(pts, fill=255)
    box = mask.getbbox()
    return np.asarray(mask, dtype=np.float32) / 255.0, box


# ---------------------------------------------------------------- 背景與疊圖

def _parse_color(text: str) -> tuple:
    text = (text or "").strip().lstrip("#")
    if len(text) in (3, 4):
        text = "".join(c * 2 for c in text)
    try:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        a = int(text[6:8], 16) if len(text) >= 8 else 255
    except (ValueError, IndexError):
        return (0, 0, 0, 255)
    return (r, g, b, a)


def _background(width: int, height: int, bg: str, cell: int) -> np.ndarray:
    canvas = np.zeros((height, width, 4), dtype=np.float32)
    if bg in ("none", "transparent", ""):
        return canvas
    if bg == "checker":
        yy, xx = np.mgrid[0:height, 0:width]
        light = ((xx // cell + yy // cell) % 2 == 0)
        value = np.where(light, 0.30, 0.22).astype(np.float32)
        canvas[..., 0] = value
        canvas[..., 1] = value
        canvas[..., 2] = value + 0.015
        canvas[..., 3] = 1.0
        return canvas
    r, g, b, a = _parse_color(bg)
    alpha = a / 255.0
    canvas[...] = (r / 255.0 * alpha, g / 255.0 * alpha, b / 255.0 * alpha, alpha)
    return canvas


def _to_image(canvas: np.ndarray) -> Image.Image:
    alpha = canvas[..., 3:4]
    rgb = np.where(alpha > 1e-6, canvas[..., :3] / np.maximum(alpha, 1e-6), 0.0)
    out = np.concatenate([np.clip(rgb, 0, 1), np.clip(alpha, 0, 1)], axis=2)
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8), "RGBA")


_FONT = None
_FONT_LOCK = threading.Lock()


def _font():
    global _FONT
    with _FONT_LOCK:
        if _FONT is None:
            for name in ("msjh.ttc", "msyh.ttc", "NotoSansCJK-Regular.ttc", "arial.ttf",
                         "DejaVuSans.ttf"):
                try:
                    _FONT = ImageFont.truetype(name, 12)
                    break
                except OSError:
                    continue
            if _FONT is None:
                _FONT = ImageFont.load_default()
        return _FONT


# ---------------------------------------------------------------- Renderer

class Renderer:
    """一份 Document 加一組選項。取景範圍等計算結果會快取，可跨執行緒共用。"""

    BOUNDS_SAMPLES = 90

    def __init__(self, doc: Document, options: Optional[RenderOptions] = None):
        self.doc = doc
        self.options = options or RenderOptions()
        self._bounds_cache: OrderedDict = OrderedDict()
        self._bounds_gates: dict = {}
        self._lock = threading.Lock()

    # ---- 造型與姿勢
    def skin(self, name: Optional[str]) -> Optional[Skin]:
        """造型名稱；多個造型可用 + 或 , 組合（spine 3.8 的 Skin::addSkin）。"""
        data = self.doc.data
        if not name:
            return None
        exact = data.find_skin(name)
        if exact is not None:            # 造型名稱本身就含 + 或 , 時，先照整個名稱找
            return exact
        parts = [p.strip() for p in name.replace(",", "+").split("+") if p.strip()]
        found = []
        for part in parts:
            skin = data.find_skin(part)
            if skin is None:
                names = ", ".join(s.name for s in data.skins if s is not None)
                raise ValueError(f"找不到造型 {part!r}，可用的有：{names}")
            found.append(skin)
        if len(found) == 1:
            return found[0]
        combined = Skin("+".join(s.name for s in found))
        for skin in found:
            combined.add_skin(skin)
        return combined

    def default_skin_name(self) -> Optional[str]:
        """沒指定造型時用哪個。

        引擎不呼叫 setSkin 時只有 default 造型。但很多骨架的角色本體放在具名造型裡、
        default 只剩共用特效，照引擎預設畫出來會缺一大塊，看的人會以為壞了。
        所以：第一個具名造型能多提供一半以上「有附件可放的插槽」時，改用它。
        （不能只看 setup pose：不少骨架的附件全由動畫的 attachment 關鍵影格放上去。）
        實際用了哪個一律回報在 JSON 的 skin 欄位（info 的 skinUsedByDefault）。
        """
        data = self.doc.data
        named = [s for s in data.skins if s is not None and s.name != "default"]
        if not named:
            return None
        default = data.defaultSkin
        base = {i for (i, _n) in default.attachments} if default is not None else set()
        extra = {i for (i, _n) in named[0].attachments} | base
        if len(extra) > max(len(base) * 1.5, len(base) + 1):
            return named[0].name
        return None

    def animation(self, name: Optional[str]):
        if not name:
            return None
        anim = self.doc.data.find_animation(name)
        if anim is None:
            names = ", ".join(a.name for a in self.doc.data.animations)
            raise ValueError(f"找不到動畫 {name!r}，可用的有：{names}")
        return anim

    def new_skeleton(self, skin: Optional[Skin]) -> Skeleton:
        sk = Skeleton(self.doc.data)
        sk.set_skin(skin)
        return sk

    def pose(self, animation, time: float, skin: Optional[Skin],
             skeleton: Optional[Skeleton] = None) -> Skeleton:
        """擺出某一格的姿勢。

        連續擺很多格時傳入同一個 `skeleton`（用 new_skeleton(skin) 建的）可省去每格重建骨架：
        set_to_setup_pose 會把骨骼、約束、插槽、繪製順序全部重設，結果與新建的完全相同
        （tests/golden.py 逐項比對過）。骨架物件不可跨執行緒共用——各自建一個。
        """
        sk = skeleton if skeleton is not None else self.new_skeleton(skin)
        sk.set_to_setup_pose()
        if animation is not None:
            apply_animation(animation, sk, time)
        sk.update_world_transform()
        return sk

    # ---- 取景
    def frame_times(self, animation, step: int = 1) -> list[float]:
        if animation is None:
            return [0.0]
        fps = self.doc.fps
        last = int(round(animation.duration * fps))
        return [frame_to_time(f, fps, animation.duration) for f in range(0, last + 1, max(1, step))]

    def content_bounds(self, animation, skin_name: Optional[str], hidden: set,
                       only: Optional[set]) -> Optional[tuple]:
        """整段動畫所有影格的聯集外框。逐格算過，所以播放時角色不會跳動。"""
        key = (getattr(animation, "name", None), skin_name, tuple(sorted(hidden)),
               tuple(sorted(only)) if only is not None else None)
        with self._lock:
            if key in self._bounds_cache:
                self._bounds_cache.move_to_end(key)
                return self._bounds_cache[key]
            # 同一個外框同時只算一次：網頁一打開會平行發好幾個影格請求，
            # 每個都要取景，不擋的話大檔的 90 次擺姿勢會被重複算好幾遍
            gate = self._bounds_gates.setdefault(key, threading.Lock())
        with gate:
            with self._lock:
                if key in self._bounds_cache:
                    return self._bounds_cache[key]
            skin = self.skin(skin_name)
            box = None
            times = self.frame_times(animation)
            if len(times) > self.BOUNDS_SAMPLES:
                # 長動畫平均抽樣（頭尾一定算到）：取景只需要外框，不必每格都擺一次姿勢
                step = (len(times) - 1) / (self.BOUNDS_SAMPLES - 1)
                times = [times[int(round(i * step))] for i in range(self.BOUNDS_SAMPLES)]
            sk = self.new_skeleton(skin)
            for t in times:
                self.pose(animation, t, skin, sk)
                box = _union(box, _item_bounds(_collect(sk, hidden, only)))
            with self._lock:
                self._bounds_cache[key] = box
                # 上限：網頁切換隱藏插槽會產生新的鍵，不設限會一直長
                while len(self._bounds_cache) > 64:
                    self._bounds_cache.popitem(last=False)
                self._bounds_gates.pop(key, None)
            return box

    def _viewport(self, animation, skin_name, hidden, only, items) -> tuple:
        opts = self.options
        if opts.viewport:
            x, y, w, h = opts.viewport
            return (x, y, x + w, y + h)
        mode = opts.bounds
        if mode == "auto":
            mode = "anim" if animation is not None else "setup"
        box = None
        if mode == "frame":
            box = _item_bounds(items)
        elif mode == "setup":
            box = self.content_bounds(None, skin_name, hidden, only)
        elif mode == "data":
            d = self.doc.data
            if d.width > 0 and d.height > 0:
                box = (d.x, d.y, d.x + d.width, d.y + d.height)
        else:
            box = self.content_bounds(animation, skin_name, hidden, only)
        if box is None:
            box = _item_bounds(items)
        if box is None:
            box = (-50.0, -50.0, 50.0, 50.0)
        x0, y0, x1, y1 = (float(v) for v in box)
        if x1 - x0 < 1:
            x0, x1 = x0 - 0.5, x1 + 0.5
        if y1 - y0 < 1:
            y0, y1 = y0 - 0.5, y1 + 0.5
        return (x0, y0, x1, y1)

    # ---- 主流程
    def render(self, animation_name: Optional[str] = None, time: float = 0.0,
               skin_name: Optional[str] = "__default__", hidden=(), only=None,
               frame: Optional[int] = None) -> RenderResult:
        opts = self.options
        if skin_name == "__default__":
            skin_name = self.default_skin_name()
        hidden = set(hidden or ())
        only = set(only) if only else None
        animation = self.animation(animation_name)
        if animation is not None:
            time = max(0.0, min(time, animation.duration))
        skin = self.skin(skin_name)
        sk = self.pose(animation, time, skin)
        reports: list[SlotReport] = []
        items = _collect(sk, hidden, only, reports)

        vx0, vy0, vx1, vy1 = self._viewport(animation, skin_name, hidden, only, items)
        scale = opts.scale
        pad = opts.padding
        width = int(math.ceil((vx1 - vx0) * scale)) + pad * 2
        height = int(math.ceil((vy1 - vy0) * scale)) + pad * 2
        clamped = False
        if width * height > opts.max_pixels:
            factor = math.sqrt(opts.max_pixels / (width * height))
            scale *= factor
            width = int(math.ceil((vx1 - vx0) * scale)) + pad * 2
            height = int(math.ceil((vy1 - vy0) * scale)) + pad * 2
            clamped = True

        def to_screen(xy: np.ndarray, factor: float = 1.0) -> np.ndarray:
            out = np.empty_like(xy)
            out[:, 0] = ((xy[:, 0] - vx0) * scale + pad) * factor
            out[:, 1] = ((vy1 - xy[:, 1]) * scale + pad) * factor
            return out

        for rep in reports:
            if rep.world is not None:
                x0, y0, x1, y1 = rep.world
                rep.rect = ((x0 - vx0) * scale + pad, (vy1 - y1) * scale + pad,
                            (x1 - x0) * scale, (y1 - y0) * scale)

        bones = self._bone_reports(sk, vx0, vy1, scale, pad)
        events = []
        if animation is not None:
            half = 0.5 / self.doc.fps
            events = [e.to_dict() for e in animation_events(animation)
                      if abs(e.time - time) <= half + 1e-6]

        image = None
        if not opts.layout_only:
            ss = max(1, int(opts.ss))
            W, H = width * ss, height * ss
            canvas = _background(W, H, opts.background, 8 * ss)
            # 先在主執行緒把每個附件要用的東西都準備好（貼圖、畫面座標、遮罩），
            # 平行的部分只讀這些、只寫自己那幾列畫布——沒有共用的可變狀態
            masks: dict = {}
            jobs = []
            for it in items:
                pair = load_texture_pair(it.texture, it.pma)
                if pair is None:
                    continue
                tex, nonzero = pair
                xy = to_screen(it.xy, ss)
                th, tw = tex.shape[:2]
                uvpx = np.empty_like(it.uv)
                uvpx[:, 0] = it.uv[:, 0] * tw
                uvpx[:, 1] = it.uv[:, 1] * th
                r, g, b, a = it.color
                color_pm = np.array([r * a, g * a, b * a, a], dtype=np.float32)
                dark = (np.asarray(it.dark, dtype=np.float32)
                        if opts.tint_black and it.dark is not None else None)
                clip = None
                if it.clip is not None:
                    name, poly = it.clip
                    key = (name, id(poly))
                    if key not in masks:
                        masks[key] = _clip_mask(to_screen(poly, ss), W, H)
                    clip = masks[key]
                jobs.append((xy, uvpx, it.tris, tex, nonzero, color_pm, dark, it.blend, clip))
            _paint(canvas, jobs, opts.threads)
            if ss > 1:
                canvas = canvas.reshape(height, ss, width, ss, 4).mean(axis=(1, 3))
            image = _to_image(canvas)
            if opts.annotate or opts.bones or opts.bboxes:
                self._overlay(image, sk, reports, bones, to_screen)

        return RenderResult(
            image=image, size=(width, height),
            animation=getattr(animation, "name", "") or "", time=time, frame=frame,
            skin=skin_name, viewport=(vx0, vy0, vx1, vy1), scale=scale, padding=pad,
            clamped=clamped, slots=reports, bones=bones,
            missing=list(self.doc.missing_regions), events=events)

    def _bone_reports(self, sk: Skeleton, vx0, vy1, scale, pad) -> list[BoneReport]:
        out: list[BoneReport] = []
        for bone in sk.bones:
            wx, wy = bone.worldX, bone.worldY
            rep = BoneReport(
                index=bone.data.index, name=bone.data.name,
                parent=bone.parent.data.name if bone.parent else None,
                world=(wx, wy), screen=((wx - vx0) * scale + pad, (vy1 - wy) * scale + pad),
                rotation=bone.world_rotation_x,
                scale=(bone.world_scale_x, bone.world_scale_y),
                local={"x": bone.x, "y": bone.y, "rotation": bone.rotation,
                       "scaleX": bone.scaleX, "scaleY": bone.scaleY},
                length=bone.data.length, active=bone.active)
            out.append(rep)
        for rep, bone in zip(out, sk.bones):
            if bone.parent is not None:
                out[bone.parent.data.index].children.append(rep)
        return out

    def _overlay(self, image: Image.Image, sk: Skeleton, reports, bones, to_screen) -> None:
        opts = self.options
        draw = ImageDraw.Draw(image, "RGBA")
        font = _font()
        if opts.bboxes:
            arrays = sk.bone_arrays()
            for slot in sk.drawOrder:
                att = slot.attachment
                if isinstance(att, (BoundingBoxAttachment, ClippingAttachment)) and slot.bone.active:
                    pts = to_screen(world_vertices(att, slot, arrays))
                    color = (90, 230, 120, 230) if isinstance(att, BoundingBoxAttachment) \
                        else (255, 120, 220, 230)
                    if len(pts) >= 2:
                        draw.line([tuple(p) for p in pts] + [tuple(pts[0])], fill=color, width=1)
        if opts.annotate:
            for rep in reports:
                if rep.state != "drawn" and rep.state != "missingRegion":
                    continue
                if rep.rect is None:
                    continue
                x, y, w, h = rep.rect
                color = (255, 210, 60, 210) if rep.state == "drawn" else (255, 80, 80, 230)
                draw.rectangle([x, y, x + w, y + h], outline=color, width=1)
                draw.text((x + 2, y + 1), rep.name, fill=color, font=font)
        if opts.bones:
            for rep in bones:
                bone = sk.bones[rep.index]
                if not bone.active:
                    continue
                sx, sy = rep.screen
                length = bone.data.length
                if length > 0:
                    ex, ey = bone.local_to_world(length, 0)
                    end = to_screen(np.array([[ex, ey]]))[0]
                    draw.line([(sx, sy), (float(end[0]), float(end[1]))],
                              fill=(80, 200, 255, 230), width=2)
                draw.ellipse([sx - 2.5, sy - 2.5, sx + 2.5, sy + 2.5],
                             fill=(255, 255, 255, 230), outline=(30, 120, 200, 255))
            # 世界原點（SkeletonAnimation 節點的位置）
            ox, oy = to_screen(np.array([[0.0, 0.0]]))[0]
            draw.line([(ox - 8, oy), (ox + 8, oy)], fill=(255, 90, 90, 220), width=1)
            draw.line([(ox, oy - 8), (ox, oy + 8)], fill=(255, 90, 90, 220), width=1)


def render_file(path: str, animation: Optional[str] = None, time: float = 0.0,
                skin: Optional[str] = "__default__", **options):
    """函式庫用的捷徑：讀檔並渲染一格。"""
    from .loader import load
    doc = load(path)
    renderer = Renderer(doc, RenderOptions(**options))
    return doc, renderer.render(animation, time, skin)
