"""Lapsed cloud logins: renewed in the background where the CLI can, else one
browser sign-in, every session on that login paused until it's back, and then
everything picks up where it was.

Part of the cull sweep (``cull.sweep``). For each running session (a root agent
and everything it started, interactive or not) whose Claude Code agents use a
cloud route (Bedrock, Vertex, Foundry; see ``cloud_login``):

* A login within ``LOOKAHEAD`` of expiry, or an agent whose last reply ended
  on an expired-login API error, gets a silent refresh
  (``cloud_login.refresh``). Nobody is told; an agent stopped on the error is
  told to continue. Each distinct login among the session's agents is looked
  at, not only the root's.
  A pane's own credential errors only prompt that refresh: a pane also fails
  on a signed-out login, an unreachable token endpoint or a half-written
  credentials file, none of which a sign-in fixes. Only the CLI saying the
  login lapsed (``LoginStatus.expired``) counts.
* When only the person can renew it (Google's invalid_rapt, a lapsed AWS SSO
  session) the session goes ``auth_paused``: autopilot doesn't nudge
  (``autopilot.on_stop``), messages to its agents stay queued (``holding``),
  and a turn that starts anyway (a Claude Code cron) is interrupted with
  Escape; in the person's own chat only while they're away. Its root shows
  that work is paused: pinned in its status bar until the login is back, and
  once in an interactive root's chat.
* The browser sign-in is ``cloud_login.sign_in``'s: one tab machine-wide,
  opened when the person is at brindle, and replaced only on their input
  after it died. Each sweep of a paused session gives it the chance.
* While paused it re-checks with backoff (``FIRST_DELAY`` doubling up to
  ``MAX_DELAY``), and as soon as a sign-in exits. Once the login works the bar
  clears, held messages are delivered, every agent whose turn ended on the
  error or was escaped is told to continue, and the supervisor is told (and
  warned when the new login is another account). Claude Code reads the
  credentials afresh for every request, so no pane needs a restart.

Headless workers wait out a lapse in their runner (``agents.run_headless``).

State is the ``login_state`` row of the session's root.
"""

from __future__ import annotations

import json
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


def _last_input() -> float | None:
    try:
        return tmux.last_input()
    except tmux.TmuxError:
        return None


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


def _logins(db: DB, root: Agent, members: list[Agent]) -> list[tuple[Agent, dict[str, str]]]:
    """One agent (and its env) per distinct cloud login in the session."""
    seen, out = set(), []
    for a in [root, *(m for m in members if m.id != root.id)]:
        if a.provider != "claude":
            continue
        env = _env(db, a)
        key = cloud_login.login_key({**os.environ, **env}) if _on_cloud(env) else None
        if key is not None and key not in seen:
            seen.add(key)
            out.append((a, env))
    return out


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
    last_input = _last_input()
    done: list[str] = []
    for root in db.list_agents():
        if (root.parent_id
                or root.status in ("paused", "done") or root.dismissed_at is not None
                or not agents.runs_process(root)):
            continue
        try:
            done += _session(db, root, now, panes, last_input)
        except Exception:  # noqa: BLE001 - culling must never break what calls it
            log.debug("login pause: session %s", root.id, exc_info=True)
    return done


def _session(db: DB, root: Agent, now: float, panes, last_input: float | None) -> list[str]:
    st = db.get_login_state(root.id)
    members = _members(db, root, panes)
    if st is not None and st.state == "auth_paused":
        return _paused(db, root, st, members, now, last_input)
    done: list[str] = []
    for a in members:
        env = _env(db, a)
        if a.headless or not _on_cloud(env) or not _ended_on_expired_login(db, a):
            continue
        s = cloud_login.refresh(env=env)
        if s.expired:
            return done + _pause(db, root, a, s, env, members, now, last_input)
        if s.ok and _send(db, a.id, "Your last request failed on the cloud login, which works "
                          "again. Continue where you left off."):
            done.append(f"renewed the cloud login {a.id} stopped on")
    if st is not None and now - (st.checked_at or 0) < CHECK_EVERY:
        return done
    db.set_login_state(root.id, checked_at=now)
    for a, env in _logins(db, root, members):
        s = cloud_login.status(env=env)
        if s.expired or (s.ok and s.expires_at is not None and s.expires_at - now <= LOOKAHEAD):
            s = cloud_login.refresh(env=env)
            if s.expired:
                return done + _pause(db, root, a, s, env, members, now, last_input)
            if s.ok:
                done.append(f"renewed {a.id}'s cloud login before it expired")
    return done


def _away(db: DB, root: Agent, now: float) -> bool:
    """Nobody is at this session's chat: a turn in it isn't theirs."""
    session = _session_name(db, root)
    return root.mode != "interactive" or session is None or not user_active(session, now)


def _interrupt(members: list[Agent], root: Agent, away: bool) -> list[str]:
    """Escape any turn running in the session: with the login lapsed, a turn
    can only fail. The person's own chat only while they're away: a turn there
    may be theirs. Returns who was escaped: each gets a turn back once the
    login works. A headless runner waits the lapse out itself."""
    claude = get_provider("claude")
    escaped = []
    for a in members:
        if a.headless or (a.id == root.id and not away):
            continue
        screen = _screen(a)
        if screen and claude.screen_state(screen) == "busy":
            try:
                tmux.send_keys(a.tmux_window, "Escape", server=agents.server_of(a))
            except tmux.TmuxError:
                continue
            escaped.append(a.id)
    return escaped


