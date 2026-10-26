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

Precedence: any matching deny wins; then explicit ask; then allow; else ask.
Specificity never matters. An allow rule for bash only ever applies to a
single simple command (letters, digits, spaces and ``_-./:=@%+,`` only): a
command with any other character (``;``, ``|``, ``&``, ``$``, quotes, globs,
redirections, newlines...) is never auto-allowed as a whole. A list of
commands joined only by ``&&``, ``;`` or ``|`` is allowed when an allow rule
covers every part (word by word, as the shell will run it, with quoted or
escaped characters treated literally) and nothing else can run or write
(see compound_parts); a deny on any part denies it. Anything unknown or
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
import glob
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
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
STORE_VERSION = 2

# A command an allow rule may cover: one simple command, nothing the shell
# would treat specially.
SIMPLE_COMMAND = re.compile(r"[A-Za-z0-9_\-./:=@%+, ]+")
_NO_TILDE = re.compile(r"[^~]")
# SIMPLE_COMMAND plus unquoted pathname patterns, only to decide whether a
# restriction could apply. Never used to grant allows.
RESTRICTION_WORD = re.compile(r"[A-Za-z0-9_\-./:=@%+, *?\[\]]+")
# A heredoc's body, from its delimiter line to the closing line.
_HEREDOC = re.compile(r"<<-?[ \t]*([\"']?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
# The operators a compound command may join its parts with ("" ends it).
COMPOUND_SEPARATORS = ("&&", ";", "|", "")
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
    # The grant the person is offered with it, as JSON (brindle.permission_grants):
    # recorded with the request, so the digest the app answers binds it too.
    grant: str | None = None

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
    repo_root: str | None = None
    expires_at: float | None = None
    exclusions: tuple[Rule, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            identity = rule_id(self.kind, self.match_type, self.match, self.decision)
            if self.constrained:
                scope = json.dumps([self.repo_root, self.expires_at, [r.to_dict() for r in self.exclusions]],
                                   sort_keys=True)
                identity = hashlib.sha256((identity + scope).encode()).hexdigest()[:16]
            object.__setattr__(self, "id", identity)

    @property
    def constrained(self) -> bool:
        return bool(self.repo_root or self.expires_at is not None or self.exclusions)

    def describe(self) -> str:
        if self.match_type in BUILTIN_MATCHERS:
            return self.match
        return f"{self.kind} {self.match_type} {self.match!r}"

    def to_dict(self) -> dict:
        data = {"id": self.id, "kind": self.kind, "match": self.match, "match_type": self.match_type,
                "decision": self.decision, "source": self.source, "created": self.created}
        if self.constrained:
            data.update(repo_root=self.repo_root, expires_at=self.expires_at,
                        exclusions=[r.to_dict() for r in self.exclusions])
        return data

    @classmethod
    def from_dict(cls, data: dict, source: str | None = None, *, depth: int = 0) -> "Rule | None":
        """A rule read from a file, or None if it isn't a valid one."""
        # Future scope fields must not silently become unrestricted rules.
        known = {"id", "kind", "match", "match_type", "decision", "source", "created",
                 "repo_root", "expires_at", "exclusions", "editable", "protected"}
        if not isinstance(data, dict) or data.keys() - known:
            return None
        try:
            kind, match = str(data["kind"]), str(data["match"])
            match_type = str(data.get("match_type", "exact"))
            decision = str(data["decision"])
        except (KeyError, TypeError):
            return None
        matchers = (*MATCH_TYPES, *BUILTIN_MATCHERS) if depth else MATCH_TYPES
        if kind not in KINDS or match_type not in matchers or decision not in DECISIONS or not match:
            return None
        if depth > 8:
            return None
        repo = data.get("repo_root")
        if repo is not None and (not isinstance(repo, str) or not os.path.isabs(repo)):
            return None
        expires = data.get("expires_at")
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float))
                                    or not math.isfinite(expires) or expires <= 0):
            return None
        raw = data.get("exclusions", [])
        if not isinstance(raw, list) or len(raw) > 100:
            return None
        exclusions = [cls.from_dict(r, depth=depth + 1) if isinstance(r, dict) else None for r in raw]
        if any(r is None or r.kind != kind for r in exclusions):
            return None
        src = source or str(data.get("source", "user"))
        if src not in ("user", "learned", "repo"):
            src = "user"
        try:
            created = float(data.get("created") or 0)
        except (TypeError, ValueError):
            created = 0.0
        identity = data.get("id", "")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{8}|[0-9a-f]{16}", identity):
            identity = ""
        return cls(kind, match, match_type, decision, src, created, id=identity,
                   repo_root=os.path.realpath(repo) if repo else None,
                   expires_at=expires, exclusions=tuple(exclusions))


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
    _default("d-echo", "bash", "echo", "exact", "allow"),
    _default("d-echo-args", "bash", "echo ", "prefix", "allow"),
    _default("d-git-push", "bash", "git push", "git-push", "deny"),
    _default("d-git-force", "bash", "a git command with a force flag", "git-force", "deny"),
    _default("d-ssh", "read", "~/.ssh*", "glob", "deny"),
    _default("d-aws", "read", "~/.aws*", "glob", "deny"),
    _default("d-gnupg", "read", "~/.gnupg*", "glob", "deny"),
    # The local daemon's token: with it an agent could act as the person (brindle.daemon.auth).
    _default("d-daemon", "read", "*/.brindle/daemon.json*", "glob", "deny"),
    _default("d-daemon-sh", "bash", "*daemon.json*", "glob", "deny"),
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


def claude_answer(answer: str, suggestions: list | None = None) -> dict:
    """What the PermissionRequest hook prints for the person's answer from
    the app: allow_once, allow_always (with Claude Code's own suggested
    updates, which it then applies as if picked in its prompt) or deny."""
    if answer == "deny":
        decision: dict = {"behavior": "deny", "message": "brindle: the person denied this in the brindle app"}
    else:
        decision = {"behavior": "allow"}
        if answer == "allow_always" and suggestions:
            decision["updatedPermissions"] = suggestions
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def persistent_suggestions(suggestions: object) -> list | None:
    """Claude Code's suggested updates that persist an allow rule (addRules),
    nothing else: "Allow always" must not switch the session into a mode like
    acceptEdits, nor only open a directory for the session. None when no such
    rule is suggested (brindle's own exact rule is offered then)."""
    if not isinstance(suggestions, list):
        return None
    kept = [u for u in suggestions
            if isinstance(u, dict) and u.get("type") == "addRules" and u.get("behavior", "allow") == "allow"
            and u.get("rules") and u.get("destination") != "session"]
    return kept or None


