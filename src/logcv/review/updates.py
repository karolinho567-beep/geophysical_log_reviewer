"""GitHub release discovery, verified download, and updater hand-off.

The running Windows application never overwrites itself. It downloads and
validates a complete portable distribution, then launches the small, independent
``LogReviewUpdater.exe`` helper from a temporary directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

GITHUB_OWNER = "karolinho567-beep"
GITHUB_REPOSITORY = "geophysical_log_reviewer"
LATEST_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
)
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
STATE_FILE = "update_check.json"
UPDATER_EXE = "LogReviewUpdater.exe"


class UpdateError(RuntimeError):
    """A release was invalid, unsafe, or could not be installed."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    asset_name: str
    download_url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class StagedUpdate:
    work_dir: str
    app_dir: str


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse the three-part release versions used by LogReview."""
    text = str(value).strip()
    if text.lower().startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise UpdateError(f"Unsupported release version: {value!r}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def release_asset_name(version: str) -> str:
    clean = ".".join(str(part) for part in parse_version(version))
    return f"LogReview_v{clean}_windows_x64.zip"


def _open(request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_latest_release(
    current_version: str,
    *,
    timeout: float = 6.0,
    opener=_open,
) -> ReleaseInfo | None:
    """Return a newer full GitHub release, or ``None`` when already current."""
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"LogReview/{current_version}",
        },
    )
    with opener(request, timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    tag = str(payload.get("tag_name") or "").strip()
    latest = parse_version(tag)
    if latest <= parse_version(current_version):
        return None
    version = ".".join(str(part) for part in latest)
    wanted = release_asset_name(version)
    asset = next(
        (item for item in payload.get("assets", []) if item.get("name") == wanted), None
    )
    if not asset:
        raise UpdateError(f"Release {tag} does not contain {wanted}.")
    digest = str(asset.get("digest") or "")
    if not digest.lower().startswith("sha256:"):
        raise UpdateError(f"GitHub did not provide a SHA-256 digest for {wanted}.")
    sha256 = digest.split(":", 1)[1].lower()
    if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
        raise UpdateError(f"GitHub returned an invalid digest for {wanted}.")
    return ReleaseInfo(
        version=version,
        tag=tag,
        asset_name=wanted,
        download_url=str(asset.get("browser_download_url") or ""),
        size=int(asset.get("size") or 0),
        sha256=sha256,
    )


def update_check_due(cache_dir: str, *, now: float | None = None) -> bool:
    """Limit automatic GitHub requests to once every 24 hours."""
    try:
        with open(os.path.join(cache_dir, STATE_FILE), encoding="utf-8") as handle:
            checked = float(json.load(handle).get("last_checked", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return True
    return (time.time() if now is None else now) - checked >= CHECK_INTERVAL_SECONDS


def record_update_check(cache_dir: str, *, now: float | None = None) -> None:
    """Best-effort timestamp; update availability must never block the viewer."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, STATE_FILE)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"last_checked": time.time() if now is None else now}, handle)
        os.replace(temp, path)
    except OSError:
        pass


def _safe_extract(archive: str, destination: str) -> None:
    root = os.path.abspath(destination)
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            name = item.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            mode = item.external_attr >> 16
            if (
                not name
                or name.startswith("/")
                or ".." in parts
                or (parts and ":" in parts[0])
                or stat.S_ISLNK(mode)
            ):
                raise UpdateError(f"Unsafe path in update archive: {item.filename!r}")
            target = os.path.abspath(os.path.join(root, *parts))
            if os.path.commonpath((root, target)) != root:
                raise UpdateError(f"Unsafe path in update archive: {item.filename!r}")
        bundle.extractall(root)


def download_and_stage(
    release: ReleaseInfo,
    *,
    timeout: float = 60.0,
    opener=_open,
    parent: str | None = None,
) -> StagedUpdate:
    """Download, hash, and safely extract one complete portable application."""
    work_dir = tempfile.mkdtemp(prefix="LogReview-update-", dir=parent)
    archive = os.path.join(work_dir, release.asset_name)
    try:
        request = urllib.request.Request(
            release.download_url,
            headers={"User-Agent": f"LogReview/{release.version}"},
        )
        digest = hashlib.sha256()
        received = 0
        with opener(request, timeout) as response, open(archive, "wb") as handle:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)
                digest.update(block)
                received += len(block)
        if release.size and received != release.size:
            raise UpdateError(
                f"Incomplete download: expected {release.size} bytes, received {received}."
            )
        if digest.hexdigest().lower() != release.sha256.lower():
            raise UpdateError("The downloaded update failed SHA-256 verification.")

        extracted = os.path.join(work_dir, "extracted")
        os.makedirs(extracted)
        _safe_extract(archive, extracted)
        app_dir = os.path.join(extracted, "LogReview")
        for required in ("LogReview.exe", UPDATER_EXE):
            if not os.path.isfile(os.path.join(app_dir, required)):
                raise UpdateError(f"The update archive is missing {required}.")
        return StagedUpdate(work_dir=work_dir, app_dir=app_dir)
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def launch_staged_update(staged: StagedUpdate) -> subprocess.Popen:
    """Copy the helper out of the live app directory and hand off replacement."""
    if not getattr(sys, "frozen", False):
        raise UpdateError("Automatic installation is available only in the Windows app.")
    target = os.path.dirname(sys.executable)
    bundled_helper = os.path.join(target, UPDATER_EXE)
    if not os.path.isfile(bundled_helper):
        raise UpdateError(f"The installed application is missing {UPDATER_EXE}.")
    helper = os.path.join(staged.work_dir, "LogReviewUpdater-apply.exe")
    shutil.copy2(bundled_helper, helper)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    return subprocess.Popen(
        [
            helper,
            "--wait-pid", str(os.getpid()),
            "--source", staged.app_dir,
            "--target", target,
        ],
        cwd=staged.work_dir,
        close_fds=True,
        creationflags=flags,
    )
