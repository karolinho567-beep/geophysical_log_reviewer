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
- `dist/windows/LogReview_v<version>_<date>.zip` — distributable archive.
- `dist/windows/build_report.txt` and `selftest_report.txt` — verification record.
- `build/windows/` — disposable PyInstaller scratch files.

The unpacked application contains empty `reviews/` and `cache/` directories.
Review workbooks and disposable image-pyramid tiles therefore remain beside the
portable application rather than beside the source logs.

The build script runs the packaged executable's own end-to-end self-test before
creating the ZIP.
