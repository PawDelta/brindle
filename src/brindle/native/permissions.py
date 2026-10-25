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

READ_ONLY = {"Read", "Glob", "Grep", "Diagnostics", "Definition", "References"}
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
    too (``cd sub && pytest`` from a worker's own worktree), but only before
    any other command: the target is checked now and entered later, and what
    runs first (``git checkout``, a build) may swap a directory on the way
    for a symlink out. The residual risk is a process running alongside
    (something this agent backgrounded earlier) doing that swap between this
    check and the cd; _cd_inside refuses symlinks so it at least has to
    replace a directory, not just retarget a link."""
    command = command.strip()
    if not command or "$(" in command or "`" in command:
        return command or "(empty)"
    parsed = _simple_commands(command)
    if not parsed:
        return command
    # Where the shell may be: a cd can fail, or be skipped after a failure,
    # so every later cd must stay inside from each place it may start.
    reached = {cd_root} if cd_root else set()  # anywhere, once a list stops
    here = set(reached)                         # where this command starts
    before = ";"
    ran = False                                 # a command other than cd has
    for words, after in parsed:
        if cd_root and words[0] == "cd":
            # In a pipe or backgrounded, it changes a subshell's directory;
            # after ||, whether it ran says nothing about what runs next.
            if ran or before in ("||", "|", "|&") or after in ("|", "|&", "&"):
                return _joined(words)
            landed = {_cd_inside(words, cwd, cd_root) for cwd in here}
            if None in landed or len(reached | landed) > 32:
                return _joined(words)
            reached |= landed
            if after == "&&":  # what follows runs only if this cd worked
                here = landed
        elif not any(_covers(spec, words) for spec in specs if spec):
            return _joined(words)
        else:
            ran = True
        if after != "&&":
            here = set(reached)
        before = after
    return None


def _cd_inside(words: list[str], cwd: str, root: str) -> str | None:
    """Where ``cd`` with ``words`` lands from ``cwd`` (the logical path, as
    $PWD has it), if that is ``root`` or below it; None for any other target.
    No ``-`` option, ``~`` (``~-`` is $OLDPWD, ``~1`` the dir stack) or glob,
    and the same place whether the shell resolves ``..`` logically (bash's
    default: ``link/..`` is back where it started) or physically. Below
    ``root`` it must be an existing directory reached through no symlink:
    one, even pointing inside, can be retargeted before the cd runs."""
    import os

    target = words[1] if len(words) == 2 else ""
    if not target or target.startswith(("-", "~")) or any(c in target for c in "*?[{"):
        return None
    root = os.path.normpath(root)
    logical = os.path.normpath(os.path.join(cwd, target))
    if logical != root and not logical.startswith(root.rstrip(os.sep) + os.sep):
        return None
    path = root     # which itself may sit under a symlink (/tmp on macOS)
    for part in os.path.relpath(logical, root).split(os.sep):
        path = os.path.join(path, part)
        if os.path.islink(path):
            return None
    if not os.path.isdir(logical):
        return None
    # The way there may pass through one (``link/..``), which cd -L skips
    # and cd -P follows.
    physical = os.path.realpath(os.path.join(os.path.realpath(cwd), target))
    return logical if os.path.realpath(logical) == physical else None


def _covers(spec: str, words: list[str]) -> bool:
    """Word by word, so a quoted ``'git status'`` (one word, a command of
    that name) or ``python "-m pytest"`` isn't ``git status`` or ``python -m
    pytest``."""
    if spec.endswith(":*"):
        prefix = spec[:-2].split()
        return bool(prefix) and words[:len(prefix)] == prefix
    return words == spec.split()


def _joined(words: list[str]) -> str:
    """``words`` as one line for a person or a deny rule to read: split at
    any whitespace, even inside a word, so an empty or spaced word (``task
    test "" secret``) can't slip past an exact deny on ``task test secret``."""
    return " ".join(" ".join(words).split())


def split_commands(command: str) -> list[str]:
    """The simple commands joined by a control operator (``&&``, ``||``,
    ``;``, ``;;``, ``|``, ``|&``, ``&`` or a newline), each re-joined from
    its words (so spacing and quoting don't matter; see _joined), or an
    empty list for what _simple_commands can't read."""
    return [_joined(words) for words, _ in _simple_commands(command) or []]


def _simple_commands(command: str) -> list[tuple[list[str], str]] | None:
    """Each simple command's words (quotes removed) with the control operator
    after it ("" for the last). A redirection is dropped when it's harmless:
    output to /dev/null, an fd dup (``2>&1``), input from a file. Anything
    else gives None: an unclosed quote, other output redirection, a heredoc
    (``<<``), process substitution (``<(...)``), a subshell or any other
    operator, a command with no words (``; < x``, ``a ; ; b``) or a list
    left open (``git status |``)."""
    tokens = _tokens(command)
    if tokens is None:
        return None
    out: list[tuple[list[str], str]] = []
    words: list[str] = []
    redirected = False
    i = 0
    while i < len(tokens):
        is_op, text = tokens[i]
        if not is_op:
            words.append(text)
        elif text in _SEPARATORS:
            if words:
                out.append((words, text))
                words, redirected = [], False
            elif redirected or text != "\n":
                return None
            # else a blank line, or a newline continuing a && or |
        else:
            target = tokens[i + 1] if i + 1 < len(tokens) else (True, "")
            if target[0] or not _harmless_redirect(text, target[1]):
                return None
            redirected = True
            i += 1
        i += 1
    if words:
        out.append((words, ""))
    elif redirected or not out or out[-1][1] in ("&&", "||", "|", "|&"):
        return None
    return out


def _harmless_redirect(op: str, target: str) -> bool:
    if op in _TO_DEV_NULL:
        return target == "/dev/null"
    if op in (">&", "<&"):
        return target == "-" or target.isascii() and target.isdigit()
    return op == "<"  # reads a file, as the command itself could


def _tokens(command: str, word_pattern: re.Pattern[str] | None = None) -> list[tuple[bool, str]] | None:
    """``(is_operator, text)`` for each word (quotes removed) and each run of
    operator characters outside quotes, or None for input whose quoting the
    shell may read differently: an unclosed quote, a ``$`` starting quoting
    or an expansion (``$'...'``, where \\' doesn't end it; ``${...}``, with
    its own quoting rules; see _expands), or a # starting a word (a comment
    to bash, a plain word to interactive zsh: either way a quote after it
    pairs differently). An fd number written against a redirection (the 2
    of ``2>&1``) belongs to it and is dropped. With ``word_pattern``, each
    unquoted, unescaped word character must match it, except a literal tilde
    or caret embedded in a word. Quoted and escaped characters are literal arguments,
    not shell syntax."""
    out: list[tuple[bool, str]] = []
    word: list[str] | None = None  # None between words
    quoted = False
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c in " \t\n" or c in _OPERATOR_CHARS:
            j = i + 1
            if c in _OPERATOR_CHARS:
                # \<newline> goes before the shell reads operators: &\<newline>> is &>
                while j < n and (command[j] in _OPERATOR_CHARS or command.startswith("\\\n", j)):
                    j += 1 if command[j] in _OPERATOR_CHARS else 2
            op = command[i:j].replace("\\\n", "")
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
            if word_pattern is not None and not word_pattern.fullmatch(c):
                # HEAD~1 and HEAD^ are literal revision arguments. Keep
                # leading shell expansions and assignment positions closed.
                prefix = "".join(word)
                if c not in "~^" or not prefix or prefix[-1] in "=:":
                    return None
            word.append(c)
            i += 1
    if word is not None and (word or quoted):
        out.append((False, "".join(word)))
    return out


def _expands(command: str, i: int) -> bool:
    """Whether the ``$`` at ``i`` starts quoting or any expansion (``$'``,
    ``$"``, ``${``, ``$(``, ``$[``, ``$VAR``, ``$_``, ``$1``, ``$@``...),
    once the shell has deleted the line continuations (\\<newline>) after
    it: what a parameter holds is only known at run time (``$unset`` is no
    word at all)."""
    if command[i] != "$":
        return False
    k = i + 1
    while command.startswith("\\\n", k):
        k += 2
    return k < len(command) and (command[k] in "'\"{([_@*#?$!-" or command[k].isalnum())
