"""讀 libGDX／Spine 的 .atlas 文字檔。

3.8 的格式（每頁一個貼圖，空行分隔頁面）：

    WealthGod.png
    size: 491,491
    format: RGBA8888
    filter: Linear,Linear
    repeat: none
    FX/Coin_01
      rotate: true
      xy: 90, 5
      size: 5, 28
      orig: 5, 28
      offset: 0, 0
      index: -1

也順便認得 4.x 的寫法（bounds／offsets 合併欄位、頁首沒有空行），
讓 doctor 能說清楚「這是 4.x 的圖集」而不是直接讀壞。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .cache import memoize

_PAGE_KEYS = {"size", "format", "filter", "repeat", "pma", "scale"}


@dataclass(eq=False)
class AtlasPage:
    name: str
    path: str
    width: int = 0
    height: int = 0
    format: str = ""
    filter: str = ""
    repeat: str = ""
    pma: bool = False


@dataclass(eq=False)
class AtlasRegion:
    name: str
    page: AtlasPage
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    originalWidth: int = 0
    originalHeight: int = 0
    offsetX: float = 0.0
    offsetY: float = 0.0
    degrees: int = 0
    index: int = -1
    u: float = 0.0
    v: float = 0.0
    u2: float = 0.0
    v2: float = 0.0
    splits: Optional[list] = None
    pads: Optional[list] = None

    @property
    def rotate(self) -> bool:
        return self.degrees == 90

    def packed_size(self) -> tuple[int, int]:
        """在貼圖上實際佔的寬高（旋轉 90 度時寬高互換）。"""
        if self.degrees in (90, 270):
            return self.height, self.width
        return self.width, self.height


@dataclass
class Atlas:
    path: str
    pages: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    format_hint: str = "3.x"

    def __post_init__(self):
        self._by_name: dict[str, AtlasRegion] = {}

    def index(self) -> None:
        self._by_name = {}
        for region in self.regions:
            # AtlasAttachmentLoader::findRegion 回傳第一個同名的
            self._by_name.setdefault(region.name, region)

    def find_region(self, name: str) -> Optional[AtlasRegion]:
        return self._by_name.get(name)


def _ints(text: str) -> list[int]:
    out = []
    for piece in text.split(","):
        piece = piece.strip()
        if piece:
            try:
                out.append(int(float(piece)))
            except ValueError:
                out.append(0)
    return out


def parse_atlas(path: str) -> Atlas:
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        text = fh.read()
    # 照 spine-cpp：只認 \n 換行，行尾空白（含 \r）去掉。不能用 splitlines()——
    # 有些圖集被 git 轉換過兩次，行尾是 \r\r\n，splitlines 會把每個 \r 都當成一個空行，
    # 而空行在圖集裡代表「換下一頁」，整份就讀爛了；引擎則完全正常。
    lines = [line.rstrip("\r") for line in text.split("\n")]
    atlas = Atlas(path=path)
    folder = os.path.dirname(os.path.abspath(path))
    page: Optional[AtlasPage] = None
    i = 0
    n = len(lines)

    def props(start: int) -> tuple[dict, int]:
        found: dict[str, str] = {}
        j = start
        while j < n:
            raw = lines[j]
            stripped = raw.strip()
            if not stripped or ":" not in stripped:
                break
            key, _, value = stripped.partition(":")
            found[key.strip()] = value.strip()
            j += 1
        return found, j

    while i < n:
        line = lines[i].strip()
        if not line:
            page = None
            i += 1
            continue
        if page is None:
            page = AtlasPage(name=line, path=os.path.join(folder, line))
            fields, i = props(i + 1)
            if "size" in fields:
                size = _ints(fields["size"])
                if len(size) >= 2:
                    page.width, page.height = size[0], size[1]
            page.format = fields.get("format", "")
            page.filter = fields.get("filter", "")
            page.repeat = fields.get("repeat", "")
            page.pma = fields.get("pma", "").lower() == "true"
            if "pma" in fields or "scale" in fields:
                atlas.format_hint = "4.x"
            atlas.pages.append(page)
            continue

        # 4.x 沒有空行就直接接下一頁：這一行是圖檔名、下一行是 size 之類的頁首欄位
        if line.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) and i + 1 < n:
            nxt = lines[i + 1].strip()
            if ":" in nxt and nxt.split(":")[0].strip() in _PAGE_KEYS:
                page = None
                atlas.format_hint = "4.x"
                continue

        region = AtlasRegion(name=line, page=page)
        fields, i = props(i + 1)
        rotate = fields.get("rotate", "false").lower()
        if rotate == "true":
            region.degrees = 90
        elif rotate == "false":
            region.degrees = 0
        else:
            try:
                region.degrees = int(float(rotate))
            except ValueError:
                region.degrees = 0
        if "bounds" in fields:
            b = _ints(fields["bounds"])
            if len(b) >= 4:
                region.x, region.y, region.width, region.height = b[:4]
            atlas.format_hint = "4.x"
        if "xy" in fields:
            xy = _ints(fields["xy"])
            if len(xy) >= 2:
                region.x, region.y = xy[0], xy[1]
        if "size" in fields:
            size = _ints(fields["size"])
            if len(size) >= 2:
                region.width, region.height = size[0], size[1]
        if "offsets" in fields:
            o = _ints(fields["offsets"])
            if len(o) >= 4:
                region.offsetX, region.offsetY = o[0], o[1]
                region.originalWidth, region.originalHeight = o[2], o[3]
        if "orig" in fields:
            orig = _ints(fields["orig"])
            if len(orig) >= 2:
                region.originalWidth, region.originalHeight = orig[0], orig[1]
        if "offset" in fields:
            off = _ints(fields["offset"])
            if len(off) >= 2:
                region.offsetX, region.offsetY = off[0], off[1]
        if not region.originalWidth:
            region.originalWidth = region.width
        if not region.originalHeight:
            region.originalHeight = region.height
        if "index" in fields:
            try:
                region.index = int(fields["index"])
            except ValueError:
                pass
        if "split" in fields:
            region.splits = _ints(fields["split"])
        if "pad" in fields:
            region.pads = _ints(fields["pad"])
        atlas.regions.append(region)

    for region in atlas.regions:
        _compute_uvs(region)
    atlas.index()
    return atlas


def _compute_uvs(region: AtlasRegion) -> None:
    page = region.page
    w = page.width or 1
    h = page.height or 1
    region.u = region.x / w
    region.v = region.y / h
    if region.degrees == 90:
        region.u2 = (region.x + region.height) / w
        region.v2 = (region.y + region.width) / h
    else:
        region.u2 = (region.x + region.width) / w
        region.v2 = (region.y + region.height) / h


def fill_page_sizes(atlas: Atlas, sizes: dict) -> None:
    """頁首沒寫 size 的舊圖集（老版 TexturePacker）用實際貼圖尺寸補上再算 UV。"""
    changed = False
    for page in atlas.pages:
        if (not page.width or not page.height) and page.path in sizes:
            page.width, page.height = sizes[page.path]
            changed = True
    if changed:
        for region in atlas.regions:
            _compute_uvs(region)


# ---------------------------------------------------------------- 貼圖

_TEX_CACHE: dict = {}
_TEX_LOCK = threading.Lock()


def _stamp(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_texture_pair(path: str, pma: bool = False):
    """回傳 (預乘 alpha 的 float32 陣列 (H, W, 4), 每個貼圖像素是否非零的 bool 陣列 (H*W,))；
    檔案不存在回傳 None。

    快取鍵含 mtime 與大小：美術重新匯出貼圖後不必重開伺服器就會讀到新圖。
    遊戲端（Cocos2d-x）載入 PNG 時預設會預乘 alpha，這裡同樣在載入時預乘；
    圖集若標了 pma（貼圖本身已預乘）就不再乘一次。

    非零表和貼圖一起建、存在同一筆快取裡——分開快取的話，檔案剛好在兩次讀取之間被換掉，
    會拿到不同版本的貼圖和非零表，光柵化就會跳過不該跳過的像素。
    """
    stamp = _stamp(path)
    if stamp is None:
        return None
    key = (os.path.normcase(os.path.abspath(path)), stamp, pma)

    def build():
        from PIL import Image
        with Image.open(path) as img:
            arr = np.asarray(img.convert("RGBA"), dtype=np.float32) / 255.0
        if not pma:
            arr[..., :3] *= arr[..., 3:4]
        arr.setflags(write=False)
        nonzero = arr.reshape(-1, 4).max(axis=1) > 0
        nonzero.setflags(write=False)
        return arr, nonzero

    return memoize(_TEX_CACHE, key, _TEX_LOCK, build, limit=48)


def load_texture(path: str, pma: bool = False):
    """只要預乘 alpha 的貼圖陣列；檔案不存在回傳 None。"""
    pair = load_texture_pair(path, pma)
    return None if pair is None else pair[0]


def texture_size(path: str) -> Optional[tuple[int, int]]:
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None
