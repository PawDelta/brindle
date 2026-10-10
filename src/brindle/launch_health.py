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
import uuid
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


def prepare(db, agent, fp: str, *, argv_task: bool = False, submitted_prompt: str = "") -> None:
    if agent.parent_id and agent.task:
        agent._launch_health_token = uuid.uuid4().hex
        db.conn.execute("INSERT OR REPLACE INTO launch_health (agent_id,token,pane,fingerprint,argv_task,submitted_prompt) "
                        "VALUES (?,?,?,?,?,?)",
                        (agent.id, agent._launch_health_token, agent.tmux_window or "", fp, argv_task, submitted_prompt))


def accepted(db, agent) -> None:
    """An argv task reaches the CLI only after startup/trust or prompt-submit."""
    row = db.conn.execute("SELECT argv_task FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    if row and row["argv_task"]:
        delivered(db, agent)


def helper_token(agent) -> list[str]:
    token = getattr(agent, "_launch_health_token", None)
    return ["--token", token] if token else []


def current(db, agent_id: str, token: str | None) -> bool:
    row = db.conn.execute("SELECT token FROM launch_health WHERE agent_id=?", (agent_id,)).fetchone()
    return token is None or bool(row and row["token"] == token)


def delivered(db, agent, baseline: str = "") -> None:
    """Called only after delivery of the real task, once per launch."""
    from brindle import agents
    from brindle.providers import brindle_invocation

    row = db.conn.execute("SELECT token FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    expected = getattr(agent, "_launch_health_token", None)
    if row is None or (expected is not None and expected != row["token"]):
        return
    changed = db.conn.execute("UPDATE launch_health SET delivered_at=?, baseline=?, pane=? "
                              "WHERE agent_id=? AND delivered_at IS NULL AND token=?",
                              (time.time(), baseline, agent.tmux_window or os.environ.get("TMUX_PANE", ""), agent.id, row["token"])).rowcount
    if changed:
        agents._detach([*brindle_invocation(), "_launch-health", agent.id, "--token", row["token"]])


def capture(agent) -> str:
    from brindle import agents

    try:
        return tmux.capture(agent.tmux_window, lines=80, server=agents.server_of(agent))
    except tmux.TmuxError:
        return ""


def before_delivery(db, agent) -> str:
    row = db.conn.execute("SELECT delivered_at FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    return capture(agent) if row and row["delivered_at"] is None else ""


def failure(screen: str, *, baseline: str = "", task: str = "", submitted_prompt: str = ""):
    """Read complete response context before accepting a provider error block."""
    old = {line.strip() for line in baseline.splitlines()}
    text = re.sub(r"\x1b\[[0-9;]*m", "", screen)
    lines = text.splitlines()
    codex_ui = bool(re.search(r"OpenAI Codex|\b\d+% context left", text))
    context = "ui" if codex_ui else "startup"
    prompt_text = " ".join((submitted_prompt or task).split())
    # The last composer plus its footer is UI, including dim placeholders and
    # multiline drafts. It is not a new submitted turn superseding an error.
    composers = [i for i, line in enumerate(lines) if re.match(r"^\s*›", line)]
    composer_start = composers[-1] if composers else len(lines)
    footer = next((i for i in range(composer_start + 1, len(lines))
                   if re.match(r"^\s*(?:\d+% context left|\? for shortcuts)", lines[i])), len(lines))
    submitted = (composer_start < len(lines) and
                 " ".join(re.sub(r"^\s*›\s*", "", lines[composer_start]).split()) in prompt_text
                 and bool(re.sub(r"^\s*›\s*", "", lines[composer_start]).strip()))
    if footer == len(lines) or (submitted and any(re.match(r"^\s*[•●■]", line) for line in lines[composer_start + 1:footer])):
        composer_start = len(lines)
    in_code = False
    candidate = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if composer_start <= index <= footer:
            continue
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code or not stripped:
            continue
        if re.match(r"^[❯>›]\s+\S", stripped):
            prompt = re.sub(r"^[❯>›]\s+", "", stripped)
            context, candidate = ("submitted" if " ".join(prompt.split()) in prompt_text else "prompt"), None
            continue
        if " ".join(stripped.split()) in prompt_text:
            continue  # wrapped profile/task text is not provider output
        if codex_ui and re.match(r"^(?:•\s|(?:Tool output|Ran |Explored |Example output))", stripped):
            context, candidate = "assistant", None
            continue
        if re.search(r"(?i)esc to (?:interrupt|cancel)|^thinking|^working\s*…", stripped):
            context, candidate = ("assistant" if context == "assistant" else "ui"), None
            continue
        if stripped.startswith("●") and len(line) - len(line.lstrip()) <= 1:
            # A tool result or assistant explanation is not a provider error.
            context = "provider" if re.match(r"^●\s*API Error:", stripped) else "assistant"
            candidate = _cause(stripped) if context == "provider" and stripped not in old else None
        elif (len(line) - len(line.lstrip()) <= 1
              and re.match(r"^■\s*(?:unexpected status|(?:API )?Error\b)", stripped)):
            if context in ("startup", "provider", "ui", "submitted"):
                context = "provider"
                candidate = _cause(stripped) if stripped not in old else None
        elif (not line[:1].isspace() and context in ("startup", "provider")
              and re.match(r"^(?:(?:API )?Error:|ERROR\b)", stripped)):
            context = "provider"
            candidate = _cause(stripped) if stripped not in old else None
        elif (not line[:1].isspace() and context not in ("assistant", "prompt")
              and stripped.startswith("── brindle: the model endpoint failed:")):
            context = "provider"
            candidate = _cause(stripped) if stripped not in old else None
        elif context in ("ui", "submitted") and not re.match(r"^(?:[╭╰│─]|OpenAI Codex|model:|directory:|\d+% context left|\? for shortcuts)", stripped):
            context = "assistant"
        elif context == "startup":
            context = "prose"
    return candidate


def _cause(line: str):
    cause = model_access.classify(line)
    if cause:
        return cause
    low = line.lower()
    if re.search(r"\b(401|403)\b|unauthorized|authentication failed|invalid.api.key|not authenticated|access denied|permission denied", low):
        return AUTH
    if re.search(r"\b429\b|rate.limit|quota|capacity|overloaded|resource.exhausted|usage.limit", low):
        return CAPACITY
    if re.search(r"model.*(?:not found|does not exist|not available|not supported|unavailable)|unsupported model", low):
        return MODEL
    return None


def inspect(db, agent_id: str, *, wait: bool = True, token: str | None = None) -> str | None:
    """Run in a detached helper, five seconds after actual delivery."""
    from brindle import agents

    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (agent_id,)).fetchone()
    agent = db.get_agent(agent_id)
    if (not row or not agent or (token is not None and token != row["token"])
            or row["outcome"] or agent.result is not None or agent.status in ("paused", "done")):
        return None
    if row["delivered_at"] is not None and wait:
        time.sleep(max(0.0, row["delivered_at"] + FOLLOWUP_SECONDS - time.time()))
    latest = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (agent_id,)).fetchone()
    if (not latest or latest["token"] != row["token"]
            or latest["delivered_at"] != row["delivered_at"] or latest["outcome"]):
        return None
    agent = db.get_agent(agent_id)
    if (not agent or agent.result is not None or agent.status in ("paused", "done")
            or (row["pane"] and agent.tmux_window != row["pane"]) or not agents.same_server(agent)):
        return None
    alive = agents.is_alive(agent)
    cause = failure(capture(agent), baseline=row["baseline"], task=agent.task or "",
                    submitted_prompt=row["submitted_prompt"])
    if alive and row["delivered_at"] is None and not cause:
        return None
    if alive and row["delivered_at"] is not None and not wait and time.time() < row["delivered_at"] + FOLLOWUP_SECONDS:
        return None
    outcome = "failed" if cause else "exited" if not alive else "pending"
    if not db.conn.execute("UPDATE launch_health SET outcome=? WHERE agent_id=? AND outcome IS NULL "
                           "AND pane=? AND token=? AND delivered_at IS ?",
                           (outcome, agent_id, row["pane"], row["token"], row["delivered_at"])).rowcount:
        return None
    if outcome == "pending":
        return outcome  # diagnostics only; do not spend a supervisor turn on a live process
    ws = db.get_workspace(agent.workspace_id)
    if cause and ws:
        model_access.refuse(agent.profile, cause, repo_root=ws.repo_root, fp=row["fingerprint"])
    detail = (cause.line() if cause else
              "the worker exited before launch health could be established. Check its pane and CLI installation/login, then retry.")
    # Never forward provider output, which can contain URLs, keys or prompt text.
    body = f"Worker {agent.id} launch health: {detail}"
    try:
        agents.send_message(db, agent.parent_id, body)
    except agents.AgentError:
        db.enqueue(agent.parent_id, body, None)
    return outcome
