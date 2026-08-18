"""Element-agnostic cleanup of a bilevel log raster.

The one operation that matters most here is **long-line suppression**. Stamps and
other elements are printed *on top of* the log's ruled grid, so their outlines are
crossed by page-length rules and the raster is dominated by straight ink. Removing
runs longer than any real element leaves curved strokes and text behind, which
cuts the number of edge pixels the shape detectors have to consider by an order of
magnitude.

Openings use ``scipy.ndimage.minimum_filter1d`` / ``maximum_filter1d``, which run
in O(1) per pixel regardless of kernel length (van Herk / Gil-Werman), so a
1.5-inch structuring element costs the same as a 3-pixel one.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi


def _open_1d(mask: np.ndarray, size: int, axis: int) -> np.ndarray:
    """Morphological opening along one axis with a ``size``-long line element."""
    if size <= 1:
        return mask
    a = mask.astype(np.uint8, copy=False)
    eroded = ndi.minimum_filter1d(a, size, axis=axis, mode="constant", cval=0)
    return ndi.maximum_filter1d(eroded, size, axis=axis, mode="constant", cval=0) > 0


def long_line_mask(
    ink: np.ndarray,
    run_px: int,
    thickness_px: int = 3,
) -> np.ndarray:
    """Pixels belonging to ink runs at least ``run_px`` long, horizontal or vertical.

    ``thickness_px`` dilates across the run first, so a rule that drifts by a pixel
    from scanner skew is still detected as one continuous line.
    """
    if run_px <= 1:
        return np.zeros_like(ink, dtype=bool)
    t = max(1, thickness_px)

    fat_rows = ndi.maximum_filter1d(ink.astype(np.uint8), t, axis=0) > 0
    horizontal = _open_1d(fat_rows, run_px, axis=1)

    fat_cols = ndi.maximum_filter1d(ink.astype(np.uint8), t, axis=1) > 0
    vertical = _open_1d(fat_cols, run_px, axis=0)

    return (horizontal | vertical) & ink


def suppress_long_lines(ink: np.ndarray, run_px: int, thickness_px: int = 3) -> np.ndarray:
    """``ink`` with the ruled grid taken out.

    ``run_px`` must comfortably exceed the longest straight run inside the element
    being looked for. A circle of radius R has a chord that stays within
    ``c^2 / 8R`` of straight, so for a 0.65-inch-radius stamp ring a 1.5-inch
    threshold cannot touch the ring itself.
    """
    return ink & ~long_line_mask(ink, run_px, thickness_px)


def despeckle(ink: np.ndarray, min_area_px: int) -> np.ndarray:
    """Drop connected components smaller than ``min_area_px`` (scanner grit)."""
    if min_area_px <= 1:
        return ink
    labels, n = ndi.label(ink)
    if n == 0:
        return ink
    areas = np.bincount(labels.ravel())
    keep = areas >= min_area_px
    keep[0] = False
    return keep[labels]


def boundary(ink: np.ndarray) -> np.ndarray:
    """The inner boundary of the ink: ink pixels adjacent to background.

    ``border_value=1`` treats everything outside the array as ink, so the array's own
    edge is not reported as an ink edge. Without it, the page margin and every band
    cut become a long phantom edge whose normals all point inward, and the Hough
    dutifully finds a column of non-existent circles one radius in from the margin.
    """
    return ink & ~ndi.binary_erosion(ink, structure=np.ones((3, 3), bool), border_value=1)


def gradients(ink: np.ndarray, sigma: float = 1.2) -> tuple[np.ndarray, np.ndarray]:
    """Unit gradient components (gx, gy) of a smoothed ink mask.

    A bilevel mask has no usable gradient until it is blurred; sigma ~1 px gives a
    stable normal direction for the Hough vote without smearing thin strokes.
    """
    smooth = ndi.gaussian_filter(ink.astype(np.float32), sigma)
    gy = ndi.sobel(smooth, axis=0, mode="constant")
    gx = ndi.sobel(smooth, axis=1, mode="constant")
    mag = np.hypot(gx, gy)
    np.maximum(mag, 1e-6, out=mag)
    return gx / mag, gy / mag


def ink_density(ink: np.ndarray, window_px: int) -> np.ndarray:
    """Fraction of ink in a square window around every pixel."""
    return ndi.uniform_filter(ink.astype(np.float32), size=max(1, window_px))
