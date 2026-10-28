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
import glob
import hashlib
import json
import math
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
SOURCES = ("preset", "repo", "user", "learned", "profile", "org")
STORE_SOURCES = ("preset", "user", "learned")    # what the person's store holds
# Every field a stored rule may have (Rule.to_dict, and the 0.4.7 editor's rows).
RULE_FIELDS = frozenset({"id", "kind", "match", "match_type", "decision", "source", "created", "limits", "expires",
                         "exclusions", "label", "repo_root", "expires_at", "editable", "protected", "person_only"})
UNMATCHED = ("ask", "allow", "deny")             # Store.unmatched: what no rule covers
# How far a saved approval of a push may reach (an ask rule's limits.max_scope), narrowest first.
LEVELS = ("branch", "destination", "remote", "repository", "all")
LIVES = ("session", "time", "forever")           # how long a rule's approvals, or a saved action, last
PUSH_ARROW = " → "                                # a push rule's "<source> → <destination>"
DEFAULT_BRANCH = "@default"                       # in a push pattern: the repo's default branch
SUGGEST_AFTER = 2
PROMPT_WINDOW = 86400.0                           # seconds of asks kept for per-rule prompt counts
FILE = "permissions.json"
# 1: before presets (built-in rules outside the store); 2: the 0.4.7 engine's
# (built-ins, editor rules, remembered grants); 3: presets and saved actions.
# A newer store is read fail-closed and never rewritten (Store.unsupported_version).
STORE_VERSION = 3

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
READONLY_GIT = ("status", "diff", "log", "show", "rev-list")
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
    pushes, else a pattern a saved action must stay inside), ``life`` (a
    LIVES entry, or "once": no saved approval settles it) and, for life
    "time", ``minutes``. None when nothing valid is given."""
    if not isinstance(limits, dict):
        return None
    out: dict = {}
    scope = limits.get("max_scope")
    if isinstance(scope, str) and scope:
        out["max_scope"] = scope
    life = limits.get("life")
    if life in (*LIVES, "once"):
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
    repo_root: str | None = None        # the one repo (resolved) it applies in; None: every repo
    # An ask only the person answers: never routed to the worker's supervisor first
    # (brindle.permission_routing).
    person_only: bool = False

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
        if self.repo_root:
            out["repo_root"] = self.repo_root
        if self.person_only:
            out["person_only"] = True
        return out

    @classmethod
    def from_dict(cls, data: dict, source: str | None = None, *, depth: int = 0) -> "Rule | None":
        """A rule read from a file, or None if it isn't a valid one. A field
        this engine doesn't know may narrow the rule in a way it can't
        evaluate, so such a row is not a rule here (load_store keeps it, see
        Store.blocking_rules). ``expires_at`` is the 0.4.7 engine's name for
        ``expires``."""
        if not isinstance(data, dict) or data.keys() - RULE_FIELDS or depth > 8:
            return None
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
        raw = data.get("expires", data.get("expires_at"))
        expires = _number(raw)
        if raw is not None and (expires is None or not math.isfinite(expires) or expires <= 0):
            return None
        repo = data.get("repo_root")
        if repo is not None and (not isinstance(repo, str) or not os.path.isabs(repo)):
            return None
        rows = data.get("exclusions") or []
        if not isinstance(rows, list) or len(rows) > 100:
            return None
        exclusions = tuple(cls.from_dict(d, src, depth=depth + 1) for d in rows)
        if any(x is None or x.kind != kind for x in exclusions):
            return None    # an exception it can't read: the rule might apply to less than it says
        id_ = data.get("id") if isinstance(data.get("id"), str) else ""
        label = data.get("label") if isinstance(data.get("label"), str) else ""
        return cls(kind, match, match_type, decision, src, created, id_,
                   clean_limits(data.get("limits")) if decision == "ask" else None, expires, exclusions, label,
                   os.path.realpath(repo) if repo else None, data.get("person_only") is True)


@dataclass
class SavedAction:
    """A decision the person saved from the Inbox: allow or deny requests of
    ``kind`` (a KINDS entry, or ``push``) whose key (brindle.permission_grants
    .ask_key) ``pattern`` covers. ``scope``/``level`` say which rung of the
    scope ladder the pattern is (exact, general, repository, ...); ``request``
    is the exact request it was saved from; ``life`` session (of ``session``,
    a root agent), time (``minutes``, until ``expires``) or forever; ``repo``
    the repo root it applies in, or None for every repo. One migrated from
    brindle 0.4.7 may also carry its ``binding`` (the approving ``owner``,
    the repo's ``git_dir``, a push's remote ``url``: permission_grants
    ._binding_holds) and ``uses_left`` (None: no use budget)."""
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
    binding: dict = field(default_factory=dict)
    uses_left: int | None = None

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
        binding = data.get("binding", {})
        uses = data.get("uses_left")
        if (not isinstance(binding, dict) or not all(isinstance(v, str) for v in binding.values())
                or uses is not None and (type(uses) is not int or uses < 0)):
            return None    # a bound it can't read: never an unbounded allow
        return cls(id_, kind, pattern, decision, str(data.get("scope") or "exact"), text["level"], request, life,
                   _number(data.get("minutes")), _number(data.get("expires")), text["session"], text["repo"],
                   _number(data.get("created")) or 0.0, str(data.get("note") or ""), dict(binding), uses)


@dataclass(frozen=True)
class Decision:
    decision: str  # allow | deny | ask
    reason: str
    rule: Rule | None = None
    # An ask's: every ask rule that matches it (its parts and each push included).
    # ``rule`` is the strictest of them; a saved allow must fit all of their limits.
    asks: tuple = ()
    # The id of the saved action that settled it (permission_grants.settle), if one did.
    saved: str | None = None


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
# A push into a release branch starts a signed build (app-release.yml): every
# preset asks the person for it each time (never a supervisor), and no saved
# approval settles it (life "once").
RELEASE_ASKS: tuple[Rule, ...] = (
    _seed("d-push-release", "bash", f"*{PUSH_ARROW}release", "push", "ask", limits={"life": "once"},
          person_only=True),
    _seed("d-push-release-sub", "bash", f"*{PUSH_ARROW}release/*", "push", "ask", limits={"life": "once"},
          person_only=True),
)
# The rules of before presets, kept for stores written then (load_store migrates them).
LEGACY_DEFAULT_IDS = ("d-tracked", "d-checks", "d-git-ro", "d-git-push", "d-git-force", "d-ssh", "d-aws",
                      "d-gnupg", "d-daemon", "d-daemon-sh", "d-env", "d-env-rel")


def _push_default(max_scope: str, life: str, minutes: float | None = None) -> Rule:
    """Pushes into the repo's default branch ask the person, with limits on saved approvals."""
    limits = {"max_scope": max_scope, "life": life}
    if minutes is not None:
        limits["minutes"] = minutes
    return _seed("d-push-default", "bash", f"*{PUSH_ARROW}{DEFAULT_BRANCH}", "push", "ask", limits=limits,
                 person_only=True)


def _autonomous() -> list[Rule]:
    return [*ROUTINE_ALLOWS, *PRESET_RULES, *CREDENTIAL_DENIES, *DESTRUCTIVE_DENIES,
            _push_default("all", "forever"), *RELEASE_ASKS]


def _permissive() -> list[Rule]:
    return [*ROUTINE_ALLOWS, *PRESET_RULES,
            _seed("d-read-worktree", "read", "a file in the worktree", "worktree", "allow"),
            _seed("d-write-worktree", "write", "a file in the worktree", "worktree", "allow"),
            _seed("d-edit-worktree", "edit", "a file in the worktree", "worktree", "allow"),
            *CREDENTIAL_DENIES, *DESTRUCTIVE_DENIES, _push_default("remote", "forever"), *RELEASE_ASKS]


def _standard() -> list[Rule]:
    return [*ROUTINE_ALLOWS, *PRESET_RULES, *CREDENTIAL_DENIES, *DESTRUCTIVE_DENIES,
            _push_default("destination", "time", 30), *RELEASE_ASKS]


