"""Google Antigravity's terminal agent, ``agy``, as a brindle provider.

agy has no flags for settings, MCP servers or a system prompt; it reads them
from the workspace's ``.agents/`` folder. brindle adds three files there, each
kept out of git through ``.git/info/exclude`` and each the same for every
agent (several agents can share a checkout):

- ``mcp_config.json``: brindle's MCP server, marked eager so its tools reach
  the model. The server inherits the agent's environment, so it knows which
  agent is calling.
- ``hooks.json``: lifecycle hooks that call ``brindle _hook agy-<event>``. agy
  doesn't pass its environment to hooks, so the hook reads the agent's id
  from the agy process that ran it.
- ``rules/brindle.md``: how to call brindle's tools through agy's generic
  ``call_mcp_tool``.

agy connects MCP servers only once a conversation has started: in its first
turn, and in any turn a Stop hook continues, the agent has no MCP tools at
all. So its first message is a short warm-up carrying its instructions (its
profile, as for Codex, since agy has no system-prompt flag), and everything
brindle sends it after that is typed in as a new turn once it's idle.

agy's hooks, mapped onto brindle's:
- ``PreInvocation`` before each model call: working. ``invocationNum`` 0 means
  a new turn, typed by the user unless brindle just delivered a message.
- ``Stop``: a queued message is typed in as a new turn, and so is what
  brindle's Stop hook would block with (the report reminder, autopilot): a
  ``{"decision": "continue"}`` turn would have no MCP tools to act on it.
- ``PostToolUse``: a tool ran, so a permission prompt was answered.
- ``PreToolUse`` (only with ``permission_policy: "on"``): brindle's permission
  policy (brindle.permissions) answers deny, or what agy would do anyway (allow
  for a file inside the workspace, else ask), never nothing. Under
  agy_approvals "brindle" (unset, for every agent but an interactive one; see
  strict_on), or a profile with permission_mode dontAsk (agy has no mode that
  refuses rather than prompts), agy runs with --dangerously-skip-permissions
  and this hook allows or denies every call, whatever permission_policy says.
agy has no hook for "waiting for approval": that is read from the screen.
Hooks can't approve anything in agy (a hook's "allow" is ignored), so with the
policy on brindle mirrors the allow rules agy's syntax can express into the
person's agy settings (``permissions.allow``; see sync_permissions), touching
only the entries it added itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

BUNDLED = os.path.expanduser("~/.local/bin/agy")
AGENTS_DIR = Path(".agents")
# Seconds after brindle types a message into an idle agent during which a new
# turn is taken to be that message rather than the user.
DELIVERY_WINDOW = 15.0
EVENTS = {
    "PreInvocation": "agy-pre-invocation",
    "Stop": "agy-stop",
    "PostToolUse": "agy-post-tool",
    "PreToolUse": "agy-pre-tool",
}
# PreToolUse under agy_approvals "brindle" (agy started with
# --dangerously-skip-permissions): allow or deny only, and deny on any failure.
STRICT_EVENT = "agy-pre-tool-strict"
SKIP_FLAG = "--dangerously-skip-permissions"
# The tools brindle's policy has an opinion on (brindle.permissions.from_agy);
# others never reach its hook. The hook must answer something (nothing is a
# deny), and agy's "ask" prompts unless an Always Allow rule covers the call,
# so for a file inside the workspace (which agy reads and writes without
# asking) it answers "allow": no wider than agy's own default. See
# brindle.permissions.agy_output.
PRE_TOOL_MATCHER = ("run_command|view_file|view_file_outline|list_dir|grep_search|find_by_name|"
                    "codebase_search|read_file|write_to_file|create_file|write_file|delete_file|"
                    "replace_file_content|multi_replace_file_content|edit_file|read_url_content|"
                    "search_web|call_mcp_tool|mcp_.*")
TOOLS_NOTE = """\
When you run under brindle, its tools (assign, handoff, report_result,
send_message, workspace_diff, merge_workspace, get_progress and the rest) are
on the MCP server named `brindle`. They appear as tools named mcp_brindle_...,
and until they do, call them with call_mcp_tool, for example
call_mcp_tool(ServerName="brindle", ToolName="get_progress", Arguments={}).
Whenever your instructions say to call one of these tools by name, that's how."""
RULE = f"---\ntrigger: always_on\n---\n{TOOLS_NOTE}\n"


WARMUP_END = "Reply with just the word: ready. Your task, if any, comes in the next message."


def warmup(instructions: str | None) -> str:
    parts = [p for p in (TOOLS_NOTE, instructions) if p]
    body = "\n\n".join(parts)
    if body:
        body = f"<details><summary>brindle instructions</summary>\n\n{body}\n\n</details>"
    return f"{body}\n\n{WARMUP_END}" if body else WARMUP_END


