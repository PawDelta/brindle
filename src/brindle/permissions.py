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
against the URL; mcp/other against the tool name. A few named matchers
(``tracked``, ``check``, ``git-readonly``, ``git-push``, ``git-force``,
``push``, ``worktree``, ``readonly-pipeline``) are code, not patterns: a
``push`` rule's match is ``"<source> → <destination>"`` (globs on both sides;
``@default`` is the repo's default branch), matched against every ``git push``
in the command.

Nothing is built in: every rule in force lives in the person's store, where
it can be edited or removed, the ones a preset seeded included (see
``PRESETS``: Autonomous, the default for a new store, Permissive, Standard
and Secure; ``apply_preset`` seeds the store and records the choice). A
rule's ``decision`` is allow, deny or ask: an ask rule names what a person
must decide, with ``limits`` on how far a saved approval may reach (its
``max_scope`` level or pattern, and ``life``: session, time with ``minutes``,
or forever). What no rule covers follows the store's ``unmatched`` setting
(ask, allow or deny). A rule may carry ``expires`` (a timed rule) and
``exclusions`` (requests it doesn't apply to: a deny's exceptions).

Saved actions (``SavedAction``, from the app's Inbox) are the person's own
allow and deny decisions, with a structured scope, the exact request they
came from and an expiry for timed ones. A saved deny stops a matching ask;
a saved allow resolves one only within the ask rule's limits, never past a
deny rule, and never once expired (see brindle.permission_grants).

Precedence: any matching deny wins; else any matching ask (then a saved
action may settle it); else any matching allow; else the unmatched setting.
Specificity never matters. An allow rule for bash only ever applies to a
single simple command (letters, digits, spaces and ``_-./:=@%+,`` only): a
command with any other character (``;``, ``|``, ``&``, ``$``, quotes, globs,
redirections, newlines...) is never auto-allowed as a whole. A list of
commands joined only by ``&&``, ``;`` or ``|`` is allowed when an allow rule
covers every part (word by word, as the shell will run it, with quoted or
escaped characters treated literally) and nothing else can run or write
(see compound_parts); a deny on any part denies it. Anything unknown or
unparseable is ask.

Where rules come from (``source``): ``preset`` (seeded by the chosen preset),
``repo`` (``<repo>/.brindle/permissions.json``: only its deny rules are used,
so a repo can narrow but never broaden), ``user`` (``brindle permissions
allow|deny`` or the app) and ``learned`` (a suggestion the person accepted).
Preset, user and learned rules live in ``~/.brindle/permissions.json`` with
the saved actions, the approval counts learning keeps and the asks of the
last day (per-rule prompt counts).

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
import secrets
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
BUILTIN_MATCHERS = ("tracked", "check", "git-readonly", "git-push", "git-force", "push", "worktree",
                    "readonly-pipeline", "rm-destructive")
SOURCES = ("preset", "repo", "user", "learned", "profile")
STORE_SOURCES = ("preset", "user", "learned")    # what the person's store holds
UNMATCHED = ("ask", "allow", "deny")             # Store.unmatched: what no rule covers
# How far a saved approval of a push may reach (an ask rule's limits.max_scope), narrowest first.
LEVELS = ("branch", "destination", "remote", "repository", "all")
LIVES = ("session", "time", "forever")           # how long a rule's approvals, or a saved action, last
PUSH_ARROW = " → "                                # a push rule's "<source> → <destination>"
DEFAULT_BRANCH = "@default"                       # in a push pattern: the repo's default branch
SUGGEST_AFTER = 2
PROMPT_WINDOW = 86400.0                           # seconds of asks kept for per-rule prompt counts
FILE = "permissions.json"

# A command an allow rule may cover: one simple command, nothing the shell
# would treat specially.
SIMPLE_COMMAND = re.compile(r"[A-Za-z0-9_\-./:=@%+, ]+")
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


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def clean_limits(limits: object) -> dict | None:
    """An ask rule's limits, validated: ``max_scope`` (a LEVELS entry for
    pushes, else a pattern a saved action must stay inside), ``life`` and,
    for life "time", ``minutes``. None when nothing valid is given."""
    if not isinstance(limits, dict):
        return None
    out: dict = {}
    scope = limits.get("max_scope")
    if isinstance(scope, str) and scope:
        out["max_scope"] = scope
    life = limits.get("life")
    if life in LIVES:
        out["life"] = life
        minutes = _number(limits.get("minutes"))
        if life == "time":
            if minutes is None or minutes <= 0:
                return None
            out["minutes"] = minutes
    return out or None


@dataclass(frozen=True)
class Rule:
    kind: str
    match: str
    match_type: str
    decision: str
    source: str = "user"
    created: float = 0.0
    id: str = ""
    limits: dict | None = field(default=None, hash=False)   # an ask rule's: how far a saved approval may reach
    expires: float | None = None        # a timed rule: epoch seconds after which it no longer applies
    exclusions: tuple = ()              # Rules: a request any of them matches is not this rule's
    label: str = ""                     # a person's name for it (the app's)

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", rule_id(self.kind, self.match_type, self.match, self.decision))
        if not isinstance(self.exclusions, tuple):
            object.__setattr__(self, "exclusions", tuple(self.exclusions))

    def describe(self) -> str:
        if self.match_type in BUILTIN_MATCHERS and not (self.match_type == "push" and PUSH_ARROW in self.match):
            return self.match
        return f"{self.kind} {self.match_type} {self.match!r}"

    def expired(self, now: float | None = None) -> bool:
        return self.expires is not None and self.expires <= (time.time() if now is None else now)

    def to_dict(self) -> dict:
        out = {"id": self.id, "kind": self.kind, "match": self.match, "match_type": self.match_type,
               "decision": self.decision, "source": self.source, "created": self.created}
        if self.limits:
            out["limits"] = dict(self.limits)
        if self.expires is not None:
            out["expires"] = self.expires
        if self.exclusions:
            out["exclusions"] = [x.to_dict() for x in self.exclusions]
        if self.label:
            out["label"] = self.label
        return out

    @classmethod
    def from_dict(cls, data: dict, source: str | None = None) -> "Rule | None":
        """A rule read from a file, or None if it isn't a valid one."""
        try:
            kind, match = str(data["kind"]), str(data["match"])
            match_type = str(data.get("match_type", "exact"))
            decision = str(data["decision"])
        except (KeyError, TypeError):
            return None
        if (kind not in KINDS or match_type not in (*MATCH_TYPES, *BUILTIN_MATCHERS)
                or decision not in DECISIONS or not match):
            return None
        src = source or str(data.get("source", "user"))
        if src not in (*STORE_SOURCES, "repo"):
            src = "user"
        try:
            created = float(data.get("created") or 0)
        except (TypeError, ValueError):
            created = 0.0
        expires = _number(data.get("expires"))
        exclusions = tuple(x for x in (cls.from_dict(d, src) for d in data.get("exclusions") or []
                                       if isinstance(d, dict)) if x)
        id_ = data.get("id") if isinstance(data.get("id"), str) else ""
        label = data.get("label") if isinstance(data.get("label"), str) else ""
        return cls(kind, match, match_type, decision, src, created, id_,
                   clean_limits(data.get("limits")) if decision == "ask" else None, expires, exclusions, label)


@dataclass
class SavedAction:
    """A decision the person saved from the Inbox: allow or deny requests of
    ``kind`` (a KINDS entry, or ``push``) whose key (brindle.permission_grants
    .ask_key) ``pattern`` covers. ``scope``/``level`` say which rung of the
    scope ladder the pattern is (exact, general, repository, ...); ``request``
    is the exact request it was saved from; ``life`` session (of ``session``,
    a root agent), time (``minutes``, until ``expires``) or forever; ``repo``
    the repo root it applies in, or None for every repo."""
    id: str
    kind: str
    pattern: str
    decision: str                       # allow | deny
    scope: str = "exact"
    level: str | None = None
    request: dict = field(default_factory=dict)
    life: str = "forever"
    minutes: float | None = None
    expires: float | None = None
    session: str | None = None
    repo: str | None = None
    created: float = 0.0
    note: str = ""

    def expired(self, now: float | None = None) -> bool:
        return self.expires is not None and self.expires <= (time.time() if now is None else now)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", {})}

    @classmethod
    def from_dict(cls, data: dict) -> "SavedAction | None":
        try:
            id_, kind, pattern, decision = (str(data["id"]), str(data["kind"]), str(data["pattern"]),
                                            str(data["decision"]))
        except (KeyError, TypeError):
            return None
        if kind not in (*KINDS, "push") or decision not in ("allow", "deny") or not pattern or not id_:
            return None
        life = data.get("life") if data.get("life") in LIVES else "forever"
        request = data.get("request") if isinstance(data.get("request"), dict) else {}
        text = {k: (data.get(k) if isinstance(data.get(k), str) else None) for k in ("level", "session", "repo")}
        return cls(id_, kind, pattern, decision, str(data.get("scope") or "exact"), text["level"], request, life,
                   _number(data.get("minutes")), _number(data.get("expires")), text["session"], text["repo"],
                   _number(data.get("created")) or 0.0, str(data.get("note") or ""))


@dataclass(frozen=True)
class Decision:
    decision: str  # allow | deny | ask
    reason: str
    rule: Rule | None = None


def rule_id(kind: str, match_type: str, match: str, decision: str) -> str:
    return hashlib.sha1(f"{kind}\0{match_type}\0{match}\0{decision}".encode()).hexdigest()[:8]


# -- presets ----------------------------------------------------------------------------------
#
# A preset is a named starting set of ordinary rules (source "preset") and an
# ``unmatched`` setting. Choosing one replaces the preset-sourced rules in the
# store and leaves the person's own and learned rules alone. Every seeded rule
# can be edited or removed afterwards: none is built in.


def _seed(id_: str, kind: str, match: str, match_type: str, decision: str, **extra) -> Rule:
    return Rule(kind, match, match_type, decision, "preset", 0.0, id_, **extra)


def _preset(kind: str, match: str, match_type: str, decision: str) -> Rule:
    return Rule(kind, match, match_type, decision, "preset", 0.0, "")


PRESET_COMMANDS = ("aws", "uv", "pytest", "python", "npm", "pnpm", "yarn", "docker", "cargo", "go", "make", "gh")
# The everyday commands the Standard, Permissive and Autonomous presets allow outright.
PRESET_RULES = tuple(
    rule
    for cmd in PRESET_COMMANDS
    for rule in (
        _preset("bash", cmd, "exact", "allow"),
        _preset("bash", f"{cmd} ", "prefix", "allow"),
    )
)

# Credential files and the daemon's token (with it an agent could act as the
# person, brindle.daemon.auth): denied by every preset, as ordinary rules.
CREDENTIAL_DENIES: tuple[Rule, ...] = (
    _seed("d-ssh", "read", "~/.ssh*", "glob", "deny"),
    _seed("d-aws", "read", "~/.aws*", "glob", "deny"),
    _seed("d-gnupg", "read", "~/.gnupg*", "glob", "deny"),
    _seed("d-daemon", "read", "*/.brindle/daemon.json*", "glob", "deny"),
    _seed("d-daemon-sh", "bash", "*daemon.json*", "glob", "deny"),
    _seed("d-env", "read", "*/.env*", "glob", "deny"),
    _seed("d-env-rel", "read", ".env*", "glob", "deny"),
)
# What every preset lets a worker do without asking: read what git tracks, run
# the repo's checks, the read-only git commands and read-only pipelines.
ROUTINE_ALLOWS: tuple[Rule, ...] = (
    _seed("d-tracked", "read", "a file git tracks in the worktree or repo root", "tracked", "allow"),
    _seed("d-checks", "bash", "exactly one of the repo's `checks` commands", "check", "allow"),
    _seed("d-git-ro", "bash", "git status / diff / log / show", "git-readonly", "allow"),
    _seed("d-readonly", "bash", "a pipeline of read-only commands (grep, ls, cat, head, sed -n ...)",
          "readonly-pipeline", "allow"),
)
DESTRUCTIVE_DENIES: tuple[Rule, ...] = (
    _seed("d-git-force", "bash", "a git command with a force flag, or a push that forces or deletes a branch",
          "git-force", "deny"),
    _seed("d-rm", "bash", "a recursive rm of the root, a top-level folder or the home directory",
          "rm-destructive", "deny"),
)
# The rules of before presets, kept for stores written then (load_store migrates them).
LEGACY_DEFAULT_IDS = ("d-tracked", "d-checks", "d-git-ro", "d-git-push", "d-git-force", "d-ssh", "d-aws",
                      "d-gnupg", "d-daemon", "d-daemon-sh", "d-env", "d-env-rel")


def _push_default(max_scope: str, life: str, minutes: float | None = None) -> Rule:
    """Pushes into the repo's default branch ask, with limits on saved approvals."""
    limits = {"max_scope": max_scope, "life": life}
    if minutes is not None:
        limits["minutes"] = minutes
    return _seed("d-push-default", "bash", f"*{PUSH_ARROW}{DEFAULT_BRANCH}", "push", "ask", limits=limits)


def _autonomous() -> list[Rule]:
    return [*ROUTINE_ALLOWS, *PRESET_RULES, *CREDENTIAL_DENIES, *DESTRUCTIVE_DENIES,
            _push_default("all", "forever")]


def _permissive() -> list[Rule]:
    return [*ROUTINE_ALLOWS, *PRESET_RULES,
            _seed("d-read-worktree", "read", "a file in the worktree", "worktree", "allow"),
            _seed("d-write-worktree", "write", "a file in the worktree", "worktree", "allow"),
            _seed("d-edit-worktree", "edit", "a file in the worktree", "worktree", "allow"),
            *CREDENTIAL_DENIES, *DESTRUCTIVE_DENIES, _push_default("remote", "forever")]


def _standard() -> list[Rule]:
    return [*ROUTINE_ALLOWS, *PRESET_RULES, *CREDENTIAL_DENIES, *DESTRUCTIVE_DENIES,
            _push_default("destination", "time", 30)]


def _secure() -> list[Rule]:
    short = {"max_scope": "*", "life": "time", "minutes": 15}
    return [*ROUTINE_ALLOWS[:3], *CREDENTIAL_DENIES, *DESTRUCTIVE_DENIES,
            _push_default("branch", "time", 15),
            _seed("d-write-ask", "write", "*", "glob", "ask", limits=short),
            _seed("d-edit-ask", "edit", "*", "glob", "ask", limits=short),
            _seed("d-bash-ask", "bash", "*", "glob", "ask", limits=short),
            _seed("d-fetch-ask", "fetch", "*", "glob", "ask", limits=short)]


# name -> (rules, unmatched, one-line description). Rules are built on each
# call so PRESET_RULES can be adjusted (tests) before a store is seeded.
PRESETS: dict[str, tuple] = {
    "autonomous": (_autonomous, "allow",
                   "Agents work without stopping. Everything runs automatically except pushes into the "
                   "default branch and destructive actions."),
    "permissive": (_permissive, "ask",
                   "Fewer interruptions for trusted teams. Edits in the worktree and everyday commands run; "
                   "the rest asks, and approvals can be saved for good."),
    "standard": (_standard, "ask",
                 "Routine reads and everyday commands run freely. Changes and sensitive actions ask within "
                 "a defined scope."),
    "secure": (_secure, "deny",
               "Narrow scopes, 15-minute approvals and a deny for anything no rule covers."),
}
DEFAULT_PRESET = "autonomous"       # for a store that has none yet (a new person)
MIGRATED_PRESET = "standard"        # for a store from before presets: what its rules amounted to


def preset_rules(name: str) -> list[Rule]:
    """The rules preset ``name`` seeds (ValueError for an unknown name)."""
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}: one of {', '.join(PRESETS)}")
    return list(PRESETS[name][0]())


