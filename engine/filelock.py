"""
Simple file-based locking for 3body task JSON files.

Prevents concurrent writes from patrol, autopilot, and task_board_server
from corrupting JSON files. Uses atomic write (write to temp, then rename).
"""
import fcntl
import json
import os
import tempfile
from pathlib import Path


def read_json_locked(path: Path):
    """Read a JSON file with a shared (read) lock."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as exc:
        return {'__error__': str(exc)}


def write_json_locked(path: Path, data: dict):
    """Write a JSON file with an exclusive lock.
    Tries atomic rename first; falls back to direct write for iCloud/sandboxed dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Try atomic write (temp + rename) first
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f'.{path.stem}.',
            suffix='.tmp'
        )
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(data, f, ensure_ascii=False, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.rename(tmp_path, str(path))
        return
    except PermissionError:
        # iCloud sandbox blocks rename — fall back to direct write
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Fallback: direct write with lock (not atomic, but works on iCloud)
    with open(path, 'w', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
