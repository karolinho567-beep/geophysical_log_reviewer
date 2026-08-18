"""Unit tests for logcv.

Weighted towards the things that failed **silently** during development, because
those are the ones that will fail silently again:

* the Hough vote threshold, which compared a density against a count and so found
  nothing at all while looking perfectly healthy;
* the rotation sign convention, which is 50/50 by inspection and would have
  delivered upside-down crops;
* bilevel polarity, where getting MINISWHITE backwards yields a page that is 85 %
  "ink" and still runs end to end.

Run:  pytest tests -q     (from the project root, with src/ on PYTHONPATH)
"""
from __future__ import annotations

import numpy as np
import pytest

from logcv.detectors.date_stamp import _ratio_score, _trapezoid
from logcv.detectors.registry import available, build
from logcv.detection import Detection
from logcv.features import polar as P
from logcv.features.circles import box_sum, hough_ring_candidates
from logcv.io import max_pool
from logcv.preprocess import boundary, suppress_long_lines
from logcv.units import Box, inches_to_px


def make_ring(h=300, w=320, cx=150, cy=160, r=70, thickness=1.0):
    yy, xx = np.mgrid[:h, :w]
    return np.abs(np.hypot(yy - cy, xx - cx) - r) <= thickness


# --------------------------------------------------------------------- units

def test_box_geometry():
    box = Box.centered(100.4, 200.6, 10)
    assert box.width == box.height == 20
    assert box.center == (100.0, 201.0)
    assert box.clip(50, 50).as_tuple() == (50, 50, 50, 50)
    assert box.clip(50, 50).is_empty


def test_inches_to_px_never_zero():
    assert inches_to_px(0.0001, 400) == 1
    assert inches_to_px(0.70, 400) == 280


# ------------------------------------------------------------------ raster io

def test_max_pool_keeps_thin_ink():
    """Averaging would erase a 1-px stroke; MAX pooling must not."""
    mask = np.zeros((16, 16), bool)
    mask[7, :] = True
    pooled = max_pool(mask, 4)
    assert pooled.shape == (4, 4)
    assert pooled[1].all()
    assert not pooled[0].any()


def test_max_pool_identity():
    mask = np.eye(8, dtype=bool)
    assert max_pool(mask, 1) is mask


# ---------------------------------------------------------------- preprocess

def test_suppress_long_lines_removes_rule_keeps_ring():
    ring = make_ring()
    with_rule = ring.copy()
    with_rule[160, :] = True          # a page-long horizontal rule through the centre
    cleaned = suppress_long_lines(with_rule, run_px=150)
    assert cleaned[160].sum() < 10, "the rule should be gone"
    # the ring must survive: its straightest chord deviates far more than the
    # dilation tolerance over a 150-px run
    kept = (cleaned & ring).sum() / ring.sum()
    assert kept > 0.85, f"ring damaged by line suppression: only {kept:.2f} kept"


def test_boundary_ignores_array_edge():
    """Otherwise every band cut becomes a phantom edge and votes for fake circles."""
    solid = np.ones((20, 20), bool)
    assert not boundary(solid).any()


# -------------------------------------------------------------------- circles

def test_box_sum_preserves_total():
    grid = np.zeros((21, 21), np.float32)
    grid[10, 10] = 7.0
    assert box_sum(grid, 5)[10, 10] == pytest.approx(7.0)
    grid[10, 12] = 3.0
    assert box_sum(grid, 5)[10, 10] == pytest.approx(10.0)


def test_hough_finds_synthetic_ring():
    """The regression guard for the count-vs-density threshold bug."""
    ring = make_ring(r=70)
    found = hough_ring_candidates(ring, np.arange(60, 81, 3.0), min_vote_fraction=0.25)
    assert found, "no candidate for a clean synthetic ring"
    best = found[0]
    assert abs(best.cx - 150) <= 4 and abs(best.cy - 160) <= 4
    assert abs(best.radius - 70) <= 4
    assert best.votes > 0.25 * 2 * np.pi * 70


def test_hough_ignores_blank_and_noise():
    assert hough_ring_candidates(np.zeros((80, 80), bool), [20.0]) == []
    rng = np.random.default_rng(0)
    speckle = rng.random((300, 320)) < 0.02
    found = hough_ring_candidates(speckle, np.arange(60, 81, 5.0), min_vote_fraction=0.25)
    assert not found, "sparse noise should not produce circles"


def test_ring_candidate_scaled_to_native():
    from logcv.features.circles import RingCandidate

    lifted = RingCandidate(10.0, 20.0, 5.0, 99.0).scaled(4, dx=100, dy=200)
    assert (lifted.cx, lifted.cy, lifted.radius) == (140.0, 280.0, 20.0)


# ---------------------------------------------------------------------- polar

def test_polar_sample_reads_the_ring():
    ring = make_ring(r=70, thickness=2.0)
    polar = P.polar_sample(ring, 150, 160, 70, r_lo=0.5, r_hi=1.2, n_r=36, n_theta=180)
    profile = P.radial_profile(polar)
    peak = int(np.argmax(profile))
    grid = np.linspace(0.5, 1.2, 36)
    assert grid[peak] == pytest.approx(1.0, abs=0.05)
    assert profile[peak] > 0.9