def _secure() -> list[Rule]:
    short = {"max_scope": "*", "life": "time", "minutes": 15}
    return [*ROUTINE_ALLOWS[:3], *CREDENTIAL_DENIES, *DESTRUCTIVE_DENIES,
            _push_default("branch", "time", 15), *RELEASE_ASKS,
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
# An MCP tool's name (mcp__<server>__<tool>), as a Claude Code hook matcher regex.
MCP_TOOL_PATTERN = "mcp__.*"


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
    updates, which it then applies as if picked in its prompt) or deny; or
    the worker's supervisor's, supervisor_allow or supervisor_deny
    (brindle.permission_routing)."""
    if answer == "deny":
        decision: dict = {"behavior": "deny", "message": "brindle: the person denied this in the brindle app"}
    elif answer == "supervisor_deny":
        decision = {"behavior": "deny", "message": "brindle: your supervisor denied this"}
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
               rules: list[Rule] | None = None, spend: bool = False, **context) -> Decision:
    """One answer for several requests (an apply_patch touching several
    files): deny if any is denied, allow only if every one is allowed, else
    ask. ``spend``: as decide's, but the call runs once however many files it
    touches, so each saved action that settles any of them spends one use,
    and only once all are allowed. ``context``: decide's other keyword
    arguments (session ...)."""
    if not reqs:
        return Decision("ask", "brindle couldn't read the request")
    if len(reqs) == 1:
        return decide(reqs[0], checks=checks, rules=rules, spend=spend, **context)
    decisions = [decide(r, checks=checks, rules=rules, **context) for r in reqs]
    for d in decisions:
        if d.decision == "deny":
            return d
    if all(d.decision == "allow" for d in decisions):
        grants = list(dict.fromkeys(d.saved for d in decisions if d.saved))
        for id_ in grants if spend else ():
            if not spend_saved_use(id_):     # its last use went meanwhile
                return Decision("ask", f"your saved action {id_} has no uses left; asking the user")
        # A saved action that settled any one of them settled the whole (it would ask without it).
        return Decision("allow", f"every one of {len(decisions)} files is allowed",
                        saved=grants[0] if grants else None)
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
        # A rule scoped to a repo, a time or with exceptions is only the policy's to weigh.
        if (rule.kind == "bash" and rule.decision == "allow" and rule.match.strip()
                and not (rule.repo_root or rule.expires is not None or rule.exclusions)):
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
    restricted = _restriction_in_parts(req, checks=checks, rules=rules)
    if restricted is not None and restricted.decision == "deny":
        return restricted
    if restricted is not None:
        return None    # an ask, or syntax brindle can't check against one: the person's to decide
    # The shell rewrites an unquoted tilde word (``~/x``, ``a=~/x``,
    # ``a=b:~/x``) after this check; HEAD~1 stays a literal (see _tokens).
    if _tokens(req.command, word_pattern=_NO_TILDE) is None:
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

    denied = _matching_restriction([replace(req, command=_joined(words))], checks, rules)
    if denied is not None:
        return denied
    if all(SIMPLE_COMMAND.fullmatch(word) and " " not in word for word in words):
        return decide(replace(req, command=" ".join(words)), checks=checks, rules=rules, unmatched="ask", saved=[])
    for rule in rules:
        head = rule.match.split()
        if (rule.kind == "bash" and rule.decision == "allow" and rule.match_type == "prefix"
                and head and words[:len(head)] == head and _in_scope(rule, req, checks)):
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


def _readonly_git(req: Request, argv: list[str]) -> bool:
    """Whether ``argv`` is a read-only git command (READONLY_GIT, no unsafe or
    force option), with ``-C`` only into the worktree or repo root."""
    if not argv or argv[0] != "git":
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
    # A wildcard refspec (refs/heads/*) names a set of branches only git knows.
    if not ref or ref == "HEAD" or ref.startswith("@") or any(c in ref for c in "~^@{}*?["):
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


# -- read-only commands ---------------------------------------------------------------------------
#
# A command whose every part only reads (grep, ls, cat, head ...) joined by
# pipes or &&, with quoting the shell may read any way (an escaped regex), is
# safe to run even when it can't be parsed into exact allow rules: the
# preset rule d-readonly allows it (a readonly-pipeline rule, which decide
# reads through ``readonly_pipeline`` with the rules in force). Anything that writes a
# file (a redirection other than to /dev/null), reads one through an input
# redirect, substitutes a command, sets a variable first, runs a program not
# on the list (or one named by a path), recurses into a directory, or reads a
# file a read deny covers goes through the normal decision.

# program -> (short option letters, long options) that make it write a file,
# run another program or read a whole tree; a short letter counts anywhere in
# an option cluster (``sort -ro out``), a long option with or without
# ``=value``. Only programs audited for such options are listed; ``uniq``'s
# second operand is an output file, so it takes one operand at most (see
# _readonly_part). grep, rg and wc take only the options OPTION_CHECKS knows.
READONLY_PROGRAMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "grep": ("", ()), "egrep": ("", ()), "fgrep": ("", ()), "rg": ("", ("--pre", "--pre-glob")),
    "ls": ("", ()), "cat": ("", ()), "head": ("", ()), "tail": ("", ()), "wc": ("", ()), "nl": ("", ()),
    "tac": ("", ()), "rev": ("", ()), "fold": ("", ()), "expand": ("", ()), "unexpand": ("", ()),
    "cut": ("", ()), "tr": ("", ()), "paste": ("", ()), "comm": ("", ()), "column": ("", ()), "seq": ("", ()),
    "sort": ("o", ("--output", "--compress-program", "--files0-from")), "uniq": ("", ()),
    "diff": ("r", ("--recursive",)), "cmp": ("", ()), "file": ("C", ("--compile",)), "stat": ("", ()),
    "du": ("", ()),
    "df": ("", ()), "pwd": ("", ()), "echo": ("", ()), "printf": ("", ()), "which": ("", ()),
    "whereis": ("", ()), "type": ("", ()), "basename": ("", ()), "dirname": ("", ()), "realpath": ("", ()),
    "readlink": ("", ()), "true": ("", ()), "false": ("", ()), "test": ("", ()), "[": ("", ()), "whoami": ("", ()),
    "uname": ("", ()), "printenv": ("", ()), "strings": ("", ()), "od": ("", ()), "hexdump": ("", ()),
    "md5": ("", ()), "md5sum": ("", ()), "shasum": ("", ()), "sha256sum": ("", ()), "jq": ("", ()),
    "yq": ("i", ("--inplace",)), "tree": ("o", ()), "locate": ("", ()),
    "fd": ("xX", ("--exec", "--exec-batch")), "fdfind": ("xX", ("--exec", "--exec-batch")),
}
ONE_OPERAND = frozenset({"uniq"})
# Programs that read names, never a file's contents: no read deny is checked against their arguments.
NAME_ONLY = frozenset({"ls", "tree", "find", "du", "df", "stat", "pwd", "echo", "printf", "which", "whereis",
                       "type", "basename", "dirname", "realpath", "readlink", "true", "false", "test", "[",
                       "whoami", "uname", "seq", "locate", "fd", "fdfind", "hostname"})
# Programs whose arguments are only text they print or compare: a word only the run
# knows ("behind: $(git rev-list ...)", [ "$s" = done ]) leaves them read-only.
TEXT_ONLY = frozenset({"echo", "test", "[", "true", "false"})

# The options _reads_only_named accepts, written out in full: GNU tools
# also take any unambiguous abbreviation of a long option (--recur is
# --recursive), so an option not listed here is refused, never guessed.
# grep: flags (a cluster may end in one taking a value, attached or next),
# then long flags and long options taking a value.
_GREP_SHORT = re.compile(r"-[nilcwvoxFEGHhsqLZaIUrRz0-9]*(?P<value>[efmABC].*)?", re.DOTALL)
_GREP_FLAGS = ("--line-number", "--ignore-case", "--no-ignore-case", "--count", "--word-regexp", "--line-regexp",
               "--invert-match", "--only-matching", "--fixed-strings", "--extended-regexp", "--basic-regexp",
               "--perl-regexp", "--with-filename", "--no-filename", "--no-messages", "--quiet", "--silent",
               "--files-with-matches", "--files-without-match", "--null", "--null-data", "--text",
               "--byte-offset", "--initial-tab", "--color", "--colour", "--recursive", "--dereference-recursive")
_GREP_VALUES = ("--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context",
                "--label", "--color", "--colour", "--binary-files", "--include", "--exclude", "--exclude-dir")
_GREP_RECURSIVE = re.compile(r"-[^-]*[rR].*|--(recursive|dereference-recursive)")
# The most files a recursive read is checked across (_reads_clear); past it, it isn't clear.
WALK_LIMIT = 5000
_WC = re.compile(r"-[cmlwL]+|--(bytes|chars|lines|words|max-line-length)")
# rg's (it takes no abbreviations, but the same rule holds): flags, and those taking a value.
_RG_FLAGS = re.compile(r"-[nilcwvoxFsSHNu]+|--(line-number|ignore-case|smart-case|case-sensitive|files-with-matches"
                       r"|count|word-regexp|invert-match|only-matching|fixed-strings|no-heading|heading"
                       r"|with-filename|no-filename|hidden|no-ignore|json|vimgrep)")
_RG_VALUES = ("-e", "--regexp", "-m", "--max-count", "-A", "-B", "-C", "--after-context",
              "--before-context", "--context", "-t", "--type", "-T", "--type-not")


def _grep_options_known(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return True
        if arg.startswith("--"):
            name, eq, _ = arg.partition("=")
            if eq and name not in _GREP_VALUES or not eq and name not in (*_GREP_FLAGS, *_GREP_VALUES):
                return False
            if not eq and name in _GREP_VALUES and name not in _GREP_FLAGS:
                i += 1  # its value is the next word
        elif arg.startswith("-") and arg != "-":
            short = _GREP_SHORT.fullmatch(arg)
            if short is None:
                return False
            if short.group("value") is not None and len(short.group("value")) == 1:
                i += 1  # its value is the next word
        i += 1
    return True


def _reads_only_named(program: str, args: list[str], cwd: str) -> bool:
    """Whether ``program`` with ``args`` reads only files its arguments
    name, or the trees of the directories they name when it recurses (which
    _reads_clear then walks): no list of names read from a file, no command
    run (rg's --pre). grep and wc take only the options listed above (so no
    --files0-from or --devices, however spelled); rg only the options it
    knows (anything else could take the operand that would have been a path)."""
    if program in ("grep", "egrep", "fgrep"):
        return _grep_options_known(args)
    if program == "wc":
        return all(_WC.fullmatch(a) or a == "-" or not a.startswith("-") for a in args)
    if program != "rg":
        return True
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break
        if arg in _RG_VALUES:
            i += 2
            continue
        if not _RG_FLAGS.fullmatch(arg) and arg.startswith("-") and arg != "-":
            return False
        i += 1
    return True


def _recurses(words: list[str]) -> bool:
    """Whether the read-only command ``words`` reads whole directory trees."""
    if words[0] == "rg":
        return True
    return words[0] in ("grep", "egrep", "fgrep") and any(_GREP_RECURSIVE.fullmatch(a) for a in words[1:]
                                                          if a.startswith("-"))


def _search_paths(words: list[str]) -> list[str]:
    """The paths a grep or rg command ``words`` searches: its operands after
    the pattern (none when the pattern came from -e or -f)."""
    values = (_RG_VALUES if words[0] == "rg"
              else ("-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D", *_GREP_VALUES))
    positional, patterned, i = [], False, 1
    while i < len(words):
        arg = words[i]
        if arg == "--":
            positional += words[i + 1:]
            break
        if arg in values:
            patterned = patterned or arg in ("-e", "-f", "--regexp", "--file")
            i += 2
            continue
        if arg.startswith("-") and arg != "-":
            patterned = patterned or arg.startswith(("--regexp=", "--file=")) or bool(
                re.fullmatch(r"-[^-]*[ef].+", arg))
        else:
            positional.append(arg)
        i += 1
    return positional if patterned else positional[1:]


def _tree(root: str, budget: list[int]) -> list[str] | None:
    """Every file and directory under ``root`` (``.git`` aside), or None past
    ``budget`` (shared across a command's walks)."""
    out = []
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in (*dirs, *files):
            budget[0] -= 1
            if budget[0] < 0:
                return None
            out.append(os.path.join(top, name))
    return out


def _has_option(args: list[str], short: str, long: tuple[str, ...]) -> bool:
    """Whether any of ``args`` carries one of the short letters (in any
    cluster, ``-ro``) or long options (``--output`` or ``--output=x``). GNU
    getopt takes any unambiguous prefix of a long option (``--out``,
    ``--compress-prog``), so a prefix of a listed one counts too."""
    for a in args:
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if len(name) > 2 and any(opt.startswith(name) for opt in long):
                return True
        elif a.startswith("-") and len(a) > 1 and any(c in short for c in a[1:]):
            return True
    return False
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


def _readonly_part(req: Request, words: list[str]) -> bool:
    """Whether the simple command ``words`` only reads (see the section above).
    A program named by a path (``./cat``) could be anything."""
    if not words or "/" in words[0]:
        return False
    program = words[0]
    args = words[1:]
    if program == "git":
        while args[:1] == ["--no-pager"]:
            args = args[1:]
        return _readonly_git(req, ["git", *args])
    if not _reads_only_named(program, args, req.cwd or req.worktree):
        return False
    if program == "find":
        return not any(a in FIND_WRITING for a in args)
    if program == "sed":
        return _sed_readonly(args)
    if program == "tee":
        return not any(not a.startswith("-") for a in args)   # tee with no file only echoes
    if program == "hostname":
        return not any(not a.startswith("-") for a in args)   # with an operand it sets the name
    audited = READONLY_PROGRAMS.get(program)
    if audited is None:
        return False
    if _has_option(args, *audited):
        return False
    if program in ONE_OPERAND and sum(1 for a in args if not a.startswith("-")) > 1:
        return False
    return True


def _expanded(req: Request, word: str) -> list[str] | None:
    """The names ``word`` may stand for as the shell expands it in ``req.cwd``:
    itself (a leading ``~`` expanded), and each file a pathname pattern in it
    matches, dotfiles included. None when listing them would take more than
    _GLOB_BUDGET directory entries."""
    names = [os.path.expanduser(word)]
    if any(c in word for c in "*?["):
        found = _bounded_glob(os.path.expanduser(word), req.cwd or req.worktree or None)
        if found is None:
            return None
        names += found
    return names


# The most directory entries expanding one pattern may look at: past it, brindle can't know its names.
_GLOB_BUDGET = 5000


def _bounded_glob(pattern: str, root: str | None) -> list[str] | None:
    """The names ``pattern`` matches from ``root``, dotfiles included (as
    glob.glob names them, ``[^`` read as ``[!``), or None once that would
    look at more than _GLOB_BUDGET directory entries."""
    budget = _GLOB_BUDGET
    found = ["/" if os.path.isabs(pattern) else ""]
    pieces = [p for p in pattern.replace("[^", "[!").split("/") if p]
    for n, piece in enumerate(pieces):
        last, matched = n == len(pieces) - 1, []
        for path in found:
            if not glob.has_magic(piece):
                budget -= 1
                if os.path.lexists(os.path.join(root or "", path, piece)):
                    matched.append(os.path.join(path, piece))
                continue
            try:
                entries = os.scandir(os.path.join(root or "", path) or ".")
            except OSError:
                continue
            with entries:
                for entry in entries:
                    budget -= 1
                    if budget < 0:
                        return None
                    if fnmatch.fnmatchcase(entry.name, piece) and (last or entry.is_dir()):
                        matched.append(os.path.join(path, entry.name))
        if budget < 0:
            return None
        found = matched
    return sorted(found) if pieces else []


def _reads_clear(req: Request, words: list[str], path_denies: list[Rule]) -> bool:
    """Whether no read deny in ``path_denies`` covers a file ``words`` may
    read: every argument counts as a name it may read, an option's attached
    value too (``-fFILE``, ``--file=FILE``), each pattern expanded, and for
    a recursive grep or rg everything under each directory it names (the
    working directory when it names none). A pattern the expansion here
    can't read exactly (``[[:alpha:]]``), or a tree past WALK_LIMIT, is never clear."""
    if words[0] in NAME_ONLY or not path_denies:
        return True
    cwd = req.cwd or req.worktree
    names: list[str] = []
    for word in words[1:]:
        if any(c in word for c in "*?[") and ("[:" in word or "[=" in word or "[." in word):
            return False
        expanded = _expanded(req, word)
        if expanded is None:
            return False
        for name in expanded:
            names.append(name)
            if name.startswith("--"):
                names.append(name.partition("=")[2])
            elif name.startswith("-"):
                names += [name[i:] for i in range(2, len(name))]
    if _recurses(words):
        expanded = [_expanded(req, word) for word in _search_paths(words)]
        if None in expanded:
            return False
        paths = [name for names in expanded for name in names]
        dirs = ([p for p in (os.path.join(cwd, n) for n in paths if n) if os.path.isdir(p)] if paths
                else [cwd or "."])
        budget = [WALK_LIMIT]
        for d in dirs:
            found = _tree(d, budget)
            if found is None:
                return False
            names += found
    reads = [replace(req, kind="read", command=None, path=os.path.join(cwd, name)) for name in names if name]
    return not any(rule_matches(rule, read) for rule in path_denies for read in reads)


def readonly_pipeline(req: Request, rules: list[Rule], checks: list[str] | None = None) -> bool:
    """Whether ``req`` is one of the read-only commands this section describes: one or more joined by control operators,
    with no substitution, input or file redirection, heredoc or subshell
    (what _simple_commands reads), each a listed program used in a way that
    writes nothing and reads no file a read deny in ``rules`` covers; and no
    bash deny or ask in ``rules`` applies to the whole command or any part
    of it. Quoting is read as the shell reads it, so an escaped regex
    (``grep -v "^ \\|^$"``) is no reason to ask."""
    from brindle.native.permissions import _joined, _simple_commands, _tokens

    command = req.command
    if req.kind != "bash" or not command or "$(" in command or "`" in command:
        return False
    tokens = _tokens(command)
    if tokens is None or any(is_op and "<" in text for is_op, text in tokens):
        return False
    if _tokens(command, word_pattern=_NO_TILDE) is None:
        return False    # an unquoted ~ the shell expands (~/x, a=~/x), reaching outside the worktree
    parsed = _simple_commands(command)
    if not parsed or any(after not in (*COMPOUND_SEPARATORS, "||") for _, after in parsed):
        return False
    if not all(_readonly_part(req, words) for words, _ in parsed):
        return False
    restrictions = [r for r in rules if r.kind == "bash" and r.decision in ("deny", "ask")]
    parts = [req, *(replace(req, command=_joined(words)) for words, _ in parsed)]
    if any(rule_matches(rule, part, checks) for rule in restrictions for part in parts):
        return False
    path_denies = [r for r in rules if r.kind == "read" and r.decision == "deny"]
    if restrictions and _restriction_parts(command) is None:
        # Unquoted pathname patterns: a restriction may match what they expand to.
        assigned: list[bool] = []
        redirects: list[tuple[str, str]] = []
        patterns = _restriction_parts(command, word_pattern=RESTRICTION_WORD, assigned=assigned, redirects=redirects)
        if patterns is not None and not _globs_excluded(req, patterns, restrictions, path_denies, assigned, redirects):
            return False
    return all(_reads_clear(req, words, path_denies) for words, _ in parsed)


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
        return bool(argv and _readonly_git(req, argv))
    if rule.match_type == "git-push":
        return any(sub == "push" for sub, _ in _git_calls(command))
    if rule.match_type == "push":
        return push_matches(rule.match, req, loose=rule.decision != "allow")
    if rule.match_type == "git-force":
        return any(_is_force(a) for _, args in _git_calls(command) for a in args) or forced_push(command)
    if rule.match_type == "rm-destructive":
        return destructive_rm(command)
    if rule.match_type == "readonly-pipeline":
        return readonly_pipeline(req, [], checks)   # the programs alone: decide adds the rules in force
    return False


def _in_scope(rule: Rule, req: Request, checks: list[str] | None) -> bool:
    """Whether ``rule`` applies to ``req`` at all: its kind, unexpired, in its
    repo (if it names one) and not one of its exclusions."""
    if rule.kind != req.kind or rule.expired():
        return False
    if rule.repo_root and (not req.repo_root or os.path.realpath(req.repo_root) != rule.repo_root):
        return False
    return not any(rule_matches(x, req, checks) for x in rule.exclusions)


def rule_matches(rule: Rule, req: Request, checks: list[str] | None = None) -> bool:
    if not _in_scope(rule, req, checks):
        return False
    if rule.source == "preset" and rule.id == f"d-store-unsupported-{req.kind}":
        return True    # Store.blocking_rules: also a request with no target (a search tool without a URL)
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
    """Every rule in force: the org's denies (a Team org's policy,
    brindle.permission_grants.org_denies), the repo's, then the store's (the
    preset's, the person's and learned ones, and the denies standing in for
    rows it can't read)."""
    from brindle import permission_grants

    store = load_store()
    return [*permission_grants.org_denies(repo_root), *(repo_rules(repo_root) if repo_root else []),
            *store.rules, *store.blocking_rules]


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
            if rule.decision != wanted:
                continue
            # A read-only pipeline is one only while no rule in force restricts what it runs or reads.
            if (_in_scope(rule, req, checks) and readonly_pipeline(req, rules, checks)
                    if rule.match_type == "readonly-pipeline" else rule_matches(rule, req, checks)):
                return Decision(wanted, f"{VERBS[wanted]} by {rule.source} rule {rule.id} ({rule.describe()})", rule)
    return None


def decide(req: Request | None, *, checks: list[str] | None = None,
           rules: list[Rule] | None = None, unmatched: str | None = None,
           session: str | None = None, saved: list[SavedAction] | None = None,
           spend: bool = False, groups: dict[str, dict] | None = None) -> Decision:
    """The policy's answer for ``req``: deny > ask > allow > the capability
    group's baseline (``groups``, brindle.permission_groups; the store's when
    None and ``unmatched`` is too) > ``unmatched`` (the store's setting when
    None). An ask a saved action covers (within
    every matching ask rule's limits; ``session``: the asking agent's
    session, for saved actions that last one) is settled by it. ``saved``:
    the saved actions to consider (the store's when None). ``spend``: the
    answer lets the call run, so a saved action with a use budget spends a use."""
    if req is None or req.kind not in KINDS:
        return Decision("ask", "brindle couldn't read the request")
    store = None
    if rules is None or unmatched is None or saved is None:
        store = load_store()
    if rules is None:
        rules = all_rules(req.repo_root or None)
    if groups is None:
        groups = store.groups if unmatched is None else {}
    if unmatched is None:
        unmatched = store.unmatched
    if saved is None:
        saved = store.saved
    ruled = _rule_decision(req, rules, checks)
    if ruled is not None and ruled.decision == "deny":
        return ruled
    # A deny or ask on a part of the command, however it's spelled, beats an
    # allow of the whole; a read-only pipeline was already checked part by part.
    readonly = ruled is not None and ruled.rule is not None and ruled.rule.match_type == "readonly-pipeline"
    restricted = None if readonly else _restriction_in_parts(req, checks=checks, rules=rules)
    if restricted is not None and restricted.decision == "deny":
        return restricted
    if restricted is not None and (ruled is None or ruled.decision == "allow"):
        ruled = restricted
    if saved:
        from brindle import permission_grants

        # A saved deny stops it before any automatic allow; a saved allow is
        # weighed only once the policy's own answer is known (below).
        stopped = permission_grants.settle(req, None, saved, session=session, denies_only=True)
        if stopped is not None:
            return stopped
    if ruled is not None and ruled.decision == "allow":
        return ruled
    from brindle import permission_groups

    # The parts of a command no rule covers: a capability group answers for
    # them even beside a part an ask rule covers (None: the whole command).
    open_parts = None
    compound = req.kind == "bash" and req.command and simple_argv(req.command) is None
    syntax = ruled is not None and ruled.rule is not None and ruled.rule.id == SHELL_SYNTAX_RULE
    # A command no rule covers as a whole, read part by part (read_parts), a cd's folder followed.
    unread: list[ValueError] = []
    read = read_parts(req, rules, checks, unread) if compound and (syntax or ruled is None) else None
    # What brindle can't verify, where it would ask anyway or (an eval of what only the run knows) wherever.
    unverified = compound and (syntax or ruled is None) and read is None
    if unverified and ruled is None and any(isinstance(e, _Unverifiable) for e in unread):
        ruled = _syntax_ask(req)
    if read is not None:
        answer = permission_groups.parts_decision(req, *read, groups, unmatched, rules, checks)
        if answer.decision != "ask":
            return answer
        answer = replace(answer, reason=answer.reason.removesuffix("; asking the user"))
        if answer.rule is None:
            if saved:
                from brindle import permission_grants

                settled = permission_grants.settle(req, None, saved, session=session, spend=spend)
                if settled is not None:
                    return settled
            return Decision("ask", answer.reason + "; asking the user")
        ruled = answer
    elif compound and (ruled is None or groups):
        parts = compound_parts(req.command)
        if parts:
            answers = [_part_decision(req, words, checks, rules) for words in parts]
            for answer in answers:
                if answer.decision == "deny":
                    return answer
            if ruled is None and all(answer.decision == "allow" for answer in answers):
                return Decision("allow", "every part of the command is allowed by a rule")
            ruled = ruled or next((a for a in answers if a.decision == "ask" and a.rule is not None), None)
            open_parts = [words for words, answer in zip(parts, answers)
                          if answer.decision != "allow" and answer.rule is None]
    fallback, why = unmatched, "unmatched actions are denied" if unmatched == "deny" else "unmatched actions run"
    # An ask on one part of a command whose parts can't be read: the group answers for the whole.
    partial = (compound and open_parts is None and ruled is not None and read is None
               and (ruled.rule is None or not rule_matches(ruled.rule, req, checks)))
    if groups and (ruled is None or open_parts or partial):
        fallback, where = permission_groups.baseline(req, groups, rules, checks, open_parts)
        why = f"the {where} baseline is {fallback}"
        if ruled is not None and fallback == "deny":
            return Decision("deny", f"{ruled.reason}, but {why}", ruled.rule)
    if ruled is None and fallback == "deny":
        return Decision("deny", f"no rule covers it, and {why}")
    if ruled is None and fallback == "allow":
        # Nothing covers it, not even a part hiding in it (cd x && git push
        # origin main: _restriction_in_parts above): it runs.
        return Decision("allow", f"no rule covers it; {why}")
    if ruled is not None and ruled.decision == "ask":
        # Several ask rules may match (git push origin HEAD:main HEAD:release):
        # the strictest is the one shown, and a saved allow must fit every one.
        asks = tuple(dict.fromkeys([*(r for r in [ruled.rule] if r is not None), *_asks_in_force(req, rules, checks)]))
        # Shown: the strictest that surely applies (a push whose branch only git
        # knows may fit any push rule; nothing saved covers it anyway).
        sure = [r for r in asks if r.match_type != "push" or push_matches(r.match, req, loose=False)]
        strictest = min(sure, key=_ask_strictness) if sure else None
        if strictest is not None and strictest is not ruled.rule:
            ruled = Decision("ask", f"{VERBS['ask']} by {strictest.source} rule {strictest.id} "
                                    f"({strictest.describe()})", strictest)
        # A command brindle can't verify is approved once at a time, whatever else asks for it.
        if compound and not any(r.id == SHELL_SYNTAX_RULE for r in asks) and (
                unverified or read is None and read_parts(req, rules, checks) is None):
            asks += (_syntax_ask(req).rule,)
        ruled = replace(ruled, asks=asks)
    # What brindle can't verify that no rule asks for: an ask no rule made (a profile's
    # allowed_tools may still answer it), limited as the syntax ask is (once at a time).
    unruled = (_syntax_ask(req).rule,) if ruled is None and unverified else ()
    if saved:
        settled = permission_grants.settle(req, ruled.rule if ruled else None, saved, session=session,
                                           asks=ruled.asks if ruled else unruled, spend=spend)
        if settled is not None:
            return settled
    unchecked = permission_groups.unverifiable(groups, unmatched)
    # Unchecked: brindle can't verify it (the syntax ask is in force), alone or beside other asks.
    asks = ruled.asks if ruled else unruled
    syntax = any(r.id == SHELL_SYNTAX_RULE for r in (*asks, ruled.rule if ruled else None) if r)
    if syntax and unchecked == "deny":
        return Decision("deny", permission_groups.UNVERIFIABLE_GUIDANCE, ruled.rule if ruled else None)
    # The opt-in runs what brindle can't read; never what it could read but a deny rule may
    # cover a word of (read_parts: no reason in ``unread``), nor what another rule asks for.
    alone = ruled is None or ruled.rule is not None and ruled.rule.id == SHELL_SYNTAX_RULE and not asks[1:]
    if syntax and alone and unchecked == "allow" and unread:
        return Decision("allow", f"brindle can't check it, and the {permission_groups.UNVERIFIABLE} setting runs "
                                 f"such commands", ruled.rule if ruled else None)
    if ruled is not None:
        return Decision("ask", ruled.reason + "; asking the user", ruled.rule, ruled.asks)
    if req.kind == "bash" and req.command and simple_argv(req.command) is None:
        why = "the command isn't a single simple command, so no allow rule applies"
    elif req.kind == "read" and req.path:
        why = "not a file git tracks in the worktree, and no rule allows it"
    else:
        why = "no rule allows or denies it"
    return Decision("ask", f"{why}; asking the user", asks=unruled)


def decide_in_parts(req: Request, *, checks: list[str] | None = None,
                    rules: list[Rule], decisions: tuple[str, ...] = ("deny", "ask")) -> Decision | None:
    """The deny (or ask) for any simple command inside ``req``'s shell
    command (``cd x && rm y`` holds two), a deny first, else None: an exact
    or prefix rule on the whole command never sees a part of it."""
    found = _restriction_in_parts(req, checks=checks, rules=[rule for rule in rules if rule.decision in decisions])
    if found is None or found.decision not in decisions:
        return None
    return found if found.decision != "ask" else Decision("ask", found.reason + "; asking the user", found.rule)


def deny_in_parts(req: Request, *, checks: list[str] | None = None,
                  rules: list[Rule]) -> Decision | None:
    """The deny for any simple command inside ``req``'s shell command, else None."""
    return decide_in_parts(req, checks=checks, rules=rules, decisions=("deny",))


# -- restrictions inside a command ---------------------------------------------------------------
#
# A deny or ask rule must not be dodged by spelling: spacing, quotes, leading
# assignments, redirects, a substitution or a pattern the shell expands.
# _restriction_in_parts finds the deny or ask any part of a command is
# subject to; where the command's syntax can't be read and such a rule could
# match it, it answers the bound d-shell-syntax ask (_syntax_ask) rather than
# let it fall to an allow. It never makes an ask where no deny or ask rule
# is in force, and a read-only pipeline (readonly_pipeline) never reaches it
# as a whole.


def _restriction_parts(command: str, *, word_pattern: re.Pattern[str] = SIMPLE_COMMAND,
                       assigned: list[bool] | None = None,
                       redirects: list[tuple[str, str]] | None = None) -> list[list[str]] | None:
    """Command arguments without redirects, solely for restrictive matching.

    Unlike the allow parser, this accepts output files. With RESTRICTION_WORD
    it also accepts unquoted pathname patterns in arguments, for deciding
    whether a restriction could apply, not for literal matching. Quoted
    operators remain arguments; redirects and their operands are removed
    (into ``redirects``, as (operator, operand), when given), and so are
    leading assignments (``assigned`` gets whether each part had any).
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
            if redirects is not None:
                redirects.append((value, tokens[index][1]))
        else:
            return None
        index += 1
    if words:
        parts.append(words)
    elif not parts or tokens[-1][1] in ("&&", "||", "|", "|&"):
        return None
    for words in parts:
        if assigned is not None:
            assigned.append(bool(words) and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])))
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words.pop(0)
        if not words or words[0] in ("!", "{", "}", "if", "then", "else", "elif", "fi", "for", "while",
                                     "until", "do", "done", "case", "esac", "select", "function", "[[", "time", "coproc"):
            return None
        # A pattern in the executable position can select a different program.
        if word_pattern is RESTRICTION_WORD and any(c in words[0] for c in "*?["):
            return None
    return parts


# Programs whose reads a pattern's expansion is checked for (_globs_excluded).
READERS = ("grep", "rg", "head", "tail", "cat", "wc", "sed")
# Rules about what a command does, not the names it lists: plain ls can't match them.
_LISTING_SAFE = ("git-push", "git-force", "push", "rm-destructive")


def _globs_excluded(req: Request, parts: list[list[str]], rules: list[Rule],
                    path_denies: list[Rule], assigned: list[bool], redirects: list[tuple[str, str]]) -> bool:
    """Whether no restriction can apply to what the patterns in ``parts``
    expand to. Plain ``ls`` has a proof: listing names can't push, force
    a git action, rm or read a file's contents, so those rules (and the
    daemon-token deny) don't apply, nor do exact/prefix rules that don't
    overlap the words before its first pattern. A READERS program has one
    once its patterns are expanded, but releasing the command releases every
    part of it, so then every part must be ls, a read-only git command (no
    pattern reaches here) or a READERS program that only reads, whose
    expanded command no restriction matches and whose reads no deny covers,
    with no leading assignment or input redirect (a file it reads that no
    argument names). Any other program may treat an expanded name as a
    subcommand, option or file to act on."""
    readers = False
    for words in parts:
        first = next((i for i, word in enumerate(words) if any(c in word for c in "*?[")), None)
        if first is None:
            continue
        if words[0] in READERS:
            readers = True
            continue
        if words[0] != "ls":
            return False
        prefix = " ".join(" ".join(words[:first]).split()) + " "
        for rule in rules:
            if rule.match_type in _LISTING_SAFE or rule.source == "preset" and rule.id == "d-daemon-sh":
                continue
            if (rule.match_type in ("exact", "prefix")
                    and not prefix.startswith(rule.match) and not rule.match.startswith(prefix)):
                continue
            return False
    if not readers:
        return True
    if any(assigned):
        return False  # an assignment (LD_PRELOAD=..., a tool's config path) changes what any program does
    if any(op in ("<", "<>") or op == "<&" and not operand.isdigit() for op, operand in redirects):
        return False
    for words in parts:
        if words[0] == "ls" or words[0] == "git" and _readonly_git(req, words):
            continue
        if words[0] not in READERS or not _readonly_part(req, words) or not _reads_clear(req, words, path_denies):
            return False
        expanded = [_expanded(req, word) for word in words[1:]]
        if None in expanded:
            return False
        argv = [words[0], *(name for word, names in zip(words[1:], expanded) for name in (names[1:] or [word]))]
        if _matching_restriction([replace(req, command=" ".join(argv))], None, rules) is not None:
            return False
    return True


def _matching_restriction(parts: list[Request], checks: list[str] | None,
                          rules: list[Rule]) -> Decision | None:
    for wanted in ("deny", "ask"):
        for part in parts:
            for rule in rules:
                if rule.decision == wanted and rule_matches(rule, part, checks):
                    return Decision(wanted, f"{VERBS[wanted]} by {rule.source} rule {rule.id} ({rule.describe()})",
                                    rule)
    return None


def _asks_in_force(req: Request, rules: list[Rule], checks: list[str] | None) -> list[Rule]:
    """Every ask rule that matches ``req`` as a whole or, for a shell
    command, any part of it (read as _restriction_in_parts reads it)."""
    parts = [req]
    if req.kind == "bash" and req.command:
        words = (_restriction_parts(req.command)
                 or _restriction_parts(req.command, word_pattern=RESTRICTION_WORD) or _segments(req.command))
        parts += [replace(req, command=" ".join(" ".join(w).split())) for w in words]
    return [r for r in rules if r.decision == "ask" and any(rule_matches(r, p, checks) for p in parts)]


def _ask_strictness(rule: Rule) -> tuple:
    """Sorts ask rules strictest first: by how long a saved approval may
    last (never, a time, the session, until revoked), then how wide."""
    limits = rule.limits or {}
    life = {"once": 0, "time": 1, "session": 2}.get(limits.get("life"), 3)
    scope = limits.get("max_scope")
    reach = LEVELS.index(scope) if scope in LEVELS else len(LEVELS) if not scope or scope in ("*", "**") else 0
    return life, limits.get("minutes") or 0, reach


def _restriction_in_parts(req: Request, *, checks: list[str] | None,
                          rules: list[Rule]) -> Decision | None:
    """The deny (else ask) that a part of ``req``'s shell command is subject
    to, the bound d-shell-syntax ask when its syntax can't be read and a deny
    or ask in force could match it, else None. Shell segments are never
    taken as allows."""
    if req.kind != "bash" or not req.command:
        return None
    # Unsupported syntax is not itself a restriction. Only an active Bash
    # restriction can require us to withhold an ordinary fallback.
    path_denies = [r for r in rules if r.kind == "read" and r.decision == "deny"]
    rules = [r for r in rules if r.kind == "bash" and r.decision in ("deny", "ask") and not r.expired()
             and (not r.repo_root or bool(req.repo_root) and os.path.realpath(req.repo_root) == r.repo_root)]
    if not rules:
        return None
    flat = _flat_script(req.command, req.cwd or req.worktree)
    if flat is not None:
        if not flat.strip():
            return None    # only assignments of literals: nothing runs
        req = replace(req, command=flat)
    if "$(" in req.command or "`" in req.command:
        substituted = _substituted(req, checks, rules)
        if isinstance(substituted, Decision):
            return substituted
        req = substituted
    parsed = _restriction_parts(req.command)
    assigned: list[bool] = []
    redirects: list[tuple[str, str]] = []
    patterns = (_restriction_parts(req.command, word_pattern=RESTRICTION_WORD, assigned=assigned, redirects=redirects)
                if parsed is None else None)
    words = parsed if parsed is not None else patterns if patterns is not None else _segments(req.command)
    parts = [replace(req, command=" ".join(" ".join(part).split())) for part in words]
    matched = _matching_restriction(parts, checks, rules)
    if matched is not None:
        return matched
    if patterns is not None:
        uncertain = not _globs_excluded(req, patterns, rules, path_denies, assigned, redirects)
    else:
        uncertain = parsed is None and any(_may_restrict(rule, req.command) for rule in rules)
    return _syntax_ask(req) if uncertain else None


SHELL_SYNTAX_RULE = "d-shell-syntax"


def _syntax_ask(req: Request) -> Decision:
    # A bound ask keeps unsupported shell syntax from falling to an allow
    # (the unmatched setting, a profile's allowed_tools or the CLI's own).
    # An unchecked command may do anything: it is approved once at a time.
    return Decision("ask", "shell syntax cannot be checked safely",
                    Rule("bash", req.command, "exact", "ask", "preset", id=SHELL_SYNTAX_RULE,
                         limits={"life": "once"}))


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
    """Whether ``rule`` could match some command starting with ``head``,
    whatever a substituted message makes of the rest: unless its fixed
    leading text and ``head`` diverge, it might. A preset's rules are known
    not to apply to a commit or tag message."""
    if rule.source == "preset":
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

    if rule.match_type in ("git-push", "push"):
        return seen("git") and seen("push")
    if rule.match_type == "git-force":
        return seen("git") and (any(_is_force(w.strip("`$(){};|&<>")) for w in text.split())
                                or any(seen(flag) for flag in ("-f", *FORCE_OPTIONS)))
    if rule.match_type == "rm-destructive":
        return (bool(re.search(r"(?<![\w.-])rm(?![\w.-])", text))
                or any(fnmatch.fnmatchcase("rm", p) for p in patterns))
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


# -- scripts --------------------------------------------------------------------------------------
#
# An ordinary script (a for/while/if, variables set and used, a $(...)
# whose output is assigned, a quoted heredoc fed to a program) is read as
# the simple commands it runs (_flat_script), so a restriction is matched
# against each of them instead of answering the d-shell-syntax ask for the
# whole. A variable assigned a literal is substituted where the shell would
# expand it; one whose value only the run knows stays as written, which
# _restriction_in_parts reads as unknown, as before.

# The reserved words that open or continue a compound command before a command.
_SCRIPT_OPENERS = ("if", "then", "elif", "else", "do", "while", "until", "{")
# Reserved words and syntax _flat_script doesn't read: the script is checked as written.
_SCRIPT_UNREAD = ("case", "esac", "select", "function", "coproc")
_SCRIPT_OPERATORS = ("<<<", "<<-", "&>>", ";;", "&&", "||", "|&", ">>", "<<", ">&", "<&", "&>", ">|", "<>",
                     ";", "&", "|", "<", ">", "(", ")")
_SCRIPT_SEPARATORS = (";", "\n", "&&", "||", "|", "|&", "&")
# Builtins that may set a shell variable named among their arguments (read -r line, printf -v x).
_VARIABLE_SETTERS = frozenset({"read", "mapfile", "readarray", "getopts", "printf", "declare", "typeset", "local",
                               "export", "readonly", "let", "unset"})
# Programs that may run what a heredoc feeds them as commands (or wrap one that does):
# with one in the script, a heredoc body isn't data.
_STDIN_RUNNERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "xargs", "parallel", "source", ".", "eval",
                            "env", "sudo", "doas", "su", "exec", "command", "nohup", "nice", "time", "watch", "ssh",
                            "timeout"})
# The most simple commands a script may flatten to (loops unrolled).
_FLAT_LIMIT = 500
_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
_ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)=")


@dataclass
class _Word:
    """A word as written, with the ``$NAME``/``${NAME}`` references in it
    (start, end, name, inside double quotes) and the ``$(...)``/backtick
    substitutions (their inner commands)."""
    raw: str
    spaced: bool
    refs: list[tuple[int, int, str, bool]]
    substitutions: list[str]

    def plain(self) -> str | None:
        """The word when it is literal text with nothing to expand or unquote."""
        return self.raw if re.fullmatch(r"[A-Za-z0-9_\-./:=@%+,{}!\[\]]+", self.raw) else None


def _script_word(command: str, i: int) -> tuple[str, list, list, int] | None:
    """The word starting at ``i``: (raw text, references, substitutions,
    end), or None for quoting or an expansion it doesn't read (``$'...'``,
    ``${x:-y}``) or an unclosed quote or substitution."""
    start, refs, subs, quoted, n = i, [], [], False, len(command)
    while i < n:
        c = command[i]
        if not quoted and (c in " \t\n" or c in ";&|<>()"):
            break
        if c == "\\":
            i += 2
        elif c == "'" and not quoted:
            close = command.find("'", i + 1)
            if close < 0:
                return None
            i = close + 1
        elif c == '"':
            quoted, i = not quoted, i + 1
        elif c == "`" or command.startswith("$(", i):
            end = _substitution_end(command, i)
            if end is None:
                return None
            subs.append(command[i + (1 if c == "`" else 2):end - 1])
            i = end
        elif c == "$":
            found = _REFERENCE.match(command, i)
            if found:
                refs.append((i - start, found.end() - start, found.group(1) or found.group(2), quoted))
                i = found.end()
            elif command.startswith("${", i) or not quoted and command.startswith("$'", i):
                return None
            else:
                i += 1    # $1, $@, $?: left as written, unknown
        else:
            i += 1
    if quoted:
        return None
    return command[start:i], refs, subs, i


def _script_tokens(command: str) -> list[tuple] | None:
    """``("word", _Word)``, ``("op", text, spaced)`` and ``("heredoc", body,
    expands, spaced)`` for each word, operator and heredoc of ``command``,
    or None for what _flat_script doesn't read: a comment, a subshell,
    process substitution, ``;;`` or an unclosed heredoc."""
    from brindle.native.permissions import _tokens

    out: list[tuple] = []
    pending: list[tuple[str, bool, list]] = []   # heredocs whose body starts after the next newline
    i, n, spaced = 0, len(command), True
    while i < n:
        c = command[i]
        if c in " \t" or command.startswith("\\\n", i):
            i += 1 if c in " \t" else 2
            spaced = True
            continue
        if c == "\n":
            out.append(("op", "\n", spaced))
            i, spaced = i + 1, True
            for delimiter, dash, body in pending:
                while True:
                    if i >= n:
                        return None
                    end = command.find("\n", i)
                    end = n if end < 0 else end
                    line, i = command[i:end], end + 1
                    if (line.lstrip("\t") if dash else line) == delimiter:
                        break
                    body.append(line)
            pending = []
            continue
        if c == "#":
            return None
        if c in ";&|<>()":
            op = next(o for o in _SCRIPT_OPERATORS if command.startswith(o, i))
            if op in ("(", ")", ";;") or op in ("<", ">") and command.startswith("(", i + 1):
                return None
            i += len(op)
            if op in ("<<", "<<-"):
                while i < n and command[i] in " \t":
                    i += 1
                word = _script_word(command, i)
                delimiter = _tokens(word[0]) if word is not None and not word[1] and not word[2] else None
                if not delimiter or len(delimiter) != 1 or delimiter[0][0]:
                    return None
                raw, _, _, i = word
                body: list[str] = []
                pending.append((delimiter[0][1], op == "<<-", body))
                out.append(("heredoc", body, not any(q in raw for q in "'\"\\"), spaced))
            else:
                out.append(("op", op, spaced))
            spaced = False
            continue
        word = _script_word(command, i)
        if word is None:
            return None
        raw, refs, subs, i = word
        out.append(("word", _Word(raw, spaced, refs, subs)))
        spaced = False
    if pending:
        return None
    return out


def _script_items(tokens: list[tuple]) -> list[tuple]:
    """The script as ``("kw", word)`` (``("kw", word, piped)`` for a group's
    braces: a pipe before its { or after its }), ``("for", name, values or None)`` and
    ``("cmd", tokens, separator before, separator after, negated by !)``
    items. Raises ValueError for what _flat_script doesn't read."""
    commands: list[tuple[list, str, str]] = []
    current: list[tuple] = []
    before = ""
    for token in [*tokens, ("op", "", False)]:
        if token[0] != "op" or token[1] not in (*_SCRIPT_SEPARATORS, ""):
            current.append(token)
        elif current:
            commands.append((current, before, token[1]))
            current, before = [], token[1]
        elif token[1] not in ("\n", ""):
            raise ValueError("an empty command")
    items: list[tuple] = []
    negated = False    # a ! before a { on a line of its own marks the group's first command
    for toks, before, after in commands:
        while toks and toks[0][0] == "word" and toks[0][1].plain() in (*_SCRIPT_OPENERS, "!"):
            if toks[0][1].raw == "{":
                items.append(("kw", "{", before in ("|", "|&")))    # piped: it runs in a subshell
            elif toks[0][1].raw != "!":
                items.append(("kw", toks[0][1].raw))
            negated |= toks[0][1].raw == "!"
            toks = toks[1:]
        first = toks[0][1].plain() if toks and toks[0][0] == "word" else None
        if first == "for":
            words = [t[1] for t in toks]
            if len(words) < 2 or any(t[0] != "word" for t in toks) or not re.fullmatch(r"[A-Za-z_]\w*", words[1].raw):
                raise ValueError("a for loop it can't read")
            if len(words) > 2 and words[2].raw != "in":
                raise ValueError("a for loop it can't read")
            items.append(("for", words[1].raw, words[3:] if len(words) > 2 else None))
            continue
        if first in ("done", "fi", "}"):
            items.append(("kw", first, after in ("|", "|&")) if first == "}" else ("kw", first))
            toks = toks[1:]
            if not toks:
                continue
            # The redirects of the whole loop (done < input): checked on a no-op command.
            if toks[0][0] == "word" and not (toks[0][1].raw.isdigit() and len(toks) > 1 and toks[1][0] != "word"):
                raise ValueError("words after " + first)
            toks = [("word", _Word(":", True, [], [])), *toks]
        elif first in _SCRIPT_UNREAD:
            raise ValueError(f"{first} isn't read")
        if toks:
            items.append(("cmd", toks, before, after, negated))
            negated = False
    return items


