"""One error type for the service layer. ``from_exception`` maps the engine's
own error classes onto a stable code and an HTTP status; anything it doesn't
know becomes ``internal`` with a generic message, so no detail leaks."""

from __future__ import annotations

from typing import Any

from brindle import (
    agents,
    autopilot,
    coordinator,
    git,
    profiles,
    rewind,
    scratch,
    spawn,
    tmux,
    workspaces,
)
from brindle import repos as repos_mod
from brindle.cost import NotEntitled as CostNotEntitled
from brindle.pro.auth import AuthError
from brindle.pro.license import NotEntitled as LicenseNotEntitled


class ServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = extra          # more fields for the error object (e.g. a log tail)

    def to_json(self) -> dict[str, Any]:
        return {"error": {**self.extra, "code": self.code, "message": self.message}}


# Checked in order: the first class an exception is an instance of wins, so a
# subclass (StillRunning, ProfileError) sits above its base.
_MAP: tuple[tuple[tuple[type[BaseException], ...], str, int], ...] = (
    ((agents.StillRunning,), "still_running", 409),
    ((spawn.Busy,), "busy", 503),        # above GitError and TmuxError: GitBusy and TmuxBusy are both
    ((CostNotEntitled, LicenseNotEntitled), "not_entitled", 402),
    ((AuthError,), "signed_out", 401),
    ((profiles.ProfileError, coordinator.CoordinatorError, autopilot.AutopilotError), "invalid", 400),
    ((agents.AgentError, workspaces.WorkspaceError, rewind.RewindError, scratch.ScratchError,
      repos_mod.RepoError, git.GitError), "conflict", 409),
    ((tmux.TmuxError,), "terminal_unavailable", 503),
    ((ValueError,), "invalid", 400),
)


def from_exception(exc: BaseException) -> ServiceError:
    if isinstance(exc, ServiceError):
        return exc
    if isinstance(exc, agents.PreflightBlocked):   # a job's launch: the items, as POST answers them
        return ServiceError("preflight", str(exc), 409, preflight=[i.to_json() for i in exc.items])
    for kinds, code, status in _MAP:
        if isinstance(exc, kinds):
            return ServiceError(code, str(exc), status)
    return ServiceError("internal", "unexpected error", 500)
