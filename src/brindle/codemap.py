"""A code map for agents: the repo's graphify knowledge graph, if it has one.

Finding the code a task touches is much of what an agent spends tokens on:
listing folders, grepping, reading whole files to find one function.
``graphify query "<question>"`` answers from a prebuilt graph of the repo
instead, listing the relevant symbols with file and line, in well under a
second. When the repo has ``graphify-out/graph.json`` and graphify is
installed, brindle tells its agents to ask the graph first and read only what
it points to.

Agents query the graph in the checkout of their workspace's base branch
(falling back to the main checkout). When a session checkout has no graph,
brindle seeds it from the store and runs ``graphify update <checkout> --force``
in the background. After each merge brindle refreshes the graph of the checkout
the merge landed in with ``--force``, carries community names over without an
LLM, and copies the result to the store.

``graphify`` in ``.brindle/config.json``: true or false to force it on or off;
unset, it's on whenever the graph and the command are there.
``graphify_auto_refresh: false`` disables automatic rebuilds but keeps queries.
Automatic rebuilds always skip tracked graphify-out/ artifacts: a code-only
update can discard semantic nodes and rewrite the report, HTML and manifest.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

from brindle import spawn

if TYPE_CHECKING:
    from brindle.db import Workspace

GRAPH =Path("graphify-out") / "graph.json"
# Read-only graphify commands, pre-approved for Claude agents.
ALLOWED_TOOLS = ["Bash(graphify query:*)", "Bash(graphify explain:*)", "Bash(graphify path:*)",
                 "Bash(graphify affected:*)"]
MAP_TOP = 10
STORE_RETENTION_DAYS = 30
_QUERY_RE = re.compile(r"^\s*graphify\s+(?:query|explain|path|affected)\b")
_map_cache: dict[str, tuple[float, str]] = {}   # graph path -> (mtime, rendered map)
_refresh_lock = threading.RLock()


def store_path(repo_root: str | Path) -> Path:
    """The store path for the repo's graph: brindle_home()/graphs/<repo name>/graph.json."""
    from brindle.config import brindle_home

    repo_name = Path(repo_root).name
    return brindle_home() / "graphs" / repo_name / "graph.json"


def prune_stores(now: float | None = None) -> int:
    """Remove graph stores whose graph (or store directory) is over 30 days old."""
    from brindle.config import brindle_home

    root = brindle_home() / "graphs"
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - STORE_RETENTION_DAYS * 24 * 60 * 60
    removed = 0
    for store in root.iterdir():
        if not store.is_dir():
            continue
        graph = store / "graph.json"
        try:
            modified = (graph if graph.exists() else store).stat().st_mtime
        except OSError:
            continue
        if modified < cutoff:
            shutil.rmtree(store, ignore_errors=True)
            if not store.exists():
                removed += 1
    return removed


def base_checkout(target: Workspace | str | Path) -> str:
    """The checkout holding the base branch for target (or repo_root)."""
    if isinstance(target, (str, Path)):
        return str(target)
    ws = target
    if ws.base_branch:
        from brindle import git

        try:
            wt = git.worktree_for_branch(ws.repo_root, ws.base_branch)
        except git.GitError:
            wt = None
        if wt:
            p = Path(wt) / GRAPH
            if not p.is_file():
                seed_session(wt, ws.repo_root)
            if p.is_file():
                return wt
    if getattr(ws, "path", None) and (Path(ws.path) / GRAPH).is_file():
        return ws.path
    return ws.repo_root


def graph_path(target: Workspace | str | Path) -> Path | None:
    """The repo's graph, if agents should use it."""
    from brindle.config import load_repo_config

    checkout = base_checkout(target)
    try:
        setting = load_repo_config(checkout).graphify
    except ValueError:
        setting = None
    path = Path(checkout) / GRAPH
    if setting is False or not path.is_file() or not shutil.which("graphify"):
        return None
    return path


def guidance(target: Workspace | str | Path) -> str | None:
    """What to tell an agent about the code map, or None if there isn't one."""
    path = graph_path(target)
    if path is None:
        return None
    checkout = path.parent.parent
    return (
        "Code map: this repo has a graphify knowledge graph. To find where something "
        "lives or what calls what, ask it before listing folders, grepping or reading "
        f"whole files: `graphify query \"<question>\" --graph {path} --budget 1500` lists "
        "the relevant functions and classes with file and line (`graphify explain "
        f"\"<name>\" --graph {path}` for one symbol). Then read just those lines. File "
        "paths it prints are relative to the repo root: open them in your own working "
        f"directory, not under {checkout}, which may be a different checkout. The map "
        "can lag behind the code a little, so trust the files when they differ."
    )