def _script_tree(items: list[tuple], i: int = 0, stops: tuple[str, ...] = ()) -> tuple[list[tuple], int]:
    """Nodes from ``items[i:]`` up to a reserved word in ``stops``:
    ``("cmd", ...)``, ``("for", name, values, body)``, ``("maybe", body)``
    for what may run any number of times (if, while, until) and
    ``("group", body, piped)`` (a pipeline's part: in a subshell of its
    own). Raises ValueError for a structure left open."""
    def expect(i: int, word: str) -> int:
        if i >= len(items) or items[i][:2] != ("kw", word):
            raise ValueError(f"no {word}")
        return i + 1

    nodes: list[tuple] = []
    while i < len(items):
        item = items[i]
        if item[0] == "kw" and item[1] in stops:
            return nodes, i
        if item[0] == "cmd":
            nodes.append(item)
            i += 1
        elif item[0] == "for":
            body, i = _script_tree(items, expect(i + 1, "do"), ("done",))
            nodes.append(("for", item[1], item[2], body))
            i = expect(i, "done")
        elif item[1] in ("while", "until"):
            cond, i = _script_tree(items, i + 1, ("do",))
            body, i = _script_tree(items, expect(i, "do"), ("done",))
            nodes.append(("maybe", cond + body))
            i = expect(i, "done")
        elif item[1] == "if":
            body: list[tuple] = []
            while True:
                cond, i = _script_tree(items, i + 1, ("then",))
                branch, i = _script_tree(items, expect(i, "then"), ("elif", "else", "fi"))
                body += cond + branch
                if items[i] != ("kw", "elif"):
                    break
            if items[i] == ("kw", "else"):
                branch, i = _script_tree(items, i + 1, ("fi",))
                body += branch
            nodes.append(("maybe", body))
            i = expect(i, "fi")
        elif item[1] == "{":
            body, i = _script_tree(items, i + 1, ("}",))
            i = expect(i, "}")
            nodes.append(("group", body, item[2] or items[i - 1][2]))
        else:
            raise ValueError(f"a stray {item[1]}")
    if stops:
        raise ValueError(f"no {stops[-1]}")
    return nodes, i


