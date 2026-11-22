#!/usr/bin/env python3
"""Check markdown and HTML docs against brindle's real CLI and config schema.

Extracts every ``brindle <subcommand>`` invocation and every
``.brindle/config.json`` key from the given markdown or HTML files, checks them
against the Click/Typer command tree imported from ``src/brindle/cli.py`` and
the config schema imported from ``src/brindle/config.py`` (and sibling modules),
and exits 1 listing each unknown command or key as ``file:line: ...``.
"""
from __future__ import annotations

import argparse
import dataclasses
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

# RepoConfig fields that hold computed provenance/diagnostics rather than config keys.
_INTERNAL_CONFIG_FIELDS = frozenset({"permission_policy_source", "routing_sources"})

# User-level keys read directly via user_settings().get(...) without a module constant.
_EXTRA_USER_CONFIG_KEYS = frozenset({
    "claude_permission_mode",
    "machine_max_workers",
    "opencode_permission_mode",
    "quota_switch",
    "retention",
    "visuals",
    "visuals_enabled",
})

_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
_SHELL_LANGS = frozenset({"", "sh", "bash", "shell", "zsh", "console", "text", "txt"})
_JSON_LANGS = frozenset({"json", "jsonc"})

_CONFIG_JSON_RE = re.compile(r"(?:~/)?\.brindle/config(?:\.local)?\.json\b")
_ANY_JSON_FILE_RE = re.compile(r"(?:~/)?(?:[.\w-]+/)*[\w.-]+\.json\b", re.I)
_POLICY_REF_RE = re.compile(r"\borg\s+policy\b", re.I)
_FENCE_LINE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})")
_HEADING_LINE_RE = re.compile(r"^\s*(?:#{1,6}\s|<h[1-6]\b)", re.I)
_HTML_PRE_OPEN_RE = re.compile(r"<pre\b", re.I)
_HTML_PRE_CLOSE_RE = re.compile(r"</pre>", re.I)

_CMD_TOKEN_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")
_ALT_GROUP_RE = re.compile(r"^[a-z_][a-z0-9_-]*(?:[/|][a-z_][a-z0-9_-]*)+$")
_CONFIG_KEY_IDENT_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_INLINE_JSON_KEY_RE = re.compile(r'^"?([a-zA-Z_][a-zA-Z0-9_-]*)"?\s*:')
_STRICT_JSON_FRAGMENT_RE = re.compile(r'^"[a-zA-Z_][a-zA-Z0-9_-]*"\s*:')
_QUOTED_KEY_RE = re.compile(r'^"([a-zA-Z_][a-zA-Z0-9_-]*)"$')
_TABLE_KEY_HEADERS = frozenset({"key", "setting", "option", "config key"})

