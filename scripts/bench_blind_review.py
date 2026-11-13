#!/usr/bin/env python3
"""Blind quality review of the solo vs brindle benchmark's two arms.

    python3 scripts/bench_blind_review.py review --task large --run-id 1

Hidden tests alone overstate quality: on the large task both arms passed every
hidden test while their code still lost data. ``review`` reads both arms'
records for one task and run from ``.brindle/bench/solo_vs_brindle.jsonl``
(written by ``scripts/bench_solo_vs_brindle.py run``; each has the final
``checkout``) and the task kit's ``repo.json`` (its ``commit`` is the base),
then for each arm:

1. Blinds it: a fresh git repo in a neutral temp directory whose one commit
   ("base") is the kit's base commit and whose staged working tree is the
   arm's final files. Left out: brindle's files (``.brindle/``, ``.claude/``,
   ``.codex/``, ``.mcp.json``), ``HISTORY_SPEC.md`` and any other top-level
   Markdown file the arm added (planning notes), the grader's
   ``bench_hidden_tests/``, ``.venv/`` and ``.git``. No commit message or
   branch of the arm's is carried over, and every "brindle" and arm branch
   name is redacted in every text file and path of the reviewers' copy
   (working tree, index and history). Reproductions run on a separate,
   unredacted copy of the same files.
2. Gives the staged diff and ``spec.md`` to two independent reviewers, each
   through its own CLI as installed (never patched or wrapped) and run in the
   blinded repo: Opus 5.5 (``claude -p --model claude-opus-5-5``, read-only
   tools) and Codex (``codex exec``, read-only sandbox, default approval
   policy). Each returns JSON findings, each with a standalone reproduction
   script that exits 3 when the bug is present and 0 when it isn't.
3. Verifies each finding by running its reproduction in a fresh temp copy of
   the blinded tree with the arm's ``.venv`` interpreter, under a time limit
   (the subprocess API's own) and ``sandbox-exec``, which denies writes
   outside that copy. Only exit 3 confirms it; anything else drops it. With
   no enforcing sandbox (anywhere but macOS) the review refuses to start.
4. Cross-applies: every confirmed reproduction also runs against the other
   arm, so a bug found on one side is checked on both. Then duplicates merge
   across reviewers and arms (same location and similar title, or
   near-identical titles; reports naming different files or functions never
   merge).

One record per task and run is appended to ``.brindle/bench/blind_review.jsonl``:
per arm, the bugs (title, severity, reproduction, who found it, found there or
cross-applied), their count by severity and the findings that didn't
reproduce; per reviewer, the review's own tokens and cost, kept apart from
both arms' cost.
"""

from __future__ import annotations

import argparse
import difflib
import errno
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT = Path(__file__).resolve()


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod   # dataclasses look their module up there
    spec.loader.exec_module(mod)
    return mod


bench = _load("bench_solo_vs_brindle", SCRIPT.parent / "bench_solo_vs_brindle.py")
BenchError = bench.BenchError

REPO = bench.REPO
KITS = REPO / "bench" / "solo_vs_brindle"
OUT = REPO / ".brindle" / "bench" / "blind_review.jsonl"
PRICING = REPO / "src" / "brindle" / "pricing.py"
REVIEWERS = ("opus", "codex")
OPUS_MODEL = "claude-opus-5-5"
BUG_EXIT = 3
REPRO_NAME = "repro.py"
SEVERITIES = ("critical", "high", "medium", "low")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
REPRO_SECONDS = 120.0
REVIEW_SECONDS = 3600.0
DROPPED_DIRS = {".git", ".brindle", ".claude", ".codex", ".venv", "bench_hidden_tests", "__pycache__",
                ".pytest_cache"}
DROPPED_FILES = {".mcp.json", "HISTORY_SPEC.md"}
COMMON_BRANCHES = {"bench", "main", "master", "HEAD"}   # shared by both arms: they reveal nothing
REDACTED = "[redacted]"
STOP_WORDS = {"a", "an", "the", "of", "on", "in", "to", "for", "and", "or", "is", "are", "that", "which",
              "when", "with", "by", "it", "its", "be", "not", "no"}

FINDINGS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "severity", "location", "description", "reproduction"],
        "properties": {"title": {"type": "string"}, "severity": {"type": "string", "enum": list(SEVERITIES)},
                       "location": {"type": "string"}, "description": {"type": "string"},
                       "reproduction": {"type": "string"}}}}}}

