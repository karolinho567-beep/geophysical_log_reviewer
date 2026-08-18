"""The contract every element detector implements.

Adding element #2 (header block, API number, operator, depth scale, ...) means
adding one module here and registering it. Nothing else in the package changes.

Two rules keep detectors composable:

1. A detector **takes a** :class:`~logcv.io.LogImage` **and returns
   Detections.** It never opens or writes files and never prints -- the CLI and
   ``report.py`` own all I/O, so the same detector works in a batch run, in a
   notebook, or under a unit test.
2. A detector always returns a **list**. Zero, one, or several of an element may
   be on a page, and no caller should assume which.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..detection import Detection
from ..io import LogImage


class Detector(ABC):
    #: Element type written into every Detection, e.g. ``"date_stamp"``.
    name: str = "unnamed"
    #: Bump whenever the algorithm changes, so old results stay interpretable.
    version: str = "0.0"
    #: Resolution the coarse search wants, in dpi. The page is decimated to the
    #: nearest integer factor of this, never resampled fractionally.
    work_dpi: float = 100.0

    def __init__(self, **config: Any):
        self.config: dict[str, Any] = {**self.defaults(), **config}
        unknown = set(config) - set(self.defaults())
        if unknown:
            raise TypeError(f"{self.name}: unknown config keys {sorted(unknown)}")

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        """Tunable parameters and their defaults. Lengths belong in INCHES."""
        return {}

    @abstractmethod
    def detect(self, img: LogImage) -> list[Detection]:
        """Find every instance of this element on ``img``."""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.name} v{self.version}>"
