"""Rotation-invariant description of a circular element.

Resampling a circular stamp into (radius, angle) space turns rotation into a
*shift* along one axis. That buys two things at once:

* radial structure (outer ring / digit band / gap / inner text) becomes a 1-D
  profile that is completely independent of how the stamp was rotated;
* matching the angular signature by **circular** cross-correlation recovers the
  rotation angle as a by-product of scoring it.

That is the reason to prefer this over rotated template matching, which would need
one pass per angle and still not report the angle precisely.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def as_float(ink: np.ndarray) -> np.ndarray:
    """View a mask as float32 for sampling, copying only when it has to.

    Refinement probes one crop hundreds of times; converting inside the sampler
    instead of once at the call site dominated the whole detector's runtime.
    """
    return np.asarray(ink, dtype=np.float32)


def polar_sample(
    ink: np.ndarray,
    cx: float,
    cy: float,
    radius: float,
    r_lo: float = 0.30,
    r_hi: float = 1.10,
    n_r: int = 48,
    n_theta: int = 360,
) -> np.ndarray:
    """Resample ``ink`` around (cx, cy) onto a (n_r, n_theta) polar grid.

    Radii are fractions of ``radius``. Sampling is bilinear, so the profile does not
    flicker with sub-pixel centre changes. Samples outside the array read as 0.

    Pass a float32 array (see :func:`as_float`) when calling this in a loop.
    """
    rs = np.linspace(r_lo, r_hi, n_r) * radius
    thetas = np.linspace(0.0, 2.0 * np.pi, n_theta, endpoint=False)
    xs = cx + rs[:, None] * np.cos(thetas)[None, :]
    ys = cy + rs[:, None] * np.sin(thetas)[None, :]
    return _bilinear(as_float(ink), xs, ys)


def _bilinear(img: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    h, w = img.shape
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    fx = (xs - x0).astype(np.float32)
    fy = (ys - y0).astype(np.float32)

    def at(yy: np.ndarray, xx: np.ndarray) -> np.ndarray:
        ok = (yy >= 0) & (yy < h) & (xx >= 0) & (xx < w)
        out = np.zeros(yy.shape, dtype=np.float32)
        out[ok] = img[yy[ok], xx[ok]]
        return out

    return (at(y0, x0) * (1 - fx) * (1 - fy) + at(y0, x0 + 1) * fx * (1 - fy)
            + at(y0 + 1, x0) * (1 - fx) * fy + at(y0 + 1, x0 + 1) * fx * fy)


def radial_profile(polar: np.ndarray) -> np.ndarray:
    """Mean ink coverage as a function of radius. Rotation-invariant by construction."""
    return polar.mean(axis=1)


def angular_signature(polar: np.ndarray, r_lo_frac: float, r_hi_frac: float,
                      r_lo: float = 0.30, r_hi: float = 1.10) -> np.ndarray:
    """Mean ink over a radial band, as a function of angle.

    ``r_lo_frac`` / ``r_hi_frac`` select the band as fractions of the element
    radius; ``r_lo`` / ``r_hi`` must match what ``polar_sample`` was called with.
    """
    n_r = polar.shape[0]
    grid = np.linspace(r_lo, r_hi, n_r)
    band = (grid >= r_lo_frac) & (grid <= r_hi_frac)
    if not band.any():
        band = np.ones(n_r, dtype=bool)
    return polar[band].mean(axis=0)


def circular_ncc(signal: np.ndarray, template: np.ndarray) -> tuple[float, int]:
    """Best circular normalised cross-correlation of ``signal`` against ``template``.

    Returns ``(peak, shift_bins)`` where ``shift_bins`` is how far the signal is
    rotated relative to the template. Both inputs are mean-removed and unit-norm,
    so the peak is a plain correlation coefficient in [-1, 1] and is unaffected by
    how heavily the stamp was inked.
    """
    a = _normalise(signal)
    b = _normalise(template)
    if a is None or b is None:
        return 0.0, 0
    if a.size != b.size:
        b = np.interp(np.linspace(0, b.size, a.size, endpoint=False),
                      np.arange(b.size), b, period=b.size)
        b = _normalise(b)
        if b is None:
            return 0.0, 0
    spectrum = np.fft.rfft(a) * np.conj(np.fft.rfft(b))
    corr = np.fft.irfft(spectrum, n=a.size)
    shift = int(np.argmax(corr))
    return float(corr[shift]), shift


def _normalise(x: np.ndarray) -> np.ndarray | None:
    x = np.asarray(x, dtype=np.float64) - np.mean(x)
    norm = np.linalg.norm(x)
    return None if norm < 1e-9 else x / norm


def ring_completeness(polar: np.ndarray, r_lo: float = 0.30, r_hi: float = 1.10,
                      band: tuple[float, float] = (0.94, 1.04),
                      min_ink: float = 0.25) -> float:
    """Fraction of angles at which the outer ring is actually present.

    Tolerates a ring interrupted by overprinted rules or faint inking; a value near
    1.0 means an unbroken circle.
    """
    sig = angular_signature(polar, band[0], band[1], r_lo, r_hi)
    return float((sig >= min_ink).mean())


@dataclass
class RefineResult:
    cx: float
    cy: float
    radius: float
    ring_strength: float


@dataclass
class MatchResult:
    """Best template match over a small neighbourhood of a candidate's geometry."""

    peak: float
    shift_bins: int
    n_bins: int
    cx: float
    cy: float
    radius: float

    @property
    def angle_deg(self) -> float:
        """Rotation of the element, CCW, relative to the template frame."""
        return (-(self.shift_bins / self.n_bins) * 360.0) % 360.0


