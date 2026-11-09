"""
DISPOSABLE PATHS ONLY. A test helper may delete or overwrite a file only inside
a temp directory. Anything else is refused, loudly, whatever path it was handed.

WHY (OPEN D-060). reset_for_tests() in tick_map, sentiment_window and
symbol_block deleted or overwrote whatever path the configuration resolved to.
Run on the box with one environment variable missing, that is the live tick map,
the live stand-down, the live symbol blocks. Every other defect found that week
produced a wrong number; this one destroyed data, and nothing stood between it
and production except the caller remembering to redirect the path. "Nothing
changed during the runs" was luck, not a guard.

So the check is on the PATH, not on the environment: the real path is resolved
(symlinks followed, so a link cannot smuggle a production file through) and
must sit inside the system temp directory. There is no override flag -- a
helper that needs to reset production state is not a test helper.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("disposable")

# The system temp directory, resolved. On macOS tempfile.gettempdir() is under
# /var/folders (a symlink to /private/var/folders); on Linux it is /tmp. Both
# spellings are resolved, so either form of a temp path is recognised.
_ROOTS = tuple(sorted({os.path.realpath(p) for p in
                       (tempfile.gettempdir(), "/tmp")}))


class NotDisposable(RuntimeError):
    """A test helper was pointed at a path outside the temp directory."""


def is_disposable(path) -> bool:
    try:
        real = os.path.realpath(os.fspath(path))
    except Exception:
        return False
    return any(real == r or real.startswith(r + os.sep) for r in _ROOTS)


def require_disposable(path, what: str) -> Path:
    """Return the path if it is inside the temp directory; otherwise refuse."""
    if not is_disposable(path):
        msg = (f"REFUSED: {what} would delete/overwrite {path!s} "
               f"(resolves to {os.path.realpath(os.fspath(path))}), which is not "
               f"inside the temp directory {_ROOTS}. A test helper may only touch "
               f"its own fixture files. Point the configured path at a temp file.")
        log.critical(msg)
        raise NotDisposable(msg)
    return Path(path)
