"""``brindle account``: hand the command line to an installed account plugin.

``brindle account [args...]`` passes its arguments, untouched, to the plugin
installed in the ``brindle.account`` group (a factory ``make(repo_root) ->
AccountPlugin | None``; see ``brindle.plugins`` for how one is selected) and
exits with what it returns. brindle ships one (``brindle.pro.account``), so
there normally is one; with the group turned off in the config, or none
installed, it says so and exits 0.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from brindle import plugins
from brindle.config import RepoConfig

log = logging.getLogger(__name__)

GROUP = plugins.ACCOUNT
NOT_INSTALLED = ("brindle Pro isn't installed: `brindle account` has nothing to do. "
                 "brindle's own account plugin is `pro`; check \"plugins\" in .brindle/config.json.")


class AccountPlugin(ABC):
    """What an account plugin implements."""

    @abstractmethod
    def run(self, args: list[str]) -> int | None:
        """Handle ``brindle account <args>``; return the exit code (None: 0)."""


def plugin(cfg: RepoConfig, repo_root: str) -> AccountPlugin | None:
    return plugins.select(GROUP, cfg, repo_root)  # type: ignore[return-value]


def run(cfg: RepoConfig, repo_root: str, args: list[str], echo=print) -> int:
    """Run ``brindle account args`` through the plugin; the exit code."""
    p = plugin(cfg, repo_root)
    if p is None:
        echo(NOT_INSTALLED)
        return 0
    try:
        code = p.run(list(args))
    except Exception as e:
        log.exception("brindle: the account plugin failed")
        echo(f"the account plugin failed: {e}")
        return 1
    return int(code or 0)


__all__ = ["GROUP", "NOT_INSTALLED", "AccountPlugin", "plugin", "run"]
