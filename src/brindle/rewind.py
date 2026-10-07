"""Rewind: turn snapshots of a worker's worktree, and restarting it from one.

After every turn of a worker that changed files, brindle snapshots its whole
worktree (tracked and untracked files, not ignored ones) onto a hidden ref,
``refs/brindle/turns/<agent>/<n>``. A snapshot is a commit object built with
a temporary index (``git write-tree`` / ``commit-tree``): the worker's branch,
HEAD and index are never touched, so it can keep working, and a snapshot
costs about what ``git status`` does. The commit's parent is the branch's
HEAD at the time, so a snapshot also remembers which commit the worker was
on; its message carries a short summary of the turn (what the agent said,
read from its transcript where there is one, else the tail of its terminal,
and the files that changed), which is what a rewind brief is built from.

``rewind`` resets the worktree and branch to the state after turn N, stops
the worker, and starts a FRESH session in the same workspace (optionally with
another profile) with a brief: the original task, a summary of turns 1..N, and
the user's note. A fresh session works for every provider; resuming a CLI
session at an earlier point doesn't (Codex can't resume at all, see
``providers.Provider.can_resume``). The snapshots up to N are carried over to
the new agent, so it can be rewound again, further back.

The refs are removed with the workspace (``workspaces.remove``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from brindle import git, tmux
from brindle.db import DB, Agent, Workspace

log = logging.getLogger(__name__)

REF_PREFIX = "refs/brindle/turns"
# Modes whose agents edit code in a worktree of their own: what gets snapshots.
SNAPSHOT_MODES = ("handoff", "handoff_detached", "assign")
SUMMARY_CHARS = 600      # of what the agent said, per turn
SCREEN_LINES = 30        # of terminal read for a summary when there's no transcript
TAIL_BYTES = 256 * 1024  # of a transcript read for the turn's last words
TURN_SUBJECT = re.compile(r"^brindle turn (\d+) of (\S+)")


class RewindError(RuntimeError):
    pass


def refs_for(agent_id: str) -> str:
    return f"{REF_PREFIX}/{agent_id}"


def baseline_ref(agent_id: str) -> str:
    """Where the worker started: the commit its worktree was on before its
    first turn (recorded at session start), so a first turn that commits
    straight away still counts as a change."""
    return f"{refs_for(agent_id)}/base"


# -- snapshots ----------------------------------------------------------------------


def turns(repo_root: str, agent_id: str) -> list[tuple[int, str]]:
    """``(n, sha)`` of every snapshot of ``agent_id``, in turn order."""
    prefix = refs_for(agent_id) + "/"
    text = git.out(["for-each-ref", "--format=%(refname) %(objectname)", prefix], repo_root)
    found = []
    for line in text.splitlines():
        ref, _, sha = line.partition(" ")
        tail = ref[len(prefix):]
        if tail.isdigit():
            found.append((int(tail), sha))
    return sorted(found)


def _snapshot_index(path: str) -> str:
    """A scratch index file in the worktree's own git dir (never the one the
    worker is using)."""
    git_dir = git.out(["rev-parse", "--path-format=absolute", "--git-dir"], path)
    return os.path.join(git_dir, "brindle-snapshot-index")


def _tree_of_worktree(path: str) -> str:
    """The tree of ``path`` as it is now, tracked and untracked files alike
    (ignored ones left out), without touching the real index."""
    index = _snapshot_index(path)
    env = {**os.environ, "GIT_INDEX_FILE": index}
    try:
        os.unlink(index)
    except FileNotFoundError:
        pass
    try:
        # Seed from HEAD so unchanged files cost nothing to hash, then add
        # what differs (including deletions and new files).
        git.run_env(["read-tree", "HEAD"], path, env)
        git.run_env(["add", "-A", "--", "."], path, env)
        return git.run_env(["write-tree"], path, env).stdout.strip()
    finally:
        try:
            os.unlink(index)
        except FileNotFoundError:
            pass


def _parent_tree(repo_root: str, sha: str) -> tuple[str | None, str]:
    """``(parent, tree)`` of commit ``sha``."""
    parent = git.out(["rev-parse", "--verify", "-q", f"{sha}^"], repo_root) if git.ok(
        ["rev-parse", "--verify", "-q", f"{sha}^"], repo_root) else None
    tree = git.out(["rev-parse", f"{sha}^{{tree}}"], repo_root)
    return parent, tree


def mark_baseline(agent: Agent, ws: Workspace) -> None:
    """Record the commit ``agent``'s worktree is on as its baseline, once
    (at its session start, before any turn). No-op for anything that gets
    no snapshots."""
    if agent.mode not in SNAPSHOT_MODES or ws.kind != "worktree" or not os.path.isdir(ws.path):
        return
    ref = baseline_ref(agent.id)
    if not git.ok(["rev-parse", "--verify", "-q", ref], ws.repo_root):
        git.run(["update-ref", ref, git.out(["rev-parse", "HEAD"], ws.path)], ws.repo_root)


def baseline(ws: Workspace, agent_id: str) -> str:
    """The commit a worker's first snapshot is compared with: its recorded
    baseline, else where its branch forked from its base (a worker launched
    by an older brindle, or whose start hook never ran), else HEAD."""
    ref = baseline_ref(agent_id)
    if git.ok(["rev-parse", "--verify", "-q", ref], ws.repo_root):
        return git.out(["rev-parse", ref], ws.repo_root)
    if ws.base_branch and git.branch_exists(ws.repo_root, ws.base_branch):
        fork = git.run(["merge-base", "HEAD", f"refs/heads/{ws.base_branch}"], ws.path, check=False)
        if fork.returncode == 0 and fork.stdout.strip():
            return fork.stdout.strip()
    return git.out(["rev-parse", "HEAD"], ws.path)


def snapshot(db: DB, agent: Agent, ws: Workspace, *, summary: str | None = None) -> int | None:
    """Snapshot ``ws`` after a turn of ``agent``, if anything changed since the
    last snapshot (files, or the commit the branch is on). Returns the turn
    number, or None when nothing changed. Only workers in a worktree of
    their own get snapshots; anything else is a no-op."""
    if agent.mode not in SNAPSHOT_MODES or ws.kind != "worktree" or not os.path.isdir(ws.path):
        return None
    # No lock: the worker's own index and branch aren't touched, and this runs
    # inside its Stop hook, which must stay quick.
    head = git.out(["rev-parse", "HEAD"], ws.path)
    tree = _tree_of_worktree(ws.path)
    existing = turns(ws.repo_root, agent.id)
    if existing:
        last_n, last_sha = existing[-1]
        last_parent, last_tree = _parent_tree(ws.repo_root, last_sha)
        before_tree = last_tree
    else:
        # The first snapshot is measured against where the worker started,
        # not HEAD: a first turn that committed its edits has moved HEAD.
        last_n, last_sha = 0, None
        last_parent = baseline(ws, agent.id)
        before_tree = git.out(["rev-parse", f"{last_parent}^{{tree}}"], ws.path)
    if last_parent == head and before_tree == tree:
        return None  # a turn that only read code (or a duplicate hook): nothing new
    n = last_n + 1
    said = summary if summary is not None else turn_words(agent)
    changed = _changed_files(ws.repo_root, before_tree, tree)
    message = _message(agent.id, n, head, said, changed)
    sha = git.run_env(["commit-tree", tree, "-p", head, "-m", message], ws.path,
                      _author_env()).stdout.strip()
    git.run(["update-ref", "-m", f"brindle turn {n}", f"{refs_for(agent.id)}/{n}", sha],
            ws.repo_root)
    return n


def _author_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        env.setdefault(k, "brindle")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        env.setdefault(k, "brindle@localhost")
    return env


def _changed_files(repo_root: str, before_tree: str, tree: str) -> list[str]:
    """Files that differ between the previous snapshot's tree (the baseline's,
    for the first one) and this one."""
    text = git.run(["diff-tree", "-r", "--name-only", before_tree, tree], repo_root, check=False).stdout
    return [line for line in text.splitlines() if line]


def _message(agent_id: str, n: int, head: str, said: str | None, changed: list[str]) -> str:
    body = [f"brindle turn {n} of {agent_id}", "", f"head: {head}"]
    if changed:
        shown = changed[:40]
        body.append("files: " + ", ".join(shown) + (f", (+{len(changed) - 40} more)" if len(changed) > 40 else ""))
    if said:
        body.extend(["", said.strip()])
    return "\n".join(body) + "\n"


def turn_words(agent: Agent) -> str | None:
    """What the agent last said, for the turn's summary: the final assistant
    text of its transcript (Claude Code's JSONL), else the tail of its
    terminal, which is what ``brindle agent peek`` shows. Never raises."""
    try:
        if agent.transcript_path:
            said = _last_assistant_text(agent.transcript_path)
            if said:
                return _trim(said)
        if agent.tmux_window:
            from brindle import agents

            if agents.is_alive(agent):
                screen = tmux.capture(agent.tmux_window, lines=SCREEN_LINES,
                                      server=agents.server_of(agent))
                text = "\n".join(l.rstrip() for l in screen.splitlines() if l.strip())
                return _trim(text) if text else None
    except Exception:  # noqa: BLE001 - a summary is a courtesy, never a failure
        log.debug("brindle: no turn summary for %s", agent.id, exc_info=True)
    return None


def _last_assistant_text(transcript: str) -> str | None:
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(f.tell() - TAIL_BYTES, 0))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if isinstance(blocks, str) and blocks.strip():
            return blocks
        texts = [b.get("text") for b in (blocks if isinstance(blocks, list) else [])
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]
        if texts:
            return "\n".join(texts)
    return None


def _trim(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


# -- reading snapshots ----------------------------------------------------------------


def describe(repo_root: str, agent_id: str) -> list[dict]:
    """Each snapshot of ``agent_id``: ``{"turn", "sha", "head", "files",
    "said", "at"}``, in turn order."""
    out = []
    for n, sha in turns(repo_root, agent_id):
        body = git.out(["log", "-1", "--format=%B", sha], repo_root)
        at = git.out(["log", "-1", "--format=%ct", sha], repo_root)
        head, files, said = _parse_message(body)
        out.append({"turn": n, "sha": sha, "head": head, "files": files, "said": said,
                    "at": float(at) if at.isdigit() else None})
    return out


def _parse_message(body: str) -> tuple[str | None, list[str], str | None]:
    """The inverse of ``_message``: the subject line, then ``head:`` and an
    optional ``files:`` line, then (after a blank line) what the agent said."""
    head, files = None, []
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if TURN_SUBJECT.match(line) or (not line.strip() and head is None):
            continue
        if line.startswith("head: "):
            head = line[6:].strip()
        elif line.startswith("files: "):
            files = [f.strip() for f in line[7:].split(",") if f.strip()]
            if files and files[-1].startswith("(+"):
                files.pop()
        else:
            i -= 1
            break
    said = "\n".join(lines[i:]).strip() or None
    return head, files, said


def format_turns(repo_root: str, agent_id: str) -> str:
    rows = describe(repo_root, agent_id)
    if not rows:
        return f"{agent_id} has no turn snapshots yet."
    lines = []
    for r in rows:
        when = time.strftime("%H:%M", time.localtime(r["at"])) if r["at"] else ""
        files = ", ".join(r["files"][:6]) + (" ..." if len(r["files"]) > 6 else "")
        first = (r["said"] or "").strip().splitlines()
        lines.append(f"turn {r['turn']:>3}  {when}  {files or '(no file changes)'}"
                     + (f"\n           {first[0][:120]}" if first else ""))
    return "\n".join(lines)


def brief(agent: Agent, repo_root: str, to: int, note: str | None) -> str:
    """The fresh session's prompt: the original task, what happened in turns
    1..``to`` (from the snapshots' summaries), and the user's note."""
    parts = [(agent.task or "").strip() or "(no task was recorded for the original worker)"]
    history = [r for r in describe(repo_root, agent.id) if r["turn"] <= to]
    lines = [
        "---",
        f"You are continuing work an earlier session started. Its worktree has been rewound to "
        f"the state right after its turn {to}: the files are exactly as that session left them "
        f"then (check `git status` and `git diff`). What that session did, turn by turn:",
    ]
    for r in history:
        files = ", ".join(r["files"][:12]) + (f" (+{len(r['files']) - 12} more)" if len(r["files"]) > 12 else "")
        lines.append(f"\nTurn {r['turn']}" + (f" (changed: {files})" if files else " (no file changes)") + ":")
        if r["said"]:
            lines.append(_indent(r["said"]))
    if note and note.strip():
        lines.extend(["", "From the person rewinding you:", _indent(note.strip())])
    parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _indent(text: str) -> str:
    return "\n".join("  " + l for l in text.strip().splitlines())


# -- restoring -------------------------------------------------------------------------


def restore(ws: Workspace, sha: str) -> None:
    """Put ``ws``'s worktree and branch back as snapshot ``sha`` recorded
    them: the branch at the commit the worker was on then, the files exactly
    as they were (uncommitted edits and untracked files included)."""
    parent, _tree = _parent_tree(ws.repo_root, sha)
    if parent is None:
        raise RewindError(f"snapshot {sha[:8]} has no parent commit")
    with git.checkout_lock(ws.path, timeout=30):
        git.run(["reset", "-q", "--hard", parent], ws.path)
        git.run(["clean", "-fdq"], ws.path)
        # The snapshot's files into the index and worktree (removing what it
        # didn't have), then the index back to HEAD so the worker's edits
        # show up as the uncommitted changes they were.
        git.run(["read-tree", "--reset", "-u", sha], ws.path)
        git.run(["reset", "-q", parent], ws.path)


def copy_turns(repo_root: str, src: str, dst: str, upto: int) -> None:
    """Snapshots 1..``upto`` of ``src`` become ``dst``'s, so it can be rewound
    further back later."""
    for n, sha in turns(repo_root, src):
        if n <= upto:
            git.run(["update-ref", f"{refs_for(dst)}/{n}", sha], repo_root)


def forget(repo_root: str, agent_ids: list[str]) -> int:
    """Delete every snapshot ref of ``agent_ids``. Returns how many went."""
    gone = 0
    for agent_id in agent_ids:
        prefix = refs_for(agent_id) + "/"
        listed = git.out(["for-each-ref", "--format=%(refname) %(objectname)", prefix], repo_root)
        for line in listed.splitlines():
            ref, _, sha = line.partition(" ")
            try:
                git.run(["update-ref", "-d", ref, sha], repo_root)
                gone += 1
            except git.GitError:
                log.debug("brindle: couldn't delete turn ref %s", ref, exc_info=True)
    return gone


def forget_workspace(db: DB, ws: Workspace) -> int:
    """``forget`` for every agent that ever ran in ``ws``. Never raises."""
    try:
        return forget(ws.repo_root, [a.id for a in db.list_agents(ws.id)])
    except Exception:  # noqa: BLE001
        log.debug("brindle: couldn't clean turn refs of %s", ws.id, exc_info=True)
        return 0


# -- the rewind itself -------------------------------------------------------------------


def rewind(db: DB, agent_id: str, to: int, *, profile: str | None = None,
           note: str | None = None) -> Agent:
    """Reset ``agent_id``'s worktree to its snapshot ``to``, stop it, and
    start a fresh worker there (profile ``profile``, default the same) with
    a brief of the task, turns 1..``to`` and ``note``. Returns the new agent."""
    from brindle import agents

    agent = agents.get(db, agent_id)
    ws = db.get_workspace(agent.workspace_id)
    if ws is None:
        raise RewindError(f"{agent_id}'s workspace is gone")
    if agent.mode not in SNAPSHOT_MODES:
        raise RewindError(f"{agent_id} is a {agent.mode} agent; only workers (handoff/assign) can be rewound")
    if ws.kind != "worktree" or not os.path.isdir(ws.path):
        raise RewindError(f"{agent_id}'s worktree {ws.path} is gone")
    snapshots = dict(turns(ws.repo_root, agent_id))
    if not snapshots:
        raise RewindError(f"{agent_id} has no turn snapshots (it hasn't finished a turn that changed files)")
    if to not in snapshots:
        have = ", ".join(str(n) for n in sorted(snapshots))
        raise RewindError(f"{agent_id} has no turn {to}; its snapshots: {have}")
    if profile:
        from brindle.profiles import load_profile

        try:  # an unknown profile fails here, before anything is touched
            load_profile(profile, ws.repo_root)
        except (KeyError, ValueError, OSError) as e:
            raise RewindError(str(e).strip('"')) from e
    text = brief(agent, ws.repo_root, to, note)

    if agents.is_alive(agent):
        agents._stop(db, agent)
    db.set_status(agent.id, "rewound")
    db.update_agent(agent.id, dismissed_at=time.time())
    restore(ws, snapshots[to])
    mode = "assign" if agent.mode in ("handoff", "handoff_detached") else agent.mode
    fresh = agents.spawn(
        db, ws, profile or agent.profile, prompt=text, parent_id=agent.parent_id, mode=mode,
        done_when=agent.done_when, background_setup=True,
    )
    # The brief carries the decorated prompt; the record keeps the original task
    # so a later rewind (or a reviewer) sees what was really asked.
    db.update_agent(fresh.id, task=agent.task)
    fresh.task = agent.task
    copy_turns(ws.repo_root, agent.id, fresh.id, to)
    if git.ok(["rev-parse", "--verify", "-q", baseline_ref(agent.id)], ws.repo_root):
        git.run(["update-ref", baseline_ref(fresh.id), git.out(["rev-parse", baseline_ref(agent.id)], ws.repo_root)],
                ws.repo_root)
    try:
        from brindle import events
        from brindle.config import load_repo_config

        events.emit(load_repo_config(ws.repo_root), "assign", ws, fresh, actor=None)
    except Exception:  # noqa: BLE001
        pass
    return fresh