def request_digest(request: str, suggestions: str | None) -> str:
    """The exact request (its recorded JSON) and suggested rule a person was
    shown: the app's answer carries it, and the daemon hands the waiting
    hook only an answer to the digest the hook registered for its own
    request (brindle.daemon.holds), so a stored request changed while it
    waits gets no answer."""
    return hashlib.sha256(json.dumps([request, suggestions]).encode()).hexdigest()


def describe_updates(updates: list) -> str:
    """Claude Code's permission updates in a line: "allow Bash(npm test:*) (localSettings)"."""
    parts = []
    for u in updates:
        if not isinstance(u, dict):
            continue
        if u.get("type") in ("addRules", "replaceRules"):
            rules = [f"{r.get('toolName')}({r['ruleContent']})" if r.get("ruleContent") else str(r.get("toolName"))
                     for r in u.get("rules") or [] if isinstance(r, dict)]
            text = f"{u.get('behavior', 'allow')} {', '.join(rules)}"
        elif u.get("type") == "addDirectories":
            text = "access to " + ", ".join(map(str, u.get("directories") or []))
        elif u.get("type") == "setMode":
            text = f"permission mode {u.get('mode')}"
        else:
            text = str(u.get("type"))
        parts.append(text + (f" ({u['destination']})" if u.get("destination") else ""))
    return "; ".join(parts)


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
        return _patch_requests(patch, cwd, base)
    return [Request(kind="other", **base)]


def _patch_requests(patch: str | None, cwd: str, base: dict) -> list[Request]:
    """One Request per file an apply_patch touches."""
    files = [(CODEX_PATCH_KINDS[m.group(1)], m.group(2)) for m in CODEX_PATCH_FILE.finditer(patch or "")]
    files += [("write", m.group(1)) for m in CODEX_PATCH_MOVE.finditer(patch or "")]
    out = list(dict.fromkeys((k, _abs_path(f, cwd)) for k, f in files))
    reqs = [Request(kind=k, path=path, **base) for k, path in out]
    if not files or not _codex_patch_accounted(patch or ""):
        # An unknown file prevents allow, but must not erase a known deny.
        reqs.append(Request(kind="edit", **base))
    return reqs


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
    for d in decisions:
        if d.decision == "ask" and d.rule is not None:
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


# -- providers: OpenCode -----------------------------------------------------------------------
#
# brindle's OpenCode plugin (brindle.opencode) asks for a decision from its
# tool.execute.before hook, which runs for every tool call: {tool, args}.
# Only a deny is acted on (the hook throws); allow and ask leave the call to
# OpenCode's own permission config.

OPENCODE_KINDS = {
    "read": "read", "glob": "read", "grep": "read", "list": "read",
    "write": "write", "edit": "edit", "patch": "edit", "multiedit": "edit",
    "bash": "bash", "webfetch": "fetch",
}


def from_opencode(payload: dict, worktree: str = "", repo_root: str = "") -> list[Request] | None:
    """The plugin's {tool, args} as Requests (one per file an ``apply_patch``
    touches); None when it names no tool."""
    tool = payload.get("tool")
    if not isinstance(tool, str) or not tool:
        return None
    args = payload.get("args")
    args = args if isinstance(args, dict) else {}
    cwd = str(payload.get("cwd") or worktree or "")
    if tool == "apply_patch":   # what OpenCode gives GPT-5/Codex models in place of edit and write
        patch = args.get("patchText") if isinstance(args.get("patchText"), str) else None
        return _patch_requests(patch, cwd, dict(provider="opencode", tool=tool, cwd=cwd,
                                                worktree=worktree, repo_root=repo_root))
    kind = OPENCODE_KINDS.get(tool, "other")
    command = path = url = None
    if kind == "bash":
        command = args.get("command") if isinstance(args.get("command"), str) else None
    elif kind in PATH_KINDS:
        raw = args.get("filePath") or args.get("path")
        if raw is None and tool in ("glob", "grep", "list"):
            raw = cwd  # searches the working directory
        if isinstance(raw, str) and raw:
            path = _abs_path(raw, cwd)
    elif kind == "fetch":
        url = args.get("url") if isinstance(args.get("url"), str) else None
    return [Request("opencode", kind, tool, cwd, worktree, repo_root, command, path, url)]


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
    if decision is not None and decision.decision == "ask" and decision.rule is not None:
        return {"decision": "ask", "reason": f"brindle: {decision.reason}"}
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
                  "({why}). If a simpler command would do (one command, no $(...), no "
                  "redirection) or a file tool (view_file, grep_search, list_dir) gets you what "
                  "you need, use that instead. Only if you still need this, {ask}, so the person "
                  "can allow it with `brindle permissions allow`.")
ASK_SUPERVISOR = "tell your supervisor with send_message what you need and why"
ASK_PERSON = "say what you need and why in your reply to the person running brindle"
TOLD_SUPERVISOR = ("carry on with other work and retry it once your supervisor says it's allowed: "
                   "brindle has told your supervisor")


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


