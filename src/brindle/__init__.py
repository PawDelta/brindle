"""brindle: run CLI coding agents in tmux, each isolated on its own git worktree."""

import os
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("brindle")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "unknown"


def staff_build() -> bool:
    """The desktop's offline build marker; the bundled engine version is plain."""
    return os.environ.get("BRINDLE_STAFF_BUILD") == "1"