# Prose invocation patterns and English stop words that terminate a prose command.
_PROSE_BRINDLE_RE = re.compile(r"(?<![./\w-])brindle(?=\s)")
_PROSE_LEADIN_BEFORE_RE = re.compile(
    r"(?:^|[\s(\[,;—–-])"
    r"(?:run|running|use|using|try|trying|call|calling|invoke|invoking|"
    r"execute|executing|type|typing|via|command)\s+$",
    re.I,
)
_PROSE_CONJ_BEFORE_RE = re.compile(r"(?:(?:^|[\s,])(?:and|or|then)\s+|,\s*|/\s*|->\s*)$", re.I)
_PROSE_DETERMINER_BEFORE_RE = re.compile(
    r"(?:^|[\s(\[,;—–-])"
    r"(?:a|an|any|each|every|free|its|my|new|no|one|other|our|some|that|their|this|your)\s+$",
    re.I,
)
_PROSE_QUOTED_RE = re.compile(r"""(?<![\w=])(["'])brindle\s+([^"'\n]+?)\1""")
_PROSE_PROMPT_RE = re.compile(r"^\s*(?:\$|%)\s+brindle\s+(.+)$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_EMPHASIS_RE = re.compile(r"(?<!\w)\*\*?([^\s*][^*\n]*?)\*\*?(?!\w)")
_PROSE_CMD_SUFFIX_WORDS = frozenset({"command", "commands", "subcommand", "subcommands"})
_PROSE_STOP_WORDS = frozenset({
    "a", "about", "above", "across", "after", "again", "against", "all",
    "alone", "already", "also", "although", "always", "an", "and", "another",
    "any", "are", "around", "as", "at", "automatically", "be", "because",
    "been", "before", "being", "below", "beside", "between", "both", "briefly",
    "but", "by", "can", "command", "commands", "continuously", "could",
    "desktop", "did", "directly", "do", "does", "during", "each", "either",
    "enterprise", "every", "exactly", "first", "for", "frequently", "from",
    "had", "has", "have", "her", "here", "his", "however", "if", "in",
    "inside", "instead", "interactively", "into", "is", "it", "its", "itself",
    "just", "later", "like", "locally", "manually", "may", "might", "more",
    "most", "must", "my", "neither", "never", "no", "normally", "not", "now",
    "of", "on", "once", "only", "onto", "or", "other", "our", "outside",
    "over", "pro", "regularly", "remotely", "safely", "shall", "should",
    "since", "so", "some", "still", "subcommand", "subcommands", "such",
    "team", "than", "that", "the", "their", "then", "there", "these", "they",
    "this", "those", "though", "through", "to", "today", "too", "twice",
    "unattended", "under", "unless", "until", "was", "we", "were", "when",
    "where", "whether", "which", "while", "who", "will", "with", "within",
    "without", "work", "would", "yet", "you", "your",
})
# Bare `-s` / `--supervisor` accepts an optional value in brindle.cli._bare_supervisor_flag.
_OPTIONAL_VALUE_GLOBAL_OPTS = frozenset({"-s", "--supervisor"})


class Finding(NamedTuple):
    path: str
    line: int
    kind: str   # "command" | "config_key"
    value: str

    def format(self) -> str:
        label = "command" if self.kind == "command" else "config key"
        return f"{self.path}:{self.line}: unknown {label}: {self.value}"


class CLISchema(NamedTuple):
    tree: dict[str, dict | None]
    global_options: dict[str, int]   # option flag -> number of value args (0 for boolean flags)


def load_cli_schema() -> CLISchema:
    """Import ``brindle.cli.app`` and return its command tree and global option metadata."""
    import typer
    from brindle import cli

    root = typer.main.get_command(cli.app)

    def walk(cmd) -> dict[str, dict | None]:
        tree: dict[str, dict | None] = {}
        subcommands = getattr(cmd, "commands", None) or {}
        for name, sub in subcommands.items():
            nested = getattr(sub, "commands", None)
            tree[name] = walk(sub) if nested else None
        return tree

    global_options: dict[str, int] = {"-h": 0, "--help": 0}
    for param in getattr(root, "params", ()):
        opts = list(getattr(param, "opts", ())) + list(getattr(param, "secondary_opts", ()))
        if not opts:
            continue
        is_flag = bool(getattr(param, "is_flag", False))
        nargs = 0 if is_flag else max(1, int(getattr(param, "nargs", 1) or 1))
        for opt in opts:
            global_options[opt] = nargs

    return CLISchema(walk(root), global_options)


def load_cli_tree() -> dict[str, dict | None]:
    """Build the real CLI command tree by importing ``brindle.cli.app``."""
    return load_cli_schema().tree


def load_config_keys() -> frozenset[str]:
    """Return every valid key for ``.brindle/config.json`` (and user/local config)."""
    from brindle import (
        company_login,
        config,
        connections,
        debuglog,
        doc_templates,
        login_shell_env,
        providers,
        routing_goal,
        self_serve,
        worker_models,
    )
    from brindle.pro import audit_export

    keys = {
        f.name
        for f in dataclasses.fields(config.RepoConfig)
        if f.name not in _INTERNAL_CONFIG_FIELDS
    }
    keys.update({
        config.SUPERVISOR_KEY,
        config.POLICY_DEFAULT_KEY,
        company_login.ENV_KEY,
        connections.KEY,
        debuglog.SETTING,
        doc_templates.KEY,
        login_shell_env.SETTING,
        audit_export.SETTING,
        providers.APP_THEME_SETTING,
        routing_goal.SETTING,
        self_serve.LOCAL_KEY,
        *(f"routing_{tier}" for tier in worker_models.TIERS),
    })
    keys.update(_EXTRA_USER_CONFIG_KEYS)
    return frozenset(keys)


# -- CLI command extraction and validation ------------------------------------


def _strip_shell_comment(line: str) -> str:
    """Drop an unquoted ``# ...`` comment from a shell line."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx]
    return line


def _split_shell_segments(text: str) -> list[str]:
    """Split a shell snippet on command separators (``&&``, ``||``, ``;``, ``->``)."""
    protected = text.replace(r"\|", "\x00")
    parts = re.split(r"\s*(?:&&|\|\||;|->)\s*", protected)
    return [p.replace("\x00", "|").strip() for p in parts if p.strip()]


def _consume_global_options(
    tokens: list[str],
    tree: dict[str, dict | None],
    global_options: dict[str, int],
) -> list[str]:
    """Consume leading global Click options and their values from ``tokens``."""
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":
            return tokens[i + 1:]
        if not tok.startswith("-"):
            break
        if "=" in tok:
            opt_name = tok.split("=", 1)[0]
            if opt_name in global_options:
                i += 1
                continue
            return []
        if tok not in global_options:
            return []
        nargs = global_options[tok]
        i += 1
        if nargs == 0:
            continue
        # Handle bare `-s` / `--supervisor` followed directly by another option or real command.
        if tok in _OPTIONAL_VALUE_GLOBAL_OPTS:
            if i >= n or tokens[i].startswith("-") or tokens[i] in tree:
                continue
        i += nargs
    return tokens[i:]


def _extract_command_candidates(
    snippet: str,
    tree: dict[str, dict | None],
    global_options: dict[str, int],
) -> list[list[str]]:
    """Return candidate ``brindle`` subcommand paths found in ``snippet``.

    Handles single commands (``brindle agent kill``), global options before
    commands (``brindle -s codex start``), slash/pipe shorthands
    (``brindle keys set/unset/list``, ``brindle audit verify|export``), and
    spaced-slash alternatives (``brindle attach / cd / open [WS]``,
    ``brindle repo add PATH / rm ALIAS / ls``).
    """
    candidates: list[list[str]] = []
    for seg in _split_shell_segments(snippet):
        seg = re.sub(r"^(?:\$|%)\s+", "", seg).strip()
        if not seg:
            continue
        m = re.match(r"^brindle(?:\s+(.*))?$", seg)
        if not m or not m.group(1):
            continue
        rest = m.group(1).strip()
        if not rest:
            continue
        candidates.extend(_parse_after_brindle(rest, tree, global_options))
    return candidates


def _is_candidate_token(token: str) -> bool:
    return bool(_CMD_TOKEN_RE.match(token))


def _expand_alternatives(tokens: list[str], prefix: list[str]) -> list[list[str]]:
    """Expand inline ``a/b/c`` or ``a|b|c`` alternatives into individual paths."""
    if not tokens:
        return [prefix] if prefix else []
    first = tokens[0]
    if _ALT_GROUP_RE.match(first):
        alts = re.split(r"[/|]", first)
        return [[*prefix, a] for a in alts if _is_candidate_token(a)]
    if "|" in first:
        joined = " ".join(tokens)
        out: list[list[str]] = []
        for branch in joined.split("|"):
            parts = branch.strip().split()
            if parts and _is_candidate_token(parts[0]):
                out.append([*prefix, parts[0]])
        return out
    return [[*prefix, *tokens]]


def _parse_after_brindle(
    rest: str,
    tree: dict[str, dict | None],
    global_options: dict[str, int],
) -> list[list[str]]:
    """Parse everything after ``brindle `` into one or more token lists."""
    rest = rest.replace(r"\|", "|")
    if " / " in rest:
        branches = [b.strip() for b in rest.split(" / ") if b.strip()]
        if not branches:
            return []
        first_tokens = _consume_global_options(branches[0].split(), tree, global_options)
        if not first_tokens:
            return []
        if len(first_tokens) >= 2 and _is_candidate_token(first_tokens[0]) and (
            _is_candidate_token(first_tokens[1]) or _ALT_GROUP_RE.match(first_tokens[1])
        ):
            group = first_tokens[0]
            paths = _expand_alternatives(first_tokens[1:], [group])
            for br in branches[1:]:
                btoks = br.split()
                if btoks:
                    paths.extend(_expand_alternatives(btoks, [group]))
            return paths
        paths = []
        for br in branches:
            btoks = _consume_global_options(br.split(), tree, global_options)
            if btoks:
                paths.extend(_expand_alternatives(btoks, []))
        return paths

    tokens = _consume_global_options(rest.split(), tree, global_options)
    if not tokens:
        return []
    if _ALT_GROUP_RE.match(tokens[0]) or "|" in tokens[0]:
        return _expand_alternatives(tokens, [])
    if len(tokens) >= 2 and _is_candidate_token(tokens[0]) and (
        _ALT_GROUP_RE.match(tokens[1]) or "|" in tokens[1]
    ):
        return _expand_alternatives(tokens[1:], [tokens[0]])
    return [tokens]


def _validate_command_tokens(
    tokens: list[str],
    tree: dict[str, dict | None],
    *,
    prose: bool = False,
) -> str | None:
    """Validate a token list against ``tree``; return the unknown command string or None."""
    node: dict[str, dict | None] | None = tree
    matched: list[str] = []
    for tok in tokens:
        if node is None:
            return None
        clean = tok.rstrip(",.:;)")
        if not clean:
            return None
        if clean.startswith("-") or not _is_candidate_token(clean):
            return None
        if clean in node:
            matched.append(clean)
            node = node[clean]
            continue
        if prose and clean in _PROSE_STOP_WORDS:
            return None
        return " ".join(["brindle", *matched, clean])
    return None


def _has_command_suffix(tokens: list[str]) -> bool:
    """True when ``tokens`` look like ``<subcommand...> command|subcommand``."""
    for idx in range(1, min(len(tokens), 4)):
        if tokens[idx].rstrip(",.:;)").lower() in _PROSE_CMD_SUFFIX_WORDS:
            return all(_is_candidate_token(t) for t in tokens[:idx])
    return False


def _scan_prose_text(
    text: str,
    char_lines: list[int],
    tree: dict[str, dict | None],
    global_options: dict[str, int],
    record,
    *,
    prior_cmd_offsets: set[int] | None = None,
    prior_cmd_at_start: bool = False,
) -> bool:
    """Find ``brindle <subcommand>`` invocations across visible prose ``text``.

    Uses ``char_lines[offset]`` for exact 1-based source line numbers. Returns
    whether ``text`` ends in a sentence containing a ``brindle`` CLI invocation.
    """
    cmd_positions: list[int] = sorted(prior_cmd_offsets) if prior_cmd_offsets else []
    if prior_cmd_at_start:
        cmd_positions.insert(0, 0)

    def has_prior_cmd_in_sentence(pos: int) -> bool:
        prev = None
        for p in cmd_positions:
            if p <= pos:
                prev = p
            else:
                break
        if prev is None:
            return False
        between = text[prev:pos]
        return not re.search(r"\n\s*\n|[.;?!]", between)

    if "brindle" not in text:
        return has_prior_cmd_in_sentence(len(text))

    # 1. Shell-prompt lines in plain text (`$ brindle ...`).
    # Mask matched prompt lines so comments/arguments are not rescanned as prose.
    prompt_chars = list(text)
    for m in re.finditer(r"(?m)^\s*(?:\$|%)\s+brindle\s+(.+)$", text):
        lineno = char_lines[m.start()]
        for tokens in _extract_command_candidates(
            _strip_shell_comment(m.group(0)), tree, global_options
        ):
            bad = _validate_command_tokens(tokens, tree)
            if bad:
                record(lineno, "command", bad)
        cmd_positions.append(m.end())
        for idx in range(m.start(), m.end()):
            if prompt_chars[idx] != "\n":
                prompt_chars[idx] = " "
    scan_text = "".join(prompt_chars)

    # 2. Quoted invocations (`"brindle bogus-cmd"` or `'brindle bogus-cmd'`).
    for qm in _PROSE_QUOTED_RE.finditer(scan_text):
        lineno = char_lines[qm.start()]
        for tokens in _parse_after_brindle(qm.group(2).strip(), tree, global_options):
            bad = _validate_command_tokens(tokens, tree, prose=True)
            if bad:
                record(lineno, "command", bad)
        cmd_positions.append(qm.end())

    cmd_positions.sort()

    # 3. Standalone `brindle ...` invocation boundaries anywhere in visible prose.
    for bm in _PROSE_BRINDLE_RE.finditer(scan_text):
        pos = bm.start()
        lineno = char_lines[pos]
        before_raw = scan_text[max(0, pos - 60):pos]
        before_clause = re.split(r"\n\s*\n|[.;?!]", before_raw)[-1]
        tail = scan_text[bm.end():].lstrip()
        if not tail:
            continue
        clause = re.split(r"""\n\s*\n|[.,;:?!)\]}"'`—–·]""", tail, maxsplit=1)[0].strip()
        if not clause:
            continue
        raw_tokens = clause.split()
        if not raw_tokens:
            continue

        # Decide whether this occurrence is a CLI command reference vs a product mention:
        # - explicit option/delimiter (`brindle -s ...`, `brindle -- ...`)
        # - lead-in verb (`Run brindle ...`) or conjunction after a prior command in the same sentence
        # - followed by `command`/`subcommand` (`The brindle foo command ...`)
        # - starts with a real imported CLI command/group (`brindle agent nonexistent ...`)
        # - standalone hyphen/underscore command token (`brindle bogus-cmd`, not `a brindle ...`)
        starts_with_opt = raw_tokens[0].startswith("-")
        has_leadin = bool(_PROSE_LEADIN_BEFORE_RE.search(before_clause))
        has_conj = has_prior_cmd_in_sentence(pos) and bool(
            _PROSE_CONJ_BEFORE_RE.search(before_clause)
        )
        post_opt_tokens = _consume_global_options(raw_tokens, tree, global_options)
        has_cmd_suffix = _has_command_suffix(post_opt_tokens)
        is_determiner_noun = bool(_PROSE_DETERMINER_BEFORE_RE.search(before_clause))
        is_known_cmd = (
            bool(post_opt_tokens)
            and post_opt_tokens[0] in tree
            and not is_determiner_noun
        )
        is_hyphen_cmd = (
            bool(post_opt_tokens)
            and _is_candidate_token(post_opt_tokens[0])
            and ("-" in post_opt_tokens[0] or "_" in post_opt_tokens[0])
            and not is_determiner_noun
        )

        if not (
            starts_with_opt
            or has_leadin
            or has_conj
            or has_cmd_suffix
            or is_known_cmd
            or is_hyphen_cmd
        ):
            continue

        for tokens in _parse_after_brindle(clause, tree, global_options):
            bad = _validate_command_tokens(tokens, tree, prose=True)
            if bad:
                record(lineno, "command", bad)
        cmd_positions.append(bm.end())
        cmd_positions.sort()

    return has_prior_cmd_in_sentence(len(scan_text))


