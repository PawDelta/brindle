"""Rule packs, checked against a branch's diff.

A profile's ``rules`` packs (see ``brindle.profiles``) carry prompt text the
agent reads, and up to three mechanical rules brindle checks itself, in the
same step that runs the repo's ``checks`` before a branch is reviewed or
merged (``brindle.gates``), so a violation goes back to the worker the way a
failed check does:

- ``deny_deps``: names that may not be added as a dependency (an added line
  naming one in a manifest: pyproject.toml, requirements*.txt, package.json,
  go.mod, Cargo.toml, ...) or imported (an added ``import x`` / ``from x
  import`` / ``require("x")`` line).
- ``require_tests_for``: globs; a diff that changes a file matching one must
  also change a test file (``tests/``, ``test_*``, ``*_test.*``, ``*.test.*``,
  ``*.spec.*``, ``__tests__/``).
- ``deny_patterns``: regexes no added line may match.

For a Team org member (``org_budgets``) it also fails any change to one of
the org's ``protected_paths``, whoever the worker is (``run``).

The same step checks the profile's ``write_scope`` (brindle Pro guardrails,
see ``brindle.guardrails``): every file the branch changes must match it.

Only committed work is checked (``merge-base..HEAD``): the gates require a
clean tree first, and the reviewer's summary runs on the same commit.

These are pattern checks on text, and that is their limit: a dependency
loaded through a name built at run time, a test file that tests nothing, a
denied construct spelled in a way the regex doesn't know, go through. They
catch the honest mistake and the obvious shortcut and name the line; the
pack's prose, which the agent reads, and the reviewer, which reads the diff,
carry the rest. The diff itself can't be hidden from them (see
``branch_diff``), and a profile that can't be loaded fails closed.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from brindle import git
from brindle.profiles import RulePack

MANIFESTS = (
    "pyproject.toml", "requirements*.txt", "requirements*.in", "requirements/*", "setup.py",
    "setup.cfg", "Pipfile", "environment.yml", "environment.yaml", "package.json", "go.mod",
    "Cargo.toml", "Gemfile", "*.gemspec", "pom.xml", "build.gradle", "build.gradle.kts",
    "mix.exs", "composer.json", "Package.swift", "*.csproj", "pubspec.yaml", "deno.json",
)
TEST_PATTERNS = ("tests/*", "test/*", "*/tests/*", "*/test/*", "test_*", "*/test_*",
                 "*_test.*", "*.test.*", "*.spec.*", "__tests__/*", "*/__tests__/*", "spec/*")
MAX_SHOWN_PER_RULE = 10


@dataclass
class Violation:
    pack: str
    rule: str          # deny_deps | require_tests_for | deny_patterns
    detail: str        # "src/app.py:12: eval(" or "src/app.py changed without a test"

    def __str__(self) -> str:
        return f"{self.pack} ({self.rule}): {self.detail}"


@dataclass
class Diff:
    """The added lines of a unified diff, by new path."""
    files: list[str]
    added: dict[str, list[tuple[int, str]]]


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _header_path(target: str, prefix: str) -> str | None:
    """The path in a ``--- a/...`` or ``+++ b/...`` header line (git quotes
    one with unusual characters C-style); None for ``/dev/null``."""
    target = target.rstrip("\r")
    if target.startswith('"') and target.endswith('"') and len(target) >= 2:
        import ast

        try:
            target = ast.literal_eval("b" + target).decode("utf-8", "surrogateescape")
        except (ValueError, SyntaxError):
            target = target[1:-1]
    elif "\t" in target:
        target = target.split("\t", 1)[0]
    if target == "/dev/null":
        return None
    return target[len(prefix):] if target.startswith(prefix) else target


def parse_diff(text: str) -> Diff:
    """Paths touched and the lines added, from ``git diff`` output. Deleted
    files are touched but add nothing; binary files add nothing.

    Hunk bodies are consumed by the line counts in their ``@@`` header, so a
    removed line that happens to start with ``-- `` or an added one that
    starts with ``++ `` is content, never mistaken for a file header: the
    worker writes the branch, and the headers decide which file a rule
    applies to. Lines end at ``\\n`` and nothing else, as git counts them: a
    form feed, a bare ``\\r`` or U+2028 inside a line (what ``splitlines``
    would also break on) must not throw the counts off, or the rest of the
    hunk would go unread."""
    files: list[str] = []
    added: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    line_no = 0
    old_left = new_left = 0

    def touched(p: str | None) -> None:
        if p is not None and p not in files:
            files.append(p)

    for line in text.split("\n"):
        if old_left > 0 or new_left > 0:
            # Inside a hunk: every line is content until the counts run out.
            mark = line[:1]
            if mark == "\\":          # "\ No newline at end of file"
                continue
            if mark == "+":
                if path is not None:
                    added.setdefault(path, []).append((line_no, line[1:]))
                line_no += 1
                new_left -= 1
            elif mark == "-":
                old_left -= 1
            else:                     # a context line (" ..." or, blank-suppressed, "")
                line_no += 1
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("diff --git "):
            path = None
            continue
        if line.startswith("--- "):
            touched(_header_path(line[4:], "a/"))   # a deleted file: its +++ is /dev/null
            continue
        if line.startswith("+++ "):
            path = _header_path(line[4:], "b/")
            touched(path)
            if path is not None:
                added.setdefault(path, [])
            continue
        hunk = _HUNK.match(line)
        if hunk:
            old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
            new_left = int(hunk.group(3)) if hunk.group(3) is not None else 1
            line_no = int(hunk.group(2))
    return Diff(files, added)


# The diff must show every added line, whatever the branch or the shared git
# config says: no external or textconv drivers, binary (a ``-diff`` attribute
# in the branch's .gitattributes) forced to text, and the a/ b/ prefixes,
# unquoted paths and blank context lines the parser expects, whatever
# diff.noprefix, diff.mnemonicPrefix, core.quotepath or
# diff.suppressBlankEmpty are set to.
DIFF_ARGS = (
    "-c", "core.quotepath=false", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.suppressBlankEmpty=false", "-c", "diff.relative=false",
    "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "--no-renames",
    "--src-prefix=a/", "--dst-prefix=b/", "--ignore-submodules=all",
)


def branch_diff(path: str, base: str) -> Diff:
    """What the branch at ``path`` commits on top of where it forked from ``base``."""
    import subprocess

    start = git.merge_base(path, git.base_ref(path, base))
    args = ["git", *DIFF_ARGS, start, "HEAD"]
    # Bytes, not text: a file in some other encoding must not make the gate
    # crash (and so not run), and the diff must reach the parser whole, not
    # stripped. Undecodable bytes become U+FFFD; the line structure survives.
    try:
        proc = subprocess.run(args, cwd=path, capture_output=True, timeout=git.TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise git.GitError(f"{' '.join(args)}: {e}") from e
    if proc.returncode != 0:
        raise git.GitError(f"{' '.join(args)}: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return parse_diff(proc.stdout.decode("utf-8", "replace"))


# -- the rules ------------------------------------------------------------------


def _matches(path: str, globs: tuple[str, ...] | list[str]) -> bool:
    """Whether ``path`` (or its file name) matches one of ``globs``. Case
    doesn't count: a branch made on a case-insensitive filesystem can carry
    ``Src/app.py`` next to ``src/``, and that must not slip past ``src/*``."""
    path = path.lower()
    name = path.rsplit("/", 1)[-1]
    for g in globs:
        g = g.lower()
        if fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(name, g):
            return True
        # ``src/**/*.py`` means any depth, including none; fnmatch's ``*``
        # already crosses ``/``, so collapse ``**/`` for the zero-depth case.
        if "**/" in g and fnmatch.fnmatchcase(path, g.replace("**/", "")):
            return True
    return False