def _assigned_in(nodes: list[tuple]) -> set[str] | None:
    """The variables ``nodes`` may set, or None when any may be (eval, source)."""
    names: set[str] = set()
    for node in nodes:
        if node[0] == "for":
            inner = _assigned_in(node[3])
            if inner is None:
                return None
            names |= inner | {node[1]}
        elif node[0] in ("maybe", "group"):
            inner = _assigned_in(node[1])
            if inner is None:
                return None
            names |= inner
        else:
            words = [t[1] for t in node[1] if t[0] == "word"]
            program = next((w.raw for w in words if not _ASSIGNMENT.match(w.raw)), None)
            if program in ("eval", "source", "."):
                return None
            names |= {m.group(1) for w in words if (m := _ASSIGNMENT.match(w.raw))}
            if program in _VARIABLE_SETTERS:
                names |= {m.group(0) for w in words if (m := re.match(r"[A-Za-z_]\w*", w.raw))}
    return names


def _dq_escaped(text: str) -> str:
    return re.sub(r'([\\"$`])', r"\\\1", text)


class _Flattener:
    """Walks a script's nodes (_script_tree) in order, keeping the literal
    value each variable surely has (``("lit", text)``, or ``("glob",
    pattern)`` for a for loop's pattern word), and collects the simple
    commands it runs."""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.out: list[str] = []
        # Whether each runs in this shell whenever the script does (not in an if, a loop,
        # after && or ||, in a pipeline, or a substitution's subshell).
        self.definite: list[bool] = []
        # Whether each runs only if the command before it succeeded (after && or in its
        # pipeline), and whether what follows it runs only if it succeeded, in this shell
        # (before &&, not negated or at a pipeline's end): what a cd's chain is (_PartReader).
        # Neither in an if, a loop or a substitution, nor anywhere after a negation (! { ... }).
        self.chained: list[tuple[bool, bool]] = []
        self.track = True
        self.changed = False

    def emit(self, command: str, definite: bool = False, chained: tuple[bool, bool] = (False, False)) -> None:
        self.out.append(command)
        self.definite.append(definite)
        self.chained.append(chained if self.track else (False, False))
        if len(self.out) > _FLAT_LIMIT:
            raise ValueError("too many commands")

    def script(self, command: str, env: dict) -> None:
        tokens = _script_tokens(command)
        if tokens is None:
            raise ValueError("unreadable syntax")
        if any(t[0] == "heredoc" for t in tokens) and any(
                t[0] == "word" and os.path.basename(t[1].raw) in _STDIN_RUNNERS for t in tokens):
            raise ValueError("a heredoc may be run as commands")
        nodes, _ = _script_tree(_script_items(tokens))
        self.walk(nodes, env, True)

    def walk(self, nodes: list[tuple], env: dict, definite: bool, chains: bool = True) -> None:
        for node in nodes:
            if node[0] == "cmd":
                self.track &= not node[4]
                chained = (chains and node[2] in ("&&", "|", "|&"),
                           chains and node[2] in ("", ";", "\n", "&&", "&") and node[3] == "&&")
                self.command(node[1], env, definite and node[2] in ("", ";", "\n")
                             and node[3] in ("", ";", "\n", "&&", "||"), chained)
                continue
            self.changed = True
            if node[0] == "group":
                self.walk(node[1], env, definite, chains and not node[2])
                continue
            body = node[3] if node[0] == "for" else node[1]
            values = self.loop_values(node[2], env) if node[0] == "for" else []
            assigned = _assigned_in(body)
            if assigned is None:
                env.clear()
            for name in assigned or ():
                env.pop(name, None)    # a later pass sees what an earlier one set
            if node[0] == "maybe":
                self.walk(body, env, False, False)
                continue
            for value in dict.fromkeys(values):
                if value is None:
                    env.pop(node[1], None)
                else:
                    env[node[1]] = value
                self.walk(body, env, False, False)
            env.pop(node[1], None)

    def loop_values(self, words: list[_Word] | None, env: dict) -> list[tuple | None]:
        """What a for loop's variable takes, in order (None: unknown)."""
        from brindle.native.permissions import _tokens

        if words is None:
            return [None]    # for x; do: the positional parameters
        values: list[tuple | None] = []
        for word in words:
            for inner in word.substitutions:
                self.substitution(inner, env)
            text = self.text(word, env)
            if word.substitutions:
                return [None]
            found = _tokens(text, word_pattern=SIMPLE_COMMAND)
            if found is not None and not any(is_op for is_op, _ in found):
                values += [("lit", value) for _, value in found]
            elif RESTRICTION_WORD.fullmatch(text) and " " not in text:
                values.append(("glob", text))
            else:
                return [None]
        return values

    def substitution(self, inner: str, env: dict) -> None:
        sub = _Flattener(self.cwd)
        sub.script(inner, dict(env))
        for command in sub.out:
            self.emit(command)

    def text(self, word: _Word, env: dict, assignment: bool = False) -> str:
        """``word`` as written with each reference to a known variable
        replaced by text the shell reads as the same words; any other stays."""
        out, last = [], 0
        for start, end, name, quoted in word.refs:
            value = env.get(name)
            replacement = None
            if value is not None and value[0] == "glob":
                # A loop over a pattern: each name it matches, as one word ("$f") or split again ($f).
                whole = word.raw == word.raw[start:end] or quoted and word.raw == f'"{word.raw[start:end]}"'
                names = _bounded_glob(value[1], self.cwd or None) if whole and not assignment and not quoted else None
                if whole and not assignment and (quoted or names is not None and not any(
                        re.search(r"[\s*?\[]", match) for match in names)):
                    self.changed = True
                    return value[1]
            elif value is not None:
                text = value[1]
                fields = [f for f in re.split(r"[ \t\n]+", text) if f]
                if quoted:
                    replacement = _dq_escaped(text)
                elif assignment:
                    replacement = f'"{_dq_escaped(text)}"'
                elif not any(c in text for c in "*?["):
                    replacement = " ".join(shlex.quote(f) for f in fields)
                elif all(RESTRICTION_WORD.fullmatch(f) for f in fields):
                    replacement = " ".join(fields)    # it expands to the names the pattern matches
            if replacement is not None:
                out += [word.raw[last:start], replacement]
                last = end
                self.changed = True
        return "".join(out) + word.raw[last:]

    def command(self, tokens: list[tuple], env: dict, definite: bool,
                chained: tuple[bool, bool] = (False, False)) -> None:
        from brindle.native.permissions import _tokens

        words = [t[1] for t in tokens if t[0] == "word"]
        # export D=/path sets its variables as an assignment does; the export still runs as a
        # command of its own (what it exports reaches every program after it).
        exported = (len(words) == len(tokens) > 1 and words[0].raw == "export"
                    and all(_ASSIGNMENT.match(w.raw) for w in words[1:]))
        # Plain assignments expand left to right, each seeing the one before; export's
        # words all expand before any of its assignments is made.
        seen = dict(env) if exported else env
        if exported or len(words) == len(tokens) and all(_ASSIGNMENT.match(w.raw) for w in words):
            self.changed = True
            for word in words[exported:]:
                name = _ASSIGNMENT.match(word.raw).group(1)
                if _PROGRAM_VARIABLES.fullmatch(f"{name}="):
                    raise ValueError(f"{name} changes which program runs or how words split")
                for inner in word.substitutions:
                    self.substitution(inner, seen)
                text = self.text(word, seen, assignment=True)[len(name) + 1:]
                found = _tokens(text) if text else [(False, "")]
                known = (definite and not word.substitutions and "~" not in text and found is not None
                         and len(found) == 1 and not found[0][0])
                if known:
                    env[name] = ("lit", found[0][1])
                else:
                    env.pop(name, None)
            if not exported:
                return
        program = next((w.raw for w in words if not _ASSIGNMENT.match(w.raw)), None)
        if program in ("eval", "source", "."):
            env.clear()
        elif program in _VARIABLE_SETTERS and not exported:
            for word in words:
                if found := re.match(r"[A-Za-z_]\w*", word.raw):
                    env.pop(found.group(0), None)
        pieces: list[str] = []
        for index, token in enumerate(tokens):
            if token[0] == "heredoc":
                _, body, expands, spaced = token
                if expands and any(c in line for line in body for c in "$`\\"):
                    raise ValueError("a heredoc that expands")
                if index and tokens[index - 1][0] == "word" and tokens[index - 1][1].raw.isdigit() and not spaced:
                    raise ValueError("a heredoc on another descriptor")
                self.changed = True    # its body is only the program's input
                continue
            spaced, text = (token[1].spaced, self.text(token[1], seen)) if token[0] == "word" else (token[2], token[1])
            pieces.append((" " if spaced and pieces else "") + text)
        if pieces:
            self.emit("".join(pieces), definite, chained)