def _scan_prose_line(
    line_text: str,
    lineno: int,
    tree: dict[str, dict | None],
    global_options: dict[str, int],
    record,
    *,
    prior_cmd_offsets: set[int] | None = None,
) -> bool:
    """Find ``brindle <subcommand>`` invocations in a single line of plain prose."""
    return _scan_prose_text(
        line_text,
        [lineno] * len(line_text),
        tree,
        global_options,
        record,
        prior_cmd_offsets=prior_cmd_offsets,
    )


# -- JSON config key extraction -----------------------------------------------


_SELF_CONTAINED_BRINDLE_RE = re.compile(r"^(?:(?:\$|%)\s+)?brindle\s+\S")


def _is_self_contained_brindle_code(span_text: str) -> bool:
    """True when a code span holds a complete ``brindle <args...>`` invocation."""
    return bool(_SELF_CONTAINED_BRINDLE_RE.match(span_text.strip()))


def _looks_like_json_block(text: str) -> bool:
    """Return True when ``text`` looks like a JSON object or JSON key/value fragment."""
    stripped = text.lstrip()
    return stripped.startswith("{") or bool(_STRICT_JSON_FRAGMENT_RE.match(stripped))


def _extract_top_level_json_keys(text: str, start_line: int) -> list[tuple[int, str]]:
    """Extract top-level JSON object keys from ``text`` with 1-based line numbers.

    Handles both full JSON objects (``{"a": 1, "b": {"nested": 2}}``) and
    top-level fragments (``"pricing": {"my-model": {...}}``), tolerating
    comments or ellipses in documentation examples.
    """
    stripped = text.lstrip()
    if not stripped:
        return []
    target_depth = 1 if stripped.startswith("{") else 0

    keys: list[tuple[int, str]] = []
    depth = 0
    bracket_depth = 0
    in_string = False
    escaped = False
    str_start_line = start_line
    buf: list[str] = []
    pending_key: tuple[int, str] | None = None
    line = start_line
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            if in_string:
                in_string = False
                escaped = False
                buf = []
            i += 1
            continue

        if in_string:
            if escaped:
                buf.append(ch)
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                if depth == target_depth and bracket_depth == 0:
                    pending_key = (str_start_line, "".join(buf))
                buf = []
            else:
                buf.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue

        if ch == '"':
            in_string = True
            escaped = False
            str_start_line = line
            buf = []
            pending_key = None
        elif ch == ":" and pending_key is not None and depth == target_depth and bracket_depth == 0:
            keys.append(pending_key)
            pending_key = None
        elif ch == "{":
            depth += 1
            pending_key = None
        elif ch == "}":
            depth = max(0, depth - 1)
            pending_key = None
        elif ch == "[":
            bracket_depth += 1
            pending_key = None
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
            pending_key = None
        elif not ch.isspace():
            pending_key = None
        i += 1

    return keys


