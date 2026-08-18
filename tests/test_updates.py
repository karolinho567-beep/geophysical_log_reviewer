"""Security and rollback tests for GitHub updates and the replacement helper."""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile

import pytest

from logcv.review.updates import (
    ReleaseInfo,
    UpdateError,
    download_and_stage,
    fetch_latest_release,
    parse_version,
    record_update_check,
    release_asset_name,
    update_check_due,
)
from logreview_updater import ApplyError, apply_update


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _opener(payload: bytes):
    return lambda _request, _timeout: _Response(payload)


def test_release_versions_and_asset_names_are_strict():
    assert parse_version("v1.5.0") == (1, 5, 0)
    assert release_asset_name("1.5.0") == "LogReview_v1.5.0_windows_x64.zip"
    with pytest.raises(UpdateError):
        parse_version("1.5")
    with pytest.raises(UpdateError):
        parse_version("v1.5.0-beta")


def test_latest_release_uses_exact_asset_and_github_digest():
    digest = "a" * 64
    payload = json.dumps({
        "tag_name": "v1.5.0",
        "assets": [{
            "name": "LogReview_v1.5.0_windows_x64.zip",
            "browser_download_url": "https://example.invalid/app.zip",
            "size": 123,
            "digest": f"sha256:{digest}",
        }],
    }).encode()
    release = fetch_latest_release("1.4.0", opener=_opener(payload))
    assert release is not None
    assert release.version == "1.5.0"
    assert release.sha256 == digest
    assert fetch_latest_release("1.5.0", opener=_opener(payload)) is None


def test_release_without_a_github_digest_cannot_auto_install():
    payload = json.dumps({
        "tag_name": "v1.5.0",
        "assets": [{
            "name": "LogReview_v1.5.0_windows_x64.zip",
            "browser_download_url": "https://example.invalid/app.zip",
            "size": 123,
        }],
    }).encode()
    with pytest.raises(UpdateError, match="SHA-256"):
        fetch_latest_release("1.4.0", opener=_opener(payload))


def _update_zip(*, unsafe: bool = False) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr("LogReview/LogReview.exe", b"new application")
        bundle.writestr("LogReview/LogReviewUpdater.exe", b"new updater")
        bundle.writestr("LogReview/README.txt", b"new readme")
        if unsafe:
            bundle.writestr("../outside.txt", b"escape")
    return stream.getvalue()


def _release_for(data: bytes) -> ReleaseInfo:
    return ReleaseInfo(
        version="1.5.0",
        tag="v1.5.0",
        asset_name="LogReview_v1.5.0_windows_x64.zip",
        download_url="https://example.invalid/app.zip",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def test_download_is_hashed_and_safely_staged(tmp_path):
    data = _update_zip()
    staged = download_and_stage(
        _release_for(data), opener=_opener(data), parent=str(tmp_path)
    )
    assert os.path.isfile(os.path.join(staged.app_dir, "LogReview.exe"))
    assert os.path.isfile(os.path.join(staged.app_dir, "LogReviewUpdater.exe"))


def test_zip_path_traversal_is_rejected(tmp_path):
    data = _update_zip(unsafe=True)
    with pytest.raises(UpdateError, match="Unsafe path"):
        download_and_stage(_release_for(data), opener=_opener(data), parent=str(tmp_path))
    assert not (tmp_path / "outside.txt").exists()


def test_automatic_check_is_due_on_every_app_start(tmp_path):
    assert update_check_due(str(tmp_path), now=1000)
    record_update_check(str(tmp_path), now=1000)
    assert update_check_due(str(tmp_path), now=1000)
    assert not (tmp_path / "update_check.json").exists()


def _write_app(folder, label: str):
    folder.mkdir()
    (folder / "LogReview.exe").write_text(label, encoding="utf-8")
    (folder / "LogReviewUpdater.exe").write_text(label, encoding="utf-8")


def test_updater_replaces_app_and_preserves_user_data(tmp_path):
    target = tmp_path / "PortableReview"
    source = tmp_path / "staged" / "LogReview"
    source.parent.mkdir()
    _write_app(target, "old")
    _write_app(source, "new")
    (source / "new.txt").write_text("new", encoding="utf-8")
    (source / "cache").mkdir()
    (source / "reviews").mkdir()
    (target / "cache").mkdir()
    (target / "cache" / "tile.png").write_bytes(b"tile")
    (target / "reviews").mkdir()
    (target / "reviews" / "review.xlsx").write_bytes(b"workbook")
    (target / "root.xlsx").write_bytes(b"root workbook")
    (target / "stamp_types.json").write_text('{"types":["IHS"]}', encoding="utf-8")

    backup = apply_update(str(source), str(target), launch=False)

    assert (target / "LogReview.exe").read_text(encoding="utf-8") == "new"
    assert (target / "new.txt").exists()
    assert (target / "cache" / "tile.png").read_bytes() == b"tile"
    assert (target / "reviews" / "review.xlsx").read_bytes() == b"workbook"
    assert (target / "root.xlsx").read_bytes() == b"root workbook"
    assert (target / "stamp_types.json").exists()
    assert os.path.isdir(backup)
    assert open(os.path.join(backup, "LogReview.exe"), encoding="utf-8").read() == "old"


def test_failed_launch_rolls_back_the_previous_app(tmp_path, monkeypatch):
    target = tmp_path / "LogReview"
    source = tmp_path / "staged" / "LogReview"
    source.parent.mkdir()
    _write_app(target, "old")
    _write_app(source, "new")

    def fail_launch(*_args, **_kwargs):
        raise OSError("simulated launch failure")

    monkeypatch.setattr("logreview_updater.subprocess.Popen", fail_launch)
    with pytest.raises(ApplyError, match="restored"):
        apply_update(str(source), str(target), launch=True)
    assert (target / "LogReview.exe").read_text(encoding="utf-8") == "old"
