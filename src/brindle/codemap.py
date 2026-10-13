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
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brindle.db import Workspace

GRAPH = Path("graphify-out") / "graph.json"
# Read-only graphify commands, pre-approved for Claude agents.
ALLOWED_TOOLS = ["Bash(graphify query:*)", "Bash(graphify explain:*)", "Bash(graphify path:*)",
                 "Bash(graphify affected:*)"]
MAP_TOP = 10
STORE_RETENTION_DAYS = 30
_QUERY_RE = re.compile(r"^\s*graphify\s+(?:query|explain|path|affected)\b")
_map_cache: dict[str, tuple[float, str]] = {}   # graph path -> (mtime, rendered map)


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


def seed_session(checkout: str | Path, repo_root: str | None = None) -> bool:
    """When a session checkout has no graph, seed it from the store if present
    and run graphify update in the background. Returns True if update was started."""
    from brindle.config import brindle_home, load_repo_config

    checkout_str = str(checkout)
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
    if stored.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stored, path)

    log = brindle_home() / "graphify.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as out:
        subprocess.Popen(
            ["graphify", "update", checkout_str, "--force"],
            cwd=checkout_str,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
        )
    return True


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
    """Synchronous graph refresh: run graphify update --force, carry community names, copy to store."""
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
    with open(log, "a", encoding="utf-8") as out:
        proc = subprocess.Popen(
            ["graphify", "update", checkout, "--force"],
            cwd=checkout,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
        )
    if proc is not None and hasattr(proc, "wait"):
        proc.wait()
    if target_graph.is_file():
        carry_community_names(old_graph, target_graph)
        copy_to_store(target_graph, repo_root)


def refresh_later(checkout: str, repo_root: str | None = None) -> None:
    """Rebuild the code part of the graph in the background, after a merge."""
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
    with open(log, "a", encoding="utf-8") as out:
        proc = subprocess.Popen(
            ["graphify", "update", checkout, "--force"],
            cwd=checkout,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
        )

    def _post():
        if proc is not None and hasattr(proc, "wait"):
            proc.wait()
        if target_graph.is_file():
            carry_community_names(old_graph, target_graph)
            copy_to_store(target_graph, repo_root)

    t = threading.Thread(target=_post, daemon=True)
    t.start()


def remove_session_graph(checkout: str | Path) -> None:
    """Remove a session checkout's graphify-out graph when its worktree is removed."""
    out = Path(checkout) / GRAPH.parent
    if out.is_dir():
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


def graph_queries(db, repo_root: str, agent_id: str | None = None) -> list:
    """The recorded graph queries, newest first; one agent's if ``agent_id`` is given."""
    from brindle import history

    rows = db.list_history(repo_root, "graph_query", history.CAP_PER_REPO)
    return [r for r in rows if agent_id is None or r.agent_id == agent_id]
