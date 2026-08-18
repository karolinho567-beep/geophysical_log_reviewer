"""Name -> Detector lookup, so the CLI can take ``--detectors date_stamp,header_block``."""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .base import Detector

_REGISTRY: dict[str, type[Detector]] = {}


def register(name: str) -> Callable[[type[Detector]], type[Detector]]:
    """Class decorator that makes a detector addressable by name."""

    def wrap(cls: type[Detector]) -> type[Detector]:
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise ValueError(f"detector {name!r} already registered")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return wrap


def available() -> list[str]:
    _load_builtins()
    return sorted(_REGISTRY)


def build(name: str, **config: Any) -> Detector:
    _load_builtins()
    if name not in _REGISTRY:
        raise KeyError(f"unknown detector {name!r}; available: {', '.join(available())}")
    return _REGISTRY[name](**config)


def build_many(names: Iterable[str], config: dict[str, dict] | None = None) -> list[Detector]:
    config = config or {}
    return [build(n, **config.get(n, {})) for n in names]


def _load_builtins() -> None:
    """Import the shipped detectors so their decorators run."""
    from . import date_stamp  # noqa: F401
