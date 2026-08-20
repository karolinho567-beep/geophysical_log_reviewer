# Geophysical Log Reviewer — reusable review of scanned geophysical logs

**Status: reusable source application and standalone Windows build.** The product
is installed from `src/logcv/review`; platform packaging lives separately under
`packaging/windows`.

## Goal

Answer the same question `detect_date_stamp` answers automatically, but by eye:
for each scanned log, **does it carry a records date stamp, and what kind?**
The reviewer sees the page in an image explorer, builds a draft containing the
stamp verdict, one or more stamp types, optional log classifications and notes,
then explicitly adds the entry. Every entry lands in one Excel workbook keyed
to the file.

Two reasons to have this alongside the detector:

1. it is the ground truth the detector is scored against — a hand sweep of 70
   pages is the accepted basis for accepting v2 (see
   [`../tasks/detect_date_stamp/README.md`](../tasks/detect_date_stamp/README.md));
2. it generalises past date stamps: the type column means "which stamp is this",
   so a second or third stamp design gets recorded rather than argued about.

## Run

```powershell
conda activate victoria-cv-logs
pip install -e . --no-deps
log-review
```

Equivalently: `python -m logcv.review` or `python -m logcv review`. All forms accept
`--in <folder>`, `--xlsx <workbook>`, and `--cache <folder>`. If `--in` is omitted,
the app asks whether to open an image folder or a TXT/CSV/TSV/XLSX image list,
then offers **Open existing / Create new / Back**. Both workbook browsers start in
the portable application's `reviews/` directory, which is created automatically.
File-dialog cancellation returns
to those choices; backing out shows a welcome screen with separate folder and
image-list choices. After
the workbook is selected and reconciled, the app asks for the current reviewer.
Existing workbooks are accepted only when their `review` sheet, required columns,
and verdict values are valid; filename differences use the reconciliation flow
described below instead of producing an unexplained rejection.

The app also creates `cache/` beside the executable (or under the source launch
directory). This contains only disposable display tiles; source TIFFs are opened
read-only and are never modified.

The window opens **maximised**, and its top lines state what is being reviewed
and where the Excel answers are written. The cache remains app-local but is not
shown because it is an implementation detail:

```
Reviewing:        …\data\raw\Digitization_TIF_FINAL   (70 images)
Saving to Excel:  …\reviews\Digitization_TIF_FINAL_stamp_review.xlsx
                  [Change…] [Show in Explorer] [Save now (Ctrl+S)]
Reviewer:         KP [Change]                         saved 15:04:22 - 23 entries
```

The viewer controls include **Show TIFF in Explorer**, which opens Windows
Explorer with the unmodified source image highlighted.

`--selftest` drives the whole app once without a human — opens a page, renders
it, records a verdict, writes and re-reads the workbook — and exits 0 if healthy.
That is what verifies a packaged build.

### Standalone build (no Python on the reviewer's machine)

```powershell
conda activate logreview-build
python packaging\windows\build_exe.py
```

Produces `dist/windows/LogReview_v<version>_windows_x64.zip` — unzip anywhere writable,
double-click `LogReview.exe`. Python, Tk, GDAL, Pillow and openpyxl are all
inside; nothing is installed on the machine. The build script runs the packaged
exe's own `--selftest` and refuses to report success if it fails.

Starting with v1.5, the portable app checks the repository's latest published
GitHub Release on every application start. When a newer version is available, the only choices
are **Update now** and **Later**. Update now downloads the full ZIP, requires
GitHub's SHA-256 asset digest to match, rejects unsafe archive paths, then closes
the app and hands replacement to `LogReviewUpdater.exe`.

The updater preserves `reviews/`, `cache/`, any workbook stored directly in the
portable app folder, and `stamp_types.json`. It retains one sibling
`.LogReview.previous` application directory for rollback. A failed download or
verification changes nothing; a replacement or restart failure restores the
previous application. Because v1.4 has no update checker, v1.5 must be installed
manually once. Later releases can update automatically.

Build it from the **`logreview-build`** env, not the project env:

```powershell
conda create -n logreview-build -c conda-forge python=3.12 "libblas=*=*openblas" numpy gdal pillow openpyxl tk
conda activate logreview-build; pip install pyinstaller
```

