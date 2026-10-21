"""OpenCode (sst/opencode, npm ``opencode-ai``) as a brindle provider.

OpenCode reads extra configuration from ``OPENCODE_CONFIG_CONTENT`` (JSON,
merged over the person's own config), so brindle gives each agent its own
without writing anything into the worktree:

- ``mcp.brindle``: brindle's MCP server, with the agent's id in its
  environment. OpenCode connects it before the first turn, so the brief can
  go on the command line (``--prompt``).
- ``plugin``: brindle's plugin (written to ``~/.brindle/opencode/brindle.js``),
  which runs ``brindle _hook opencode-<event>`` on OpenCode's own events, so
  status never comes from reading the screen.
- ``instructions``: the profile's prompt and brindle's delivery note, as a
  file OpenCode adds to its system prompt.
- ``permission``: the profile's permission_mode and allowed_tools, in
  OpenCode's terms (see permission_config).
- The plugin's ``tool.execute.before`` hook validates scoped read-only git
  calls. With ``permission_policy`` on it also checks all other tools and
  throws on a deny (the launch sets ``BRINDLE_OPENCODE_POLICY=1``).

OpenCode's events, mapped onto brindle's hooks (agents.handle_hook):
- ``chat.message`` (a prompt is sent): ``prompt-submit``.
- ``permission.asked`` / ``question.asked``: waiting for the person.
- ``permission.replied`` / ``question.replied`` / ``question.rejected``:
  ``tool-done`` (it's working again).
- ``session.idle``: the turn ended, as agy's Stop (antigravity.end_turn): a
  queued message, or what brindle's Stop hook would block with, is typed in
  as a new turn. A ``session.error`` earlier in the turn travels with it.
Events from OpenCode's own subagent sessions (a ``parentID``) are left out.

Verified against OpenCode 1.19.3.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

INSTALLER = os.path.expanduser("~/.opencode/bin/opencode")   # where opencode.ai/install puts it
EVENTS = ("opencode-prompt", "opencode-permission", "opencode-permission-replied", "opencode-stop",
          "opencode-pre-tool")

PLUGIN = """\
// brindle's OpenCode plugin: tells brindle what this agent is doing.
// Written by brindle on every launch; changes here are overwritten.
import { spawn } from "node:child_process"

const BRINDLE = __BRINDLE__

export const Brindle = async ({ client }) => {
  const agent = process.env.BRINDLE_AGENT_ID
  if (!agent) return {}   // OpenCode running outside brindle
  const children = new Set()
  const approvals = new Map()
  const key = (session, call) => JSON.stringify([session, call])
  let error = null
  const send = (event, payload) => new Promise((resolve) => {
    try {
      let out = ""
      const p = spawn(BRINDLE[0], [...BRINDLE.slice(1), "_hook", event, "--agent", agent],
                      { stdio: ["pipe", "pipe", "ignore"] })
      p.stdout.on("data", (d) => { out += d })
      p.on("error", () => resolve(""))
      p.on("close", () => resolve(out))
      p.stdin.end(JSON.stringify(payload))
    } catch { resolve("") }
  })
  const mine = (id) => typeof id === "string" && !children.has(id)
  const policy = process.env.BRINDLE_OPENCODE_POLICY === "1"
  const dontAsk = JSON.parse(process.env.OPENCODE_CONFIG_CONTENT || "{}").permission?.bash?.["*"] === "deny"
  return {
    // Bash also checks scoped read-only git commands when the policy is off.
    // An approval is tied to this exact call, never installed as a wildcard.
    "tool.execute.before": async (input, output) => {
      const id = key(input.sessionID, input.callID)
      for (const [cached, approval] of approvals) {
        if (approval.expires <= Date.now()) approvals.delete(cached)
      }
      approvals.delete(id)
      if (!policy && input.tool !== "bash") return
      const out = await send("opencode-pre-tool", { session_id: input.sessionID, tool: input.tool, args: output.args })
      let answer = null
      try { answer = JSON.parse(out) } catch {}
      if (answer && answer.decision === "deny") throw new Error(answer.reason || "denied by brindle's permission policy")
      if (input.tool === "bash" && typeof input.sessionID === "string" && input.sessionID
          && typeof input.callID === "string" && input.callID
          && answer?.decision === "allow" && typeof output.args.command === "string") {
        // OpenCode matches its rules against the text as written: run brindle's respelling.
        if (typeof answer.command === "string") output.args.command = answer.command
        approvals.set(id, { command: output.args.command, expires: Date.now() + 30000 })
      }
      else if (dontAsk && /git\\s+(?:-C|branch\\s+--list)\\s/.test(output.args.command || "")) {
        throw new Error("read-only git request could not be approved")
      }
    },
    "tool.execute.after": async (input) => {
      approvals.delete(key(input.sessionID, input.callID))
    },
    "chat.message": async (input, output) => {
      if (!mine(input.sessionID)) return
      error = null
      const prompt = (output.parts || []).filter((p) => p.type === "text").map((p) => p.text).join("\\n")
      await send("opencode-prompt", { session_id: input.sessionID, prompt })
    },
    event: async ({ event }) => {
      const p = event.properties || {}
      if (event.type === "permission.asked" && p.permission === "bash" && p.tool) {
        const id = key(p.sessionID, p.tool.callID)
        const approval = approvals.get(id)
        if (approval && approval.expires <= Date.now()) approvals.delete(id)
        else if (approval && approval.command === p.metadata?.command) {
          approvals.delete(id)
          try {
            const response = await client.postSessionIdPermissionsPermissionId({
              path: { id: p.sessionID, permissionID: p.id }, body: { response: "once" }
            })
            if (!response.error) return
          } catch {}   // failure leaves OpenCode's normal prompt in place
        }
      }
      if (event.type === "session.idle" || event.type === "session.error") {
        for (const id of approvals.keys()) {
          if (JSON.parse(id)[0] === p.sessionID) approvals.delete(id)
        }
      }
      if (event.type === "session.created" && p.info && p.info.parentID) {
        children.add(p.info.id)
        return
      }
      if (!mine(p.sessionID)) return
      if (event.type === "permission.asked" || event.type === "question.asked") {
        await send("opencode-permission", { session_id: p.sessionID })
      } else if (["permission.replied", "question.replied", "question.rejected"].includes(event.type)) {
        await send("opencode-permission-replied", { session_id: p.sessionID })
      } else if (event.type === "session.error") {
        const e = p.error || {}
        if (e.name !== "MessageAbortedError") error = [e.name, e.data && e.data.message].filter(Boolean).join(": ") || "error"
      } else if (event.type === "session.idle") {
        const payload = { session_id: p.sessionID, error }
        error = null
        await send("opencode-stop", payload)
      }
    },
  }
}
"""


def binary() -> str:
    """BRINDLE_OPENCODE_BIN, else `opencode` on PATH, else where OpenCode's installer puts it."""
    explicit = os.environ.get("BRINDLE_OPENCODE_BIN")
    if explicit:
        return explicit
    from brindle.providers import which_cli

    return which_cli("opencode") or (INSTALLER if os.path.exists(INSTALLER) else "opencode")


