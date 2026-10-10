"""Permission policy: brindle decides a worker's tool-permission requests from
the structured request the agent CLI hands its hook (tool name and input),
never from the terminal.

Provider-neutral core. A provider's hook turns its own payload into a
``Request`` (``from_claude`` for Claude Code's ``PermissionRequest`` hook,
``from_codex`` for Codex's, ``from_agy`` for Antigravity's ``PreToolUse``) and
calls ``decide``, which
returns ``allow``, ``deny`` or ``ask`` with a reason. ``ask`` means "no
decision": the CLI shows its normal prompt to the person.

Rules are data::

    {"id": "3f2a91c0", "kind": "bash", "match": "npm test", "match_type": "exact",
     "decision": "allow", "source": "user", "created": 1767225600.0}

``kind`` is read | write | edit | bash | fetch | mcp | other. ``match_type`` is
``exact``, ``prefix`` or ``glob`` (``fnmatch``: ``*`` also crosses ``/``; no
regular expressions). What a rule is matched against depends on the kind: a
bash rule against the whole command; a path rule against the resolved
absolute path, the path as given, and the path relative to the worktree and
the repo root (``~`` in a rule means the home directory); a fetch rule
against the URL; mcp/other against the tool name. The built-in defaults also
use a few named matchers (``tracked``, ``check``, ``git-readonly``,
``git-push``, ``git-force``) that are code, not patterns.

Precedence: any matching deny wins; else any matching allow; else ask.
Specificity never matters. An allow rule for bash only ever applies to a
single simple command (letters, digits, spaces and ``_-./:=@%+,`` only): a
command with any other character (``;``, ``|``, ``&``, ``$``, quotes, globs,
redirections, newlines...) is never auto-allowed. Anything unknown or
unparseable is ask.

Where rules come from (``source``): ``default`` (built in, below), ``repo``
(``<repo>/.brindle/permissions.json``: only its deny rules are used, so a repo
can narrow but never broaden), ``user`` (``brindle permissions allow|deny``) and
``learned`` (a suggestion the person accepted). User and learned rules live in
``~/.brindle/permissions.json`` with the approval counts learning keeps.

Learning: when a request fell to ask and the person approved it in the CLI
(for Claude Code: the tool's PostToolUse arrives for the same tool_use_id),
brindle counts that approval for the request's exact pattern. Nothing becomes a
rule on its own: a pattern approved at least SUGGEST_AFTER times is shown by
``brindle permissions suggestions`` and becomes a rule only on ``accept``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from brindle.config import CONFIG_DIR, config_root, brindle_home

KINDS = ("read", "write", "edit", "bash", "fetch", "mcp", "other")
PATH_KINDS = ("read", "write", "edit")
DECISIONS = ("allow", "deny", "ask")
MATCH_TYPES = ("exact", "prefix", "glob")
BUILTIN_MATCHERS = ("tracked", "check", "git-readonly", "git-push", "git-force")
SOURCES = ("default", "repo", "user", "learned")
SUGGEST_AFTER = 2
FILE = "permissions.json"

# A command an allow rule may cover: one simple command, nothing the shell
# would treat specially.
SIMPLE_COMMAND = re.compile(r"[A-Za-z0-9_\-./:=@%+, ]+")
READONLY_GIT = ("status", "diff", "log", "show")
# Options that make an otherwise read-only git command write or read files
# outside the repo.
GIT_UNSAFE_OPTIONS = ("--output", "--ext-diff", "--no-index", "-O")
FORCE_OPTIONS = ("--force", "--force-with-lease", "--force-if-includes")


@dataclass(frozen=True)
class Request:
    """One tool-permission request, as any provider's hook describes it."""
    provider: str
    kind: str
    tool: str
    cwd: str = ""
    worktree: str = ""
    repo_root: str = ""
    command: str | None = None
    path: str | None = None
    url: str | None = None

    def target(self) -> str:
        return self.command or self.path or self.url or self.tool

    def summary(self, width: int = 200) -> str:
        text = f"{self.tool}: {self.target()}" if self.target() != self.tool else self.tool
        text = " ".join(text.split())
        return text if len(text) <= width else text[: width - 1] + "…"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Request":
        names = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass(frozen=True)
class Rule:
    kind: str
    match: str
    match_type: str
    decision: str
    source: str = "user"
    created: float = 0.0
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", rule_id(self.kind, self.match_type, self.match, self.decision))

    def describe(self) -> str:
        if self.match_type in BUILTIN_MATCHERS:
            return self.match
        return f"{self.kind} {self.match_type} {self.match!r}"

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "match": self.match, "match_type": self.match_type,
                "decision": self.decision, "source": self.source, "created": self.created}

    @classmethod
    def from_dict(cls, data: dict, source: str | None = None) -> "Rule | None":
        """A rule read from a file, or None if it isn't a valid one."""
        try:
            kind, match = str(data["kind"]), str(data["match"])
            match_type = str(data.get("match_type", "exact"))
            decision = str(data["decision"])
        except (KeyError, TypeError):
            return None
        if kind not in KINDS or match_type not in MATCH_TYPES or decision not in ("allow", "deny") or not match:
            return None
        src = source or str(data.get("source", "user"))
        if src not in ("user", "learned", "repo"):
            src = "user"
        try:
            created = float(data.get("created") or 0)
        except (TypeError, ValueError):
            created = 0.0
        return cls(kind, match, match_type, decision, src, created)