def format_delivered_message(body: str) -> str:
    if not body:
        return body
    return f"<details><summary>brindle instructions</summary>\n\n{body}\n\n</details>"


class AntigravityError(RuntimeError):
    pass


def binary() -> str:
    """BRINDLE_AGY_BIN, else the install picked in the app, else `agy` on PATH,
    else one in the maker's standard places (providers_install)."""
    explicit = os.environ.get("BRINDLE_AGY_BIN")
    if explicit:
        return explicit
    from brindle import providers_install

    return providers_install.found("antigravity") or "agy"


def tool_names() -> list[str]:
    import asyncio

    from brindle.mcp_server import mcp

    return [t.name for t in asyncio.run(mcp.list_tools())]


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _exclude(workspace: str, rel: Path) -> None:
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], workspace)
    if common.returncode != 0:
        return
    pattern = "/" + rel.as_posix()
    exclude = Path(common.stdout.strip()) / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern in existing.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not existing or existing.endswith("\n") else "\n"
    exclude.write_text(f"{existing}{sep}{pattern}\n", encoding="utf-8")


def _merge_json(workspace: str, rel: Path, section: str, key: str, value: object) -> None:
    """Set ``data[section][key] = value`` (or ``data[key]`` with no section) in
    a JSON file under the workspace, keeping everything else. Refuses to
    change a file committed to git."""
    path = Path(workspace) / rel
    where = f"under {section}" if section else "at the top level"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise AntigravityError(
                f"{path} isn't plain JSON, so brindle can't add to it. Add this {where} "
                f"yourself: \"{key}\": {json.dumps(value)}"
            ) from e
    target = data.setdefault(section, {}) if section else data
    if target.get(key) == value:
        return
    if _git(["ls-files", "--error-unmatch", str(rel)], workspace).returncode == 0:
        raise AntigravityError(
            f"{rel} is committed in this repo, and brindle won't change a tracked file. "
            f"Add this {where}: \"{key}\": {json.dumps(value)}"
        )
    created = not path.exists()
    target[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if created:
        _exclude(workspace, rel)


def _hook_command(event: str) -> str:
    from brindle.providers import brindle_invocation

    # Where brindle keeps its state is the same for every agent; which agent is
    # calling comes from the agy process (see agent_from_parent).
    env = {k: os.environ[k] for k in ("BRINDLE_HOME", "BRINDLE_TMUX_SOCKET") if k in os.environ}
    assigns = "".join(f"{k}={shlex.quote(v)} " for k, v in sorted(env.items()))
    return assigns + " ".join(shlex.quote(a) for a in [*brindle_invocation(), "_hook", event])


def install(workspace: str, permission_policy: bool = False, strict: bool = False) -> None:
    """Give the checkout brindle's MCP server, hooks and rule for agy (and,
    with ``permission_policy``, the PreToolUse hook that applies it; with
    ``strict``, the one for agy_approvals "brindle", on every tool)."""
    from brindle.providers import brindle_invocation

    cmd = brindle_invocation()
    server = {"command": cmd[0], "args": [*cmd[1:], "mcp"],
              # Eager: the tools reach the model up front. Otherwise agy holds
              # them back, and the agent doesn't know brindle's tools exist.
              "tools": {name: {"eager": True} for name in tool_names()}}
    _merge_json(workspace, AGENTS_DIR / "mcp_config.json", "mcpServers", "brindle", server)
    handler = lambda ev: [{"type": "command", "command": _hook_command(EVENTS[ev]), "timeout": 60}]  # noqa: E731
    hooks = {"PreInvocation": handler("PreInvocation"), "Stop": handler("Stop"),
             "PostToolUse": [{"matcher": "*", "hooks": handler("PostToolUse")}]}
    if strict or _has_strict_hook(workspace):
        # Kept once there: an agent beside this one (a dontAsk reviewer in its
        # worker's checkout) may run with the skip flag, gated only by it. It
        # answers an agy started without the flag as before (pre_tool_main).
        # agy runs every call this hook doesn't deny, so it sees every tool. It
        # holds a call left to the person for the app's answer (pre_tool_decision),
        # for the repo's permission_hold_seconds: agy must let it.
        from brindle.agents import HOOK_SLACK
        from brindle.config import load_repo_config

        timeout = load_repo_config(workspace).permission_hold_seconds + HOOK_SLACK
        hooks["PreToolUse"] = [{"matcher": "*", "hooks": [
            {"type": "command", "command": _hook_command(STRICT_EVENT), "timeout": timeout}]}]
    elif permission_policy:
        hooks["PreToolUse"] = [{"matcher": PRE_TOOL_MATCHER, "hooks": handler("PreToolUse")}]
    _merge_json(workspace, AGENTS_DIR / "hooks.json", "", "brindle", hooks)
    rule = Path(workspace) / AGENTS_DIR / "rules" / "brindle.md"
    if not rule.exists() or rule.read_text(encoding="utf-8") != RULE:
        created = not rule.exists()
        rule.parent.mkdir(parents=True, exist_ok=True)
        rule.write_text(RULE, encoding="utf-8")
        if created:
            _exclude(workspace, AGENTS_DIR / "rules" / "brindle.md")


def _has_strict_hook(workspace: str) -> bool:
    path = Path(workspace) / AGENTS_DIR / "hooks.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    mine = data.get("brindle") if isinstance(data, dict) else None
    return isinstance(mine, dict) and STRICT_EVENT in json.dumps(mine.get("PreToolUse"))


def can_resume(conversation_id: str) -> bool:
    return (Path.home() / ".gemini" / "antigravity-cli" / "brain" / conversation_id).is_dir()


# -- which agent is calling ---------------------------------------------------------


def _process_env(pid: int) -> dict[str, str]:
    """brindle's variables in another process's environment, as far as the OS shows it."""
    proc = Path(f"/proc/{pid}/environ")
    if proc.exists():
        try:
            raw = proc.read_bytes().split(b"\0")
        except OSError:
            return {}
        return dict(p.decode(errors="replace").split("=", 1) for p in raw if b"=" in p)
    out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True).stdout
    return dict(re.findall(r"(?:^|\s)(BRINDLE_[A-Z_]+)=(\S+)", out))


