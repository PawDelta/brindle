"""Expired cloud logins: renewed in the background, the person told once.

Part of the cull sweep (``cull.sweep``). For each running session whose
Claude Code agents use a cloud route (Bedrock, Vertex, Foundry; see
``cloud_login``):

* A login within ``LOOKAHEAD`` of expiry, or an agent whose last reply ended
  on an expired-login API error, gets a silent refresh
  (``cloud_login.refresh``). Nobody is told; an agent stopped on the error is
  told to continue.
  A pane's own credential errors only prompt that refresh: a pane also fails
  on a signed-out login, an unreachable token endpoint or a half-written
  credentials file, none of which a sign-in fixes. Only the CLI saying the
  login lapsed (``LoginStatus.expired``) counts.
* When only the person can renew it (Google's invalid_rapt, an AWS SSO
  session that needs a browser) brindle opens the one browser sign-in for
  the machine (``cloud_login.sign_in``) and the session goes ``auth_paused``: autopilot
  doesn't nudge (``autopilot.on_stop``), messages to its agents stay queued
  (``holding``), and a turn that starts on its own (a Claude Code cron) is
  interrupted with Escape; in the person's own chat only while they're away.
  They're told once per lapse: a chat message when they're at the session,
  else an alert pinned in its status bar with the command to run and when
  the login expired. Failed re-checks never tell them again.
* While paused it re-checks with backoff (``FIRST_DELAY`` doubling up to
  ``MAX_DELAY``), refreshing each time, and while the sign-in runs it waits on
  it and re-checks once it's over. Once the login is good it clears the bar,
  delivers what was held, tells agents stopped on the error and the
  supervisor to continue, once.

State is the ``login_state`` row of the session's root.
"""

from __future__ import annotations

import logging
import os
import time

from brindle import agents, cloud_login, model_access, tmux
from brindle.db import DB, Agent, LoginState
from brindle.providers import ClaudeCode, get_provider

log = logging.getLogger(__name__)

LOOKAHEAD = 600.0       # renew a login this close to its expiry
CHECK_EVERY = 120.0     # how often a session's expiry is looked at
FIRST_DELAY = 60.0      # re-check backoff while paused: from this ...
MAX_DELAY = 900.0       # ... doubling up to this
ACTIVE_WITHIN = 300.0   # a client with input this recent: the person is at the session
TAG = "[brindle login]"


def paused(db: DB, root_id: str) -> bool:
    st = db.get_login_state(root_id)
    return st is not None and st.state == "auth_paused"


def holding(db: DB, agent_id: str) -> bool:
    """Whether messages to ``agent_id`` wait: its session is auth_paused."""
    from brindle import autopilot

    return paused(db, autopilot.root_of(db, agent_id))


def user_active(session: str, now: float) -> bool:
    """Someone is attached to ``session`` and gave it input lately."""
    if not tmux.session_attached(session):
        return False
    last = tmux.client_activity(session)
    return last is not None and now - last <= ACTIVE_WITHIN


def _env(db: DB, a: Agent) -> dict[str, str]:
    ws = db.get_workspace(a.workspace_id)
    if ws is None:
        return {}
    try:
        return agents.agent_env(ws, a.id, a)
    except Exception:  # noqa: BLE001 - an unreadable env is the ambient one
        return {}


def _on_cloud(env: dict[str, str]) -> bool:
    return cloud_login.provider_from_env({**os.environ, **env}) is not None


def _session_name(db: DB, root: Agent) -> str | None:
    ws = db.get_workspace(root.workspace_id)
    return ws.tmux_session if ws else None


def _members(db: DB, root: Agent, panes) -> list[Agent]:
    return [a for a in agents.tree(db, root.id)
            if a.provider == "claude" and a.status not in ("paused", "done")
            and a.dismissed_at is None and agents.runs_process(a) and agents.is_alive(a, panes)]


def _screen(a: Agent) -> str | None:
    try:
        return tmux.capture(a.tmux_window, lines=40, server=agents.server_of(a))
    except tmux.TmuxError:
        return None


def _ended_on_expired_login(db: DB, a: Agent) -> bool:
    if db.pending_count(a.id):
        return False    # already told to go on (or about to be)
    screen = _screen(a)
    return bool(screen) and model_access.expired_login(ClaudeCode.api_error(screen)) is not None


def _send(db: DB, to: str, text: str) -> bool:
    try:
        agents.send_message(db, to, f"{TAG} {text}", sender_id=None)
    except (agents.AgentError, tmux.TmuxError):
        return False
    return True


def sweep(db: DB, now: float | None = None, panes=None) -> list[str]:
    """Cull-pass step: one look at every running session. Never raises."""
    now = time.time() if now is None else now
    done: list[str] = []
    for root in db.list_agents():
        if (root.parent_id or root.mode != "interactive" or root.provider != "claude"
                or root.status in ("paused", "done") or root.dismissed_at is not None
                or not agents.runs_process(root)):
            continue
        try:
            done += _session(db, root, now, panes)
        except Exception:  # noqa: BLE001 - culling must never break what calls it
            log.debug("login pause: session %s", root.id, exc_info=True)
    return done