@dataclass(frozen=True)
class Decision:
    decision: str  # allow | deny | ask
    reason: str
    rule: Rule | None = None


def rule_id(kind: str, match_type: str, match: str, decision: str) -> str:
    return hashlib.sha1(f"{kind}\0{match_type}\0{match}\0{decision}".encode()).hexdigest()[:8]


def _default(id_: str, kind: str, match: str, match_type: str, decision: str) -> Rule:
    return Rule(kind, match, match_type, decision, "default", 0.0, id_)


def _preset(kind: str, match: str, match_type: str, decision: str) -> Rule:
    return Rule(kind, match, match_type, decision, "user", time.time(), "")


PRESET_COMMANDS = ("aws", "uv", "pytest", "python", "npm", "pnpm", "yarn", "docker", "cargo", "go", "make", "gh")
PRESET_RULES = tuple(
    rule
    for cmd in PRESET_COMMANDS
    for rule in (
        _preset("bash", cmd, "exact", "allow"),
        _preset("bash", f"{cmd} ", "prefix", "allow"),
    )
)

DEFAULT_RULES: tuple[Rule, ...] = (
    _default("d-tracked", "read", "a file git tracks in the worktree or repo root", "tracked", "allow"),
    _default("d-checks", "bash", "exactly one of the repo's `checks` commands", "check", "allow"),
    _default("d-git-ro", "bash", "git status / diff / log / show", "git-readonly", "allow"),
    _default("d-git-push", "bash", "git push", "git-push", "deny"),
    _default("d-git-force", "bash", "a git command with a force flag", "git-force", "deny"),
    _default("d-ssh", "read", "~/.ssh*", "glob", "deny"),
    _default("d-aws", "read", "~/.aws*", "glob", "deny"),
    _default("d-gnupg", "read", "~/.gnupg*", "glob", "deny"),
    _default("d-env", "read", "*/.env*", "glob", "deny"),
    _default("d-env-rel", "read", ".env*", "glob", "deny"),
)


# -- providers: Claude Code ------------------------------------------------------------------

CLAUDE_KINDS = {
    "Read": "read", "Glob": "read", "Grep": "read", "LS": "read", "NotebookRead": "read",
    "Write": "write",
    "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Bash": "bash",
    "WebFetch": "fetch", "WebSearch": "fetch",
}


def from_claude(payload: dict, worktree: str = "", repo_root: str = "") -> Request | None:
    """Claude Code's PermissionRequest payload as a Request; None when it
    names no tool (nothing to decide)."""
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return None
    ti = payload.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    cwd = str(payload.get("cwd") or worktree or "")
    kind = "mcp" if tool.startswith("mcp__") else CLAUDE_KINDS.get(tool, "other")
    command = path = url = None
    if kind == "bash":
        command = ti.get("command") if isinstance(ti.get("command"), str) else None
    elif kind in PATH_KINDS:
        raw = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
        if raw is None and tool in ("Glob", "Grep", "LS"):
            raw = cwd  # searches the working directory
        if isinstance(raw, str) and raw:
            raw = os.path.expanduser(raw)
            path = raw if os.path.isabs(raw) else os.path.join(cwd or os.getcwd(), raw)
    elif kind == "fetch":
        url = ti.get("url") if isinstance(ti.get("url"), str) else None
    return Request("claude", kind, tool, cwd, worktree, repo_root, command, path, url)


def claude_output(decision: Decision) -> dict | None:
    """What Claude Code's PermissionRequest hook prints; None (no output)
    for ask, so the normal prompt is shown."""
    if decision.decision not in ("allow", "deny"):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": decision.decision, "message": f"brindle: {decision.reason}"},
    }}


# -- providers: Codex -------------------------------------------------------------------------
#
# Codex's PermissionRequest hook (see brindle.codex_hook for how it's installed)
# gets {session_id, turn_id, cwd, tool_name, tool_input, ...}: tool_name is
# "Bash", "apply_patch" or "mcp__<server>__<tool>", and both Bash and
# apply_patch carry their text in tool_input.command. It has no tool_use_id,
# so an asked request can't be paired with the PostToolUse that follows it:
# brindle doesn't learn from Codex approvals.

CODEX_PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$", re.M)
CODEX_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$", re.M)
CODEX_PATCH_KINDS = {"Add": "write", "Update": "edit", "Delete": "write"}
CODEX_PATCH_MARKERS = ("*** Begin Patch", "*** End Patch", "*** End of File")


def _codex_patch_accounted(patch: str) -> bool:
    """Every line that looks like a patch header ("***" after any leading
    whitespace) is exactly one brindle reads. Codex's own reader may accept a
    looser header (indented, other case); brindle then can't know every file."""
    for line in patch.splitlines():
        if not line.lstrip().startswith("***"):
            continue
        bare = line.rstrip()
        if bare in CODEX_PATCH_MARKERS or CODEX_PATCH_FILE.fullmatch(bare) or CODEX_PATCH_MOVE.fullmatch(bare):
            continue
        return False
    return True


def _abs_path(raw: str, cwd: str) -> str:
    raw = os.path.expanduser(raw)
    return raw if os.path.isabs(raw) else os.path.join(cwd or os.getcwd(), raw)


def _codex_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return shlex.join(value)
    return None