REVIEW_PROMPT = """You are reviewing a change to a Python repository for real bugs. The working
directory is the repository after the change: read whatever you need, but change nothing. Below
are the task the change was written for and the change itself, as a diff against the base commit.

Look for bugs in the behaviour the change adds or alters: wrong results, lost or corrupted data,
crashes, and places where it breaks the task's spec. Skip style, naming and missing tests unless
they hide a bug.

For each bug, write a standalone Python reproduction script. It runs as `python repro.py` from
the repository root, with the repository's own interpreter and installed dependencies, in a
throwaway copy of the repository, without network access, for at most <<SECONDS>> seconds. It must:
- exit with code 3 when the bug is present, and with code 0 when the code behaves correctly;
- catch the failure it looks for itself: any other exit, such as an uncaught exception, counts as
  inconclusive and the finding is dropped;
- use only the public Python API or command line the code offers, and create any files it needs
  in the current directory or a tempfile directory.

Answer with JSON only, in this shape:
{"findings": [{"title": "...", "severity": "critical|high|medium|low",
  "location": "path/to/file.py:function", "description": "...", "reproduction": "<the script>"}]}
Severity: critical = data lost or corrupted; high = wrong results or a crash on ordinary input;
medium = wrong on edge cases; low = minor. Answer {"findings": []} if you find none.

<task>
<<SPEC>>
</task>

<diff>
<<DIFF>>
</diff>
"""


# -- inputs -----------------------------------------------------------------------------------------


def find_kit(task: str, kits: Path = KITS) -> Path:
    """The task kit whose ``repo.json`` names ``task`` (an unnamed kit is tinydb)."""
    for repo_json in sorted(kits.glob("*/repo.json")):
        if json.loads(repo_json.read_text()).get("name", "tinydb") == task:
            return repo_json.parent
    raise BenchError(f"no task kit named {task!r} under {kits} (pass --task-dir)")


def pick_records(records: list[dict], task: str, run_id: str) -> dict[str, dict]:
    """Per arm, the latest record of ``task`` and ``run_id``."""
    out: dict[str, dict] = {}
    for r in records:
        if r.get("task", "tinydb") == task and str(r.get("run_id")) == str(run_id) and r.get("arm") in bench.ARMS:
            out[r["arm"]] = r
    missing = [a for a in bench.ARMS if not (out.get(a) or {}).get("checkout")]
    if missing:
        raise BenchError(f"no record with a checkout for task {task!r} run {run_id}: {', '.join(missing)}")
    return {a: out[a] for a in bench.ARMS}


# -- blinding ---------------------------------------------------------------------------------------


def git(cwd: Path, *args: str, env: dict, binary: bool = False):
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=not binary,
                         stdin=subprocess.DEVNULL, check=False)
    if res.returncode != 0:
        err = res.stderr if isinstance(res.stderr, str) else res.stderr.decode(errors="replace")
        raise BenchError(f"git {args[0]} failed in {cwd}: {err.strip()[-500:]}")
    return res.stdout


def kept(path: str, base_paths: set[str]) -> bool:
    """Whether a file of the arm's checkout goes into its blinded tree."""
    parts = Path(path).parts
    if not parts or any(p in DROPPED_DIRS for p in parts) or parts[-1] in DROPPED_FILES:
        return False
    # a top-level Markdown file the arm added is a planning note, not part of the change
    return not (len(parts) == 1 and path.lower().endswith(".md") and path not in base_paths)


def redact(text: str, words) -> str:
    for w in sorted({w for w in words if w}, key=len, reverse=True):
        text = re.sub(re.escape(w), REDACTED, text, flags=re.IGNORECASE)
    return text


def redact_bytes(data: bytes, words) -> bytes:
    """``data`` with ``words`` redacted if it is UTF-8 text (binary is left alone)."""
    if not words:
        return data
    try:
        return redact(data.decode("utf-8"), words).encode("utf-8")
    except UnicodeDecodeError:
        return data