def _session(db: DB, root: Agent, now: float, panes) -> list[str]:
    st = db.get_login_state(root.id)
    if st is not None and st.state == "auth_paused":
        session = _session_name(db, root)
        members = _members(db, root, panes)
        _interrupt(members, root, away=session is None or not user_active(session, now))
        culprit = db.get_agent(st.agent_id) if st.agent_id else None
        env = _env(db, culprit or root)
        if cloud_login.signing_in(env=env):
            # Wait on the sign-in, and look as soon as it's over.
            db.set_login_state(root.id, next_check=now, delay=FIRST_DELAY)
            return []
        return _recheck(db, root, st, env, members, now) if now >= (st.next_check or 0) else []
    done: list[str] = []
    for a in _members(db, root, panes):
        env = _env(db, a)
        if not _on_cloud(env) or not _ended_on_expired_login(db, a):
            continue
        s = cloud_login.refresh(env=env)
        if s.expired:
            return done + _pause(db, root, a, s, now, panes)
        if s.ok and _send(db, a.id, "Your last request failed on the cloud login, which works "
                          "again. Continue where you left off."):
            done.append(f"renewed the cloud login {a.id} stopped on")
    if st is not None and now - (st.checked_at or 0) < CHECK_EVERY:
        return done
    env = _env(db, root)
    if not _on_cloud(env):
        return done
    db.set_login_state(root.id, checked_at=now)
    s = cloud_login.status(env=env)
    if s.expired or (s.ok and s.expires_at is not None and s.expires_at - now <= LOOKAHEAD):
        s = cloud_login.refresh(env=env)
        if s.expired:
            return done + _pause(db, root, root, s, now, panes)
        if s.ok:
            done.append(f"renewed {root.id}'s cloud login before it expired")
    return done


def _interrupt(members: list[Agent], root: Agent, away: bool) -> None:
    """Escape any turn running in the session: with the login expired, a turn
    can only fail. The person's own chat only while they're away: a turn there
    may be theirs."""
    claude = get_provider("claude")
    for a in members:
        if a.id == root.id and not away:
            continue
        screen = _screen(a)
        if screen and claude.screen_state(screen) == "busy":
            try:
                tmux.send_keys(a.tmux_window, "Escape", server=agents.server_of(a))
            except tmux.TmuxError:
                pass


def _pause(db: DB, root: Agent, culprit: Agent, s: cloud_login.LoginStatus,
           now: float, panes) -> list[str]:
    expired_at = s.expires_at if s.expires_at is not None and s.expires_at <= now else now
    when = time.strftime("%H:%M", time.localtime(expired_at))
    session = _session_name(db, root)
    active = session is not None and user_active(session, now)
    _interrupt(_members(db, root, panes), root, away=not active)
    # Every pane and session shares the one login: one sign-in renews it for all.
    how = cloud_login.sign_in(env=_env(db, culprit))
    todo = (f"finish the sign-in open in your browser (or run `{s.refresh_cmd}`)"
            if how in ("opened", "waiting") else f"run `{s.refresh_cmd}`")
    noticed = None
    # The chat notice goes out before the session holds its messages.
    # With no tmux session there is no bar to pin it in: the chat is all there is.
    if (active or session is None) and _send(db, root.id, f"The cloud login expired at {when} and needs you to renew "
                        f"it: {todo}. Messages to this session's agents wait "
                        "until it's back; brindle keeps checking and says when to continue."):
        noticed = "chat"
    elif session is not None:
        tmux.set_alert(session, f"cloud login expired {when}: {todo.replace('`', '')}", "alert")
        noticed = "bar"
    db.set_login_state(root.id, state="auth_paused", agent_id=culprit.id,
                       refresh_cmd=s.refresh_cmd, expired_at=expired_at, noticed=noticed,
                       next_check=now + FIRST_DELAY, delay=FIRST_DELAY)
    return [f"paused {root.id}: its cloud login expired and needs `{s.refresh_cmd}` "
            f"(told by {noticed or 'nothing'})"]


def _recheck(db: DB, root: Agent, st: LoginState, env: dict[str, str], members: list[Agent],
             now: float) -> list[str]:
    s = cloud_login.refresh(env=env)
    if not s.ok:
        # No second sign-in: one that was abandoned or failed waits for the
        # person (cloud_login.sign_in says "tried" for the same lapse).
        delay = min((st.delay or FIRST_DELAY) * 2, MAX_DELAY)
        db.set_login_state(root.id, next_check=now + delay, delay=delay)
        return []
    session = _session_name(db, root)
    if st.noticed == "bar" and session is not None:
        tmux.set_alert(session, "")
    db.set_login_state(root.id, state="ok", checked_at=now, agent_id=None, refresh_cmd=None,
                       expired_at=None, noticed=None, next_check=None, delay=None)
    # Claude Code builds a new API client, which reads the credentials afresh,
    # for every request: a pane needs no restart, only a turn. Agents whose
    # turn ended on the error get one (held messages are a turn of their own).
    stopped = [a for a in members if a.id != root.id and _ended_on_expired_login(db, a)]
    for a in agents.tree(db, root.id):
        if db.pending_count(a.id):
            try:
                agents.flush(db, a.id)
            except (agents.AgentError, tmux.TmuxError):
                pass    # stays queued; its Stop hook hands it over
    for a in stopped:
        _send(db, a.id, "Cloud credentials restored: continue where you left off.")
    _send(db, root.id, "Cloud credentials restored: continue where you left off. "
          "Messages held while the login was expired are delivered.")
    return [f"cloud login restored for {root.id}: resumed"]