def _in_nested_root(p: str, root: str, roots: list[str], f) -> bool:
    """``p`` lies in another of ``roots`` that sits inside ``root`` (a worktree
    under its repo's .brindle/worktrees): its folders are judged against that
    root, where the worktree's own .agents/.git/.brindle are still protected."""
    for other in (f(o) for o in roots if o):
        if other != root and _lexically_within(other, root) and _lexically_within(p, other):
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
        for p, f in ((literal, os.path.normpath), (real, os.path.realpath)):
            r = f(root)
            if _in_nested_root(p, r, roots, f):
                continue
            rel = os.path.relpath(p, r)
            if rel != ".." and not rel.startswith(".." + os.sep):
                if names & {part.casefold() for part in rel.split(os.sep)}:
                    return True
        if _in_nested_root(real, os.path.realpath(root), roots, os.path.realpath):
            continue
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
                      protected_roots: list[str] | None = None,
                      readable: list[str] | None = None,
                      has_supervisor: bool = True, escalated: bool = False) -> dict:
    """agy's PreToolUse answer under ``agy_approvals: "brindle"``, where agy
    runs with --dangerously-skip-permissions: a hook's "ask" then runs the
    call without a prompt, so brindle answers allow or deny, never ask.
    Allowed: what a rule allows, a file inside a workspace (or the
    conversation's artifact folder, passed in ``workspaces``), a read
    inside one of ``readable``, brindle's own tools, and agy's
    conversation-only tools. Everything else is denied with a reason that
    tells the agent to try a simpler way, then ask its supervisor (or, with
    no supervisor, the person); ``escalated``, brindle tells the supervisor
    itself (brindle.antigravity._escalate)."""
    if decision is not None and decision.decision == "deny":
        return {"decision": "deny", "reason": f"brindle: {decision.reason}"}
    if decision is not None and decision.decision == "allow":
        return {"decision": "allow", "reason": f"brindle: {decision.reason}"}
    if req is not None and not (decision is not None and decision.rule is not None):
        path = os.path.realpath(req.path) if req.kind in PATH_KINDS and req.path else None
        # Protected wherever the workspaces list says the workspace is: the
        # anchors are brindle's own worktree and repo root.
        literal = os.path.normpath(req.path) if path else None
        protected = (path and req.kind != "read"
                     and _agy_protected(literal, path, protected_roots or []))
        # In the workspace as written, not only once resolved: a path that
        # gets there through a symlink outside it would skip the symlink walk.
        def within(roots: list[str] | None) -> bool:
            return any(r and _lexically_within(literal, r) and (path == os.path.realpath(r) or _inside(path, r))
                       for r in roots or [])

        if path and not protected and within(workspaces):
            return {"decision": "allow", "reason": "brindle: a file in the workspace"}
        if path and req.kind == "read" and within(readable):
            return {"decision": "allow", "reason": "brindle: reading agy's own instructions"}
        if req.kind == "mcp" and req.tool in _brindle_tools():
            return {"decision": "allow", "reason": "brindle: brindle's own tool"}
        if req.kind == "other" and req.tool in AGY_CONVERSATION_TOOLS:
            return {"decision": "allow", "reason": "brindle: acts only on the conversation"}
    why = decision.reason if decision is not None else "brindle couldn't read the request"
    why = why.removesuffix("; asking the user")  # nobody is asked here
    ask = TOLD_SUPERVISOR if escalated else ASK_SUPERVISOR if has_supervisor else ASK_PERSON
    return {"decision": "deny", "reason": "brindle: " + NEEDS_APPROVAL.format(why=why, ask=ask)}


def _allow_specs(rules: list[Rule], checks: list[str] | None) -> list[str]:
    """The bash allow rules and checks as uncovered_part's specs."""
    specs = [c.strip() for c in checks or []]
    for rule in rules:
        if rule.kind == "bash" and rule.decision == "allow" and rule.match.strip() and not rule.constrained:
            if rule.match_type == "exact":
                specs.append(rule.match.strip())
            elif rule.match_type == "prefix":
                specs.append(rule.match.strip() + ":*")
    return specs


def bash_grants(req: Request, rules: list[Rule], checks: list[str] | None = None,
                cd_root: str | None = None) -> list[str] | None:
    """The exact bash allows that would let ``req`` through allowlist_decision
    on a retry: one per simple command no rule covers yet. None when no exact
    allow can (``$(...)``, a redirection that writes, a heredoc, a word with
    spaces in it, a cd it can't vouch for, ...)."""
    from brindle.native.permissions import uncovered_part

    if req.kind != "bash" or not req.command:
        return None
    specs, grants = _allow_specs(rules, checks), []
    while (part := uncovered_part(specs, req.command, cd_root=cd_root)) is not None:
        if part in grants:
            return None  # granting it didn't cover it
        grants.append(part)
        specs.append(part)
    return grants


def allowlist_decision(req: Request | None, rules: list[Rule], checks: list[str] | None = None,
                       cd_root: str | None = None) -> Decision | None:
    """For a shell command no rule allows as a whole: allow it when every
    simple command in it (``a && b | c`` has three) is named by an exact or
    prefix bash allow rule or a check, the reading Claude Code's PreToolUse
    hook gives a profile's allowed_tools (brindle.agents.pre_tool_decision);
    a ``cd`` inside ``cd_root`` counts too. A rule denying any one part wins.
    None when neither applies, or for what uncovered_part can't vouch for
    (a redirection that writes a file, a heredoc, ...)."""
    from dataclasses import replace

    from brindle.native.permissions import _tokens, split_commands, uncovered_part

    if req is None or req.kind != "bash" or not req.command:
        return None
    whole = decide(req, checks=checks, rules=rules)
    if whole.decision == "deny" or (whole.decision == "ask" and whole.rule is not None):
        return whole
    # The shell rewrites an unquoted tilde word (``~/x``, ``a=~/x``,
    # ``a=b:~/x``) after this check; HEAD~1 stays a literal (see _tokens).
    if _tokens(req.command, word_pattern=_NO_TILDE) is None:
        return None
    if uncovered_part(_allow_specs(rules, checks), req.command, cd_root=cd_root) is not None:
        return None
    for part in split_commands(req.command):
        denied = decide(replace(req, command=part), checks=checks, rules=rules)
        if denied.decision == "deny" or (denied.decision == "ask" and denied.rule is not None):
            return denied
    return Decision("allow", "every part of the command is allowed by a rule or the profile's allowed_tools")


# -- matching ---------------------------------------------------------------------------------


def simple_argv(command: str | None) -> list[str] | None:
    """The words of a single simple command with no shell metacharacters,
    else None."""
    if not command or not SIMPLE_COMMAND.fullmatch(command):
        return None
    argv = command.split()
    return argv or None