def _extract_inline_config_keys(code_text: str, line: int) -> list[tuple[int, str]]:
    """Extract config key name(s) from an inline code span associated with config.json."""
    s = code_text.strip()
    if not s:
        return []
    if s.startswith("{"):
        return _extract_top_level_json_keys(s, line)
    m = _INLINE_JSON_KEY_RE.match(s)
    if m:
        return [(line, m.group(1))]
    m = _QUOTED_KEY_RE.match(s)
    if m:
        return [(line, m.group(1))]
    if _CONFIG_KEY_IDENT_RE.match(s):
        return [(line, s)]
    return []


def _file_refs(text: str) -> list[tuple[int, bool]]:
    """Return ``(pos, is_brindle_config)`` for every JSON file or org-policy mention."""
    refs: list[tuple[int, bool]] = []
    for m in _ANY_JSON_FILE_RE.finditer(text):
        refs.append((m.start(), bool(_CONFIG_JSON_RE.search(m.group(0)))))
    for m in _POLICY_REF_RE.finditer(text):
        refs.append((m.start(), False))
    refs.sort(key=lambda item: item[0])
    return refs


def _looks_like_next_block_intro(line: str) -> bool:
    """True when ``line`` ends with ``:`` as an intro for a following code block."""
    plain = re.sub(r"<[^>]+>", "", line).strip()
    return plain.endswith(":")