def is_test_file(path: str) -> bool:
    return _matches(path, TEST_PATTERNS)


def _import_pattern(dep: str) -> re.Pattern:
    """An added line that imports ``dep`` (the top-level name): Python's
    ``import a, dep.sub as x`` / ``from dep import`` / ``import_module("dep")``
    / ``__import__("dep")``, JavaScript's ``import ... from 'dep/sub'`` /
    ``export ... from`` / ``require('dep')`` / ``import('dep')``, Ruby's
    ``require 'dep'``, Rust's ``use dep::`` / ``extern crate dep``, and a Go
    import line ``[alias] "dep/sub"``."""
    d = re.escape(dep)
    q = "['\"`]"
    return re.compile(
        rf"(?:^|;)\s*(?:import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*{d}(?:[\s.,;]|$)|from\s+{d}(?:[\s.]|$)"
        rf"|import\s+{q}{d}(?:{q}|/)"
        rf"|(?:pub(?:\([^)]*\))?\s+)?use\s+(?:::)?{d}(?:::|;|\s)|extern\s+crate\s+{d}\b"
        rf"|(?:[\w.]+\s+)?{q}{d}(?:/[^'\"`]*)?{q}\s*;?\s*$)"
        rf"|\bfrom\s+{q}{d}(?:{q}|/)"
        rf"|\b(?:require|require_relative|import|import_module|__import__)\s*\(?\s*{q}{d}(?:{q}|/)",
        re.IGNORECASE,
    )