def preset_unmatched(name: str) -> str:
    return PRESETS[name][1]


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
               rules: list[Rule] | None = None, **context) -> Decision:
    """One answer for several requests (an apply_patch touching several
    files): deny if any is denied, allow only if every one is allowed, else
    ask. ``context``: decide's other keyword arguments (session ...)."""
    if not reqs:
        return Decision("ask", "brindle couldn't read the request")
    decisions = [decide(r, checks=checks, rules=rules, **context) for r in reqs]
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
    # An ask a rule asks for explicitly is the person's to decide: not even a
    # file in the workspace runs on agy's default then.
    explicit = decision is not None and decision.rule is not None and decision.rule.decision == "ask"
    if req is not None and not explicit:
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
        if rule.kind == "bash" and rule.decision == "allow" and rule.match.strip():
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

    from brindle.native.permissions import split_commands, uncovered_part

    if req is None or req.kind != "bash" or not req.command:
        return None
    if uncovered_part(_allow_specs(rules, checks), req.command, cd_root=cd_root) is not None:
        return None
    for part in split_commands(req.command):
        got = decide(replace(req, command=part), checks=checks, rules=rules, unmatched="ask", saved=[])
        if got.decision == "deny":
            return got
        if got.rule is not None and got.rule.decision == "ask":
            return None    # a part an ask rule covers is the person's to decide, whatever the profile allows
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

    denied = decide(replace(req, command=_joined(words)), checks=checks,
                    rules=[rule for rule in rules if rule.decision == "deny"], unmatched="ask", saved=[])
    if denied.decision == "deny":
        return denied
    if all(SIMPLE_COMMAND.fullmatch(word) and " " not in word for word in words):
        return decide(replace(req, command=" ".join(words)), checks=checks, rules=rules, unmatched="ask", saved=[])
    for rule in rules:
        head = rule.match.split()
        if (rule.kind == "bash" and rule.decision == "allow" and rule.match_type == "prefix"
                and head and words[:len(head)] == head):
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


