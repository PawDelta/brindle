"""Observe the real first task; never generate a test request.

A live process is pending, not evidence of model access. Only fresh, explicitly
marked CLI/API errors are classified. Transcripts, prompt text and unmarked
prose are not evidence. This deliberately misses unfamiliar/wrapped errors;
the existing readiness, watch and cull paths continue to handle later problems.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import sys
import time
import tomllib
import uuid
from dataclasses import replace
from pathlib import Path

from brindle import model_access, tmux
from brindle.pro import route_health

FOLLOWUP_SECONDS = 5.0
# A visible composer can precede MCP/session initialization on a loaded host.
# Give each submission the same budget as the Codex MCP connection before
# assuming it was swallowed; a busy model turn itself needs no such wait.
DELIVERY_SECONDS = 120.0
_LAUNCH_ENV = frozenset({"BRINDLE_AGENT_ID", "BRINDLE_ROOT_PATH", "BRINDLE_WORKSPACE_PATH",
                         "BRINDLE_WORKSPACE_NAME", "BRINDLE_WORKSPACE_ID", "BRINDLE_BRANCH",
                         "BRINDLE_BASE_BRANCH", "BRINDLE_PORT_BASE", "BRINDLE_TMUX_SOCKET",
                         "PWD", "TMUX", "TMUX_PANE"})
AUTH = model_access.LAUNCH_AUTH
MODEL = model_access.LAUNCH_MODEL
CAPACITY = model_access.LAUNCH_CAPACITY
# Claude Code's own line for a --model its backend doesn't serve (a 404), which
# carries no "API Error:" marker: "There's an issue with the selected model (...)".
# Vertex says "The model X is not available on your vertex deployment." instead.
SELECTED_MODEL = (r"(?i:there.s an issue with the selected model"
                  r"|the model \S+ is not available on your \w+ deployment)")


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
        db.conn.execute("INSERT OR REPLACE INTO launch_health (agent_id,token,pane,fingerprint,argv_task,submitted_prompt,launched_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (agent.id, agent._launch_health_token, "", fp, argv_task, submitted_prompt,
                         int(time.time() * 1000) / 1000))   # the log names' resolution


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
    """A turn hook confirms acceptance, including a pasted first task."""
    row = db.conn.execute("SELECT argv_task, submitted_prompt FROM launch_health WHERE agent_id=?",
                          (agent.id,)).fetchone()
    if row and (row["argv_task"] or row["submitted_prompt"]):
        delivered(db, agent)


def verify_delivery(db, agent, text: str | None = None, baseline: str | None = None,
                    lead: str | None = None) -> None:
    """Schedule one verification without blocking the caller or a sweep."""
    from brindle import agents
    from brindle.providers import brindle_invocation

    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    if not row or (text is None and not row["argv_task"]):
        return
    expected = getattr(agent, "_launch_health_token", None)
    token = row["token"] if expected is None else expected
    lead = lead if lead is not None else agents.message_lead(db, agent, None)
    if not db.conn.execute("UPDATE launch_health SET verifying=1, delivery_lead=?, baseline=COALESCE(?,baseline), "
                           "submitted_prompt=COALESCE(?,submitted_prompt) "
                           "WHERE agent_id=? AND token=? "
                           "AND verifying=0 AND delivered_at IS NULL AND outcome IS NULL",
                           (lead, baseline, text, agent.id, token)).rowcount:
        return
    agents._detach([*brindle_invocation(), "_launch-health", agent.id, "--token", token, "--verify"])


def _turn_running(provider, screen: str, baseline: str, prompt: str) -> bool:
    """Recognize acceptance without requiring Codex's turn-complete notify."""
    if provider.screen_state(screen) == "busy" and provider.paste_blocked(screen, True) != "typing":
        return True
    if provider.name != "codex":
        return False
    plain = provider.ANSI_SGR.sub("", screen)
    old = set(provider.ANSI_SGR.sub("", baseline).splitlines())
    lines = plain.splitlines()
    # A submitted brief can be the last › on screen while Codex works.
    # paste_blocked deliberately treats everything after it as a possible
    # draft; delivery can instead use fresh, structured turn output.
    prompts = [i for i, line in enumerate(lines) if provider.INPUT_LINE.match(line)]
    prompt_text = " ".join(prompt.split())
    matching = [i for i in prompts
                if (heading := " ".join(provider.INPUT_LINE.match(lines[i]).group(1).split()))
                and prompt_text.startswith(heading)]
    if not matching:
        return False
    # A resume may replay old tool output with no baseline at all. Only
    # output following this brief, before the next composer/turn, proves
    # acceptance. Choose the last match so a repeated brief still in the
    # composer cannot inherit evidence from an earlier submission.
    start = matching[-1]
    end = next((i for i in prompts if i > start), len(lines))
    for line in lines[start + 1:end]:
        if line in old or " ".join(line.split()) in prompt_text:
            continue
        # Match Codex's own row markers, not bare keyboard-help prose.
        if re.fullmatch(r"• Working \([^\n]*[Ee]sc to interrupt\)", line):
            return True
        # Any fresh bullet is the model's own output after the brief: tool
        # rows (Ran, Edited, Explored...) or analysis, with or without Working.
        if re.match(r"^• \S", line):
            return True
    return False