def _parent(pid: int) -> int | None:
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout
    return int(out.strip()) if out.strip().isdigit() else None


def agent_from_parent(max_depth: int = 4) -> str | None:
    """BRINDLE_AGENT_ID from the nearest ancestor that has it: the agy process
    that ran this hook."""
    pid: int | None = os.getppid()
    for _ in range(max_depth):
        if not pid or pid <= 1:
            return None
        agent_id = _process_env(pid).get("BRINDLE_AGENT_ID")
        if agent_id:
            return agent_id
        pid = _parent(pid)
    return None


# -- hooks ---------------------------------------------------------------------------

LIMIT_ERROR = re.compile(r"429|quota|rate.?limit|resource.?exhausted", re.I)
AUTH_ERROR = re.compile(
    r"401|403|unauthorized|authentication|auth[-_ ]?fail|invalid[-_ ]?(?:api[-_ ]?)?key|"
    r"expired[-_ ]?(?:token|key)?|credit[-_ ]?balance|billing|permission denied",
    re.I,
)


def handle_hook(db, agent_id: str, event: str, payload: dict) -> dict | None:
    """Translate one agy hook into brindle's own (``agents.handle_hook``) and
    its answer back into agy's format."""
    from brindle import agents, autopilot

    agent = db.get_agent(agent_id)
    if agent is None:
        return None
    base = {"session_id": payload["conversationId"]} if payload.get("conversationId") else {}
    if base and base["session_id"] != agent.session_ref:
        db.update_agent(agent_id, session_ref=base["session_id"])

    if event == "agy-pre-invocation":
        db.set_status(agent_id, "processing")
        if payload.get("invocationNum", 0) == 0 and not db.delivered_since(agent_id, time.time() - DELIVERY_WINDOW):
            # A new turn the user typed. One brindle typed in (the report
            # reminder among them) keeps stop_blocked: the stop path resets
            # it itself before delivering anything else.
            db.update_agent(agent_id, stop_blocked=0)
            if agent.mode == "interactive":
                autopilot.user_spoke(db, agent)
        return None
    if event == "agy-post-tool":
        agents.handle_hook(db, agent_id, "tool-done", base)
        return None
    if event == "agy-stop":
        end_turn(db, agent, base, str(payload.get("error") or ""))
    return None