def place_link(dest: Path, rel: str, target: str, checkout: Path, words=()) -> None:
    """Create the symlink ``rel`` -> ``target`` in ``dest`` only if, on its
    own, it stays inside ``dest`` (``confine_links`` then checks chains
    through other links): an absolute target inside ``checkout`` is rebased onto
    its copy in ``dest``; any other target outside the tree (absolute, or
    climbing out with ``..``) leaves the link out, so nothing read through a
    link in ``dest`` comes from the original checkout or anywhere else."""
    link = dest / redact(rel, words)
    if os.path.isabs(target):
        try:
            inside = Path(os.path.realpath(target)).relative_to(os.path.realpath(checkout))
        except ValueError:
            return
        if str(inside) == ".":
            return
        target = os.path.relpath(dest / redact(str(inside), words), link.parent)
    else:
        resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
        if resolved == ".." or resolved.startswith("../") or os.path.isabs(resolved):
            return
        target = redact(target, words)
    if not real_dirs(dest, link.parent):
        raise BenchError(f"{link.relative_to(dest)}'s directory is not a plain directory in the tree")
    os.symlink(target, link)


def real_dirs(root: Path, path: Path) -> bool:
    """Whether every component from ``root`` down to ``path`` exists as a
    plain directory (never a link), checked without following links."""
    cur = root
    for part in path.relative_to(root).parts:
        cur = cur / part
        try:
            if not stat.S_ISDIR(os.lstat(cur).st_mode):
                return False
        except OSError:
            return False
    return True


def path_key(path: str) -> str:
    """``path`` as a case-insensitive, normalization-insensitive filesystem
    (macOS's default) sees it: two paths with one key are one file there."""
    return unicodedata.normalize("NFC", path).casefold()


def confine_links(root: Path) -> None:
    """Remove every symlink under ``root`` (``.git`` aside) that, resolved the
    way the filesystem resolves it (through any other links on the way),
    ends outside ``root`` or loops. Removing one can change how another
    resolves, so it repeats until nothing changes. A link that only dangles
    inside ``root`` stays: reading it fails, it leaks nothing."""
    top = os.path.realpath(root)
    changed = True
    while changed:
        changed = False
        for here, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (here == str(root) and d == ".git")]
            for name in [*dirs, *files]:
                p = os.path.join(here, name)
                if not os.path.islink(p):
                    continue
                try:
                    os.stat(p)
                    loops = False
                except OSError as e:
                    loops = e.errno == errno.ELOOP
                real = os.path.realpath(p)
                if loops or (real != top and not real.startswith(top + os.sep)):
                    os.unlink(p)
                    changed = True


def write_tree(dest: Path, files: list, links: list, checkout: Path, words=()) -> None:
    """Write ``files`` ((path, mode, read) triples) and ``links`` ((path,
    target) pairs) into ``dest``, which holds no link yet. Every final
    (redacted) path is checked first: two entries that redact to one path, or
    one that would sit under another, stop it before anything is written.
    Regular files come next, each a new file in freshly made directories, so
    no write can pass through a link. Links come last (``place_link``), and
    ``confine_links`` checks them all together. Nothing is written once a
    link exists."""
    final = [redact(rel, words) for rel, _, _ in files] + [redact(rel, words) for rel, _ in links]
    taken: dict[str, str] = {}   # path_key -> path: compared as the filesystem may compare them
    for out in final:
        parts = Path(out).parts
        if not parts or os.path.isabs(out) or ".." in parts:
            raise BenchError(f"a path outside the tree: {out!r}")
        if path_key(out) in taken:
            raise BenchError(f"{taken[path_key(out)]!r} and {out!r} are one path once redacted")
        taken[path_key(out)] = out
    for out in taken.values():
        parts = Path(out).parts
        for i in range(1, len(parts)):
            if path_key(str(Path(*parts[:i]))) in taken:
                raise BenchError(f"{out!r} would sit under {str(Path(*parts[:i]))!r} once redacted")
    # every directory, for files and links alike, is made while the tree still has no link
    for rel in [r for r, _, _ in files] + [r for r, _ in links]:
        (dest / redact(rel, words)).parent.mkdir(parents=True, exist_ok=True)
    for rel, mode, read in files:
        out = dest / redact(rel, words)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(out, flags, 0o755 if mode & 0o111 else 0o644), "wb") as f:
            f.write(redact_bytes(read(), words))
    for rel, target in links:   # each checks that no directory on its way is a link
        place_link(dest, rel, target, checkout, words)
    confine_links(dest)


def clear_tree(dest: Path) -> None:
    """Remove everything in ``dest`` but ``.git``, never following a link."""
    for name in os.listdir(dest):
        p = dest / name
        if name == ".git":
            continue
        if p.is_symlink() or not p.is_dir():
            p.unlink()
        else:
            shutil.rmtree(p)