def compound_parts(command: str) -> list[list[str]] | None:
    """The words of each simple command in a list joined only by ``&&``,
    ``;`` or ``|``, when unquoted word characters are plain text
    (SIMPLE_COMMAND; quoted or escaped characters are literal); else None.
    Nothing beyond those commands may run
    or write: no command substitution, backticks, subshell, process
    substitution, heredoc, input from a file (a read no read rule saw) or
    redirection other than an fd dup or output to /dev/null."""
    from brindle.native.permissions import _simple_commands, _tokens

    if "$(" in command or "`" in command:
        return None
    tokens = _tokens(command, word_pattern=SIMPLE_COMMAND)
    if tokens is None or any(is_op and "<" in text for is_op, text in tokens):
        return None
    parsed = _simple_commands(command)
    if not parsed or any(after not in COMPOUND_SEPARATORS for _, after in parsed):
        return None
    return [words for words, _ in parsed]


def _part_decision(req: Request, words: list[str], checks: list[str] | None,
                   rules: list[Rule]) -> Decision:
    """One part of a compound command. A deny reads the words re-joined at
    every space, so a spaced word can't dodge it. An allow must see the
    words as the shell will run them: ``"./check --safe"`` is one word,
    a different program from ``./check --safe``. Words without spaces
    re-join to exactly that; a word with spaces can only fall in the
    arguments of a prefix rule whose words come before it. The same holds
    for literal metacharacters: re-parsing them without their quotes would
    mistake argument text for shell syntax."""
    from brindle.native.permissions import _joined

    denied = _matching_restriction([replace(req, command=_joined(words))], checks, rules)
    if denied is not None:
        return denied
    if all(SIMPLE_COMMAND.fullmatch(word) and " " not in word for word in words):
        return decide(replace(req, command=" ".join(words)), checks=checks, rules=rules)
    for rule in rules:
        head = rule.match.split()
        if (rule.kind == "bash" and rule.decision == "allow" and rule.match_type == "prefix"
                and not rule.constrained and head and words[:len(head)] == head):
            return Decision("allow", f"allowed by {rule.source} rule {rule.id} ({rule.describe()})", rule)
    return Decision("ask", "no rule allows it")


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


def _readonly_command(rule: Rule, req: Request, argv: list[str]) -> bool:
    """Validate the arguments of a read-only git probe. There is no built-in
    curl read: where a host name leads is only known when curl connects, so
    any fetch, even https to a public-looking name, needs a rule or approval."""
    if rule.kind != "bash" or not argv or rule.match_type != "git-readonly" or argv[0] != "git":
        return False
    args = argv[1:]
    if args[:1] == ["-C"]:
        if len(args) < 3:
            return False
        path = os.path.realpath(os.path.join(req.cwd or req.worktree, args[1]))
        if not any(path == os.path.realpath(root) or _inside(path, root) for root in _roots(req)):
            return False
        args = args[2:]
    return bool(args and args[0] in READONLY_GIT
                and not any(a.startswith(GIT_UNSAFE_OPTIONS) or _is_force(a) for a in args[1:]))


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
        return bool(argv and _readonly_command(rule, req, argv))
    if rule.match_type == "git-push":
        return any(sub == "push" for sub, _ in _git_calls(command))
    if rule.match_type == "git-force":
        return any(_is_force(a) for _, args in _git_calls(command) for a in args)
    return False


def rule_matches(rule: Rule, req: Request, checks: list[str] | None = None) -> bool:
    if rule.kind != req.kind:
        return False
    # Store guards also cover requests without a target (e.g. a provider's
    # search tool without a URL). File data cannot create these reserved IDs.
    if rule.source == "default" and rule.id == f"d-store-unsupported-{req.kind}":
        return True
    if rule.repo_root and (not req.repo_root or os.path.realpath(req.repo_root) != rule.repo_root):
        return False
    if rule.expires_at is not None and time.time() >= rule.expires_at:
        return False
    # Exclusion scopes use restrictive matching (including literal path aliases),
    # even when the excluded rule originally allowed the scope.
    if any(rule_matches(replace(r, decision="deny"), req, checks) for r in rule.exclusions):
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
    store = load_store()
    return [*DEFAULT_RULES, *(repo_rules(repo_root) if repo_root else []), *store.rules, *store.blocking_rules]


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
    """The profile's ``allowed_tools`` shell entries as allow rules, for a
    Codex worker or agy worker under agy_approvals "brindle", which Claude Code's
    ``--allowedTools`` never reaches: ``Bash(git status)`` is that exact
    command, ``Bash(uv run:*)`` it alone or followed by arguments. Like
    every bash allow, a compound or quoted command never matches."""
    return [rule for entry in getattr(profile, "allowed_tools", None) or [] for rule in _profile_bash(entry)]


def _profile_bash(entry: str) -> list[Rule]:
    m = PROFILE_BASH.fullmatch(entry.strip())
    if not m:
        return []
    command = m.group(1).strip()
    out = [Rule("bash", command, "exact", "allow", "profile")]
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
    """The policy's answer for ``req``: deny > explicit ask > allow > fallback ask."""
    if req is None or req.kind not in KINDS:
        return Decision("ask", "brindle couldn't read the request")
    if rules is None:
        rules = all_rules(req.repo_root or None)
    # Normalize restrictions even for simple commands: spacing and leading
    # assignments must not hide explicit asks from provider/profile fallback.
    restricted = _restriction_in_parts(req, checks=checks, rules=rules)
    for wanted in ("deny", "ask", "allow"):
        if restricted is not None and restricted.decision == wanted:
            return restricted
        for rule in rules:
            if rule.decision == wanted and rule_matches(rule, req, checks):
                verb = {"deny": "denied", "ask": "approval required", "allow": "allowed"}[wanted]
                return Decision(wanted, f"{verb} by {rule.source} rule {rule.id} ({rule.describe()})", rule)
    if req.kind == "bash" and req.command and simple_argv(req.command) is None:
        parts = compound_parts(req.command)
        if parts:
            answers = [_part_decision(req, words, checks, rules) for words in parts]
            for answer in answers:
                if answer.decision == "deny":
                    return answer
            for answer in answers:
                if answer.decision == "ask" and answer.rule is not None:
                    return answer
            if all(answer.decision == "allow" for answer in answers):
                return Decision("allow", "every part of the command is allowed by a rule")
        why = "the command isn't a single simple command, so no allow rule applies"
    elif req.kind == "read" and req.path:
        why = "not a file git tracks in the worktree, and no rule allows it"
    else:
        why = "no rule allows or denies it"
    return Decision("ask", f"{why}; asking the user")


