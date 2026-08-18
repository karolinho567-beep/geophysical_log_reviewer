# Windows packaging

Build the reusable LogReview desktop product as a standalone Windows x64 folder
and ZIP. The application entry point remains `logcv.review.cli:main`; `entry.py`
is only the small script PyInstaller requires.

## Build

Use the lightweight OpenBLAS build environment documented in
[`../../docs/LOG_REVIEW_APP.md`](../../docs/LOG_REVIEW_APP.md):

```powershell
conda activate logreview-build
python packaging\windows\build_exe.py
```

Outputs:

- `dist/windows/LogReview/` — unpacked standalone application.
- `dist/windows/LogReview_v<version>_windows_x64.zip` — GitHub Release asset.
- The matching `.zip.sha256` file — human-readable checksum.
- `dist/windows/build_report.txt` and `selftest_report.txt` — verification record.
- `build/windows/` — disposable PyInstaller scratch files.

The unpacked application contains empty `reviews/` and `cache/` directories.
Review workbooks and disposable image-pyramid tiles therefore remain beside the
portable application rather than beside the source logs.

The build script runs the packaged executable's own end-to-end self-test before
creating the ZIP.

It also builds the independent `LogReviewUpdater.exe` helper included in the
portable folder. The GUI downloads and verifies releases; the helper runs only
after the GUI closes, replaces application files, preserves user data, and
restarts LogReview.
