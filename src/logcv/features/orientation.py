"""Which way up is an element?

For a circular stamp the outline says nothing about rotation, and the ring of
numerals is nearly periodic -- 31 near-identical glyphs 11.6 degrees apart -- so
correlating the dial alone gives 31 competing peaks and lands on the wrong one
often enough to produce sideways and upside-down crops.

The inner block of text is a far better cue: three parallel lines of type are
strongly anisotropic, so the direction in which they stack is recoverable from a
projection profile, and it is recoverable *modulo 180 degrees*. The dial is then
only asked to choose between two options instead of thirty-one, which it does
reliably.

The projection is computed straight from the ink coordinates rather than by
rotating the raster 180 times -- same answer, a few milliseconds.
"""
from __future__ import annotations

import numpy as np


def projection_sharpness(mask: np.ndarray, angles_deg: np.ndarray,
                         bin_px: float = 1.0) -> np.ndarray:
    """How sharply ``mask``'s ink stacks into lines, for each candidate angle.

    For each angle the ink is projected onto the perpendicular axis and binned; text
    lines running along that angle pile into a few tall bins, so the normalised
    variance of the profile peaks there.
    """
    ys, xs = np.nonzero(mask)
    if ys.size < 20:
        return np.zeros(len(angles_deg))
    ys = ys - ys.mean()
    xs = xs - xs.mean()

    out = np.empty(len(angles_deg))
    for i, angle in enumerate(angles_deg):
        theta = np.deg2rad(angle)
        t = (-xs * np.sin(theta) + ys * np.cos(theta)) / bin_px
        bins = (t - t.min()).astype(np.int64)
        profile = np.bincount(bins).astype(np.float64)
        mean = profile.mean()
        out[i] = profile.var() / (mean * mean) if mean > 0 else 0.0
    return out


def line_direction(mask: np.ndarray, step_deg: float = 2.0,
                   refine_deg: float = 0.5) -> tuple[float, float]:
    """Direction (degrees, modulo 180) along which linear structure runs.

    Returns ``(angle, strength)``. ``strength`` is the peak-to-median ratio of the
    projection sharpness: ~1 means there is no linear structure to find and the
    angle is meaningless, so callers should treat a low value as "unknown".
    """
    coarse = np.arange(0.0, 180.0, step_deg)
    scores = projection_sharpness(mask, coarse)
    if not scores.any():
        return 0.0, 0.0
    peak = float(coarse[int(np.argmax(scores))])

    fine = np.arange(peak - step_deg, peak + step_deg + 1e-9, refine_deg)
    fine_scores = projection_sharpness(mask, fine)
    best = float(fine[int(np.argmax(fine_scores))]) % 180.0

    median = float(np.median(scores))
    strength = float(np.max(scores) / median) if median > 0 else 0.0
    return best, strength


def disc_mask(mask: np.ndarray, cx: float, cy: float, radius: float) -> np.ndarray:
    """``mask`` with everything outside a centred disc cleared."""
    ys, xs = np.ogrid[: mask.shape[0], : mask.shape[1]]
    return mask & (((xs - cx) ** 2 + (ys - cy) ** 2) <= radius * radius)


#: Below this peak-to-median ratio the projection is indistinguishable from noise.
#: Measured: a confirmed stamp's inner text scores 2.16, random ink at similar
#: density scores 1.51.
MIN_STRENGTH = 1.7


def upright_rotation(
    mask: np.ndarray,
    cx: float,
    cy: float,
    radius: float,
    inner_frac: float = 0.62,
    disambiguate: "callable[[float], float] | None" = None,
    snap_deg: float = 90.0,
) -> tuple[float | None, float]:
    """Degrees to rotate counter-clockwise to stand the element upright.

    The text direction fixes the answer modulo 180 degrees; ``disambiguate`` is handed
    both candidates and the higher score wins. Pass the dial correlation for that -- a
    two-way choice it can be trusted with, unlike the 31-way one.

    The result is **snapped** to a multiple of ``snap_deg`` (90 by default). Log sheets
    are printed at cardinal orientations, and the raw estimate is only good to about
    15 degrees because the partially-suppressed ruled grid inside the stamp pulls the
    projection towards 0 and 90 -- on the reference stamp the raw angle reads 74 where
    the truth is 90. Snapping absorbs exactly that error, and an element that really
    does sit at 45 degrees is no less readable for being turned to the nearer
    neighbour. Pass ``snap_deg=0`` for the unsnapped estimate.
    """
    inner = disc_mask(mask, cx, cy, radius * inner_frac)
    angle, strength = line_direction(inner)
    if strength < MIN_STRENGTH:            # no usable linear structure
        return None, strength

    # Rotating the image CCW by `angle` brings a line running at `angle`
    # (measured with y downward) onto the horizontal.
    if snap_deg > 0:
        angle = round(angle / snap_deg) * snap_deg
    candidates = [angle % 360.0, (angle + 180.0) % 360.0]
    if disambiguate is None:
        return candidates[0], strength
    return max(candidates, key=disambiguate), strength