The project environment's numpy may link **MKL**, which alone adds hundreds of MB
of DLLs the app
never executes (it does no linear algebra). Building against the OpenBLAS numpy
takes the bundle from 733 MB / 247 MB zipped to **181 MB / 68 MB zipped**. The
packaged app defaults differ from the source app, because a frozen copy has no
project tree around it: it **asks** for the image folder on first run, and saves
to `reviews\<folder name>_stamp_review.xlsx` beside the executable.

### Keyboard (a 70-page sweep by mouse is a bad afternoon)

| Key | Does |
|---|---|
| `Y` / `N` | set the draft to has a stamp / has no stamp; does not advance |
| `1`…`9` | pick the *n*th stamp type for the active or first empty stamp row |
| `Ctrl+Enter` | add or update the current entry and advance |
| `Space` | next unreviewed page |
| `Ctrl+→` / `Ctrl+←` | next / previous page in the list |
| `T` / `B` | jump to the top / **bottom** of the page — stamps sit at both ends |
| `W` / `F` | fit width / fit the whole page |
| mouse wheel, `+` / `−` | zoom (at the cursor) |
| right scrollbar, arrows, drag | pan vertically; `Shift`+wheel pans sideways |
| `Ctrl+S` | save the workbook |
| `Esc` | focus back to the viewer (out of the notes box) |

## Inputs

- `data/raw/Digitization_TIF_FINAL/*.TIF` — 70 single-page bilevel TIFFs, read by
  path and never copied. Also accepts `.tiff/.png/.jpg/.jpeg/.bmp/.gif`; anything
  that is not a TIFF is opened with PIL instead of GDAL.
- An existing workbook selected with `--xlsx`, or the folder-specific workbook
  under `reviews/`, is **read back on start**
  so a half-finished review resumes where it stopped.
- `stamp_types.json` beside the workbook — the list of stamp types offered (created on first
  use, seeded with `IHS`).
- Bundled `log_types.json` — the fixed log classifications offered by the app:
  `Caliper`, `Gamma Ray`, `Porosity`, `Resistivity deep`, `Resistivity medium`,
  `Resistivity shallow`, `Sonic`, and `Spontaneous Potential`.
- An optional TXT, CSV, TSV, or XLSX image list for startup or **Evaluate a
  subset**. Headerless TXT files contain one identifier per nonblank line.
  Structured files contain a numeric identifier column and may contain a TIFF
  path, latitude, longitude, and depth column. Common `API`/`UWI`/`WELL_ID`,
  `TIFPATH`/`FILE_PATH`/`IMAGE_PATH`, `LAT`/`LATITUDE`, `LONG`/`LONGITUDE`, and
  `DEPTH1`/`WELL_DEPTH` headers are detected case-insensitively; ambiguous files
  prompt for an explicit column mapping.

## Steps

1. **Launch** `log-review`, select an image folder or image-list file, then either open an existing
   LogReview workbook or choose the location and name for a new one. Reconcile
   any filename differences, then enter the reviewer name. The most recently
   populated `reviewed_by` value is offered as the default. The app lists every
   current-folder image and opens the **first unreviewed** page.
2. **Look at the page.** It opens fit-to-width at the top (where most stamps are).
   Press `B` to check the bottom of the page as well — in the v2 detector run, 3
   of 13 confirmed stamps sat within 3 in of the *tail* of the page, so a
   header-only look is not a review. Zoom in to confirm what you are seeing.
3. **Build the entry draft.** Answer `Y` or `N`. A Yes answer reveals a required
   stamp-type dropdown; **Add another stamp** creates another visible dropdown.
   Stamp types cannot repeat. Check any applicable fixed log-type boxes and add
   an optional single-line note.
4. Click **Add entry** (or **Update entry** for an existing row). The button is
   enabled only after a valid change. The app writes Excel and advances to the
   next incomplete page. Navigating away from a draft offers Save, Discard, or
   Cancel; `Ctrl+S` saves a draft without advancing.
5. **Track progress** in the list: a green `✔` marks a reviewed log, an amber `⚠`
   a committed partial entry, and the toolbar counts
   `reviewed / total` and how many carry a stamp. The `show:` filter narrows the
   list to *to do* or *done*.