def _render_map(graph: dict) -> str:
    nodes = graph.get("nodes", [])
    sizes: Counter = Counter()
    names: dict = {}
    for n in nodes:
        c = n.get("community")
        if c is not None:
            sizes[c] += 1
            names.setdefault(c, n.get("community_name") or str(c))
    degree: Counter = Counter()
    for e in graph.get("links", graph.get("edges", [])):
        degree[e.get("source")] += 1
        degree[e.get("target")] += 1
    by_id = {n.get("id"): n for n in nodes}
    hubs = [by_id[i] for i, _ in degree.most_common() if i in by_id and by_id[i].get("source_file")]
    lines = ["Communities (by size): " + ", ".join(f"{names[c]} ({k})" for c, k in sizes.most_common(MAP_TOP))]
    lines.append("Most-connected: " + ", ".join(
        f"{n.get('label', n['id'])} {n['source_file']}:{str(n.get('source_location', '')).lstrip('L')}"
        for n in hubs[:MAP_TOP]))
    return "\n".join(lines)


def supervisor_map(target: Workspace | str | Path) -> str | None:
    """A compact repo map for a supervisor's prompt, built from the graph (cached
    per graph mtime), with how to use the graph to plan tasks. None without a graph."""
    path = graph_path(target)
    if path is None:
        return None
    key, mtime = str(path), path.stat().st_mtime
    cached = _map_cache.get(key)
    if cached is None or cached[0] != mtime:
        try:
            body = _render_map(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, AttributeError):
            return None
        _map_cache[key] = cached = (mtime, body)
    return (
        f"Repo map:\n{cached[1]}\n"
        "Before assigning, fill each task's `files` with `graphify affected \"<symbol>\" "
        f"--graph {path}` (what a change to a symbol reaches) and `graphify query` "
        "(where something lives), and use them to spot tasks that overlap."
    )


def _automatic_rebuild_allowed(checkout: str | Path) -> bool:
    """Only rebuild disposable graphs, and only when the repo permits it.

    Checking before graphify starts avoids even transient changes to tracked
    artifacts that a concurrent worker commit could pick up. We cannot know
    the rebuilt node count before running graphify, so leave all tracked
    graphs to explicit updates by the user, including partially tracked output.
    """
    from brindle import git
    from brindle.config import load_repo_config

    try:
        cfg = load_repo_config(checkout)
    except ValueError:
        # An unreadable config must not start an unsolicited writer.
        return False
    if cfg.graphify is False or cfg.graphify_auto_refresh is False:
        return False
    try:
        tracked = git.paths(["ls-files", str(GRAPH.parent)], checkout)
    except git.GitError:
        # If tracking cannot be determined, preserve the checkout.
        return False
    return not tracked


def seed_session(checkout: str | Path, repo_root: str | None = None) -> bool:
    """Seed a missing graph and the source checkout's cache, then refresh.

    Without a stored graph the repo has not opted in, so leave it alone.
    Returns True if a background update was requested.
    """
    from brindle.config import load_repo_config

    checkout_str = str(checkout)
    if not _automatic_rebuild_allowed(checkout_str):
        return False
    try:
        setting = load_repo_config(checkout_str).graphify
    except ValueError:
        setting = None
    if setting is False or not shutil.which("graphify"):
        return False
    path = Path(checkout_str) / GRAPH
    if path.is_file():
        return False

    if repo_root is None:
        from brindle import git

        try:
            repo_root = git.main_repo_root(checkout_str)
        except Exception:
            repo_root = checkout_str

    stored = store_path(repo_root)
    if not stored.is_file():
        return False
    with _refresh_lock:
        if path.is_file():
            return False
        out = path.parent
        if out.is_symlink() and not out.exists():
            out.unlink()   # the shared folder went away (pruned): link afresh
        # One graph per repo: a session checkout's graphify-out is a link to
        # the repo's shared one rather than a copy of the graph and its cache.
        # A checkout that already has a folder of its own keeps it.
        if not os.path.lexists(out) and os.path.realpath(checkout_str) != os.path.realpath(repo_root):
            out = shared_out(repo_root)
        out.mkdir(parents=True, exist_ok=True)
        cache = Path(repo_root) / GRAPH.parent / "cache"
        if cache.is_dir() and cache.resolve() != (out / "cache").resolve() and not (out / "cache").exists():
            shutil.copytree(cache, out / "cache", dirs_exist_ok=True)
        if not (out / GRAPH.name).is_file():
            shutil.copy2(stored, out / GRAPH.name)
        if out != path.parent:
            from brindle import git

            os.symlink(out, path.parent, target_is_directory=True)
            # graphify-out/ in a .gitignore matches folders only, not a link.
            if (Path(checkout_str) / ".git").exists():
                try:
                    git.exclude_name(checkout_str, GRAPH.parent.as_posix())
                except git.GitError:
                    pass
        refresh_later(checkout_str, repo_root)
    return True


