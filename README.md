# Geophysical Log Reviewer

LogReview is a reusable Windows desktop application for reviewing scanned
geophysical logs without modifying the source images. It renders very large TIFFs
through GDAL-backed tiled pyramids and records reviewer decisions in a resumable
Excel workbook.

Current version: **1.6.2**

## Features

- Opens original TIFFs read-only; no resized copies or sidecar overviews are
  written beside the logs.
- Fast zooming and scrolling through persistent reduced-resolution tile caches.
- Explicit draft-and-commit workflow for stamp verdicts, multiple stamp types,
  fixed log classifications, notes, timestamps, and reviewer attribution.
- Displays notes in the log list and supports a persistent API-based subset view
  without duplicating review answers.
- Reconciles an existing workbook with a changed TIFF folder while protecting the
  original workbook and preserving optional out-of-folder rows.
- Migrates compatible v1.2 and v1.3 workbooks to the current nine-column schema.
- Builds as a portable Windows x64 application; Python is not required on the
  reviewer's computer.
- Checks public GitHub Releases on every application start and can securely replace and restart
  the portable application after verifying GitHub's SHA-256 asset digest.

## Install from source

Python 3.12 and GDAL are required. A conda-forge environment is recommended on
Windows because it provides compatible GDAL binaries.

```powershell
conda create -n logreview -c conda-forge python=3.12 numpy gdal pillow openpyxl tk pytest
conda activate logreview
pip install -e . --no-deps
log-review
```

The application can also be launched with `python -m logcv.review`.

## Build the portable Windows application

```powershell
conda create -n logreview-build -c conda-forge python=3.12 "libblas=*=*openblas" numpy gdal pillow openpyxl tk
conda activate logreview-build
pip install pyinstaller
python packaging\windows\build_exe.py
```

The build script runs the frozen application's GUI self-test before creating a
versioned ZIP under `dist/windows/`.

## Testing

```powershell
pytest -q
python -m logcv.review --selftest --in <folder-containing-test-images>
```

## Privacy and repository hygiene

TIFFs, review workbooks, caches, GIS data, executables, ZIP archives, and local
environment files are intentionally excluded from Git. GitHub Releases should
contain the portable Windows ZIP; it should not be committed to the source tree.

See [the application guide](docs/LOG_REVIEW_APP.md) for the workflow, workbook
schema, compatibility behavior, and packaging details.