def blind_tree(checkout: Path, base: str, dest: Path, env: dict, words=()) -> str:
    """Build the blinded repo in ``dest`` (see the module doc) and return its
    staged diff against the base commit. With ``words``, they are redacted in
    every text file's content, every path and every link as it is written,
    so neither the working tree, the index nor the history ever holds them.
    Both the base snapshot and the arm's files go in through ``write_tree``
    (collisions refused, files before links, links confined as a whole), so
    no write ever passes through a link and no link resolves outside ``dest``."""
    archive = git(checkout, "archive", "--format=tar", base, env=env, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = tar.getmembers()
        write_tree(dest, [(m.name, m.mode, lambda m=m: tar.extractfile(m).read()) for m in members if m.isfile()],
                   [(m.name, m.linkname) for m in members if m.issym()], checkout, words)
    base_paths = set(git(checkout, "ls-tree", "-r", "-z", "--name-only", base, env=env).split("\0")) - {""}
    ident = ["-c", "user.name=review", "-c", "user.email=review@localhost", "-c", "commit.gpgsign=false"]
    git(dest, "init", "-q", "-b", "review", env=env)
    git(dest, "add", "-A", env=env)
    git(dest, *ident, "commit", "-q", "--no-verify", "--allow-empty", "-m", "base", env=env)
    clear_tree(dest)
    files, links = [], []
    for rel in sorted({f for f in git(checkout, "ls-files", "-z", "-co", "--exclude-standard", env=env).split("\0")
                       if f}):
        src = checkout / rel
        if not kept(rel, base_paths):
            continue
        if src.is_symlink():
            links.append((rel, os.readlink(src)))
        elif src.is_file():
            files.append((rel, src.stat().st_mode, src.read_bytes))
    write_tree(dest, files, links, checkout, words)
    git(dest, "add", "-A", env=env)
    return git(dest, "diff", "--cached", "--no-color", "--no-ext-diff", env=env)


def branch(checkout: Path, env: dict) -> str:
    try:
        return git(checkout, "rev-parse", "--abbrev-ref", "HEAD", env=env).strip()
    except BenchError:
        return ""


# -- reviewers --------------------------------------------------------------------------------------


def review_prompt(spec: str, diff: str, seconds: float) -> str:
    return (REVIEW_PROMPT.replace("<<SECONDS>>", f"{seconds:.0f}")
            .replace("<<SPEC>>", spec.strip()).replace("<<DIFF>>", diff))


def _json_in(text: str):
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    for candidate in (text, fenced.group(1) if fenced else None, text[text.find("{"):text.rfind("}") + 1]):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def parse_findings(payload) -> list[dict] | None:
    """A reviewer's findings from its JSON answer (a dict, or text holding
    one); None when the answer holds no findings list at all."""
    if isinstance(payload, str):
        payload = _json_in(payload)
    items = payload.get("findings") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    out = []
    for f in items:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "").strip().lower()
        out.append({"title": str(f.get("title") or "untitled").strip(),
                    "severity": sev if sev in SEV_RANK else "low",
                    "location": str(f.get("location") or "").strip(),
                    "description": str(f.get("description") or "").strip(),
                    "reproduction": f.get("reproduction") if isinstance(f.get("reproduction"), str) else ""})
    return out


def _tail(text: str | None, n: int = 2000) -> str:
    return (text or "")[-n:]


def _call_claude(model: str, prompt: str, workdir: Path, env: dict, seconds: float) -> dict:
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "Read,Grep,Glob",
           "--permission-prompts", "none", "--safe-mode", "--no-session-persistence",
           "--json-schema", json.dumps(FINDINGS_SCHEMA)]
    res = subprocess.run(cmd, input=prompt, cwd=workdir, env=env, capture_output=True, text=True,
                         check=False, timeout=seconds)
    obj = _json_in(res.stdout)
    if not isinstance(obj, dict):
        obj = next((e for e in reversed(bench.iter_json(res.stdout.splitlines())) if e.get("type") == "result"), {})
    by_model = bench.cli_usage(obj.get("modelUsage") or {})
    structured = obj.get("structured_output")
    findings = parse_findings(structured if isinstance(structured, dict) else obj.get("result"))
    error = None
    if res.returncode != 0 or obj.get("is_error") or findings is None:
        error = f"claude exit {res.returncode}: {_tail(res.stderr or res.stdout, 500)}"
    return {"findings": findings or [], "by_model": by_model, "error": error,
            "cli_cost_usd": obj.get("total_cost_usd")}


