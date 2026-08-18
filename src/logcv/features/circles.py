"""Find circular outlines of a known physical size.

Gradient-voting Hough transform: every boundary pixel votes for a centre at
distance R along its gradient normal, in both directions. Cost is O(edge pixels x
radii), not O(pixels x radii), which is what makes a full 2.75-gigapixel page
affordable, and votes accumulate happily from a **broken** arc -- essential here,
because the stamp's ring is chopped up by the log grid it was pressed over.

Votes are accumulated with ``np.bincount`` on flattened indices rather than
``np.add.at``, which is roughly an order of magnitude faster for this many hits.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage as ndi

from ..preprocess import boundary, gradients


@dataclass
class RingCandidate:
    """A possible circle, in the coordinate frame of the array searched."""

    cx: float
    cy: float
    radius: float
    votes: float

    def scaled(self, factor: int, dx: int = 0, dy: int = 0) -> "RingCandidate":
        """Lift from a decimated band frame back into native page pixels."""
        return RingCandidate(
            cx=self.cx * factor + dx,
            cy=self.cy * factor + dy,
            radius=self.radius * factor,
            votes=self.votes,
        )


def hough_ring_candidates(
    ink: np.ndarray,
    radii: np.ndarray | list[float],
    min_vote_fraction: float = 0.25,
    nms_radius_px: int | None = None,
    gather_fraction: float = 0.22,
    max_candidates: int = 40,
    edge_stride: int = 1,
) -> list[RingCandidate]:
    """Candidate circles whose radius is in ``radii`` (pixels, same frame as ``ink``).

    ``min_vote_fraction`` is the share of a full circumference that must vote, so
    the threshold scales with radius instead of needing a magic vote count. 0.25
    means a quarter of the ring is enough -- deliberately permissive, since the
    verification stage is what actually decides.

    ``gather_fraction`` sets the box-sum window used to collect each centre's votes,
    as a fraction of the radius. It cannot be zero: a boundary pixel's normal carries
    an angular error of a few degrees, which throws its vote ``R * epsilon`` off the
    true centre, so the votes for one circle arrive as a cloud ~0.2 R across rather
    than stacked on a single cell. The window must gather that cloud, and it must
    **sum** (not average) the votes, so the peak stays directly comparable to the
    ``2 * pi * R`` votes a complete ring would cast.
    """
    if ink.size == 0 or not ink.any():
        return []

    edges = boundary(ink)
    ys, xs = np.nonzero(edges)
    if edge_stride > 1:
        ys, xs = ys[::edge_stride], xs[::edge_stride]
    if ys.size == 0:
        return []

    gx, gy = gradients(ink)
    nx = gx[ys, xs]
    ny = gy[ys, xs]

    h, w = ink.shape
    found: list[RingCandidate] = []

    for radius in radii:
        acc = np.zeros(h * w, dtype=np.float32)
        for sign in (1.0, -1.0):
            cx = np.rint(xs + sign * radius * nx).astype(np.int64)
            cy = np.rint(ys + sign * radius * ny).astype(np.int64)
            ok = (cx >= 0) & (cx < w) & (cy >= 0) & (cy < h)
            if not ok.any():
                continue
            flat = cy[ok] * w + cx[ok]
            acc += np.bincount(flat, minlength=h * w).astype(np.float32)

        # Sum (not average) the vote cloud, so the peak is a vote count.
        grid = box_sum(acc.reshape(h, w), max(3, int(round(gather_fraction * radius))))

        # A full ring of this radius contributes ~2*pi*R votes (before stride).
        expected = 2.0 * np.pi * radius / max(1, edge_stride)
        threshold = min_vote_fraction * expected
        if grid.max() < threshold:
            continue

        peak_radius = max(3, int(round(radius * 0.6)))
        local_max = ndi.maximum_filter(grid, size=2 * peak_radius + 1)
        hits = (grid >= local_max) & (grid >= threshold)
        py, px_ = np.nonzero(hits)
        for y, x in zip(py, px_):
            found.append(RingCandidate(float(x), float(y), float(radius), float(grid[y, x])))

    return _nms(found, nms_radius_px or int(round(0.6 * float(np.min(radii)))))[:max_candidates]


def box_sum(grid: np.ndarray, size: int) -> np.ndarray:
    """Sum of ``grid`` over a ``size`` x ``size`` window centred on every pixel.

    ``uniform_filter`` averages, so multiply the area back out. O(1) per pixel.
    """
    size = max(1, size | 1)
    return ndi.uniform_filter(grid, size=size, mode="constant", cval=0.0) * (size * size)


def _nms(cands: list[RingCandidate], min_sep: int) -> list[RingCandidate]:
    """Keep the strongest candidate within every ``min_sep`` neighbourhood.

    Runs across radii as well as positions, so one stamp yields one candidate
    rather than one per radius tried.
    """
    kept: list[RingCandidate] = []
    for cand in sorted(cands, key=lambda c: -c.votes):
        if all((cand.cx - k.cx) ** 2 + (cand.cy - k.cy) ** 2 > min_sep ** 2 for k in kept):
            kept.append(cand)
    return kept


def annulus_response(ink: np.ndarray, radius: float, thickness: float = 2.0) -> np.ndarray:
    """Correlate ``ink`` with a thin ring kernel of the given radius.

    An independent second opinion on the Hough vote, with different failure modes:
    matched filtering likes complete rings and tolerates missing gradients, the
    Hough likes crisp edges and tolerates missing arcs. Only affordable on small
    crops -- do not call it on a whole page.
    """
    size = int(np.ceil(2 * (radius + thickness))) | 1
    c = size // 2
    yy, xx = np.mgrid[:size, :size]
    dist = np.hypot(yy - c, xx - c)
    kernel = (np.abs(dist - radius) <= thickness).astype(np.float32)
    total = kernel.sum()
    if total == 0:
        return np.zeros_like(ink, dtype=np.float32)
    kernel /= total
    return ndi.correlate(ink.astype(np.float32), kernel, mode="constant", cval=0.0)
