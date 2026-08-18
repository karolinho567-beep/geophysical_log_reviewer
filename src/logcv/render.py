"""Turn detections into pictures a person can check.

Every automated decision in this package is meant to be verifiable by eye, so the
renderers here are part of the deliverable rather than debug scaffolding: the review
workbook is only trustworthy because each row carries the crop it was judged from.

PIL only, deliberately -- matplotlib's conda-forge build fights GDAL over freetype
DLLs on this workstation, and a QC image is not worth that risk.
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw

from .detection import Detection
from .io import LogImage
from .units import Box

MARK_RGB = (220, 30, 30)


def ink_to_pil(mask: np.ndarray) -> Image.Image:
    """Boolean ink mask -> greyscale image, ink black on white."""
    if mask.size == 0:
        return Image.new("L", (1, 1), 255)
    return Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), mode="L")


def _fit(image: Image.Image, max_px: int) -> Image.Image:
    scale = min(1.0, max_px / max(image.size))
    if scale >= 1.0:
        return image
    return image.resize((max(1, int(image.width * scale)),
                         max(1, int(image.height * scale))), Image.LANCZOS)


def crop_image(
    img: LogImage,
    det: Detection,
    pad_frac: float = 0.35,
    max_px: int = 460,
    rotate_ccw_deg: float | None = None,
) -> Image.Image:
    """Tight crop of one detected element, for reading in a spreadsheet cell.

    ``rotate_ccw_deg`` turns the crop counter-clockwise by that many degrees --
    pass ``det.attrs["upright_ccw_deg"]`` to stand the element up. Log headers are
    printed sideways, so for a stamp pressed onto one this is the difference between
    a legible crop and one nobody can read.
    """
    radius = max(1.0, (det.x1 - det.x0) / 2.0)
    half = int(round(radius * (1.0 + pad_frac)))
    box = Box.centered(det.center_x, det.center_y, half).clip(img.width, img.height)
    picture = ink_to_pil(img.read(box))
    if rotate_ccw_deg:
        picture = picture.rotate(rotate_ccw_deg, resample=Image.BICUBIC,
                                 expand=False, fillcolor=255)
    return _fit(picture, max_px)


def context_image(
    img: LogImage,
    det: Detection,
    view_in: float = 6.0,
    out_dpi: float = 150.0,
    max_px: int = 1700,
) -> Image.Image:
    """A wide view around the element with the element ringed in red.

    This is what "open the log at the spot" links to. A hyperlink cannot position a
    TIFF viewer at a pixel, and these pages run up to 110 ft long, so the tool
    renders the neighbourhood instead of asking anyone to go hunting for it.
    """
    half = int(round(img.px(view_in) / 2))
    box = Box.centered(det.center_x, det.center_y, half).clip(img.width, img.height)
    picture = ink_to_pil(img.read(box)).convert("RGB")

    scale = min(1.0, out_dpi / img.dpi)
    if scale < 1.0:
        picture = picture.resize((max(1, int(picture.width * scale)),
                                  max(1, int(picture.height * scale))), Image.LANCZOS)

    cx = (det.center_x - box.x0) * scale
    cy = (det.center_y - box.y0) * scale
    radius = (det.x1 - det.x0) / 2.0 * scale
    draw = ImageDraw.Draw(picture)
    width = max(2, int(round(radius * 0.05)))
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 outline=MARK_RGB, width=width)
    tick = radius * 1.5
    draw.line([cx - tick, cy, cx - radius * 1.15, cy], fill=MARK_RGB, width=width)
    draw.line([cx + radius * 1.15, cy, cx + tick, cy], fill=MARK_RGB, width=width)
    return _fit(picture, max_px)


def page_overview(img: LogImage, out_dpi: float = 25.0, max_px: int = 1400,
                   top_in: float | None = None) -> Image.Image:
    """Whole page (or its top ``top_in`` inches) shrunk to a thumbnail."""
    height = img.height if top_in is None else min(img.height, img.px(top_in))
    factor = max(1, int(round(img.dpi / out_dpi)))
    mask = img.read(Box(0, 0, img.width, height), factor=factor)
    return _fit(ink_to_pil(mask), max_px)


def page_ends(img: LogImage, top_in: float = 14.0, bottom_in: float = 10.0,
              out_dpi: float = 75.0, max_px: int = 1500) -> Image.Image:
    """The top and bottom of a page, stacked, with a rule between them.

    This is what a page with **no** detection gets, and the reason is a measured one:
    stamps are not only in the header. Of the stamps confirmed on this corpus, three
    sit within 3 inches of the *bottom* of pages 32 to 64 ft long, and those pages
    carry a second stamp at the top. A header-only thumbnail cannot rule out a stamp
    on such a page, so it cannot honestly support a negative -- and at the ~10 dpi a
    16-inch strip is reduced to in a spreadsheet cell, a 1.4-inch stamp is 15 px and
    invisible in any case.
    """
    factor = max(1, int(round(img.dpi / out_dpi)))
    top_px = min(img.height, img.px(top_in))
    top = img.read(Box(0, 0, img.width, top_px), factor=factor)

    bottom_px = min(img.height - top_px, img.px(bottom_in))
    parts = [top]
    if bottom_px > 0:
        parts.append(img.read(Box(0, img.height - bottom_px, img.width, img.height),
                              factor=factor))

    gap = 6
    width = max(p.shape[1] for p in parts)
    height = sum(p.shape[0] for p in parts) + gap * (len(parts) - 1)
    canvas = np.zeros((height, width), dtype=bool)
    y = 0
    for i, part in enumerate(parts):
        canvas[y:y + part.shape[0], :part.shape[1]] = part
        y += part.shape[0]
        if i < len(parts) - 1:
            canvas[y:y + gap, :] = True          # a solid rule marks the join
            y += gap
    return _fit(ink_to_pil(canvas), max_px)


def contact_sheet(images: list[Image.Image], labels: list[str], columns: int = 6,
                  tile: int = 240, pad: int = 8) -> Image.Image:
    """Grid of labelled thumbnails, for eyeballing a whole batch at once."""
    if not images:
        return Image.new("L", (1, 1), 255)
    label_h = 14
    cell = tile + pad
    rows = (len(images) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell, rows * (cell + label_h)), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    for i, (picture, label) in enumerate(zip(images, labels)):
        r, c = divmod(i, columns)
        x, y = c * cell, r * (cell + label_h)
        thumb = _fit(picture.convert("RGB"), tile)
        sheet.paste(thumb, (x, y))
        draw.text((x + 2, y + tile + 1), label[:38], fill=(40, 40, 40))
    return sheet


def save(image: Image.Image, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if path.lower().endswith((".jpg", ".jpeg")) and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.save(path, optimize=True)
    return path
