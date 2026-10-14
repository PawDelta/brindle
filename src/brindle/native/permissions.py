"""``permission_mode`` and ``allowed_tools`` for the native loop.

The shapes are Claude Code's, so a profile reads the same whichever provider
runs it: ``Bash(git add:*)`` allows commands starting with ``git add``,
``Bash(pytest)`` exactly ``pytest``, ``Edit`` every edit, ``Read(src/*)``
reads matching a glob. Brindle's own tools are always allowed.

Modes: ``bypassPermissions`` allows everything; ``acceptEdits`` and ``auto``
allow file changes (there's no classifier here, so ``auto`` is
``acceptEdits`` with the allowlist); ``dontAsk``, ``plan`` and the default
allow only reads and what ``allowed_tools`` names. A tool that would need a
person's answer comes back as ``ask``; the loop decides what to do with that
(refuse, for an unattended worker).
"""

from __future__ import annotations

import fnmatch
import re

READ_ONLY = {"Read", "Glob", "Grep"}
# brindle's own tools, as the native loop names them (the MCP server's names
# are the same, prefixed mcp__brindle__).
BRINDLE_TOOLS = {"report_result", "submit_review", "send_message", "workspace_diff"}
EDITS = {"Edit", "Write"}
_RULE = re.compile(r"^(\w+)(?:\((.*)\))?$")
_SEPARATORS = {"&&", "||", ";", ";;", "|", "|&", "&", "\n"}
_OPERATOR_CHARS = set(";&|<>()")
_TO_DEV_NULL = {">", ">>", ">|", "&>", "&>>"}


class Permissions:
    def __init__(self, mode: str | None, allowed: list[str] | None):
        self.mode = _canonical(mode)
        self.rules: list[tuple[str, str | None]] = []
        for raw in allowed or []:
            m = _RULE.match(raw.strip())
            if m:
                self.rules.append((m.group(1), m.group(2)))

    def decide(self, tool: str, args: dict) -> str:
        """'allow', 'deny' or 'ask'."""
        if tool.startswith("mcp__brindle") or tool in BRINDLE_TOOLS or tool in READ_ONLY:
            return "allow"  # a Read(...) rule narrows nothing: reads are always fine
        if self.mode == "bypassPermissions" or self._allowed_by_rule(tool, args):
            return "allow"
        if tool in EDITS and self.mode in ("acceptEdits", "auto"):
            return "allow"
        if self.mode in ("dontAsk", "plan"):
            return "deny"
        return "ask"

    def _allowed_by_rule(self, tool: str, args: dict) -> bool:
        specs = [spec for name, spec in self.rules if name == tool]
        if not specs:
            return False
        if any(spec in (None, "", "*") for spec in specs):
            return True
        if tool == "Bash":
            return uncovered_part(specs, str(args.get("command", ""))) is None
        target = str(args.get("path") or args.get("file_path") or args.get("pattern") or "")
        return any(fnmatch.fnmatchcase(target, spec) or spec.endswith(":*") and target.startswith(spec[:-2])
                   for spec in specs)

    def reason(self, tool: str, args: dict) -> str:
        """Why a Bash command isn't allowed, for the model: the part no rule
        covers. Empty for other tools."""
        if tool != "Bash":
            return ""
        command = str(args.get("command", ""))
        if "$(" in command or "`" in command:
            return "command substitution ($(...) or backticks) is never allowed"
        specs = [spec for name, spec in self.rules if name == "Bash"]
        part = uncovered_part(specs, command)
        if part is None:
            return ""
        if part != command.strip():
            return f"`{part}` isn't covered by the allowed commands (each part of a compound command must be)"
        return "it isn't covered by the allowed commands"


def _canonical(mode: str | None) -> str:
    m = (mode or "default").replace("-", "").lower()
    return {"acceptedits": "acceptEdits", "bypasspermissions": "bypassPermissions",
            "dontask": "dontAsk", "auto": "auto", "plan": "plan"}.get(m, "default")


def bash_matches(spec: str, command: str) -> bool:
    """Whether every simple command in ``command`` is covered by ``spec``
    (``prefix:*`` or an exact command)."""
    return uncovered_part([spec], command) is None


def uncovered_part(specs: list[str | None], command: str, cd_root: str | None = None) -> str | None:
    """The first simple command in ``command`` (``a && b | c`` has three)
    that no spec in ``specs`` covers, or None when all are covered. Command
    substitution is never covered: what it runs can't be seen from here.
    Neither is what split_commands can't split (an unclosed quote, a
    heredoc, a redirection that writes a file): it counts as uncovered. With
    ``cd_root``, a ``cd`` whose target is inside that directory is covered
    too (``cd sub && pytest`` from a worker's own worktree)."""
    command = command.strip()
    if not command or "$(" in command or "`" in command:
        return command or "(empty)"
    parts = split_commands(command)
    if not parts:
        return command
    cwd = cd_root
    for part in parts:
        if cd_root and _cd_inside(part, cwd, cd_root) is not None:
            cwd = _cd_inside(part, cwd, cd_root)
            continue
        if not any(_covers(spec, part) for spec in specs if spec):
            return part
    return None