def _context_is_config_json(
    lines: list[str],
    start_idx: int,
    end_idx: int,
    in_config_heading: bool,
) -> bool:
    """Resolve whether a JSON block at ``lines[start_idx:end_idx]`` belongs to
    ``.brindle/config.json``, preferring the block's own nearest file context
    and stopping at structural boundaries so adjacent examples cannot bleed."""
    # 1. Nearest preceding file/policy reference (stop at previous block or heading).
    before_lines: list[str] = []
    for idx in range(start_idx - 1, max(-1, start_idx - 7), -1):
        ln = lines[idx]
        if _FENCE_LINE_RE.match(ln) or _HTML_PRE_CLOSE_RE.search(ln):
            break
        before_lines.append(ln)
        if _HEADING_LINE_RE.match(ln):
            break
    before_lines.reverse()
    before_refs = _file_refs("\n".join(before_lines))
    if before_refs:
        return before_refs[-1][1]

    # 2. Immediate following prose (stop at next block or heading, excluding the next block's intro).
    after_lines: list[str] = []
    hit_next_block = False
    for idx in range(end_idx, min(len(lines), end_idx + 7)):
        ln = lines[idx]
        if _HEADING_LINE_RE.match(ln):
            break
        if _FENCE_LINE_RE.match(ln) or _HTML_PRE_OPEN_RE.search(ln):
            hit_next_block = True
            break
        after_lines.append(ln)
    if hit_next_block:
        while after_lines and not after_lines[-1].strip():
            after_lines.pop()
        if after_lines and _looks_like_next_block_intro(after_lines[-1]):
            after_lines.pop()
    after_refs = _file_refs("\n".join(after_lines))
    if after_refs:
        return after_refs[0][1]

    # 3. Fall back to whether the enclosing heading names `.brindle/config.json`.
    return in_config_heading


# -- Markdown parser ----------------------------------------------------------


