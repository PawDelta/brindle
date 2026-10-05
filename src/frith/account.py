"""``frith account``: hand the command line to an installed account plugin.

``frith account [args...]`` passes its arguments, untouched, to the plugin
installed in the ``frith.account`` group (a factory ``make(repo_root) ->
AccountPlugin | None``; see ``frith.plugins`` for how one is selected) and
exits with what it returns. frith ships one (``frith.pro.account``), so
there normally is one; with the group turned off in the config, or none
installed, it says so and exits 0.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from frith import plugins
from frith.config import RepoConfig

log = logging.getLogger(__name__)

GROUP = plugins.ACCOUNT
NOT_INSTALLED = ("frith Pro isn't installed: `frith account` has nothing to do. "
                 "frith's own account plugin is `pro`; check \"plugins\" in .frith/config.json.")


class AccountPlugin(ABC):
    """What an account plugin implements."""

    @abstractmethod
    def run(self, args: list[str]) -> int | None:
        """Handle ``frith account <args>``; return the exit code (None: 0)."""


def plugin(cfg: RepoConfig, repo_root: str) -> AccountPlugin | None:
    return plugins.select(GROUP, cfg, repo_root)  # type: ignore[return-value]


def run(cfg: RepoConfig, repo_root: str, args: list[str], echo=print) -> int:
    """Run ``frith account args`` through the plugin; the exit code."""
    p = plugin(cfg, repo_root)
    if p is None:
        echo(NOT_INSTALLED)
        return 0
    try:
        code = p.run(list(args))
    except Exception as e:
        log.exception("frith: the account plugin failed")
        echo(f"the account plugin failed: {e}")
        return 1
    return int(code or 0)


__all__ = ["GROUP", "NOT_INSTALLED", "AccountPlugin", "plugin", "run"]