def band_signature(ink: np.ndarray, cx: float, cy: float, radius: float,
                   band: tuple[float, float], n_r: int = 12,
                   n_theta: int = 360) -> np.ndarray:
    """Angular signature of one radial band, sampled directly (no full polar grid)."""
    rs = np.linspace(band[0], band[1], n_r) * radius
    thetas = np.linspace(0.0, 2.0 * np.pi, n_theta, endpoint=False)
    xs = cx + rs[:, None] * np.cos(thetas)[None, :]
    ys = cy + rs[:, None] * np.sin(thetas)[None, :]
    return _bilinear(as_float(ink), xs, ys).mean(axis=0)


def best_signature_match(
    ink: np.ndarray,
    cx: float,
    cy: float,
    radius: float,
    template: np.ndarray,
    band: tuple[float, float],
    search_px: float = 6.0,
    step_px: float = 1.5,
    radius_tol: float = 0.06,
    n_radius_steps: int = 3,
) -> MatchResult:
    """Correlate the template over a small neighbourhood of the candidate geometry.

    Why search at all, rather than trusting the refined circle: the angular signature
    is *extremely* sensitive to the centre and barely sensitive to the radius.
    Measured on the reference stamp -- a 4 px centre error (1.4 % of R) drops the
    correlation from 1.00 to 0.63 and 8 px drops it to 0.32, while an 8.6 % radius
    error still scores 0.82. A circle-fitting objective does not localise to 2 px on
    a stamp whose outline is broken by overprinted rules, so asking it to would be
    asking the wrong question.

    Optimising the match instead makes the verification self-correcting, and the
    geometry that best explains the dial is also the right geometry to measure every
    other band at. Widening the radial band does not substitute for this: the offset
    smears the dial non-uniformly, which no amount of radial averaging undoes.

    Searched coarse-to-fine over the **centre only**, then a short radius sweep, and
    at reduced angular resolution until the final scoring pass -- exploiting the same
    asymmetry: spending probes on the radius, or on 360 bins throughout, costs several
    times the runtime for no accuracy.
    """
    def probe(x: float, y: float, r: float, n_theta: int) -> MatchResult:
        sig = band_signature(ink, x, y, r, band, n_r=8, n_theta=n_theta)
        peak, shift = circular_ncc(sig, template)
        return MatchResult(peak, shift, sig.size, x, y, r)

    # coarse centre sweep at the candidate radius
    coarse_step = max(1.0, step_px * 2)
    grid = np.arange(-search_px, search_px + 1e-9, coarse_step)
    best = max((probe(cx + dx, cy + dy, radius, 180) for dy in grid for dx in grid),
               key=lambda m: m.peak)

    # fine centre sweep about the coarse winner
    fine = np.arange(-coarse_step, coarse_step + 1e-9, max(0.5, step_px))
    best = max((probe(best.cx + dx, best.cy + dy, best.radius, 180)
                for dy in fine for dx in fine), key=lambda m: m.peak)

    # short radius sweep -- the signature barely cares, so a few steps suffice
    if n_radius_steps > 1:
        scales = np.linspace(1 - radius_tol, 1 + radius_tol, n_radius_steps)
        best = max((probe(best.cx, best.cy, float(best.radius * s), 180) for s in scales),
                   key=lambda m: m.peak)

    return probe(best.cx, best.cy, best.radius, len(template))