def _manifest_pattern(dep: str) -> re.Pattern:
    """``dep`` as a manifest names it: case doesn't matter, and ``-``, ``_``
    and ``.`` are one character, as they are to PyPI (``python-dateutil`` is
    ``python_dateutil``), so a different spelling isn't a different package."""
    parts = re.split(r"[-_.]", dep)
    d = r"[-_.]".join(re.escape(p) for p in parts)
    return re.compile(rf"(?<![\w.-]){d}(?![\w.-])", re.IGNORECASE)


def check_deny_deps(pack: RulePack, diff: Diff) -> list[Violation]:
    found: list[Violation] = []
    for dep in pack.deny_deps:
        imp, man = _import_pattern(dep), _manifest_pattern(dep)
        for path, lines in diff.added.items():
            manifest = _matches(path, MANIFESTS)
            for no, text in lines:
                if (manifest and man.search(text)) or imp.search(text):
                    what = "added as a dependency" if manifest else "imported"
                    found.append(Violation(pack.name, "deny_deps",
                                           f"{path}:{no}: {dep} {what}: {text.strip()}"))
    return found


def check_require_tests(pack: RulePack, diff: Diff) -> list[Violation]:
    if not pack.require_tests_for:
        return []
    # A test file the diff adds or changes; one it only deletes doesn't count.
    if any(is_test_file(p) and p in diff.added for p in diff.files):
        return []
    covered = [p for p in diff.files if not is_test_file(p) and _matches(p, pack.require_tests_for)]
    if not covered:
        return []
    shown = ", ".join(covered[:MAX_SHOWN_PER_RULE]) + (" ..." if len(covered) > MAX_SHOWN_PER_RULE else "")
    return [Violation(pack.name, "require_tests_for",
                      f"{shown} changed, but the diff adds or changes no test file")]


def compile_patterns(pack: RulePack) -> tuple[list[tuple[str, re.Pattern]], list[str]]:
    """The pack's ``deny_patterns`` compiled, and the ones that don't compile."""
    compiled, bad = [], []
    for pattern in pack.deny_patterns:
        try:
            compiled.append((pattern, re.compile(pattern)))
        except re.error as e:
            bad.append(f"{pattern!r}: {e}")
    return compiled, bad


def check_deny_patterns(pack: RulePack, diff: Diff) -> list[Violation]:
    found: list[Violation] = []
    compiled, bad = compile_patterns(pack)
    for problem in bad:
        found.append(Violation(pack.name, "deny_patterns", f"pattern doesn't compile: {problem}"))
    for pattern, rx in compiled:
        hits = 0
        for path, lines in diff.added.items():
            for no, text in lines:
                if rx.search(text):
                    hits += 1
                    if hits <= MAX_SHOWN_PER_RULE:
                        found.append(Violation(pack.name, "deny_patterns",
                                               f"{path}:{no}: matches /{pattern}/: {text.strip()}"))
        if hits > MAX_SHOWN_PER_RULE:
            found.append(Violation(pack.name, "deny_patterns",
                                   f"... and {hits - MAX_SHOWN_PER_RULE} more lines match /{pattern}/"))
    return found


