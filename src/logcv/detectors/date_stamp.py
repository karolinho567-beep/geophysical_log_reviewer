"""Element #1 -- the circular records date stamp ("OCT 1995 / LOG REC'D / RECORDS").

The stamp is a rigid rubber stamp: fixed geometry, arbitrary rotation, pressed on
top of whatever was already printed there. So detection keys on its **geometry**,
never on the date text -- a stamp reading a different month or year is still a
stamp, and the date is reported as an attribute that is allowed to be unknown.

Measured design (from 42089319020000, 400 dpi), as fractions of the outer radius:

    1.00        solid outer circle
    0.78-0.95   annulus of numerals 1..31 (the day dial) + a solid arrowhead
    0.62-0.74   mostly blank gap
    0.30-0.58   three lines of inner text

Two stages:

* **coarse** -- at ~100 dpi, suppress the ruled grid, then gradient-Hough for
  rings of the right physical radius. Cheap, over-generous, tuned for recall.
* **verify** -- at native dpi, refine the circle and score the radial band
  structure above via a polar resample, which is rotation-invariant.

Anything scoring between the two thresholds is returned as ``uncertain`` rather
than being silently dropped, because a page of dense log header can produce
ring-like accidents and a faint stamp can produce a weak ring.
"""
from __future__ import annotations

import os
import time
from typing import Any

import numpy as np

from ..detection import Detection
from ..features import orientation as O
from ..features import polar as P
from ..features.circles import RingCandidate, hough_ring_candidates
from ..io import LogImage
from ..preprocess import suppress_long_lines
from ..units import Box
from .base import Detector
from .registry import register

# Radial bands of the stamp, as fractions of the outer radius.
BAND_RING = (0.94, 1.04)
BAND_DIGITS = (0.78, 0.95)
BAND_GAP = (0.62, 0.74)
BAND_TEXT = (0.30, 0.58)
BAND_OUTSIDE = (1.08, 1.16)

_POLAR_R_LO, _POLAR_R_HI = 0.25, 1.20


