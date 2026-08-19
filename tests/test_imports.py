"""Manifest parsing, TIFF recovery, and row-level audit tests."""
from __future__ import annotations

import csv
import os
from collections import OrderedDict

import pytest
from openpyxl import Workbook

from logcv.review.imports import (
    ColumnSelectionRequired,
    audit_report_path,
    load_manifest,
    normalize_identifier,
    read_source,
    resolve_manifest,
    select_columns,
    write_audit_csv,
)


def _touch(path, value=b"test"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return str(path)


def _ok_probe(path):
    return os.path.getsize(path)


def test_headerless_text_preserves_short_and_14_digit_identifiers(tmp_path):
    source = tmp_path / "apis.txt"
    source.write_text("21441\n\n22536\n42175000720000\n", encoding="utf-8-sig")
    table = load_manifest(str(source))
    assert table.identifier_column == "input_value"
    assert table.path_column is None
    assert [row["input_value"] for row in table.rows] == [
        "21441", "22536", "42175000720000",
    ]


def test_csv_and_tsv_aliases_and_quoted_unc_paths_are_detected(tmp_path):
    csv_path = tmp_path / "manifest.csv"
    csv_path.write_text(
        'UWI,TIFPATH,COMMENT\n"21441","\\\\server\\logs\\21441_RES.tif","x"\n',
        encoding="utf-8-sig",
    )
    table = load_manifest(str(csv_path))
    assert (table.identifier_column, table.path_column) == ("UWI", "TIFPATH")
    assert table.rows[0]["TIFPATH"] == r"\\server\logs\21441_RES.tif"

    tsv_path = tmp_path / "manifest.tsv"
    tsv_path.write_text("well_id\timage_path\n22536\tC:\\logs\\22536.tif\n",
                        encoding="utf-8")
    table = load_manifest(str(tsv_path))
    assert (table.identifier_column, table.path_column) == ("well_id", "image_path")


def test_xlsx_uses_first_sheet_with_identifier_columns_and_formats_numbers(tmp_path):
    path = tmp_path / "manifest.xlsx"
    book = Workbook()
    book.active.title = "notes"
    book.active.append(["description"])
    book.active.append(["not the data"])
    data = book.create_sheet("wells")
    data.append(["API Number", "TIFF Path"])
    data.append([21441, r"C:\logs\21441.tif"])
    book.save(path)
    table = load_manifest(str(path))
    assert table.rows[0]["API Number"] == "21441"
    assert table.identifier_column == "API Number"
    assert table.path_column == "TIFF Path"


def test_ambiguous_columns_require_explicit_mapping(tmp_path):
    path = tmp_path / "ambiguous.csv"
    path.write_text("API,UWI,TIFPATH,FILE_PATH\n1,2,a.tif,b.tif\n", encoding="utf-8")
    with pytest.raises(ColumnSelectionRequired) as caught:
        load_manifest(str(path))
    table = select_columns(caught.value.table, "UWI", "FILE_PATH")
    assert (table.identifier_column, table.path_column) == ("UWI", "FILE_PATH")


def test_identifier_matching_preserves_source_and_adds_only_12_digit_variant():
    assert normalize_identifier("21441") == ("21441", ("21441",))
    assert normalize_identifier("424693343100") == (
        "424693343100", ("424693343100", "42469334310000")
    )
    assert normalize_identifier("42175000720000") == (
        "42175000720000", ("42175000720000",)
    )
    assert normalize_identifier("ABC") == ("", ())


def test_resolution_prefers_folder_then_recovers_external_and_loads_all_tiffs(tmp_path):
    local = tmp_path / "local"
    external = tmp_path / "external"
    local_copy = _touch(local / "21441_RES.tif")
    listed_copy = _touch(external / "21441_RES.tif")
    second = _touch(external / "21441_GR.tif")
    source = tmp_path / "manifest.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["UWI", "TIFPATH"])
        writer.writerow(["21441", listed_copy])
        writer.writerow(["21441", second])
        writer.writerow(["21441", listed_copy])
        writer.writerow(["21441", "IHS ASSOC IMAGE"])
    result = resolve_manifest(load_manifest(str(source)), [local_copy], _ok_probe)
    assert result.loaded_identifiers == ["21441"]
    assert result.paths == [os.path.abspath(local_copy), os.path.abspath(second)]
    assert result.stats.loaded_tiffs == 2
    assert result.stats.loaded_from_folder == 1
    assert result.stats.recovered_from_paths == 1
    assert result.stats.duplicate_references == 1
    assert result.stats.placeholder_covered == 1
    statuses = [row.load_status for row in result.audit_rows]
    assert statuses == [
        "loaded_from_selected_folder", "recovered_from_listed_path",
        "loaded_from_selected_folder", "placeholder_covered",
    ]


def test_relative_listed_paths_resolve_from_manifest_directory(tmp_path):
    tif = _touch(tmp_path / "images" / "21441.tif")
    source = tmp_path / "manifest.csv"
    source.write_text("UWI,TIFPATH\n21441,images/21441.tif\n", encoding="utf-8")
    result = resolve_manifest(load_manifest(str(source)), probe_fn=_ok_probe)
    assert result.paths == [os.path.abspath(tif)]
    assert result.audit_rows[0].source_tif_path == "images/21441.tif"
    assert result.audit_rows[0].load_status == "recovered_from_listed_path"


def test_resolution_marks_missing_unreadable_and_placeholder_only_ids(tmp_path):
    unreadable = _touch(tmp_path / "bad.tif")
    source = tmp_path / "manifest.csv"
    source.write_text(
        f"UWI,TIFPATH\n1,{tmp_path / 'missing.tif'}\n2,{unreadable}\n"
        "3,IHS ASSOC IMAGE\n4,not-an-image\n",
        encoding="utf-8",
    )

    def probe(path):
        if path == unreadable:
            raise ValueError("broken TIFF")
        return True

    result = resolve_manifest(load_manifest(str(source)), probe_fn=probe)
    assert result.paths == []
    assert [row.load_status for row in result.audit_rows] == [
        "path_missing_unresolved", "unreadable_image_unresolved",
        "placeholder_unresolved", "unsupported_image_path_unresolved",
    ]
    assert result.stats.unrecovered_identifiers == 4


def test_folder_matching_supports_12_digit_ids_without_changing_stored_value(tmp_path):
    tif = _touch(tmp_path / "42469334310000_log.tif")
    source = tmp_path / "ids.txt"
    source.write_text("424693343100\n", encoding="utf-8")
    result = resolve_manifest(load_manifest(str(source)), [tif], _ok_probe)
    assert result.loaded_identifiers == ["424693343100"]
    assert result.path_identifiers[os.path.normcase(os.path.abspath(tif))] == "424693343100"
    assert result.audit_rows[0].load_status == "loaded_from_selected_folder"


def test_conflicting_basename_for_different_identifiers_is_reported(tmp_path):
    first = tmp_path / "one" / "same.tif"
    second = tmp_path / "two" / "same.tif"
    _touch(first)
    _touch(second)
    source = tmp_path / "manifest.csv"
    source.write_text(f"UWI,TIFPATH\n1,{first}\n2,{second}\n", encoding="utf-8")
    result = resolve_manifest(load_manifest(str(source)), probe_fn=_ok_probe)
    assert result.loaded_identifiers == ["1"]
    assert result.audit_rows[1].load_status == "basename_conflict_unresolved"


def test_audit_csv_preserves_source_columns_and_appends_status(tmp_path):
    tif = _touch(tmp_path / "21441.tif")
    source = tmp_path / "manifest.csv"
    source.write_text(f"UWI,TIFPATH,COMMENT\n21441,{tif},source note\n", encoding="utf-8")
    result = resolve_manifest(load_manifest(str(source)), probe_fn=_ok_probe)
    report = audit_report_path(str(source), str(tmp_path / "review.xlsx"))
    write_audit_csv(report, result)
    with open(report, encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["COMMENT"] == "source note"
    assert row["source_identifier"] == "21441"
    assert row["well_loaded"] == "True"
    assert row["load_status"] == "recovered_from_listed_path"


def test_supplied_raster_summary_shape_is_stable():
    source = os.path.join(os.path.dirname(__file__), "..", "data", "raw",
                          "RasterSummary_464_Model_Wells.CSV")
    if not os.path.exists(source):
        pytest.skip("project fixture not present")
    table = load_manifest(source)
    identifiers = {row[table.identifier_column] for row in table.rows}
    placeholders = sum(
        row[table.path_column].strip().casefold() == "ihs assoc image"
        for row in table.rows
    )
    lengths = [len(value) for value in identifiers]
    assert len(table.rows) == 4081
    assert len(identifiers) == 405
    assert lengths.count(5) == 2
    assert lengths.count(12) == 19
    assert lengths.count(14) == 384
    assert placeholders == 460