def _flat_script(command: str, cwd: str) -> str | None:
    """The simple commands ``command`` runs, one per line, with loops
    unrolled, known variables substituted, the inner commands of an
    assigned ``$(...)`` in place of the assignment, and quoted heredoc
    bodies dropped (they are a program's input, unless a shell may read
    them). None when there's nothing to flatten or its syntax isn't read."""
    flattener = _Flattener(cwd)
    try:
        flattener.script(command, {})
    except ValueError:
        return None
    return "\n".join(flattener.out) if flattener.changed else None


# -- reading a command part by part -------------------------------------------------------------
#
# A command no simpler reading covers (a pattern, a tilde, a variable, a
# redirection, a loop) is read as the simple commands it runs (ShellPart),
# each judged on its own (read_parts; brindle.permission_groups decides the
# whole from them). What can't be verified gives None: a backtick, eval or
# source, a heredoc a program may run as code, a program only the run knows,
# or a word only the run knows where a deny rule could match it.

# Programs that run what they're fed as code: a heredoc or here-string to one isn't data.
_CODE_RUNNERS = _STDIN_RUNNERS | {"python", "python3", "node", "ruby", "perl", "php", "osascript", "deno", "bun",
                                  "lua", "Rscript", "irb", "swift"}
# Programs that run another program named among their arguments.
_WRAPPERS = _STDIN_RUNNERS | {"builtin", "stdbuf", "caffeinate", "flock", "unbuffer", "chroot"}
# Variables that change which program runs or what it loads.
_PROGRAM_VARIABLES = re.compile(r"(PATH|LD_\w+|DYLD_\w+|BASH_ENV|ENV|IFS)=")
_OUTPUT_REDIRECTS = (">", ">>", ">|", "&>", "&>>", "<>")
_REDIRECTS = (*_OUTPUT_REDIRECTS, "<", "<<<", ">&", "<&")