def codex_default_model() -> str | None:
    """The model ``codex exec`` uses when given none: its config's ``model``."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        import tomllib
        return tomllib.loads((home / "config.toml").read_text()).get("model")
    except (OSError, ValueError, ImportError):
        return None


def _call_codex(model: str | None, prompt: str, workdir: Path, scratch: Path, env: dict,
                seconds: float) -> dict:
    schema = scratch / "findings.schema.json"
    schema.write_text(json.dumps(FINDINGS_SCHEMA))
    fd, last_name = tempfile.mkstemp(prefix="codex-answer-", suffix=".txt", dir=scratch)
    os.close(fd)
    last = Path(last_name)
    cmd = ["codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
           "--cd", str(workdir), "--output-last-message", str(last), "--output-schema", str(schema)]
    if model:
        cmd += ["--model", model]
    cmd.append("-")
    res = subprocess.run(cmd, input=prompt, cwd=workdir, env=env, capture_output=True, text=True,
                         check=False, timeout=seconds)
    inp = cached = out = 0
    for e in bench.iter_json(res.stdout.splitlines()):
        u = e.get("usage") if e.get("type") == "turn.completed" else None
        if isinstance(u, dict):
            inp += int(u.get("input_tokens") or 0)
            cached += int(u.get("cached_input_tokens") or 0)
            out += int(u.get("output_tokens") or 0)
    name = _pricing().normalize(model or codex_default_model() or "codex")
    by_model = {}
    if inp or out:   # OpenAI's input count includes the cached part
        bench.add_usage(by_model, name, {"input": max(inp - cached, 0), "output": out, "cache_read": cached})
    findings = parse_findings(last.read_text() if last.exists() else "")
    error = None
    if res.returncode != 0 or findings is None:
        error = f"codex exit {res.returncode}: {_tail(res.stderr or res.stdout, 500)}"
    return {"findings": findings or [], "by_model": by_model, "error": error}


def call_reviewer(name: str, model: str | None, prompt: str, workdir: Path, scratch: Path, env: dict,
                  seconds: float) -> dict:
    """Run one reviewer on one blinded arm: {"findings", "by_model", "error"}."""
    try:
        if name == "opus":
            return _call_claude(model, prompt, workdir, env, seconds)
        return _call_codex(model, prompt, workdir, scratch, env, seconds)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"findings": [], "by_model": {}, "error": f"{name}: {type(e).__name__}: {e}"}


_PRICING = []


def _pricing():
    """brindle's price table (src/brindle/pricing.py, stdlib only), loaded by path."""
    if not _PRICING:
        _PRICING.append(_load("brindle_pricing", PRICING))
    return _PRICING[0]


def price(by_model: dict) -> tuple[float, dict, list[str]]:
    rates = {m: [p.input, p.output, p.cache_write, p.cache_read] for m, p in _pricing().PRICES.items()}
    try:
        return bench.price_usage(by_model, rates)
    except bench.UnknownModel as e:
        return 0.0, {}, [str(e)]


def review_costs(calls: dict[tuple[str, str], dict], models: dict[str, str | None]) -> dict[str, dict]:
    """Per reviewer: its tokens and cost over both arms, and per arm."""
    out = {}
    for name in REVIEWERS:
        total: dict[str, dict[str, int]] = {}
        per_arm = {}
        for (arm, who), call in calls.items():
            if who != name:
                continue
            bench.merge(total, call["by_model"])
            cost, _, unpriced = price(call["by_model"])
            per_arm[arm] = {"tokens_total": bench.tokens_total(call["by_model"]), "cost_usd": cost,
                            "unpriced": unpriced, "findings": len(call["findings"]), "error": call.get("error")}
        cost, per_model, unpriced = price(total)
        out[name] = {"model": models.get(name), "tokens": total, "tokens_total": bench.tokens_total(total),
                     "cost_usd": cost, "cost_by_model": per_model, "unpriced": unpriced, "per_arm": per_arm}
    return out


# -- reproductions ----------------------------------------------------------------------------------


def sandbox_profile(root: Path) -> str:
    """A sandbox-exec profile that allows everything but writes outside ``root``."""
    if '"' in str(root):
        raise BenchError(f"can't sandbox a path with a quote in it: {root}")
    return ("(version 1)\n(allow default)\n(deny file-write*)\n"
            f'(allow file-write* (subpath "{root}") (literal "/dev/null") (literal "/dev/tty")'
            ' (literal "/dev/dtracehelper") (regex #"^/dev/fd/"))\n')


def can_sandbox() -> bool:
    """Whether reproductions can run confined: only sandbox-exec (macOS) is supported."""
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