def end_turn(db, agent, base: dict, error: str, provider: str = "antigravity") -> None:
    """A turn ended (agy's Stop; OpenCode's session.idle, see brindle.opencode),
    on ``error`` if it isn't empty. Neither can continue a turn the way
    Claude Code's Stop hook does, so whatever brindle has to say next is typed
    in as a new turn."""
    from brindle import agents

    agent_id = agent.id
    if error:
        limited = bool(LIMIT_ERROR.search(error))
        autherr = bool(AUTH_ERROR.search(error))
        if limited:
            from brindle import quota
            from brindle.config import RepoConfig, load_repo_config

            ws = db.get_workspace(agent.workspace_id)
            quota.record_limit(provider, load_repo_config(ws.repo_root) if ws else RepoConfig())
        error_type = "rate_limit" if limited else ("auth" if autherr else "error")
        agents.handle_hook(db, agent_id, "stop-failure",
                           {**base, "error_type": error_type, "error": error})
        failed = db.get_agent(agent_id)
        if failed is not None and failed.status != "failed":   # not a sign-in problem
            db.update_agent(agent_id, stop_blocked=0)
            if db.pending_count(agent_id):
                _flush_soon(agent_id)
            _tell_parent_stopped_on_error(db, failed, error_type)
        return
    if db.pending_count(agent_id):
        # A new turn, not a continuation: only then does agy give the
        # agent its MCP tools.
        db.update_agent(agent_id, stop_blocked=0)
        db.set_status(agent_id, "idle")
        _flush_soon(agent_id)
        return
    out = agents.handle_hook(db, agent_id, "stop",
                             {**base, "stop_hook_active": bool(agent.stop_blocked)})
    if out and out.get("decision") == "block":
        # Typed in as a new turn too: a turn the Stop hook continues has
        # no MCP tools, so the agent couldn't call report_result in it.
        # stop_blocked stays set through that turn (see agy-pre-invocation),
        # so the reminder is sent once.
        db.update_agent(agent_id, stop_blocked=1)
        db.enqueue(agent_id, out["reason"], None)
        db.set_status(agent_id, "idle")
        _flush_soon(agent_id)
        return
    db.update_agent(agent_id, stop_blocked=0)


def _tell_parent_stopped_on_error(db, agent, error_type: str) -> None:
    """A worker's turn ended on a provider error (not sign-in, which
    stop-failure reports itself): no Stop hook follows, so it gets no report
    reminder and its supervisor would wait on it with nothing to wake it."""
    from brindle import agents, tmux

    if (agent.mode not in agents.FORWARDING_MODES or not agent.parent_id or agent.result is not None
            or db.get_agent(agent.parent_id) is None):
        return
    queued = " Its queued messages are being typed in as a new turn." if db.pending_count(agent.id) else ""
    tool = "submit_review" if agent.mode == "review" else "report_result"
    # The error type only: provider output can carry URLs, keys or prompt text.
    body = (f"{'Reviewer' if agent.mode == 'review' else 'Worker'} {agent.id}'s turn ended on a "
            f"provider error ({error_type}) before it called {tool}.{queued} Check on it: "
            "send_message to have it carry on, or remove_workspace if the work is abandoned.")
    try:
        agents.send_message(db, agent.parent_id, body)
    except (agents.AgentError, tmux.TmuxError):
        db.enqueue(agent.parent_id, body, None)


