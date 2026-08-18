"""Geometry in page pixels, with inches as the resolution-free unit.

The corpus mixes 100/200/300/400 dpi scans, so every threshold in this package is
declared in INCHES and converted per-image. Nothing downstream should ever hard
-code a pixel count.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Box:
    """Half-open pixel box in page coordinates: [x0, x1) x [y0, y1)."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def center(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0

    def expand(self, px: int) -> "Box":
        return Box(self.x0 - px, self.y0 - px, self.x1 + px, self.y1 + px)

    def clip(self, width: int, height: int) -> "Box":
        return Box(
            max(0, min(self.x0, width)),
            max(0, min(self.y0, height)),
            max(0, min(self.x1, width)),
            max(0, min(self.y1, height)),
        )

    def shift(self, dx: int, dy: int) -> "Box":
        return Box(self.x0 + dx, self.y0 + dy, self.x1 + dx, self.y1 + dy)

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    @classmethod
    def centered(cls, cx: float, cy: float, half: float) -> "Box":
        return cls(int(round(cx - half)), int(round(cy - half)),
                   int(round(cx + half)), int(round(cy + half)))

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x0, self.y0, self.x1, self.y1


def inches_to_px(inches: float, dpi: float) -> int:
    """Round a physical length to whole pixels, never below 1."""
    return max(1, int(round(inches * dpi)))
