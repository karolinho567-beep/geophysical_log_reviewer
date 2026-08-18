"""Independent Windows helper that replaces a stopped portable LogReview app."""
from __future__ import annotations

import argparse
import ctypes
import glob
import os
import shutil
import subprocess
import sys
import time

APP_EXE = "LogReview.exe"
UPDATER_EXE = "LogReviewUpdater.exe"
PRESERVE_DIRS = ("cache", "reviews")
PRESERVE_PATTERNS = ("*.xlsx", "*.xlsm", "stamp_types.json")


class ApplyError(RuntimeError):
    pass


def wait_for_process(pid: int, timeout: float = 120.0) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return
        try:
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            if result == 0x00000102:
                raise ApplyError("LogReview did not close before the update timeout.")
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    raise ApplyError("LogReview did not close before the update timeout.")


def _remove(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)


def _preserve_user_data(backup: str, target: str) -> list[tuple[str, str]]:
    moved: list[tuple[str, str]] = []
    names: list[str] = list(PRESERVE_DIRS)
    for pattern in PRESERVE_PATTERNS:
        names.extend(os.path.basename(path) for path in glob.glob(os.path.join(backup, pattern)))
    for name in dict.fromkeys(names):
        source = os.path.join(backup, name)
        destination = os.path.join(target, name)
        if not os.path.exists(source):
            continue
        if os.path.exists(destination):
            _remove(destination)
        shutil.move(source, destination)
        moved.append((source, destination))
    return moved


def apply_update(source: str, target: str, *, launch: bool = True) -> str:
    """Swap in ``source`` transactionally and retain one previous app copy."""
    source = os.path.abspath(source)
    target = os.path.abspath(target)
    if not os.path.isdir(source) or not os.path.isfile(os.path.join(source, APP_EXE)):
        raise ApplyError("The staged application is incomplete.")
    if not os.path.isfile(os.path.join(source, UPDATER_EXE)):
        raise ApplyError("The staged application has no updater helper.")
    if not os.path.isdir(target) or not os.path.isfile(os.path.join(target, APP_EXE)):
        raise ApplyError("The installed application directory is invalid.")
    if os.path.commonpath((source, target)) in (source, target):
        raise ApplyError("The staged and installed application directories overlap.")

    backup = os.path.join(os.path.dirname(target), f".{os.path.basename(target)}.previous")
    if os.path.exists(backup):
        _remove(backup)

    moved_data: list[tuple[str, str]] = []
    target_moved = False
    new_moved = False
    try:
        shutil.move(target, backup)
        target_moved = True
        shutil.move(source, target)
        new_moved = True
        moved_data = _preserve_user_data(backup, target)
        if launch:
            subprocess.Popen([os.path.join(target, APP_EXE)], cwd=target, close_fds=True)
    except Exception as exc:
        try:
            for old_path, new_path in reversed(moved_data):
                if os.path.exists(new_path):
                    if os.path.exists(old_path):
                        _remove(old_path)
                    shutil.move(new_path, old_path)
            if new_moved and os.path.exists(target):
                _remove(target)
            if target_moved and os.path.exists(backup):
                shutil.move(backup, target)
        except Exception as rollback_exc:
            raise ApplyError(
                f"Update failed ({exc}); rollback also failed ({rollback_exc})."
            ) from rollback_exc
        raise ApplyError(f"Update failed and the previous version was restored: {exc}") from exc
    return backup


def _show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "LogReview update failed", 0x10)
    else:
        print(message, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-pid", type=int, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        wait_for_process(args.wait_pid)
        apply_update(args.source, args.target)
        return 0
    except Exception as exc:
        _show_error(str(exc))
        return 1


__all__ = ["ApplyError", "apply_update", "main", "wait_for_process"]