# -- pushes -------------------------------------------------------------------------------------

# git push options that take a value as a separate word.
PUSH_VALUE_OPTIONS = ("-o", "--push-option", "--receive-pack", "--exec", "--repo")
PUSH_ALL_OPTIONS = ("--all", "--branches", "--mirror", "--tags")
UNKNOWN_REF = "?"   # a push whose source or destination the command doesn't say (its upstream's)
PUSH_AT = " @ "     # an ask key's "<source> → <destination> @ <remote>"


@dataclass(frozen=True)
class Push:
    """One ``git push`` as the command spells it: the remote, and the source
    and destination branch (UNKNOWN_REF where git's configuration or the
    checkout decides: no refspec, ``HEAD``, ``@{u}``, ``--all``). ``force``:
    a force flag or a ``+refspec``; ``delete``: it deletes the destination."""
    remote: str
    source: str
    destination: str
    force: bool = False
    delete: bool = False

    def key(self) -> str:
        return f"{self.source}{PUSH_ARROW}{self.destination}{PUSH_AT}{self.remote}"


def _branch_name(ref: str) -> str:
    """A refspec side as a branch name, or UNKNOWN_REF when only git can tell
    which branch it means (HEAD, @{upstream}, a revision expression)."""
    ref = ref.removeprefix("refs/heads/")
    if not ref or ref == "HEAD" or ref.startswith("@") or any(c in ref for c in "~^@{}"):
        return UNKNOWN_REF
    return ref