def _check_markdown(
    text: str,
    path: str,
    schema: CLISchema,
    config_keys: frozenset[str],
) -> list[Finding]:
    cli_tree, global_options = schema
    lines = text.splitlines()
    findings: list[Finding] = []
    seen: set[tuple[int, str, str]] = set()

    def record(line: int, kind: str, value: str) -> None:
        key = (line, kind, value)
        if key not in seen:
            seen.add(key)
            findings.append(Finding(path, line, kind, value))

    def check_command_snippet(snippet: str, line: int) -> None:
        for tokens in _extract_command_candidates(snippet, cli_tree, global_options):
            bad = _validate_command_tokens(tokens, cli_tree)
            if bad:
                record(line, "command", bad)

    def check_key(key_line: int, key_name: str) -> None:
        if key_name not in config_keys:
            record(key_line, "config_key", key_name)

    # Pass 1: fenced code blocks, headings, and markdown tables under config headings.
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_lang = ""
    fence_start_idx = 0
    fence_lines: list[str] = []
    fenced_line_indices: set[int] = set()
    in_config_heading = False
    in_config_table = False

    for idx, raw_line in enumerate(lines):
        lineno = idx + 1
        stripped = raw_line.strip()

        if not in_fence:
            fm = re.match(r"^(`{3,}|~{3,})\s*([^\s`~]*)", stripped)
            if fm:
                in_fence = True
                fence_char = fm.group(1)[0]
                fence_len = len(fm.group(1))
                fence_lang = fm.group(2).lower()
                fence_start_idx = idx
                fence_lines = []
                fenced_line_indices.add(idx)
                in_config_table = False
                continue

            hm = re.match(r"^#{1,6}\s+(.*)$", stripped)
            if hm:
                in_config_heading = bool(_CONFIG_JSON_RE.search(hm.group(1)))
                in_config_table = False
                continue

            if in_config_heading and stripped.startswith("|") and stripped.endswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if not in_config_table:
                    if cells and cells[0].lower() in _TABLE_KEY_HEADERS:
                        in_config_table = True
                elif cells and not all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                    first_cell = cells[0]
                    cm = re.fullmatch(r"`([^`]+)`", first_cell)
                    if cm:
                        for kl, kn in _extract_inline_config_keys(cm.group(1), lineno):
                            check_key(kl, kn)
            else:
                in_config_table = False
        else:
            fenced_line_indices.add(idx)
            close_re = rf"^{re.escape(fence_char)}{{{fence_len},}}\s*$"
            if re.match(close_re, stripped):
                body = "\n".join(fence_lines)
                body_start_line = fence_start_idx + 2
                is_json_fence = fence_lang in _JSON_LANGS or (
                    fence_lang == "" and _looks_like_json_block(body)
                )
                if is_json_fence:
                    if _context_is_config_json(lines, fence_start_idx, idx + 1, in_config_heading):
                        for kl, kn in _extract_top_level_json_keys(body, body_start_line):
                            check_key(kl, kn)
                elif fence_lang in _SHELL_LANGS:
                    i = 0
                    while i < len(fence_lines):
                        cmd_line = body_start_line + i
                        joined = _strip_shell_comment(fence_lines[i]).rstrip()
                        while joined.endswith("\\") and i + 1 < len(fence_lines):
                            i += 1
                            joined = joined[:-1] + " " + _strip_shell_comment(fence_lines[i]).strip()
                        check_command_snippet(joined, cmd_line)
                        i += 1
                in_fence = False
                fence_lines = []
            else:
                fence_lines.append(raw_line)

    # Pass 2: inline code spans and visible prose outside fenced code blocks.
    unmasked = "\n".join("" if i in fenced_line_indices else ln for i, ln in enumerate(lines))

    code_spans: list[tuple[int, int, int, str]] = []
    prose_parts: list[str] = []
    cmd_marker = "\x01"
    last_end = 0
    for m in re.finditer(r"(`+)([^`]+?)\1", unmasked):
        start_pos, end_pos = m.start(), m.end()
        prose_parts.append(unmasked[last_end:start_pos])
        span_line = unmasked.count("\n", 0, start_pos) + 1
        span_text = " ".join(m.group(2).split())
        code_spans.append((start_pos, end_pos, span_line, span_text))
        is_cmd_span = _is_self_contained_brindle_code(span_text)
        # Self-contained `brindle ...` code spans are validated directly and replaced with
        # a command marker so prose after them isn't glued onto the command. Other inline
        # code spans (e.g. ``Run brindle `bogus-cmd` ``) keep their visible text in prose.
        nl_count = m.group(0).count("\n")
        if is_cmd_span:
            prose_parts.append(("\n" * nl_count) + cmd_marker)
        else:
            prose_parts.append(m.group(2))
        last_end = end_pos
        check_command_snippet(span_text, span_line)
    prose_parts.append(unmasked[last_end:])

    # Scan plain prose lines for unformatted `brindle <subcommand>` invocations.
    # Strip raw inline HTML tags (e.g. `<img alt="brindle logo">`) and markdown emphasis
    # while preserving exact line numbers and code-command positions.
    prose_text = _HTML_TAG_RE.sub(lambda m: "\n" * m.group(0).count("\n"), "".join(prose_parts))
    for lineno, prose_line in enumerate(prose_text.splitlines(), start=1):
        clean_line = _MD_EMPHASIS_RE.sub(r"\1", prose_line)
        cmd_offsets: set[int] = set()
        stripped_chars: list[str] = []
        for ch in clean_line:
            if ch == cmd_marker:
                cmd_offsets.add(len(stripped_chars))
            else:
                stripped_chars.append(ch)
        _scan_prose_line(
            "".join(stripped_chars),
            lineno,
            cli_tree,
            global_options,
            record,
            prior_cmd_offsets=cmd_offsets,
        )

    # Inline config.json key references.
    after_target_re = re.compile(
        r"^\s*(?:"
        r"(?:\([^)]*config[^)]*\)\s*)?(?:in|of|under)\s+"
        r"(?:your\s+|the\s+|a\s+)?(?:repo'?s\s+)?(?:shared\s+|local\s+)?"
        r"(`[^`]*`|\S+)"
        r"|\(([^)]*)\)"
        r")",
        re.I,
    )
    before_target_re = re.compile(
        r"(?:~/)?\.brindle/config(?:\.local)?\.json`?\s+(?:as|key|setting|with)\s+$",
        re.I,
    )
    for start_pos, end_pos, span_line, span_text in code_spans:
        if _CONFIG_JSON_RE.search(span_text):
            continue
        after_text = unmasked[end_pos:end_pos + 100]
        after_clause = re.split(r"\n\s*\n|[;|]|\.(?:\s|$)", after_text, maxsplit=1)[0]
        am = after_target_re.match(after_clause)
        if am and _CONFIG_JSON_RE.search(am.group(1) or am.group(2) or ""):
            for kl, kn in _extract_inline_config_keys(span_text, span_line):
                check_key(kl, kn)
            continue

        before_text = unmasked[max(0, start_pos - 80):start_pos]
        before_clause = re.split(r"\n\s*\n|[;|]|\.(?:\s|$)", before_text)[-1]
        if before_target_re.search(before_clause):
            for kl, kn in _extract_inline_config_keys(span_text, span_line):
                check_key(kl, kn)

    return findings


# -- HTML parser --------------------------------------------------------------