def from_codex(payload: dict, worktree: str = "", repo_root: str = "") -> list[Request] | None:
    """Codex's PermissionRequest payload as Requests: one, or one per file an
    apply_patch touches; None when it names no tool."""
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return None
    ti = payload.get("tool_input")
    cwd = str(payload.get("cwd") or worktree or "")
    base = dict(provider="codex", tool=tool, cwd=cwd, worktree=worktree, repo_root=repo_root)
    if tool.startswith("mcp__"):
        return [Request(kind="mcp", **base)]
    command = _codex_text(ti.get("command") if isinstance(ti, dict) else ti)
    if tool == "Bash":
        return [Request(kind="bash", command=command, **base)]
    if tool == "apply_patch":
        patch = command
        if isinstance(ti, dict) and isinstance(ti.get("command"), list) and ti["command"]:
            patch = ti["command"][-1] if isinstance(ti["command"][-1], str) else None
        files = [(CODEX_PATCH_KINDS[m.group(1)], m.group(2)) for m in CODEX_PATCH_FILE.finditer(patch or "")]
        files += [("write", m.group(1)) for m in CODEX_PATCH_MOVE.finditer(patch or "")]
        if not files or not _codex_patch_accounted(patch or ""):
            return [Request(kind="edit", **base)]  # no path (or not every one known): ask
        out = list(dict.fromkeys((k, _abs_path(f, cwd)) for k, f in files))
        return [Request(kind=k, path=path, **base) for k, path in out]
    return [Request(kind="other", **base)]


def decide_all(reqs: list[Request] | None, *, checks: list[str] | None = None,
               rules: list[Rule] | None = None) -> Decision:
    """One answer for several requests (an apply_patch touching several
    files): deny if any is denied, allow only if every one is allowed, else ask."""
    if not reqs:
        return Decision("ask", "brindle couldn't read the request")
    decisions = [decide(r, checks=checks, rules=rules) for r in reqs]
    for d in decisions:
        if d.decision == "deny":
            return d
    if all(d.decision == "allow" for d in decisions):
        if len(decisions) == 1:
            return decisions[0]
        return Decision("allow", f"every one of {len(decisions)} files is allowed")
    return next(d for d in decisions if d.decision == "ask")


def codex_output(decision: Decision) -> dict | None:
    """What Codex's PermissionRequest hook prints; None (no output) for ask.
    Codex's schema requires hookEventName and fails closed on unknown fields."""
    if decision.decision == "allow":
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                       "decision": {"behavior": "allow"}}}
    if decision.decision == "deny":
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                       "decision": {"behavior": "deny", "message": f"brindle: {decision.reason}"}}}
    return None


# -- providers: Antigravity (agy) --------------------------------------------------------------
#
# agy's PreToolUse hook gets {toolCall: {name, args}, stepIdx, conversationId,
# workspacePaths, ...}; tool arguments are CamelCase, and agy's own transcripts
# show some string values JSON-encoded a second time ("\"npm test\""), so
# both are accepted. agy ignores a hook's "allow" (upstream bug
# google-antigravity/antigravity-cli#1053) and treats a missing answer as a
# deny, so the hook only ever answers deny or ask, and brindle mirrors its allow
# rules into agy's own settings instead (brindle.antigravity.sync_permissions).
# PreToolUse runs for every tool call, not only for one agy would prompt for,
# so a PostToolUse after it doesn't mean the person approved anything: brindle
# doesn't learn from agy.

AGY_KINDS = {
    "run_command": "bash",
    "view_file": "read", "view_file_outline": "read", "list_dir": "read", "grep_search": "read",
    "find_by_name": "read", "codebase_search": "read", "read_file": "read",
    "write_to_file": "write", "create_file": "write", "write_file": "write", "delete_file": "write",
    "replace_file_content": "edit", "multi_replace_file_content": "edit", "edit_file": "edit",
    "read_url_content": "fetch", "search_web": "fetch",
    "call_mcp_tool": "mcp",
}
AGY_PATH_ARGS = ("AbsolutePath", "TargetFile", "DirectoryPath", "SearchPath", "SearchDirectory",
                 "FilePath", "File", "Path")


def _agy_arg(args: dict, name: str) -> str | None:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        return None
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = None
        if isinstance(decoded, str):
            return decoded
    return value


def from_agy(payload: dict, worktree: str = "", repo_root: str = "") -> Request | None:
    """agy's PreToolUse payload as a Request; None when it names no tool."""
    call = payload.get("toolCall")
    if not isinstance(call, dict):
        return None
    name = call.get("name")
    if not isinstance(name, str) or not name:
        return None
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    spaces = payload.get("workspacePaths")
    first = spaces[0] if isinstance(spaces, list) and spaces and isinstance(spaces[0], str) else ""
    cwd = _agy_arg(args, "Cwd") or first or worktree
    kind = "mcp" if name.startswith("mcp_") else AGY_KINDS.get(name, "other")
    tool, command, path, url = name, None, None, None
    if kind == "bash":
        command = _agy_arg(args, "CommandLine")
    elif kind in PATH_KINDS:
        raw = next((v for v in (_agy_arg(args, a) for a in AGY_PATH_ARGS) if v), None)
        path = _abs_path(raw, cwd) if raw else None
    elif kind == "fetch":
        url = _agy_arg(args, "Url")
    elif name == "call_mcp_tool":
        server, inner = _agy_arg(args, "ServerName"), _agy_arg(args, "ToolName")
        if server and inner:
            tool = f"mcp__{server}__{inner}"
    return Request("antigravity", kind, tool, cwd, worktree, repo_root, command, path, url)