6. **Optionally evaluate a subset.** Click **Evaluate a subset…**, select a TXT,
   CSV, TSV, or XLSX file, review the load summary, and create or replace the
   workbook's `subset` sheet. Use **whole dataset** and **subset** above the log
   list to switch scopes. The importer loads every readable TIFF basename for an
   identifier, preferring the current image collection and falling back to paths
   listed in the file. Only identifiers with a readable image enter the subset.
   Cancellation or a failed workbook save leaves the prior subset unchanged. A
   timestamped row-level audit CSV is written beside the workbook.
   When a depth column is detected, first choose **Load all depths** or apply a
   strict **less than** / **greater than** threshold in feet. The dialog separately
   controls whether blank depths remain eligible. Values equal to the threshold,
   nonnumeric nonblank values, and blank values when unchecked are excluded and
   identified explicitly in the audit.
7. **When the sweep is complete**, compare it against the detector's
   `stamp_inventory.csv` (join its `api14` to `log_api`) and promote as described in
   [Outputs](#outputs).

### Adding a new stamp type

Either **+ Add stamp type…** in the app (writes the JSON for you), or edit
`stamp_types.json` beside the workbook between sessions:

```json
{ "types": ["IHS", "TWDB", "Railroad Commission"] }
```

Order is the order in the dropdown, and therefore what `1`…`9` select. Types
already used in the workbook are merged in on load, so a workbook edited by hand
in Excel never loses a type. A stamp type cannot contain a comma because commas
separate multiple types in the workbook.

### Renaming or removing a stamp type

Click **Manage stamp types…**, select a type, then choose **Rename…** or
**Remove…**. Rename updates the catalog and every saved review row containing
that value. Renaming to an existing type merges the two values and removes any
per-row duplicate. Remove deletes the type from both the catalog and affected
rows after showing how many entries will change. A `YES` row whose only type is
removed becomes incomplete and returns to the to-do workflow.

Both operations save immediately. Pending edits on the current log must first be
saved or discarded. Before changing an existing workbook, LogReview writes a
sibling backup named like `review.pre_stamp_types_20260820_101500.xlsx`. A locked
workbook or failed JSON/workbook write rolls the in-memory catalog and records
back to their prior state.

## Assumptions

- **A page gets one stamp verdict but may list several distinct stamp types.**
  `has_stamp` means "this page carries at least one"; `type_of_stamp` preserves
  the visible stamp-row order as a comma-separated value. Duplicate categories
  are intentionally not used as a count of physical stamp occurrences.
- **Yes requires a type.** Clicking Yes reveals a blank dropdown and never
  silently assumes `IHS`. Legacy Yes rows without a type remain visible as
  incomplete until corrected.
- **Log classification is independent and optional.** A log may have multiple
  fixed types. Notes or classifications may be submitted without a stamp verdict,
  but such an entry remains incomplete and cycles back through *to do*.
- **The file name is the key.** Comparisons ignore letter case while Excel keeps
  the actual spelling. Moving the folder keeps the review and rewrites its path.
  A renamed TIFF is intentionally treated as one new blank row plus one workbook-
  only row; LogReview never guesses that two names represent the same file.
- **Not yet reviewed ≠ no stamp.** `has_stamp` is left **blank**, never `FALSE`,
  until a person answers. Anything consuming the workbook must filter on that.
- **Source identifiers are preserved.** `log_api` accepts 1–14 digits as text;
  values such as `21441` and `22536` are not padded. For matching only, a
  12-digit identifier also tries the standard trailing-`00` filename form.
- **`IHS ASSOC IMAGE` is a placeholder, not a path.** It is marked covered when
  another TIFF loads for the same identifier and unresolved otherwise.
- **Location fields are source metadata.** When a manifest maps latitude and/or
  longitude, the first nonblank value for each eligible identifier is copied to
  every loaded TIFF row for that identifier. The source file remains unchanged.
- **Depth comparisons are strict and assume feet.** `< 200` excludes a depth of
  exactly 200; `> 200` also excludes exactly 200. Invalid nonblank depths never
  pass a numeric filter and are counted separately from blank depths.
- **Attribution is latest-editor metadata, not an audit log.** Every successful
  Add/Update writes the active reviewer and local timestamp, replacing any prior
  attribution on that row. Existing untouched legacy rows remain blank.

### Reconciling a workbook with the selected folder

- A folder TIFF missing from Excel (including a manually deleted row) is listed;
  **Continue** recreates it blank and incomplete. Deleted review content cannot
  be reconstructed.
- An Excel row missing from the folder offers **Remove rows / Keep rows / Back**.
  Remove first copies the original workbook to a sibling named like
  `review.pre_reconcile_20260818_143000.xlsx`. Keep excludes those rows from the
  current UI and progress count but preserves them after current-folder rows on
  every save.
- Back and any failed reconciliation leave the original workbook untouched.
  Duplicate filenames, invalid verdicts, and invalid schemas remain hard errors.

## Parameters

| Parameter | Value | Note |
|---|---|---|
| Image formats listed | `.tif .tiff .png .jpg .jpeg .bmp .gif` | `Thumbs.db` and non-images skipped |
| Zoom ladder | 1:512 … 4:1, 23 rungs | wheel zooms about the cursor |
| Opening view of a page | fit width, at row 0 | header first; `B` for the tail |
| Pyramid levels | powers of two, no coarser than the screen | stable tile reuse while zooming |
| Persistent tile size | 512 × 512 grayscale pixels | reduced levels only; full resolution is not duplicated on disk |
| Decimation for display | GDAL native average; `AVERAGE_BIT2GRAYSCALE` for 1-bit TIFFs | preserves true ink coverage |
| Render debounce | 90 ms | coalesces a drag into one render |
| Rendered-viewport cache | 8 views, in the worker | revisiting a page or a zoom is instant |
| In-memory tile cache | 128 tiles (~32 MB maximum) | nearby scrolling reuses decoded pixels |
| Prefetch | one tile row above and below the viewport | warms the next likely wheel/drag movement while idle |
| Persistent cache | `cache/` beside the app | keyed by source path, size, mtime, and format version |
| Entry persistence | explicit Add/Update entry | saves the whole workbook, then advances |

Measured on this workstation (2026-08-18), largest page in the corpus
(`42285314450000…TIF`, 5200 × 528110 px, 2746 MP @ 400 dpi):

| View | First uncached | Memory cache | Reopened disk cache |
|---|---:|---:|---:|
| fit width, top of page (factor 4) | 0.205 s | 0.007 s | 0.040 s |
| whole page (factor 512) | 10.039 s | 0.001 s | 0.016 s |

The cold whole-page build is a one-time scan of all 2.75 billion source pixels.
GDAL's dedicated 1-bit grayscale overview path is slower on that first pass than
the former Python pooling implementation, but the result now persists across
sessions. Ordinary fit-width navigation remains sub-second before caching and
near-instant afterward.

## Outputs

- `reviews/<folder>_stamp_review.xlsx` by default, sheet `review`, one row per
  file in folder order:

  | Column (exact order) | Meaning |
  |---|---|
  | `log_api` | supplied 1–14 digit API/well identifier, preserved as text |
  | `file_link` | clickable hyperlink to the image; shows the file name |
  | `has_stamp` | Excel boolean `TRUE` / `FALSE`; **blank = not yet reviewed** |
  | `type_of_stamp` | ordered comma-separated stamp types; blank when `has_stamp` is `FALSE` |
  | `log_types` | comma-separated fixed log classifications |
  | `notes` | free text from the reviewer |
  | `reviewed_at` | local timestamp of the latest committed edit |
  | `reviewed_by` | active reviewer responsible for the latest committed edit |
  | `file_path` | the same target as plain text (hyperlinks are invisible to pandas) |
  | `latitude` | optional source latitude, appended when a manifest maps that field |
  | `longitude` | optional source longitude, appended when a manifest maps that field |

  Header is frozen and auto-filtered. The sheet is **rewritten in full on every
  save**, so the file on disk is always the complete current answer.
- Optional sheet `subset` uses the exact same columns as `review` (the nine base
  columns plus any appended location columns) and contains
  one complete row for every selected TIFF. It is a synchronized mirror: both
  viewer scopes edit the canonical records in `review`, and every successful save
  regenerates the subset rows with current verdicts, stamp types, log types, notes,
  timestamps, reviewer values, hyperlinks, and file paths. Saving from subset mode
  still writes every current-folder review row and preserves retained out-of-folder
  rows. The older v1.6/v1.6.1 `position` / `log_api` subset sheet is accepted and
  upgraded on the next successful save.
- `stamp_types.json` beside the workbook — the type list, as above.
- `<source>_load_status_<timestamp>.csv` beside the workbook — every input row
  plus its identifier/path resolution, loaded flags, status, source, and detail.
  Its summary reports folder loads, listed-path recoveries, duplicates,
  partial/unrecovered identifiers, covered/unresolved placeholders, and any
  depth-filter exclusions.
- Bundled `log_types.json` — fixed choices maintained with the application.
- `cache/<source-id>/<revision>/...` — disposable grayscale pyramid tiles and
  source identity metadata. Delete them at any time to force regeneration.

- `dist/windows/LogReview_v<version>_windows_x64.zip` and its `.sha256` file, plus
  `dist/windows/LogReview/` — the standalone build;
  `dist/windows/build_report.txt` and `selftest_report.txt` record its size and
  verification run. Build scratch (`build/windows/`) is disposable.

Promotion (only once a sweep is complete and checked): the workbook is the truth
set for `detect_date_stamp`, so promote it to `data/processed/` and log it there,
rather than to `deliverables/`. The zip is a **tool**, not a result — promote it
to `deliverables/` only when it is actually handed to someone outside, and log
that in the ledger with the version and date.

## Caveats

- **Dithered pages, and why the viewer averages.** Some scans carry large
  halftone/dithered regions — `42175010740000_HOU-WL-IMG-1-1304011.TIF` is ~39 %
  ink as salt-and-pepper speckle across the right 40 % of the sheet, which at 1:1
  is legible light grey. Decimating that by MAX pool (any ink in the cell → black)
  renders it as a **solid black slab** and the page looks corrupt; the file is
  fine — GDAL, PIL and tifffile agree bit-for-bit. The viewer therefore always
  averages. The same trap applies to any zoomed-out QC render of this corpus, including
  `render.page_overview`, which still MAX-pools.
- **The first visit pays the decode cost.** A cache directory can ship ready for
  use, but a reusable application cannot precompute pixels for folders it has not
  seen. Tiles are generated lazily from each selected folder and reused thereafter.
  Moving or changing a TIFF creates a new cache revision.
- **Original TIFFs are immutable inputs.** The viewer never builds internal TIFF
  overviews and never writes `.ovr` sidecars beside the images; all derivatives
  live under the application's `cache/` directory.
- **"Whole page" is a locator, not a reading.** At 1:660 a 44-ft log is an 8-px
  strip; use it to find where the ink is, then zoom. Judging a stamp needs at
  least ~1:8 (a 1.4 in stamp is then ~70 px).
- **The workbook must be closed in Excel to save.** A locked file leaves the
  current draft intact and does not advance. Close Excel and retry Add/Update or
  press `Ctrl+S`; nothing is written to a `_<timestamp>` fallback file.
- Reading a page uses the TIFF's own dpi tag; two files in the corpus carry an
  implausible tag, which changes only the reported inches, not the pixels.
- Non-TIFF images are loaded whole by PIL, so a very large PNG or JPEG will be
  slow and memory-hungry in a way the TIFFs are not.
- The viewer is optimized for single-band scanned documents. Non-TIFF images are
  still loaded whole through PIL and do not use the persistent GDAL pyramid.
- v1.2 through v1.6 workbooks may use `api14`, omit `log_types`, `reviewed_by`,
  or the optional `subset` sheet, and place `file_path` earlier in the schema.
  They are accepted as legacy input and rewritten to the current base schema
  on their next successful save without altering untouched review values.
- The packaged executable is **unsigned**, so Windows SmartScreen shows "Windows
  protected your PC" on first launch on another machine — More info → Run anyway.
  Some corporate policies block unsigned executables outright; there is no fix
  short of code-signing.
- The updater verifies integrity but the executables are not yet Authenticode-
  signed. SHA-256 verification protects against corrupt or substituted downloads
  relative to GitHub's metadata; a certificate is still required to establish
  publisher identity and reduce SmartScreen warnings.
- The build is **Windows x64 only**, and it freezes whatever the
  `logreview-build` environment holds. A fix to `src/logcv/` reaches packaged
  users only after a rebuild and re-send. The ZIP version comes from
  `pyproject.toml`.
