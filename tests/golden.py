"""效能優化的正確性護欄：改動前存一份「黃金基準」，改動後逐項比對。

    python tests/golden.py capture <基準.pkl> <骨架檔...>
    python tests/golden.py compare <基準.pkl> <骨架檔...>
    python tests/golden.py bench   <骨架檔...>
    python tests/golden.py shortcut <骨架檔...>   # 渲染捷徑開／關逐像素比對

基準內容（每個檔案、每段動畫取頭／1/3／2/3／尾四個時間點）：
- 每根骨骼的世界矩陣 a b c d worldX worldY
- 每個要畫的附件：插槽、世界座標頂點、顏色、混色、clipping 多邊形、繪製順序
- 每個插槽的狀態報告
- 整段動畫的取景外框
- 一張 0.5 倍的 numpy 渲染圖（逐像素）

compare 的標準：幾何與顏色差 < 1e-9（浮點運算順序改變時容許的誤差），渲染圖逐像素完全相同。
"""
from __future__ import annotations

import os
import pickle
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from spineview.loader import load                          # noqa: E402
from spineview.render import Renderer, RenderOptions, _collect  # noqa: E402


def snapshot(path: str) -> dict:
    doc = load(path)
    renderer = Renderer(doc, RenderOptions(scale=0.5, padding=4, max_pixels=1_500_000))
    skin_name = renderer.default_skin_name()
    skin = renderer.skin(skin_name)
    out = {"frames": [], "bounds": {}, "images": {}}
    anims = doc.data.animations[:3] or [None]
    for anim in anims:
        name = anim.name if anim is not None else None
        out["bounds"][name] = renderer.content_bounds(anim, skin_name, set(), None)
        times = [0.0] if anim is None else [anim.duration * k / 3 for k in range(4)]
        for t in times:
            sk = renderer.pose(anim, t, skin)
            reports: list = []
            items = _collect(sk, set(), None, reports)
            out["frames"].append({
                "anim": name, "time": t,
                "bones": np.array([(b.a, b.b, b.c, b.d, b.worldX, b.worldY) for b in sk.bones]),
                "order": [s.data.index for s in sk.drawOrder],
                "items": [(it.slot.data.index, np.array(it.xy), tuple(it.color), it.blend,
                           None if it.clip is None else np.array(it.clip[1]),
                           None if it.dark is None else tuple(it.dark)) for it in items],
                "reports": [(r.name, r.attachment, r.state, r.clippedBy,
                             None if r.world is None else tuple(r.world)) for r in reports],
            })
        mid = 0.0 if anim is None else anim.duration / 2
        out["images"][name] = np.asarray(renderer.render(name, mid, skin_name).image)
    return out


def _close(a, b, label, errors):
    if a is None or b is None:
        if a is not b:
            errors.append(f"{label}: 一邊是 None")
        return
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.shape != b.shape:
        errors.append(f"{label}: 形狀不同 {a.shape} vs {b.shape}")
        return
    both_nan = np.isnan(a) & np.isnan(b)
    diff = np.where(both_nan, 0.0, np.abs(a - b))
    if np.isnan(diff).any() or diff.max(initial=0.0) > 1e-9:
        errors.append(f"{label}: 最大差 {np.nanmax(diff)}")


def compare(old: dict, new: dict, name: str) -> list[str]:
    errors: list[str] = []
    if len(old["frames"]) != len(new["frames"]):
        return [f"{name}: 影格數不同"]
    for i, (fa, fb) in enumerate(zip(old["frames"], new["frames"])):
        tag = f"{name} {fa['anim']}@{fa['time']:.3f}"
        _close(fa["bones"], fb["bones"], tag + " 骨骼", errors)
        if fa["order"] != fb["order"]:
            errors.append(tag + " 繪製順序不同")
        if fa["reports"] != fb["reports"]:
            for ra, rb in zip(fa["reports"], fb["reports"]):
                if ra[:4] != rb[:4]:
                    errors.append(f"{tag} 插槽報告不同 {ra[:4]} vs {rb[:4]}")
                    break
                _close(ra[4], rb[4], f"{tag} {ra[0]} 外框", errors)
        if len(fa["items"]) != len(fb["items"]):
            errors.append(f"{tag} 要畫的附件數不同 {len(fa['items'])} vs {len(fb['items'])}")
            continue
        for ia, ib in zip(fa["items"], fb["items"]):
            if ia[0] != ib[0] or ia[3] != ib[3]:
                errors.append(f"{tag} 附件順序或混色不同")
                break
            _close(ia[1], ib[1], f"{tag} slot{ia[0]} 頂點", errors)
            _close(ia[2], ib[2], f"{tag} slot{ia[0]} 顏色", errors)
            _close(ia[4], ib[4], f"{tag} slot{ia[0]} clip", errors)
            _close(ia[5], ib[5], f"{tag} slot{ia[0]} dark", errors)
    for key, box in old["bounds"].items():
        _close(box, new["bounds"].get(key), f"{name} {key} 取景", errors)
    for key, img in old["images"].items():
        other = new["images"].get(key)
        if other is None or img.shape != other.shape:
            errors.append(f"{name} {key} 渲染圖尺寸不同")
        elif not np.array_equal(img, other):
            n = int((img != other).any(axis=2).sum())
            m = int(np.abs(img.astype(int) - other.astype(int)).max())
            errors.append(f"{name} {key} 渲染圖有 {n} 個像素不同（最大差 {m}）")
    return errors