def _restriction_parts(command: str, *, word_pattern: re.Pattern[str] = SIMPLE_COMMAND) -> list[list[str]] | None:
    """Command arguments without redirects, solely for restrictive matching.

    Unlike the allow parser, this accepts output files. With RESTRICTION_WORD
    it also accepts unquoted pathname patterns in arguments, for deciding
    whether a restriction could apply, not for literal matching. Quoted
    operators remain arguments; redirects and their operands are removed.
    Unsupported syntax is unknown, never evidence that restrictions are absent.
    """
    from brindle.native.permissions import _SEPARATORS, _tokens

    if "$(" in command or "`" in command:
        return None
    tokens = _tokens(command, word_pattern=word_pattern)
    if tokens is None:
        return None
    parts: list[list[str]] = []
    words: list[str] = []
    index = 0
    while index < len(tokens):
        operator, value = tokens[index]
        if not operator:
            words.append(value)
        elif value in _SEPARATORS:
            if words:
                parts.append(words)
                words = []
            elif value != "\n":
                return None
        elif value in (">", ">>", ">|", "<", "<>", ">&", "<&", "&>", "&>>"):
            index += 1
            if index >= len(tokens) or tokens[index][0]:
                return None
            if word_pattern is RESTRICTION_WORD and any(c in tokens[index][1] for c in "*?["):
                return None  # an expanded redirect can select a protected file
        else:
            return None
        index += 1
    if words:
        parts.append(words)
    elif not parts or tokens[-1][1] in ("&&", "||", "|", "|&"):
        return None
    for words in parts:
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words.pop(0)
        if not words or words[0] in ("!", "{", "}", "if", "then", "else", "elif", "fi", "for", "while",
                                     "until", "do", "done", "case", "esac", "select", "function", "[[", "time", "coproc"):
            return None
        # A pattern in the executable position can select a different program.
        if word_pattern is RESTRICTION_WORD and any(c in words[0] for c in "*?["):
            return None
    return parts


def _globs_excluded(req: Request, parts: list[list[str]], rules: list[Rule],
                    path_denies: list[Rule]) -> bool:
    """Whether no restriction can apply to what the patterns in ``parts``
    expand to. Plain ``ls`` has a proof: listing names can't push, force
    a git action or read a file's contents, so the default git and daemon
    denies don't apply, nor do exact/prefix rules that don't overlap the
    words before its first pattern. A READ_ONLY_PROGRAMS one (or a sed that
    only prints) has one once its patterns are expanded: it only reads its
    files, so it is enough that no restriction matches the expanded command
    and no deny covers a file it names. Any other program may treat an
    expanded name as a subcommand, option or file to act on."""
    moved = any(words[0] in ("cd", "pushd", "popd") for words in parts)
    for words in parts:
        first = next((i for i, word in enumerate(words) if any(c in word for c in "*?[")), None)
        if first is None:
            continue
        if words[0] in (*READ_ONLY_PROGRAMS, "sed") and not moved:
            if not _reads_excluded(req, words, rules, path_denies):
                return False
            continue
        if words[0] != "ls":
            return False
        prefix = " ".join(" ".join(words[:first]).split()) + " "
        for rule in rules:
            if rule.source == "default" and rule.id in ("d-git-push", "d-git-force", "d-daemon-sh"):
                continue
            if (rule.match_type in ("exact", "prefix")
                    and not prefix.startswith(rule.match) and not rule.match.startswith(prefix)):
                continue
            return False
    return True


# Programs that only read the files they're given; a pattern among their
# arguments is checked by expanding it (_reads_excluded).
READ_ONLY_PROGRAMS = ("grep", "head", "cat", "wc")


def _reads_excluded(req: Request, words: list[str], rules: list[Rule], path_denies: list[Rule]) -> bool:
    """Whether the read-only command ``words`` is clear of ``rules`` and of
    ``path_denies`` once its patterns are expanded the way the shell would
    in ``req.cwd``. Dotfiles are expanded too, and each pattern is also
    checked as the literal name it stays when nothing matches. Patterns the
    expansion here can't read exactly (``[[:alpha:]]``) are never clear."""
    cwd = req.cwd or req.worktree
    if not cwd:
        return False
    argv, names = [words[0]], []
    for word in words[1:]:
        if any(c in word for c in "*?["):
            if "[:" in word or "[=" in word or "[." in word:
                return False
            found = sorted(glob.glob(word.replace("[^", "[!"), root_dir=cwd, include_hidden=True))
            argv += found or [word]
            names += [word, *found]
        else:
            argv.append(word)
            names.append(word)
    if argv[0] == "sed" and not _sed_prints_only(argv[1:]):
        return False
    if _matching_restriction([replace(req, command=" ".join(argv))], None, rules) is not None:
        return False
    reads = [replace(req, kind="read", command=None, path=os.path.join(cwd, name)) for name in names]
    return not any(rule_matches(rule, read) for rule in path_denies for read in reads)


def _sed_prints_only(args: list[str]) -> bool:
    """Whether sed with ``args`` runs under -n and its scripts only print:
    no -i or -f, no w/W/e/r/R/a/i/c commands and no s flags beyond g, p, I
    and a number."""
    quiet, scripts, rest, i = False, [], [], 0
    while i < len(args):
        arg = args[i]
        option = re.fullmatch(r"-([nErsuz]*)(e(.*))?", arg, re.DOTALL)
        if arg in ("--quiet", "--silent"):
            quiet = True
        elif arg.startswith("--expression="):
            scripts.append(arg.partition("=")[2])
        elif arg == "--expression" or option and option.group(2) and not option.group(3):
            if i + 1 >= len(args):
                return False
            scripts.append(args[i + 1])
            quiet = quiet or bool(option and "n" in option.group(1))
            i += 1
        elif option and arg != "-":
            quiet = quiet or "n" in option.group(1)
            if option.group(2):
                scripts.append(option.group(3))
        elif arg.startswith("-") and arg != "-":
            return False
        else:
            rest.append(arg)
        i += 1
    if not scripts and rest:
        scripts.append(rest[0])
    return quiet and bool(scripts) and all(_sed_script_prints_only(s) for s in scripts)