def verify_pending(db, agent_id: str, token: str | None) -> None:
    """Detached turn observation; retry once, fenced to this launch.

    State 1 is scheduled, 2 is running. A duplicate helper cannot take over
    an existing observation, clear its input or submit another copy.
    """
    from brindle import agents
    from brindle.providers import get_provider

    row = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (agent_id,)).fetchone()
    agent = db.get_agent(agent_id)
    if not row or not agent or token != row["token"]:
        return
    if not db.conn.execute("UPDATE launch_health SET verifying=2 WHERE agent_id=? AND token=? "
                           "AND verifying=1 AND delivered_at IS NULL AND outcome IS NULL", (agent_id, token)).rowcount:
        return
    agent._launch_health_token = token
    text, baseline, lead = row["submitted_prompt"] or agent.task, row["baseline"], row["delivery_lead"]
    provider = get_provider(agent.provider)

    def pending():
        latest = db.conn.execute("SELECT * FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
        fresh = db.get_agent(agent.id)
        if (not latest or latest["token"] != token or latest["outcome"]
                or latest["delivered_at"] is not None or not fresh or fresh.result is not None
                or fresh.status in ("paused", "done", "failed") or fresh.tmux_window != row["pane"]
                or not agents.owns_pane(db, fresh)):
            return None
        return fresh

    for attempt in range(2):
        deadline = time.time() + DELIVERY_SECONDS
        while True:
            fresh = pending()
            if fresh is None:
                return
            screen = capture(fresh)
            if (_turn_running(provider, screen, baseline, text)
                    or failure(screen, baseline=baseline, task=agent.task or "",
                               submitted_prompt=text, provider=agent.provider)):
                delivered(db, agent, baseline)
                if provider.tool_permission(screen):
                    _tell(db, agent, _approval_body(agent, screen))
                return
            if time.time() >= deadline:
                break
            time.sleep(0.5)
        if attempt == 0:
            # Never clear a menu, a permission dialog or a different session.
            from brindle.prompt_answer import brief_draft
            draft = provider.paste_blocked(screen, True) == "typing" or brief_draft(screen, text)
            if (not provider.input_ready(screen) and not draft
                    or provider.tool_permission(screen) or provider.paste_blocked(screen, False)):
                break
            if draft:
                provider.clear_input(fresh.tmux_window, server=agents.server_of(fresh))
                # Clearing is a TUI event too. Do not append a duplicate to
                # a frozen partial composer if it never handles that event.
                for _ in range(10):
                    time.sleep(0.5)
                    fresh = pending()
                    if fresh is None:
                        return
                    screen = capture(fresh)
                    if _turn_running(provider, screen, baseline, text):
                        delivered(db, agent, baseline)
                        return
                    if provider.input_ready(screen) and not provider.paste_blocked(screen, True):
                        break
                else:
                    break
            tmux.paste(fresh.tmux_window, text, lead=lead, server=agents.server_of(fresh))
    asking = provider.tool_permission(screen)
    if not db.conn.execute("UPDATE launch_health SET outcome=? WHERE agent_id=? AND token=? "
                           "AND delivered_at IS NULL AND outcome IS NULL",
                           ("awaiting-approval" if asking else "delivery-failed", agent.id, token)).rowcount:
        return
    if asking:
        # A tool approval only exists inside a running turn: never fail the worker.
        body = _approval_body(agent, screen)
    else:
        db.update_agent(agent.id, status="failed")
        body = (f"Worker {agent.id} brief delivery failed: no turn was confirmed and automatic "
                "re-delivery is exhausted or blocked by its pane. Its brief may be missing or left unsubmitted in the input box. "
                "Check its pane and restart the worker; this is not a startup prompt.")
    _tell(db, agent, body)


def _tell(db, agent, body: str) -> None:
    from brindle import agents

    try:
        agents.send_message(db, agent.parent_id, body)
    except agents.AgentError:
        db.enqueue(agent.parent_id, body, None)


def _approval_body(agent, screen: str) -> str:
    """What a working agent is asking to run, for its supervisor."""
    from brindle import prompt_answer

    plain = prompt_answer.plain(screen).splitlines()
    at = next((i for i, line in enumerate(plain) if "Would you like to" in line), None)
    asked = " ".join(" ".join(plain[at + 1:at + 6]).split())[:300] if at is not None else ""
    return (f"Worker {agent.id} is working but waiting at an approval prompt"
            + (f": {asked}" if asked else "") + ". Answer it in its pane; the brief was not lost.")


def helper_token(agent) -> list[str]:
    token = getattr(agent, "_launch_health_token", None)
    return ["--token", token] if token else []


def current(db, agent_id: str, token: str | None) -> bool:
    row = db.conn.execute("SELECT token FROM launch_health WHERE agent_id=?", (agent_id,)).fetchone()
    return token is None or bool(row and row["token"] == token)


def delivered(db, agent, baseline: str | None = None) -> None:
    """Called only after delivery of the real task, once per launch."""
    row = db.conn.execute("SELECT token FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    expected = getattr(agent, "_launch_health_token", None)
    if row is None or (expected is not None and expected != row["token"]):
        return
    changed = db.conn.execute("UPDATE launch_health SET delivered_at=?, baseline=COALESCE(?,baseline) "
                              "WHERE agent_id=? AND delivered_at IS NULL AND token=? AND outcome IS NULL "
                              "AND EXISTS (SELECT 1 FROM agents WHERE id=? AND result IS NULL "
                              "AND status NOT IN ('paused','done')) RETURNING pane",
                              (time.time(), baseline, agent.id, row["token"], agent.id)).fetchone()
    if changed and changed["pane"]:
        _followup(agent.id, row["token"])


def capture(agent) -> str:
    from brindle import agents

    try:
        return tmux.capture(agent.tmux_window, lines=80, escapes=True, server=agents.server_of(agent))
    except tmux.TmuxError:
        return ""


def before_delivery(db, agent, text: str | None = None) -> str:
    row = db.conn.execute("SELECT token, delivered_at, pane FROM launch_health WHERE agent_id=?", (agent.id,)).fetchone()
    # Bind this delivery before capture/paste/IPC can race with a resume,
    # including the absence of a launch observation at this point.
    agent._launch_health_token = row["token"] if row else ""
    if not row or row["delivered_at"] is not None:
        return ""
    if text is not None:
        # Publish what the startup observer must recognize before the paste
        # can become visible, including briefs delivered from the queue.
        db.conn.execute("UPDATE launch_health SET submitted_prompt=? WHERE agent_id=? AND token=? "
                        "AND delivered_at IS NULL AND outcome IS NULL", (text, agent.id, row["token"]))
    if row["pane"]:
        # A runner may have loaded its Agent before publication, including
        # the previous tmux server. Refresh that metadata along with the pane.
        published = db.get_agent(agent.id)
        baseline = capture(replace(published, tmux_window=row["pane"])) if published else ""
    elif os.environ.get("BRINDLE_AGENT_ID") == agent.id and os.environ.get("TMUX_PANE"):
        # The runner is already inside the new pane, but its launcher has not
        # published that pane yet. Never capture the old resumed pane.
        baseline = capture(replace(agent, tmux_window=os.environ["TMUX_PANE"], tmux_server=tmux.current_server()))
    else:
        baseline = ""
    db.conn.execute("UPDATE launch_health SET baseline=? WHERE agent_id=? AND token=? AND delivered_at IS NULL",
                    (baseline, agent.id, row["token"]))
    return baseline


def failure(screen: str, *, baseline: str = "", task: str = "", submitted_prompt: str = "",
            provider: str | None = None):
    """Read complete response context before accepting a provider error block."""
    return _failure(screen, baseline=baseline, task=task, submitted_prompt=submitted_prompt,
                    provider=provider)[0]


def _failure(screen: str, *, baseline: str = "", task: str = "", submitted_prompt: str = "",
             provider: str | None = None):
    """(the cause, the provider's error block it was read from, or "")."""
    old = {line.strip() for line in re.sub(r"\x1b\[[0-9;]*m", "", baseline).splitlines()}
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
            context = ("provider" if provider in (None, "claude")
                       and re.match(r"^[●⏺]\s*(?:API Error:|" + SELECTED_MODEL + ")", stripped) else "assistant")
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
              and re.match(r"^(?:(?:API )?Error:|ERROR\b|" + SELECTED_MODEL + ")", stripped)):
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
    if re.search(r"model.*(?:not found|does not exist|not available|not supported|unavailable)|unsupported model"
                 r"|" + SELECTED_MODEL, low):
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
    agents._note_reviewer_end(db, agent, ws)
    if cause and ws:
        model_access.refuse(agent.profile, cause, repo_root=ws.repo_root, fp=row["fingerprint"])
        if cause.key == MODEL.key:  # permanent for this profile; AUTH/CAPACITY may be transient
            db.update_agent(agent.id, status="failed")
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


def mcp_log_dir(worktree: str) -> Path:
    """Where Claude Code writes the brindle MCP server's per-start logs."""
    cache = (Path.home() / "Library" / "Caches" if sys.platform == "darwin"
             else Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"))
    return cache / "claude-cli-nodejs" / re.sub(r"[^A-Za-z0-9]", "-", worktree) / "mcp-logs-brindle"


def _log_started(name: str) -> float | None:
    m = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d)T(\d\d)-(\d\d)-(\d\d)(?:-(\d+))?Z\.jsonl", name)
    if not m:
        return None
    *whole, frac = m.groups()
    # Without a fraction the stamp is only good to the second: assume the end of it.
    return (calendar.timegm(tuple(int(g) for g in whole) + (0, 0, 0))
            + (int(frac) / 10 ** len(frac) if frac else 0.999))


def mcp_failed(worktree: str, session_id: str, launched_at: float) -> bool:
    """Did the brindle MCP server fail to start for this session since this launch?

    Reads only the explicit error rows Claude Code logs; a missing directory or
    an unfamiliar format reads as healthy, and nothing here raises.
    """
    try:
        for path in mcp_log_dir(worktree).iterdir():
            started = _log_started(path.name)
            if started is None or started < launched_at:
                continue
            for line in path.read_text(errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict) and row.get("sessionId") == session_id
                        and isinstance(row.get("error"), str) and row["error"]):
                    return True
    except (OSError, ValueError, TypeError):
        pass
    return False