def agy_output(decision: Decision | None, req: Request | None = None,
               workspaces: list[str] | None = None) -> dict:
    """What agy's PreToolUse hook prints. Never empty: agy reads that as deny.

    A deny is a deny. Otherwise brindle answers what agy would do on its own,
    since agy has no "no opinion": "allow" for reading or writing a file
    inside one of its workspaces (agy does that without asking), else "ask"
    (agy prompts unless its own allow rules cover the call). agy currently
    ignores a hook's "allow" (google-antigravity/antigravity-cli#1053); once
    it doesn't, this still allows nothing agy wouldn't by default."""
    if decision is not None and decision.decision == "deny":
        return {"decision": "deny", "reason": f"brindle: {decision.reason}"}
    if (req is not None and req.kind in ("read", "write", "edit") and req.path
            and any(_inside(os.path.realpath(req.path), w) for w in workspaces or [])):
        return {"decision": "allow", "reason": "brindle: a file in the workspace (agy's default)"}
    why = decision.reason if decision is not None else "no decision"
    return {"decision": "ask", "reason": f"brindle: {why}"}


# agy's own tools that act only on the conversation (tasks, messages to its
# subagents, questions to the person); a subagent's tool calls reach the hook
# themselves (checked on agy 1.3.2). Anything not listed here or mapped in
# AGY_KINDS is denied under agy_approvals "brindle", so a tool agy adds later
# starts out blocked.
AGY_CONVERSATION_TOOLS = frozenset({"manage_task", "send_message", "ask_question", "invoke_subagent"})
# Inside a workspace, but not files the agent may write without a rule: what
# brindle's hook and agy's settings are read from (.agents), what git runs
# (.git/hooks, config), and brindle's repo config (checks, rules).
AGY_PROTECTED_DIRS = (".agents", ".git", ".brindle")
NEEDS_APPROVAL = ("brindle runs this agent without approval prompts, and no rule allows this "
                  "({why}). Don't try another way to do the same thing: tell your supervisor "
                  "with send_message what you need and why, so the person can allow it with "
                  "`brindle permissions allow`.")


def _lexically_within(literal: str, root: str) -> bool:
    """``literal`` is ``root`` or under it as spelled, taking ``root`` as
    given or resolved (macOS's /tmp is /private/tmp), with nothing resolved
    in ``literal`` itself."""
    for r in {os.path.normpath(root), os.path.realpath(root)}:
        if literal == r or literal.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def _through_symlink(literal: str, roots: list[str]) -> bool:
    """Whether ``literal`` (absolute, normalized) passes through a symlink
    below one of ``roots``, the file itself included. A root's own symlinks
    (macOS's /tmp -> /private/tmp) don't count."""
    for root in {f(r) for r in roots if r for f in (os.path.normpath, os.path.realpath)}:
        rel = os.path.relpath(literal, root)
        if rel == ".." or rel.startswith(".." + os.sep) or rel == ".":
            continue
        prefix = root
        for part in rel.split(os.sep):
            prefix = os.path.join(prefix, part)
            if os.path.islink(prefix):
                return True
    return False


def _agy_protected(literal: str, real: str, roots: list[str]) -> bool:
    """Whether a write lands in a .agents/.git/.brindle folder under one of
    ``roots``, at any depth, ignoring case (macOS and Windows file systems
    do). Checked on the path as written and as resolved, and against each
    top-level protected folder resolved, so a symlink either way (a
    ``.agents`` that points elsewhere, or a path that points into one) is
    still caught."""
    names = {d.casefold() for d in AGY_PROTECTED_DIRS}
    if _through_symlink(literal, roots):
        return True  # where it really lands is not worth guessing at
    for root in (r for r in roots if r):
        for p, r in ((literal, os.path.normpath(root)), (real, os.path.realpath(root))):
            rel = os.path.relpath(p, r)
            if rel != ".." and not rel.startswith(".." + os.sep):
                if names & {part.casefold() for part in rel.split(os.sep)}:
                    return True
        for d in AGY_PROTECTED_DIRS:
            target = os.path.realpath(os.path.join(root, d))
            if real == target or real.startswith(target + os.sep):
                return True
    return False


def _brindle_tools() -> set[str]:
    """brindle's MCP tools by both names agy gives them: mcp_brindle_<tool>
    and, through call_mcp_tool, mcp__brindle__<tool>. Exact names, so another
    server's tools (say a server named brindle_x) never count."""
    from brindle.antigravity import tool_names

    names = tool_names()
    return {f"mcp_brindle_{n}" for n in names} | {f"mcp__brindle__{n}" for n in names}


def agy_strict_ambiguity(payload: dict, worktree: str) -> str | None:
    """Why brindle can't be sure it reads an agy call the way agy will, or
    None. Under agy_approvals "brindle" such a call is denied rather than
    guessed at: a JSON-encoded value (brindle decodes it, agy may not), a
    relative or ~ path (agy may resolve it against something other than
    Cwd), more than one path, or a command run outside the worktree."""
    call = payload.get("toolCall") if isinstance(payload, dict) else None
    args = call.get("args") if isinstance(call, dict) and isinstance(call.get("args"), dict) else {}
    for name in (*AGY_PATH_ARGS, "Cwd", "CommandLine"):
        raw = args.get(name)
        if isinstance(raw, str) and raw and _agy_arg(args, name) != raw:
            return f"{name} is JSON-encoded"
    paths = {args[a] for a in AGY_PATH_ARGS if isinstance(args.get(a), str) and args[a]}
    if len(paths) > 1:
        return "the call names more than one path"
    if any(not os.path.isabs(p) for p in paths):
        return "the call names a relative path"
    if any(".." in p.split("/") or ".." in p.split(os.sep) for p in paths):
        return "the path has a .. in it"  # resolved after a symlink, not as written
    cwd = args.get("Cwd")
    if call.get("name") == "run_command" and isinstance(cwd, str) and cwd:
        real, root = os.path.realpath(cwd), os.path.realpath(worktree)
        if not os.path.isabs(cwd) or (real != root and not _inside(real, worktree)):
            return "the command runs outside the worktree"
    return None