def refine_circle(
    ink: np.ndarray,
    cx: float,
    cy: float,
    radius: float,
    search_px: float = 6.0,
    step_px: float = 2.0,
    radius_tol: float = 0.12,
    n_radius_steps: int = 9,
) -> RefineResult:
    """Local refinement of centre and radius by maximising the outer-ring response.

    The Hough peak lands within a few pixels of centre; pinning it down matters
    because every later measurement (the digit band, the arrow angle) is expressed
    as a *fraction* of the radius, so a 10 % radius error shifts every band.

    Centre and radius are refined in alternating passes rather than as one 3-D grid:
    the two are nearly independent here, so two sweeps of ~50 probes reach the same
    optimum as several hundred.
    """
    img = as_float(ink)
    thetas = np.linspace(0.0, 2.0 * np.pi, 360, endpoint=False)
    cos_t, sin_t = np.cos(thetas), np.sin(thetas)

    def ink_on(x: float, y: float, r: float) -> float:
        return float(_bilinear(img, x + r * cos_t, y + r * sin_t).mean())

    def ring_ink(x: float, y: float, r: float, clear_frac: float = 1.12) -> float:
        """Sharpness of an outline at radius ``r``: ink on it, minus ink just outside.

        Maximising raw ink on the circle is not enough, and neither is preferring the
        outermost strong radius. Both let the estimate drift outward until it pins to
        whatever bound it is given -- measured on real stamps, four of ten landed on
        exactly the search maximum. Because every band is measured as a *fraction* of
        the radius, that pushes the dial band off the real dial and destroys the
        angular signature, which is the most discriminative cue there is.

        Requiring blank paper just outside removes the drift by construction: moving
        outward costs the first term without gaining on the second. Reference stamp:
        0.58 at the true outline, -0.04 on the dial band inside it, 0.01 beyond it.
        """
        return ink_on(x, y, r) - ink_on(x, y, r * clear_frac)

    def pick(probes: list[tuple[float, float, float, float]]) -> RefineResult:
        """Simply the sharpest probe -- see ``ring_ink`` for why that is sufficient."""
        return RefineResult(*max(probes, key=lambda p: p[3]))

    # --- pass 1: joint coarse grid over centre AND radius -------------------
    # Sweeping the centre first at a wrong radius locks in a wrong centre before
    # the radius is ever corrected, and the later fine passes are too narrow to
    # recover. Searching both together at low resolution avoids that ordering trap.
    coarse = np.arange(-search_px, search_px + 1e-9, max(1.0, step_px * 1.5))
    scales = np.linspace(1 - radius_tol, 1 + radius_tol, n_radius_steps)
    probes = [(cx + dx, cy + dy, float(radius * s),
               ring_ink(cx + dx, cy + dy, float(radius * s)))
              for dy in coarse for dx in coarse for s in scales]
    best = pick(probes)

    # --- passes 2-3: alternate fine sweeps about a fixed origin -------------
    offsets = np.arange(-step_px * 1.5, step_px * 1.5 + 1e-9, max(0.5, step_px / 2))
    tol = radius_tol / 2.0
    for _ in range(2):
        ox, oy, r_now = best.cx, best.cy, best.radius
        best = pick([(ox + dx, oy + dy, r_now, ring_ink(ox + dx, oy + dy, r_now))
                     for dy in offsets for dx in offsets] + [(ox, oy, r_now, best.ring_strength)])

        base_r = best.radius
        best = pick([(best.cx, best.cy, float(base_r * s),
                      ring_ink(best.cx, best.cy, float(base_r * s)))
                     for s in np.linspace(1 - tol, 1 + tol, n_radius_steps)])
        offsets = offsets / 2.0
        tol = tol / 2.0
    return best