def push_calls(command: str) -> list[Push]:
    """Every ``git push`` in ``command``, one per refspec."""
    out = []
    for sub, args in _git_calls(command):
        if sub != "push":
            continue
        positional, delete, force, everything, i = [], False, False, False, 0
        while i < len(args):
            a = args[i]
            if a in ("-d", "--delete"):
                delete = True
            elif _is_force(a):
                force = True
            elif a in PUSH_ALL_OPTIONS or a == "--prune":
                everything = True
            elif a in PUSH_VALUE_OPTIONS:
                i += 1
            elif not a.startswith("-"):
                positional.append(a)
            i += 1
        remote = positional[0] if positional else UNKNOWN_REF
        refspecs = positional[1:] or [UNKNOWN_REF]
        if everything:
            refspecs = [UNKNOWN_REF]
        for spec in refspecs:
            forced = force or spec.startswith("+")
            spec = spec.lstrip("+")
            if spec == UNKNOWN_REF:
                src = dst = UNKNOWN_REF
            elif delete:
                src, dst = "(delete)", _branch_name(spec.split(":", 1)[-1])
            elif ":" in spec:
                left, right = spec.split(":", 1)
                src, dst = (_branch_name(left) if left else "(delete)"), _branch_name(right)
                delete = delete or not left
            else:
                src = dst = _branch_name(spec)
            out.append(Push(remote, src, dst, forced, delete or src == "(delete)"))
    return out


_DEFAULT_BRANCHES: dict[str, tuple[float, str]] = {}


def default_branch(repo_root: str) -> str | None:
    """The repo's default branch (brindle.git), remembered a minute."""
    if not repo_root:
        return None
    hit = _DEFAULT_BRANCHES.get(repo_root)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        from brindle import git

        name = git.default_branch(repo_root)
    except Exception:  # noqa: BLE001 - not a repo: no default branch to match
        name = None
    _DEFAULT_BRANCHES[repo_root] = (time.monotonic() + 60, name)
    return name


def push_pattern(match: str) -> tuple[str, str, str] | None:
    """The (source, destination, remote) globs of a push pattern
    ``"<source> → <destination>[ @ <remote>]"``, or None."""
    if PUSH_ARROW not in match:
        return None
    match, _, remote = match.partition(PUSH_AT)
    src, dst = (part.strip() for part in match.split(PUSH_ARROW, 1))
    return (src or "*", dst or "*", remote.strip() or "*")


def _ref_matches(pattern: str, ref: str, repo_root: str, loose: bool) -> bool:
    """``ref`` under a push pattern's glob; an unknown ref matches only ``*``,
    unless ``loose`` (a deny or ask, which errs toward matching)."""
    if pattern == DEFAULT_BRANCH:
        pattern = default_branch(repo_root) or "main"
    if ref == UNKNOWN_REF:
        return loose or pattern == "*"
    return fnmatch.fnmatchcase(ref, pattern)


def push_fits(match: str, push: Push, repo_root: str, loose: bool) -> bool:
    """Whether one push fits push pattern ``match`` (see _ref_matches)."""
    pattern = push_pattern(match)
    if pattern is None:
        return False
    src_pat, dst_pat, remote_pat = pattern
    return (_ref_matches(src_pat, push.source, repo_root, loose) and _ref_matches(dst_pat, push.destination, repo_root, loose)
            and _ref_matches(remote_pat, push.remote, repo_root, loose))


def push_matches(match: str, req: Request, loose: bool) -> bool:
    """Whether any git push in ``req``'s command fits push pattern ``match``."""
    if not req.command:
        return False
    return any(push_fits(match, push, req.repo_root, loose) for push in push_calls(req.command))


# -- read-only pipelines -------------------------------------------------------------------------
#
# A command whose every part only reads (grep, ls, cat, head ...) joined by
# pipes or &&, with quoting the shell may read any way (an escaped regex), is
# safe to run even when it can't be parsed into exact allow rules. Anything
# that writes a file (a redirection other than to /dev/null), substitutes a
# command, or runs a program not on the list goes through the normal decision.

# program -> the options that make it write a file or run another program
# (any argument starting with one of them refuses the command). Only programs
# audited for such options are listed; ``uniq``'s second operand is an output
# file, so it takes one operand at most (see _readonly_part).
READONLY_PROGRAMS: dict[str, tuple[str, ...]] = {
    "grep": (), "egrep": (), "fgrep": (), "rg": ("--pre",), "ag": (),
    "ls": (), "cat": (), "head": (), "tail": (), "wc": (), "nl": (), "tac": (), "rev": (), "fold": (),
    "expand": (), "unexpand": (), "cut": (), "tr": (), "paste": (), "comm": (), "column": (), "seq": (),
    "sort": ("-o", "--output", "--compress-program", "-S"), "uniq": (),
    "diff": (), "cmp": (), "file": ("-C", "--compile"), "stat": (), "du": (), "df": (), "pwd": (),
    "echo": (), "printf": (), "which": (), "whereis": (), "type": (), "basename": (), "dirname": (),
    "realpath": (), "readlink": (), "true": (), "false": (), "test": (), "whoami": (), "uname": (),
    "printenv": (), "strings": (), "od": (), "hexdump": (), "md5": (), "md5sum": (), "shasum": (),
    "sha256sum": (), "jq": (), "yq": ("-i", "--inplace"), "tree": ("-o",), "locate": (),
}
ONE_OPERAND = frozenset({"uniq"})
FIND_WRITING = ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls", "-fprint0")
# A sed script that only prints: an optional address (line numbers, $, /re/)
# then p, P, l or =. Anything else (s///w, w, r, e ...) may write or run.
SED_PRINT = re.compile(r"^(\d+|\$|/[^/]*/|\d+,(\d+|\$)|/[^/]*/,/[^/]*/)?\s*[pPl=]$")
SED_SCRIPT_OPTIONS = ("-e", "--expression")


