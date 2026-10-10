"""Keep a company-identity session away from other identities.

``signin_pause`` records the identity each Claude agent launched under (Claude
org, AWS account and role, GCP project, Azure subscription; ids only, never
tokens). While the identity signed in *now* differs from an agent's recorded
one, the agent is locked: brindle won't resume, rewind, message or start
anything in its worktree or conversation, and the views show it as
``locked: <who>`` without its conversation content.

An agent with no recorded identity (started before this, or without a company
setup) is never locked, and neither is one whose provider's identity can't be
told right now (signed out, CLI missing, timeout): only a determined,
different identity locks. Nothing here reads a credential; the current
identity comes from ``signin_pause.check``.
"""

from __future__ import annotations

CONTINUE_HINT = "sign in as it to continue (`brindle login`)"


def message(who: str) -> str:
    return f"This session belongs to {who}; {CONTINUE_HINT}"


def locked_by(agent_id: str) -> str | None:
    """Who the agent's session belongs to when the identity signed in now is a
    different one, else None. Never raises."""
    try:
        from brindle import company_identity, signin_pause

        was = signin_pause.recorded_identity(agent_id)
        if not was:
            return None
        got = signin_pause.check("claude", cache=True)
        if got is None or got[0].kind != "ok" or not got[0].ident:
            return None
        if company_identity.matches(was, got[0].ident):   # the same test resume uses
            return None
        return was
    except Exception:  # noqa: BLE001 - an unreadable answer never locks anyone
        return None


def ensure(agent) -> None:
    """Raise ``AgentError`` when ``agent``'s session belongs to another identity."""
    who = locked_by(agent.id)
    if who:
        from brindle.agents import AgentError

        raise AgentError(message(who))


def workspace_locked(db, ws) -> str | None:
    """Who a worktree belongs to when an agent that ran in it is locked. Only
    an agent's own worktree counts, never the repo's main checkout."""
    if ws is None or ws.kind != "worktree":
        return None
    for a in db.list_agents(ws.id):
        who = locked_by(a.id)
        if who:
            return who
    return None


def ensure_workspace(db, ws) -> None:
    who = workspace_locked(db, ws)
    if who:
        from brindle.agents import AgentError

        raise AgentError(message(who))