def has_mcp_failure(db, agent, ws) -> bool:
    """A recorded failure survives closing the agent; also check fresh logs."""
    row = db.conn.execute("SELECT outcome, launched_at FROM launch_health WHERE agent_id=?",
                          (agent.id,)).fetchone()
    return bool(row and (row["outcome"] == "mcp-failed" or (
        agent.provider == "claude" and agent.session_ref and row["launched_at"] is not None
        and mcp_failed(ws.path, agent.session_ref, row["launched_at"]))))


def retry_reviewer(db, agent, ws):
    """Called under the review dispatch lock, after checking for live work."""
    from brindle import agents

    db.conn.execute("UPDATE launch_health SET outcome='mcp-failed' WHERE agent_id=?", (agent.id,))
    claimed = db.conn.execute("INSERT OR IGNORE INTO review_mcp_retries (workspace_id, sha) VALUES (?,?)",
                              (ws.id, agent.review_sha)).rowcount
    agents.close(db, agent.id)
    if not claimed:
        raise agents.AgentError(
            f"Reviewer {agent.id}'s brindle MCP connection failed at {agent.review_sha[:8]}; "
            "the one automatic retry for this commit has been used. The reviewer was stopped. "
            "Check the MCP startup logs and CLI installation; automatic recovery is exhausted.")
    try:
        # Preserve the exact task (focus and check results included), profile,
        # parent and commit, but start a new conversation and MCP connection.
        replacement = agents.spawn(db, ws, agent.profile, prompt=agent.task, parent_id=agent.parent_id,
                                   mode="review", background_setup=True, review_sha=agent.review_sha)
        if agent.review_slot is not None:
            db.update_agent(replacement.id, review_slot=agent.review_slot)
    except Exception as exc:
        raise agents.AgentError(
            f"Reviewer {agent.id} was stopped after its brindle MCP connection failed; "
            f"the automatic retry could not start ({type(exc).__name__}). Check launch configuration.") from exc
    # This attempt is over once a replacement exists. Keep its failure log for
    # diagnostics, but never let it compete with later reviews or helpers.
    db.conn.execute("UPDATE review_claims SET agent_id=? WHERE workspace_id=? AND agent_id=?",
                    (replacement.id, ws.id, agent.id))   # the commit's one reviewer now
    db.set_result(agent.id, f"Superseded by reviewer {replacement.id} after MCP startup failed.")
    return replacement


