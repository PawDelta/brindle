"""A supervisor's operational memory (repo setting ``supervisor_state``).

A supervisor rereads its whole chat every turn, so what each turn costs grows
with the conversation. With this setting on, brindle keeps a compact state of
the session instead, folded from the events it already publishes
(brindle.bus): the goal's milestones and their status, each branch and where
it stands, the decisions made, the person's open questions and every
worker's last result. The goal and milestones themselves are read live from
the autopilot rows, which are already the source of truth.

At the end of each supervisor turn (the Stop hook) the state is brought up to
date. Once the turn's context passes ``supervisor_restart_tokens``, the
supervisor restarts: its CLI is stopped and a fresh session of it starts with
an ordinary first prompt (nothing the CLI doesn't already accept), seeded
with the state and the latest messages instead of the whole chat. A restart
never clears the state, so no decision or open question is lost across one.
From the claim until the fresh session is launched, the supervisor's queue is
held (db.RESTART_HOLD), so mail waits for the fresh session; the restart
itself checks, under the session's resume lock, that the claim is still its
own and the session hasn't moved on since.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import shlex
import time
from contextlib import contextmanager
from pathlib import Path

from brindle import bus
from brindle.db import DB, RESTART_HOLD, Agent, _deliveries_in_flight, _restart_holds

log = logging.getLogger(__name__)

# The events the state is folded from.
KINDS = ("autopilot.need_user", "autopilot.answered", "autopilot.audit_decision", "decision.recorded",
         "pipeline.reported", "pipeline.review", "pipeline.escalated", "pipeline.merged",
         "pipeline.removed", "agent.result")
WORKER_MODES = ("handoff", "handoff_detached", "assign")
RESULT_CHARS = 600         # of each worker's last result
MESSAGES_BYTES = 10_000    # of the latest messages, kept whole, newest first (the person's newest always)
ACTIONS_BYTES = 3_000      # of the digest of the supervisor's own recent outward actions
ACTION_CHARS = 300         # of each action in that digest
PROMISES = 3               # open promises ("I'm watching CI on #N") kept in that digest
KEEP_ANSWERED = 3         # answered questions kept, for the replies in the latest messages
TAIL_BYTES = 512 * 1024    # of the transcript read for its context size and latest messages
RESTART_CLAIM = 600.0      # seconds a restart claim (and its hold on the queue) lasts at most
RESTART_LOCK_WAIT = 120.0  # seconds a pause waits for a restart's launch to commit (restart_lock)
RESTART_HEAD = "[brindle] Your context was restarted"
SEED_BYTES = 30_000        # bytes upper-bound tokens, including non-ASCII text
RESTART_COOLDOWN = 300.0   # let a fresh supervisor make progress before another restart
FINISHED_BRANCHES = {"merged", "removed unmerged"}


def _empty() -> dict:
    return {"branches": {}, "decisions": [], "questions": [], "answered": [], "workers": {}}


def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# -- the state ------------------------------------------------------------------


def load(db: DB, root_id: str) -> tuple[dict, int]:
    """Session ``root_id``'s state as last folded, and the last event in it."""
    row = db.conn.execute("SELECT state, last_event FROM supervisor_state WHERE root_id=?",
                          (root_id,)).fetchone()
    if row is None:
        return _empty(), 0
    try:
        state = {**_empty(), **json.loads(row["state"])}
    except (ValueError, TypeError):
        state = _empty()
    return state, int(row["last_event"])


def _save(db: DB, root_id: str, state: dict, last_event: int) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO supervisor_state (root_id, state, last_event, updated_at) VALUES (?, ?, ?, ?)"
                  " ON CONFLICT(root_id) DO UPDATE SET state=excluded.state,"
                  " last_event=excluded.last_event, updated_at=excluded.updated_at",
                  (root_id, json.dumps(state), last_event, time.time()))


def refresh(db: DB, root_id: str) -> dict:
    """Fold session ``root_id``'s events since the last fold into its state;
    returns the state."""
    state, last = load(db, root_id)
    while True:
        events = bus.tail(db, last, kinds=KINDS, root_id=root_id)
        for ev in events:
            try:
                _fold(db, root_id, state, ev)
            except Exception:  # noqa: BLE001 - one odd event must not stop the rest
                log.warning("brindle: couldn't fold event %s into %s's state", ev.get("id"), root_id,
                            exc_info=True)
            last = ev["id"]
        if len(events) < 500:
            break
    if db.get_agent(root_id) is not None:
        _save(db, root_id, state, last)
    return state


