"""畫面比對：把 spineview 的渲染結果跟另一張圖（例如 Cocos Creator 或遊戲的截圖）對照。

沿用 csdview 的比對方法：
1. 相位相關估計整張圖的位移量——移植時最常見的錯誤是整體差一個偏移（節點錨點、位置）。
2. 逐插槽算出各自區域的差異，直接指出「哪一個插槽畫錯了」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw

from .render import RenderResult, _parse_color


def _to_array(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGBA"), dtype=np.float32) / 255.0


def _flatten(arr: np.ndarray, background) -> np.ndarray:
    alpha = arr[..., 3:4]
    bg = np.array(background, dtype=np.float32).reshape(1, 1, 3)
    return arr[..., :3] * alpha + bg * (1.0 - alpha)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _phase_correlate(a: np.ndarray, b: np.ndarray):
    height, width = a.shape
    window = np.outer(np.hanning(height), np.hanning(width)).astype(np.float32)
    fa = np.fft.rfft2((a - a.mean()) * window)
    fb = np.fft.rfft2((b - b.mean()) * window)
    cross = fa * np.conj(fb)
    magnitude = np.abs(cross)
    cross /= np.where(magnitude > 1e-9, magnitude, 1.0)
    surface = np.fft.irfft2(cross, s=a.shape)
    peak = int(np.argmax(surface))
    dy, dx = divmod(peak, width)
    if dy > height // 2:
        dy -= height
    if dx > width // 2:
        dx -= width
    confidence = float(surface.max() / (surface.std() + 1e-9))
    return int(dx), int(dy), confidence


def _box_downsample(arr: np.ndarray, step: int) -> np.ndarray:
    height, width = arr.shape
    h2, w2 = height // step, width // step
    return arr[:h2 * step, :w2 * step].reshape(h2, step, w2, step).mean(axis=(1, 3))


def _refine_shift(a, b, dx, dy, radius, window=1200):
    height, width = a.shape
    ch, cw = min(height, window), min(width, window)
    y0, x0 = (height - ch) // 2, (width - cw) // 2
    patch = a[y0:y0 + ch, x0:x0 + cw]
    if float(patch.std()) < 1e-4:
        return dx, dy
    best_score, best = None, (dx, dy)
    for ody in range(dy - radius, dy + radius + 1):
        for odx in range(dx - radius, dx + radius + 1):
            sy, sx = y0 - ody, x0 - odx
            if sy < 0 or sx < 0 or sy + ch > height or sx + cw > width:
                continue
            score = float(np.abs(patch - b[sy:sy + ch, sx:sx + cw]).mean())
            if best_score is None or score < best_score:
                best_score, best = score, (odx, ody)
    return best


def estimate_shift(a: np.ndarray, b: np.ndarray, max_pixels: int = 4_000_000):
    height, width = a.shape
    if height < 8 or width < 8:
        return 0, 0, 0.0
    step = 1
    while step < 4 and (height // (step + 1)) * (width // (step + 1)) >= max_pixels:
        step += 1
    if step == 1:
        return _phase_correlate(a, b)
    dx, dy, confidence = _phase_correlate(_box_downsample(a, step), _box_downsample(b, step))
    dx, dy = _refine_shift(a, b, dx * step, dy * step, step)
    return dx, dy, confidence


@dataclass
class RegionDiff:
    name: str
    attachment: str
    blend: str
    rect: tuple
    mae: float
    max_diff: float
    coverage: float
    kind: str = "content"       # content / additive / clipped

    def to_dict(self) -> dict:
        return {"slot": self.name, "attachment": self.attachment, "blend": self.blend,
                "kind": self.kind, "screenRect": [round(v, 1) for v in self.rect],
                "meanDiff": round(self.mae, 4), "maxDiff": round(self.max_diff, 4),
                "diffPixelRatio": round(self.coverage, 4)}


@dataclass
class CompareResult:
    size: tuple
    reference_size: tuple
    resized: bool
    mae: float
    rmse: float
    psnr: float
    diff_ratio: float
    alpha_iou: float
    shift: tuple
    shift_confidence: float
    aspect_delta: float = 0.0
    regions: list = field(default_factory=list)
    aligned_mae: float = 0.0
    aligned_diff_ratio: float = 0.0
    aligned_regions: list = field(default_factory=list)
    hints: list = field(default_factory=list)
    image: Image.Image | None = None

    @property
    def shifted(self) -> bool:
        return self.shift != (0, 0) and self.shift_confidence > 20

    @property
    def verdict(self) -> str:
        if self.diff_ratio < 0.01 and self.mae < 0.01:
            return "match"
        if self.shifted and self.aligned_diff_ratio < 0.02:
            return "shifted"
        if self.diff_ratio < 0.08:
            return "close"
        return "different"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "renderSize": list(self.size), "referenceSize": list(self.reference_size),
            "referenceResized": self.resized,
            "referenceAspectDelta": round(self.aspect_delta, 4),
            "meanDiff": round(self.mae, 5), "rmse": round(self.rmse, 5),
            "psnr": round(self.psnr, 2), "diffPixelRatio": round(self.diff_ratio, 5),
            "alphaIoU": round(self.alpha_iou, 4),
            "shiftToAlignReference": list(self.shift),
            "shiftConfidence": round(self.shift_confidence, 2),
            "afterShiftCompensation": {
                "meanDiff": round(self.aligned_mae, 5),
                "diffPixelRatio": round(self.aligned_diff_ratio, 5),
                "worstRegions": [r.to_dict() for r in self.aligned_regions],
            },
            "worstRegions": [r.to_dict() for r in self.regions],
            "hints": self.hints,
        }


def _hints(outcome: CompareResult, missing: list) -> list[str]:
    """依實際數字產生判讀建議；前提失效就停，不依據不可信的數字往下推。"""
    out: list[str] = []
    if outcome.aspect_delta > 0.02:
        out.append(f"兩張圖長寬比差 {outcome.aspect_delta * 100:.0f}%，取景範圍不一致。"
                   "縮放對齊會扭曲畫面，其餘差異數字都不可信。請用相同取景重截再比。")
        return out
    if missing:
        out.append(f"圖集缺少 {len(missing)} 個 region，基準圖本身就不完整（遊戲端整份骨架會載入失敗）。"
                   "先跑 doctor 補齊再比對。")
        return out
    if outcome.verdict == "match":
        out.append("畫面一致，這一格通過。")
        return out
    if outcome.alpha_iou >= 0.99 and outcome.diff_ratio >= 0.10:
        out.append(f"形狀與位置都吻合（alphaIoU {outcome.alpha_iou:.2f}），但 {outcome.diff_ratio * 100:.0f}% "
                   "的像素顏色不同——幾乎都是兩張圖底色不同。拿 spineview 的 render 當基準時請加 "
                   "--bg none，或把 --diff-bg 設成與截圖相同的底色。")
        return out
    if outcome.shifted:
        dx, dy = outcome.shift
        out.append(f"整張圖差一個位移 ({dx}, {dy})，扣掉後差異從 {outcome.diff_ratio * 100:.1f}% 降到 "
                   f"{outcome.aligned_diff_ratio * 100:.1f}%。這多半是節點位置或取景原點不同，改一處即可。")
        if outcome.aligned_diff_ratio < 0.02:
            out.append("扣掉位移之後沒有其他明顯差異。")
            return out
    regions = [r for r in (outcome.aligned_regions if outcome.shifted else outcome.regions)
               if r.coverage >= 0.05]
    real = [r for r in regions if r.kind == "content"]
    glow = [r for r in regions if r.kind == "additive"]
    if real:
        names = "、".join(r.name for r in real[:3])
        out.append(f"需要追查的是這 {len(real)} 個插槽，差異最大的是：{names}。"
                   "用 spec --slot <名稱> 取它們的 screenRect 與附件，對照另一邊的實際畫面。")
    if glow:
        out.append(f"有 {len(glow)} 個差異在 additive／screen 混色的插槽（多半是光暈特效）。"
                   "這類插槽的顏色取決於底下畫了什麼，底色不同時本來就會不同，先確認背景一致。")
    if not regions:
        out.append("差異沒有集中在任何單一插槽，比較像整體性的問題（背景色、縮放倍率或取景）。")
    return out


def compare(result: RenderResult, reference: Image.Image, threshold: float = 0.05,
            background: str = "#000000", min_area: int = 64, top: int = 15,
            make_image: bool = True) -> CompareResult:
    render_image = result.image
    ref_size = reference.size
    resized = False
    aspect_delta = 0.0
    if reference.size != render_image.size:
        ra = render_image.size[0] / max(1, render_image.size[1])
        rb = ref_size[0] / max(1, ref_size[1])
        aspect_delta = abs(ra - rb) / max(ra, rb)
        reference = reference.resize(render_image.size, Image.LANCZOS)
        resized = True
    r, g, b, _ = _parse_color(background)
    bg = (r / 255, g / 255, b / 255)
    a = _flatten(_to_array(render_image), bg)
    bb = _flatten(_to_array(reference), bg)
    delta = np.abs(a - bb)
    per_pixel = delta.max(axis=2)
    mae = float(delta.mean())
    rmse = float(np.sqrt((delta ** 2).mean()))
    psnr = float(20 * np.log10(1.0 / rmse)) if rmse > 1e-9 else 99.0
    diff_ratio = float((per_pixel > threshold).mean())
    alpha_a = np.asarray(render_image.convert("RGBA"))[..., 3] > 8
    alpha_b = np.asarray(reference.convert("RGBA"))[..., 3] > 8
    union = float((alpha_a | alpha_b).sum())
    alpha_iou = float((alpha_a & alpha_b).sum() / union) if union > 0 else 1.0
    dx, dy, confidence = estimate_shift(_luma(a), _luma(bb))
    regions = _region_diffs(result.slots, per_pixel, threshold, min_area, top)
    aligned_mae, aligned_ratio, aligned_regions = mae, diff_ratio, regions
    if (dx, dy) != (0, 0) and confidence > 20 and abs(dx) < a.shape[1] // 4 \
            and abs(dy) < a.shape[0] // 4:
        shifted = np.roll(np.roll(bb, dy, axis=0), dx, axis=1)
        valid = np.ones(a.shape[:2], dtype=bool)
        if dy > 0:
            valid[:dy, :] = False
        elif dy < 0:
            valid[dy:, :] = False
        if dx > 0:
            valid[:, :dx] = False
        elif dx < 0:
            valid[:, dx:] = False
        aligned_delta = np.abs(a - shifted).max(axis=2)
        aligned_mae = float(np.abs(a - shifted).mean(axis=2)[valid].mean())
        aligned_ratio = float((aligned_delta[valid] > threshold).mean())
        aligned_regions = _region_diffs(result.slots, np.where(valid, aligned_delta, 0.0),
                                        threshold, min_area, top)
    image = _diff_image(render_image, reference, per_pixel, threshold) if make_image else None
    outcome = CompareResult(
        size=render_image.size, reference_size=ref_size, resized=resized,
        aspect_delta=aspect_delta, mae=mae, rmse=rmse, psnr=psnr, diff_ratio=diff_ratio,
        alpha_iou=alpha_iou, shift=(dx, dy), shift_confidence=confidence, regions=regions,
        aligned_mae=aligned_mae, aligned_diff_ratio=aligned_ratio,
        aligned_regions=aligned_regions, image=image)
    outcome.hints = _hints(outcome, result.missing)
    return outcome


def _region_diffs(slots, per_pixel, threshold, min_area, top) -> list[RegionDiff]:
    height, width = per_pixel.shape
    out: list[RegionDiff] = []
    for rep in slots:
        if rep.state != "drawn" or rep.rect is None:
            continue
        x, y, w, h = rep.rect
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(width, int(x + w) + 1), min(height, int(y + h) + 1)
        if x1 - x0 < 1 or y1 - y0 < 1 or (x1 - x0) * (y1 - y0) < min_area:
            continue
        patch = per_pixel[y0:y1, x0:x1]
        kind = "additive" if rep.blend in ("additive", "screen") else (
            "clipped" if rep.clippedBy else "content")
        out.append(RegionDiff(name=rep.name, attachment=rep.attachment or "", blend=rep.blend,
                              rect=(x, y, w, h), mae=float(patch.mean()),
                              max_diff=float(patch.max()),
                              coverage=float((patch > threshold).mean()), kind=kind))
    out.sort(key=lambda r: (r.coverage, r.mae), reverse=True)
    return out[:top]


def _diff_image(render, reference, per_pixel, threshold) -> Image.Image:
    width, height = render.size
    heat = np.zeros((height, width, 4), dtype=np.uint8)
    normalized = np.clip(per_pixel / max(threshold * 4, 1e-6), 0, 1)
    heat[..., 0] = (normalized * 255).astype(np.uint8)
    heat[..., 1] = ((1 - normalized) * 90).astype(np.uint8)
    heat[..., 3] = np.where(per_pixel > threshold, 220, 0).astype(np.uint8)
    grey = render.convert("RGBA").convert("L").convert("RGBA")
    grey.alpha_composite(Image.fromarray(heat, "RGBA"))
    gap, label = 8, 20
    sheet = Image.new("RGBA", (width * 3 + gap * 2, height + label), (22, 22, 26, 255))
    for index, panel in enumerate([render, reference, grey]):
        sheet.alpha_composite(panel.convert("RGBA"), (index * (width + gap), label))
    draw = ImageDraw.Draw(sheet)
    for index, title in enumerate(["spineview render", "reference", "diff"]):
        draw.text((index * (width + gap) + 4, 4), title, fill=(150, 200, 255, 255))
    return sheet