NO_SANDBOX = ("reproductions need an enforcing sandbox (sandbox-exec, macOS) so they can't write outside "
              "their temp copy; none is available here, so none is run")


def write_new(path: Path, text: str) -> None:
    """Write ``path`` as a new regular file, never through a symlink or onto
    something the copied tree already had there."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def run_repro(tree: Path, python: Path, code: str, scratch: Path, seconds: float) -> dict:
    """Run one reproduction in a fresh copy of ``tree`` under sandbox-exec,
    which denies writes outside the copy: {"status": "bug" | "ok" |
    "inconclusive", "exit", "output"}. Its HOME, TMPDIR and working directory
    are inside the copy, and the copy is removed afterwards. Without a
    sandbox it raises before writing or running anything."""
    if not can_sandbox():
        raise BenchError(NO_SANDBOX)
    run = Path(tempfile.mkdtemp(prefix="repro-", dir=scratch)).resolve()
    python = Path(os.path.abspath(python))   # absolute, but not resolved: the venv is found from its path
    try:
        work = run / "repo"
        shutil.copytree(tree, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        for d in ("home", "tmp"):
            (run / d).mkdir()
        write_new(work / REPRO_NAME, code)
        env = {"PATH": os.pathsep.join([str(python.parent), "/usr/bin", "/bin"]), "HOME": str(run / "home"),
               "TMPDIR": str(run / "tmp"), "PYTHONPATH": str(work), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONNOUSERSITE": "1", "LANG": "en_US.UTF-8"}
        cmd = ["sandbox-exec", "-p", sandbox_profile(run), str(python), "-B", REPRO_NAME]
        proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc.pid)
            output, _ = proc.communicate()
        _kill_group(proc.pid)   # anything it left running
        rc = proc.returncode
        status = "inconclusive" if timed_out else "bug" if rc == BUG_EXIT else "ok" if rc == 0 else "inconclusive"
        return {"status": status, "exit": None if timed_out else rc,
                "output": _tail(output) + (f"\n[stopped after {seconds:.0f}s]" if timed_out else "")}
    finally:
        shutil.rmtree(run, ignore_errors=True)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# -- verifying, merging, cross-applying -------------------------------------------------------------


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)]
    return word


def _words(title: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9_]+", title.lower()) if w not in STOP_WORDS}


def _location(loc: str) -> tuple[str, str]:
    """(file, symbol) of a finding's location, line numbers dropped."""
    parts = [p.strip() for p in re.split(r"::|:|#", loc.strip().lower().removeprefix("./")) if p.strip()]
    parts = [p for p in parts if not re.fullmatch(r"(line\s*)?\d+(-\d+)?", p)]
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def compatible(a: dict, b: dict) -> bool:
    """Whether two findings' known locations can be one place: not another
    file, and not another function in the same file."""
    (fa, sa), (fb, sb) = _location(a["location"]), _location(b["location"])
    return not ((fa and fb and fa != fb) or (sa and sb and sa != sb))


def same_bug(a: dict, b: dict) -> bool:
    """Whether two confirmed findings report one bug. Reports whose known
    locations differ (another file, or another function in the same file)
    are always kept apart, however alike their titles."""
    if not compatible(a, b):
        return False
    (fa, _), (fb, _) = _location(a["location"]), _location(b["location"])
    wa, wb = _words(a["title"]), _words(b["title"])
    overlap = len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0
    ratio = difflib.SequenceMatcher(None, a["title"].lower(), b["title"].lower()).ratio()
    if fa and fa == fb:
        return overlap >= 0.5 or ratio >= 0.6
    return ratio >= 0.9 or (overlap >= 0.8 and min(len(wa), len(wb)) >= 3)


def merge_duplicates(findings: list[dict]) -> list[list[dict]]:
    """Group findings that report one bug. Two groups join only if every
    member of one is location-compatible with every member of the other, so
    a report naming only a file can't bridge two different functions."""
    group_of = list(range(len(findings)))
    groups: dict[int, list[int]] = {i: [i] for i in range(len(findings))}
    for i in range(len(findings)):
        for j in range(i + 1, len(findings)):
            gi, gj = group_of[i], group_of[j]
            if gi == gj or not same_bug(findings[i], findings[j]):
                continue
            if all(compatible(findings[a], findings[b]) for a in groups[gi] for b in groups[gj]):
                for b in groups.pop(gj):
                    group_of[b] = gi
                    groups[gi].append(b)
    return [[findings[i] for i in sorted(members)] for members in groups.values()]


