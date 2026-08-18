r"""Build and verify the standalone Windows LogReview distribution.

Run from the project root in the dedicated ``logreview-build`` environment:

    python packaging\windows\build_exe.py

Generated artifacts go to ``dist/windows`` and disposable PyInstaller work goes
to ``build/windows``. A ZIP is created only after the packaged executable passes
its end-to-end self-test.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(ROOT, "src")
OUTPUTS = os.path.join(ROOT, "dist", "windows")
DIST = OUTPUTS
WORK = os.path.join(ROOT, "build", "windows")
ENTRY = os.path.join(HERE, "entry.py")
UPDATER_ENTRY = os.path.join(HERE, "updater_entry.py")
APP_NAME = "LogReview"
SAMPLE_LOGS = os.path.join(ROOT, "data", "raw", "Digitization_TIF_FINAL")

with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
    VERSION = tomllib.load(handle)["project"]["version"]

# Large libraries carried by the broader project environment but never imported
# by the review product. The build environment should still be kept minimal.
EXCLUDES = [
    "scipy", "cv2", "skimage", "sklearn", "matplotlib", "pandas", "geopandas",
    "shapely", "pyproj", "fiona", "contextily", "pytest", "IPython",
    "notebook", "tifffile", "imagecodecs", "pytesseract", "setuptools", "pip",
]

def _folder_size_mb(path: str) -> float:
    total = 0
    for dirpath, _, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total / 1e6


def _rmtree_retry(path: str, attempts: int = 8) -> None:
    """Remove build scratch while tolerating antivirus briefly holding DLLs."""
    for attempt in range(attempts):
        if not os.path.isdir(path):
            return
        try:
            shutil.rmtree(path)
            return
        except (PermissionError, OSError):
            if attempt == attempts - 1:
                raise
            time.sleep(1.5)


def build() -> tuple[str, list[str]]:
    app_dir = os.path.join(DIST, APP_NAME)
    for folder in (app_dir, WORK):
        _rmtree_retry(folder)
    os.makedirs(OUTPUTS, exist_ok=True)

    args = [
        sys.executable, "-m", "PyInstaller", ENTRY,
        "--name", APP_NAME,
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--paths", SRC,
        "--distpath", DIST, "--workpath", WORK, "--specpath", WORK,
        "--collect-submodules", "osgeo",
        "--collect-data", "osgeo",
        "--collect-data", "openpyxl",
        "--add-data", os.path.join(SRC, "logcv", "review", "log_types.json")
                      + os.pathsep + os.path.join("logcv", "review"),
        "--hidden-import", "PIL._tkinter_finder",
    ]
    for module in EXCLUDES:
        args += ["--exclude-module", module]

    print("$ " + " ".join(args[1:]))
    started = time.time()
    env = os.environ.copy()
    if os.name == "nt":
        # Invoking <env>/python.exe directly does not activate Conda, so its DLL
        # directories are absent from PATH. PyInstaller then sees osgeo/Pillow's
        # extension modules but cannot discover gdal.dll and their image codecs.
        conda_dll_dirs = [
            os.path.join(sys.prefix, "Library", "bin"),
            os.path.join(sys.prefix, "DLLs"),
            sys.prefix,
        ]
        env["PATH"] = os.pathsep.join(conda_dll_dirs + [env.get("PATH", "")])
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, env=env)
    print(f"PyInstaller finished in {time.time() - started:.0f} s "
          f"(exit {result.returncode})")
    warnings = [line for line in result.stderr.splitlines()
                if "WARNING" in line or "ERROR" in line]
    if result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:])
        raise SystemExit("PyInstaller failed")

    updater_work = os.path.join(WORK, "updater")
    updater_args = [
        sys.executable, "-m", "PyInstaller", UPDATER_ENTRY,
        "--name", "LogReviewUpdater",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--paths", SRC,
        "--distpath", app_dir,
        "--workpath", updater_work,
        "--specpath", updater_work,
    ]
    print("$ " + " ".join(updater_args[1:]))
    updater_result = subprocess.run(
        updater_args, cwd=ROOT, capture_output=True, text=True, env=env
    )
    warnings.extend(
        line for line in updater_result.stderr.splitlines()
        if "WARNING" in line or "ERROR" in line
    )
    if updater_result.returncode != 0:
        print(updater_result.stdout[-4000:])
        print(updater_result.stderr[-4000:])
        raise SystemExit("Updater PyInstaller build failed")
    return app_dir, warnings


def prepare_distribution(app_dir: str) -> None:
    os.makedirs(os.path.join(app_dir, "reviews"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "cache"), exist_ok=True)


def verify(app_dir: str) -> tuple[bool, str]:
    exe = os.path.join(app_dir, f"{APP_NAME}.exe")
    report = os.path.join(OUTPUTS, "selftest_report.txt")
    cmd = [exe, "--selftest", "--report", report]
    if os.path.isdir(SAMPLE_LOGS):
        cmd += ["--in", SAMPLE_LOGS]
    print("$ " + " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    text = ""
    if os.path.exists(report):
        with open(report, encoding="utf-8") as handle:
            text = handle.read()
    print(text or result.stdout or result.stderr)
    return result.returncode == 0, text


def package() -> tuple[str, str]:
    base = os.path.join(OUTPUTS, f"{APP_NAME}_v{VERSION}_windows_x64")
    if os.path.exists(base + ".zip"):
        os.remove(base + ".zip")
    print("zipping...")
    archive = shutil.make_archive(base, "zip", root_dir=DIST, base_dir=APP_NAME)
    digest = hashlib.sha256()
    with open(archive, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    checksum = archive + ".sha256"
    with open(checksum, "w", encoding="ascii") as handle:
        handle.write(f"{digest.hexdigest()}  {os.path.basename(archive)}\n")
    return archive, checksum


def main() -> int:
    app_dir, warnings = build()
    prepare_distribution(app_dir)
    ok, selftest_report = verify(app_dir)
    zipped, checksum = package() if ok else ("NOT CREATED", "NOT CREATED")

    lines = [
        f"LogReview v{VERSION} build report - {time.strftime('%Y-%m-%d %H:%M')}",
        f"  app folder : {app_dir}  ({_folder_size_mb(app_dir):.0f} MB)",
        f"  zip        : {zipped}",
        f"  sha256     : {checksum}",
        f"  selftest   : {'PASS' if ok else 'FAIL'}",
        "",
        selftest_report,
        "",
        f"PyInstaller warnings ({len(warnings)}):",
        *[f"  {line}" for line in warnings[:40]],
    ]
    text = "\n".join(lines)
    with open(os.path.join(OUTPUTS, "build_report.txt"), "w", encoding="utf-8") as handle:
        handle.write(text + "\n")
    print("\n" + text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