def check_mcp(db, agent) -> None:
    """From tool-done: recover reviewers, or tell the supervisor once."""
    from brindle import agents

    if agent.provider != "claude" or not agent.session_ref or not agent.parent_id:
        return
    row = db.conn.execute("SELECT launched_at FROM launch_health WHERE agent_id=? "
                          "AND (outcome IS NULL OR outcome='pending')", (agent.id,)).fetchone()
    ws = db.get_workspace(agent.workspace_id)
    if not row or row["launched_at"] is None or not ws:
        return
    if not mcp_failed(ws.path, agent.session_ref, row["launched_at"]):
        return
    if not db.conn.execute("UPDATE launch_health SET outcome='mcp-failed' WHERE agent_id=? "
                           "AND (outcome IS NULL OR outcome='pending')", (agent.id,)).rowcount:
        return
    if agent.mode == "review" and agent.review_sha:
        from brindle.providers import brindle_invocation

        # Stopping the pane also kills its hook. This helper must outlive it.
        agents._detach_orphan([*brindle_invocation(), "_recover-reviewer", agent.id])
        return
    body = (f"Worker {agent.id} launch health: its brindle MCP server failed to connect, so it has no "
            "report_result tools and cannot finish. Stop it with `brindle agent kill "
            f"{agent.id}`, then assign a fresh worker in its workspace.")
    try:
        agents.send_message(db, agent.parent_id, body)
    except agents.AgentError:
        db.enqueue(agent.parent_id, body, None)


def recover_reviewer(db, agent_id: str) -> None:
    """Read recovery state under the same lock as replacement publication."""
    from brindle import agents

    agent = db.get_agent(agent_id)
    if agent:
        with agents.review_dispatch_lock(agent.workspace_id):
            _recover_reviewer(db, agent_id)


def _recover_reviewer(db, agent_id: str) -> None:
    """Detached from the broken CLI; refresh state after acquiring the lock."""
    from brindle import agents, gates

    agent = db.get_agent(agent_id)
    ws = db.get_workspace(agent.workspace_id) if agent else None
    if not agent or not ws or agent.result is not None or not has_mcp_failure(db, agent, ws):
        return
    if gates.head(ws) != agent.review_sha:
        agents.close(db, agent.id)
        body = (f"Reviewer {agent.id}'s brindle MCP connection failed and it was stopped. "
                "The branch has moved; request a review of its current commit.")
    else:
        try:
            agents.request_review(db, db.get_agent(agent.parent_id), ws, agent.profile)
            return
        except agents.AgentError as exc:
            body = str(exc)
    try:
        agents.send_message(db, agent.parent_id, body)
    except agents.AgentError:
        db.enqueue(agent.parent_id, body, None)