def assess(found: dict[str, list[dict]], reproduce) -> dict[str, dict]:
    """Per arm, its confirmed bugs. ``found``: per arm, the reviewers' findings
    (each with its ``reviewer``); ``reproduce(arm, code)`` runs a reproduction
    against that arm. A finding counts only if its reproduction shows the bug
    in the arm it was found in. Every confirmed reproduction then runs against
    every other arm too, before confirmed findings that are the same bug merge
    (across reviewers and arms); a merged bug counts in an arm it wasn't found
    in if any of its reproductions shows it there."""
    cache: dict[tuple[str, str], dict] = {}

    def check(arm: str, code: str) -> dict:
        if (arm, code) not in cache:
            cache[(arm, code)] = (reproduce(arm, code) if code.strip()
                                  else {"status": "inconclusive", "exit": None, "output": "no reproduction"})
        return cache[(arm, code)]

    confirmed: list[dict] = []
    out = {arm: {"bugs": [], "unconfirmed": [], "not_in_this_arm": []} for arm in found}
    for arm, findings in found.items():
        for f in findings:
            res = check(arm, f["reproduction"])
            if res["status"] == "bug":
                confirmed.append({**f, "arm": arm})
            else:
                out[arm]["unconfirmed"].append({k: f.get(k) for k in ("title", "severity", "location", "reviewer")}
                                               | {"status": res["status"], "exit": res.get("exit"),
                                                  "output": _tail(res.get("output"), 500)})
    for f in confirmed:   # cross-apply every confirmed reproduction before anything merges
        for arm in found:
            check(arm, f["reproduction"])
    for group in merge_duplicates(confirmed):
        lead = min(group, key=lambda f: SEV_RANK.get(f["severity"], len(SEVERITIES)))
        found_in = sorted({f["arm"] for f in group}, key=list(found).index)
        common = {"title": lead["title"], "severity": lead["severity"], "location": lead["location"],
                  "description": lead.get("description", ""),
                  "found_by": sorted({f["reviewer"] for f in group}), "found_in": found_in,
                  "also_reported_as": sorted({f["title"] for f in group} - {lead["title"]})}
        for arm in found:
            if arm in found_in:
                hit, how = next(f for f in group if f["arm"] == arm), "found"
            else:
                hit, how = next((f for f in group if check(arm, f["reproduction"])["status"] == "bug"), None), "cross-applied"
            if hit is None:
                out[arm]["not_in_this_arm"].append({"title": lead["title"], "found_in": found_in, "status": sorted(
                    {check(arm, f["reproduction"])["status"] for f in group})})
                continue
            out[arm]["bugs"].append({**common, "reproduction": hit["reproduction"], "how": how})
    for arm, o in out.items():
        o["bugs"].sort(key=lambda b: (SEV_RANK.get(b["severity"], len(SEVERITIES)), b["title"]))
        o["by_severity"] = {s: sum(b["severity"] == s for b in o["bugs"]) for s in SEVERITIES}
    return out


# -- review -----------------------------------------------------------------------------------------


