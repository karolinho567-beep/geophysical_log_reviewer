"""The output schema shared by every detector.

One ``Detection`` == one found element. The schema is deliberately stable and
element-agnostic: adding a header-block or API-number detector later must not
change these fields, so anything element-specific goes in ``attrs``.

Geometry is carried **twice** -- native page pixels for cropping, and inches for
anything a person or another dataset compares across files, because the corpus
mixes four scan resolutions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

Decision = Literal["hit", "uncertain", "miss"]


@dataclass
class Detection:
    # --- what and by whom -------------------------------------------------
    element_type: str
    detector_name: str
    detector_version: str

    # --- where (native page pixels) ---------------------------------------
    center_x: float
    center_y: float
    x0: int
    y0: int
    x1: int
    y1: int

    # --- where (resolution-free) ------------------------------------------
    center_in_x: float
    center_in_y: float
    radius_in: float

    # --- how sure ---------------------------------------------------------
    score: float
    decision: Decision
    angle_deg: float | None = None

    # --- element-specific, free-form --------------------------------------
    attrs: dict[str, Any] = field(default_factory=dict)
    #: Named sub-scores that produced ``score``. Kept so a rejected element can be
    #: explained without re-running the detector.
    evidence: dict[str, float] = field(default_factory=dict)
    crop_path: str | None = None
    context_path: str | None = None

    def to_row(self) -> dict[str, Any]:
        """Flatten for a table: attrs and evidence become prefixed columns."""
        row = asdict(self)
        row.pop("attrs")
        row.pop("evidence")
        for key, value in self.attrs.items():
            row[f"attr_{key}"] = value
        for key, value in self.evidence.items():
            row[f"ev_{key}"] = round(float(value), 4)
        return row


@dataclass
class PageResult:
    """Everything one detector found on one page, plus how the page was read."""

    source_file: str
    api14: str | None
    width: int
    height: int
    dpi: float
    dpi_source: str
    detections: list[Detection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def hits(self) -> list[Detection]:
        return [d for d in self.detections if d.decision == "hit"]

    @property
    def uncertain(self) -> list[Detection]:
        return [d for d in self.detections if d.decision == "uncertain"]

    @property
    def best(self) -> Detection | None:
        """Highest-scoring non-rejected detection, or None."""
        keep = [d for d in self.detections if d.decision != "miss"]
        return max(keep, key=lambda d: d.score) if keep else None

    def to_json(self, indent: int = 2) -> str:
        payload = {
            "source_file": self.source_file,
            "api14": self.api14,
            "width": self.width,
            "height": self.height,
            "dpi": self.dpi,
            "dpi_source": self.dpi_source,
            "seconds": round(self.seconds, 2),
            "warnings": self.warnings,
            "detections": [asdict(d) for d in self.detections],
        }
        return json.dumps(payload, indent=indent, default=str)