def default_template_path() -> str:
    """The reference signature shipped alongside the code."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "date_stamp_v1.npz")


@register("date_stamp")
class DateStampDetector(Detector):
    version = "1.0"
    work_dpi = 100.0

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        return {
            # --- geometry, in inches ---------------------------------------
            # Measured outer radius on the reference stamp is 0.70 in (1.40 in
            # across); the band allows for a different stamp die and for scans
            # whose resolution tag is a little off.
            "radius_in_min": 0.55,
            "radius_in_max": 0.85,
            "radius_steps": 7,
            # --- coarse pass -----------------------------------------------
            "band_in": 24.0,          # page slice height
            "overlap_in": 2.5,        # > one stamp diameter, so none is split
            "line_run_in": 1.5,       # grid rules are page-long; the ring is not
            "min_vote_fraction": 0.22,
            "max_candidates_per_band": 12,
            # --- verification ----------------------------------------------
            "verify_line_run_in": 0.35,
            "n_theta": 360,
            "n_radius": 56,
            # --- decision --------------------------------------------------
            "t_hit": 0.62,
            "t_miss": 0.38,
            "max_per_page": 6,
            # How many candidates per page get the (costly) template match.
            "match_budget": 3,
            # --- optional template for the rotation angle ------------------
            "template_npz": None,
        }

    def __init__(self, **config: Any):
        super().__init__(**config)
        self._upright_offset = 0.0
        self._template_signature = self._load_template()

    # ------------------------------------------------------------------ api

    def detect(self, img: LogImage) -> list[Detection]:
        candidates = self._coarse(img)

        # Score every candidate on geometry alone first -- that is cheap. The
        # template match is not, so it is spent only on the handful whose geometry
        # makes them plausible; the rest cannot become this page's answer anyway.
        ranked = []
        for cand in candidates[: self.config["max_per_page"] * 3]:
            prepared = self._verify(img, cand, with_match=False, rank_only=True)
            if prepared is not None:
                ranked.append((prepared.score, cand))
        ranked.sort(key=lambda pair: -pair[0])

        detections = []
        budget = self.config["match_budget"] if self._template_signature is not None else 0
        for i, (_, cand) in enumerate(ranked):
            det = self._verify(img, cand, with_match=i < budget)
            if det is not None:
                detections.append(det)

        detections.sort(key=lambda d: -d.score)
        return detections[: self.config["max_per_page"]]

    # --------------------------------------------------------------- coarse

    def _coarse(self, img: LogImage) -> list[RingCandidate]:
        """Ring candidates over the whole page, in native page pixels."""
        factor = img.factor_for_dpi(self.work_dpi)
        wdpi = img.effective_dpi(factor)
        radii = np.linspace(self.config["radius_in_min"],
                            self.config["radius_in_max"],
                            self.config["radius_steps"]) * wdpi
        run_px = max(3, int(round(self.config["line_run_in"] * wdpi)))

        out: list[RingCandidate] = []
        for box, ink in img.bands(factor=factor,
                                  band_in=self.config["band_in"],
                                  overlap_in=self.config["overlap_in"]):
            if not ink.any():
                continue
            clean = suppress_long_lines(ink, run_px)
            found = hough_ring_candidates(
                clean,
                radii,
                min_vote_fraction=self.config["min_vote_fraction"],
                max_candidates=self.config["max_candidates_per_band"],
            )
            out.extend(c.scaled(factor, dx=box.x0, dy=box.y0) for c in found)

        # Overlapping bands mean the same stamp can be reported twice.
        return _dedupe(out, min_sep=int(round(0.7 * self.config["radius_in_min"] * img.dpi)))

    # ----------------------------------------------------------------- verify

    def _verify(self, img: LogImage, cand: RingCandidate,
                with_match: bool = True, rank_only: bool = False) -> Detection | None:
        """Score one candidate at native resolution.

        ``with_match=False`` skips the template search. With ``rank_only`` the score is
        renormalised over the geometric cues alone -- a fair basis for choosing which
        candidates deserve the expensive match. Without it, a skipped match counts as
        a *measured* zero, so a candidate that never earned a dial match can never
        outrank one that did.
        """
        radius = cand.radius
        half = int(round(radius * 2.0))
        box = Box.centered(cand.cx, cand.cy, half).clip(img.width, img.height)
        if box.width < radius or box.height < radius:
            return None

        raw = img.read(box)
        cx = cand.cx - box.x0
        cy = cand.cy - box.y0

        if rank_only:
            # Ranking only needs a rough geometric score, and refinement is the
            # expensive half of verification. Skipping it here roughly halves the
            # per-page cost, since only the top few candidates are verified properly.
            refined = P.RefineResult(cx, cy, radius, 0.0)
        else:
            refined = P.refine_circle(
                raw, cx, cy, radius,
                search_px=max(2.0, radius * 0.08),
                step_px=max(1.0, radius * 0.02),
                radius_tol=0.18,
                n_radius_steps=13,
            )

        # Ring completeness wants the ring intact; band densities want the grid
        # gone, so the two are measured on different masks of the same crop.
        run_px = max(3, int(round(self.config["verify_line_run_in"] * img.dpi)))
        clean = suppress_long_lines(raw, run_px)

        # The circle fit gets the radius right but not the centre to the ~2 px the
        # dial correlation needs, so let the template match settle the geometry, then
        # measure every band there.
        angle, angular_score = None, None
        if with_match and self._template_signature is not None:
            match = P.best_signature_match(
                clean, refined.cx, refined.cy, refined.radius,
                self._template_signature, BAND_DIGITS,
                search_px=max(3.0, refined.radius * 0.045),
                step_px=max(1.0, refined.radius * 0.008),
            )
            if match.peak > 0:
                refined = P.RefineResult(match.cx, match.cy, match.radius,
                                         refined.ring_strength)
                angle, angular_score = match.angle_deg, match.peak

        polar_raw = self._polar(raw, refined)
        polar_clean = self._polar(clean, refined)
        geometry_only = rank_only or self._template_signature is None
        evidence, score = self._score(polar_raw, polar_clean, angular_score or 0.0,
                                      geometry_only)

        cfg = self.config
        decision = ("hit" if score >= cfg["t_hit"]
                    else "miss" if score < cfg["t_miss"]
                    else "uncertain")

        centre_x = box.x0 + refined.cx
        centre_y = box.y0 + refined.cy
        r = refined.radius
        clipped = (box.x0 == 0 and centre_x - r < 0) or (box.x1 == img.width and centre_x + r > img.width) \
            or (box.y0 == 0 and centre_y - r < 0) or (box.y1 == img.height and centre_y + r > img.height)

        return Detection(
            element_type="date_stamp",
            detector_name=self.name,
            detector_version=self.version,
            center_x=round(centre_x, 1),
            center_y=round(centre_y, 1),
            x0=int(round(centre_x - r)), y0=int(round(centre_y - r)),
            x1=int(round(centre_x + r)), y1=int(round(centre_y + r)),
            center_in_x=round(img.inches(centre_x), 3),
            center_in_y=round(img.inches(centre_y), 3),
            radius_in=round(img.inches(r), 3),
            score=round(score, 4),
            decision=decision,
            angle_deg=None if angle is None else round(angle, 1),
            attrs={
                "month": None, "year": None, "day": None,
                "clipped": bool(clipped),
                # How far to turn a crop counter-clockwise to stand the stamp up.
                "upright_ccw_deg": self._upright(clean, refined, angle),
            },
            evidence=evidence,
        )

    def _polar(self, ink: np.ndarray, refined: P.RefineResult) -> np.ndarray:
        return P.polar_sample(
            ink, refined.cx, refined.cy, refined.radius,
            r_lo=_POLAR_R_LO, r_hi=_POLAR_R_HI,
            n_r=self.config["n_radius"], n_theta=self.config["n_theta"],
        )

    # ------------------------------------------------------------------ score

    def _score(self, polar_raw: np.ndarray, polar_clean: np.ndarray,
               angular: float, geometry_only: bool) -> tuple[dict[str, float], float]:
        """Turn the radial band structure into named sub-scores and one number.

        Each sub-score is a 0..1 window on a measured quantity, so the evidence dict
        explains any decision without re-running anything.

        Thresholds come from the reference stamp measured against the false positives
        the coarse pass throws up on real pages -- dense tables of figures, which
        satisfy any test that only asks "is there ink in this band". What they cannot
        fake is a **sharp-edged** ring with clear paper outside it, or the angular
        signature of the 1..31 dial, so those two carry the most weight.
        """
        ring = P.ring_completeness(polar_raw, _POLAR_R_LO, _POLAR_R_HI, BAND_RING, min_ink=0.25)
        dens = {name: float(P.angular_signature(polar_clean, lo, hi, _POLAR_R_LO, _POLAR_R_HI).mean())
                for name, (lo, hi) in (("ring", BAND_RING), ("digits", BAND_DIGITS),
                                       ("gap", BAND_GAP), ("text", BAND_TEXT),
                                       ("outside", BAND_OUTSIDE))}
        contrast = dens["ring"] - dens["outside"]

        evidence = {
            # An unbroken circle.
            "ring": _trapezoid(ring, 0.40, 0.70, 1.01, 1.01),
            # The outline is ink and just past it is paper. Reference: 0.55 vs 0.05.
            "contrast": _trapezoid(contrast, 0.05, 0.30, 1.01, 1.01),
            # The day dial is inked but not solid.
            "digits": _trapezoid(dens["digits"], 0.06, 0.14, 0.35, 0.55),
            # The gap between dial and text is emptier than the dial. Reference: 0.34.
            "gap": _ratio_score(dens["gap"], dens["digits"], good=0.40, bad=0.90),
            # Three lines of text: present, but sparser than the dial.
            "text": _trapezoid(dens["text"], 0.03, 0.08, 0.30, 0.50),
            # Outside the ring is blank paper. Reference: 0.14; false positives 0.5+.
            "outside": _ratio_score(dens["outside"], dens["digits"], good=0.30, bad=0.85),
            # The 1..31 dial's angular signature -- the most specific cue there is,
            # and rotation-invariant. Kept at a weight that a smudged dial can
            # survive, since a stamp too faint to correlate is still a stamp.
            "angular": max(0.0, min(1.0, angular / 0.75)),
        }
        weights = {"ring": 0.14, "contrast": 0.20, "digits": 0.08, "gap": 0.10,
                   "text": 0.07, "outside": 0.16, "angular": 0.25}
        if geometry_only:                        # rank on the geometric cues alone
            weights.pop("angular")
            total = sum(weights.values())
            weights = {k: v / total for k, v in weights.items()}
        score = sum(evidence[k] * w for k, w in weights.items())

        evidence["raw_ring_completeness"] = ring
        evidence["raw_contrast"] = contrast
        evidence["raw_angular"] = angular
        evidence.update({f"raw_{k}": v for k, v in dens.items()})
        return evidence, float(score)

    # ------------------------------------------------------------------ upright

    def _upright(self, mask: np.ndarray, refined: P.RefineResult,
                 dial_angle: float | None) -> float | None:
        """Degrees CCW that stand this stamp up, for a legible crop.

        The dial is a poor orientation cue on its own -- 31 near-identical glyphs
        11.6 degrees apart give the correlation 31 competing peaks, and on the first
        full run it put 8 of 10 crops at the wrong angle and one fully upside down.
        The inner block of text is unambiguous modulo 180 degrees, so it sets the
        angle and the dial is only asked to break the tie.
        """
        def dial_score(candidate: float) -> float:
            if dial_angle is None:
                return 0.0
            # Prefer the candidate closer to what the dial correlation implies.
            delta = abs((candidate - (self._upright_offset - dial_angle)) % 360.0)
            return -min(delta, 360.0 - delta)

        angle, strength = O.upright_rotation(
            mask, refined.cx, refined.cy, refined.radius,
            disambiguate=dial_score if dial_angle is not None else None,
        )
        if angle is not None:
            return round(angle % 360.0, 1)
        if dial_angle is None:
            return None
        return round((self._upright_offset - dial_angle) % 360.0, 1)

    def _load_template(self) -> np.ndarray | None:
        """Load the reference signature, defaulting to the asset shipped with logcv."""
        path = self.config.get("template_npz")
        if path is None:
            path = default_template_path()
            if not os.path.exists(path):
                return None
        elif path is False:            # explicitly disabled
            return None
        with np.load(path, allow_pickle=False) as data:
            self._upright_offset = float(data["upright_ccw_deg"]) if "upright_ccw_deg" in data else 0.0
            return np.asarray(data["digit_signature"], dtype=np.float64)


def _trapezoid(value: float, lo: float, plateau_lo: float, plateau_hi: float, hi: float) -> float:
    """Trapezoidal membership: 0 below ``lo``, 1 across the plateau, 0 above ``hi``.

    Soft-edged on purpose: hard thresholds on any single measurement are what make
    classical detectors brittle on heterogeneous scans. Too little ink in a band and
    the feature is absent; far too much and we are looking at a blot, not a stamp.
    """
    if value <= lo or value >= hi:
        return 0.0
    if value < plateau_lo:
        return (value - lo) / max(1e-9, plateau_lo - lo)
    if value <= plateau_hi:
        return 1.0
    return (hi - value) / max(1e-9, hi - plateau_hi)


def _ratio_score(numerator: float, denominator: float, good: float, bad: float) -> float:
    """1.0 when ``numerator/denominator`` <= ``good``, 0.0 once it reaches ``bad``."""
    if denominator <= 1e-6:
        return 0.0
    ratio = numerator / denominator
    if ratio <= good:
        return 1.0
    if ratio >= bad:
        return 0.0
    return (bad - ratio) / (bad - good)


def _dedupe(cands: list[RingCandidate], min_sep: int) -> list[RingCandidate]:
    kept: list[RingCandidate] = []
    for cand in sorted(cands, key=lambda c: -c.votes):
        if all((cand.cx - k.cx) ** 2 + (cand.cy - k.cy) ** 2 > min_sep ** 2 for k in kept):
            kept.append(cand)
    return kept