def cmd_review(args) -> int:
    arms = pick_records(bench.load_records(Path(args.results)), args.task, args.run_id)
    kit = Path(args.task_dir) if args.task_dir else find_kit(args.task)
    base = json.loads((kit / "repo.json").read_text())["commit"]
    spec = (kit / "spec.md").read_text()
    env = bench.clean_env()
    if not can_sandbox():   # before any reviewer spends tokens
        raise BenchError(NO_SANDBOX)
    scratch = (Path(args.scratch).resolve() / f"{args.task}-{args.run_id}-{time.strftime('%Y%m%d-%H%M%S')}")
    if scratch.is_relative_to(REPO):
        raise BenchError(f"the scratch directory must be outside this repo: {scratch}")
    scratch.mkdir(parents=True)
    models = {"opus": args.opus_model, "codex": args.codex_model or codex_default_model()}
    try:
        checkouts = {arm: Path(r["checkout"]) for arm, r in arms.items()}
        pythons = {arm: co / ".venv" / "bin" / "python" for arm, co in checkouts.items()}
        for arm, py in pythons.items():
            if not py.exists():
                raise BenchError(f"{arm}: no {py} (the checkout's .venv runs the reproductions)")
        words = ["brindle", *(b for co in checkouts.values() if (b := branch(co, env)) not in COMMON_BRANCHES)]
        # reviewers read a redacted tree; reproductions run on the arm's files as written
        trees, run_trees, diffs = {}, {}, {}
        for arm, co in checkouts.items():
            trees[arm] = Path(tempfile.mkdtemp(prefix="change-", dir=scratch))
            diffs[arm] = blind_tree(co, base, trees[arm], env, words)
            run_trees[arm] = Path(tempfile.mkdtemp(prefix="exec-", dir=scratch))
            blind_tree(co, base, run_trees[arm], env)
        print(f"reviewing {', '.join(f'{a} ({len(d.splitlines())} diff lines)' for a, d in diffs.items())}",
              file=sys.stderr)
        jobs = [(arm, name) for arm in arms for name in REVIEWERS]
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {(arm, name): pool.submit(call_reviewer, name, models[name],
                                                review_prompt(spec, diffs[arm], args.repro_seconds),
                                                trees[arm], scratch, env, args.review_seconds)
                       for arm, name in jobs}
            calls = {job: f.result() for job, f in futures.items()}
        found = {arm: [{**f, "reviewer": name} for name in REVIEWERS for f in calls[(arm, name)]["findings"]]
                 for arm in arms}
        result = assess(found, lambda arm, code: run_repro(run_trees[arm], pythons[arm], code, scratch,
                                                           args.repro_seconds))
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
    costs = review_costs(calls, models)
    record = {"task": args.task, "run_id": str(args.run_id), "reviewed_at": time.time(), "base_commit": base,
              "sandbox": "sandbox-exec", "repro_seconds": args.repro_seconds,
              "arms": {arm: {"checkout": str(checkouts[arm]), "run_valid": r.get("valid", True),
                             "pass_rate": r.get("pass_rate"), "diff_lines": len(diffs[arm].splitlines()),
                             "reviewer_errors": {n: calls[(arm, n)]["error"] for n in REVIEWERS
                                                 if calls[(arm, n)].get("error")},
                             **result[arm]}
                       for arm, r in arms.items()},
              "reviewers": costs,
              "review_tokens_total": sum(c["tokens_total"] for c in costs.values()),
              "review_cost_usd": round(sum(c["cost_usd"] for c in costs.values()), 6)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(record) + "\n")
    for arm, a in record["arms"].items():
        counts = ", ".join(f"{n} {s}" for s, n in a["by_severity"].items() if n) or "none"
        print(f"{arm}: {len(a['bugs'])} bugs ({counts}); {len(a['unconfirmed'])} findings didn't reproduce"
              + (f"; reviewer errors: {a['reviewer_errors']}" if a["reviewer_errors"] else ""))
    print(f"review: {record['review_tokens_total']:,} tokens, ${record['review_cost_usd']:,.2f} "
          f"(not part of either arm's cost)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rev = sub.add_parser("review", help="blind-review both arms of one task and run",
                         description="Blind both arms' final diffs, have Opus and Codex review each, keep only "
                                     "findings whose reproduction shows the bug, merge duplicates, check each "
                                     "bug in both arms and append a record to the results file.")
    rev.add_argument("--task", required=True, help="the kit's name (repo.json 'name'; unnamed kits are tinydb)")
    rev.add_argument("--run-id", required=True)
    rev.add_argument("--task-dir", help=f"the task kit (default: the kit under {KITS.relative_to(REPO)} "
                                        "named --task)")
    rev.add_argument("--results", default=str(bench.RESULTS), help="the benchmark's records")
    rev.add_argument("--out", default=str(OUT), help="where the review record is appended")
    rev.add_argument("--scratch", default=str(Path(tempfile.gettempdir()) / "bench_blind_review"),
                     help="where blinded copies and reproductions run (outside this repo)")
    rev.add_argument("--opus-model", default=OPUS_MODEL, help="the Claude reviewer's model")
    rev.add_argument("--codex-model", help="the Codex reviewer's model (default: Codex's configured one)")
    rev.add_argument("--repro-seconds", type=float, default=REPRO_SECONDS,
                     help="time limit for each reproduction run")
    rev.add_argument("--review-seconds", type=float, default=REVIEW_SECONDS,
                     help="time limit for each reviewer run")
    rev.add_argument("--jobs", type=int, default=len(REVIEWERS) * len(bench.ARMS),
                     help="reviewer runs at once")
    rev.add_argument("--keep", action="store_true", help="keep the scratch directory afterwards")
    args = ap.parse_args(argv)
    try:
        return cmd_review(args)
    except BenchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
