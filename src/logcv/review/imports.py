"""Flexible identifier-list/manifest import for LogReview.

The review UI deliberately delegates parsing and path resolution to this module
so the behavior can be tested without Tk.  Source files are never modified.
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from openpyxl import load_workbook

from . import pages


IDENTIFIER_ALIASES = {
    "api", "api14", "apinumber", "api_number", "uwi", "wellid", "well_id",
    "wellapi", "well_api", "logapi", "log_api",
}
PATH_ALIASES = {
    "tifpath", "tif_path", "tiffpath", "tiff_path", "filepath", "file_path",
    "imagepath", "image_path", "logpath", "log_path",
}
PLACEHOLDERS = {"ihs assoc image"}
AUDIT_COLUMNS = [
    "import_row", "source_identifier", "match_identifier", "source_tif_path",
    "resolved_tif_path", "well_loaded", "row_image_loaded", "load_status",
    "resolution_source", "detail",
]


class ManifestError(ValueError):
    """A selected source cannot be interpreted safely."""


class ColumnSelectionRequired(ManifestError):
    """The UI must ask which columns carry identifiers and paths."""

    def __init__(self, table: "SourceTable", id_candidates: list[str],
                 path_candidates: list[str]):
        super().__init__("Select the identifier and optional TIFF-path columns.")
        self.table = table
        self.id_candidates = id_candidates
        self.path_candidates = path_candidates


@dataclass(frozen=True)
class SourceTable:
    source_path: str
    headers: tuple[str, ...]
    rows: tuple[OrderedDict[str, str], ...]
    identifier_column: str | None = None
    path_column: str | None = None

    @property
    def has_paths(self) -> bool:
        return bool(self.path_column)


@dataclass
class AuditRow:
    original: OrderedDict[str, str]
    import_row: int
    source_identifier: str
    match_identifier: str
    source_tif_path: str
    resolved_tif_path: str = ""
    well_loaded: bool = False
    row_image_loaded: bool = False
    load_status: str = ""
    resolution_source: str = ""
    detail: str = ""

    def as_dict(self) -> OrderedDict[str, object]:
        out: OrderedDict[str, object] = OrderedDict(self.original)
        for name in AUDIT_COLUMNS:
            out[name] = getattr(self, name)
        return out


@dataclass
class ImportStats:
    input_rows: int = 0
    valid_unique_identifiers: int = 0
    invalid_unique_identifiers: int = 0
    unique_tiff_candidates: int = 0
    loaded_tiffs: int = 0
    loaded_from_folder: int = 0
    recovered_from_paths: int = 0
    duplicate_references: int = 0
    loaded_identifiers: int = 0
    partially_loaded_identifiers: int = 0
    unrecovered_identifiers: int = 0
    placeholder_rows: int = 0
    placeholder_covered: int = 0
    placeholder_unresolved: int = 0

    def summary(self) -> str:
        return (
            f"Input rows: {self.input_rows}\n"
            f"Valid unique identifiers: {self.valid_unique_identifiers}\n"
            f"Invalid unique identifiers: {self.invalid_unique_identifiers}\n"
            f"Unique TIFF candidates: {self.unique_tiff_candidates}\n"
            f"Successfully loaded TIFFs: {self.loaded_tiffs}\n"
            f"Loaded from selected folder: {self.loaded_from_folder}\n"
            f"Recovered from listed paths: {self.recovered_from_paths}\n"
            f"Duplicate references skipped: {self.duplicate_references}\n"
            f"Unique identifiers loaded: {self.loaded_identifiers}\n"
            f"Partially loaded identifiers: {self.partially_loaded_identifiers}\n"
            f"Identifiers not recovered: {self.unrecovered_identifiers}\n"
            f"IHS placeholder rows: {self.placeholder_rows} "
            f"({self.placeholder_covered} covered, "
            f"{self.placeholder_unresolved} unresolved)"
        )


@dataclass
class ImportResult:
    source: SourceTable
    paths: list[str]
    path_identifiers: dict[str, str]
    loaded_identifiers: list[str]
    audit_rows: list[AuditRow]
    stats: ImportStats


@dataclass
class _LogicalCandidate:
    identifier: str
    basename: str
    row_indices: list[int] = field(default_factory=list)
    listed_paths: list[str] = field(default_factory=list)
    folder_path: str = ""
    resolved_path: str = ""
    resolution_source: str = ""
    failure_status: str = "path_missing"
    failure_detail: str = ""


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).strip().lower())


def _alias_keys(values: Iterable[str]) -> set[str]:
    return {_header_key(value) for value in values}


_ID_KEYS = _alias_keys(IDENTIFIER_ALIASES)
_PATH_KEYS = _alias_keys(PATH_ALIASES)


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _unique_headers(values: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    used: set[str] = set()
    for index, value in enumerate(values, start=1):
        base = _cell_text(value) or f"column_{index}"
        name = base
        suffix = 2
        while name.casefold() in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name.casefold())
        out.append(name)
    return tuple(out)


def _read_text_list(path: str) -> SourceTable:
    with open(path, encoding="utf-8-sig") as handle:
        values = [line.strip() for line in handle.read().splitlines() if line.strip()]
    rows = tuple(OrderedDict([("input_value", value)]) for value in values)
    return SourceTable(os.path.abspath(path), ("input_value",), rows,
                       identifier_column="input_value")


def _read_delimited(path: str) -> SourceTable:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(65536)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel_tab if Path(path).suffix.lower() == ".tsv" else csv.excel
        reader = csv.reader(handle, dialect)
        try:
            header_values = next(reader)
        except StopIteration as exc:
            raise ManifestError("The selected file is empty.") from exc
        headers = _unique_headers(header_values)
        rows: list[OrderedDict[str, str]] = []
        for values in reader:
            padded = list(values) + [""] * max(0, len(headers) - len(values))
            row = OrderedDict((name, _cell_text(padded[i]))
                              for i, name in enumerate(headers))
            if any(row.values()):
                rows.append(row)
    return SourceTable(os.path.abspath(path), headers, tuple(rows))


def _read_xlsx(path: str) -> SourceTable:
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        fallback: SourceTable | None = None
        for sheet in book.worksheets:
            values = sheet.iter_rows(values_only=True)
            try:
                header_values = next(values)
            except StopIteration:
                continue
            headers = _unique_headers(header_values)
            rows: list[OrderedDict[str, str]] = []
            for cells in values:
                padded = list(cells) + [None] * max(0, len(headers) - len(cells))
                row = OrderedDict((name, _cell_text(padded[i]))
                                  for i, name in enumerate(headers))
                if any(row.values()):
                    rows.append(row)
            table = SourceTable(os.path.abspath(path), headers, tuple(rows))
            if fallback is None:
                fallback = table
            if any(_header_key(h) in _ID_KEYS for h in headers):
                return table
        if fallback is not None:
            return fallback
    finally:
        book.close()
    raise ManifestError("The selected workbook contains no non-empty worksheets.")


def read_source(path: str) -> SourceTable:
    """Read a supported source without interpreting its columns."""
    suffix = Path(path).suffix.lower()
    try:
        if suffix == ".txt":
            return _read_text_list(path)
        if suffix in {".csv", ".tsv"}:
            return _read_delimited(path)
        if suffix == ".xlsx":
            return _read_xlsx(path)
    except (OSError, UnicodeError) as exc:
        raise ManifestError(str(exc)) from exc
    raise ManifestError("Supported list formats are .txt, .csv, .tsv, and .xlsx.")


def select_columns(table: SourceTable, identifier_column: str | None = None,
                   path_column: str | None = None) -> SourceTable:
    """Attach selected columns, auto-detecting unambiguous aliases."""
    headers = list(table.headers)
    id_candidates = [h for h in headers if _header_key(h) in _ID_KEYS]
    path_candidates = [h for h in headers if _header_key(h) in _PATH_KEYS]
    if identifier_column is None:
        if table.identifier_column:
            identifier_column = table.identifier_column
        elif len(id_candidates) == 1:
            identifier_column = id_candidates[0]
    if path_column is None and len(path_candidates) == 1:
        path_column = path_candidates[0]
    needs_id = identifier_column not in headers
    ambiguous_path = len(path_candidates) > 1 and path_column not in headers
    if needs_id or ambiguous_path:
        raise ColumnSelectionRequired(table, id_candidates, path_candidates)
    if path_column and path_column not in headers:
        raise ManifestError(f"Unknown TIFF-path column: {path_column}")
    return SourceTable(table.source_path, table.headers, table.rows,
                       identifier_column, path_column)


def load_manifest(path: str, identifier_column: str | None = None,
                  path_column: str | None = None) -> SourceTable:
    return select_columns(read_source(path), identifier_column, path_column)


def normalize_identifier(value: str) -> tuple[str, tuple[str, ...]]:
    """Return stored identifier and filename-match variants."""
    identifier = str(value or "").strip()
    if not identifier.isdigit() or not 1 <= len(identifier) <= 14:
        return "", ()
    variants = [identifier]
    if len(identifier) == 12:
        variants.append(identifier + "00")
    return identifier, tuple(variants)


def _name_matches_identifier(name: str, variants: Iterable[str]) -> bool:
    lower = os.path.basename(name).casefold()
    for value in variants:
        if lower.startswith(value.casefold()):
            if len(lower) == len(value) or not lower[len(value)].isdigit():
                return True
    return False


def _probe(path: str, probe_fn: Callable[[str], object]) -> tuple[bool, str, str]:
    if not path.lower().endswith(pages.IMAGE_EXTS):
        return False, "unsupported_image_path", "Unsupported image extension."
    if not os.path.isfile(path):
        return False, "path_missing", "File does not exist or is not accessible."
    try:
        probe_fn(path)
    except Exception as exc:
        return False, "unreadable_image", str(exc)
    return True, "", ""


def resolve_manifest(table: SourceTable, folder_images: Iterable[str] = (),
                     probe_fn: Callable[[str], object] = pages.probe) -> ImportResult:
    """Resolve a manifest into readable logical images and row-level audit data."""
    if not table.identifier_column:
        raise ManifestError("No identifier column was selected.")
    folder_paths = [os.path.abspath(path) for path in folder_images]
    folder_by_name = {os.path.basename(path).casefold(): path for path in folder_paths}
    audit: list[AuditRow] = []
    valid_order: list[str] = []
    variants_by_id: dict[str, tuple[str, ...]] = {}
    invalid_values: set[str] = set()
    rows_by_id: dict[str, list[int]] = defaultdict(list)

    for row_number, original in enumerate(table.rows, start=2):
        raw_id = original.get(table.identifier_column, "")
        identifier, variants = normalize_identifier(raw_id)
        raw_path = original.get(table.path_column, "") if table.path_column else ""
        item = AuditRow(OrderedDict(original), row_number, raw_id, variants[-1] if variants else "",
                        raw_path)
        audit.append(item)
        if not identifier:
            item.load_status = "invalid_identifier"
            item.detail = "Identifier must contain 1 to 14 digits."
            invalid_values.add(str(raw_id).strip())
            continue
        if identifier not in variants_by_id:
            valid_order.append(identifier)
            variants_by_id[identifier] = variants
        rows_by_id[identifier].append(len(audit) - 1)

    candidates: OrderedDict[tuple[str, str], _LogicalCandidate] = OrderedDict()
    placeholder_indices: list[int] = []
    duplicate_references = 0

    for identifier in valid_order:
        variants = variants_by_id[identifier]
        for folder_path in folder_paths:
            if _name_matches_identifier(folder_path, variants):
                basename = os.path.basename(folder_path).casefold()
                key = (identifier, basename)
                candidates.setdefault(key, _LogicalCandidate(identifier, basename))
                candidates[key].folder_path = folder_path

    seen_row_refs: set[tuple[str, str, str]] = set()
    source_dir = os.path.dirname(os.path.abspath(table.source_path))

    def listed_path(value: str) -> str:
        return value if os.path.isabs(value) else os.path.join(source_dir, value)

    for identifier, indices in rows_by_id.items():
        for index in indices:
            item = audit[index]
            raw_path = item.source_tif_path.strip()
            if raw_path.casefold() in PLACEHOLDERS:
                placeholder_indices.append(index)
                continue
            if not raw_path:
                continue
            basename = os.path.basename(raw_path.replace("/", os.sep)).casefold()
            if not basename:
                continue
            key = (identifier, basename)
            candidate = candidates.setdefault(
                key, _LogicalCandidate(identifier, basename)
            )
            candidate.row_indices.append(index)
            resolved_listing = listed_path(raw_path)
            ref_key = (identifier, basename,
                       os.path.normcase(os.path.abspath(resolved_listing)))
            if ref_key in seen_row_refs:
                duplicate_references += 1
            else:
                seen_row_refs.add(ref_key)
                if candidate.listed_paths:
                    duplicate_references += 1
                candidate.listed_paths.append(resolved_listing)
            if basename in folder_by_name:
                candidate.folder_path = folder_by_name[basename]

    basename_owner: dict[str, str] = {}
    conflict_keys: set[tuple[str, str]] = set()
    for key, candidate in candidates.items():
        owner = basename_owner.setdefault(candidate.basename, candidate.identifier)
        if owner != candidate.identifier:
            conflict_keys.add(key)

    probe_cache: dict[str, tuple[bool, str, str]] = {}

    def checked(path: str) -> tuple[bool, str, str]:
        key = os.path.normcase(os.path.abspath(path))
        if key not in probe_cache:
            probe_cache[key] = _probe(path, probe_fn)
        return probe_cache[key]

    failed_by_id: dict[str, int] = defaultdict(int)
    resolved_candidates: list[_LogicalCandidate] = []
    for key, candidate in candidates.items():
        if key in conflict_keys:
            candidate.failure_status = "basename_conflict"
            candidate.failure_detail = (
                f"The same TIFF basename is already assigned to identifier "
                f"{basename_owner[candidate.basename]}."
            )
            failed_by_id[candidate.identifier] += 1
            continue
        choices: list[tuple[str, str]] = []
        if candidate.folder_path:
            choices.append((candidate.folder_path, "selected_folder"))
        for path in candidate.listed_paths:
            if not any(os.path.normcase(os.path.abspath(path)) ==
                       os.path.normcase(os.path.abspath(existing))
                       for existing, _ in choices):
                choices.append((path, "listed_path"))
        failures: list[tuple[str, str]] = []
        for path, source in choices:
            ok, status, detail = checked(path)
            if ok:
                candidate.resolved_path = os.path.abspath(path)
                candidate.resolution_source = source
                resolved_candidates.append(candidate)
                break
            failures.append((status, detail))
        if not candidate.resolved_path:
            failed_by_id[candidate.identifier] += 1
            if failures:
                candidate.failure_status, candidate.failure_detail = failures[-1]
            elif candidate.listed_paths:
                candidate.failure_status = "path_missing"
                candidate.failure_detail = "No listed path was accessible."
            else:
                candidate.failure_status = "no_path"
                candidate.failure_detail = "No TIFF path was supplied or matched."

    resolved_by_id: dict[str, list[_LogicalCandidate]] = defaultdict(list)
    candidate_by_key = {(c.identifier, c.basename): c for c in candidates.values()}
    for candidate in resolved_candidates:
        resolved_by_id[candidate.identifier].append(candidate)

    for identifier, indices in rows_by_id.items():
        loaded = bool(resolved_by_id.get(identifier))
        for index in indices:
            item = audit[index]
            item.well_loaded = loaded
            raw_path = item.source_tif_path.strip()
            if raw_path.casefold() in PLACEHOLDERS:
                item.load_status = "placeholder_covered" if loaded else "placeholder_unresolved"
                item.detail = (
                    "Another TIFF loaded for this identifier."
                    if loaded else "No TIFF was available for this identifier."
                )
                continue
            if not raw_path:
                if loaded:
                    resolved = resolved_by_id[identifier]
                    item.resolved_tif_path = "; ".join(
                        candidate.resolved_path for candidate in resolved
                    )
                    item.row_image_loaded = True
                    item.resolution_source = "selected_folder"
                    item.load_status = "loaded_from_selected_folder"
                    item.detail = (
                        f"Matched {len(resolved)} image(s) by identifier in the "
                        "selected image collection."
                    )
                else:
                    item.load_status = "no_path_unresolved"
                    item.detail = "No TIFF path was supplied or matched."
                continue
            basename = os.path.basename(raw_path.replace("/", os.sep)).casefold()
            candidate = candidate_by_key.get((identifier, basename))
            if candidate and candidate.resolved_path:
                item.resolved_tif_path = candidate.resolved_path
                item.row_image_loaded = True
                item.resolution_source = candidate.resolution_source
                if candidate.resolution_source == "selected_folder":
                    item.load_status = "loaded_from_selected_folder"
                elif os.path.normcase(os.path.abspath(listed_path(raw_path))) == os.path.normcase(
                        candidate.resolved_path):
                    item.load_status = "recovered_from_listed_path"
                else:
                    item.load_status = "duplicate_reference_loaded"
                continue
            status = candidate.failure_status if candidate else "path_missing"
            detail = candidate.failure_detail if candidate else "No TIFF candidate was resolved."
            item.load_status = status + ("_well_covered" if loaded else "_unresolved")
            item.detail = detail

    loaded_ids = [identifier for identifier in valid_order if resolved_by_id.get(identifier)]
    ordered_paths: list[str] = []
    path_identifiers: dict[str, str] = {}
    for identifier in valid_order:
        for candidate in resolved_by_id.get(identifier, []):
            ordered_paths.append(candidate.resolved_path)
            path_identifiers[os.path.normcase(os.path.abspath(candidate.resolved_path))] = identifier

    placeholder_covered = sum(
        1 for index in placeholder_indices if audit[index].load_status == "placeholder_covered"
    )
    stats = ImportStats(
        input_rows=len(audit),
        valid_unique_identifiers=len(valid_order),
        invalid_unique_identifiers=len(invalid_values),
        unique_tiff_candidates=len(candidates),
        loaded_tiffs=len(resolved_candidates),
        loaded_from_folder=sum(c.resolution_source == "selected_folder"
                               for c in resolved_candidates),
        recovered_from_paths=sum(c.resolution_source == "listed_path"
                                 for c in resolved_candidates),
        duplicate_references=duplicate_references,
        loaded_identifiers=len(loaded_ids),
        partially_loaded_identifiers=sum(
            bool(resolved_by_id.get(identifier)) and failed_by_id[identifier] > 0
            for identifier in valid_order
        ),
        unrecovered_identifiers=sum(not resolved_by_id.get(identifier)
                                    for identifier in valid_order),
        placeholder_rows=len(placeholder_indices),
        placeholder_covered=placeholder_covered,
        placeholder_unresolved=len(placeholder_indices) - placeholder_covered,
    )
    return ImportResult(table, ordered_paths, path_identifiers, loaded_ids, audit, stats)


def audit_report_path(source_path: str, workbook_path: str,
                      now: _dt.datetime | None = None) -> str:
    stamp = (now or _dt.datetime.now()).strftime("%Y%m%d_%H%M%S")
    stem = Path(source_path).stem or "image_list"
    return os.path.join(os.path.dirname(os.path.abspath(workbook_path)),
                        f"{stem}_load_status_{stamp}.csv")


def write_audit_csv(path: str, result: ImportResult) -> str:
    """Write a source-preserving row-level load report."""
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    headers = list(result.source.headers) + AUDIT_COLUMNS
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in result.audit_rows:
            writer.writerow(row.as_dict())
    return os.path.abspath(path)