def _sed_script_prints_only(script: str) -> bool:
    i = 0
    while i < len(script):
        c = script[i]
        if c in " \t\n;!{}0123456789,$~+" or c in "pPlqQnNdDhHgGx=":
            i += 1
        elif c in "/\\":
            delim, start = ("/", i + 1) if c == "/" else (script[i + 1:i + 2], i + 2)
            end = _sed_delimited(script, start, delim)
            if end is None:
                return False
            i = end + 1
            while i < len(script) and script[i] in "IM":
                i += 1
        elif c in "sy":
            delim = script[i + 1:i + 2]
            if not delim or delim in "\n\\":
                return False
            middle = _sed_delimited(script, i + 2, delim)
            end = _sed_delimited(script, middle + 1, delim) if middle is not None else None
            if end is None:
                return False
            i = end + 1
            while c == "s" and i < len(script) and script[i] in "gpI0123456789":
                i += 1
        else:
            return False
    return True


def _sed_delimited(script: str, start: int, delim: str) -> int | None:
    """The index of the first unescaped ``delim`` from ``start``."""
    i = start
    while i < len(script):
        if script[i] == "\\":
            i += 2
        elif script[i] == delim:
            return i
        else:
            i += 1
    return None


def _matching_restriction(parts: list[Request], checks: list[str] | None,
                          rules: list[Rule]) -> Decision | None:
    for wanted in ("deny", "ask"):
        for part in parts:
            for rule in rules:
                if rule.decision == wanted and rule_matches(rule, part, checks):
                    verb = "denied" if wanted == "deny" else "approval required"
                    return Decision(wanted, f"{verb} by {rule.source} rule {rule.id} ({rule.describe()})", rule)
    return None


def _restriction_in_parts(req: Request, *, checks: list[str] | None,
                          rules: list[Rule]) -> Decision | None:
    """Find explicit restrictions without treating shell segments as allows."""
    if req.kind != "bash" or not req.command:
        return None
    # Unsupported syntax is not itself a restriction. Only an active Bash
    # restriction can require us to withhold the provider's ordinary fallback.
    path_denies = [r for r in rules if r.kind == "read" and r.decision == "deny"]
    rules = [r for r in rules if r.kind == "bash" and r.decision in ("deny", "ask")
             and (not r.repo_root or bool(req.repo_root) and os.path.realpath(req.repo_root) == r.repo_root)
             and (r.expires_at is None or time.time() < r.expires_at)]
    if not rules:
        return None
    if "$(" in req.command or "`" in req.command:
        substituted = _substituted(req, checks, rules)
        if isinstance(substituted, Decision):
            return substituted
        req = substituted
    parsed = _restriction_parts(req.command)
    patterns = _restriction_parts(req.command, word_pattern=RESTRICTION_WORD) if parsed is None else None
    words = parsed if parsed is not None else patterns if patterns is not None else _segments(req.command)
    parts = [replace(req, command=" ".join(" ".join(part).split())) for part in words]
    matched = _matching_restriction(parts, checks, rules)
    if matched is not None:
        return matched
    if patterns is not None:
        uncertain = not _globs_excluded(req, patterns, rules, path_denies)
    else:
        uncertain = parsed is None and any(_may_restrict(rule, req.command) for rule in rules)
    return _syntax_ask(req) if uncertain else None


def _syntax_ask(req: Request) -> Decision:
    # A bound built-in ask prevents provider/profile fallback from treating
    # unsupported shell syntax as an ordinary unmatched request.
    return Decision("ask", "shell syntax cannot be checked safely",
                    Rule("bash", req.command, "exact", "ask", "default", id="d-shell-syntax"))


# Options whose value is only data, by (program, subcommand): the one place a
# $(...) or backtick substitution may supply a word without approval. Its
# inner commands are still checked.
SUBSTITUTION_DATA_OPTIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("git", "commit"): ("-m", "--message"),
    ("git", "tag"): ("-m", "--message"),
}
# Stands in for a substitution's output once its inner commands are checked.
_SUBSTITUTED = "BRINDLESUBSTITUTION"


def _substituted(req: Request, checks: list[str] | None, rules: list[Rule]) -> Request | Decision:
    """``req`` with each top-level substitution replaced by a placeholder
    word, or the decision that ends the check. A substitution always runs its
    inner commands, so they get the whole restriction check (recursively,
    quoted or not); of a quoted heredoc fed to cat, only the cat is checked,
    as its body is data. Its output is unknown text, so it may only be the
    entire content of one double-quoted word (unquoted, the shell splits it
    into words) given to an option in SUBSTITUTION_DATA_OPTIONS, in a command
    with no leading assignments."""
    from brindle.native.permissions import _simple_commands

    spans = _substitution_spans(req.command)
    if spans is None:
        return _syntax_ask(req)
    for _, _, body, _ in spans:
        inner = _restriction_in_parts(replace(req, command="cat" if _heredoc_data(body) else body),
                                      checks=checks, rules=rules)
        if inner is not None:
            return inner
    pieces, last = [], 0
    for start, end, _, quoted in spans:
        if not (quoted and req.command[start - 1] == '"' and req.command[end:end + 1] == '"'):
            return _syntax_ask(req)
        pieces += [req.command[last:start], _SUBSTITUTED]
        last = end
    command = "".join(pieces) + req.command[last:]
    parsed = _simple_commands(command)
    if parsed is None:
        return _syntax_ask(req)
    for words, _ in parsed:
        for i, word in enumerate(words):
            if _SUBSTITUTED in word and not (
                    word == _SUBSTITUTED and i >= 3
                    and words[i - 1] in SUBSTITUTION_DATA_OPTIONS.get((words[0], words[1]), ())
                    and not any(_could_cover(rule, f"{words[0]} {words[1]}") for rule in rules)):
                return _syntax_ask(req)
    return replace(req, command=command)