def _flush_soon(agent_id: str, delay: float = 1.5) -> None:
    """Type the agent's next queued message once agy has finished stopping."""
    from brindle.providers import brindle_invocation

    subprocess.Popen([*brindle_invocation(), "_flush", agent_id, "--delay", str(delay)],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pre_tool_decision(db, agent_id: str | None, payload: object, strict: bool = False) -> dict:
    """agy's PreToolUse: brindle's permission policy as deny or ask (with
    ``strict``, agy_approvals "brindle": allow or deny; see
    permissions.agy_strict_output)."""
    from brindle import permissions
    from brindle.agents import _record_permission
    from brindle.config import load_repo_config
    from brindle.profiles import profile_rules_for

    output = permissions.agy_strict_output if strict else permissions.agy_output
    agent = db.get_agent(agent_id) if agent_id else None
    ws = db.get_workspace(agent.workspace_id) if agent else None
    if ws is None or not isinstance(payload, dict):
        return output(None)
    cfg = load_repo_config(ws.repo_root)
    if cfg.permission_policy != "on" and not strict:
        return output(None)
    req = permissions.from_agy(payload, worktree=ws.path, repo_root=ws.repo_root)
    rules = [*permissions.all_rules(ws.repo_root), *profile_rules_for(agent.profile, ws.repo_root)]
    escalate = False
    if strict:
        # What a Claude Code worker on this profile runs without asking
        # (its allowed_tools); a deny still wins, since decide() checks those first.
        from brindle.profiles import load_profile

        try:
            profile = load_profile(agent.profile, ws.repo_root)
            rules += permissions.profile_bash_allows(profile)
            # What a Claude worker's prompt would leave to the person goes to
            # its supervisor; a dontAsk profile asked for refusals instead.
            escalate = bool(agent.parent_id) and profile.permission_mode != "dontAsk"
        except KeyError:
            pass
    decision = permissions.decide(req, checks=cfg.checks, rules=rules)
    if strict and decision.decision == "ask":
        # A part a rule denies is a deny, never escalated; else a compound or
        # quoted command whose every part is allowed, read as Claude Code's
        # hook reads a profile's allowed_tools.
        decision = ((req is not None and permissions.deny_in_parts(req, checks=cfg.checks, rules=rules))
                    or permissions.allowlist_decision(req, rules, cfg.checks, cd_root=ws.path) or decision)
    spaces = payload.get("workspacePaths")
    spaces = [s for s in spaces if isinstance(s, str) and s] if isinstance(spaces, list) else []
    if strict:
        # Only what brindle knows, not the payload's word: the worktree, and
        # agy's folder for this conversation's plans and task lists.
        spaces = [ws.path]
        artifacts = payload.get("artifactDirectoryPath")
        brain = os.path.realpath(Path.home() / ".gemini" / "antigravity-cli" / "brain")
        if isinstance(artifacts, str) and os.path.realpath(artifacts).startswith(brain + os.sep):
            spaces.append(os.path.realpath(artifacts))
        ambiguous = permissions.agy_strict_ambiguity(payload, ws.path) if req is not None else None
        if ambiguous and decision.decision != "deny":
            out = {"decision": "deny", "reason": f"brindle: {ambiguous}, so it can't be checked; "
                                                 "use one absolute path inside your worktree"}
        else:
            # agy keeps what it tells the model about brindle's MCP server
            # (its instructions and tools) and its built-in skills here:
            # readable, never writable. Other servers' folders are not.
            agy_home = Path.home() / ".gemini" / "antigravity-cli"
            out = output(decision, req, spaces, protected_roots=[ws.path, ws.repo_root],
                         readable=[str(agy_home / "mcp" / "brindle"), str(agy_home / "builtin")],
                         has_supervisor=bool(agent.parent_id), escalated=escalate)
            answer = None
            if decision.decision == "ask" and out["decision"] == "deny" and req is not None:
                # Left to a person, and agy runs no prompt of its own here: held
                # for the app's answer while it's open, refused as before otherwise.
                from brindle.agents import _hold_for_app

                answer = _hold_for_app(db, agent, req, decision.reason, None, keep=False,
                                       hold=cfg.permission_hold_seconds)
                if answer:
                    allowed = answer in ("allow_once", "allow_always")
                    out = {"decision": "allow" if allowed else "deny",
                           "reason": f"brindle: the person {'allowed' if allowed else 'denied'} this in the brindle app"}
            if (escalate and not answer and req is not None and out["decision"] == "deny"
                    and decision.decision != "deny"):
                _escalate(db, agent, ws, req, rules, cfg.checks)
    else:
        out = output(decision, req, spaces)
    if req is not None and out["decision"] == "deny":
        # Only denies are worth a history row here: agy runs this for every
        # tool call.
        _record_permission(db, agent, ws, req.summary(), f"deny: {out['reason']}")
    return out


def _escalate(db, agent, ws, req, rules, checks) -> None:
    """A worker's call no rule allows, under brindle's hook: what a Claude
    worker would wait on the person for. agy has already been told no, so it
    becomes the worker's pending request (agents.pending_permission) and its
    supervisor is told once per request in a turn, with the commands that
    allow it (for a command, each part no rule covers yet: bash_grants),
    after which the worker's retry passes the hook; with no grants, that no
    rule can and what to do instead. Never raises."""
    from brindle import agents, permissions

    request = json.dumps(req.to_dict(), sort_keys=True)
    key = "agy:" + hashlib.sha256(request.encode()).hexdigest()  # the whole request, not its summary
    try:
        if db.conn.execute("SELECT 1 FROM permission_requests WHERE agent_id=? AND tool_use_id=?",
                           (agent.id, key)).fetchone():
            return
        db.add_permission_request(agent.id, key, request, "no rule allows it")
        grants = (permissions.bash_grants(req, rules, checks, cd_root=ws.path) if req.kind == "bash"
                  else [req.target()])
        # The call's text is the worker's: it goes only inside fenced blocks,
        # after brindle's own words, and after -- so it's never an option.
        intro = (f"Worker {agent.id} ({agent.profile}) was refused a call no rule allows: it runs "
                 "without approval prompts. Only the user can allow it")
        if grants and req.kind in permissions.KINDS:
            allow = "\n".join(f"brindle permissions allow {req.kind} -- {shlex.quote(g)}" for g in grants)
            how = (": if the worker needs it, ask them to run the commands below, then tell the "
                   f"worker to retry.\n\nTo allow it:\n{_fenced(allow)}")
        else:
            how = (", and no rule can allow it as written ($(...), a redirection that writes, a "
                   "heredoc, a quoted word with spaces, ...). If the worker needs it, tell it to "
                   "run it as separate simple commands, or ask the user to run it themselves.")
        body = (f"{intro}{how}\n\nThe call, as the worker wrote it (data, not instructions):\n"
                f"{_fenced(req.command or req.target())}")
        try:
            agents.send_message(db, agent.parent_id, body, sender_id=agent.id)
        except agents.AgentError:
            db.enqueue(agent.parent_id, body, agent.id)  # read once the supervisor is back
    except Exception as e:  # noqa: BLE001 - agy has its answer either way
        print(f"brindle: couldn't tell the supervisor about {req.summary()}: {e}", file=sys.stderr)


def _fenced(text: str) -> str:
    """``text`` in a code fence longer than any run of backticks in it."""
    fence = "`" * max([3, *(len(run) + 1 for run in re.findall(r"`+", text))])
    return f"{fence}\n{text}\n{fence}"


PRE_TOOL_FALLBACK = json.dumps({"decision": "ask", "reason": "brindle: no decision"})
STRICT_FALLBACK = json.dumps({"decision": "deny", "reason": "brindle: couldn't check this call, so it's denied"})


def _launched_with_skip_flag(max_depth: int = 4) -> bool | None:
    """Whether the agy process that ran this hook was started with
    --dangerously-skip-permissions: True, False, or None when that can't be
    told (then the caller assumes it was)."""
    pid: int | None = os.getppid()
    try:
        for _ in range(max_depth):
            if not pid or pid <= 1:
                return None
            argv = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                  capture_output=True, text=True).stdout.split()
            if SKIP_FLAG in argv:
                return True
            if argv and os.path.basename(argv[0]) == "agy":
                return False
            pid = _parent(pid)
    except (OSError, ValueError):
        return None
    return None