def home() -> Path:
    from brindle.config import brindle_home

    return brindle_home() / "opencode"


def _write(path: Path, text: str) -> str:
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return str(path)


def write_plugin() -> str:
    from brindle.providers import brindle_invocation

    return _write(home() / "brindle.js", PLUGIN.replace("__BRINDLE__", json.dumps(brindle_invocation())))


def write_instructions(agent_id: str, text: str) -> str:
    return _write(home() / "instructions" / f"{agent_id}.md", text)


def remove_instructions(agent_id: str) -> None:
    (home() / "instructions" / f"{agent_id}.md").unlink(missing_ok=True)


# Claude Code's model names, for a profile written for it. OpenCode wants provider/model.
_FAMILIES = ((re.compile(r"claude-|opus|sonnet|haiku|fable"), "anthropic"),
             (re.compile(r"gpt-|o\d|codex"), "openai"),
             (re.compile(r"gemini-"), "google"))


def model_arg(model: str | None) -> str | None:
    if not model or "/" in model:
        return model
    for pattern, provider in _FAMILIES:
        if pattern.match(model):
            return f"{provider}/{model}"
    return model


_BASH_RULE = re.compile(r"^Bash\((.+)\)$")


def _bash_patterns(allowed_tools: list[str] | None) -> list[str]:
    """Claude Code's ``Bash(git diff:*)`` rules as OpenCode bash patterns
    (``git diff`` and ``git diff *``).

    OpenCode checks each simple command in a compound shell command, so
    separators need no extra patterns. It retains ``git -C <directory>``.
    Validate those calls in the plugin: ``git -C * diff *`` could also approve
    ``git -C . checkout diff file`` because its wildcard spans arguments.
    """
    out: list[str] = []
    for rule in allowed_tools or []:
        m = _BASH_RULE.match(rule.strip())
        if not m:
            continue
        command = m.group(1).strip()
        if command.endswith(":*"):
            prefix = command[:-2].strip()
            out += [prefix, f"{prefix} *"]
        else:
            out.append(command)
    return list(dict.fromkeys(out))


_GIT_REQUEST = re.compile(r"git\s+(?:-C|branch\s+--list)\s")