def check(packs: list[RulePack], diff: Diff) -> list[Violation]:
    found: list[Violation] = []
    for pack in packs:
        found += check_deny_deps(pack, diff)
        found += check_require_tests(pack, diff)
        found += check_deny_patterns(pack, diff)
    return found


def format_violations(violations: list[Violation]) -> str:
    return "\n".join(f"- {v}" for v in violations)


# -- for the gates -------------------------------------------------------------------


@dataclass
class Result:
    packs: list[str]
    violations: list[Violation]
    problem: str | None = None   # the packs couldn't be loaded or the diff read

    @property
    def ok(self) -> bool:
        return not self.violations and self.problem is None

    def summary(self) -> str:
        """One PASS/FAIL block per pack, in the form of ``gates.check_summary``."""
        if self.problem:
            return f"FAIL `rules`\n{self.problem}"
        lines = []
        for name in self.packs:
            own = [v for v in self.violations if v.pack == name]
            if own:
                lines.append(f"FAIL `rules {name}`\n{format_violations(own)}")
            else:
                lines.append(f"PASS `rules {name}`")
        return "\n".join(lines)


def _worker_rules(db, ws):
    """The profile (guardrails as they apply, see ``guardrails.effective``)
    and rule packs of the worker on ``ws``'s branch; ``(None, [])`` without
    a worker. Raises RuleCheckError when either can't be loaded."""
    from brindle import agents, guardrails
    from brindle.profiles import ProfileError, load_profile, load_rule_packs

    worker = agents.workspace_worker(db, ws)
    if worker is None:
        return None, []
    try:
        profile = load_profile(worker.profile, ws.repo_root)
    except (KeyError, ProfileError) as e:
        raise RuleCheckError(f"profile {worker.profile!r} can't be loaded: {e}") from e
    try:
        packs = load_rule_packs(profile, ws.repo_root)
    except (KeyError, ProfileError) as e:
        raise RuleCheckError(f"the rule packs of profile {worker.profile!r} can't be loaded: {e}") from e
    return guardrails.effective(profile), packs


def packs_for_workspace(db, ws) -> list[RulePack]:
    """The rule packs of the worker on ``ws``'s branch. A workspace without a
    worker (a person's, or a supervisor's checkout) has none. A worker whose
    profile can't be loaded (deleted, a parent missing, a cycle) is a
    RuleCheckError, not "no rules": the gate fails closed, and the message
    says which file to restore."""
    return _worker_rules(db, ws)[1]


class RuleCheckError(Exception):
    pass


# -- write scope (guardrails) ------------------------------------------------------------

SCOPE = "write_scope"