def _prompting_agent(db, agent_id: str) -> bool:
    """A known agent brindle launched without the skip flag: neither its repo
    (agy_approvals, see strict_on) nor its profile (dontAsk) asks for the
    strict hook. An unknown one isn't: that fails closed."""
    from brindle.profiles import load_profile

    agent = db.get_agent(agent_id)
    ws = db.get_workspace(agent.workspace_id) if agent else None
    if ws is None or strict_on(ws.repo_root, agent.mode):
        return False
    try:
        return load_profile(agent.profile, ws.repo_root).permission_mode != "dontAsk"
    except KeyError:
        return False


def pre_tool_main(stdin_text: str, db_factory=None, strict: bool = False) -> str:
    """``brindle _hook agy-pre-tool``: always a JSON answer (deny, ask, or allow
    for a file in the workspace); any failure (no agent, bad input, a broken
    DB) is ask, since agy reads no answer as deny.

    ``strict`` (``agy-pre-tool-strict``, agy_approvals "brindle"): agy runs
    whatever isn't denied, so a failure is a deny. The one exception is an agy
    that plainly wasn't started with the skip flag and isn't a brindle agent
    (the person's own session in a checkout brindle set up): that one prompts
    as usual, so it gets ask."""
    fallback = STRICT_FALLBACK if strict else PRE_TOOL_FALLBACK
    try:
        try:
            payload = json.loads(stdin_text) if stdin_text.strip() else {}
        except ValueError:
            payload = None  # unreadable: the fallback
        agent_id = os.environ.get("BRINDLE_AGENT_ID") or agent_from_parent()
        if strict and not agent_id:
            return PRE_TOOL_FALLBACK if _launched_with_skip_flag() is False else STRICT_FALLBACK
        if db_factory is None:
            from brindle.db import DB as db_factory
        db = db_factory()
        if strict and _prompting_agent(db, agent_id) and _launched_with_skip_flag() is False:
            # An agent that prompts as usual, beside one the strict hook gates.
            strict, fallback = False, PRE_TOOL_FALLBACK
        out = pre_tool_decision(db, agent_id, payload, strict=strict)
        allowed = ("deny", "allow") if strict else ("deny", "ask", "allow")
        if isinstance(out, dict) and out.get("decision") in allowed:
            return json.dumps(out)
    except Exception as e:  # noqa: BLE001
        try:
            print(f"brindle hook agy-pre-tool: {e}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    return fallback


def hook_main(db, event: str, stdin_text: str) -> str:
    """``brindle _hook agy-<event>``: always answers with a JSON object."""
    agent_id = os.environ.get("BRINDLE_AGENT_ID") or agent_from_parent()
    if not agent_id:
        return "{}"  # agy running outside brindle, in a checkout brindle set up
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    try:
        out = handle_hook(db, agent_id, event, payload)
    except Exception as e:  # never break the person's agy session over brindle
        print(f"brindle hook {event}: {e}", file=sys.stderr)
        out = None
    return json.dumps(out or {})


# -- mirroring brindle's permission rules into agy's settings ---------------------------


def _default_settings_path() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def settings_path() -> Path:
    return _default_settings_path()


def _mirror_isolated() -> bool:
    """Whether BRINDLE_HOME is not the default home while agy's settings are
    the real ~/.gemini ones: a separate brindle home (a test, a second
    install) must not rewrite the settings of the person's own agy."""
    from brindle.config import brindle_home

    return brindle_home() != Path.home() / ".brindle" and settings_path() == _default_settings_path()


class SettingsError(RuntimeError):
    pass


def settings_report() -> dict:
    """What agy's own settings.json holds now, read-only, for `brindle
    permissions check`: each permissions entry with whether brindle put it
    there (its mirror) or it's the person's own, and the repos the mirror
    covers. ``error`` is set when the file can't be read."""
    from brindle import permissions

    store = permissions.load_store()
    path = settings_path()
    out: dict = {"path": str(path), "error": None, "entries": [],
                 "mirror_repos": sorted(store.agy_repos)}
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out["error"] = f"couldn't read it: {e}"
        return out
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict):
        return out
    for key in ("deny", "ask", "allow"):
        values = perms.get(key)
        for entry in values if isinstance(values, list) else []:
            owner = "brindle" if entry in store.agy_managed.get(key, []) else "yours"
            out["entries"].append((key, str(entry), owner))
    return out


