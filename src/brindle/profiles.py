"""Agent profiles: markdown files with a small frontmatter header.

    ---
    name: developer
    description: Implements a well-scoped coding task
    provider: claude
    ---
    You are a developer agent...

Lookup order: ``<repo>/.brindle/agents/``, ``~/.brindle/agents/``, the org's
library (brindle Team, ``brindle.pro.org_profiles``), then the built-in
profiles shipped with brindle. An org profile (or rule pack) its admins
*pinned* is looked up first: a repo or user file of that name is ignored, with
a warning.

A profile can build on another with ``extends: <profile>`` (one parent, found
by the same lookup order): the child's frontmatter fields override the
parent's, its ``rules`` packs add to the parent's, and its prompt is appended
to the parent's. ``rules: [pack, ...]`` names rule packs (``brindle.rule_packs``):
markdown files under ``.brindle/rules/``, ``~/.brindle/rules/`` or brindle's
built-ins whose text joins the agent's prompt and whose mechanical rules
(``deny_deps``, ``require_tests_for``, ``deny_patterns``) are checked against
the branch before it is reviewed or merged (``brindle.rule_checks``).

``write_scope``, ``read_scope`` and ``env_allow`` (lists of globs) are brindle
Pro guardrails: see ``brindle.guardrails`` for what enforces them and how far.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from brindle.config import CONFIG_DIR, brindle_home, user_profiles_dir


log = logging.getLogger(__name__)


class ProfileError(ValueError):
    """A profile file that can't be used as written: an ``extends`` cycle, or
    a native profile naming a Claude subscription token."""


SUBSCRIPTION_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"


def subscription_token_problem(profile: "Profile") -> str | None:
    """Why a native profile may not use ``CLAUDE_CODE_OAUTH_TOKEN`` (Anthropic's
    terms bar third parties from routing Claude subscription credentials), or
    None. Names the variable only, never a value."""
    if profile.api_key_env == SUBSCRIPTION_TOKEN or (
            profile.provider == "native" and SUBSCRIPTION_TOKEN in (profile.env or {})):
        return (f"profile {profile.name!r} uses {SUBSCRIPTION_TOKEN}, a Claude subscription token, "
                "which brindle's native provider may not send: use an API key "
                "(set api_key_env to its variable)")
    return None


MAX_EXTENDS_DEPTH = 10


@dataclass
class Profile:
    name: str
    description: str
    provider: str
    prompt: str
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    # Claude Code only; all off by default (see README, "Cheap workers").
    strict_mcp: bool = False                 # --strict-mcp-config: only brindle's MCP server
    setting_sources: list[str] | None = None  # --setting-sources, e.g. project,local
    effort: str | None = None                # --effort low|medium|high|xhigh|max
    headless: bool = False                   # run with `claude -p`, turn by turn
    tool_search: bool | None = None          # Claude Code's deferred tool loading (None: off for workers)
    # The native provider (brindle's own loop; see brindle.native): where the model is.
    api: str | None = None                   # openai (chat completions) | anthropic (messages)
    base_url: str | None = None              # e.g. http://localhost:11434/v1
    api_key_env: str | None = None           # name of the variable holding the key, if one is needed
    context_tokens: int | None = None        # the model's window, less room for its reply
    # The model runs on this machine or the private network: air-gap mode (brindle.airgap)
    # lets this profile run even when its endpoint can't be checked (e.g. a hostname).
    local: bool = False
    # Extra environment for the agent's process, from ``env.NAME: value`` lines.
    env: dict[str, str] = field(default_factory=dict)
    # --add-dir. Full tool access, not read access: edits and Bash reach these too,
    # and Claude Code loads any CLAUDE.md it finds in them. Added to the repo's own
    # add_dirs rather than replacing it; see load_profile.
    add_dirs: list[str] | None = None
    # Additional profile-local deny rules (JSON array of "KIND MATCH_TYPE MATCH").
    permission_denies: list[str] = field(default_factory=list)
    permission_denies_errors: list[str] = field(default_factory=list)
    # ``extends: <profile>``: the parent this profile was built on, if any
    # (already merged in; kept so `brindle profile show` can say so).
    extends: str | None = None
    # ``rules: [pack, ...]``: rule packs, the parent's first (see load_rule_packs).
    rules: list[str] = field(default_factory=list)
    # Guardrails (brindle Pro, feature ``guardrails``; see brindle.guardrails).
    # Globs relative to the worktree: the only files the worker may change /
    # read, and the only environment variables (besides brindle's own and the
    # provider's sign-in) its process starts with. None: no limit. Without
    # the feature they are ignored, with a warning (guardrails.effective).
    write_scope: list[str] | None = None
    read_scope: list[str] | None = None
    env_allow: list[str] | None = None
    # How the agent signs in: ``auto`` (a key from the environment or `brindle keys`
    # wins, else the CLI's own login), ``subscription`` (no key reaches the pane,
    # so the login is used) or ``api_key`` (refuse to start without a key).
    auth: str = "auto"


AUTH_CHOICES = ("auto", "subscription", "api_key")


@dataclass
class RulePack:
    """A rule pack: prompt text for the agent, plus mechanical rules brindle
    checks against the branch diff (see ``brindle.rule_checks``).

        ---
        name: security/backend
        description: No shell-outs, no evals, no secrets in code
        deny_deps: [pickle]              # imports/dependencies that may not be added
        require_tests_for: [src/**/*.py] # changing these needs a test file in the diff
        deny_patterns:                   # regexes no added line may match
          - shell=True
          - eval\\(
        ---
        Never shell out with shell=True...
    """
    name: str
    description: str
    prompt: str
    deny_deps: list[str] = field(default_factory=list)
    require_tests_for: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)
    source: str = ""   # where it was read from, for `brindle profile show`

    @property
    def mechanical(self) -> bool:
        return bool(self.deny_deps or self.require_tests_for or self.deny_patterns)


_COMMENT = re.compile(r"(?:^|\s)#.*$")


def _value(raw: str) -> str:
    """A frontmatter value without its trailing ``# comment``. As in YAML, a
    ``#`` only starts a comment at the start or after whitespace, and a quoted
    value is taken as is."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end > 0:
            return raw[1:end]
    return _COMMENT.sub("", raw).strip()


def _flag(value: str | None) -> bool:
    return (value or "").lower() in ("true", "yes", "on", "1")


def _list(value: str | None) -> list[str] | None:
    """A comma-separated list; YAML's ``[a, b]`` form works too, and so does
    a block list (one ``- item`` per line, which ``_frontmatter`` joins with
    newlines), where an item may itself contain a comma."""
    value = (value or "").strip()
    if "\n" in value:
        items = [t.strip() for t in value.split("\n") if t.strip()]
        return items or None
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    items = [t.strip().strip("'\"") for t in value.split(",") if t.strip()]
    return items or None


def _json_list(value: str | None) -> tuple[list[str], list[str]]:
    if not value:
        return [], []
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return [], [value]
    if not isinstance(parsed, list):
        return [], [value]
    valid = [item for item in parsed if isinstance(item, str)]
    invalid = [repr(item) for item in parsed if not isinstance(item, str)]
    return valid, invalid


def _int(value: str | None) -> int | None:
    value = (value or "").strip().replace("_", "").replace(",", "")
    if value.lower().endswith("k") and value[:-1].isdigit():
        return int(value[:-1]) * 1000
    return int(value) if value.isdigit() else None


def _bool(value: str) -> bool:
    return value.strip().lower() in ('true', 'yes', 'on', '1')


_BLOCK_ITEM = re.compile(r"^\s+-\s*(.*)$")


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """The ``key: value`` header and the body. A key with no value followed
    by indented ``- item`` lines is a block list, stored newline-joined (see
    ``_list``). Whole-line and trailing ``# comments`` are dropped."""
    meta: dict[str, str] = {}
    body = text
    if not text.startswith("---"):
        return meta, body
    # the closing fence is a line of its own: a value may contain "---"
    m = re.match(r"---[^\n]*\n(.*?)^---[ \t]*$(.*)", text, re.S | re.M)
    if m is None:
        raise ProfileError("frontmatter opened with '---' is never closed with another '---' line")
    header, body = m.groups()
    last_key: str | None = None
    for line in header.strip("\n").splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        item = _BLOCK_ITEM.match(line)
        if item and last_key is not None and (meta[last_key] == "" or "\n" in meta[last_key]):
            value = _value(item.group(1))
            if value:
                meta[last_key] = f"{meta[last_key]}\n{value}" if meta[last_key] else f"\n{value}"
            continue
        key, _, value = line.partition(":")
        if key.strip() and not key.startswith((" ", "\t")):
            last_key = key.strip()
            meta[last_key] = _value(value)
    return meta, body


def _parse(text: str, fallback_name: str) -> Profile:
    meta, body = _frontmatter(text)
    return _build(meta, body, fallback_name)


def _build(meta: dict[str, str], body: str, fallback_name: str) -> Profile:
    permission_denies, permission_denies_errors = _json_list(meta.get("permission_denies"))
    profile = Profile(
        name=meta.get("name", fallback_name),
        extends=meta.get("extends") or None,
        rules=_list(meta.get("rules")) or [],
        description=meta.get("description", ""),
        provider=meta.get("provider", "claude"),
        prompt=body.strip(),
        model=meta.get("model") or None,
        permission_mode=meta.get("permission_mode") or None,
        allowed_tools=_list(meta.get("allowed_tools")),
        strict_mcp=_flag(meta.get("strict_mcp")),
        setting_sources=_list(meta.get("setting_sources")),
        add_dirs=_list(meta.get("add_dirs")),
        permission_denies=permission_denies,
        permission_denies_errors=permission_denies_errors,
        effort=meta.get("effort") or None,
        tool_search=_bool(meta.get('tool_search')) if meta.get('tool_search') else None,
        headless=_flag(meta.get("headless")),
        api=meta.get("api") or None,
        base_url=meta.get("base_url") or None,
        api_key_env=meta.get("api_key_env") or None,
        context_tokens=_int(meta.get("context_tokens")),
        local=_flag(meta.get("local")),
        env={k[4:]: v for k, v in meta.items() if k.startswith("env.") and k[4:]},
        write_scope=_list(meta.get("write_scope")),
        read_scope=_list(meta.get("read_scope")),
        env_allow=_list(meta.get("env_allow")),
        auth=(meta.get("auth") or "auto").strip().lower(),
    )
    if profile.auth not in AUTH_CHOICES:
        raise ProfileError(f"profile {profile.name!r} has auth: {profile.auth!r}; "
                           f"choose from {', '.join(AUTH_CHOICES)}")
    problem = subscription_token_problem(profile)
    if problem:
        raise ProfileError(problem)
    return profile


def _search_dirs(repo_root: str | None) -> list[Path]:
    dirs = []
    if repo_root:
        dirs.append(Path(repo_root) / CONFIG_DIR / "agents")
    dirs.append(user_profiles_dir())
    return dirs


def _with_repo_add_dirs(profile: Profile, repo_root: str | None) -> Profile:
    """Union the repo's ``add_dirs`` with the profile's, resolved and checked.

    The repo config is the primary home: a shared build cache or a folder of
    profiles beside the repo is a property of the repository, so every profile
    launched in it needs the same list and copies would drift. A profile adds to
    that list for a role that needs more, and never removes from it, which keeps
    the result easy to reason about.

    Entries are resolved against the repo root rather than passed through, because
    a worktree is the process's working directory and a relative path would
    otherwise mean ``~/.brindle/worktrees/<repo>/<branch>/<path>``. A leading ``~``
    is expanded first, since nothing downstream runs a shell that would.
    Checking that they exist is left to launch (see ``missing_add_dirs``): this
    runs every time a profile is loaded, several times per launch and on every
    resume, and one launch should say so once.
    """
    if repo_root is None:
        return profile

    from brindle.config import load_repo_config

    root = Path(repo_root)
    merged: list[str] = []
    for entry in [*load_repo_config(repo_root).add_dirs, *(profile.add_dirs or [])]:
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            path = Path(entry).expanduser()
        except RuntimeError:
            # An unknown user (a typo like ~typo/cache) or no home directory.
            # Kept as written so missing_add_dirs reports it at launch; raising
            # here would fail every load_profile, and with it every launch.
            if entry not in merged:
                merged.append(entry)
            continue
        resolved = str(path if path.is_absolute() else (root / path).resolve())
        if resolved not in merged:
            merged.append(resolved)
    return replace(profile, add_dirs=merged or None)


def missing_add_dirs(profile: Profile) -> list[str]:
    """The profile's ``add_dirs`` that do not exist. Claude Code ignores an
    ``--add-dir`` that does not exist, so without this the failure would be the
    one the field exists to prevent, silently."""
    return [d for d in profile.add_dirs or [] if not Path(d).is_dir()]


def _org_item(kind: str, name: str):
    """The org library's profile or rule pack ``name`` (brindle Team, feature
    ``org_profiles``; see ``brindle.pro.org_profiles``), or None: no
    entitlement, no usable library, or no such item."""
    try:
        from brindle.pro import org_profiles

        return org_profiles.items(kind).get(name)
    except Exception:  # noqa: BLE001 - the org tier is optional; never break local lookups
        return None


def _org_names(kind: str) -> set[str]:
    try:
        from brindle.pro import org_profiles

        return set(org_profiles.items(kind))
    except Exception:  # noqa: BLE001
        return set()


_shadow_warned: set[tuple[str, str]] = set()


def _pinned_over(kind: str, name: str, found: list[Path]) -> None:
    """Warn (once) that the repo or user file(s) ``found`` for a pinned org
    item are ignored."""
    for f in found:
        if (str(f), name) not in _shadow_warned:
            _shadow_warned.add((str(f), name))
            log.warning("brindle: %s is ignored: the %s %r is pinned by your org, so a repo or "
                        "user file can't replace it", f, kind, name)


def _read(name: str, repo_root: str | None) -> tuple[str, str] | None:
    """The text of ``<name>.md`` and where it came from, by lookup order:
    the repo, the user, the org's library, the built-ins. A pinned org
    profile comes before the repo and user files."""
    org = _org_item("profile", name)
    local = [d / f"{name}.md" for d in _search_dirs(repo_root)]
    if org is not None and org.pinned:
        _pinned_over("agent profile", name, [f for f in local if f.is_file()])
        return org.text, f"org profile {name}"
    for f in local:
        if f.is_file():
            return f.read_text(encoding="utf-8"), str(f)
    if org is not None:
        return org.text, f"org profile {name}"
    builtin = resources.files("brindle.builtin_agents").joinpath(f"{name}.md")
    if builtin.is_file():
        return builtin.read_text(encoding="utf-8"), f"built-in profile {name}"
    return None


def profile_source(name: str, repo_root: str | None = None) -> str | None:
    """Where ``name`` is read from: a path, or "built-in profile <name>"."""
    found = _read(name, repo_root)
    return found[1] if found else None


def _merge_rules(parent: str | None, child: str | None) -> str:
    """The parent's packs, then the child's new ones, in block-list form."""
    merged: list[str] = []
    for pack in [*(_list(parent) or []), *(_list(child) or [])]:
        if pack not in merged:
            merged.append(pack)
    return "\n" + "\n".join(merged) if merged else ""


def _resolve(name: str, repo_root: str | None, chain: tuple[str, ...] = ()) -> tuple[dict[str, str], str]:
    """``name``'s frontmatter and body with its ``extends`` chain folded in:
    the child's fields override the parent's, ``rules`` packs accumulate, and
    the child's prompt follows the parent's. ``chain`` is the profiles that
    led here, for cycle detection."""
    found = _read(name, repo_root)
    if found is None:
        if chain:
            raise KeyError(f"profile {chain[-1]!r} extends {name!r}, and there is no profile named {name!r}")
        raise KeyError(f"no agent profile named {name!r}")
    meta, body = _frontmatter(found[0])
    parent = meta.pop("extends", "")
    if not parent:
        return meta, body
    if parent in (*chain, name):
        cycle = " -> ".join((*chain, name, parent))
        raise ProfileError(f"profile {name!r} extends itself: {cycle}")
    if len(chain) >= MAX_EXTENDS_DEPTH:
        raise ProfileError(
            f"profile {chain[0]!r} extends more than {MAX_EXTENDS_DEPTH} profiles deep: "
            + " -> ".join((*chain, name, parent))
        )
    parent_meta, parent_body = _resolve(parent, repo_root, (*chain, name))
    merged = {**parent_meta, **meta, "extends": parent}
    rules = _merge_rules(parent_meta.get("rules"), meta.get("rules"))
    if rules:
        merged["rules"] = rules
    else:
        merged.pop("rules", None)
    return merged, "\n\n".join(p for p in (parent_body.strip(), body.strip()) if p)


def _load(name: str, repo_root: str | None) -> Profile:
    meta, body = _resolve(name, repo_root)
    return replace(_build(meta, body, name), name=name)


def load_profile(name: str, repo_root: str | None = None) -> Profile:
    """The profile in ``<name>.md``, with its ``extends`` parent folded in.
    Its ``name`` is always ``name``, even if the file's frontmatter says
    otherwise (a copied profile whose name wasn't changed): agents record it,
    and a resume or relaunch loads the profile again by that name, so it must
    find this same file, permissions and all. Raises KeyError when there is
    no such profile (or no such parent) and ProfileError on an extends cycle."""
    return _with_repo_add_dirs(_load(name, repo_root), repo_root)


# -- rule packs ---------------------------------------------------------------


_PACK_NAME = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")


def _rule_dirs(repo_root: str | None) -> list[Path]:
    dirs = []
    if repo_root:
        dirs.append(Path(repo_root) / CONFIG_DIR / "rules")
    dirs.append(brindle_home() / "rules")
    return dirs


def _read_pack(name: str, repo_root: str | None) -> tuple[str, str] | None:
    parts = name.split("/")
    org = _org_item("pack", name)
    local = [d.joinpath(*parts[:-1], f"{parts[-1]}.md") for d in _rule_dirs(repo_root)]
    if org is not None and org.pinned:
        _pinned_over("rule pack", name, [f for f in local if f.is_file()])
        return org.text, f"org rule pack {name}"
    for f in local:
        if f.is_file():
            return f.read_text(encoding="utf-8"), str(f)
    if org is not None:
        return org.text, f"org rule pack {name}"
    builtin = resources.files("brindle.builtin_rules").joinpath(*parts[:-1], f"{parts[-1]}.md")
    if builtin.is_file():
        return builtin.read_text(encoding="utf-8"), f"built-in rule pack {name}"
    return None


def load_rule_pack(name: str, repo_root: str | None = None) -> RulePack:
    """The rule pack ``name`` (``security/backend`` is ``security/backend.md``)
    from ``.brindle/rules/``, ``~/.brindle/rules/`` or brindle's built-ins.
    Raises KeyError when there is none, ProfileError on a name that isn't a
    plain relative path."""
    if not _PACK_NAME.match(name) or ".." in name.split("/"):
        raise ProfileError(f"rule pack name {name!r} must be like security/backend")
    found = _read_pack(name, repo_root)
    if found is None:
        raise KeyError(f"no rule pack named {name!r}")
    text, source = found
    meta, body = _frontmatter(text)
    return RulePack(
        name=name,
        description=meta.get("description", ""),
        prompt=body.strip(),
        deny_deps=_list(meta.get("deny_deps")) or [],
        require_tests_for=_list(meta.get("require_tests_for")) or [],
        deny_patterns=_list(meta.get("deny_patterns")) or [],
        source=source,
    )


LEARNED_PACK = "learned"   # .brindle/rules/learned.md, written by `brindle rules accept`


def learned_pack_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / CONFIG_DIR / "rules" / f"{LEARNED_PACK}.md"


def load_rule_packs(profile: Profile, repo_root: str | None = None) -> list[RulePack]:
    """The packs ``profile.rules`` names, in order, then the repo's learned
    rules (``.brindle/rules/learned.md``, see ``brindle.learned_rules``) when
    it has any: those apply to every profile in the repo. A missing pack
    raises KeyError: a rule that silently doesn't apply is worse than a
    launch that says which file to write."""
    try:
        from brindle.pro import rollout

        required = rollout.required_packs(repo_root)
    except ImportError:  # the enterprise tier is optional
        required = []
    # A required name always comes from the org library, even when the profile
    # lists it too: a repo or user file of that name must not stand in for it.
    packs = [load_rule_pack(name, repo_root) for name in profile.rules if name not in required]
    if repo_root and LEARNED_PACK not in profile.rules and learned_pack_path(repo_root).is_file():
        packs.append(load_rule_pack(LEARNED_PACK, repo_root))
    packs.extend(_required_packs(required))
    return packs


def _required_packs(names: list[str]) -> list[RulePack]:
    """The org's required rule packs (Enterprise ``managed_rollout``), read
    from the org library only, for every profile. A required pack the library
    doesn't have raises KeyError, like any missing pack."""
    out = []
    for name in names:
        org = _org_item("pack", name)
        if org is None:
            raise KeyError(f"no rule pack named {name!r} in the org library (required by your org)")
        meta, body = _frontmatter(org.text)
        out.append(RulePack(
            name=name, description=meta.get("description", ""), prompt=body.strip(),
            deny_deps=_list(meta.get("deny_deps")) or [],
            require_tests_for=_list(meta.get("require_tests_for")) or [],
            deny_patterns=_list(meta.get("deny_patterns")) or [],
            source=f"org rule pack {name}"))
    return out


def list_rule_packs(repo_root: str | None = None) -> list[str]:
    """Every rule pack name that would load, the nearest definition winning."""
    names: set[str] = set()
    builtin = resources.files("brindle.builtin_rules")

    def walk(entry, prefix: str) -> None:
        for child in entry.iterdir():
            if child.is_dir():
                walk(child, f"{prefix}{child.name}/")
            elif child.name.endswith(".md"):
                names.add(f"{prefix}{child.name[:-3]}")

    walk(builtin, "")
    names.update(_org_names("pack"))
    for d in _rule_dirs(repo_root):
        if d.is_dir():
            for f in d.rglob("*.md"):
                names.add(f.relative_to(d).with_suffix("").as_posix())
    return sorted(names)


def _mechanical_lines(pack: RulePack) -> list[str]:
    lines = []
    if pack.deny_deps:
        lines.append("no new import of, or dependency on: " + ", ".join(pack.deny_deps))
    if pack.require_tests_for:
        lines.append("a change to " + ", ".join(f"`{g}`" for g in pack.require_tests_for)
                     + " must come with a test file in the same diff")
    if pack.deny_patterns:
        lines.append("no added line may match: " + ", ".join(f"/{p}/" for p in pack.deny_patterns))
    return lines


def rules_prompt(packs: list[RulePack]) -> str:
    """The packs' text for an agent's prompt: each pack's prose, then the
    mechanical rules spelled out, since those are what brindle checks."""
    if not packs:
        return ""
    parts = ["Rules (your profile's rule packs). brindle checks the branch against the "
             "mechanical ones before it is reviewed or merged, and a violation comes back "
             "to you like a failed check:"]
    for pack in packs:
        title = f"## Rules: {pack.name}" + (f": {pack.description}" if pack.description else "")
        body = [title]
        if pack.prompt:
            body.append(pack.prompt)
        mechanical = _mechanical_lines(pack)
        if mechanical:
            body.append("Checked mechanically:\n" + "\n".join(f"- {m}" for m in mechanical))
        parts.append("\n".join(body))
    return "\n\n".join(parts)


def profile_rules_for(name: str, repo_root: str | None = None):
    """Return profile denies, treating a deleted profile as no override."""
    from brindle import permissions

    try:
        return permissions.profile_rules(load_profile(name, repo_root))
    except KeyError:
        return []


def profile_names(repo_root: str | None = None) -> list[str]:
    """Every profile file name (without ``.md``) in the lookup order's
    directories and the built-ins, each once."""
    names: set[str] = set()
    for entry in resources.files("brindle.builtin_agents").iterdir():
        if entry.name.endswith(".md"):
            names.add(entry.name[:-3])
    names.update(_org_names("profile"))
    for d in _search_dirs(repo_root):
        if d.is_dir():
            names.update(f.stem for f in d.glob("*.md"))
    return sorted(names)


def list_profiles(repo_root: str | None = None) -> list[Profile]:
    """Every profile, resolved (``extends`` folded in) when it can be: one
    that can't (a cycle, a missing parent) is listed as written, so one bad
    file doesn't hide the rest; ``brindle profile lint`` reports it."""
    seen: dict[str, Profile] = {}
    for name in profile_names(repo_root):
        try:
            p = _load(name, repo_root)
        except (KeyError, ProfileError):
            found = _read(name, repo_root)
            if found is None:
                continue
            try:
                p = replace(_parse(found[0], name), name=name)
            except ProfileError:
                continue   # unreadable even as written; `brindle profile lint` reports it
        seen[p.name] = p
    return sorted(seen.values(), key=lambda p: p.name)
