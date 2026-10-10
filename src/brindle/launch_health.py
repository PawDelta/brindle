"""Observe the real first task; never generate a test request.

A live process is pending, not evidence of model access. Only fresh, explicitly
marked CLI/API errors are classified. Transcripts, prompt text and unmarked
prose are not evidence. This deliberately misses unfamiliar/wrapped errors;
the existing readiness, watch and cull paths continue to handle later problems.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from brindle import model_access, tmux

FOLLOWUP_SECONDS = 5.0
_LAUNCH_ENV = frozenset({"BRINDLE_AGENT_ID", "BRINDLE_ROOT_PATH", "BRINDLE_WORKSPACE_PATH",
                         "BRINDLE_WORKSPACE_NAME", "BRINDLE_WORKSPACE_ID", "BRINDLE_BRANCH",
                         "BRINDLE_BASE_BRANCH", "BRINDLE_PORT_BASE", "BRINDLE_TMUX_SOCKET",
                         "PWD", "TMUX", "TMUX_PANE"})
AUTH = model_access.LAUNCH_AUTH
MODEL = model_access.LAUNCH_MODEL
CAPACITY = model_access.LAUNCH_CAPACITY


def fingerprint(profile, env) -> str:
    # No raw credentials in refusal storage. Omit per-worker launch bookkeeping.
    identity = {k: v for k, v in env.items() if k not in _LAUNCH_ENV}
    return hashlib.sha256(json.dumps([profile.provider, profile.model, profile.base_url,
                                     profile.api, profile.api_key_env, sorted(identity.items()),
                                     account_revision(profile.provider, env)]).encode()).hexdigest()


def account_revision(provider: str, env) -> str | None:
    """Opaque local login/config revision; stat files, never read auth contents."""
    home = Path(env.get("HOME", str(Path.home())))
    paths = []
    if provider == "codex":
        root = Path(env.get("CODEX_HOME", str(home / ".codex")))
        paths = [root / "auth.json", root / "config.toml"]
    elif provider == "claude":
        root = Path(env.get("CLAUDE_CONFIG_DIR", str(home / ".claude")))
        paths = [root / ".credentials.json", root / "settings.json",
                 Path(env.get("AWS_SHARED_CREDENTIALS_FILE", str(home / ".aws" / "credentials"))),
                 Path(env.get("AWS_CONFIG_FILE", str(home / ".aws" / "config")))]
    elif provider == "antigravity":
        paths = [Path(env.get("BRINDLE_AGY_SETTINGS", str(home / ".gemini" / "antigravity-cli" / "settings.json")))]
    revisions = []
    for path in paths:
        try:
            st = path.stat()
            revisions.append((str(path), st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            pass
    return hashlib.sha256(json.dumps(revisions).encode()).hexdigest() if revisions else None


def prepare(db, agent, fp: str) -> None:
    if agent.parent_id and agent.task:
        db.conn.execute("INSERT OR REPLACE INTO launch_health (agent_id,pane,fingerprint) VALUES (?,?,?)",
                        (agent.id, agent.tmux_window, fp))


def delivered(db, agent, baseline: str = "") -> None:
    """Called only after delivery of the real task, once per launch."""
    from brindle import agents
    from brindle.providers import brindle_invocation

    changed = db.conn.execute("UPDATE launch_health SET delivered_at=?, baseline=?, pane=? "
                              "WHERE agent_id=? AND delivered_at IS NULL",
                              (time.time(), baseline, agent.tmux_window or os.environ.get("TMUX_PANE", ""), agent.id)).rowcount
    if changed:
        agents._detach([*brindle_invocation(), "_launch-health", agent.id])


def capture(agent) -> str:
    from brindle import agents

    try:
        return tmux.capture(agent.tmux_window, lines=80, server=agents.server_of(agent))
    except tmux.TmuxError:
        return ""


def before_delivery(db, agent) -> str:
    row = db.conn.execute("SELECT delivered_at FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    return capture(agent) if row and row["delivered_at"] is None else ""


def failure(screen: str, *, baseline: str = "", task: str = ""):
    """Require an error marker, then classify only its line (never user prose)."""
    old = {line.strip() for line in baseline.splitlines()}
    text = re.sub(r"\x1b\[[0-9;]*m", "", screen)
    # Resumed TUIs can render old replies. Only the latest marked response
    # can be current; a newer successful/busy reply supersedes an old error.
    replies = list(re.finditer(r"(?m)^\s*[●■]", text))
    if replies:
        text = text[replies[-1].start():]
    if re.search(r"(?i)esc to (?:interrupt|cancel)|thinking|working\s*…", text):
        return None
    # A Claude user turn or Codex/agy input block isn't a provider response.
    in_prompt = False
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if re.match(r"^[❯>]\s", stripped):
            in_prompt = True
        elif stripped.startswith(("●", "■")):
            in_prompt = False
        if in_prompt or in_code or stripped in old or (stripped and stripped in task):
            continue
        if not re.match(r"^(?:●\s*API Error:|■\s*|(?:API )?Error:|ERROR\b|── brindle: the model endpoint failed:)", stripped):
            continue
        cause = model_access.classify(stripped)
        if cause:
            return cause
        low = stripped.lower()
        if re.search(r"\b(401|403)\b|unauthorized|authentication failed|invalid.api.key|not authenticated|access denied|permission denied", low):
            return AUTH
        if re.search(r"\b429\b|rate.limit|quota|capacity|overloaded|resource.exhausted|usage.limit", low):
            return CAPACITY
        if re.search(r"model.*(?:not found|does not exist|not available|not supported|unavailable)|unsupported model", low):
            return MODEL
    return None


def inspect(db, agent_id: str, *, wait: bool = True) -> str | None:
    """Run in a detached helper, five seconds after actual delivery."""
    from brindle import agents

    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (agent_id,)).fetchone()
    agent = db.get_agent(agent_id)
    if not row or not agent or row["outcome"] or agent.result is not None or agent.status in ("paused", "done"):
        return None
    if row["delivered_at"] is not None and wait:
        time.sleep(max(0.0, row["delivered_at"] + FOLLOWUP_SECONDS - time.time()))
    agent = db.get_agent(agent_id)
    if (not agent or agent.result is not None or agent.status in ("paused", "done")
            or (row["pane"] and agent.tmux_window != row["pane"]) or not agents.same_server(agent)):
        return None
    alive = agents.is_alive(agent)
    cause = failure(capture(agent), baseline=row["baseline"], task=agent.task or "")
    if alive and row["delivered_at"] is None and not cause:
        return None
    if alive and row["delivered_at"] is not None and not wait and time.time() < row["delivered_at"] + FOLLOWUP_SECONDS:
        return None
    outcome = "failed" if cause else "exited" if not alive else "pending"
    if not db.conn.execute("UPDATE launch_health SET outcome=? WHERE agent_id=? AND outcome IS NULL AND pane=?",
                           (outcome, agent_id, row["pane"])).rowcount:
        return None
    ws = db.get_workspace(agent.workspace_id)
    if cause and ws:
        model_access.refuse(agent.profile, cause, repo_root=ws.repo_root, fp=row["fingerprint"])
    detail = (cause.line() if cause else
              "the worker exited before launch health could be established. Check its pane and CLI installation/login, then retry." if not alive else
              "the process is live; model access is still pending and has not been verified. Watch the worker for its result or later errors.")
    # Never forward provider output, which can contain URLs, keys or prompt text.
    body = f"Worker {agent.id} launch health: {detail}"
    try:
        agents.send_message(db, agent.parent_id, body)
    except agents.AgentError:
        db.enqueue(agent.parent_id, body, None)
    return outcome