def _fold(db: DB, root_id: str, state: dict, ev: dict) -> None:
    kind, p = ev["kind"], ev["payload"]
    if kind == "autopilot.need_user":
        question = (p.get("question") or "").strip()
        if p.get("ask") and question and question not in [q["text"] for q in state["questions"]]:
            state["questions"].append({"text": question, "at": ev["ts"]})
    elif kind == "autopilot.answered":
        state["answered"] = (state["answered"] + state["questions"])[-KEEP_ANSWERED:]
        state["questions"] = []
    elif kind == "decision.recorded":
        from brindle import knowledge

        d = knowledge.get_decision(db, p["decision_id"])
        text = f"{d['topic']}: {d['body']}" if d else p.get("topic") or ""
        if p.get("supersedes"):
            state["decisions"] = [x for x in state["decisions"] if x.get("id") != p["supersedes"][:8]]
        state["decisions"].append({"id": p["decision_id"][:8], "text": _clip(text, 1000)})
    elif kind == "autopilot.audit_decision":
        if p.get("decision"):
            state["decisions"].append({"id": None, "text": _clip(p["decision"], 1000)})
    elif kind.startswith("pipeline."):
        branch = p.get("branch")
        if not branch:
            return
        where = {
            "pipeline.reported": "reported, in review",
            "pipeline.review": "approved" if p.get("approved") else "changes requested by review",
            "pipeline.escalated": "review findings handed to you",
            "pipeline.merged": "merged",
            "pipeline.removed": "merged" if p.get("merged") else "removed unmerged",
        }[kind]
        state["branches"][branch] = {"state": where, "worker": ev.get("agent_id")}
    elif kind == "agent.result":
        # From the event itself: the worker's row may be gone with its workspace by now.
        if not ev.get("agent_id") or p.get("mode") not in WORKER_MODES or not p.get("result"):
            return
        state["workers"][ev["agent_id"]] = {"profile": p.get("profile"), "branch": p.get("branch"),
                                            "result": _clip(p["result"], RESULT_CHARS)}


# -- what the fresh session is told -------------------------------------------------


PIPELINE_STATES = {
    "reviewing": "reported, in review",
    "fixing": "fixing review findings",
    "resolving": "resolving merge conflicts",
    "ready": "approved, ready to merge",
    "sync_wait": "waiting to sync with its base",
}


def _row_state(a: Agent) -> str:
    """Where worker ``a`` stands now, from its own row."""
    if a.plan_state == "proposed":
        return f"waiting for your plan approval (approve_plan agent_id={a.id})"
    if a.plan_state == "revise":
        return "revising its plan"
    if a.merge_failed:
        return "approved, but its merge failed: " + _clip(a.merge_failed, 200)
    if a.pipeline:
        return PIPELINE_STATES.get(a.pipeline, a.pipeline)
    if a.status == "idle" and a.result is not None:
        return "reported"
    return a.status