@dataclass
class ShellPart:
    """One simple command a shell command runs."""
    words: list[str]        # as the shell runs them, patterns expanded; a word only the run knows as written
    line: str               # as written, known variables substituted
    cwd: str | None         # where it runs (None: after a cd brindle can't follow)
    unknown: bool = False   # a word only the run knows ($x, a $(...)'s output, a pattern it can't expand)
    writes: list[str] = field(default_factory=list)    # the files its redirections write
    reads: list[str] = field(default_factory=list)     # the files its redirections read
    # Its words with each pattern as written (for _globs_excluded), whether one has a
    # pattern, and whether a leading assignment or a redirection does.
    written: list[str] = field(default_factory=list)
    globs: bool = False
    data: bool = False    # a $(...) supplies a message (SUBSTITUTION_DATA_OPTIONS)
    # A word the shell expands (a tilde, a pattern): no allow rule's words are what it runs;
    # a tilde reaches outside the worktree, so nothing with one only reads (readonly_pipeline).
    expanded: bool = False
    tilde: bool = False
    assigned: bool = False
    redirect_glob: bool = False


class _Unverifiable(ValueError):
    """What brindle can't verify wherever it is (an eval of what only the run
    knows): the unverifiable answer even where nothing would have asked."""


class _PartReader:
    def __init__(self, req: Request):
        self.req = req
        self.cwd: str | None = req.cwd or req.worktree or None
        self.settled = self.cwd    # where the shell is once a cd's && chain ends, whether the cd ran or not
        self.parts: list[ShellPart] = []
        self.wrote = False    # an earlier part may have made files a later pattern would match
        self.expanded = self.tilde = False    # the part being read's ShellPart.expanded and .tilde

    def read(self, command: str, definite: bool = True, chains: bool = True) -> None:
        """``command``'s parts; ``definite``: whether it surely runs in this
        shell (an eval's words inside an if or a pipeline may not); ``chains``:
        whether a cd's && chain is followed in it (not in an eval's words)."""
        if "`" in command:
            raise ValueError("a backtick")
        if "<<" in command and any(os.path.basename(w) in _CODE_RUNNERS
                                   for w in re.split(r"[\s;&|()<>`\"'$=]+", command)):
            raise ValueError("a heredoc a program may run as code")
        flattener = _Flattener(self.cwd or "")
        flattener.script(command, {})
        for line, sure, (chained, leads) in zip(flattener.out, flattener.definite, flattener.chained):
            self.part(line, definite and sure, chains and chained, chains and leads)

    def inner(self, command: str) -> None:
        """A substitution's commands: they run in a subshell of their own."""
        kept = self.cwd, self.settled, self.expanded, self.tilde
        self.settled = self.cwd
        self.read(command)
        self.cwd, self.settled, self.expanded, self.tilde = kept

    def part(self, line: str, definite: bool, chained: bool = False, leads: bool = False) -> None:
        """One simple command; ``chained``: it runs only if the one before it
        succeeded (_Flattener.chained); ``leads``: what follows it runs only if
        it succeeded."""
        tokens = _script_tokens(line)
        if tokens is None or any(t[0] == "heredoc" for t in tokens):
            raise ValueError("unreadable syntax")
        if not chained:
            self.cwd = self.settled
        part = ShellPart([], line, self.cwd)
        self.expanded = self.tilde = False
        program: str | None = None
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token[0] == "op":
                if token[1] not in _REDIRECTS or i + 1 >= len(tokens) or tokens[i + 1][0] != "word":
                    raise ValueError("unreadable syntax")
                previous = tokens[i - 1] if i else None
                if previous is not None and previous[0] == "word" and previous[1].raw.isdigit() and not token[2]:
                    part.words.pop()    # the 2 of 2>&1 names a descriptor
                    part.written.pop()
                self.redirect(part, token[1], tokens[i + 1][1])
                i += 2
                continue
            word = token[1]
            if program is None and _ASSIGNMENT.match(word.raw):
                if _PROGRAM_VARIABLES.match(word.raw):
                    raise ValueError("an assignment that changes the program")
                for inner in word.substitutions:
                    self.inner(inner)
                part.assigned = True
                i += 1
                continue
            data = (len(part.words) >= 3 and len(word.substitutions) == 1 and word.raw.startswith('"$(')
                    and word.raw.endswith(')"') and not word.refs
                    and part.words[-1] in SUBSTITUTION_DATA_OPTIONS.get((part.words[0], part.words[1]), ()))
            names, known, written = self.word(word)
            if data:
                names, known, written = [_SUBSTITUTED], True, None
                part.data = True
            if program is None:
                if not known or len(names) != 1 or written is not None:
                    raise ValueError("a program only the run knows")
                program = os.path.basename(names[0])
            part.words += names
            part.written.append(written or (names[0] if known else word.raw))
            part.globs |= written is not None
            part.unknown |= not known
            i += 1
        part.expanded, part.tilde = self.expanded, self.tilde
        if program is None:
            if part.writes or part.reads:
                raise ValueError("a redirection without a command")
            return
        if program == "eval" and not (part.unknown or part.expanded or part.writes or part.reads):
            # The command its literal words make, run where the eval runs (if it surely does).
            self.read(" ".join(part.words[1:]), definite, chains=False)
            return
        if program == "eval":
            raise _Unverifiable("eval runs what only the run knows")
        if program in ("source", ".") or program in _WRAPPERS and part.unknown:
            raise ValueError(f"{program} runs what only the run knows")
        self.parts.append(part)
        if program in ("cd", "pushd"):
            # What runs only if the cd succeeded (cd x && ...) runs where it leads; anything else
            # after it (after ; or ||, past an if or a pipeline) runs where it may or may not have
            # led: somewhere unknown, unless that is where the shell already is.
            target = self.directory(part.words[1:], part.unknown)
            if target != self.cwd:
                self.settled = self.settled if target == self.settled else None
                self.cwd = target if leads else self.settled
        elif part.writes or not _readonly_part(replace(self.req, cwd=self.cwd), part.words):
            self.wrote = True

    def word(self, word: _Word) -> tuple[list[str], bool, str | None]:
        """What ``word`` stands for, whether brindle knows it before it runs,
        and the pattern it is (a leading ~ expanded), if it is one."""
        for inner in word.substitutions:
            self.inner(inner)
        raw = word.raw
        text = re.sub(r"\\.", "", _unquoted(raw))
        if re.search(r"\$[\w{(@*#?!$-]", text):    # "^$": a $ before a quote is literal
            return [raw], False, None
        bare = re.sub(r'"(?:[^"\\]|\\.)*"', "", text).replace("'", "")
        try:
            value, = shlex.split(raw)
        except ValueError:
            return [raw], False, None
        if re.search(r"\{[^}\s]*,", bare):
            return [raw], False, None    # a brace expansion
        if bare.startswith("~"):
            if not re.match(r"~(/|$)", raw):
                return [raw], False, None    # ~user, ~+
            value = os.path.expanduser(value)
            self.expanded = self.tilde = True
        elif re.search(r"[=:]~", bare):
            self.expanded = self.tilde = True    # bash may expand it (path=~/x) or not
        if re.search(r"[*?]|\[.+\]", bare):    # an unclosed [ (test's) is literal
            names = self.expand(value)
            # One that matches nothing stays as written (--include=*.py): the program gets it as is.
            self.expanded |= names != []
            return ([raw], False, value) if names is None else (names or [value], True, value)
        return [value], True, None

    def expand(self, pattern: str) -> list[str] | None:
        """The names ``pattern`` matches where it runs, None when brindle
        can't know them before it runs."""
        if self.wrote or self.cwd is None and not os.path.isabs(pattern):
            return None
        if "[:" in pattern or "[=" in pattern or "[." in pattern:
            return None
        return _bounded_glob(pattern, self.cwd)

    def redirect(self, part: ShellPart, op: str, target: _Word) -> None:
        names, known, pattern = self.word(target)
        name = names[0] if known and len(names) == 1 and pattern is None else None
        if op == "<<<":
            return    # a here-string: the program's input
        part.redirect_glob |= pattern is not None    # it can select a protected file
        if op in (">&", "<&") and name is not None and (name == "-" or name.isdigit()):
            return
        # A name only the run knows stays as written (not absolute): read_parts and the groups take it so.
        if name is None:
            name = target.raw
        elif self.cwd is not None or os.path.isabs(name):
            name = os.path.normpath(os.path.join(self.cwd or "", name))
        if op == "<":
            part.reads.append(name)
        elif name != "/dev/null":
            part.writes.append(name)

    def directory(self, args: list[str], unknown: bool) -> str | None:
        """Where a cd to ``args`` leads, when brindle can follow it."""
        args = [a for a in args if a != "--"]
        if unknown or len(args) > 1 or args and args[0].startswith("-"):
            return None
        target = os.path.expanduser(args[0] if args else "~")
        if self.cwd is None and not os.path.isabs(target):
            return None
        target = os.path.normpath(os.path.join(self.cwd or "", target))
        return target if os.path.isdir(target) else None


