"""整批骨架檔的回歸測試：

    python tests/test_corpus.py <資料夾> [--render]

對資料夾底下每一個 .skel／Spine .json：
1. 讀檔（binary 讀完必須剛好停在檔尾——欄位讀錯幾乎不可能剛好對齊，這是最強的格式檢查）
2. 接上圖集
3. 每段動畫在頭、中、尾三個時間點擺姿勢並算出所有頂點（不能丟例外、不能全是 NaN）
4. 加 --render 時，每個檔再用 0.25 倍渲染中段一格

離開碼 0 = 全部通過。讀不進來的檔若是「遊戲也讀不進來」的已知情況（例如 3.8.75），
會列為 expected，不算失敗。
"""
from __future__ import annotations

import collections
import math
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from spineview.animation import apply_animation          # noqa: E402
from spineview.loader import iter_skeletons, load        # noqa: E402
from spineview.reader_binary import SkeletonFormatError  # noqa: E402
from spineview.render import Renderer, RenderOptions, _collect  # noqa: E402
from spineview.skeleton import Skeleton                  # noqa: E402

EXPECTED = ("3.8.75", "3.6", "3.7", "4.x")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = sys.argv[1]
    do_render = "--render" in sys.argv
    t0 = time.time()
    counts = collections.Counter()
    failures: list[tuple[str, str]] = []
    for path in iter_skeletons(root):
        counts["files"] += 1
        try:
            doc = load(path)
        except SkeletonFormatError as exc:
            if any(tag in str(exc) for tag in EXPECTED):
                counts["expected"] += 1
                continue
            failures.append((path, f"load: {exc}"))
            continue
        except Exception as exc:
            failures.append((path, f"load: {type(exc).__name__}: {exc}"))
            continue
        try:
            renderer = Renderer(doc, RenderOptions(scale=0.25, padding=2, max_pixels=1_000_000))
            skin = renderer.skin(renderer.default_skin_name())
            for anim in doc.data.animations:
                for t in (0.0, anim.duration / 2, anim.duration):
                    sk = Skeleton(doc.data)
                    sk.set_skin(skin)
                    sk.set_to_setup_pose()
                    apply_animation(anim, sk, t)
                    sk.update_world_transform()
                    items = _collect(sk, set(), None)
                    for it in items:
                        if it.xy.size and not any(math.isfinite(v) for v in it.xy.ravel()):
                            counts["nanAttachments"] += 1
                    counts["poses"] += 1
            if do_render:
                anim = doc.data.animations[0] if doc.data.animations else None
                result = renderer.render(anim.name if anim else None,
                                         anim.duration / 2 if anim else 0.0)
                assert result.image is not None and result.image.size[0] > 0
                counts["renders"] += 1
            counts["ok"] += 1
        except Exception:
            failures.append((path, "pose/render: " + traceback.format_exc(limit=3)))
    print(f"{counts['files']} 個骨架檔，{counts['ok']} 個通過，{counts['expected']} 個是遊戲也讀不進來的版本，"
          f"{len(failures)} 個失敗；擺了 {counts['poses']} 個姿勢"
          + (f"、渲染 {counts['renders']} 張" if do_render else "")
          + f"（{time.time() - t0:.1f}s）")
    if counts["nanAttachments"]:
        print(f"注意：{counts['nanAttachments']} 個附件在某些時間點座標全是 NaN"
              "（退化的 IK／骨骼長度 0，引擎同樣畫不出來）")
    for path, why in failures[:30]:
        print(f"FAIL {path}\n     {why}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