def render(db: DB, root_id: str, state: dict | None = None, *, max_bytes: int | None = None) -> str:
    """The state as the supervisor reads it: the goal and milestones (live),
    branches, decisions, questions and workers' last results. With a budget,
    omit old history first; never omit current work to make history fit."""
    from brindle import agents, tasks

    state = refresh(db, root_id) if state is None else state
    ap = db.get_autopilot(root_id)
    # Priority -1 is essential, 0 is decisions/recent replies, 1 is finished
    # work. Keep the newest optional rows first, without changing display order.
    sections: list[tuple[str, list[tuple[str, int]]]] = []
    if ap and ap.goal:
        sections.append(("## Goal", [(ap.goal + (f"\n{ap.detail}" if ap.detail else ""), -1)]))
        ms = db.milestones(root_id)
        if ms:
            sections.append(("## Milestones", [
                (f"{m.position}. [{m.state or m.status}] {m.title}",
                 1 if (m.state or m.status) == "passed" else -1) for m in ms]))
        if ap.state != "running":
            sections.append(("", [(f"Autopilot: {ap.state}" + (f" ({ap.note})" if ap.note else ""), -1)]))
    tree = agents.tree(db, root_id)
    branches = dict(state["branches"])
    live_workers = set()
    live_rows = []
    for a in tree[1:]:
        ws = db.get_workspace(a.workspace_id)
        if a.mode not in WORKER_MODES:
            continue
        now = _row_state(a)
        if not agents.finished_reporting(a) or a.status == "processing" or a.plan_state == "proposed":
            live_workers.add(a.id)
            live_rows.append((f"- {a.id} ({a.profile}, {ws.branch if ws else 'no branch'}): "
                              f"{now}; {_clip(a.task, RESULT_CHARS)}", -1))
        if ws and ws.branch:
            folded = branches.get(ws.branch)
            # The pipeline's events go stale (a branch "in review" whose worker is back at
            # work or waiting on its plan): while a branch is still open, its worker's own
            # row says where it stands.
            if (folded is None or a.id in live_workers
                    or (folded.get("worker") == a.id and folded.get("state") not in FINISHED_BRANCHES)):
                branches[ws.branch] = {"state": now, "worker": a.id}

    def finished(branch: str | None) -> bool:
        info = branches.get(branch, {})
        return info.get("state") in FINISHED_BRANCHES and info.get("worker") not in live_workers

    if branches:
        sections.append(("## Branches", [
            (f"- {b} ({v.get('worker') or '?'}): {v['state']}", 1 if finished(b) else -1)
            for b, v in branches.items()]))
    if live_rows:
        sections.append(("## Live workers", live_rows))
    ids = {a.id for a in tree}
    queued = [t for t in db.list_tasks() if t.caller_id in ids and t.state in (*tasks.QUEUED, "starting")]
    if queued:
        sections.append(("## Queued workers", [
            (f"- {t.id} ({t.profile}, {t.branch or 'no branch'}): {t.state}; "
             f"{_clip(t.task_text, RESULT_CHARS)}", -1) for t in queued]))
    if state["decisions"]:
        sections.append(("## Decisions made", [
            (f"- {d['text']}" + (f" (decision {d['id']})" if d.get("id") else ""), 0)
            for d in state["decisions"]]))
    questions = [q["text"] for q in state["questions"]]
    if ap and ap.question and ap.question not in questions:
        questions.append(ap.question)
    if questions:
        sections.append(("## Open questions for the person", [(f"- {q}", -1) for q in questions] + [
            ("Still unanswered: wait for the reply (or remind them), then call need_user(answered=true).", -1)]))
    if state["answered"]:
        sections.append(("## Recently answered (the reply may be in the latest messages)", [
            (f"- {q['text']}", 0) for q in state["answered"]]))
    if state["workers"]:
        # Results belong to workers, not branches. A completed handoff on the
        # supervisor's branch is history, even when a successor uses that branch.
        # Keep a reported result awaiting review/merge only for its current owner.
        outstanding_results = live_workers | {
            info.get("worker") for branch, info in state["branches"].items()
            if info["state"] not in FINISHED_BRANCHES
            and branches.get(branch, {}).get("worker") == info.get("worker")
        }
        sections.append(("## Last result per worker", [
            (f"- {wid} ({w.get('profile')}, {w.get('branch') or 'no branch'}): {w['result']}",
             -1 if wid in outstanding_results else 1)
            for wid, w in state["workers"].items()]))
    omitted = ("Older completed work and decisions omitted. Fetch the rest with get_progress, "
               "list_agents and read_messages.")
    rows = [(section, index, text, priority) for section, (_, entries) in enumerate(sections)
            for index, (text, priority) in enumerate(entries)]
    keep = {(s, i) for s, i, _, priority in rows if priority < 0 or max_bytes is None}
    if max_bytes is not None:
        # Reserve all headings and the omission notice, conservatively. Essential
        # work may exceed the budget; seed_prompt refuses that restart intact.
        used = sum(len(title.encode()) + 3 for title, _ in sections) + len(omitted.encode()) + 2
        used += sum(len(text.encode()) + 1 for s, i, text, _ in rows if (s, i) in keep)
        for s, i, text, priority in sorted(rows, key=lambda r: (r[3], -r[0], -r[1])):
            if priority < 0:
                continue
            size = len(text.encode()) + 1
            if used + size <= max_bytes:
                keep.add((s, i))
                used += size
    out = []
    for s, (title, entries) in enumerate(sections):
        selected = [text for i, (text, _) in enumerate(entries) if (s, i) in keep]
        if selected:
            out.append((title + "\n" if title else "") + "\n".join(selected))
    if len(keep) < len(rows):
        out.append(omitted)
    return "\n\n".join(out) or "(nothing recorded yet)"


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tail_entries(path: Path) -> list[dict]:
    """The transcript's last complete entries (up to TAIL_BYTES of it)."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            data = f.read()
    except OSError:
        return []
    lines = data.splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]   # starts mid-line
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def context_tokens(path: Path) -> int | None:
    """The context the supervisor's last turn read (its last assistant
    message's input, cached or not), from its Claude Code transcript."""
    for entry in reversed(_tail_entries(path)):
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        u = message.get("usage")
        if isinstance(u, dict):
            try:
                return sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            except (TypeError, ValueError):
                return None
    return None


