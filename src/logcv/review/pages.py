"""Render a viewport of one page at one zoom, cheaply, whatever the page size.

The corpus reaches 5200 x 528110 px (2.75 GP), so the viewer never holds a page.
It composes the visible rectangle from fixed-size tiles at a power-of-two pyramid
level. GDAL performs reduced-resolution reads natively; zoomed-out tiles persist
outside the source folder and full-resolution tiles remain memory-only.

Bilevel TIFFs go through `logcv.io.LogImage` (polarity + dpi already normalised
there). Anything else -- PNG, JPEG, a grayscale scan -- falls back to PIL, so the
window works as a plain image explorer on other folders too.

**Decimation is area-average here, not the MAX pool the detectors use.** Several
pages in this corpus carry large dithered grey regions -- e.g.
`42175010740000_HOU-WL-IMG-1-1304011.TIF`, whose right 40 % is ~39 % ink as
salt-and-pepper speckle. MAX pooling turns any cell containing one ink pixel
black, so at 1:4 that region renders as a solid black slab and the page looks
corrupt. Averaging keeps it the light grey it is at 1:1. The MAX mode is still
available (`mode="max"`) for hunting hairline strokes that averaging fades.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import numpy as np
from PIL import Image

from ..io import LogImage, max_pool
from ..units import Box
from .pyramid import TilePyramid

#: What the folder listing accepts.
IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".gif")
#: Files GDAL/`LogImage` handles as bilevel ink; everything else goes to PIL.
GDAL_EXTS = (".tif", ".tiff")
Image.MAX_IMAGE_PIXELS = None

_API14_RE = re.compile(r"(\d{14})")


def api14_from_name(name: str) -> str:
    """The 14-digit API number a log filename carries, or "" if it carries none."""
    match = _API14_RE.search(os.path.basename(name))
    return match.group(1) if match else ""


def list_images(folder: str) -> list[str]:
    """Every readable image in ``folder``, sorted by name. Skips Thumbs.db."""
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(IMAGE_EXTS):
            continue
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            out.append(path)
    return out


@dataclass(frozen=True)
class PageInfo:
    """What the UI needs to lay out a page before any pixel is read."""

    path: str
    name: str
    width: int
    height: int
    dpi: float
    api14: str | None = None

    @property
    def size_in(self) -> tuple[float, float]:
        return self.width / self.dpi, self.height / self.dpi

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1e6


def probe(path: str) -> PageInfo:
    """Page dimensions and dpi without decoding anything. Milliseconds."""
    if path.lower().endswith(GDAL_EXTS):
        with LogImage(path) as img:
            return PageInfo(path, img.name, img.width, img.height, img.dpi, img.api14)
    with Image.open(path) as im:
        dpi = float((im.info.get("dpi") or (0, 0))[0]) or 96.0
        return PageInfo(path, os.path.basename(path), im.width, im.height, dpi)


def mean_pool(ink: np.ndarray, factor: int) -> np.ndarray:
    """Ink mask -> grayscale, one pixel per ``factor`` x ``factor`` cell.

    The cell's value is its ink *coverage*, so a half-inked cell comes back mid
    grey. This is what makes a dithered region look like the grey it is instead of
    the solid black a MAX pool would produce.
    """
    if factor <= 1:
        return np.where(ink, 0, 255).astype(np.uint8)
    h, w = ink.shape
    hh, ww = h // factor, w // factor
    if hh == 0 or ww == 0:
        return np.where(ink[:1, :1], 0, 255).astype(np.uint8)
    blocks = ink[: hh * factor, : ww * factor].reshape(hh, factor, ww, factor)
    # uint32 sums: factor is at most a few thousand, so factor**2 can overflow 16 bits.
    coverage = blocks.sum(axis=(1, 3), dtype=np.uint32) / float(factor * factor)
    return (255.0 * (1.0 - coverage)).round().astype(np.uint8)


class _BasePage:
    """A page that can hand back a grayscale rectangle at a decimation factor."""

    info: PageInfo

    def read_gray(self, box: Box, factor: int,
                  mode: str = "mean") -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass

    def prefetch(self, box: Box, factor: int, mode: str = "mean",
                 should_stop=None) -> int:
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class GdalPage(_BasePage):
    """Windowed TIFF reader backed by a memory/disk tile pyramid."""

    def __init__(self, path: str, cache_root: str | None = None):
        self._img = LogImage(path)
        self.info = PageInfo(path, self._img.name, self._img.width, self._img.height,
                             self._img.dpi, self._img.api14)
        self._pyramid = TilePyramid(
            path,
            self.info.width,
            self.info.height,
            cache_root,
            self._read_native_tile,
        )

    def _read_native_tile(self, box: Box, factor: int, mode: str) -> np.ndarray:
        out_width = (box.width + factor - 1) // factor
        out_height = (box.height + factor - 1) // factor
        return self._img.read_reduced_gray(
            box,
            out_width,
            out_height,
            "max" if mode == "max" else "average",
        )

    def read_gray(self, box: Box, factor: int, mode: str = "mean") -> np.ndarray:
        return self._pyramid.read(box, factor, mode)

    def prefetch(self, box: Box, factor: int, mode: str = "mean",
                 should_stop=None) -> int:
        return self._pyramid.prefetch_adjacent(box, factor, mode, should_stop)

    def close(self) -> None:
        self._img.close()


class PilPage(_BasePage):
    """Anything PIL opens. Held in memory, so only for ordinary-sized images."""

    def __init__(self, path: str):
        image = Image.open(path)
        image.load()
        self._image = image.convert("L")
        dpi = float((image.info.get("dpi") or (0, 0))[0]) or 96.0
        self.info = PageInfo(path, os.path.basename(path),
                             self._image.width, self._image.height, dpi)

    def read_gray(self, box: Box, factor: int, mode: str = "mean") -> np.ndarray:
        box = box.clip(self.info.width, self.info.height)
        if box.is_empty:
            return np.full((0, 0), 255, dtype=np.uint8)
        crop = self._image.crop(box.as_tuple())
        if factor > 1:
            if mode == "max":
                dark = max_pool(np.asarray(crop) < 128, factor)
                return np.where(dark, 0, 255).astype(np.uint8)
            crop = crop.resize((max(1, crop.width // factor),
                                max(1, crop.height // factor)), Image.BOX)
        return np.asarray(crop, dtype=np.uint8)

    def close(self) -> None:
        self._image.close()


def open_page(path: str, cache_root: str | None = None) -> _BasePage:
    """Open ``path`` with whichever backend suits it, PIL as the fallback."""
    if path.lower().endswith(GDAL_EXTS):
        try:
            return GdalPage(path, cache_root=cache_root)
        except Exception:
            pass
    return PilPage(path)


def factor_for_scale(scale: float) -> int:
    """Power-of-two pyramid factor no coarser than the current display scale."""
    if scale >= 1.0:
        return 1
    desired = max(1, int(1.0 / scale))
    return 1 << (desired.bit_length() - 1)


def viewport_box(page: _BasePage, vx: float, vy: float, scale: float,
                 vw: int, vh: int) -> Box:
    """Native-pixel source rectangle covered by a screen viewport."""
    step = 1.0 / max(scale, 1e-12)
    return Box(
        int(np.floor(vx)),
        int(np.floor(vy)),
        int(np.ceil(vx + max(1, int(vw)) * step)),
        int(np.ceil(vy + max(1, int(vh)) * step)),
    ).clip(page.info.width, page.info.height)


def prefetch_viewport(
    page: _BasePage,
    vx: float,
    vy: float,
    scale: float,
    vw: int,
    vh: int,
    mode: str = "mean",
    should_stop=None,
) -> int:
    """Warm the pyramid tile rows immediately above and below a viewport."""
    factor = factor_for_scale(scale)
    return page.prefetch(
        viewport_box(page, vx, vy, scale, vw, vh),
        factor,
        mode,
        should_stop,
    )


def render_viewport(
    page: _BasePage,
    vx: float,
    vy: float,
    scale: float,
    vw: int,
    vh: int,
    mode: str = "mean",
) -> Image.Image:
    """The ``vw`` x ``vh`` screen rectangle whose top-left page pixel is (vx, vy).

    ``scale`` is screen pixels per page pixel: 1.0 is 1:1, 0.25 is zoomed out
    four times, 2.0 is magnified twice. Page area outside the sheet comes back
    white, so the caller can pan past the edges without special cases.

    ``mode`` picks the decimation: ``"mean"`` (true tone, the default) or
    ``"max"`` (any ink in a cell goes black -- bolder hairlines, black slabs
    wherever the scan is dithered).
    """
    vw, vh = max(1, int(vw)), max(1, int(vh))
    out = Image.new("L", (vw, vh), 255)

    factor = factor_for_scale(scale)
    box = viewport_box(page, vx, vy, scale, vw, vh)
    if box.is_empty:
        return out

    tile = page.read_gray(box, factor, mode)
    if tile.size == 0:
        return out
    image = Image.fromarray(tile, mode="L")

    # Decimation lands at 1/factor; anything else is a resize of that.
    target_w = max(1, int(round(box.width * scale)))
    target_h = max(1, int(round(box.height * scale)))
    if (image.width, image.height) != (target_w, target_h):
        resample = Image.NEAREST if scale > 1.0 else Image.LANCZOS
        image = image.resize((target_w, target_h), resample)

    out.paste(image, (int(round((box.x0 - vx) * scale)),
                      int(round((box.y0 - vy) * scale))))
    return out