def _ids(text: str | None) -> list[str]:
    try:
        ids = json.loads(text or "[]")
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def _todo(how: str, cmd: str) -> str:
    if not cmd:
        return "refresh the configured cloud credential file"
    if how == "manual":
        return f"run `{cmd}`"
    return f"sign in with the browser tab brindle opens (or run `{cmd}`)"


def _pause(db: DB, root: Agent, culprit: Agent, s: cloud_login.LoginStatus, env: dict[str, str],
           members: list[Agent], now: float, last_input: float | None) -> list[str]:
    # Every pane and session shares the one login: one sign-in renews it for all.
    how = cloud_login.sign_in(env=env, now=now, last_input=last_input)
    if how == "no lapse":
        return []   # renewed meanwhile: nothing to pause for
    expired_at = s.expires_at if s.expires_at is not None and s.expires_at <= now else now
    when = time.strftime("%H:%M", time.localtime(expired_at))
    escaped = _interrupt(members, root, away=_away(db, root, now))
    todo = _todo(how, s.refresh_cmd)
    session = _session_name(db, root)
    told = []
    # Pinned until the login is back, so a glance at any session says why
    # nothing moves; an interactive root's chat also hears it once. The chat
    # notice goes out before the session holds its messages.
    if session is not None:
        tmux.set_alert(session, f"PAUSED: cloud login expired {when}; {todo.replace('`', '')}",
                       "alert")
        told.append("bar")
    if root.mode == "interactive" and _send(
            db, root.id, f"Work is paused: the cloud login expired at {when}. To renew it, {todo}. "
            "Messages to this session's agents wait until it's back; brindle keeps checking "
            "and says when to continue."):
        told.append("chat")
    db.set_login_state(root.id, state="auth_paused", agent_id=culprit.id,
                       refresh_cmd=s.refresh_cmd, expired_at=expired_at,
                       noticed="+".join(told) or None, next_check=now + FIRST_DELAY,
                       delay=FIRST_DELAY, interrupted=json.dumps(escaped),
                       account=cloud_login.account(env=env))
    return [f"paused {root.id}: its cloud login expired; {todo} "
            f"(sign-in {how}; told by {'+'.join(told) or 'nothing'})"]


def _paused(db: DB, root: Agent, st: LoginState, members: list[Agent], now: float,
            last_input: float | None) -> list[str]:
    escaped = _interrupt(members, root, away=_away(db, root, now))
    if escaped:
        st.interrupted = json.dumps(sorted({*_ids(st.interrupted), *escaped}))
        db.set_login_state(root.id, interrupted=st.interrupted)
    culprit = db.get_agent(st.agent_id) if st.agent_id else None
    env = _env(db, culprit or root)
    if cloud_login.signing_in(env=env):
        # Wait on the sign-in, and look as soon as it's over.
        db.set_login_state(root.id, next_check=now, delay=FIRST_DELAY)
        st.next_check = now
    elif now >= (st.next_check or 0):
        done = _recheck(db, root, st, env, members, now)
        if done:
            return done
    # The person may be back, or have typed since the last tab died.
    how = cloud_login.sign_in(env=env, now=now, last_input=last_input)
    if how == "no lapse":       # renewed some other way: pick up now
        return _recheck(db, root, st, env, members, now)
    return [f"opened the cloud sign-in for {root.id}"] if how == "opened" else []


def _recheck(db: DB, root: Agent, st: LoginState, env: dict[str, str], members: list[Agent],
             now: float) -> list[str]:
    s = cloud_login.refresh(env=env)
    if not s.ok:
        delay = min((st.delay or FIRST_DELAY) * 2, MAX_DELAY)
        db.set_login_state(root.id, next_check=now + delay, delay=delay)
        st.next_check, st.delay = now + delay, delay
        return []
    cloud_login.end_signin(env=env)     # a tab still open is no use now
    session = _session_name(db, root)
    if session is not None:
        tmux.set_alert(session, "")
    db.set_login_state(root.id, state="ok", checked_at=now, agent_id=None, refresh_cmd=None,
                       expired_at=None, noticed=None, next_check=None, delay=None,
                       interrupted=None, account=None)
    # Claude Code builds a new API client, which reads the credentials afresh,
    # for every request: a pane needs no restart, only a turn. Every agent
    # whose turn ended on the error or was escaped gets one; a held message is
    # a turn of its own, and an interactive root hears from us below.
    escaped = set(_ids(st.interrupted))
    stopped = [a for a in members
               if not a.headless and not (a.id == root.id and root.mode == "interactive")
               and not db.pending_count(a.id)
               and (a.id in escaped or _ended_on_expired_login(db, a))]
    for a in agents.tree(db, root.id):
        if db.pending_count(a.id):
            try:
                agents.flush(db, a.id)
            except (agents.AgentError, tmux.TmuxError):
                pass    # stays queued; its Stop hook hands it over
    for a in stopped:
        _send(db, a.id, "Cloud credentials restored: continue where you left off.")
    if root.mode == "interactive":
        text = ("Cloud credentials restored: continue where you left off. "
                "Messages held while the login was expired are delivered.")
        now_as = cloud_login.account(env=env)
        if st.account and now_as and now_as != st.account:
            text += (f" Note: the new sign-in is {now_as}, but the login that lapsed was "
                     f"{st.account}. If requests now fail on permissions, sign in as {st.account}.")
        _send(db, root.id, text)
    return [f"cloud login restored for {root.id}: resumed"]