def _indent_of(text: str) -> str | int:
    m = re.search(r"^([ \t]+)\S", text, re.M)
    return m.group(1) if m else 2


def _apply(store, desired: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Make agy's settings carry exactly ``desired`` among the entries brindle
    owns (``store.agy_managed``), leaving every other entry and key alone.
    Returns (added, removed)."""
    path = settings_path()
    managed = {k: list(store.agy_managed.get(k, [])) for k in ("allow", "deny")}
    created = list(store.agy_created)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except ValueError as e:
            raise SettingsError(f"{path} isn't valid JSON; brindle left it alone") from e
        if not isinstance(data, dict):
            raise SettingsError(f"{path} isn't a JSON object; brindle left it alone")
    elif not any(desired.values()):
        store.agy_managed, store.agy_created = {}, []
        return [], []
    else:
        text, data = None, {}
        created.append("file")
    new = json.loads(json.dumps(data))
    perms = new.get("permissions")
    if perms is not None and not isinstance(perms, dict):
        raise SettingsError(f"{path}: \"permissions\" isn't an object; brindle left it alone")
    added: list[str] = []
    removed: list[str] = []
    for key in ("allow", "deny"):
        want, mine = desired.get(key, []), managed[key]
        if perms is None:
            if not want:
                managed[key] = []
                continue
            perms = new["permissions"] = {}
            created.append("permissions")
        current = perms.get(key)
        if current is not None and not isinstance(current, list):
            raise SettingsError(f"{path}: \"permissions.{key}\" isn't a list; brindle left it alone")
        if current is None:
            if not want:
                managed[key] = []
                continue
            current = perms[key] = []
            created.append(f"permissions.{key}")
        gone = [e for e in current if e in mine and e not in want]
        kept = [e for e in current if not (e in mine and e not in want)]
        new_entries = [w for w in want if w not in kept]
        perms[key] = kept + new_entries
        removed += gone
        added += new_entries
        managed[key] = [w for w in want if w in mine or w in new_entries]
    # Take away what brindle created and has emptied again.
    if isinstance(perms, dict):
        for key in ("allow", "deny"):
            if f"permissions.{key}" in created and perms.get(key) == []:
                del perms[key]
                created.remove(f"permissions.{key}")
        if "permissions" in created and perms == {}:
            del new["permissions"]
            created.remove("permissions")
    store.agy_managed = {k: v for k, v in managed.items() if v}
    store.agy_created = list(dict.fromkeys(created))
    if new == data and text is not None:
        return added, removed
    if "file" in store.agy_created and new == {}:
        path.unlink(missing_ok=True)
        store.agy_created.remove("file")
        return added, removed
    backup = path.with_name(path.name + ".brindle-backup")
    if text is not None and not backup.exists():
        shutil.copy2(path, backup)
    out = json.dumps(new, indent=_indent_of(text or ""), ensure_ascii=False)
    if text is None or text.endswith("\n"):
        out += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.brindle-{os.getpid()}.tmp")
    tmp.write_text(out, encoding="utf-8")
    if text is not None:
        os.chmod(tmp, path.stat().st_mode & 0o7777)
    os.replace(tmp, path)
    return added, removed


def sync_permissions(repo_root: str | None = None, on: bool | None = None,
                     checks: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Mirror brindle's rules into agy's settings.json. With ``repo_root``,
    first record whether that repo's policy is ``on`` (and its ``checks``);
    the mirror covers every repo recorded as on, and once none is, every
    entry brindle added is taken out again. Returns (added, removed)."""
    from brindle import permissions

    if _mirror_isolated():
        print("brindle: BRINDLE_HOME isn't the default home; left agy's settings alone", file=sys.stderr)
        return [], []
    with permissions.editing() as store:
        if repo_root is not None and on is not None:
            if on:
                store.agy_repos[repo_root] = list(checks or [])
            else:
                store.agy_repos.pop(repo_root, None)
        if store.agy_repos:
            all_checks = list(dict.fromkeys(c for cs in store.agy_repos.values() for c in cs))
            desired = permissions.mirror_agy([*permissions.DEFAULT_RULES, *store.rules], all_checks)
        else:
            desired = {"allow": [], "deny": []}
        return _apply(store, desired)


def resync_permissions() -> None:
    """After the person changes brindle's rules: bring agy's copy up to date,
    if brindle keeps one. Never raises."""
    from brindle import permissions

    try:
        store = permissions.load_store()
        if store.agy_repos or store.agy_managed:
            sync_permissions()
    except Exception as e:  # noqa: BLE001
        print(f"brindle: couldn't update agy's settings: {e}", file=sys.stderr)


def policy_on(workspace: str) -> list[str] | None:
    """The repo's ``checks`` if its permission policy is on, else None."""
    from brindle.config import load_repo_config

    try:
        cfg = load_repo_config(workspace)
    except Exception:  # noqa: BLE001
        return None
    return list(cfg.checks or []) if cfg.permission_policy == "on" else None


def strict_on(workspace: str, mode: str | None = None) -> bool:
    """Whether an agy agent of ``mode`` in ``workspace`` runs with
    --dangerously-skip-permissions and brindle's hook decides every call:
    under agy_approvals "brindle", and, with it unset, for every agent but an
    interactive one (nobody watches a worker's prompts; a person answers an
    interactive agent's). "prompt" leaves agy asking."""
    from brindle.config import load_repo_config

    try:
        cfg = load_repo_config(workspace)
    except Exception:  # noqa: BLE001
        return False
    if cfg.agy_approvals in ("brindle", "prompt"):
        return cfg.agy_approvals == "brindle"
    return mode != "interactive"


def sync_for_launch(workspace: str, checks: list[str] | None) -> None:
    """An agy worker is starting in ``workspace``: bring agy's settings in
    line with the repo's policy (``checks`` is None when it's off). Never
    raises; nothing is written when the policy is off and brindle never added
    anything."""
    from brindle import git, permissions

    try:
        try:
            root = git.main_repo_root(workspace)
        except git.GitError:
            root = workspace
        store = permissions.load_store()
        if checks is None and root not in store.agy_repos and not (store.agy_managed and not store.agy_repos):
            return
        sync_permissions(root, on=checks is not None, checks=checks)
    except Exception as e:  # noqa: BLE001
        print(f"brindle: couldn't update agy's permission settings: {e}", file=sys.stderr)


def unapplied_rules(profile) -> list[str]:
    """The profile's ``allowed_tools`` an agy worker on agy's own prompts
    still asks about: those agy's settings don't already allow exactly. agy
    reads no hook's allow and has only settings every agy session shares,
    so brindle never adds a profile's rules there: one worker's grants would
    be every other's."""
    from brindle import permissions

    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        allow = data["permissions"]["allow"]
    except (OSError, ValueError, KeyError, TypeError):
        allow = []
    allowed = set(allow) if isinstance(allow, list) else set()
    out = []
    for spec in profile.allowed_tools or []:
        entries = permissions.profile_agy_entries(spec)
        if not entries or not allowed.issuperset(entries):
            out.append(spec)
    return out


def launch_warning(agent, ws) -> str | None:
    """Which of its profile's allowed_tools an agy worker in workspace ``ws``
    still asks about (see unapplied_rules), or None. When brindle's hook
    decides every call (strict_on, or dontAsk), it applies them instead."""
    from brindle.profiles import load_profile

    if agent.provider != "antigravity":
        return None
    try:
        profile = load_profile(agent.profile, ws.repo_root)
    except KeyError:
        return None
    if profile.permission_mode == "dontAsk" or strict_on(ws.path, agent.mode):
        return None
    missing = unapplied_rules(profile)
    if not missing:
        return None
    return (f"this Antigravity worker asks before using these allowed_tools of profile {profile.name}, "
            f"since agy applies only its own settings.json, which every agy session shares: "
            f"{', '.join(missing)}. Add them to {settings_path()} yourself, or remove agy_approvals "
            "\"prompt\" to have brindle's hook apply the Bash ones")