def shell_parts(req: Request, why: list[ValueError] | None = None) -> list[ShellPart] | None:
    """The simple commands ``req``'s shell command runs (ShellPart), or None
    when brindle can't verify what it runs (see the section above; ``why``
    gets the reason)."""
    if req.kind != "bash" or not req.command:
        return None
    reader = _PartReader(req)
    try:
        reader.read(req.command)
    except ValueError as e:
        if why is not None:
            why.append(e)
        return None
    return reader.parts or None


# git commands that only read: no force flag or push hides in them.
_GIT_UNFORCED = frozenset({*READONLY_GIT, "rev-parse", "merge-base", "rev-list", "ls-files", "ls-tree", "cat-file",
                           "describe", "shortlog", "blame", "grep", "for-each-ref", "name-rev", "show-ref"})


def _unread(word: str) -> bool:
    """Whether a ShellPart word is one only the run knows (kept as written)."""
    return bool(re.search(r"\$[\w{(@*#?!$-]|[*?]|\[.+\]|\{[^}\s]*,", word))


def _wild_match(rule: Rule, part: ShellPart) -> bool:
    """Whether ``rule`` could match ``part`` once the run supplies the words
    only it knows: a rule about a program (a push, a forced git command, a
    recursive rm, a command's leading words) where the part runs it. A
    pattern that may match anywhere in a command (``*daemon.json*``) is
    matched against what brindle can read of it (a variable known or not as
    written, a pattern expanded), as it is against what an interpreter's
    code names: text only the run produces is beyond what text matching sees."""
    program = os.path.basename(part.words[0])
    if rule.match_type in ("git-push", "push", "git-force"):
        if program != "git":
            return False
        args = part.words[1:]
        while args and args[0].startswith("-") and not _unread(args[0]):    # global options
            args = args[2:] if args[0] in ("-C", "-c") else args[1:]
        sub = args[0] if args and not _unread(args[0]) else None
        if rule.match_type == "git-force":
            return sub not in _GIT_UNFORCED    # a force flag means nothing to a command that only reads
        return sub in (None, "push")
    if rule.match_type == "rm-destructive":
        return program == "rm"
    if rule.match_type not in MATCH_TYPES:
        return True
    fixed = re.split(r"[*?\[]", rule.match, maxsplit=1)[0] if rule.match_type == "glob" else rule.match
    fixed = " ".join(fixed.split())
    if not fixed:
        return False
    head = []    # the words before the first one only the run knows
    for word in part.words:
        if _unread(word):
            break
        head.append(word)
    known = " ".join(" ".join(head).split())
    return known.startswith(fixed) or fixed.startswith(known)


def _pathspecs(part: ShellPart) -> bool:
    """A git command with every pattern after ``--``: git takes the names
    they expand to only as paths (the restrictions still see the names)."""
    if os.path.basename(part.words[0]) != "git" or "--" not in part.written:
        return False
    return not any(re.search(r"[*?]|\[.+\]", word) for word in part.written[:part.written.index("--")])


