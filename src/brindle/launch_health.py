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
import tomllib
import uuid
from dataclasses import replace
from pathlib import Path

from brindle import model_access, tmux
from brindle.pro import route_health

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
    identity = {k: v for k, v in env.items() if k not in _LAUNCH_ENV or k == profile.api_key_env}
    return hashlib.sha256(json.dumps([profile.provider, profile.model, profile.base_url,
                                     profile.api, profile.api_key_env, sorted(identity.items()),
                                     account_revision(profile.provider, env)]).encode()).hexdigest()


def account_revision(provider: str, env) -> str | None:
    """Opaque login/config revision; auth files are statted, never read."""
    home = Path(env.get("HOME", str(Path.home())))
    paths = []
    if provider == "codex":
        root = Path(env.get("CODEX_HOME", str(home / ".codex")))
        paths = [root / "auth.json"]
    elif provider == "claude":
        root = Path(env.get("CLAUDE_CONFIG_DIR", str(home / ".claude")))
        paths = [root / ".credentials.json", root / "settings.json",
                 Path(env.get("AWS_SHARED_CREDENTIALS_FILE", str(home / ".aws" / "credentials"))),
                 Path(env.get("AWS_CONFIG_FILE", str(home / ".aws" / "config")))]
    elif provider == "antigravity":
        paths = [Path(env.get("BRINDLE_AGY_SETTINGS", str(home / ".gemini" / "antigravity-cli" / "settings.json")))]
    revisions = []
    if provider == "codex":
        revisions.append((str(root / "config.toml"), _codex_config_revision(root / "config.toml")))
    for path in paths:
        try:
            st = path.stat()
            revisions.append((str(path), st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            pass
    return hashlib.sha256(json.dumps(revisions).encode()).hexdigest() if revisions else None


def _codex_config_revision(path: Path) -> str:
    # Trust acceptance rewrites config.toml during startup. Hash its semantic
    # configuration, removing only trust/UI fields, so provider/auth changes
    # still invalidate refusals without reading the auth file itself.
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError:
        try:
            st = path.stat()
            return hashlib.sha256(str((st.st_ino, st.st_size, st.st_mtime_ns)).encode()).hexdigest()
        except OSError:
            return "unreadable"
    try:
        config = tomllib.loads(raw.decode())
    except (ValueError, UnicodeError):
        return hashlib.sha256(raw).hexdigest()
    for key in ("tui", "notice"):
        config.pop(key, None)
    projects = config.get("projects")
    if isinstance(projects, dict):
        for name, project in list(projects.items()):
            if isinstance(project, dict):
                project.pop("trust_level", None)
                if not project:
                    del projects[name]
        if not projects:
            del config["projects"]
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()


def prepare(db, agent, fp: str, *, argv_task: bool = False, submitted_prompt: str = "") -> None:
    if agent.parent_id and agent.task:
        agent._launch_health_token = uuid.uuid4().hex
        db.conn.execute("INSERT OR REPLACE INTO launch_health (agent_id,token,pane,fingerprint,argv_task,submitted_prompt) "
                        "VALUES (?,?,?,?,?,?)",
                        (agent.id, agent._launch_health_token, "", fp, argv_task, submitted_prompt))


def bind(db, agent, pane: str) -> None:
    """Publish the newly created pane, never the pane retained on resume.

    A hook/runner may deliver before new_window returns. Whichever update
    completes pane binding plus delivery schedules the one follow-up.
    """
    token = getattr(agent, "_launch_health_token", None)
    if not token:
        return
    row = db.conn.execute("UPDATE launch_health SET pane=? WHERE agent_id=? AND token=? AND pane='' "
                          "RETURNING delivered_at, outcome", (pane, agent.id, token)).fetchone()
    if row and row["delivered_at"] is not None and row["outcome"] is None:
        _followup(agent.id, token)


def _followup(agent_id: str, token: str) -> None:
    from brindle import agents
    from brindle.providers import brindle_invocation

    agents._detach([*brindle_invocation(), "_launch-health", agent_id, "--token", token])


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
    row = db.conn.execute("SELECT token FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    expected = getattr(agent, "_launch_health_token", None)
    if row is None or (expected is not None and expected != row["token"]):
        return
    changed = db.conn.execute("UPDATE launch_health SET delivered_at=?, baseline=? "
                              "WHERE agent_id=? AND delivered_at IS NULL AND token=? AND outcome IS NULL "
                              "AND EXISTS (SELECT 1 FROM agents WHERE id=? AND result IS NULL "
                              "AND status NOT IN ('paused','done')) RETURNING pane",
                              (time.time(), baseline, agent.id, row["token"], agent.id)).fetchone()
    if changed and changed["pane"]:
        _followup(agent.id, row["token"])


def capture(agent) -> str:
    from brindle import agents

    try:
        return tmux.capture(agent.tmux_window, lines=80, server=agents.server_of(agent))
    except tmux.TmuxError:
        return ""


def before_delivery(db, agent) -> str:
    row = db.conn.execute("SELECT token, delivered_at, pane FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    # Bind this delivery before capture/paste/IPC can race with a resume,
    # including the absence of a launch observation at this point.
    agent._launch_health_token = row["token"] if row else ""
    if not row or row["delivered_at"] is not None:
        return ""
    if row["pane"]:
        # A runner may have loaded its Agent before publication, including
        # the previous tmux server. Refresh that metadata along with the pane.
        published = db.get_agent(agent.id)
        return capture(replace(published, tmux_window=row["pane"])) if published else ""
    if os.environ.get("BRINDLE_AGENT_ID") == agent.id and os.environ.get("TMUX_PANE"):
        # The runner is already inside the new pane, but its launcher has not
        # published that pane yet. Never capture the old resumed pane.
        return capture(replace(agent, tmux_window=os.environ["TMUX_PANE"], tmux_server=tmux.current_server()))
    return ""


def failure(screen: str, *, baseline: str = "", task: str = "", submitted_prompt: str = "",
            provider: str | None = None):
    """Read complete response context before accepting a provider error block."""
    return _failure(screen, baseline=baseline, task=task, submitted_prompt=submitted_prompt,
                    provider=provider)[0]


def _failure(screen: str, *, baseline: str = "", task: str = "", submitted_prompt: str = "",
             provider: str | None = None):
    """(the cause, the provider's error block it was read from, or "")."""
    old = {line.strip() for line in baseline.splitlines()}
    text = re.sub(r"\x1b\[[0-9;]*m", "", screen)
    lines = text.splitlines()
    has_codex_ui = provider in (None, "codex") and bool(re.search(r"OpenAI Codex|\b\d+% context left", text))
    codex_ui = provider == "codex" or has_codex_ui
    provider = provider or ("codex" if codex_ui else None)
    context = "ui" if has_codex_ui else "startup"
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
    claude_banner = False
    candidate = None
    error_block = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if composer_start <= index <= footer:
            continue
        if context == "submitted" and stripped and (
                " ".join(stripped.split()) in prompt_text
                or (provider in ("native", "claude") and stripped in old)):
            continue  # submitted task/runner preview may itself contain fences or error markers
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
            context, candidate = "assistant", None
            continue
        if in_code or not stripped:
            continue
        # Startup chrome is not assistant prose. Recognize the compact logo
        # and boxed welcome banners across Claude versions, but never let a
        # copied banner escape an assistant/tool response or a code fence.
        if provider in (None, "claude") and context in ("startup", "ui") and not stripped.startswith("── brindle:"):
            if re.match(r"^(?:(?:[▐▛▜▝▘▗▖]|╭).*?)?Claude Code v\d", stripped):
                claude_banner = stripped.startswith("╭")
                context = "ui"
                continue
            if claude_banner:
                if stripped.startswith("╰"):
                    claude_banner = False
                if stripped.startswith(("│", "├", "╰")):
                    continue
            if re.match(r"^(?:[▐▛▜▝▘▗▖]|[─╭╰]|\? for shortcuts|⏵⏵)", stripped):
                context = "ui"
                continue
        if provider in (None, "native", "claude") and context in ("startup", "ui", "submitted", "provider"):
            if re.match(r"^brindle: (?:native worker \S+ on .+ at .+|resumed the conversation \(\d+ messages\))$", stripped):
                context = "ui"
                continue
            if re.match(r"^── brindle: turn \d+ ──$", stripped) and stripped in old:
                context, candidate = "submitted", None
                continue
            if stripped in ("── brindle: turn finished ──", "── brindle: waiting for messages ──",
                            "── brindle: turn finished; waiting for messages ──"):
                context, candidate = "assistant", None
                continue
        if re.match(r"^[❯>›]\s+\S", stripped):
            if context in ("assistant", "prose"):
                continue  # quoted prompts cannot authenticate a new turn
            prompt = re.sub(r"^[❯>›]\s+", "", stripped)
            context, candidate = ("submitted" if " ".join(prompt.split()) in prompt_text else "prompt"), None
            continue
        if " ".join(stripped.split()) in prompt_text:
            continue  # wrapped profile/task text is not provider output
        if re.fullmatch(r"(?:[•·]\s+)?(?:Working|Thinking)\s*…?\s*"
                        r"\((?:\d+[hms]\s*)+[•·]\s*esc to (?:interrupt|cancel)\)", stripped, re.I):
            context, candidate = (context if context in ("assistant", "prose", "prompt") else "ui"), None
            continue
        if codex_ui and re.match(r"^(?:•\s|(?:Tool output|Ran |Explored |Example output))", stripped):
            context, candidate = "assistant", None
            continue
        if re.fullmatch(r"(?i)esc to (?:interrupt|cancel)|thinking(?:…|\.{3})?|working\s*…", stripped):
            context, candidate = (context if context in ("assistant", "prose", "prompt") else "ui"), None
            continue
        if stripped.startswith(("●", "⏺")) and len(line) - len(line.lstrip()) <= 1:
            # A tool result or assistant explanation is not a provider error.
            if context in ("assistant", "prose", "prompt"):
                continue  # copied provider markers remain part of that block
            context = "provider" if provider in (None, "claude") and re.match(r"^[●⏺]\s*API Error:", stripped) else "assistant"
            candidate = _cause(stripped) if context == "provider" and stripped not in old else None
            error_block = stripped if context == "provider" and stripped not in old else ""
        elif (len(line) - len(line.lstrip()) <= 1
              and re.match(r"^■\s*(?:unexpected status|(?:API )?Error\b)", stripped)):
            if provider in (None, "codex") and context in ("startup", "provider", "ui", "submitted"):
                context = "provider"
                candidate = _cause(stripped) if stripped not in old else None
                error_block = stripped if stripped not in old else ""
        elif (not line[:1].isspace() and (context in ("startup", "provider")
              or (provider == "claude" and context == "submitted"))
              and re.match(r"^(?:(?:API )?Error:|ERROR\b)", stripped)):
            context = "provider"
            candidate = _cause(stripped) if stripped not in old else None
            error_block = stripped if stripped not in old else ""
        elif (provider in (None, "native") and not line[:1].isspace() and context not in ("assistant", "prompt", "prose")
              and stripped.startswith("── brindle: the model endpoint failed:")):
            context = "provider"
            candidate = _cause(stripped) if stripped not in old else None
            error_block = stripped if stripped not in old else ""
        elif context == "provider" and len(line) - len(line.lstrip()) >= 2:
            # Provider-rendered hard wraps retain the authenticated error
            # block; ordinary prose and assistant/tool markers end it below.
            if error_block:
                error_block += " " + stripped
                candidate = _cause(error_block)
        elif context in ("ui", "submitted") and not re.match(r"^(?:[╭╰│─]|OpenAI Codex(?:\s+\(v[^)]+\))?$|model:|directory:|\d+% context left|\? for shortcuts)", stripped):
            context = "assistant"
        elif context == "startup":
            context = "prose"
        elif context == "provider" and not re.match(r"^(?:[╭╰│─]|\d+% context left|\? for shortcuts|[❯›]\s*$)", stripped):
            # Unmarked prose after an error is ambiguous (a recovery, example,
            # or tool output). Do not keep an earlier candidate authoritative.
            context, candidate = "prose", None
    return candidate, (error_block if candidate else "")


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
    if (not row or not row["pane"] or not agent or (token is not None and token != row["token"])
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
            or (row["pane"] and agent.tmux_window != row["pane"]) or not agents.same_server(agent)
            or not agents.owns_pane(db, agent)):
        return None
    if row["delivered_at"] is not None and not wait and time.time() < row["delivered_at"] + FOLLOWUP_SECONDS:
        return None
    alive = agents.is_alive(agent)
    cause, error_block = _failure(capture(agent), baseline=row["baseline"], task=agent.task or "",
                                  submitted_prompt=row["submitted_prompt"], provider=agent.provider)
    if alive and row["delivered_at"] is None and not cause:
        return None
    if not agents.owns_pane(db, agent):
        return None
    outcome = "failed" if cause else "exited" if not alive else "pending"
    if not db.conn.execute("UPDATE launch_health SET outcome=? WHERE agent_id=? AND outcome IS NULL "
                           "AND pane=? AND token=? AND delivered_at IS ? "
                           "AND EXISTS (SELECT 1 FROM agents WHERE id=? AND result IS NULL "
                           "AND status NOT IN ('paused','done') AND tmux_window IS ?)",
                           (outcome, agent_id, row["pane"], row["token"], row["delivered_at"],
                            agent_id, agent.tmux_window)).rowcount:
        return None
    if outcome == "pending":
        return outcome  # diagnostics only; do not spend a supervisor turn on a live process
    ws = db.get_workspace(agent.workspace_id)
    if cause and ws:
        model_access.refuse(agent.profile, cause, repo_root=ws.repo_root, fp=row["fingerprint"])
        refused = route_health.provider_outcome(error_block)   # the status code only, never the text
        if refused:
            route, model = agents.route_context(db, agent, ws)
            route_health.record(ws.repo_root, agent=agent.provider, route=route, model=model,
                                outcome=refused)
    detail = (cause.line() if cause else
              "the worker exited before launch health could be established. Check its pane and CLI installation/login, then retry.")
    # Never forward provider output, which can contain URLs, keys or prompt text.
    body = f"Worker {agent.id} launch health: {detail}"
    try:
        agents.send_message(db, agent.parent_id, body)
    except agents.AgentError:
        db.enqueue(agent.parent_id, body, None)
    return outcome