def bench(paths: list[str]) -> None:
    from spineview.animation import apply_animation
    for path in paths:
        t0 = time.perf_counter()
        doc = load(path)
        t_load = time.perf_counter() - t0
        renderer = Renderer(doc, RenderOptions(scale=0.5, max_pixels=4_000_000))
        anim = doc.data.animations[0] if doc.data.animations else None
        skin_name = renderer.default_skin_name()
        skin = renderer.skin(skin_name)
        times = renderer.frame_times(anim)[:60]
        t0 = time.perf_counter()
        for t in times:
            renderer.pose(anim, t, skin)
        t_pose = (time.perf_counter() - t0) / len(times)
        t0 = time.perf_counter()
        for t in times:
            _collect(renderer.pose(anim, t, skin), set(), None, [])
        t_collect = (time.perf_counter() - t0) / len(times) - t_pose
        t0 = time.perf_counter()
        renderer.content_bounds(anim, skin_name, set(), None)
        t_bounds = time.perf_counter() - t0
        t0 = time.perf_counter()
        renderer.render(anim.name if anim else None, (anim.duration / 2) if anim else 0, skin_name)
        t_render = time.perf_counter() - t0
        print(f"{os.path.basename(path):<28} 讀檔 {t_load * 1000:7.1f}ms  擺姿勢 {t_pose * 1000:6.2f}ms/格  "
              f"收集頂點 {t_collect * 1000:6.2f}ms/格  取景 {t_bounds * 1000:7.1f}ms  "
              f"渲染(0.5x) {t_render * 1000:7.1f}ms")


def shortcut(paths: list[str]) -> int:
    """渲染的捷徑與平行化不能改變結果：

    參考版本＝單執行緒、不套用「遮罩外的像素不算」；拿它和下列每一種逐像素比對：
    捷徑開（單執行緒）、捷徑開＋3 條（列數除不盡，條的邊界不整齊）、捷徑開＋8 條。
    """
    import spineview.render as R
    bad = 0
    variants = [("捷徑", True, 1), ("捷徑+3條", True, 3), ("捷徑+8條", True, 8)]
    for path in paths:
        doc = load(path)
        base = dict(scale=0.5, padding=4, max_pixels=1_500_000)
        renderers = {n: Renderer(doc, RenderOptions(threads=n, **base)) for n in (1, 3, 8)}
        skin = renderers[1].default_skin_name()
        for anim in doc.data.animations[:3] or [None]:
            name = anim.name if anim is not None else None
            for k in range(4):
                t = 0.0 if anim is None else anim.duration * k / 3
                R.CLIP_SHORTCUT = False
                ref = np.asarray(renderers[1].render(name, t, skin).image)
                R.CLIP_SHORTCUT = True
                for label, _on, n in variants:
                    got = np.asarray(renderers[n].render(name, t, skin).image)
                    if not np.array_equal(ref, got):
                        bad += 1
                        print(f"DIFF  {os.path.basename(path)} {name}@{t:.3f} [{label}]: "
                              f"{int((ref != got).any(axis=2).sum())} 個像素不同")
    print("捷徑與平行化的結果和參考版本逐像素相同" if not bad else f"{bad} 個比對不同")
    return 1 if bad else 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    mode = sys.argv[1]
    if mode == "bench":
        bench(sys.argv[2:])
        return 0
    if mode == "shortcut":
        return shortcut(sys.argv[2:])
    store, paths = sys.argv[2], sys.argv[3:]
    if mode == "capture":
        data = {p: snapshot(p) for p in paths}
        with open(store, "wb") as fh:
            pickle.dump(data, fh)
        print(f"已存 {len(data)} 個檔案的基準 → {store}")
        return 0
    with open(store, "rb") as fh:
        data = pickle.load(fh)
    bad = 0
    for p in paths:
        errors = compare(data[p], snapshot(p), os.path.basename(p))
        print(("OK    " if not errors else "DIFF  ") + os.path.basename(p))
        for e in errors[:8]:
            print("      " + e)
        bad += bool(errors)
    print("全部一致" if not bad else f"{bad} 個檔案不一致")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