def read_parts(req: Request, rules: list[Rule], checks: list[str] | None = None,
               why: list[ValueError] | None = None) -> tuple[list[ShellPart], Decision | None] | None:
    """``req``'s shell command as ShellParts with the deny (else ask) a rule
    gives any of them, or None when brindle can't verify it: it can't read
    the command (``why`` gets the reason), or a deny rule could match a word
    only the run knows."""
    from brindle.native.permissions import _joined

    parts = shell_parts(req, why)
    if parts is None:
        return None
    restrictions = [r for r in rules if r.kind == "bash" and r.decision in ("deny", "ask") and not r.expired()
                    and (not r.repo_root or bool(req.repo_root) and os.path.realpath(req.repo_root) == r.repo_root)]
    path_denies = [r for r in rules if r.kind == "read" and r.decision == "deny" and r.match_type != "tracked"]
    write_denies = any(r.kind in ("write", "edit") and r.decision == "deny" for r in rules)
    found = None
    for part in parts:
        at = replace(req, cwd=part.cwd or req.cwd)
        if (write_denies and any(not os.path.isabs(path) for path in part.writes)
                or path_denies and any(not os.path.isabs(path) for path in part.reads)):
            return None    # a file only the run names: a deny may cover it
        if part.data and any(_could_cover(rule, " ".join(part.words[:2])) for rule in restrictions):
            return None    # the message may be one a rule names
        if restrictions and (part.redirect_glob or part.globs and not (part.cwd is not None and (
                _pathspecs(part) or _globs_excluded(at, [part.written], restrictions, path_denies, [part.assigned],
                                                    [("<", path) for path in part.reads])))):
            return None    # a pattern a program may take as an option or a subcommand, or a protected file
        if path_denies and os.path.basename(part.words[0]) in READERS and (
                part.cwd is None or not _reads_clear(at, part.written, path_denies)):
            return None    # it may read a file a deny covers, or where brindle can't tell
        # With the files it redirects to too: a deny on a name sees them.
        texts = [replace(at, command=_joined(words)) for words in (part.words, [*part.words, *part.writes, *part.reads])]
        matched = _matching_restriction(texts, checks, restrictions)
        if matched is not None and matched.decision == "deny":
            return parts, matched
        for path in part.reads:
            read = replace(req, kind="read", command=None, path=path)
            denied = next((r for r in path_denies if rule_matches(r, read)), None)
            if denied is not None:
                return parts, Decision("deny", f"denied by {denied.source} rule {denied.id} ({denied.describe()})",
                                       denied)
        if part.unknown:
            for rule in restrictions:
                if _wild_match(rule, part):
                    if rule.decision == "deny":
                        return None
                    matched = matched or Decision("ask", f"{VERBS['ask']} by {rule.source} rule {rule.id} "
                                                         f"({rule.describe()})", rule)
        found = found or matched
    return parts, found


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
        rule = Rule.from_dict(row, source="repo")
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
    # Rule rows this engine can't read, kept verbatim across writes (see blocking_rules).
    unsupported_rules: list = field(default_factory=list)
    # Top-level keys this engine doesn't know (a 0.4.7 store's "remembered", say), kept as they are.
    extra: dict = field(default_factory=dict)
    # The capability groups' baselines (brindle.permission_groups); none: ``unmatched`` for every group.
    groups: dict[str, dict] = field(default_factory=dict)
    unsupported_version: bool = False   # written by a newer engine: read fail-closed, never rewritten

    @property
    def blocking_rules(self) -> list[Rule]:
        """Policy this engine can't read can't silently defer to an allow. An
        unreadable rule row may narrow its rule in a way that can't be
        evaluated: it denies its whole kind until a capable engine reads it
        (an allow it can't read just doesn't apply); a newer store denies
        every kind. Runtime only: never saved in place of the rows."""
        kinds = set(KINDS) if self.unsupported_version else set()
        for row in self.unsupported_rules:
            kind = row.get("kind") if isinstance(row, dict) else None
            if isinstance(row, dict) and row.get("decision") == "allow" and kind in KINDS:
                continue
            kinds.update((kind,) if kind in KINDS else KINDS)
        return [Rule(kind, "*", "glob", "deny", "preset", id=f"d-store-unsupported-{kind}")
                for kind in KINDS if kind in kinds]

    def to_json(self) -> str:
        if self.unsupported_version:
            raise ValueError("permissions.json was written by a newer brindle; edit it with that version")
        data: dict = {**self.extra, "version": STORE_VERSION, "preset": self.preset, "unmatched": self.unmatched,
                      "rules": [*(r.to_dict() for r in self.rules), *self.unsupported_rules],
                      "approvals": self.approvals}
        if self.saved:
            data["saved"] = [s.to_dict() for s in self.saved]
        if self.prompts:
            data["prompts"] = self.prompts
        for name in ("groups", "agy_managed", "agy_created", "agy_repos", "codex_hook"):
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
    and name are recorded, and its capability group baselines."""
    from brindle import permission_groups

    kept = [r for r in store.rules if r.source != "preset"]
    ids = {r.id for r in kept}    # a seeded rule the person edited keeps their version
    store.rules = [*(r for r in preset_rules(name) if r.id not in ids), *kept]
    store.preset, store.unmatched = name, preset_unmatched(name)
    store.groups = permission_groups.preset_groups(name)
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


# The 0.4.7 engine's permission editor presets, by the rule each seeded per
# kind (a "*" glob rule scoped to one repo), and the preset each becomes.
EDITOR_PRESETS: dict[str, tuple[dict[str, str], str]] = {
    "strict": ({kind: "ask" for kind in KINDS}, "secure"),
    "balanced": ({**{kind: "ask" for kind in KINDS}, "read": "allow", "write": "allow", "edit": "allow"},
                 "standard"),
    "lenient": ({**{kind: "allow" for kind in KINDS}, "mcp": "ask", "other": "ask"}, "permissive"),
}
V2_PRESET = "autonomous"    # a 0.4.7 store whose editor chose no preset
# A 0.4.7 remembered push's scope level -> (saved action level, scope value).
V2_PUSH_LEVELS = {"branch": ("destination", "general"), "remote": ("remote", "remote"),
                  "repo": ("repository", "repository"), "all": ("all", "all")}
STORE_KEYS = ("version", "preset", "unmatched", "rules", "approvals", "saved", "prompts", "agy_managed",
              "agy_created", "agy_repos", "codex_hook", "groups")


def _editor_preset(rules: list[Rule]) -> tuple[str | None, set[str]]:
    """The preset a 0.4.7 store's editor chose, as one of PRESETS (the
    strictest, when repos chose differently; None for none), and the ids of
    the rules it seeded."""
    by_repo: dict[str, dict[str, Rule]] = {}
    for r in rules:
        if r.repo_root and r.match == "*" and r.match_type == "glob" and not r.exclusions and r.expires is None:
            by_repo.setdefault(r.repo_root, {})[r.kind] = r
    chosen, seeded = [], set()
    for kinds in by_repo.values():
        for shape, preset in EDITOR_PRESETS.values():
            if {k: r.decision for k, r in kinds.items()} == shape:
                chosen.append(preset)
                seeded.update(r.id for r in kinds.values())
    order = ("secure", "standard", "permissive")
    return (min(chosen, key=order.index) if chosen else None), seeded


def _v2_saved(record: dict, now: float) -> SavedAction | None:
    """A 0.4.7 remembered permission (a lifted push, or a .env read) as a
    saved allow with the same reach, expiry and remaining uses, bound to
    what it was (the approving owner, the repo's git directory, a push's
    remote URL); None for one that has expired, is used up, or that this
    engine can't express."""
    scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
    life = record.get("lifetime") or ("time" if "expires_at" in record else "forever")
    expires = _number(record.get("expires_at"))
    created = _number(record.get("created")) or now
    repo = record.get("repo_root") if isinstance(record.get("repo_root"), str) else None
    if life not in LIVES or life == "time" and (expires is None or expires <= now):
        return None
    uses = None
    if "max_uses" in record:
        budget, spent = record.get("max_uses"), record.get("uses_spent", 0)
        if type(budget) is not int or type(spent) is not int or spent >= budget:
            return None
        uses = budget - spent
    if not isinstance(record.get("owner"), str):
        return None
    binding = {"owner": record["owner"], **{k: scope[k] for k in ("git_dir", "url") if isinstance(scope.get(k), str)}}
    session = record.get("session", {}).get("id") if isinstance(record.get("session"), dict) else None
    if life == "session" and not isinstance(session, str):
        return None
    if record.get("action") == "git-push":
        level, value = V2_PUSH_LEVELS.get(record.get("scope_level") or "branch", (None, None))
        remote, ref = scope.get("remote"), scope.get("ref")
        if level is None or level in ("destination", "remote") and not (isinstance(remote, str) and "url" in binding):
            return None
        if level != "all" and "git_dir" not in binding:
            return None
        if level == "destination":
            if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
                return None
            pattern = f"*{PUSH_ARROW}{ref.removeprefix('refs/heads/')}{PUSH_AT}{remote}"
        else:
            pattern = f"*{PUSH_ARROW}*{PUSH_AT}{remote if level == 'remote' else '*'}"
        kind = "push"
    elif record.get("action") == "env" and isinstance(scope.get("path"), str):
        level, value, kind = None, "exact", "read"
        path = scope["path"]
        pattern = (os.path.relpath(path, repo) if repo and os.path.isabs(path) and _inside(path, repo)
                   else path)
    else:
        return None
    id_ = record.get("id") if isinstance(record.get("id"), str) and record.get("id") else "s-" + secrets.token_hex(6)
    return SavedAction(id_, kind, pattern, "allow", value, level, {}, life,
                       (expires - created) / 60 if life == "time" else None, expires if life == "time" else None,
                       session, None if level == "all" else repo, created, "saved in brindle 0.4.7", binding, uses)


def _migrate(store: Store, data: dict) -> None:
    """Seed a store from before presets. Version 1 (built-in rules outside
    the store, the person's command allows written with source "user")
    amounted to MIGRATED_PRESET. Version 2 (brindle 0.4.7): its editor's
    preset becomes ours (EDITOR_PRESETS; V2_PRESET when it chose none), its
    other rules stay the person's and its remembered permissions become
    saved actions. Nothing it held is deleted: its rows stay in the file
    (``v2_rules``, and ``remembered`` as it was)."""
    name = MIGRATED_PRESET
    if data.get("version") == 2:
        chosen, seeded = _editor_preset(store.rules)
        name = chosen or V2_PRESET
        store.rules = [r for r in store.rules if r.id not in seeded]
        now = time.time()
        rows = data.get("remembered") if isinstance(data.get("remembered"), list) else []
        known = {s.id for s in store.saved}
        store.saved += [s for s in (_v2_saved(r, now) for r in rows if isinstance(r, dict))
                        if s and s.id not in known]
        store.extra.setdefault("v2_rules", data.get("rules") or [])
    seed = {(r.kind, r.match, r.match_type, r.decision) for r in preset_rules(name)}
    store.rules = [r for r in store.rules if (r.kind, r.match, r.match_type, r.decision) not in seed]
    seed_preset(store, name)


def load_store() -> Store:
    """The person's store. A new one is seeded from DEFAULT_PRESET; one from
    before presets is migrated (_migrate), keeping its user and learned
    rules, approvals and the provider records. One a newer engine wrote is
    read fail-closed (Store.blocking_rules) and never rewritten."""
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
    version = data.get("version", 1)
    if type(version) is not int or not 1 <= version <= STORE_VERSION:
        return Store(unsupported_version=True, unmatched="deny")
    legacy = "preset" not in data
    rules, unsupported = [], []
    for row in data.get("rules") or []:
        rule = Rule.from_dict(row)
        if rule is None:
            unsupported.append(row)
        elif rule.source in STORE_SOURCES and not (legacy and rule.source == "preset"):
            rules.append(rule)
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
    from brindle import permission_groups

    groups = permission_groups.normalized(data["groups"], unmatched, preset) if data.get("groups") else {}
    store = Store(rules, approvals, preset, unmatched, saved, prompts, managed, strings(data.get("agy_created")),
                  repos, codex, unsupported, {k: v for k, v in data.items() if k not in STORE_KEYS}, groups)
    if legacy:
        _migrate(store, data)
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
        rule = replace(rule, created=old.created or rule.created, repo_root=old.repo_root)   # never broadened
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
    """What no rule covers does: every capability group's baseline too
    (their subcategories' own answers stay)."""
    if value not in UNMATCHED:
        raise ValueError(f"unmatched must be one of {', '.join(UNMATCHED)}")
    with editing() as store:
        store.unmatched = value
        for entry in store.groups.values():
            entry["baseline"] = value


def set_groups(changes: dict) -> Store:
    """Change capability group baselines and subcategory answers
    (permission_groups.merged; ValueError for what it doesn't know)."""
    from brindle import permission_groups

    with editing() as store:
        store.groups = permission_groups.merged(effective_groups(store), changes)
    return store


def effective_groups(store: Store) -> dict[str, dict]:
    """The store's capability group table, its unmatched setting for every group when it has none."""
    from brindle import permission_groups

    return store.groups or permission_groups.table(store.unmatched)


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


def spend_saved_use(id_: str) -> bool:
    """Spend one use of saved action ``id_``'s budget; whether one was left."""
    with editing() as store:
        action = store.saved_action(id_)
        if action is None or action.uses_left is None or action.uses_left <= 0:
            return action is not None and action.uses_left is None
        action.uses_left -= 1
    return True


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
        if rule.decision == "ask" or rule.exclusions or rule.expired() or rule.repo_root:
            continue  # only the hook can ask, weigh an exception, or tell one repo from another
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