def _could_cover(rule: Rule, head: str) -> bool:
    """Whether a custom ``rule`` could match some command starting with
    ``head``, whatever a substituted message makes of the rest: unless its
    fixed leading text and ``head`` diverge, it might. The defaults are
    known not to apply to a commit or tag message."""
    if rule.source == "default":
        return False
    if rule.match_type not in MATCH_TYPES:
        return True
    fixed = re.split(r"[*?\[]", rule.match, maxsplit=1)[0] if rule.match_type == "glob" else rule.match
    fixed = " ".join(fixed.split())
    return head.startswith(fixed) or fixed.startswith(head)


def _substitution_spans(command: str) -> list[tuple[int, int, str, bool]] | None:
    """(start, end, inner command, inside double quotes) of each outermost
    ``$(...)`` or backtick substitution, or None when one isn't closed."""
    spans, quote, i = [], "", 0
    while i < len(command):
        c = command[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote else '"'
        elif command.startswith("$(", i) or c == "`":
            end = _substitution_end(command, i)
            if end is None:
                return None
            spans.append((i, end, command[i + (1 if c == "`" else 2):end - 1], quote == '"'))
            i = end
            continue
        i += 1
    return spans


def _substitution_end(command: str, i: int) -> int | None:
    """The index just past the substitution starting at ``i``: a backtick
    ends at the next unescaped one; ``$(`` at its matching ``)``, skipping
    quotes, nested substitutions and heredoc bodies inside it."""
    if command[i] == "`":
        j = i + 1
        while j < len(command):
            if command[j] == "\\":
                j += 2
            elif command[j] == "`":
                return j + 1
            else:
                j += 1
        return None
    depth, j = 1, i + 2
    while j < len(command):
        c = command[j]
        heredoc = re.match(r"<<-?[ \t]*([\"']?)(\w+)\1", command[j:]) if c == "<" else None
        if c == "\\":
            j += 2
        elif c == "'":
            close = command.find("'", j + 1)
            if close < 0:
                return None
            j = close + 1
        elif c == '"':
            j += 1
            while j < len(command) and command[j] != '"':
                if command[j] == "\\":
                    j += 2
                elif command.startswith("$(", j) or command[j] == "`":
                    end = _substitution_end(command, j)
                    if end is None:
                        return None
                    j = end
                else:
                    j += 1
            if j >= len(command):
                return None
            j += 1
        elif command.startswith("$(", j) or c == "`":
            end = _substitution_end(command, j)
            if end is None:
                return None
            j = end
        elif heredoc is not None:
            close = re.compile(r"\n[ \t]*" + re.escape(heredoc.group(2)) + r"[ \t]*(?=\n|\)|$)")
            found = close.search(command, j + heredoc.end())
            if found is None:
                return None
            j = found.end()
        else:
            depth += {"(": 1, ")": -1}.get(c, 0)
            j += 1
            if depth == 0:
                return j
    return None


def _heredoc_data(body: str) -> bool:
    """Whether ``body`` only prints a quoted heredoc (``cat <<'EOF'``): its
    text is never expanded or run, and its first delimiter line is the last."""
    head, _, rest = body.strip(" \t\n").partition("\n")
    found = re.fullmatch(r"cat[ \t]+<<(-?)[ \t]*([\"'])(\w+)\2[ \t]*", head)
    if found is None:
        return False
    lines = rest.split("\n")
    strip = "\t" if found.group(1) else ""
    ends = [n for n, line in enumerate(lines) if line.lstrip(strip) == found.group(3)]
    return bool(ends) and ends[0] == len(lines) - 1


def _may_restrict(rule: Rule, command: str) -> bool:
    """Whether ``rule`` could match ``command`` once the shell expands it:
    the text the rule needs appears in the command, as written or with its
    quotes and escapes removed (``r''m -r\\f`` is ``rm -rf``). Expansions
    that assemble a word from pieces (``$'\\x72m'``, ``$X``, ``${X}``,
    ``{r,}m``) could produce any text, so they always could. A pathname
    pattern (``[p]ush``, ``--f*``) could be any word it matches; heredoc
    bodies are never expanded into names. Single-quoted text is never
    expanded either (``awk '{print $1}'``)."""
    if re.search(r"\$['\"{\w@*#?!$-]|\{[^}\s]*,", _unquoted(command)):
        return True
    # The shell deletes \<newline> before anything else: r\<newline>m is rm.
    text = command + "\n" + re.sub(r"[\"'\\]", "", command.replace("\\\n", ""))
    names = _HEREDOC.sub("\n", text)
    patterns = [w for w in re.split(r"[\s;&|()<>`]+", names) if any(c in w for c in "*?[")]

    def seen(word: str) -> bool:
        return word in text or any(fnmatch.fnmatchcase(word, p) for p in patterns)

    if rule.match_type == "git-push":
        return seen("git") and seen("push")
    if rule.match_type == "git-force":
        return seen("git") and (any(_is_force(w.strip("`$(){};|&<>")) for w in text.split())
                                or any(seen(flag) for flag in ("-f", *FORCE_OPTIONS)))
    if rule.match_type not in MATCH_TYPES:
        return True
    literals = re.split(r"[*?\[\]]+", rule.match) if rule.match_type == "glob" else [rule.match]
    return all(seen(word) for literal in literals for word in literal.split())


def _unquoted(command: str) -> str:
    """``command`` with the text inside single quotes removed (the quotes
    stay, so ``$'...'`` still reads as an expansion). A single quote inside
    double quotes or after a backslash is literal and starts nothing."""
    out, quote, i = [], "", 0
    while i < len(command):
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = ""
                out.append(c)
        elif c == "\\":
            out.append(command[i:i + 2])
            i += 1
        else:
            if c == "'" and not quote or c == '"':
                quote = "" if quote else c
            out.append(c)
        i += 1
    return "".join(out)


def deny_in_parts(req: Request, *, checks: list[str] | None = None,
                  rules: list[Rule]) -> Decision | None:
    """A deny inside a shell command; explicit asks are handled by decide."""
    decision = _restriction_in_parts(req, checks=checks, rules=[r for r in rules if r.decision == "deny"])
    return decision if decision is not None and decision.decision == "deny" else None


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
    out, unsupported = [], []
    for row in rows if isinstance(rows, list) else []:
        rule = Rule.from_dict(row, source="repo") if isinstance(row, dict) else None
        if rule and rule.decision == "deny":
            out.append(rule)
        elif rule is None:
            unsupported.append(row)
    return [*out, *Store(unsupported_rules=unsupported).blocking_rules]


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
    # The Codex hook the person trusted (brindle.codex_hook), per Codex config
    # home (its resolved path): command, key, hash, definition.
    codex_hook: dict[str, dict[str, str]] = field(default_factory=dict)
    remembered: list[dict] = field(default_factory=list)
    # Keep rows this engine cannot interpret out of editable rules, but retain
    # their complete representation across unrelated writes (including grants).
    unsupported_rules: list[object] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    unsupported_version: bool = False

    @property
    def blocking_rules(self) -> list[Rule]:
        """Uninterpretable policy cannot silently defer to provider allows.

        Unknown scopes may narrow a rule in ways we cannot evaluate. Block its
        whole kind until a capable engine reads it; unknown kinds or store
        versions block every kind. These guards are runtime-only, never saved
        in place of the original policy or eligible for one-time overrides.
        """
        kinds = set(KINDS) if self.unsupported_version or self.extra else set()
        for row in self.unsupported_rules:
            kind = row.get("kind") if isinstance(row, dict) else None
            # An allow we can't read just doesn't apply; a restriction might.
            if isinstance(row, dict) and row.get("decision") == "allow" and kind in KINDS:
                continue
            kinds.update((kind,) if kind in KINDS else KINDS)
        return [_default(f"d-store-unsupported-{kind}", kind, "*", "glob", "deny")
                for kind in KINDS if kind in kinds]

    def require_writable(self) -> None:
        if self.unsupported_version:
            raise ValueError("permissions.json uses an unsupported version; use a compatible engine to edit it")

    def to_json(self) -> str:
        self.require_writable()
        data: dict = {**self.extra, "version": STORE_VERSION,
                      "rules": [r.to_dict() for r in self.rules] + self.unsupported_rules,
                      "approvals": self.approvals}
        for name in ("agy_managed", "agy_created", "agy_repos", "codex_hook", "remembered"):
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
    # Version 1 was shared by engines with different rule capabilities. Upgrade
    # known fields, but read future versions fail-closed and never rewrite them.
    # Older shipped engines that ignore this field cannot be protected here.
    version = data.get("version", 1)
    if type(version) is not int or not 1 <= version <= STORE_VERSION:
        return Store(unsupported_version=True)
    rules, unsupported = [], []
    rows = data.get("rules", [])
    for row in rows if isinstance(rows, list) else [rows]:
        rule = Rule.from_dict(row)
        if rule and rule.source in ("user", "learned"):
            rules.append(rule)
        else:
            unsupported.append(row)
    approvals = data.get("approvals")
    approvals = {k: v for k, v in approvals.items() if isinstance(v, dict)} if isinstance(approvals, dict) else {}

    def strings(value: object) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    managed = data.get("agy_managed")
    managed = {k: strings(v) for k, v in managed.items() if k in ("allow", "deny")} if isinstance(managed, dict) else {}
    repos = data.get("agy_repos")
    repos = {k: strings(v) for k, v in repos.items()} if isinstance(repos, dict) else {}
    codex = data.get("codex_hook") if isinstance(data.get("codex_hook"), dict) else {}
    if isinstance(codex.get("command"), str):
        # One record from before trust was kept per home: it was the person's own Codex home's.
        from brindle import provider_homes

        codex = {os.path.realpath(provider_homes.source("codex")): codex}
    codex = {home: {k: v for k, v in rec.items() if isinstance(v, str)}
             for home, rec in codex.items() if isinstance(rec, dict)}
    remembered = data.get("remembered")
    remembered = [r for r in remembered if isinstance(r, dict)] if isinstance(remembered, list) else []
    known = {"version", "rules", "approvals", "agy_managed", "agy_created", "agy_repos", "codex_hook", "remembered"}
    extra = {k: v for k, v in data.items() if k not in known}
    return Store(rules, approvals, managed, strings(data.get("agy_created")), repos, codex, remembered,
                 unsupported_rules=unsupported, extra=extra)


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
        store.require_writable()
        yield store
        staged = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=f".{path.name}.", delete=False) as out:
                staged = out.name
                out.write(store.to_json())
            os.replace(staged, path)
        finally:
            if staged is not None and os.path.exists(staged):
                os.unlink(staged)


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
        # IDs survive editor retargeting. Deduplicate by the current complete
        # scope, never by an ID that used to describe this pattern.
        existing = next((r for r in store.rules if
                         replace(r, id=rule.id, created=rule.created, source=rule.source) == rule), None)
        if existing is not None:
            rule = replace(rule, id=existing.id)
        else:
            occupied = {r.id for r in store.rules}
            candidate, sequence = rule.id, 0
            while candidate in occupied:
                sequence += 1
                candidate = hashlib.sha256(f"{rule.id}:{sequence}".encode()).hexdigest()[:16]
            rule = replace(rule, id=candidate)
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
        store.rules, store.approvals, store.remembered = [], {}, []


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
    if rule.constrained or rule.decision == "ask":
        return None
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
        if rule.constrained or rule.decision == "ask":
            continue
        # The CLI cannot express exclusions, expiry, repo identity or explicit ask.
        # Leave allows of an affected kind to the hook instead of broadening them.
        if rule.decision == "allow" and any(r.kind == rule.kind and
                                           (r.constrained or r.decision == "ask") for r in rules):
            continue
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


def profile_agy_entries(spec: str) -> list[str]:
    """One ``allowed_tools`` entry in agy's settings syntax, no wider than
    ``profile_bash_allows`` (a prefix takes only simple characters after
    it), or [] when agy can't say it."""
    return [e for e in (_agy_entry(rule) for rule in _profile_bash(spec)) if e]
