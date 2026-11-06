"""Durable crash barrier for multi-file restores across state/core volumes."""
import json
import os
from pathlib import Path
from scheduler.core import atomic_write


def marker_path(directory):
    return Path(directory) / "maintenance.json"


def begin(directory, reason="恢复未完成，保持关闭"):
    atomic_write(marker_path(directory), json.dumps({"format": 1, "reason": reason}, ensure_ascii=False))


def finish(directory):
    path = marker_path(directory)
    path.unlink()
    fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