def shared_out(repo_root: str | Path) -> Path:
    """The repo's one graphify-out folder its session checkouts link to."""
    from brindle import build_caches

    return build_caches.repo_cache_dir(repo_root) / GRAPH.parent


def carry_community_names(old_graph: dict | None, new_graph_path: Path) -> dict | None:
    """Carry community names from old_graph over into new_graph at new_graph_path.

    For each new community, take the name of the old community it shares the most
    nodes with; new communities with no overlap get the stem of their most common
    source_file. No LLM.
    """
    if not new_graph_path.is_file():
        return None
    try:
        new_graph = json.loads(new_graph_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    old_node_comm: dict[str, str | int] = {}
    old_comm_names: dict[str | int, str] = {}
    if old_graph and isinstance(old_graph, dict):
        for node in old_graph.get("nodes", []):
            nid = node.get("id")
            comm = node.get("community")
            cname = node.get("community_name")
            if nid and comm is not None:
                old_node_comm[nid] = comm
                if cname and comm not in old_comm_names:
                    old_comm_names[comm] = cname

    new_comm_nodes: dict[str | int, list[dict]] = defaultdict(list)
    for node in new_graph.get("nodes", []):
        comm = node.get("community")
        if comm is not None:
            new_comm_nodes[comm].append(node)

    for comm_id, nodes in new_comm_nodes.items():
        overlap = Counter()
        for node in nodes:
            nid = node.get("id")
            if nid in old_node_comm:
                overlap[old_node_comm[nid]] += 1

        comm_name = None
        if overlap:
            best_old_comm = overlap.most_common(1)[0][0]
            comm_name = old_comm_names.get(best_old_comm)
            if not comm_name:
                comm_name = str(best_old_comm)
        else:
            sf_counts = Counter()
            for node in nodes:
                sf = node.get("source_file")
                if sf:
                    sf_counts[sf] += 1
            if sf_counts:
                most_common_sf = sf_counts.most_common(1)[0][0]
                comm_name = Path(most_common_sf).stem
            else:
                comm_name = str(comm_id)

        for node in nodes:
            node["community_name"] = comm_name

    if isinstance(new_graph.get("communities"), list):
        for entry in new_graph["communities"]:
            if isinstance(entry, dict):
                cid = entry.get("id") if entry.get("id") is not None else entry.get("community")
                if cid in new_comm_nodes:
                    name = new_comm_nodes[cid][0].get("community_name")
                    if name:
                        entry["name"] = name
                        entry["community_name"] = name
    elif isinstance(new_graph.get("communities"), dict):
        for cid, entry in new_graph["communities"].items():
            if cid in new_comm_nodes:
                name = new_comm_nodes[cid][0].get("community_name")
                if name:
                    if isinstance(entry, dict):
                        entry["name"] = name
                        entry["community_name"] = name
                    else:
                        new_graph["communities"][cid] = name

    new_graph_path.write_text(json.dumps(new_graph, indent=2), encoding="utf-8")
    return new_graph


def copy_to_store(graph_path: Path, repo_root: str) -> None:
    """Copy graph_path to the graph store for repo_root."""
    store = store_path(repo_root)
    store.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(graph_path, store)


def refresh(checkout: str, repo_root: str | None = None) -> None:
    """Refresh synchronously, or queue a follow-up if an update is active."""
    _request_refresh(checkout, repo_root, background=False)


def refresh_later(checkout: str, repo_root: str | None = None) -> None:
    """Rebuild the code part of the graph in the background, after a merge.
    Inside ``refreshes_held``, only noted: its caller asks for it later."""
    held = _held.get()
    if held is not None:
        if (checkout, repo_root) not in held:
            held.append((checkout, repo_root))
        return
    _request_refresh(checkout, repo_root, background=True)


_held: contextvars.ContextVar[list[tuple[str, str | None]] | None] = contextvars.ContextVar(
    "brindle_codemap_refreshes_held", default=None)


@contextlib.contextmanager
def refreshes_held() -> Iterator[list[tuple[str, str | None]]]:
    """Hold back the background rebuilds asked for inside (a new session's
    graph, seeded before its supervisor starts: graphify's workers would take
    the cores its launch needs). Yields the (checkout, repo_root) pairs held;
    the caller passes each to refresh_later once its launch is done."""
    outer = _held.get()
    if outer is not None:
        yield outer
        return
    held: list[tuple[str, str | None]] = []
    token = _held.set(held)
    try:
        yield held
    finally:
        _held.reset(token)


def _request_refresh(checkout: str, repo_root: str | None, *, background: bool) -> None:
    """Reserve a checkout and transfer ownership to a detached runner."""
    checkout = str(Path(checkout).resolve())
    if not _automatic_rebuild_allowed(checkout) or graph_path(checkout) is None:
        return
    lock_path, pending, guard_path = _refresh_files(checkout)
    with guard_path.open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        lock = lock_path.open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            pending.write_text(checkout, encoding="utf-8")   # the follow-up rebuilds from here
            return
        pending.unlink(missing_ok=True)
        try:
            if background:
                from brindle.config import brindle_home

                log = brindle_home() / "graphify.log"
                with log.open("a", encoding="utf-8") as out:
                    spawn.start(
                        [sys.executable, "-P", "-c",
                         "import sys; from brindle import redact; redact.guard_std(); "
                         "from brindle.codemap import _run_refresh; "
                         "_run_refresh(sys.argv[1], sys.argv[2] or None, int(sys.argv[3]))",
                         checkout, repo_root or "", str(lock.fileno())],
                        cwd=checkout, start_new_session=True,
                        pass_fds=(lock.fileno(),), stdin=subprocess.DEVNULL,
                        stdout=out, stderr=out,
                    )
        except BaseException:
            lock.close()
            raise
        if background:
            # Closing our copy leaves the inherited open description locked.
            lock.close()
            return
    fd = os.dup(lock.fileno())
    lock.close()
    _run_refresh(checkout, repo_root, fd)


def _refresh_files(checkout: str) -> tuple[Path, Path, Path]:
    from brindle.config import brindle_home

    root = brindle_home() / "graphify-locks"
    root.mkdir(parents=True, exist_ok=True)
    # Keyed by the graph folder, so checkouts sharing one (shared_out) take turns.
    key = hashlib.sha256(os.path.realpath(Path(checkout) / GRAPH.parent).encode()).hexdigest()
    return root / (key + ".lock"), root / (key + ".pending"), root / (key + ".guard")


def _run_refresh(checkout: str, repo_root: str | None, fd: int) -> None:
    """Hold the inherited lock through graphify, postprocessing and follow-ups."""
    _, pending, guard_path = _refresh_files(checkout)
    with os.fdopen(fd, "a") as lock:
        while True:
            post = _start_refresh(checkout, repo_root, lock.fileno())
            if post is not None:
                post()
            with guard_path.open("a") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                if not pending.exists():
                    # Serialize release with callers' lock check and marker write.
                    lock.close()
                    return
                checkout = pending.read_text(encoding="utf-8").strip() or checkout
                pending.unlink()


def _start_refresh(checkout: str, repo_root: str | None, lock_fd: int) -> Callable[[], None] | None:
    """Start one low-priority update and return its completion work."""
    if not _automatic_rebuild_allowed(checkout):
        return
    if graph_path(checkout) is None:
        return
    from brindle.config import brindle_home

    if repo_root is None:
        from brindle import git

        try:
            repo_root = git.main_repo_root(checkout)
        except Exception:
            repo_root = checkout

    target_graph = Path(checkout) / GRAPH
    old_graph = None
    if target_graph.is_file():
        try:
            old_graph = json.loads(target_graph.read_text(encoding="utf-8"))
        except Exception:
            old_graph = None

    log = brindle_home() / "graphify.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    proc = spawn.start(
        ["nice", "-n", "10", "graphify", "update", checkout, "--force"],
        cwd=checkout,
        start_new_session=True,
        pass_fds=(lock_fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,     # copied to graphify.log masked, by _post
        stderr=subprocess.STDOUT,
    )

    def _post():
        if proc is not None and hasattr(proc, "wait"):
            _log_masked(getattr(proc, "stdout", None), log)
            proc.wait()
        if target_graph.is_file():
            carry_community_names(old_graph, target_graph)
            copy_to_store(target_graph, repo_root)

    return _post


def _log_masked(stream, log: Path) -> None:
    """Append what ``stream`` (graphify's output pipe) says to ``log``, with
    secrets masked (by a MaskingStream, so one split across lines is held
    whole), until it closes."""
    if stream is None:
        return
    from brindle.redact import MaskingStream

    with stream, open(log, "a", encoding="utf-8") as out:
        masked = MaskingStream(out)
        for raw in stream:
            masked.write(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
            masked.flush()
        masked.finish()


def remove_session_graph(checkout: str | Path) -> None:
    """Remove a session checkout's graphify-out graph when its worktree is removed."""
    out = Path(checkout) / GRAPH.parent
    if out.is_symlink():
        out.unlink()   # the repo's shared graph stays for its other checkouts
    elif out.is_dir():
        shutil.rmtree(out, ignore_errors=True)
    elif out.is_file():
        out.unlink(missing_ok=True)


def record_query(db, agent, ws: Workspace | None, command: str) -> None:
    """Log a read-only graphify command an agent runs as a ``graph_query`` history row."""
    if ws is None or not _QUERY_RE.match(command):
        return
    from brindle import history

    history.record_safely(db, ws.repo_root, "graph_query", agent=agent, branch=ws.branch,
                          task=command.strip())


# Search options that take a value: the letters of a short cluster, and the long names.
_VALUE_LETTERS = "efgtTmABCdD"
_VALUE_LONG = {"--glob", "--type", "--max-count", "--include", "--exclude", "--exclude-dir", "--file",
               "--regexp", "--after-context", "--before-context", "--context"}
_OPERATORS = {"&&", "||", ";", "|", "&", "|&"}


def _tokens(line: str) -> list[tuple[str, str, bool]] | None:
    """Shell words and operators of a line as (kind, text, attached): words get
    POSIX quote and escape handling, operators are only those outside quotes;
    ``attached`` is whether no space came before it. None when a quote is unclosed."""
    out: list[tuple[str, str, bool]] = []
    word, started, quote, i, attached = "", False, "", 0, False
    ops = "&|;()<>"

    def flush() -> None:
        nonlocal word, started
        if started:
            out.append(("w", word, False))
        word, started = "", False

    while i < len(line):
        c = line[i]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"' and i + 1 < len(line) and line[i + 1] in '$`"\\':
                i += 1
                word += line[i]
            else:
                word += c
        elif c in "'\"":
            quote, started = c, True
        elif c == "\\" and i + 1 < len(line):
            i += 1
            word, started = word + line[i], True
        elif c == "#" and not started:   # a comment runs to the end of the line
            while i + 1 < len(line) and line[i + 1] != "\n":
                i += 1
        elif c in ops:
            attached = started
            flush()
            j = i
            while j + 1 < len(line) and line[j + 1] in ops and line[j + 1] != "(" and c not in "()":
                j += 1
            out.append(("op", line[i:j + 1], attached))
            i = j
        elif c.isspace():
            flush()
            if c == "\n":
                out.append(("op", ";", False))
        else:
            word, started = word + c, True
        i += 1
    if quote:
        return None
    flush()
    return out


def _commands(command: str) -> list[tuple[list[str], bool, str]]:
    """Each command of a line as (words, inside parentheses, the operator before it),
    split at shell operators outside quotes. A command after a pipe is left out (it reads
    stdin); redirections are dropped from the words."""
    tokens = _tokens(command)
    if tokens is None:
        return []
    out, cur, piped, prev, depth, skip = [], [], False, "", 0, False
    for kind, text, attached in [*tokens, ("op", ";", False)]:
        if kind == "w":
            if skip:
                skip = False
            else:
                cur.append(text)
        elif "<" in text or ">" in text:
            if attached and cur and cur[-1].isdigit():
                cur.pop()   # 2>&1
            skip = True
        else:
            if cur and not piped:
                out.append((cur, depth > 0, prev))
            piped = text in ("|", "|&")
            depth += text.count("(") - text.count(")")
            prev, cur, skip = text, [], False
    return out


def _broad_search(command: str, cwd: str | None = None) -> bool:
    """Whether a command line starts a recursive search across a tree: ``grep -r``,
    ``rg`` or ``git grep`` unless every path it names is an existing file (paths are
    read against ``cwd``)."""
    prev_cd, start = False, cwd
    for words, nested, before in _commands(command):
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]   # VAR=x prefixes
        if not words:
            continue
        name, rest = os.path.basename(words[0]), words[1:]
        if before in ("||", "&") and cwd != start:
            cwd = None   # runs only if the cd failed, or in the background
        if name in ("cd", "pushd", "popd") or (
                name in ("{", "if", "then", "do", "else", "while", "until", "!", "time", "command", "builtin")
                and any(os.path.basename(w) in ("cd", "pushd", "popd") for w in words)):
            # Later operands are read from there, when the cd surely ran in this shell.
            sure = (name == "cd" and not nested and before in ("", ";") or (before == "&&" and prev_cd))
            ok = (cwd and sure and name == "cd" and len(rest) == 1
                  and not rest[0].startswith(("-", "~")) and "$" not in rest[0])
            cwd = os.path.normpath(os.path.join(cwd, rest[0])) if ok else None
            prev_cd = bool(ok)
            continue
        prev_cd = False
        if name == "git" and rest[:1] == ["grep"]:
            name, rest = "git-grep", rest[1:]
        if name not in ("rg", "git-grep", "grep", "egrep", "fgrep"):
            continue
        recursive = name in ("rg", "git-grep")
        positional, skip, has_pattern_opt = [], False, False
        for i, w in enumerate(rest):
            if skip:
                skip = False
            elif w == "--":
                positional += rest[i + 1:]
                break
            elif w.startswith("--"):
                opt, eq, _ = w.partition("=")
                if opt in ("--recursive", "--dereference-recursive"):
                    recursive = True
                if opt in ("--regexp", "--file"):
                    has_pattern_opt = True
                skip = opt in _VALUE_LONG and not eq
            elif w.startswith("-") and len(w) > 1:
                for j, ch in enumerate(w[1:], 1):
                    if ch in "rR":
                        recursive = True
                    elif ch in _VALUE_LETTERS:
                        has_pattern_opt = has_pattern_opt or ch in "ef"
                        skip = j == len(w) - 1   # value attached, or the next word
                        break
            else:
                positional.append(w)
        if not recursive:
            continue
        paths = positional if has_pattern_opt else positional[1:]
        if paths and (cwd is None or nested or any(c in p for p in paths for c in "$*?[`~") or all(os.path.isfile(os.path.join(cwd, p)) for p in paths)):
            continue   # named files only (or where they are can't be told: stay quiet)
        return True
    return False


NUDGE_MARK = "graph-nudge"
NUDGE = ("brindle: this repo has a code map. To find where something lives or what calls what, "
         "`graphify query \"<question>\" --graph {path}` is faster than a recursive search. "
         "grep is still fine for an exact string. (Shown once.)")


def search_nudge(db, agent, ws: Workspace | None, command: str, cwd: str | None = None) -> str | None:
    """The one-time note for an agent's first broad recursive search in a repo with a
    graph; None otherwise. ``cwd`` is the hook's working directory (default the
    worktree). Never raises: it must not change the hook's answer."""
    try:
        if ws is None or not _broad_search(command, cwd or ws.path):
            return None
        path = graph_path(ws)
        if path is None or not db.claim_mark(agent.id, NUDGE_MARK, "1")[0]:
            return None
        return NUDGE.format(path=path)
    except Exception:  # noqa: BLE001
        return None


def graph_queries(db, repo_root: str, agent_id: str | None = None) -> list:
    """The recorded graph queries, newest first; one agent's if ``agent_id`` is given."""
    from brindle import history

    rows = db.list_history(repo_root, "graph_query", history.CAP_PER_REPO)
    return [r for r in rows if agent_id is None or r.agent_id == agent_id]
