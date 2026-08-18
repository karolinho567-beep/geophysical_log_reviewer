"""Canonical command-line entry point for the LogReview desktop product.

Both the installed ``log-review`` command and the frozen Windows executable
enter here. Keeping argument parsing and frozen-runtime setup in the package
means the Windows packaging component contains no application behavior.
"""
from __future__ import annotations

import argparse
import os
import sys


def _running_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _prepare_gdal() -> None:
    """Point GDAL at support data bundled by PyInstaller, when present."""
    if not _running_frozen():
        return
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    for variable, folder in (
        ("GDAL_DATA", os.path.join(base, "osgeo", "data", "gdal")),
        ("PROJ_LIB", os.path.join(base, "osgeo", "data", "proj")),
    ):
        if os.path.isdir(folder):
            os.environ.setdefault(variable, folder)


class _Tee:
    """Mirror self-test output to a report when a windowed build has no console."""

    def __init__(self, path: str):
        self._handle = open(path, "w", encoding="utf-8")
        self._stdout = sys.stdout

    def write(self, value: str) -> int:
        self._handle.write(value)
        self._handle.flush()
        if self._stdout is not None:
            try:
                self._stdout.write(value)
            except Exception:
                pass
        return len(value)

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def execute(
    folder: str | None = None,
    workbook: str | None = None,
    cache_dir: str | None = None,
    selftest_mode: bool = False,
    report: str | None = None,
) -> int:
    """Execute one review command after its arguments have been parsed."""
    _prepare_gdal()

    from . import run, selftest

    if not selftest_mode:
        return run(folder=folder, workbook=workbook, cache_dir=cache_dir)

    tee = _Tee(report) if report else None
    if tee is not None:
        sys.stdout = tee
    try:
        return selftest(folder=folder)
    finally:
        if tee is not None:
            sys.stdout = tee._stdout
            tee.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="LogReview" if _running_frozen() else "log-review",
        description="Review scanned geophysical logs and record structured verdicts.",
    )
    parser.add_argument("--in", dest="input", default=None,
                        help="folder of scanned logs (a folder chooser opens if omitted)")
    parser.add_argument("--xlsx", default=None,
                        help="review workbook (default: ./reviews/<folder>_stamp_review.xlsx)")
    parser.add_argument("--cache", default=None,
                        help="pyramid cache directory (default: ./cache beside the app)")
    parser.add_argument("--selftest", action="store_true",
                        help="drive the app once and exit; 0 means healthy")
    parser.add_argument("--report", default=None,
                        help="with --selftest, write the check list to this file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return execute(
        folder=args.input,
        workbook=args.xlsx,
        cache_dir=args.cache,
        selftest_mode=args.selftest,
        report=args.report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