def branch_files(path: str, base: str) -> list[str]:
    """Every path the branch at ``path`` changes since it forked from ``base``:
    added, modified, deleted, both sides of a rename, a mode change or an
    empty new file (which ``branch_diff``'s hunks don't show), a submodule."""
    import subprocess

    start = git.merge_base(path, git.base_ref(path, base))
    args = ["git", "-c", "core.quotepath=false", "-c", "diff.relative=false", "diff", "--name-only",
            "-z", "--no-renames", "--no-ext-diff", start, "HEAD"]
    try:
        proc = subprocess.run(args, cwd=path, capture_output=True, timeout=git.TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise git.GitError(f"{' '.join(args)}: {e}") from e
    if proc.returncode != 0:
        raise git.GitError(f"{' '.join(args)}: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def check_write_scope(scope: list[str] | None, files: list[str]) -> list[Violation]:
    """A violation for the files outside ``scope`` (none without a scope)."""
    from brindle import guardrails

    if not scope:
        return []
    outside = [p for p in files if not guardrails.in_scope(p, scope)]
    found = [Violation(SCOPE, SCOPE, f"{p} is outside the profile's write_scope ("
                       + ", ".join(scope) + ")") for p in outside[:MAX_SHOWN_PER_RULE]]
    if len(outside) > MAX_SHOWN_PER_RULE:
        found.append(Violation(SCOPE, SCOPE, f"... and {len(outside) - MAX_SHOWN_PER_RULE} more files"))
    return found


# -- protected paths (Team org budgets) ----------------------------------------------------

PROTECTED = "protected_paths"


def protected_files(files: list[str], globs: list[str] | tuple[str, ...]) -> list[str]:
    """The ``files`` that match one of the org's protected ``globs``. A glob
    naming a directory (``infra`` or ``infra/``) covers everything under it,
    and one without a ``/`` also matches a file of that name anywhere."""
    expanded: list[str] = []
    for g in globs:
        g = g.strip().replace("\\", "/")
        while g.startswith("./"):
            g = g[2:]
        g = g.lstrip("/")
        if g:
            expanded += [g, g.rstrip("/") + "/*"]
    def norm(p: str) -> str:
        p = p.replace("\\", "/")
        return p[2:] if p.startswith("./") else p

    return [p for p in files if _matches(norm(p), expanded)]


def check_protected_paths(globs: list[str] | tuple[str, ...], files: list[str]) -> list[Violation]:
    """A violation for each file the branch changes that the org protects:
    any change at all (add, edit, delete, rename, mode) fails."""
    if not globs:
        return []
    hit = protected_files(files, globs)
    found = [Violation(PROTECTED, PROTECTED, f"{p} is a protected path of your org and may not be "
                       "changed") for p in hit[:MAX_SHOWN_PER_RULE]]
    if len(hit) > MAX_SHOWN_PER_RULE:
        found.append(Violation(PROTECTED, PROTECTED, f"... and {len(hit) - MAX_SHOWN_PER_RULE} more "
                               "protected files"))
    return found


def org_protected_paths(repo_root: str) -> tuple[tuple[str, ...], str | None]:
    """(the org's protected path globs for this member, why they can't be had).
    No globs without a Team org that has ``org_budgets``; an org policy that
    can't be read fails the check rather than skipping it."""
    from brindle.pro import team_policy

    try:
        p = team_policy.org_budgets(repo_root)
    except Exception as e:  # noqa: BLE001 - fail closed
        return (), f"the org's protected paths can't be checked: {e}"
    if p is None:
        return (), None
    if not isinstance(p, team_policy.OrgPolicy):
        return (), f"the org's protected paths can't be checked: {p.reason}"
    return p.enforced.protected_paths, None


def run(db, ws) -> Result | None:
    """Check ``ws``'s branch against its worker's rule packs and write scope,
    and, for a Team org member, the org's protected paths (any change to one
    fails, whoever made it). None when there is nothing to check, else the result."""
    from brindle import workspaces

    try:
        profile, packs = _worker_rules(db, ws)
    except RuleCheckError as e:
        return Result([], [], problem=str(e))
    scope = profile.write_scope if profile is not None else None
    protected, why = org_protected_paths(ws.repo_root)
    if why:
        return Result([PROTECTED], [], problem=why)
    names = [p.name for p in packs] + ([SCOPE] if scope else []) + ([PROTECTED] if protected else [])
    if not names:
        return None
    mechanical = any(p.mechanical for p in packs)
    if not mechanical and not scope and not protected:
        return Result(names, [])
    try:
        base = workspaces.require_base(ws)
        diff = branch_diff(ws.path, base) if mechanical else None
        files = branch_files(ws.path, base) if scope or protected else []
    except (git.GitError, workspaces.WorkspaceError) as e:
        return Result(names, [], problem=f"couldn't read the branch's diff: {e}")
    violations = check(packs, diff) if diff is not None else []
    return Result(names, violations + check_write_scope(scope, files)
                  + check_protected_paths(protected, files))


def gate_problem(result: Result | None, branch: str) -> str | None:
    """What a failing result means for the merge gate, in its own words."""
    if result is None or result.ok:
        return None
    if result.problem:
        return f"Rule check of {branch} couldn't run: {result.problem}"
    return (f"Rule check failed in {branch} (the worker's profile rule packs):\n"
            f"{format_violations(result.violations)}\nSend this to the worker to fix.")