_SYSTEM_INPUT = ("[brindle", "<details><summary>Message from", "<task-notification>", "<system-reminder>",
                 "<command-", "<local-command-")


def _from_person(who: str, text: str) -> bool:
    """Whether a typed-in message may be the person's own. Their messages from
    the app arrive with brindle's delivery line too (agents.message_lead), so
    only input known to come from an agent or brindle itself is ruled out."""
    if who != "message" or text.startswith("brindle delivered this message from "):
        return False
    body = text.removeprefix("brindle delivered this message:").lstrip()
    return not body.startswith(_SYSTEM_INPUT)


def _line(who: str, text: str) -> str:
    return f"- {who}: {text}"


def latest_messages(path: Path | None, max_bytes: int = MESSAGES_BYTES) -> list[tuple[str, str]]:
    """The chat's latest text messages, oldest first, as (who, text): what was
    typed in ("message": the person, or brindle's deliveries) and what the
    supervisor said ("you"). Each is whole: the oldest are left out to fit
    ``max_bytes``, but never the person's newest message. Tool calls and their
    results are left out (recent_actions digests them)."""
    if path is None:
        return []
    found = []
    for entry in _tail_entries(path):
        message = entry.get("message")
        if entry.get("isMeta") or entry.get("type") not in ("user", "assistant") or not isinstance(message, dict):
            continue
        text = " ".join(_text_of(message.get("content")).split())
        if text and not text.startswith(RESTART_HEAD):
            found.append(("you" if entry["type"] == "assistant" else "message", text))
    person = next((i for i in range(len(found) - 1, -1, -1) if _from_person(*found[i])), None)
    keep = {person} if person is not None else set()
    used = len(_line(*found[person]).encode()) + 1 if person is not None else 0
    for i in range(len(found) - 1, -1, -1):
        if i == person:
            continue
        size = len(_line(*found[i]).encode()) + 1
        if used + size > max_bytes:
            break
        keep.add(i)
        used += size
    return [m for i, m in enumerate(found) if i in keep]


_PR_URL = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
_PROMISE = re.compile(r"[^.!?\n]*\b(?:I'll|I will|I'm|I am)\b[^.!?\n]*"
                      r"\b(?:watch|watching|monitor|monitoring|keep an eye|check back|follow up|wait for CI)"
                      r"[^.!?\n]*[.!?]?", re.IGNORECASE)
_OPERATORS = {"&&", "||", ";", "|", "&", "(", ")"}


def _commands(command: str) -> list[tuple[list[str], str]]:
    """The simple commands in a shell command line, as words (leading
    ``VAR=x`` assignments dropped), each with the operator that follows it
    (``;`` at a line's end)."""
    out = []
    for line in command.splitlines():
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        try:
            words = list(lex)
        except ValueError:   # an unclosed quote: a multi-line argument
            words = line.split()
        current: list[str] = []
        redirect = False
        for word in [*words, ";"]:
            if redirect:     # a redirection's target
                redirect = False
                continue
            if word and word[0] in "<>" and set(word) <= set("<>&|"):
                if current and current[-1].isdigit():
                    current.pop()   # 2>&1's file descriptor
                redirect = True
                continue
            if word and (word in _OPERATORS or set(word) <= set("&|;()")):
                while current and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", current[0]):
                    current = current[1:]
                if current:
                    out.append((current, word))
                current = []
            else:
                current.append(word)   # a quoted empty argument stays one
    return out