def squeezed(command: str) -> str:
    """``command`` with each run of spaces and tabs outside quotes cut to one
    space and none at either end. Words stay the same, but OpenCode matches
    its bash rules against each command's text as written: ``git  -C x
    status`` misses ``git -C *`` and, under dontAsk, falls to the deny."""
    out: list[str] = []
    quote, space, i = "", False, 0
    while i < len(command):
        c = command[i]
        if not quote and c in " \t":
            space = True
            i += 1
            continue
        if space and out:
            out.append(" ")
        space = False
        if c == "\\" and quote != "'" and i + 1 < len(command):
            out.append(command[i:i + 2])
            i += 2
            continue
        if c in "'\"" and quote in ("", c):
            quote = "" if quote else c
        out.append(c)
        i += 1
    return "".join(out)


def readonly_git(profile, command: str, cwd: str, repo_root: str, *, workdir: str | None = None) -> bool:
    """A scoped read-only git request the plugin can approve for one call.

    Resolve paths at dispatch time so worktrees created after launch work too.
    Reuse the native shell parser to reject substitutions, writes through
    redirections, and unsupported syntax rather than guessing at shell text.
    """
    from brindle.native.permissions import Permissions, _simple_commands, uncovered_part
    from brindle.permissions import GIT_UNSAFE_OPTIONS

    if "`" in command or "$(" in command:
        return False
    parts = _simple_commands(command)
    if not parts:
        return False
    # The native parser preserves argument boundaries but drops quotes. Refuse
    # unquoted globs: shell expansion could introduce options we never checked.
    try:
        raw = shlex.shlex(command, posix=False, punctuation_chars=True)
        expands = any(any(c in token for c in "*?[") and token[0] not in "\"'" for token in raw)
    except ValueError:
        return False
    if expands:
        return False
    specs = [spec for name, spec in Permissions(profile.permission_mode, profile.allowed_tools).rules
             if name == "Bash" and spec]
    roots = [Path(cwd).resolve(), *((Path(cwd) / d).resolve() for d in profile.add_dirs or [])]
    roots.append(Path(repo_root).resolve() / ".brindle" / "worktrees")
    here = (Path(cwd) / (workdir or ".")).resolve()
    if not any(here.is_relative_to(root) for root in roots):
        return False
    dynamic = False
    for words, _ in parts:
        if words[:2] == ["git", "-C"]:
            if len(words) < 4 or not words[2] or any(c in words[2] for c in "*?["):
                return False
            target = (here / words[2]).resolve()
            if not target.is_dir() or not any(target.is_relative_to(root) for root in roots):
                return False
            words = ["git", *words[3:]]
            dynamic = True
        if words[0] == "git":
            if len(words) < 2 or any(arg.startswith((*GIT_UNSAFE_OPTIONS, "--textconv")) for arg in words[2:]):
                return False
            if words[1] == "branch":
                if words[2:3] == ["--list"] and all(not arg.startswith("-") for arg in words[3:]):
                    words = words[:3]  # patterns cannot change the listing action
                    dynamic = True
                elif words[2:] not in ([], ["-a"], ["-v"], ["-vv"]):
                    return False
            elif words[1] not in ("status", "diff", "log", "show", "ls-files") and words[1:3] != ["stash", "list"]:
                return False
        elif words[0] not in ("ls", "pwd", "cat", "tail", "head", "grep", "wc"):
            return False
        if uncovered_part(specs, shlex.join(words)) is not None:
            return False
    return dynamic


def permission_config(profile) -> dict:
    """The profile's permission_mode and allowed_tools as OpenCode's
    ``permission`` block. OpenCode takes the last rule that matches, so the
    catch-all comes first. Reads, searches and brindle's MCP tools are left at
    OpenCode's default (allow). The plugin validates scoped ``git -C`` reads.

    - ``bypassPermissions``: everything allowed.
    - ``dontAsk`` (nobody watches, e.g. a reviewer): only allowed_tools'
      commands run; edits, other commands and web fetches are refused.
    - ``acceptEdits`` / ``auto`` / ``plan``: edits allowed (``plan`` runs
      OpenCode's own plan agent, which edits nothing); allowed_tools'
      commands run, others ask.
    - none / ``default``: as above, but edits ask too."""
    mode = profile.permission_mode
    if mode == "bypassPermissions":
        return {"edit": "allow", "bash": "allow", "webfetch": "allow", "external_directory": "allow"}
    fallback = "deny" if mode == "dontAsk" else "ask"
    dirs = [*(profile.add_dirs or []), *(profile.read_dirs or [])]
    # Ask is only transport for the plugin's per-call reply, never an allow.
    # For dontAsk, the pre-tool hook refuses unapproved dynamic requests.
    bash = {"*": fallback, "git -C *": "ask", "git branch --list *": "ask",
            **{p: "allow" for p in _bash_patterns(profile.allowed_tools)}}
    edit = "allow" if mode in ("acceptEdits", "auto", "plan") else fallback
    perm: dict = {"edit": edit, "bash": bash}
    if mode == "dontAsk":
        perm["webfetch"] = "deny"
    if dirs:
        perm["external_directory"] = {"*": fallback, **{f"{d.rstrip('/')}/*": "allow" for d in dirs}}
    return perm