def _sed_readonly(args: list[str]) -> bool:
    """``sed -n`` printing only: every script (``-e``, ``--expression=`` or the
    first operand) must be a print command; no in-place edit, no script file."""
    scripts, operands, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a in SED_SCRIPT_OPTIONS:
            if i + 1 >= len(args):
                return False
            scripts.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-e") and a != "-e":
            scripts.append(a[2:])
        elif a.startswith(("-i", "--in-place", "-f", "--file", "-s")):
            return False
        elif a.startswith("-") and a != "-":
            if not re.fullmatch(r"-n|--quiet|--silent|-E|-r|--regexp-extended|--posix|-z|--null-data|-u|--unbuffered|--debug", a):
                return False
        else:
            operands.append(a)
        i += 1
    if not any(a in ("-n", "--quiet", "--silent") for a in args):
        return False
    if not scripts:
        if not operands:
            return False
        scripts, operands = operands[:1], operands[1:]
    return all(SED_PRINT.fullmatch(s.strip()) for s in scripts)


def _readonly_part(words: list[str]) -> bool:
    if not words:
        return False
    program = os.path.basename(words[0])
    args = words[1:]
    if program == "git":
        argv = [a for a in args if not a.startswith("-")]
        return bool(argv and argv[0] in READONLY_GIT
                    and not any(a.startswith(GIT_UNSAFE_OPTIONS) or _is_force(a) for a in args))
    if program == "find":
        return not any(a in FIND_WRITING for a in args)
    if program == "sed":
        return _sed_readonly(args)
    if program in ("fd", "fdfind"):
        # -x/-X run a program; a short-option cluster may hide them.
        return not any(a.startswith(("--exec", "--exec-batch", "-l")) or (a.startswith("-") and not a.startswith("--")
                                                                            and any(c in a for c in "xX"))
                       for a in args)
    if program == "tee":
        return not any(not a.startswith("-") for a in args)   # tee with no file only echoes
    if program == "hostname":
        return not any(not a.startswith("-") for a in args)   # with an operand it sets the name
    forbidden = READONLY_PROGRAMS.get(program)
    if forbidden is None:
        return False
    if any(a.startswith(forbidden) for a in args if forbidden):
        return False
    if program in ONE_OPERAND and sum(1 for a in args if not a.startswith("-")) > 1:
        return False
    return True


def readonly_pipeline(command: str | None) -> bool:
    """Whether ``command`` is one or more read-only commands joined by control
    operators, with no redirection to a file, substitution, heredoc or
    subshell (what _simple_commands reads), each a listed program used in a
    way that writes nothing."""
    from brindle.native.permissions import _simple_commands

    if not command or "$(" in command or "`" in command:
        return False
    parsed = _simple_commands(command)
    if not parsed or any(after not in (*COMPOUND_SEPARATORS, "||") for _, after in parsed):
        return False
    return all(_readonly_part(words) for words, _ in parsed)


# -- destructive commands --------------------------------------------------------------------------

# What a recursive rm must not be pointed at: the root, a top-level folder, or the home directory.
DESTRUCTIVE_TARGETS = re.compile(r"^(/|/\*|/[^/]+/?|~|~/|~/\*|\$HOME|\$\{HOME\}|\$HOME/|\$HOME/\*|\.\.?/?)$")


def destructive_rm(command: str) -> bool:
    """Whether ``command`` runs ``rm`` recursively (``-r``/``-R``/``--recursive``,
    in any flag cluster or spelling) at the root, a top-level folder, the home
    directory or the working directory itself, or with ``--no-preserve-root``."""
    for words in _segments(command):
        if not words or os.path.basename(words[0]) != "rm":
            continue
        flags = [w for w in words[1:] if w.startswith("-")]
        targets = [w for w in words[1:] if not w.startswith("-")]
        if any(f == "--no-preserve-root" for f in flags):
            return True
        recursive = any(f in ("--recursive",) or (not f.startswith("--") and any(c in f for c in "rR")) for f in flags)
        home = os.path.expanduser("~")
        if recursive and any(DESTRUCTIVE_TARGETS.fullmatch(t) or t.rstrip("/") == home for t in targets):
            return True
    return False


def forced_push(command: str) -> bool:
    """A push that forces (a flag, or a ``+refspec``) or deletes a branch."""
    return any(p.force or p.delete for p in push_calls(command))


def _in_worktree(req: Request) -> bool:
    """A path (resolved) inside the worktree or repo root, not under .git."""
    if not req.path:
        return False
    real = os.path.realpath(req.path)
    for root in _roots(req):
        rel = _inside(real, root)
        if rel and ".git" not in Path(rel).parts:
            return True
    return False


def _builtin_matches(rule: Rule, req: Request, checks: list[str]) -> bool:
    if rule.match_type == "tracked":
        return req.kind == "read" and is_tracked(req.path, _roots(req))
    if rule.match_type == "worktree":
        return req.kind in PATH_KINDS and _in_worktree(req)
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
    if rule.match_type == "push":
        return push_matches(rule.match, req, loose=rule.decision != "allow")
    if rule.match_type == "git-force":
        return any(_is_force(a) for _, args in _git_calls(command) for a in args) or forced_push(command)
    if rule.match_type == "rm-destructive":
        return destructive_rm(command)
    if rule.match_type == "readonly-pipeline":
        return readonly_pipeline(command)
    return False


def rule_matches(rule: Rule, req: Request, checks: list[str] | None = None) -> bool:
    if rule.kind != req.kind or rule.expired():
        return False
    if any(rule_matches(x, req, checks) for x in rule.exclusions):
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
    """Every rule in force: the repo's denies, then the store's (the preset's,
    the person's and learned ones)."""
    return [*(repo_rules(repo_root) if repo_root else []), *load_store().rules]


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


VERBS = {"deny": "denied", "ask": "asked for", "allow": "allowed"}


def _rule_decision(req: Request, rules: list[Rule], checks: list[str] | None) -> Decision | None:
    """The first rule that has a say, by precedence: deny > ask > allow."""
    for wanted in ("deny", "ask", "allow"):
        for rule in rules:
            if rule.decision == wanted and rule_matches(rule, req, checks):
                return Decision(wanted, f"{VERBS[wanted]} by {rule.source} rule {rule.id} ({rule.describe()})", rule)
    return None