def _option(words: list[str], *names: str) -> str | None:
    for i, word in enumerate(words):
        for name in names:
            if word == name and i + 1 < len(words):
                return words[i + 1]
            if name.startswith("--") and word.startswith(name + "="):
                return word[len(name) + 1:]
    return None


def _git_sub(words: list[str]) -> tuple[str | None, list[str]]:
    """A git command's subcommand and the words after it (past ``-C dir`` and the like)."""
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in ("-C", "-c") else 1
    return (words[i], words[i + 1:]) if i < len(words) else (None, [])


_PUSH_FAILED = ("[rejected]", "[remote rejected]", "error:", "fatal:")


def _action(words: list[str], output: str, last_push: str | None) -> dict | None:
    """What one command would do outwardly, if anything: ``did`` (said once
    it is confirmed), ``tried`` (said otherwise), ``proven`` (its own output
    shows it succeeded) and ``pushed`` (the ref a push targets)."""
    shown = " ".join(w if w and " " not in w else repr(w) for w in words)
    if words[0] == "git":
        sub, rest = _git_sub(words)
        if sub == "push":
            positional = [w for w in rest if w and not w.startswith("-")]
            dst = positional[-1].split(":")[-1] if len(positional) > 1 else None
            proven = bool(dst) and f"-> {dst}" in output and not any(m in output for m in _PUSH_FAILED)
            return {"did": "pushed: " + shown, "tried": shown, "proven": proven, "pushed": dst}
        if sub == "worktree" and rest[:1] == ["add"]:
            return {"did": "added a worktree: " + shown, "tried": shown}
        return None
    if words[0] != "gh" or len(words) < 3:
        return None
    noun, verb = words[1], words[2]
    if (noun, verb) == ("pr", "create"):
        url = _PR_URL.search(output)
        head = _option(words, "--head", "-H")
        base = _option(words, "--base", "-B")
        repo = _option(words, "--repo", "-R") or (url.group(1) if url else None)
        where = [f"head {head}" if head else
                 f"head: your current branch (last pushed: {last_push})" if last_push else "head: your current branch"]
        if base:
            where.append(f"base {base}")
        if repo:
            where.append(f"repo {repo}")
        title = _option(words, "--title", "-t")
        about = f"({', '.join(where)})" + (f": {title}" if title else "")
        if url:   # the PR's link is the proof it exists
            return {"did": f"opened PR #{url.group(2)} {about}", "proven": True}
        return {"did": f"ran gh pr create, PR number unknown {about}", "tried": f"gh pr create {about}"}
    if noun == "pr" and verb == "merge":
        return {"did": "merged: " + shown, "tried": shown}
    if (noun == "pr" and verb == "checks") or (noun == "run" and verb in ("watch", "view")):
        # A failing check fails the command: still CI watched, not a broken action.
        return {"did": "watched CI: " + shown, "tried": shown, "failed": "watched CI, which failed or hadn't finished: "}
    return None


def _outcome(action: dict, result: tuple[str, bool] | None, chained: bool) -> tuple[str, bool]:
    """How a command's action reads in the digest, and whether it is
    confirmed. ``chained``: only ``&&`` follows it, so the whole command line
    succeeding means it did too."""
    if action.get("proven"):
        return action["did"], True
    tried = action.get("tried", action["did"])
    if result is None:
        return "no result seen yet: " + tried, False
    if result[1]:
        return action.get("failed", "not confirmed, the command failed: ") + tried, False
    if chained:
        return action["did"], True
    return "ran, outcome not confirmed (piped or followed by other commands): " + tried, False