def agy_strict_output(decision: Decision | None, req: Request | None = None,
                      workspaces: list[str] | None = None,
                      protected_roots: list[str] | None = None) -> dict:
    """agy's PreToolUse answer under ``agy_approvals: "brindle"``, where agy
    runs with --dangerously-skip-permissions: a hook's "ask" then runs the
    call without a prompt, so brindle answers allow or deny, never ask.
    Allowed: what a rule allows, a file inside a workspace (or the
    conversation's artifact folder, passed in ``workspaces``), brindle's own
    tools, and agy's conversation-only tools. Everything else is denied with
    a reason that tells the agent to ask its supervisor."""
    if decision is not None and decision.decision == "deny":
        return {"decision": "deny", "reason": f"brindle: {decision.reason}"}
    if decision is not None and decision.decision == "allow":
        return {"decision": "allow", "reason": f"brindle: {decision.reason}"}
    if req is not None:
        path = os.path.realpath(req.path) if req.kind in PATH_KINDS and req.path else None
        # Protected wherever the workspaces list says the workspace is: the
        # anchors are brindle's own worktree and repo root.
        literal = os.path.normpath(req.path) if path else None
        protected = (path and req.kind != "read"
                     and _agy_protected(literal, path, protected_roots or []))
        # In the workspace as written, not only once resolved: a path that
        # gets there through a symlink outside it would skip the symlink walk.
        if path and not protected and any(
                w and _lexically_within(literal, w) and (path == os.path.realpath(w) or _inside(path, w))
                for w in workspaces or []):
            return {"decision": "allow", "reason": "brindle: a file in the workspace"}
        if req.kind == "mcp" and req.tool in _brindle_tools():
            return {"decision": "allow", "reason": "brindle: brindle's own tool"}
        if req.kind == "other" and req.tool in AGY_CONVERSATION_TOOLS:
            return {"decision": "allow", "reason": "brindle: acts only on the conversation"}
    why = decision.reason if decision is not None else "brindle couldn't read the request"
    why = why.removesuffix("; asking the user")  # nobody is asked here
    return {"decision": "deny", "reason": "brindle: " + NEEDS_APPROVAL.format(why=why)}


# -- matching ---------------------------------------------------------------------------------


def simple_argv(command: str | None) -> list[str] | None:
    """The words of a single simple command with no shell metacharacters,
    else None."""
    if not command or not SIMPLE_COMMAND.fullmatch(command):
        return None
    argv = command.split()
    return argv or None


def _inside(path: str, root: str) -> str | None:
    """``path``'s location relative to ``root`` (both resolved), if inside."""
    if not root:
        return None
    rr = os.path.realpath(root)
    if path.startswith(rr + os.sep):
        return os.path.relpath(path, rr)
    return None


def _roots(req: Request) -> list[str]:
    return list(dict.fromkeys(r for r in (req.worktree, req.repo_root) if r))


def path_candidates(req: Request, resolved_only: bool = False) -> list[str]:
    """The forms of ``req.path`` a rule may match. ``resolved_only`` (for
    allows) leaves out the name as given: a symlink under an allowed folder
    can lead anywhere, so only where the path really goes counts. Denies
    match either."""
    if not req.path:
        return []
    real = os.path.realpath(req.path)
    given = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(req.path))),
                         os.path.basename(req.path))
    paths = [real] if resolved_only else [real, os.path.abspath(req.path), given]
    out = list(paths)
    for p in paths:
        for root in _roots(req):
            rel = _inside(p, root)
            if rel:
                out.append(rel)
    return list(dict.fromkeys(out))


def candidates(req: Request) -> list[str]:
    if req.kind == "bash":
        return [req.command.strip()] if req.command and req.command.strip() else []
    if req.kind in PATH_KINDS:
        return path_candidates(req)
    if req.kind == "fetch":
        return [req.url] if req.url else []
    return [req.tool] if req.tool else []