def decide(req: Request | None, *, checks: list[str] | None = None,
           rules: list[Rule] | None = None, unmatched: str | None = None,
           session: str | None = None, saved: list[SavedAction] | None = None) -> Decision:
    """The policy's answer for ``req``: deny > ask > allow > ``unmatched``
    (the store's setting when None). An ask a saved action covers (within
    the ask rule's limits; ``session``: the asking agent's session, for
    saved actions that last one) is settled by it. ``saved``: the saved
    actions to consider (the store's when None)."""
    if req is None or req.kind not in KINDS:
        return Decision("ask", "brindle couldn't read the request")
    store = None
    if rules is None or unmatched is None or saved is None:
        store = load_store()
    if rules is None:
        rules = [*(repo_rules(req.repo_root) if req.repo_root else []), *store.rules]
    if unmatched is None:
        unmatched = store.unmatched
    if saved is None:
        saved = store.saved
    ruled = _rule_decision(req, rules, checks)
    if ruled is not None and ruled.decision == "deny":
        return ruled
    if saved:
        from brindle import permission_grants

        # A saved deny stops it before any automatic allow; a saved allow is
        # weighed only once the policy's own answer is known (below).
        stopped = permission_grants.settle(req, None, saved, session=session, denies_only=True)
        if stopped is not None:
            return stopped
    if ruled is not None and ruled.decision == "allow":
        return ruled
    if ruled is None and req.kind == "bash" and req.command and simple_argv(req.command) is None:
        parts = compound_parts(req.command)
        if parts:
            answers = [_part_decision(req, words, checks, rules) for words in parts]
            for answer in answers:
                if answer.decision == "deny":
                    return answer
            if all(answer.decision == "allow" for answer in answers):
                return Decision("allow", "every part of the command is allowed by a rule")
            ruled = next((a for a in answers if a.decision == "ask" and a.rule is not None), None)
    if ruled is None and unmatched == "deny":
        return Decision("deny", "no rule covers it, and unmatched actions are denied")
    if ruled is None and unmatched == "allow":
        # Nothing covers it: it runs, unless a part of the command a deny or ask
        # rule covers hides in it (cd x && git push origin main).
        within = decide_in_parts(req, checks=checks, rules=rules)
        if within is None:
            return Decision("allow", "no rule covers it; unmatched actions run")
        if within.decision == "deny":
            return within
        ruled = within
    if saved:
        settled = permission_grants.settle(req, ruled.rule if ruled else None, saved, session=session)
        if settled is not None:
            return settled
    if ruled is not None:
        return Decision("ask", ruled.reason + "; asking the user", ruled.rule)
    if req.kind == "bash" and req.command and simple_argv(req.command) is None:
        why = "the command isn't a single simple command, so no allow rule applies"
    elif req.kind == "read" and req.path:
        why = "not a file git tracks in the worktree, and no rule allows it"
    else:
        why = "no rule allows or denies it"
    return Decision("ask", f"{why}; asking the user")


def decide_in_parts(req: Request, *, checks: list[str] | None = None,
                    rules: list[Rule], decisions: tuple[str, ...] = ("deny", "ask")) -> Decision | None:
    """The deny (or ask) for any simple command inside ``req``'s shell
    command (``cd x && rm y`` holds two), a deny first, else None: an exact
    or prefix rule on the whole command never sees a part of it."""
    if req.kind != "bash" or not req.command:
        return None
    found: dict[str, Decision] = {}
    rules = [rule for rule in rules if rule.decision in decisions]
    for words in _segments(req.command):
        # Split even inside a word, as split_commands does: an empty or
        # spaced word (task test "" secret) mustn't dodge an exact deny.
        command = " ".join(" ".join(words).split())
        decision = _rule_decision(replace(req, command=command), rules, checks)
        if decision is not None and decision.decision not in found:
            found[decision.decision] = decision
    for wanted in decisions:
        if wanted in found:
            d = found[wanted]
            return d if wanted != "ask" else Decision("ask", d.reason + "; asking the user", d.rule)
    return None


def deny_in_parts(req: Request, *, checks: list[str] | None = None,
                  rules: list[Rule]) -> Decision | None:
    """The deny for any simple command inside ``req``'s shell command, else None."""
    return decide_in_parts(req, checks=checks, rules=rules, decisions=("deny",))


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
    # The preset the rules were seeded from (PRESETS), and what no rule covers (UNMATCHED).
    preset: str | None = None
    unmatched: str = "ask"
    # The person's saved allow and deny decisions from the Inbox.
    saved: list[SavedAction] = field(default_factory=list)
    # The asks of the last PROMPT_WINDOW seconds: [{"rule": rule id or "unmatched", "ts"}].
    prompts: list[dict] = field(default_factory=list)
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

    def to_json(self) -> str:
        data: dict = {"version": 2, "preset": self.preset, "unmatched": self.unmatched,
                      "rules": [r.to_dict() for r in self.rules], "approvals": self.approvals}
        if self.saved:
            data["saved"] = [s.to_dict() for s in self.saved]
        if self.prompts:
            data["prompts"] = self.prompts
        for name in ("agy_managed", "agy_created", "agy_repos", "codex_hook"):
            if getattr(self, name):
                data[name] = getattr(self, name)
        return json.dumps(data, indent=2) + "\n"

    def rule(self, id_: str) -> Rule | None:
        return next((r for r in self.rules if r.id == id_), None)

    def saved_action(self, id_: str) -> SavedAction | None:
        return next((s for s in self.saved if s.id == id_), None)

    def live_saved(self, now: float | None = None) -> list[SavedAction]:
        """The saved actions that still apply (expired ones stay listed, to edit or delete)."""
        return [s for s in self.saved if not s.expired(now)]


def seed_preset(store: Store, name: str) -> Store:
    """Seed ``store`` from preset ``name``: its rules replace the preset-sourced
    ones (the person's own and learned rules stay), and its unmatched setting
    and name are recorded."""
    kept = [r for r in store.rules if r.source != "preset"]
    ids = {r.id for r in kept}    # a seeded rule the person edited keeps their version
    store.rules = [*(r for r in preset_rules(name) if r.id not in ids), *kept]
    store.preset, store.unmatched = name, preset_unmatched(name)
    return store


