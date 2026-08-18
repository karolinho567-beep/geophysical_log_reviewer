"""Lazy, memory-safe access to the scanned log rasters.

A single log page reaches 5200 x 528110 px (2.75 gigapixels). Decoding one whole
page into a PIL image would want ~2.7 GB, so all access here goes through GDAL
windowed reads, which land in ~0.02 s because the TIFFs are striped at 1024 rows.

Two things are normalised on the way in, once, so no detector has to care:

* **Polarity.** The Group 4 files are ``MINISWHITE``, so the raw sample value 1
  means *ink*. Palette/LZW files are the other way up. Everything this module
  returns is a boolean array where ``True`` == ink.
* **Resolution.** ``read()`` takes an integer decimation ``factor``; downsampling
  uses a MAX pool, not averaging, because averaging a bilevel scan erases the
  1-px strokes that the detectors key on.
"""
from __future__ import annotations

import os
import re
from typing import Iterator

import numpy as np
from osgeo import gdal

from .units import Box, inches_to_px

gdal.UseExceptions()

#: Fallback when the TIFF carries no usable resolution tag.
DEFAULT_DPI = 400.0
#: A log page narrower/wider than this (in inches) means the dpi tag is wrong.
PLAUSIBLE_WIDTH_IN = (4.0, 20.0)

_API14_RE = re.compile(r"(\d{14})")


