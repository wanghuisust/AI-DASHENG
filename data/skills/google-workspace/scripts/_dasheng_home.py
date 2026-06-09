"""Resolve DASHENG_HOME for standalone skill scripts.

Skill scripts may run outside the DASHENG process (e.g. system Python,
nix env, CI) where ``dasheng_constants`` is not importable.  This module
provides the same ``get_dasheng_home()`` and ``display_dasheng_home()``
contracts as ``dasheng_constants`` without requiring it on ``sys.path``.

When ``dasheng_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``dasheng_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``DASHENG_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dasheng_constants import display_dasheng_home as display_dasheng_home
    from dasheng_constants import get_dasheng_home as get_dasheng_home
except (ModuleNotFoundError, ImportError):

    def get_dasheng_home() -> Path:
        """Return the DASHENG home directory (default: ~/.dasheng).

        Mirrors ``dasheng_constants.get_dasheng_home()``."""
        val = os.environ.get("DASHENG_HOME", "").strip()
        return Path(val) if val else Path.home() / ".dasheng"

    def display_dasheng_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``dasheng_constants.display_dasheng_home()``."""
        home = get_dasheng_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