def _write_store(path: Path, store: Store) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(store.to_json())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_store() -> Store:
    """The person's store. A new one is seeded from DEFAULT_PRESET; one from
    before presets (version 1: built-in rules outside it) is migrated to
    MIGRATED_PRESET, keeping its user and learned rules, approvals and the
    provider records."""
    path = store_path()
    if not path.exists():
        store = seed_preset(Store(), DEFAULT_PRESET)
        try:
            _write_store(path, store)
        except OSError:
            pass
        return store
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Store()
    if not isinstance(data, dict):
        return Store()
    legacy = "preset" not in data
    rules = [r for r in (Rule.from_dict(d) for d in data.get("rules") or [] if isinstance(d, dict))
             if r and r.source in STORE_SOURCES and not (legacy and r.source == "preset")]
    approvals = data.get("approvals")
    approvals = {k: v for k, v in approvals.items() if isinstance(v, dict)} if isinstance(approvals, dict) else {}
    saved = [s for s in (SavedAction.from_dict(d) for d in data.get("saved") or [] if isinstance(d, dict)) if s]
    now = time.time()
    prompts = [p for p in data.get("prompts") or [] if isinstance(p, dict) and isinstance(p.get("rule"), str)
               and _number(p.get("ts")) is not None and now - p["ts"] < PROMPT_WINDOW]
    preset = data.get("preset") if data.get("preset") in PRESETS else None
    unmatched = data.get("unmatched") if data.get("unmatched") in UNMATCHED else "ask"

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
    store = Store(rules, approvals, preset, unmatched, saved, prompts, managed, strings(data.get("agy_created")),
                  repos, codex)
    if legacy:
        # Before presets: the built-in rules lived outside the store, and the
        # person's command allows were written with source "user". What those
        # amounted to is the Standard preset; its seed takes their place.
        seed = {(r.kind, r.match, r.match_type, r.decision) for r in preset_rules(MIGRATED_PRESET)}
        store.rules = [r for r in store.rules if (r.kind, r.match, r.match_type, r.decision) not in seed]
        seed_preset(store, MIGRATED_PRESET)
        try:
            _write_store(path, store)
        except OSError:
            pass
    return store


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
        _write_store(path, store)


def make_rule(kind: str, match: str, decision: str, match_type: str = "exact", source: str = "user", *,
              limits: dict | None = None, expires: float | None = None, exclusions: list[Rule] | None = None,
              label: str = "", id_: str = "") -> Rule:
    """A validated rule (ValueError otherwise). A ``push`` rule's match is
    ``"<source> → <destination>"``; an ask rule may carry ``limits``."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: one of {', '.join(KINDS)}")
    if match_type not in (*MATCH_TYPES, *BUILTIN_MATCHERS):
        raise ValueError(f"unknown match type {match_type!r}: one of {', '.join((*MATCH_TYPES, *BUILTIN_MATCHERS))}")
    if decision not in DECISIONS:
        raise ValueError("decision must be allow, deny or ask")
    if not match:
        raise ValueError("empty match")
    if match_type == "push" and push_pattern(match) is None:
        raise ValueError(f"a push rule's match is '<source>{PUSH_ARROW}<destination>'")
    if source not in STORE_SOURCES:
        raise ValueError(f"source must be one of {', '.join(STORE_SOURCES)}")
    if limits is not None and decision == "ask" and clean_limits(limits) is None:
        raise ValueError("limits need a positive 'minutes' for life 'time'")
    if expires is not None and expires <= time.time():
        raise ValueError("expires is already past")
    return Rule(kind, match, match_type, decision, source, time.time(), id_,
                clean_limits(limits) if decision == "ask" else None, expires, tuple(exclusions or ()), label)


def add_rule(kind: str, match: str, decision: str, match_type: str = "exact",
             source: str = "user", **extra) -> Rule:
    """Add a rule to the store (replacing one with the same id). ``extra``: make_rule's keyword arguments."""
    rule = make_rule(kind, match, decision, match_type, source, **extra)
    with editing() as store:
        store.rules = [r for r in store.rules if r.id != rule.id] + [rule]
    return rule


def update_rule(id_: str, **changes) -> Rule | None:
    """Change rule ``id_`` (any of make_rule's fields); the new rule, which
    keeps the id, or None when there is no such rule. ValueError when the
    result isn't a valid rule."""
    with editing() as store:
        old = store.rule(id_)
        if old is None:
            return None
        fields = dict(kind=old.kind, match=old.match, match_type=old.match_type, decision=old.decision,
                      source=old.source, limits=old.limits, expires=old.expires, exclusions=list(old.exclusions),
                      label=old.label)
        fields.update(changes)
        source = fields.pop("source")
        if source == "preset":
            source = "user"     # edited: the person's now
        rule = make_rule(fields.pop("kind"), fields.pop("match"), fields.pop("decision"), fields.pop("match_type"),
                         source, id_=id_, **fields)
        rule = replace(rule, created=old.created or rule.created)
        store.rules = [rule if r.id == id_ else r for r in store.rules]
    return rule


def add_exclusion(id_: str, kind: str, match: str, match_type: str = "exact") -> Rule | None:
    """Except requests ``match`` covers from rule ``id_`` (a deny's exception);
    the changed rule, or None for no such rule."""
    exception = make_rule(kind, match, "deny", match_type)
    with editing() as store:
        old = store.rule(id_)
        if old is None:
            return None
        rule = replace(old, exclusions=(*old.exclusions, exception))
        store.rules = [rule if r.id == id_ else r for r in store.rules]
    return rule


def forget(id_: str) -> str | None:
    """Remove rule ``id_`` (whatever seeded it), or the approval count behind
    suggestion ``id_``; what was removed, or None if neither exists."""
    with editing() as store:
        found = store.rule(id_)
        store.rules = [r for r in store.rules if r.id != id_]
        approval = store.approvals.pop(id_, None)
    if found:
        return f"{found.decision} {found.describe()} ({found.source})"
    if approval:
        return f"approvals of {approval.get('kind')} {approval.get('match')!r}"
    return None


def reset() -> None:
    """Back to the preset's seed: no user or learned rules, saved actions or
    approval counts."""
    with editing() as store:
        store.rules, store.approvals, store.saved = [], {}, []
        seed_preset(store, store.preset or DEFAULT_PRESET)


def apply_preset(name: str) -> Store:
    """Seed the store from preset ``name`` (ValueError for an unknown one),
    keeping the person's own and learned rules and saved actions."""
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}: one of {', '.join(PRESETS)}")
    with editing() as store:
        seed_preset(store, name)
    return store


def set_unmatched(value: str) -> None:
    if value not in UNMATCHED:
        raise ValueError(f"unmatched must be one of {', '.join(UNMATCHED)}")
    with editing() as store:
        store.unmatched = value


# -- saved actions ------------------------------------------------------------------------------


def _stamp(action: SavedAction, now: float | None = None) -> SavedAction:
    """A timed action's expiry, from now (ValueError without positive minutes)."""
    now = time.time() if now is None else now
    if action.life == "time":
        if action.minutes is None or action.minutes <= 0:
            raise ValueError("a timed saved action needs positive minutes")
        action.expires = now + action.minutes * 60
    else:
        action.minutes = action.expires = None
    if action.life != "session":
        action.session = None
    if action.life == "session" and not action.session:
        raise ValueError("a saved action for this session needs the session")
    return action