class _HTMLDocExtractor(HTMLParser):
    """Collect ``<pre>``, ``<code>``, plain prose, headings, and tables with line numbers."""

    _BLOCK_TAGS = frozenset({
        "p", "div", "li", "td", "th", "tr", "section", "article",
        "h1", "h2", "h3", "h4", "h5", "h6", "br", "header", "footer",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.pre_depth = 0
        self.code_depth = 0
        self.heading_tag: str | None = None
        self.heading_buf: list[str] = []
        self.in_config_heading = False

        self.pre_blocks: list[tuple[int, int, list[tuple[int, str]], bool]] = []
        self._cur_pre_start = 1
        self._cur_pre_chunks: list[tuple[int, str]] = []

        self.inline_blocks: list[list[tuple[bool, int, str]]] = []
        self._cur_block: list[tuple[bool, int, str]] = []
        self._cur_code_start = 1
        self._cur_code_buf: list[str] = []

        self.in_table = False
        self.in_config_table = False
        self._row_cells: list[tuple[bool, int, str, bool]] = []
        self._cur_cell_is_th = False
        self._cur_cell_line = 1
        self._cur_cell_buf: list[str] = []
        self._cur_cell_code_buf: list[str] = []
        self._cur_cell_in_code = False
        self.table_config_keys: list[tuple[int, str]] = []

    def _flush_block(self) -> None:
        if self._cur_block:
            self.inline_blocks.append(self._cur_block)
            self._cur_block = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        line = self.getpos()[0]
        if tag in ("script", "style"):
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._flush_block()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.heading_tag = tag
            self.heading_buf = []
        elif tag == "pre":
            if self.pre_depth == 0:
                self._cur_pre_start = line
                self._cur_pre_chunks = []
            self.pre_depth += 1
        elif tag == "code":
            if self.pre_depth == 0:
                if self.code_depth == 0:
                    self._cur_code_start = line
                    self._cur_code_buf = []
                self.code_depth += 1
            if self.in_table:
                self._cur_cell_in_code = True
        elif tag == "table":
            self.in_table = True
            self.in_config_table = False
        elif tag == "tr" and self.in_table:
            self._row_cells = []
        elif tag in ("th", "td") and self.in_table:
            self._cur_cell_is_th = tag == "th"
            self._cur_cell_line = line
            self._cur_cell_buf = []
            self._cur_cell_code_buf = []
            self._cur_cell_in_code = False

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        line = self.getpos()[0]
        if tag in ("script", "style"):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == self.heading_tag:
            heading_text = "".join(self.heading_buf)
            self.in_config_heading = bool(_CONFIG_JSON_RE.search(heading_text))
            self.heading_tag = None
            self.heading_buf = []
        elif tag == "pre" and self.pre_depth > 0:
            self.pre_depth -= 1
            if self.pre_depth == 0:
                self.pre_blocks.append((
                    self._cur_pre_start,
                    line,
                    self._cur_pre_chunks,
                    self.in_config_heading,
                ))
                self._cur_pre_chunks = []
        elif tag == "code":
            if self.pre_depth == 0 and self.code_depth > 0:
                self.code_depth -= 1
                if self.code_depth == 0:
                    text = " ".join("".join(self._cur_code_buf).split())
                    if text:
                        self._cur_block.append((True, self._cur_code_start, text))
                    self._cur_code_buf = []
            if self.in_table:
                self._cur_cell_in_code = False
        elif tag in ("th", "td") and self.in_table:
            cell_text = " ".join("".join(self._cur_cell_buf).split())
            code_text = " ".join("".join(self._cur_cell_code_buf).split())
            self._row_cells.append((
                self._cur_cell_is_th,
                self._cur_cell_line,
                code_text or cell_text,
                bool(code_text),
            ))
        elif tag == "tr" and self.in_table:
            if self.in_config_heading and self._row_cells:
                first_is_th, first_line, first_text, first_has_code = self._row_cells[0]
                if not self.in_config_table:
                    if first_is_th and first_text.lower() in _TABLE_KEY_HEADERS:
                        self.in_config_table = True
                elif first_has_code:
                    self.table_config_keys.append((first_line, first_text))
            self._row_cells = []
        elif tag == "table":
            self.in_table = False
            self.in_config_table = False
        if tag in self._BLOCK_TAGS:
            self._flush_block()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        line = self.getpos()[0]
        if self.heading_tag is not None:
            self.heading_buf.append(data)
        if self.in_table:
            self._cur_cell_buf.append(data)
            if self._cur_cell_in_code:
                self._cur_cell_code_buf.append(data)
        if self.pre_depth > 0:
            self._cur_pre_chunks.append((line, data))
        elif self.code_depth > 0:
            self._cur_code_buf.append(data)
        else:
            self._cur_block.append((False, line, data))


def _check_html(
    text: str,
    path: str,
    schema: CLISchema,
    config_keys: frozenset[str],
) -> list[Finding]:
    cli_tree, global_options = schema
    lines = text.splitlines()
    parser = _HTMLDocExtractor()
    parser.feed(text)
    parser.close()
    parser._flush_block()

    findings: list[Finding] = []
    seen: set[tuple[int, str, str]] = set()

    def record(line: int, kind: str, value: str) -> None:
        key = (line, kind, value)
        if key not in seen:
            seen.add(key)
            findings.append(Finding(path, line, kind, value))

    def check_command_snippet(snippet: str, line: int) -> None:
        for tokens in _extract_command_candidates(snippet, cli_tree, global_options):
            bad = _validate_command_tokens(tokens, cli_tree)
            if bad:
                record(line, "command", bad)

    def check_key(key_line: int, key_name: str) -> None:
        if key_name not in config_keys:
            record(key_line, "config_key", key_name)

    # 1. <pre> blocks: JSON config blocks or shell commands.
    for start_line, end_line, chunks, in_config_heading in parser.pre_blocks:
        if not chunks:
            continue
        full_text = "".join(chunk for _, chunk in chunks)
        first_chunk_line = chunks[0][0]
        if _looks_like_json_block(full_text):
            if _context_is_config_json(lines, start_line - 1, end_line, in_config_heading):
                for kl, kn in _extract_top_level_json_keys(full_text, first_chunk_line):
                    check_key(kl, kn)
            continue

        line_map: dict[int, list[str]] = {}
        for chunk_line, chunk_text in chunks:
            for offset, part in enumerate(chunk_text.split("\n")):
                line_map.setdefault(chunk_line + offset, []).append(part)
        for ln in sorted(line_map):
            raw_cmd = _strip_shell_comment("".join(line_map[ln]))
            check_command_snippet(raw_cmd, ln)

    # 2. Table config keys under config.json headings.
    for kl, cell_code in parser.table_config_keys:
        for key_line, key_name in _extract_inline_config_keys(cell_code, kl):
            check_key(key_line, key_name)

    # 3. Inline <code> spans and plain prose inside block elements.
    html_after_target_re = re.compile(
        r"^\s*(?:"
        r"(?:\([^)]*config[^)]*\)\s*)?(?:in|of|under)\s+"
        r"(?:your\s+|the\s+|a\s+)?(?:repo'?s\s+)?(?:shared\s+|local\s+)?"
        r"(\S+)"
        r"|\(([^)]*)\)"
        r")",
        re.I,
    )
    html_before_target_re = re.compile(
        r"(?:~/)?\.brindle/config(?:\.local)?\.json\s+(?:as|key|setting|with)\s+$",
        re.I,
    )
    for block in parser.inline_blocks:
        prose_chars: list[str] = []
        prose_char_lines: list[int] = []
        cmd_offsets: set[int] = set()

        def append_visible(seg_line: int, seg_text: str) -> None:
            cur_line = seg_line
            for ch in seg_text:
                prose_chars.append(ch)
                prose_char_lines.append(cur_line)
                if ch == "\n":
                    cur_line += 1

        for i, (is_code, line, seg_text) in enumerate(block):
            if not is_code:
                append_visible(line, seg_text)
                continue

            is_cmd_span = _is_self_contained_brindle_code(seg_text)
            if is_cmd_span:
                # Self-contained `brindle ...` code spans are checked directly; record their
                # position for same-sentence conjunction tracking without gluing trailing prose.
                cmd_offsets.add(len(prose_chars))
                prose_chars.append(" ")
                prose_char_lines.append(line)
            else:
                # Retain visible text across inline `<code>` boundaries so commands like
                # `<p>Run brindle <code>bogus-cmd</code> ...</p>` are detected as one invocation.
                append_visible(line, seg_text)

            check_command_snippet(seg_text, line)
            if _CONFIG_JSON_RE.search(seg_text):
                continue

            after_text = "".join(t for _, _, t in block[i + 1:i + 5])
            after_clause = re.split(r";|\.(?:\s|$)", after_text, maxsplit=1)[0]
            am = html_after_target_re.match(after_clause)
            if am and _CONFIG_JSON_RE.search(am.group(1) or am.group(2) or ""):
                for kl, kn in _extract_inline_config_keys(seg_text, line):
                    check_key(kl, kn)
                continue

            before_text = "".join(t for _, _, t in block[max(0, i - 4):i])
            before_clause = re.split(r";|\.(?:\s|$)", before_text)[-1]
            if html_before_target_re.search(before_clause):
                for kl, kn in _extract_inline_config_keys(seg_text, line):
                    check_key(kl, kn)

        if prose_chars:
            _scan_prose_text(
                "".join(prose_chars),
                prose_char_lines,
                cli_tree,
                global_options,
                record,
                prior_cmd_offsets=cmd_offsets,
            )

    return findings


# -- Public entry points ------------------------------------------------------


def check_text(
    text: str,
    path: str = "<input>",
    *,
    is_html: bool = False,
    cli_tree: dict[str, dict | None] | None = None,
    config_keys: frozenset[str] | None = None,
    cli_schema: CLISchema | None = None,
) -> list[Finding]:
    """Check markdown or HTML text and return findings sorted by line number."""
    if cli_schema is not None:
        schema = cli_schema
    elif cli_tree is not None:
        schema = CLISchema(cli_tree, load_cli_schema().global_options)
    else:
        schema = load_cli_schema()
    keys = config_keys if config_keys is not None else load_config_keys()
    if is_html:
        found = _check_html(text, path, schema, keys)
    else:
        found = _check_markdown(text, path, schema, keys)
    return sorted(found, key=lambda f: (f.line, f.kind, f.value))


def check_file(
    path: Path,
    *,
    cli_tree: dict[str, dict | None] | None = None,
    config_keys: frozenset[str] | None = None,
    cli_schema: CLISchema | None = None,
) -> list[Finding]:
    """Check one markdown or HTML file on disk."""
    is_html = path.suffix.lower() in _HTML_SUFFIXES
    text = path.read_text(encoding="utf-8")
    return check_text(
        text,
        str(path),
        is_html=is_html,
        cli_tree=cli_tree,
        config_keys=config_keys,
        cli_schema=cli_schema,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check markdown or HTML docs for unknown brindle subcommands and .brindle/config.json keys.",
    )
    parser.add_argument("files", nargs="+", type=Path, help="Markdown or HTML files to check.")
    args = parser.parse_args(argv)

    schema = load_cli_schema()
    config_keys = load_config_keys()

    findings: list[Finding] = []
    missing = False
    for path in args.files:
        if not path.is_file():
            print(f"{path}: file not found", file=sys.stderr)
            missing = True
            continue
        findings.extend(check_file(path, cli_schema=schema, config_keys=config_keys))

    for f in findings:
        print(f.format())

    return 1 if (findings or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