def recent_actions(path: Path | None) -> list[str]:
    """A digest of the supervisor's own recent outward actions, oldest first,
    from its transcript's tool calls and results: the PRs it opened (number,
    head and base branch), pushes, worktrees it added, CI it watched, and the
    open promises it made ("I'm watching CI on #N"). Bounded by ACTIONS_BYTES,
    newest kept."""
    if path is None:
        return []
    entries = _tail_entries(path)
    results: dict[str, tuple[str, bool]] = {}   # a tool call's output, and whether it failed
    for entry in entries:
        content = (entry.get("message") or {}).get("content") if isinstance(entry.get("message"), dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                results[block["tool_use_id"]] = (_text_of(block.get("content")), bool(block.get("is_error")))
    actions: list[str] = []
    promises: list[str] = []
    last_push = None
    for entry in entries:
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                promises += [" ".join(m.group(0).split()) for m in _PROMISE.finditer(block.get("text") or "")]
                continue
            command = (block.get("input") or {}).get("command") if block.get("type") == "tool_use" else None
            if block.get("name") not in ("Bash", "Monitor") or not isinstance(command, str):
                continue
            result = results.get(block.get("id"))
            commands = _commands(command)
            # Only a plain `a && b && c` line that succeeded ran every command in it.
            chained = all(op == "&&" for _, op in commands[:-1])
            # The call's output proves a command (a PR link, a push's refs) only when no
            # other command could have printed it: it heads the line, and all that follows
            # it filters its output (`git push … 2>&1 | tail -3`).
            attributable = all(op == "|" for _, op in commands[:-1])
            for i, (words, _) in enumerate(commands):
                output = result[0] if result and i == 0 and attributable else ""
                action = _action(words, output, last_push)
                if action is None:
                    continue
                said, confirmed = _outcome(action, result, chained)
                if confirmed and action.get("pushed"):
                    last_push = action["pushed"]
                actions.append(_clip(said, ACTION_CHARS))
    lines = [*actions, *(f"you said: {_clip(p, ACTION_CHARS)}" for p in promises[-PROMISES:])]
    out: list[str] = []
    used = 0
    for line in reversed(lines):   # newest first; a repeat counts once, at its latest
        if line in out:
            continue
        used += len(line.encode()) + 3
        if used > ACTIONS_BYTES:
            break
        out.append(line)
    return out[::-1]


SEED = (f"{RESTART_HEAD} to keep it small: this is a fresh session of the same supervisor "
        "({root_id}), and the earlier chat is gone. Your goal, workers, branches, queued tasks and "
        "messages are all unchanged. Carry on from this state, which brindle keeps up to date "
        "(get_progress, list_agents and read_messages give the details), and from the latest "
        "messages below. Don't redo work the state says is done.")


class SeedTooLarge(ValueError):
    """Current work alone exceeds the safe budget: retain the existing chat."""


def seed_prompt(db: DB, root_id: str, transcript: Path | None = None) -> str:
    """The first prompt of a restarted supervisor: the state and the latest messages."""
    from brindle.pro.team_policy import effective_supervisor_restart_tokens

    root = db.get_agent(root_id)
    ws = db.get_workspace(root.workspace_id) if root else None
    limit = effective_supervisor_restart_tokens(ws.repo_root) if ws else 0
    budget = min(SEED_BYTES, limit // 4) if limit else SEED_BYTES
    state = refresh(db, root_id)
    parts = [SEED.format(root_id=root_id)]
    if root and root.task:
        parts.append("## The request this session started from\n" + _clip(root.task, 2000))
    at = len(parts)
    actions = recent_actions(transcript)
    if actions:
        parts.append("## Your recent actions (from this chat's tool calls: check them before you push, "
                     "open a PR or watch CI again)\n" + "\n".join(f"- {a}" for a in actions))
    # Current work first, then the latest messages, then history with what is left.
    essential = render(db, root_id, state, max_bytes=0)
    heading = "## Latest messages\n"
    room = budget - len("\n\n".join([*parts, essential]).encode()) - 2 - len(heading.encode())
    latest = latest_messages(transcript, max_bytes=max(0, min(MESSAGES_BYTES, room)))
    if latest:
        parts.append(heading + "\n".join(_line(who, text) for who, text in latest))
    remaining = budget - len("\n\n".join(parts).encode()) - 2
    parts.insert(at, render(db, root_id, state, max_bytes=remaining))
    seed = "\n\n".join(parts)
    if len(seed.encode()) > budget:
        raise SeedTooLarge("the supervisor's current work exceeds its restart seed budget")
    return seed


# -- the restart ------------------------------------------------------------------------


def enabled(db: DB, agent: Agent) -> bool:
    """Whether ``agent`` is a session's supervisor in a repo with ``supervisor_state`` on."""
    if agent.mode != "interactive" or agent.parent_id:
        return False
    from brindle.config import load_repo_config

    ws = db.get_workspace(agent.workspace_id)
    try:
        return bool(ws and load_repo_config(ws.repo_root).supervisor_state)
    except ValueError:
        return False


def _claim(db: DB, root: Agent, now: float) -> str | None:
    """Claim session ``root``'s restart for the session it runs now (its
    ``session_ref``), holding its queue (db.RESTART_HOLD) in the same step.
    The claim's token, or None while another restart holds it."""
    token = secrets.token_hex(8)
    with db.tx() as c:
        if _restart_holds(c, root.id, now) or _deliveries_in_flight(c, root.id):
            return None   # mail part way to this session goes in first: a later turn restarts
        c.execute("INSERT INTO agent_marks (agent_id, kind, value) VALUES (?, ?, ?)"
                  " ON CONFLICT(agent_id, kind) DO UPDATE SET value=excluded.value",
                  (root.id, RESTART_HOLD, json.dumps({"token": token, "ref": root.session_ref,
                                                       "until": now + RESTART_CLAIM})))
    return token


def _held_claim(db: DB, root_id: str, token: str) -> dict | None:
    """The restart claim ``token`` names, while it is still the one holding."""
    row = db.conn.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind=?",
                          (root_id, RESTART_HOLD)).fetchone()
    try:
        claim = json.loads(row["value"]) if row else None
    except ValueError:
        return None
    if not claim or claim.get("token") != token or float(claim.get("until") or 0) <= time.time():
        return None
    return claim


def restart_lock(root_id: str):
    """Held, across processes, by a restart from its last look at the session
    through stopping it, and by each of its launch's commits (``_gate``); taken
    by ``cancel``, so a pause or new turn and the restart never interleave."""
    from brindle import pipeline

    key = hashlib.sha1(root_id.encode()).hexdigest()[:16]
    return pipeline._reentrant_lock(f"supervisor-restart-{key}", timeout=RESTART_LOCK_WAIT)


def cancel(db: DB, root_id: str, session: str | None = None) -> None:
    """The person paused session ``root_id``, or a new turn began in it
    (``session``: the CLI session it began in; None: any): a restart claimed
    for it no longer applies. A launch the restart is committing ends first;
    nothing of it starts after this returns. A new turn in another session (the
    fresh one the restart started) leaves the claim be."""
    from brindle import pipeline

    row = db.conn.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind=?",
                          (root_id, RESTART_HOLD)).fetchone()
    if row is None:
        return
    try:
        ref = json.loads(row["value"]).get("ref")
    except ValueError:
        ref = None
    if session is not None and ref is not None and session != ref:
        return
    try:
        with restart_lock(root_id):
            _drop_claim(db, root_id)
    except pipeline.LockTimeout:
        log.warning("brindle: supervisor %s's restart held its lock too long; cancelling it anyway", root_id)
        _drop_claim(db, root_id)