def is_tracked(path: str | None, roots: list[str]) -> bool:
    """``path`` (symlinks resolved) is a regular file inside one of ``roots``,
    not under .git, and git tracks exactly that path there."""
    if not path:
        return False
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        return False
    for root in roots:
        rel = _inside(real, root)
        if not rel or ".git" in Path(rel).parts:
            continue
        try:
            res = subprocess.run(
                ["git", "-C", os.path.realpath(root), "--literal-pathspecs", "ls-files", "-z", "--", rel],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode == 0 and rel in res.stdout.split("\0"):
            return True
    return False


def _segments(command: str) -> list[list[str]]:
    """Each simple command in ``command``, split at the shell's separators,
    as words (best effort: only used to find things to deny)."""
    out = []
    for part in re.split(r"[;&|\n()`]|\$\(", command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _git_calls(command: str) -> list[tuple[str, list[str]]]:
    """(subcommand, its arguments) for every git invocation in ``command``."""
    calls = []
    for words in _segments(command):
        for i, w in enumerate(words):
            if os.path.basename(w) != "git":
                continue
            rest, j = words[i + 1:], 0
            while j < len(rest) and rest[j].startswith("-"):
                j += 2 if rest[j] in ("-C", "-c") else 1  # global options
            if j < len(rest):
                calls.append((rest[j], rest[j + 1:]))
            break
    return calls


def _is_force(arg: str) -> bool:
    return (arg.startswith(FORCE_OPTIONS)
            or bool(re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", arg)))


def _builtin_matches(rule: Rule, req: Request, checks: list[str]) -> bool:
    if rule.match_type == "tracked":
        return req.kind == "read" and is_tracked(req.path, _roots(req))
    command = (req.command or "").strip()
    if not command:
        return False
    if rule.match_type == "check":
        return simple_argv(command) is not None and command in [c.strip() for c in checks]
    if rule.match_type == "git-readonly":
        argv = simple_argv(command)
        return bool(argv and len(argv) >= 2 and argv[0] == "git" and argv[1] in READONLY_GIT
                    and not any(a.startswith(GIT_UNSAFE_OPTIONS) or _is_force(a) for a in argv[2:]))
    if rule.match_type == "git-push":
        return any(sub == "push" for sub, _ in _git_calls(command))
    if rule.match_type == "git-force":
        return any(_is_force(a) for _, args in _git_calls(command) for a in args)
    return False


def rule_matches(rule: Rule, req: Request, checks: list[str] | None = None) -> bool:
    if rule.kind != req.kind:
        return False
    if rule.match_type in BUILTIN_MATCHERS:
        return _builtin_matches(rule, req, checks or [])
    if req.kind == "bash" and rule.decision == "allow" and simple_argv(req.command) is None:
        return False  # never auto-allow a compound or quoted command
    value = os.path.expanduser(rule.match) if req.kind in PATH_KINDS else rule.match
    forms = (path_candidates(req, resolved_only=rule.decision == "allow")
             if req.kind in PATH_KINDS else candidates(req))
    for c in forms:
        if rule.match_type == "exact" and c == value:
            return True
        if rule.match_type == "prefix" and c.startswith(value):
            return True
        if rule.match_type == "glob" and fnmatch.fnmatchcase(c, value):
            return True
    return False


# -- the engine -------------------------------------------------------------------------------


def all_rules(repo_root: str | None = None) -> list[Rule]:
    """Every rule in force: defaults, the repo's denies, the user's and
    learned rules."""
    return [*DEFAULT_RULES, *(repo_rules(repo_root) if repo_root else []), *load_store().rules]


def profile_rules(profile) -> list[Rule]:
    """Validated, profile-local deny rules. Profiles can narrow policy only."""
    out = []
    for spec in getattr(profile, "permission_denies", []):
        parts = spec.split(None, 2)
        if len(parts) != 3:
            continue
        kind, match_type, match = parts
        if kind in KINDS and match_type in MATCH_TYPES and match:
            out.append(Rule(kind, match, match_type, "deny", "profile"))
    return out


PROFILE_BASH = re.compile(r"Bash\(([^()*]+?)(:\*)?\)")


def profile_bash_allows(profile) -> list[Rule]:
    """The profile's ``allowed_tools`` shell entries as allow rules, for an
    agy worker under agy_approvals "brindle", which Claude Code's
    ``--allowedTools`` never reaches: ``Bash(git status)`` is that exact
    command, ``Bash(uv run:*)`` it alone or followed by arguments. Like
    every bash allow, a compound or quoted command never matches."""
    out = []
    for entry in getattr(profile, "allowed_tools", None) or []:
        m = PROFILE_BASH.fullmatch(entry.strip())
        if not m:
            continue
        command = m.group(1).strip()
        out.append(Rule("bash", command, "exact", "allow", "profile"))
        if m.group(2):
            out.append(Rule("bash", command + " ", "prefix", "allow", "profile"))
    return out


def invalid_profile_rules(profile) -> list[str]:
    """Profile deny specs ignored by the policy because they are malformed."""
    invalid = list(getattr(profile, "permission_denies_errors", []))
    for spec in getattr(profile, "permission_denies", []):
        parts = spec.split(None, 2)
        if len(parts) != 3 or parts[0] not in KINDS or parts[1] not in MATCH_TYPES or not parts[2]:
            invalid.append(spec)
    return invalid


def decide(req: Request | None, *, checks: list[str] | None = None,
           rules: list[Rule] | None = None) -> Decision:
    """The policy's answer for ``req``: deny > allow > ask."""
    if req is None or req.kind not in KINDS:
        return Decision("ask", "brindle couldn't read the request")
    if rules is None:
        rules = all_rules(req.repo_root or None)
    for wanted in ("deny", "allow"):
        for rule in rules:
            if rule.decision == wanted and rule_matches(rule, req, checks):
                verb = "denied" if wanted == "deny" else "allowed"
                return Decision(wanted, f"{verb} by {rule.source} rule {rule.id} ({rule.describe()})", rule)
    if req.kind == "bash" and req.command and simple_argv(req.command) is None:
        why = "the command isn't a single simple command, so no allow rule applies"
    elif req.kind == "read" and req.path:
        why = "not a file git tracks in the worktree, and no rule allows it"
    else:
        why = "no rule allows or denies it"
    return Decision("ask", f"{why}; asking the user")


# -- storage ----------------------------------------------------------------------------------


def store_path() -> Path:
    return brindle_home() / FILE


def repo_rules_path(repo_root: str) -> Path:
    return config_root(repo_root) / CONFIG_DIR / FILE


def repo_rules(repo_root: str) -> list[Rule]:
    """The repo's own rules: only denies (a repo may narrow, never broaden)."""
    try:
        data = json.loads(repo_rules_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("rules") if isinstance(data, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        rule = Rule.from_dict(row, source="repo") if isinstance(row, dict) else None
        if rule and rule.decision == "deny":
            out.append(rule)
    return out


@dataclass
class Store:
    rules: list[Rule] = field(default_factory=list)
    # pattern id -> {"kind", "match", "count", "last"}: approvals the person gave
    approvals: dict[str, dict] = field(default_factory=dict)
    # What brindle put in agy's settings.json (brindle.antigravity.sync_permissions):
    # {"allow": [...], "deny": [...]}, only entries brindle added itself, and the
    # containers it created ("file", "permissions", "permissions.allow", ...).
    agy_managed: dict[str, list] = field(default_factory=dict)
    agy_created: list[str] = field(default_factory=list)
    # repo root -> its `checks`, for each repo whose agy workers run with the
    # policy on: what the mirror covers.
    agy_repos: dict[str, list] = field(default_factory=dict)
    # The Codex hook the person trusted (brindle.codex_hook): command, key, hash.
    codex_hook: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        data: dict = {"version": 1, "rules": [r.to_dict() for r in self.rules], "approvals": self.approvals}
        for name in ("agy_managed", "agy_created", "agy_repos", "codex_hook"):
            if getattr(self, name):
                data[name] = getattr(self, name)
        return json.dumps(data, indent=2) + "\n"


def load_store() -> Store:
    path = store_path()
    if not path.exists():
        store = Store(rules=list(PRESET_RULES))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(store.to_json(), encoding="utf-8")
        except OSError:
            pass
        return store
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Store()
    if not isinstance(data, dict):
        return Store()
    rules = [r for r in (Rule.from_dict(d) for d in data.get("rules") or [] if isinstance(d, dict))
             if r and r.source in ("user", "learned")]
    approvals = data.get("approvals")
    approvals = {k: v for k, v in approvals.items() if isinstance(v, dict)} if isinstance(approvals, dict) else {}

    def strings(value: object) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    managed = data.get("agy_managed")
    managed = {k: strings(v) for k, v in managed.items() if k in ("allow", "deny")} if isinstance(managed, dict) else {}
    repos = data.get("agy_repos")
    repos = {k: strings(v) for k, v in repos.items()} if isinstance(repos, dict) else {}
    codex = data.get("codex_hook")
    codex = {k: v for k, v in codex.items() if isinstance(v, str)} if isinstance(codex, dict) else {}
    return Store(rules, approvals, managed, strings(data.get("agy_created")), repos, codex)


@contextmanager
def editing() -> Iterator[Store]:
    """Load, let the caller change, and write back the user's store, under a
    lock so concurrent hooks don't lose each other's writes."""
    import fcntl

    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = load_store()
        yield store
        tmp = path.with_suffix(".tmp")
        tmp.write_text(store.to_json(), encoding="utf-8")
        os.replace(tmp, path)


def add_rule(kind: str, match: str, decision: str, match_type: str = "exact",
             source: str = "user") -> Rule:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: one of {', '.join(KINDS)}")
    if match_type not in MATCH_TYPES:
        raise ValueError(f"unknown match type {match_type!r}: one of {', '.join(MATCH_TYPES)}")
    if decision not in ("allow", "deny"):
        raise ValueError("decision must be allow or deny")
    if not match:
        raise ValueError("empty match")
    rule = Rule(kind, match, match_type, decision, source, time.time())
    with editing() as store:
        store.rules = [r for r in store.rules if r.id != rule.id] + [rule]
    return rule


def forget(id_: str) -> str | None:
    """Remove the user or learned rule ``id_``, or the approval count behind
    suggestion ``id_``; what was removed, or None if neither exists."""
    with editing() as store:
        found = next((r for r in store.rules if r.id == id_), None)
        store.rules = [r for r in store.rules if r.id != id_]
        approval = store.approvals.pop(id_, None)
    if found:
        return f"{found.decision} {found.describe()} ({found.source})"
    if approval:
        return f"approvals of {approval.get('kind')} {approval.get('match')!r}"
    return None


def reset() -> None:
    """Back to the defaults: no user or learned rules, no approval counts."""
    with editing() as store:
        store.rules, store.approvals = [], {}


# -- learning ---------------------------------------------------------------------------------


def learnable_pattern(req: Request) -> tuple[str, str] | None:
    """The exact (kind, match) an approval of ``req`` counts toward, or None
    when an allow rule for it could never apply."""
    if req.kind == "bash":
        argv = simple_argv(req.command)
        return ("bash", " ".join(argv)) if argv and req.command.strip() == " ".join(argv) else None
    if req.kind in PATH_KINDS:
        if not req.path:
            return None
        real = os.path.realpath(req.path)
        for root in _roots(req):
            rel = _inside(real, root)
            if rel:
                return req.kind, rel
        return req.kind, real
    if req.kind == "fetch":
        return ("fetch", req.url) if req.url else None
    return (req.kind, req.tool) if req.tool else None


def record_approval(req: Request) -> str | None:
    """Count the person's approval of ``req``; the pattern's id, if any."""
    pattern = learnable_pattern(req)
    if pattern is None:
        return None
    kind, match = pattern
    pid = rule_id(kind, "exact", match, "allow")
    with editing() as store:
        entry = store.approvals.get(pid) or {"kind": kind, "match": match, "count": 0}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last"] = time.time()
        store.approvals[pid] = entry
    return pid


@dataclass(frozen=True)
class Suggestion:
    id: str
    kind: str
    match: str
    count: int


def suggestions(store: Store | None = None) -> list[Suggestion]:
    """Patterns approved at least SUGGEST_AFTER times that no rule covers yet."""
    store = store or load_store()
    have = {r.id for r in store.rules}
    out = []
    for pid, e in store.approvals.items():
        if pid in have or int(e.get("count", 0)) < SUGGEST_AFTER:
            continue
        if e.get("kind") in KINDS and e.get("match"):
            out.append(Suggestion(pid, str(e["kind"]), str(e["match"]), int(e["count"])))
    return sorted(out, key=lambda s: (-s.count, s.kind, s.match))


def accept(id_: str) -> Rule | None:
    """Turn suggestion ``id_`` into a learned allow rule."""
    s = next((s for s in suggestions() if s.id == id_), None)
    if s is None:
        return None
    return add_rule(s.kind, s.match, "allow", "exact", source="learned")


# -- mirroring into agy's own settings ---------------------------------------------------------
#
# agy ignores a hook's allow, so the allow rules that agy's rule syntax can say
# exactly (or more narrowly) are copied into its settings; denies too, as a
# second line behind the hook. Anything agy can't say without widening an
# allow is left out (the tracked-files read rule, globs, URLs).

_AGY_SIMPLE_CHARS = "[A-Za-z0-9_\\-./:=@%+, ]"


def _re_literal(text: str) -> str:
    """``text`` as an RE2 literal (escaping only what RE2 treats specially)."""
    return re.sub(r"([\\.+*?()|\[\]{}^$])", r"\\\1", text)


def _agy_path(match: str) -> str:
    return os.path.expanduser(match)


def _agy_entry(rule: Rule) -> str | None:
    """``rule`` in agy's syntax, or None when agy can't express it safely."""
    allow = rule.decision == "allow"
    if rule.kind == "bash":
        if rule.match_type == "exact":
            if allow and simple_argv(rule.match) is None:
                return None  # brindle itself never allows it
            return f"command(regex:^{_re_literal(rule.match.strip())}$)"
        if rule.match_type == "prefix":
            if allow:
                # Only what brindle would allow: the prefix, then simple characters.
                if not SIMPLE_COMMAND.fullmatch(rule.match):
                    return None
                return f"command(regex:^{_re_literal(rule.match)}{_AGY_SIMPLE_CHARS}*$)"
            return f"command(regex:^{_re_literal(rule.match)})"
        if rule.match_type == "glob" and not allow and not re.search(r"[\[\]]", rule.match):
            body = "".join(".*" if c == "*" else "." if c == "?" else _re_literal(c) for c in rule.match)
            return f"command(regex:^{body}$)"
        return None
    if rule.kind in PATH_KINDS:
        verb = "read_file" if rule.kind == "read" else "write_file"
        if rule.match_type == "exact":
            return f"{verb}({_agy_path(rule.match)})"
        if rule.match_type == "prefix" and rule.match.endswith("/"):
            return f"{verb}({_agy_path(rule.match)})"  # a folder: agy's is recursive too
        if rule.match_type == "glob" and not allow:
            # "<path>*" or "<dir>/*" with no other wildcard: agy's path rule
            # covers the path (or folder) itself, which is narrower; fine for a
            # deny the hook enforces anyway.
            stem = rule.match.rstrip("*")
            if stem and stem != rule.match and not re.search(r"[*?\[]", stem) and not stem.startswith("*"):
                path = _agy_path(stem)
                if os.path.isabs(path):
                    return f"{verb}({path.rstrip('/') or '/'})"
        return None
    if rule.kind == "mcp" and rule.match.startswith("mcp__"):
        parts = rule.match[len("mcp__"):].split("__", 1)
        if rule.match_type == "exact" and len(parts) == 2 and all(parts):
            return f"mcp({parts[0]}/{parts[1]})"
        if rule.match_type == "prefix" and len(parts) == 2 and parts[0] and not parts[1]:
            return f"mcp({parts[0]}/*)"
        return None
    return None


def mirror_agy(rules: list[Rule], checks: list[str]) -> dict[str, list[str]]:
    """The agy settings entries for ``rules`` (the built-in ones expanded:
    the read-only git commands as exact commands, git push as a deny), as
    {"allow": [...], "deny": [...]}. A repo's ``checks`` aren't mirrored:
    agy's settings apply to every project, and a check runs whatever that
    repo defines, so it's allowed only for brindle workers in that repo."""
    out: dict[str, list[str]] = {"allow": [], "deny": []}

    def add(decision: str, entry: str | None) -> None:
        if entry and entry not in out[decision]:
            out[decision].append(entry)

    for rule in rules:
        if rule.match_type == "check":
            continue
        elif rule.match_type == "git-readonly":
            for sub in READONLY_GIT:
                add("allow", f"command(regex:^git {sub}$)")
        elif rule.match_type == "git-push":
            add("deny", "command(git push)")
        elif rule.match_type in BUILTIN_MATCHERS:
            continue  # tracked files, force flags: only the hook can tell
        else:
            add(rule.decision, _agy_entry(rule))
    return out