class LogImage:
    """One scanned log page.

    Parameters
    ----------
    path:
        Path to the TIFF.
    dpi_override:
        Use this resolution instead of the file's tag (for files whose tag is
        missing or implausible).
    """

    def __init__(self, path: str, dpi_override: float | None = None):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(self.path)
        self._ds = gdal.Open(self.path)
        if self._ds is None:  # pragma: no cover - gdal raises with UseExceptions
            raise OSError(f"not a readable raster: {path}")
        self._band = self._ds.GetRasterBand(1)
        self.width = self._ds.RasterXSize
        self.height = self._ds.RasterYSize
        try:
            self.nbits = int(self._band.GetMetadataItem("NBITS", "IMAGE_STRUCTURE") or 0)
        except ValueError:
            self.nbits = 0

        structure = self._ds.GetMetadata("IMAGE_STRUCTURE")
        self.compression = structure.get("COMPRESSION", "")
        self._miniswhite = structure.get("MINISWHITE") == "YES"

        self.dpi, self.dpi_source = self._resolve_dpi(dpi_override)
        self.warnings: list[str] = []
        width_in = self.width / self.dpi
        if not PLAUSIBLE_WIDTH_IN[0] <= width_in <= PLAUSIBLE_WIDTH_IN[1]:
            self.warnings.append(
                f"page width {width_in:.1f} in implausible at {self.dpi:g} dpi"
            )

    # ------------------------------------------------------------------ dpi

    def _resolve_dpi(self, override: float | None) -> tuple[float, str]:
        if override:
            return float(override), "override"
        tag = self._ds.GetMetadataItem("TIFFTAG_XRESOLUTION")
        if tag:
            try:
                value = float(tag)
                unit = self._ds.GetMetadataItem("TIFFTAG_RESOLUTIONUNIT") or ""
                if "centimeter" in unit.lower() or unit.strip().startswith("3"):
                    value *= 2.54
                if value > 20:
                    return value, "tiff_tag"
            except ValueError:
                pass
        try:  # PIL reads the tag pair more forgivingly than GDAL does
            from PIL import Image

            Image.MAX_IMAGE_PIXELS = None
            with Image.open(self.path) as im:
                value = float(im.info.get("dpi", (0, 0))[0])
            if value > 20:
                return value, "pil_tag"
        except Exception:
            pass
        return DEFAULT_DPI, "default"

    # -------------------------------------------------------------- identity

    @property
    def api14(self) -> str | None:
        """The 14-digit API number carried by the filename, if present."""
        match = _API14_RE.search(self.name)
        return match.group(1) if match else None

    @property
    def size_in(self) -> tuple[float, float]:
        return self.width / self.dpi, self.height / self.dpi

    # ----------------------------------------------------------------- units

    def px(self, inches: float) -> int:
        """Physical length in inches -> whole pixels at this page's resolution."""
        return inches_to_px(inches, self.dpi)

    def inches(self, px: float) -> float:
        return px / self.dpi

    def factor_for_dpi(self, target_dpi: float) -> int:
        """Integer decimation that lands nearest ``target_dpi``."""
        return max(1, int(round(self.dpi / float(target_dpi))))

    def effective_dpi(self, factor: int) -> float:
        return self.dpi / factor

    # ------------------------------------------------------------------ read

    def read(self, box: Box | None = None, factor: int = 1) -> np.ndarray:
        """Return a boolean ink mask (``True`` == ink) for ``box``.

        ``factor`` decimates by MAX pooling, so thin strokes survive. The array is
        the decimated size; ``box`` is always in native page pixels.
        """
        box = (box or Box(0, 0, self.width, self.height)).clip(self.width, self.height)
        if box.is_empty:
            return np.zeros((0, 0), dtype=bool)

        raw = self._band.ReadAsArray(box.x0, box.y0, box.width, box.height)
        ink = self._to_ink(raw)
        if factor > 1:
            ink = max_pool(ink, factor)
        return ink

    def read_reduced_gray(
        self,
        box: Box,
        out_width: int,
        out_height: int,
        mode: str = "average",
    ) -> np.ndarray:
        """Read a native-pixel window directly at display resolution.

        GDAL performs the reduction inside RasterIO, avoiding a full-resolution
        Python allocation. ``average`` preserves true ink coverage; ``max`` keeps
        any dark stroke that intersects an output cell.
        """
        box = box.clip(self.width, self.height)
        if box.is_empty or out_width <= 0 or out_height <= 0:
            return np.full((0, 0), 255, dtype=np.uint8)
        if self.nbits == 1 and (out_width < box.width or out_height < box.height):
            # RasterIO's documented average resampler returns only 0/1 for NBITS=1
            # sources, even with a Float32 buffer. GDAL's dedicated overview
            # algorithm is the native path that preserves fractional coverage.
            window = gdal.Translate(
                "",
                self._ds,
                format="VRT",
                srcWin=[box.x0, box.y0, box.width, box.height],
            )
            reduced = gdal.GetDriverByName("MEM").Create(
                "", int(out_width), int(out_height), 1, gdal.GDT_Byte
            )
            gdal.RegenerateOverview(
                window.GetRasterBand(1),
                reduced.GetRasterBand(1),
                "AVERAGE_BIT2GRAYSCALE",
            )
            coverage_or_gray = np.asarray(
                reduced.GetRasterBand(1).ReadAsArray(), dtype=np.uint8
            )
            if mode == "max":
                return np.where(
                    coverage_or_gray > 0 if self._miniswhite else coverage_or_gray < 255,
                    0,
                    255,
                ).astype(np.uint8)
            return 255 - coverage_or_gray if self._miniswhite else coverage_or_gray

        if mode == "max":
            # "Bold" means preserve ink, whose numeric direction depends on
            # TIFF photometric polarity.
            name = "GRIORA_Max" if self._miniswhite else "GRIORA_Min"
            algorithm = getattr(gdal, name, gdal.GRIORA_NearestNeighbour)
        else:
            algorithm = gdal.GRIORA_Average
        raw = self._band.ReadAsArray(
            box.x0,
            box.y0,
            box.width,
            box.height,
            buf_xsize=int(out_width),
            buf_ysize=int(out_height),
            buf_type=gdal.GDT_Float32,
            resample_alg=algorithm,
        )
        raw = np.asarray(raw, dtype=np.float32)
        if self._miniswhite:
            denominator = 1.0 if raw.size == 0 or float(raw.max()) <= 1.0 else 255.0
            gray = 255.0 * (1.0 - np.clip(raw / denominator, 0.0, 1.0))
        else:
            multiplier = 255.0 if raw.size and float(raw.max()) <= 1.0 else 1.0
            gray = np.clip(raw * multiplier, 0.0, 255.0)
        return gray.round().astype(np.uint8)

    def _to_ink(self, raw: np.ndarray) -> np.ndarray:
        """Normalise polarity: True == ink, whatever the photometric tag says."""
        if raw.dtype != np.uint8:
            raw = raw.astype(np.uint8)
        hi = int(raw.max()) if raw.size else 0
        if self._miniswhite:
            # value 0 == white, so anything non-zero is ink
            return raw > 0
        # MINISBLACK / palette: 0 is black. Threshold at the midpoint.
        return raw < (max(hi, 1) / 2.0)

    def bands(
        self,
        factor: int = 1,
        band_in: float = 20.0,
        overlap_in: float = 2.0,
    ) -> Iterator[tuple[Box, np.ndarray]]:
        """Stream the page as overlapping horizontal bands.

        ``overlap_in`` must exceed the largest element being searched for, or an
        element straddling a band edge is seen twice in halves and found neither
        time. Yields ``(native_box, decimated_ink)``.
        """
        band_px = max(factor, self.px(band_in))
        overlap_px = self.px(overlap_in)
        step = max(factor, band_px - overlap_px)
        y = 0
        while y < self.height:
            y1 = min(self.height, y + band_px)
            box = Box(0, y, self.width, y1)
            yield box, self.read(box, factor=factor)
            if y1 >= self.height:
                break
            y += step

    def close(self) -> None:
        self._band = None
        self._ds = None

    def __enter__(self) -> "LogImage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover
        w, h = self.size_in
        return (f"<LogImage {self.name} {self.width}x{self.height}px "
                f"{w:.1f}x{h:.1f}in @{self.dpi:g}dpi>")


def max_pool(mask: np.ndarray, factor: int) -> np.ndarray:
    """Decimate a boolean mask by ``factor`` keeping any ink in each cell."""
    if factor <= 1:
        return mask
    h, w = mask.shape
    hh, ww = h // factor, w // factor
    if hh == 0 or ww == 0:
        return mask[:1, :1].copy()
    trimmed = mask[: hh * factor, : ww * factor]
    return trimmed.reshape(hh, factor, ww, factor).any(axis=(1, 3))


def iter_logs(folder: str, pattern: str = "*.tif") -> list[str]:
    """Every readable raster in ``folder``, sorted. Skips Thumbs.db and friends."""
    import fnmatch

    out = []
    for name in sorted(os.listdir(folder)):
        if not fnmatch.fnmatch(name.lower(), pattern.lower()):
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        out.append(path)
    return out
