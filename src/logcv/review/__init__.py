"""Hand review of the scanned logs: an image explorer that records a verdict.

The automated detector (`logcv detect`) answers "is there a stamp?" by geometry.
This package answers the same question the other way round -- a person looks at
the page and says so -- and writes the answer to an Excel workbook that
downstream work can join on. The two are meant to be compared.

Four modules, with narrow responsibilities:

* `pages`  -- open any page and compose a viewport at any zoom.
* `pyramid` -- persistent reduced-resolution tiles plus the in-memory tile LRU.
* `store`  -- the workbook: one row per file, load-resume-save, plus the
              extensible list of stamp types.
* `app`    -- the Tk window that wires them to a keyboard.
"""
from __future__ import annotations

from .store import (
    InvalidWorkbook,
    Record,
    ReviewStore,
    WorkbookInspection,
    join_values,
    load_log_types,
    load_stamp_types,
    save_stamp_types,
    split_values,
    validate_review_workbook,
)

__all__ = ["InvalidWorkbook", "Record", "ReviewStore", "WorkbookInspection", "join_values",
           "load_log_types", "load_stamp_types", "save_stamp_types",
           "split_values", "validate_review_workbook", "run", "selftest"]


def run(folder: str | None = None, workbook: str | None = None,
        cache_dir: str | None = None) -> int:
    """Launch the review window. Imported lazily so `logcv detect` needs no Tk."""
    from .app import run as _run

    return _run(folder=folder, workbook=workbook, cache_dir=cache_dir)


def selftest(folder: str | None = None) -> int:
    """Drive the app once, headlessly, and report. Used to verify a packaged build."""
    from .app import selftest as _selftest

    return _selftest(folder=folder)