def _cd_inside(part: str, cwd: str | None, root: str) -> str | None:
    """The directory ``part`` (a ``cd`` command) would land in, if that is
    ``root`` or below it; None for any other command or target."""
    tokens = part.split()
    if len(tokens) != 2 or tokens[0] != "cd" or tokens[1].startswith("-") or "$" in tokens[1]:
        return None  # a variable expands at run time to who knows where
    import os

    # realpath on both sides: a symlink inside the worktree may point out of
    # it, and the worktree itself may sit under one (/tmp on macOS).
    target = os.path.realpath(os.path.join(cwd or root, os.path.expanduser(tokens[1])))
    real_root = os.path.realpath(root)
    return target if target == real_root or target.startswith(real_root + os.sep) else None


def _covers(spec: str, part: str) -> bool:
    if spec.endswith(":*"):
        prefix = spec[:-2].strip()
        return part == prefix or part.startswith(prefix + " ")
    return part == spec.strip()


def split_commands(command: str) -> list[str]:
    """The simple commands joined by a control operator (``&&``, ``||``,
    ``;``, ``;;``, ``|``, ``|&``, ``&`` or a newline), each re-joined from
    its words (so spacing and quoting don't matter). A redirection is dropped
    when it's harmless: output to /dev/null, an fd dup (``2>&1``), input from
    a file. Anything else gives an empty list, which nothing matches: an
    unclosed quote, other output redirection, a heredoc (``<<``), process
    substitution (``<(...)``), a subshell or any other operator."""
    tokens = _tokens(command)
    if tokens is None:
        return []
    parts, current = [], []
    i = 0
    while i < len(tokens):
        is_op, text = tokens[i]
        if not is_op:
            current.append(text)
        elif text in _SEPARATORS:
            if current:
                parts.append(" ".join(current))
            current = []
        else:
            target = tokens[i + 1] if i + 1 < len(tokens) else (True, "")
            if target[0] or not _harmless_redirect(text, target[1]):
                return []
            i += 1
        i += 1
    if current:
        parts.append(" ".join(current))
    return parts


def _harmless_redirect(op: str, target: str) -> bool:
    if op in _TO_DEV_NULL:
        return target == "/dev/null"
    if op in (">&", "<&"):
        return target == "-" or target.isascii() and target.isdigit()
    return op == "<"  # reads a file, as the command itself could


def _tokens(command: str) -> list[tuple[bool, str]] | None:
    """``(is_operator, text)`` for each word (quotes removed) and each run of
    operator characters outside quotes, or None for input whose quoting the
    shell may read differently: an unclosed quote, a ``$`` starting quoting
    or an expansion (``$'...'``, where \\' doesn't end it; ``${...}``, with
    its own quoting rules; see _expands), or a # starting a word (a comment
    to bash, a plain word to interactive zsh: either way a quote after it
    pairs differently). An fd number written against a redirection (the 2
    of ``2>&1``) belongs to it and is dropped."""
    out: list[tuple[bool, str]] = []
    word: list[str] | None = None  # None between words
    quoted = False
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c in " \t\n" or c in _OPERATOR_CHARS:
            j = i + 1
            if c in _OPERATOR_CHARS:
                while j < n and command[j] in _OPERATOR_CHARS:
                    j += 1
            op = command[i:j]
            if word is not None:
                text = "".join(word)
                fd = op[0] in "<>" and not quoted and text.isascii() and text.isdigit()
                if (text or quoted) and not fd:
                    out.append((False, text))
                word = None
            if op != " " and op != "\t":
                out.append((True, op))
            i = j
            continue
        if word is None:
            word, quoted = [], False
        if c == "#" and not word and not quoted or _expands(command, i):
            return None
        if c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                return None
            word.append(command[i + 1:j])
            quoted, i = True, j + 1
        elif c == '"':
            j = i + 1
            while j < n and command[j] != '"':
                if _expands(command, j):
                    return None
                if command[j] == "\\" and j + 1 < n and command[j + 1] in '"\\$`\n':
                    word.append(command[j + 1] if command[j + 1] != "\n" else "")
                    j += 2
                else:
                    word.append(command[j])
                    j += 1
            if j >= n:
                return None
            quoted, i = True, j + 1
        elif c == "\\" and i + 1 < n:
            if command[i + 1] != "\n":  # \<newline> only continues the line
                word.append(command[i + 1])
                quoted = True
            i += 2
        else:
            word.append(c)
            i += 1
    if word is not None and (word or quoted):
        out.append((False, "".join(word)))
    return out


def _expands(command: str, i: int) -> bool:
    """Whether the ``$`` at ``i`` starts quoting or an expansion with its own
    parsing (``$'``, ``$"``, ``${``, ``$(``, ``$[``), once the shell has
    deleted the line continuations (\\<newline>) after it."""
    if command[i] != "$":
        return False
    k = i + 1
    while command.startswith("\\\n", k):
        k += 2
    return k < len(command) and command[k] in "'\"{(["