def test_ring_completeness_detects_a_gap():
    full = make_ring(thickness=2.0)
    assert P.ring_completeness(P.polar_sample(full, 150, 160, 70, 0.25, 1.20, 56, 360),
                               0.25, 1.20) > 0.95
    broken = full.copy()
    broken[:, 150:] = False           # remove half
    got = P.ring_completeness(P.polar_sample(broken, 150, 160, 70, 0.25, 1.20, 56, 360),
                              0.25, 1.20)
    assert 0.35 < got < 0.65, got


def test_circular_ncc_recovers_a_known_shift():
    rng = np.random.default_rng(1)
    template = rng.random(360)
    for shift in (0, 17, 90, 271):
        peak, got = P.circular_ncc(np.roll(template, shift), template)
        assert got == shift
        assert peak == pytest.approx(1.0, abs=1e-6)


def test_circular_ncc_sign_matches_ccw_rotation():
    """A CCW image rotation must appear as a NEGATIVE shift.

    This is the convention the detector inverts to report ``angle_deg``; get it
    backwards and every crop comes out mirrored about the wrong axis.
    """
    h = w = 241
    cx = cy = 120.0
    yy, xx = np.mgrid[:h, :w]
    theta = np.arctan2(yy - cy, xx - cx)
    radius = np.hypot(yy - cy, xx - cx)
    # a lopsided ring: one bright wedge, so the signature has a unique peak
    pattern = ((radius > 60) & (radius < 80) & (np.abs(theta) < 0.6))

    def signature(mask):
        polar = P.polar_sample(mask, cx, cy, 70.0, 0.85, 1.15, 12, 360)
        return P.angular_signature(polar, 0.85, 1.15, 0.85, 1.15)

    base = signature(pattern)
    for applied_ccw in (30, 90):
        # rotate the *pattern* CCW by constructing it at theta - applied
        shifted_theta = theta + np.deg2rad(applied_ccw)
        rotated = ((radius > 60) & (radius < 80)
                   & (np.abs(np.arctan2(np.sin(shifted_theta), np.cos(shifted_theta))) < 0.6))
        _, shift = P.circular_ncc(signature(rotated), base)
        assert min(shift, 360 - shift) == pytest.approx(applied_ccw, abs=4)


def _stamp_like(cx=150.0, cy=160.0, r=70.0, h=320, w=320):
    """A crude stand-in for the real stamp: solid outline, sparser inner dial.

    Densities follow the reference measurements -- outline ~1.0 along its circle,
    dial band ~0.45 -- because the refinement's job is to prefer the outline, and a
    fixture with an unrealistically dense dial tests the opposite.

    The dial is deliberately **not** perfectly periodic. A pure ``cos(31*theta)``
    ring has 31 equally good rotational alignments, so any shift the correlation
    reports is arbitrary and a test asserting one is meaningless. The real stamp
    breaks that symmetry with the gap between 31 and 1 and with the day arrow, so the
    fixture does too.
    """
    yy, xx = np.mgrid[:h, :w]
    radius = np.hypot(yy - cy, xx - cx)
    theta = np.arctan2(yy - cy, xx - cx)
    mask = np.abs(radius - r) <= 1.5                                  # outline
    dial = (radius > r * 0.78) & (radius < r * 0.95)
    mask |= dial & (np.cos(theta * 31) > 0.1)                         # 31 numerals
    mask &= ~(dial & (np.abs(theta) < 0.10))                          # the 31->1 gap
    mask |= dial & (np.abs(theta - 1.2) < 0.06)                       # the day arrow
    inner = (radius > r * 0.30) & (radius < r * 0.58)
    mask |= inner & (np.sin(yy * 0.7) > 0.6)                          # text lines
    return mask


def test_refine_circle_locks_onto_the_outline_not_the_inner_band():
    """Regression: the search drifted 17 px off centre and settled on the dial band."""
    mask = _stamp_like()
    refined = P.refine_circle(mask, 152, 158, 74, search_px=6, step_px=2, radius_tol=0.18)
    assert refined.radius == pytest.approx(70, abs=3), refined.radius
    assert abs(refined.cx - 150) <= 2.5 and abs(refined.cy - 160) <= 2.5


def test_refine_circle_does_not_wander_from_a_good_start():
    """A correct candidate must survive refinement unchanged."""
    mask = _stamp_like()
    refined = P.refine_circle(mask, 150, 160, 70, search_px=6, step_px=2)
    assert abs(refined.cx - 150) <= 1 and abs(refined.cy - 160) <= 1
    assert refined.radius == pytest.approx(70, abs=2)


