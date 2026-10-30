# SPDX-License-Identifier: GPL-2.0-or-later

"""Guard scheduled staging synchronization against interrupted attempts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_staging_sync(pending: Path, arguments: list[str]) -> int:
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.fsync(descriptor)
        _sync_directory(pending.parent)
    finally:
        os.close(descriptor)

    from lore_review_companion.cli import main

    result = main(["sync-review-staging", *arguments])
    if result in (0, os.EX_TEMPFAIL):
        pending.unlink()
        _sync_directory(pending.parent)
    return 0 if result == os.EX_TEMPFAIL else result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("Expected pending-file path and staging synchronization arguments")
    raise SystemExit(run_staging_sync(Path(sys.argv[1]), sys.argv[2:]))