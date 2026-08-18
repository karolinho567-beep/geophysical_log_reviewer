"""Reusable, source-safe tile pyramids for interactive log viewing.

Tiles are disposable derivatives stored outside the source-image folder. The
source TIFF is never opened for writing. Cache identity includes the absolute
source path, file size, modification time, tile size, and cache format version,
so a changed source cannot silently reuse stale pixels.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from collections.abc import Callable

import numpy as np
from PIL import Image

from ..units import Box

CACHE_FORMAT_VERSION = 1
TILE_SIZE = 512
MEMORY_TILE_LIMIT = 128

TileReader = Callable[[Box, int, str], np.ndarray]


class TilePyramid:
    """Compose reduced-resolution windows from memory- and disk-cached tiles."""

    def __init__(
        self,
        source_path: str,
        width: int,
        height: int,
        cache_root: str | None,
        reader: TileReader,
        tile_size: int = TILE_SIZE,
        memory_limit: int = MEMORY_TILE_LIMIT,
    ):
        self.source_path = os.path.abspath(source_path)
        self.width = int(width)
        self.height = int(height)
        self.cache_root = os.path.abspath(cache_root) if cache_root else None
        self.reader = reader
        self.tile_size = max(32, int(tile_size))
        self.memory_limit = max(1, int(memory_limit))
        self._memory: OrderedDict[tuple[str, int, int, int], np.ndarray] = OrderedDict()
        self.cache_dir = self._cache_dir()
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)
            self._write_metadata()

    def _cache_dir(self) -> str | None:
        if not self.cache_root:
            return None
        stat = os.stat(self.source_path)
        canonical = os.path.normcase(self.source_path).encode("utf-8", "surrogatepass")
        source_id = hashlib.sha256(canonical).hexdigest()[:16]
        identity = (
            f"{stat.st_size}:{stat.st_mtime_ns}:{self.tile_size}:"
            f"{CACHE_FORMAT_VERSION}"
        ).encode("ascii")
        revision = hashlib.sha256(identity).hexdigest()[:16]
        return os.path.join(self.cache_root, source_id, revision)

    def _write_metadata(self) -> None:
        path = os.path.join(self.cache_dir, "source.json")
        if os.path.exists(path):
            return
        data = {
            "cache_format": CACHE_FORMAT_VERSION,
            "source_path": self.source_path,
            "source_size": os.path.getsize(self.source_path),
            "source_mtime_ns": os.stat(self.source_path).st_mtime_ns,
            "width": self.width,
            "height": self.height,
            "tile_size": self.tile_size,
        }
        temp = path + f".{os.getpid()}.tmp"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.write("\n")
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.remove(temp)

    def _level_size(self, factor: int) -> tuple[int, int]:
        return (
            (self.width + factor - 1) // factor,
            (self.height + factor - 1) // factor,
        )

    def _tile_box(self, factor: int, tx: int, ty: int) -> Box:
        span = self.tile_size * factor
        return Box(
            tx * span,
            ty * span,
            min(self.width, (tx + 1) * span),
            min(self.height, (ty + 1) * span),
        )

    def _expected_shape(self, factor: int, tx: int, ty: int) -> tuple[int, int]:
        box = self._tile_box(factor, tx, ty)
        return (
            (box.height + factor - 1) // factor,
            (box.width + factor - 1) // factor,
        )

    def _tile_path(self, mode: str, factor: int, tx: int, ty: int) -> str | None:
        # Full-resolution tiles remain memory-only: persisting them would largely
        # duplicate the source corpus and provides little zooming benefit.
        if not self.cache_dir or factor <= 1:
            return None
        return os.path.join(
            self.cache_dir,
            mode,
            f"level_{factor}",
            f"x{tx:05d}_y{ty:07d}.png",
        )

    def _remember(self, key: tuple[str, int, int, int], tile: np.ndarray) -> np.ndarray:
        self._memory[key] = tile
        self._memory.move_to_end(key)
        while len(self._memory) > self.memory_limit:
            self._memory.popitem(last=False)
        return tile

    def _load_disk(self, path: str, expected: tuple[int, int]) -> np.ndarray | None:
        try:
            with Image.open(path) as image:
                image.load()
                array = np.asarray(image.convert("L"), dtype=np.uint8)
            if array.shape == expected:
                return array
        except (OSError, ValueError):
            return None
        return None

    def _save_disk(self, path: str, tile: np.ndarray) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + f".{os.getpid()}.tmp"
        try:
            Image.fromarray(tile, mode="L").save(temp, format="PNG", compress_level=1)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.remove(temp)

    def get_tile(self, factor: int, tx: int, ty: int, mode: str) -> np.ndarray:
        factor = max(1, int(factor))
        key = (mode, factor, tx, ty)
        cached = self._memory.get(key)
        if cached is not None:
            self._memory.move_to_end(key)
            return cached

        level_w, level_h = self._level_size(factor)
        if tx < 0 or ty < 0 or tx * self.tile_size >= level_w or ty * self.tile_size >= level_h:
            return np.full((0, 0), 255, dtype=np.uint8)

        expected = self._expected_shape(factor, tx, ty)
        path = self._tile_path(mode, factor, tx, ty)
        if path and os.path.exists(path):
            tile = self._load_disk(path, expected)
            if tile is not None:
                return self._remember(key, tile)

        tile = self.reader(self._tile_box(factor, tx, ty), factor, mode)
        tile = np.asarray(tile, dtype=np.uint8)
        if tile.shape != expected:
            raise ValueError(f"tile reader returned {tile.shape}; expected {expected}")
        if path:
            self._save_disk(path, tile)
        return self._remember(key, tile)

    def read(self, box: Box, factor: int, mode: str) -> np.ndarray:
        """Return all pyramid cells intersecting a native-pixel source box."""
        box = box.clip(self.width, self.height)
        if box.is_empty:
            return np.full((0, 0), 255, dtype=np.uint8)
        factor = max(1, int(factor))
        lx0, ly0 = box.x0 // factor, box.y0 // factor
        lx1 = (box.x1 + factor - 1) // factor
        ly1 = (box.y1 + factor - 1) // factor
        out = np.full((ly1 - ly0, lx1 - lx0), 255, dtype=np.uint8)

        tx0, ty0 = lx0 // self.tile_size, ly0 // self.tile_size
        tx1 = (lx1 - 1) // self.tile_size
        ty1 = (ly1 - 1) // self.tile_size
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                tile = self.get_tile(factor, tx, ty, mode)
                gx, gy = tx * self.tile_size, ty * self.tile_size
                ix0, iy0 = max(lx0, gx), max(ly0, gy)
                ix1 = min(lx1, gx + tile.shape[1])
                iy1 = min(ly1, gy + tile.shape[0])
                if ix1 <= ix0 or iy1 <= iy0:
                    continue
                out[iy0 - ly0:iy1 - ly0, ix0 - lx0:ix1 - lx0] = tile[
                    iy0 - gy:iy1 - gy, ix0 - gx:ix1 - gx
                ]
        return out

    def prefetch_adjacent(
        self,
        box: Box,
        factor: int,
        mode: str,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Populate the tile rows immediately above and below ``box``."""
        box = box.clip(self.width, self.height)
        if box.is_empty:
            return 0
        factor = max(1, int(factor))
        lx0, ly0 = box.x0 // factor, box.y0 // factor
        lx1 = (box.x1 + factor - 1) // factor
        ly1 = (box.y1 + factor - 1) // factor
        tx0, tx1 = lx0 // self.tile_size, (lx1 - 1) // self.tile_size
        ty0, ty1 = ly0 // self.tile_size, (ly1 - 1) // self.tile_size
        _, level_h = self._level_size(factor)
        max_ty = max(0, (level_h - 1) // self.tile_size)

        made = 0
        for ty in (ty0 - 1, ty1 + 1):
            if ty < 0 or ty > max_ty:
                continue
            for tx in range(tx0, tx1 + 1):
                if should_stop and should_stop():
                    return made
                key = (mode, factor, tx, ty)
                path = self._tile_path(mode, factor, tx, ty)
                already = key in self._memory or bool(path and os.path.exists(path))
                self.get_tile(factor, tx, ty, mode)
                made += int(not already)
        return made