def config(ctx) -> dict:
    """``OPENCODE_CONFIG_CONTENT`` for the agent ``ctx`` launches."""
    from brindle.providers import DELIVERY_NOTE, mcp_server_spec

    spec = mcp_server_spec(ctx.agent_id)
    instructions = "\n\n".join(filter(None, [ctx.profile.prompt, DELIVERY_NOTE]))
    return {
        "mcp": {"brindle": {"type": "local", "command": [spec["command"], *spec["args"]],
                            "environment": spec["env"], "enabled": True,
                            # The default (5s) is too short on a loaded machine (as for Codex, #159).
                            "timeout": 120000}},
        "plugin": [Path(write_plugin()).as_uri()],
        "instructions": [write_instructions(ctx.agent_id, instructions)],
        "permission": permission_config(ctx.profile),
        # brindle's panes are one agent each: an update prompt nobody answers helps no one.
        "autoupdate": False,
    }


def can_resume(session_id: str) -> bool:
    return bool(session_id) and session_id.startswith("ses_")


# -- hooks ---------------------------------------------------------------------------


def handle_hook(db, agent_id: str, event: str, payload: dict) -> None:
    """Translate one OpenCode event (from brindle's plugin) into brindle's own hooks."""
    from brindle import agents, antigravity

    agent = db.get_agent(agent_id)
    if agent is None:
        return
    sid = payload.get("session_id")
    base = {"session_id": sid} if isinstance(sid, str) and sid else {}
    if event == "opencode-prompt":
        prompt = str(payload.get("prompt") or "")
        if not db.delivered_since(agent_id, time.time() - antigravity.DELIVERY_WINDOW):
            # A turn the person typed: brindle's report reminder may go out again.
            db.update_agent(agent_id, stop_blocked=0)
        agents.handle_hook(db, agent_id, "prompt-submit", {**base, "prompt": prompt})
    elif event == "opencode-permission":
        agents.handle_hook(db, agent_id, "notification", {**base, "message": "permission"})
    elif event == "opencode-permission-replied":
        agents.handle_hook(db, agent_id, "tool-done", base)
    elif event == "opencode-stop":
        if base and base["session_id"] != agent.session_ref:
            db.update_agent(agent_id, session_ref=base["session_id"])
        antigravity.end_turn(db, agent, base, str(payload.get("error") or ""), provider="opencode")


def pre_tool(db, agent_id: str, payload: dict) -> str:
    """Policy denials win; scoped read-only git commands get one-call approval."""
    from brindle import agents

    agent = db.get_agent(agent_id)
    answer = agents.permission_request_decision(db, agent, payload, provider="opencode") if agent else None
    if answer or not agent or payload.get("tool") != "bash":
        return json.dumps(answer) if answer else ""
    ws = db.get_workspace(agent.workspace_id)
    if ws is None:
        return ""
    from brindle.profiles import load_profile

    profile = load_profile(agent.profile, ws.repo_root)
    args = payload.get("args") or {}
    command = str(args.get("command") or "")
    if readonly_git(profile, command, ws.path, ws.repo_root, workdir=args.get("workdir")):
        # Apply policy to normalized subcommands too: spelling a denied
        # `git status` as `git -C <worktree> status` must not bypass it.
        from brindle.native.permissions import _simple_commands

        for words, _ in _simple_commands(command) or []:
            if words[:2] == ["git", "-C"]:
                words = ["git", *words[3:]]
            normalized = {**payload, "args": {**args, "command": shlex.join(words)}}
            denied = agents.permission_request_decision(db, agent, normalized, provider="opencode")
            if denied:
                return json.dumps(denied)
        answer = {"decision": "allow"}
        if (spelling := squeezed(command)) != command:
            answer["command"] = spelling
    elif profile.permission_mode == "dontAsk" and _GIT_REQUEST.search(command):
        answer = {"decision": "deny", "reason": "command is not an allowed read-only git request"}
    return json.dumps(answer) if answer else ""


def hook_main(db, agent_id: str | None, event: str, stdin_text: str) -> str:
    """``brindle _hook opencode-<event>``: what it prints is only read for
    ``opencode-pre-tool`` (a per-call decision); the plugin ignores the rest."""
    if not agent_id or event not in EVENTS:
        return ""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    if event == "opencode-pre-tool":
        try:
            return pre_tool(db, agent_id, payload)
        except Exception as e:  # no answer means OpenCode's own rules apply
            print(f"brindle hook {event}: {e}", file=sys.stderr)
            return ""
    try:
        handle_hook(db, agent_id, event, payload)
    except Exception as e:  # never break the person's OpenCode session over brindle
        print(f"brindle hook {event}: {e}", file=sys.stderr)
    return ""