def test_signature_match_recovers_an_offset_centre():
    """The dial signature needs the centre to ~2 px; the match search must find it.

    Measured on the reference stamp: a 4 px centre error (1.4 % of R) drops the
    correlation from 1.00 to 0.63, and 8 px drops it to 0.32 -- which is why
    verification optimises the match instead of trusting the circle fit.
    """
    from logcv.detectors.date_stamp import BAND_DIGITS

    mask = _stamp_like(cx=150.0, cy=160.0, r=70.0)
    template = P.band_signature(mask, 150.0, 160.0, 70.0, BAND_DIGITS)
    match = P.best_signature_match(mask, 156.0, 154.0, 70.0, template, BAND_DIGITS,
                                   search_px=8.0, step_px=1.5)
    assert match.peak > 0.9, match.peak
    assert abs(match.cx - 150) <= 2 and abs(match.cy - 160) <= 2
    # the fixture's dial is asymmetric, so the alignment is unique
    assert min(match.shift_bins, 360 - match.shift_bins) <= 4, match.shift_bins


def test_signature_match_is_rotation_invariant():
    from logcv.detectors.date_stamp import BAND_DIGITS

    upright = _stamp_like()
    template = P.band_signature(upright, 150.0, 160.0, 70.0, BAND_DIGITS)
    match = P.best_signature_match(upright, 150.0, 160.0, 70.0, template, BAND_DIGITS,
                                   search_px=3.0, step_px=1.5)
    assert match.peak > 0.95
    assert 0.0 <= match.angle_deg < 360.0


# ----------------------------------------------------------------- orientation

def test_line_direction_finds_stacked_lines():
    from logcv.features.orientation import line_direction

    for truth in (0, 30, 90, 135):
        canvas = np.zeros((200, 200), bool)
        yy, xx = np.mgrid[:200, :200]
        theta = np.deg2rad(truth)
        # lines perpendicular to the projection axis, spaced 14 px apart
        t = -xx * np.sin(theta) + yy * np.cos(theta)
        canvas |= (np.abs(((t + 700) % 14) - 7) < 1.2)
        got, strength = line_direction(canvas)
        delta = abs(got - truth) % 180
        assert min(delta, 180 - delta) <= 3, f"truth {truth}, got {got}"
        assert strength > 1.5


def test_line_direction_reports_weak_on_isotropic_ink():
    """Random ink measures ~1.5, a real stamp's text ~2.2 -- hence MIN_STRENGTH=1.7."""
    from logcv.features.orientation import MIN_STRENGTH, line_direction

    rng = np.random.default_rng(3)
    _, strength = line_direction(rng.random((200, 200)) < 0.15)
    assert strength < MIN_STRENGTH


def test_upright_rotation_snaps_to_cardinal():
    """The raw estimate is only good to ~15 degrees, so it must snap."""
    from logcv.features.orientation import upright_rotation

    mask = _stamp_like()
    angle, strength = upright_rotation(mask, 150.0, 160.0, 70.0)
    assert strength > 1.7
    assert angle is not None
    assert angle % 90 == pytest.approx(0.0, abs=1e-6), angle


def test_disc_mask_clears_outside():
    from logcv.features.orientation import disc_mask

    solid = np.ones((101, 101), bool)
    kept = disc_mask(solid, 50, 50, 20)
    assert kept[50, 50] and not kept[50, 90]
    assert kept.sum() == pytest.approx(np.pi * 20 ** 2, rel=0.05)


# --------------------------------------------------------------------- scoring

def test_trapezoid_shape():
    assert _trapezoid(0.0, 0.1, 0.2, 0.8, 0.9) == 0.0
    assert _trapezoid(0.15, 0.1, 0.2, 0.8, 0.9) == pytest.approx(0.5)
    assert _trapezoid(0.5, 0.1, 0.2, 0.8, 0.9) == 1.0
    assert _trapezoid(0.85, 0.1, 0.2, 0.8, 0.9) == pytest.approx(0.5)
    assert _trapezoid(1.0, 0.1, 0.2, 0.8, 0.9) == 0.0


def test_ratio_score():
    assert _ratio_score(0.1, 1.0, good=0.3, bad=0.8) == 1.0
    assert _ratio_score(0.8, 1.0, good=0.3, bad=0.8) == 0.0
    assert _ratio_score(0.55, 1.0, good=0.3, bad=0.8) == pytest.approx(0.5)
    assert _ratio_score(0.5, 0.0, good=0.3, bad=0.8) == 0.0   # no divide-by-zero


# -------------------------------------------------------------------- plumbing

def test_registry_exposes_the_date_stamp_detector():
    assert "date_stamp" in available()
    detector = build("date_stamp")
    assert detector.name == "date_stamp"
    assert detector.config["radius_in_min"] < 0.70 < detector.config["radius_in_max"]


def test_detector_rejects_unknown_config():
    with pytest.raises(TypeError):
        build("date_stamp", radius_in_typo=1.0)


def test_detection_to_row_flattens_attrs_and_evidence():
    det = Detection(
        element_type="date_stamp", detector_name="date_stamp", detector_version="1.0",
        center_x=1.0, center_y=2.0, x0=0, y0=0, x1=2, y1=2,
        center_in_x=0.01, center_in_y=0.02, radius_in=0.7,
        score=0.9, decision="hit", angle_deg=12.0,
        attrs={"month": "OCT"}, evidence={"ring": 0.98765},
    )
    row = det.to_row()
    assert row["attr_month"] == "OCT"
    assert row["ev_ring"] == 0.9877
    assert "attrs" not in row and "evidence" not in row