def _drop_claim(db: DB, root_id: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind=?", (root_id, RESTART_HOLD))


@contextmanager
def _gate(db: DB, root_id: str, token: str):
    """The restart's resume gate (agents.resume): its launch goes ahead, and
    commits, only while claim ``token`` stands; else agents.Cancelled."""
    from brindle import agents

    with restart_lock(root_id):
        if _held_claim(db, root_id, token) is None:
            raise agents.Cancelled("the supervisor's restart was cancelled")
        yield


def _release(db: DB, root_id: str, token: str) -> None:
    """Give up claim ``token``: the queue is delivered again. Another claim is left alone."""
    with db.tx() as c:
        row = c.execute("SELECT value FROM agent_marks WHERE agent_id=? AND kind=?",
                        (root_id, RESTART_HOLD)).fetchone()
        try:
            mine = row is not None and json.loads(row["value"]).get("token") == token
        except ValueError:
            mine = False
        if mine:
            c.execute("DELETE FROM agent_marks WHERE agent_id=? AND kind=?", (root_id, RESTART_HOLD))


def previous_sessions(db: DB, root_id: str) -> list[dict]:
    """Transcripts retained across CLI restarts, oldest first."""
    return json.loads(db.mark(root_id, "restart-history") or "[]")


def _remember_transcript(db: DB, root: Agent, transcript: Path | None) -> None:
    # Kept separately from the folded state so a concurrent refresh cannot
    # overwrite it. The resume/restart locks serialize additions per root.
    if not root.session_ref:
        return
    history = previous_sessions(db, root.id)
    if any(item["session_ref"] == root.session_ref for item in history):
        return
    history.append({"session_ref": root.session_ref, "provider": root.provider,
                    "transcript_path": str(transcript) if transcript else root.transcript_path})
    db.set_mark(root.id, "restart-history", json.dumps(history))


def on_turn_end(db: DB, agent: Agent, payload: dict) -> bool:
    """The supervisor's turn ended (its Stop hook): bring its state up to
    date and, once its context is past the repo's threshold, start its
    restart. True when a restart was started: the turn should then end as it
    is, leaving queued messages for the fresh session."""
    if not enabled(db, agent):
        return False
    from brindle.pro.team_policy import effective_supervisor_restart_tokens

    refresh(db, agent.id)
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return False
    tokens = context_tokens(Path(path))
    ws = db.get_workspace(agent.workspace_id)
    limit = effective_supervisor_restart_tokens(ws.repo_root)
    if tokens is None or not limit or tokens < limit:
        return False
    last_restart = db.mark(agent.id, "supervisor-restarted-at")
    if last_restart and time.time() - float(last_restart) < RESTART_COOLDOWN:
        return False
    agent = db.get_agent(agent.id) or agent    # its session_ref as this hook just recorded it
    token = _claim(db, agent, time.time())
    if token is None:
        return False
    from brindle import agents
    from brindle.providers import brindle_invocation

    try:
        # Not a child of the supervisor's processes: stopping them must not stop the restart.
        agents._detach_orphan([*brindle_invocation(), "_restart-supervisor", agent.id, token, path])
    except OSError:
        log.warning("brindle: couldn't start supervisor %s's restart", agent.id, exc_info=True)
        _release(db, agent.id, token)
        return False
    log.info("brindle: supervisor %s at %d context tokens: restarting it from its state", agent.id, tokens)
    return True


def restart(db: DB, root_id: str, token: str, transcript: Path | None = None) -> str | None:
    """Carry out restart claim ``token``: stop supervisor ``root_id``'s CLI and
    start a fresh session of it seeded with its state and the latest messages
    of ``transcript``. Its workers keep running. Returns the seed, or None
    when the restart no longer applies and nothing was touched: the claim is
    gone or another's (a second helper), or since the claim the person paused
    the session, a new turn began or another session of it started.

    The old ``session_ref`` stays until the fresh session's hooks replace
    it: if the launch fails, the session stays paused and an ordinary resume
    picks the full chat back up, so nothing is lost."""
    from brindle import agents

    try:
        with agents.resume_lock(root_id):
            if _held_claim(db, root_id, token) is None:
                return None
            try:
                seed = seed_prompt(db, root_id, transcript)
            except SeedTooLarge:
                log.warning("brindle: supervisor %s has too much current work for a safe restart", root_id)
                return None
            # No paste into its pane under way, no pause or new turn meanwhile
            # (cancel waits on restart_lock): checked immediately before the stop.
            with agents.delivery_lock(root_id), restart_lock(root_id):
                claim = _held_claim(db, root_id, token)
                if claim is None:
                    return None
                root = db.get_agent(root_id)
                if (root is None or root.status != "idle" or root.session_ref != claim.get("ref")
                        or db.deliveries_in_flight(root_id)):
                    log.info("brindle: supervisor %s's restart no longer applies; left as it is", root_id)
                    return None
                # Persist before the launch's hooks can replace the transcript.
                # Readers exclude the active ref, including after a failed launch.
                _remember_transcript(db, root, transcript)
                # Paused before its CLI stops, so the stop isn't taken for the person closing the chat
                # (agents.ended).
                db.set_status(root_id, "paused")
                agents._stop(db, root)
            try:
                # The launch commits only while the claim stands (_gate): a pause from here on wins.
                agents.resume(db, root_id, only={root_id}, prompts={root_id: seed},
                              gate=lambda: _gate(db, root_id, token))
            except agents.Cancelled:
                log.info("brindle: supervisor %s's restart was cancelled before its launch", root_id)
                return None
            db.set_mark(root_id, "supervisor-restarted-at", str(time.time()))
        with db.tx() as c:
            c.execute("UPDATE supervisor_state SET restarts=restarts+1 WHERE root_id=?", (root_id,))
        bus.publish_agent(db, "supervisor.restarted", root_id)
        return seed
    finally:
        _release(db, root_id, token)   # the queue is the fresh session's now (or, on failure, the old one's)