def save_action(kind: str, pattern: str, decision: str, *, scope: str = "exact", level: str | None = None,
                request: dict | None = None, life: str = "forever", minutes: float | None = None,
                session: str | None = None, repo: str | None = None, note: str = "") -> SavedAction:
    """Save an allow or deny decision (ValueError when it isn't a valid one)."""
    if kind not in (*KINDS, "push"):
        raise ValueError(f"unknown kind {kind!r}")
    if decision not in ("allow", "deny"):
        raise ValueError("decision must be allow or deny")
    if life not in LIVES:
        raise ValueError(f"life must be one of {', '.join(LIVES)}")
    if not pattern:
        raise ValueError("empty pattern")
    if level is not None and level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    action = _stamp(SavedAction(secrets.token_hex(4), kind, pattern, decision, scope, level, dict(request or {}),
                                life, minutes, None, session, repo, time.time(), note))
    with editing() as store:
        store.saved.append(action)
    return action


def update_saved_action(id_: str, **changes) -> SavedAction | None:
    """Change saved action ``id_`` (pattern, scope, level, decision, life,
    minutes, note ...); a timed one's expiry restarts now. None for no such action."""
    with editing() as store:
        old = store.saved_action(id_)
        if old is None:
            return None
        data = {**old.to_dict(), **changes, "id": id_}
        new = SavedAction.from_dict(data)
        if new is None or new.decision not in ("allow", "deny") or new.life not in LIVES:
            raise ValueError("not a valid saved action")
        if new.level is not None and new.level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        new.created = old.created
        _stamp(new)
        store.saved = [new if s.id == id_ else s for s in store.saved]
    return new


def delete_saved_action(id_: str) -> SavedAction | None:
    with editing() as store:
        found = store.saved_action(id_)
        store.saved = [s for s in store.saved if s.id != id_]
    return found


# -- prompt counts ------------------------------------------------------------------------------


def note_prompt(decision: Decision) -> None:
    """An ask reached a person: count it against the rule that asked (or
    "unmatched"), for the last PROMPT_WINDOW seconds. Never raises."""
    if decision.decision != "ask":
        return
    try:
        with editing() as store:
            now = time.time()
            store.prompts = [p for p in store.prompts if now - p["ts"] < PROMPT_WINDOW]
            store.prompts.append({"rule": decision.rule.id if decision.rule else "unmatched", "ts": now})
    except Exception:  # noqa: BLE001 - a count is a side record
        pass


def prompt_counts(store: Store | None = None, now: float | None = None) -> dict[str, int]:
    """Asks per rule id ("unmatched" for none) over the last PROMPT_WINDOW seconds."""
    store = store or load_store()
    now = time.time() if now is None else now
    out: dict[str, int] = {}
    for p in store.prompts:
        if now - p["ts"] < PROMPT_WINDOW:
            out[p["rule"]] = out.get(p["rule"], 0) + 1
    return out


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
    """``rule`` in agy's syntax, or None when agy can't express it safely
    (an ask rule never: agy has no "ask" entry, and the hook asks for it)."""
    if rule.decision == "ask" or rule.exclusions or rule.expired():
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


def _literal_prefix(rule: Rule) -> str | None:
    """What every request a rule matches starts with, if that can be said:
    the whole match for exact and prefix rules, a glob's text before its
    first wildcard. None for a named matcher (anything may match)."""
    if rule.match_type in ("exact", "prefix"):
        return rule.match
    if rule.match_type == "glob":
        return re.split(r"[*?\[]", rule.match, 1)[0]
    return None


def _may_overlap(ask: Rule, allow: Rule) -> bool:
    """Whether a request could fit both ``ask`` and ``allow`` (same kind), so
    that mirroring the allow into a provider's own settings would let it run
    where the hook asks. Errs toward overlap."""
    if ask.kind != allow.kind:
        return False
    if ask.match_type == "push":
        head = _literal_prefix(allow)
        if head is None:
            return False    # a named matcher (read-only git, checks, pipelines): never a push
        head = head.strip()
        return "git".startswith(head) or head.startswith("git")
    a, b = _literal_prefix(ask), _literal_prefix(allow)
    if a is None or b is None:
        return True
    return a.startswith(b) or b.startswith(a)


def mirror_agy(rules: list[Rule], checks: list[str]) -> dict[str, list[str]]:
    """The agy settings entries for ``rules`` (the built-in ones expanded:
    the read-only git commands as exact commands, git push as a deny), as
    {"allow": [...], "deny": [...]}. A repo's ``checks`` aren't mirrored:
    agy's settings apply to every project, and a check runs whatever that
    repo defines, so it's allowed only for brindle workers in that repo."""
    out: dict[str, list[str]] = {"allow": [], "deny": []}
    asks = [r for r in rules if r.decision == "ask" and not r.expired()]

    def add(decision: str, entry: str | None) -> None:
        if entry and entry not in out[decision]:
            out[decision].append(entry)

    for rule in rules:
        if rule.decision == "ask" or rule.exclusions or rule.expired():
            continue  # only the hook can ask, or weigh an exception
        if rule.decision == "allow" and any(_may_overlap(ask, rule) for ask in asks):
            continue  # agy would run it on the allow where the hook asks; left to the hook
        if rule.match_type == "check":
            continue
        elif rule.match_type == "git-readonly" and rule.decision == "allow":
            for sub in READONLY_GIT:
                add("allow", f"command(regex:^git {sub}$)")
        elif rule.match_type == "git-push" and rule.decision == "deny":
            add("deny", "command(git push)")
        elif rule.match_type == "push" and rule.decision == "deny" and push_pattern(rule.match) == ("*", "*", "*"):
            add("deny", "command(git push)")
        elif rule.match_type in BUILTIN_MATCHERS:
            continue  # tracked files, force flags, pipelines, branches: only the hook can tell
        else:
            add(rule.decision, _agy_entry(rule))
    return out


def profile_agy_entries(spec: str) -> list[str]:
    """One ``allowed_tools`` entry in agy's settings syntax, no wider than
    ``profile_bash_allows`` (a prefix takes only simple characters after
    it), or [] when agy can't say it."""
    return [e for e in (_agy_entry(rule) for rule in _profile_bash(spec)) if e]
