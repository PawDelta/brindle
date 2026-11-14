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
import secrets
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
MESSAGE_CHARS = 800        # of each latest message
LATEST_MESSAGES = 8
KEEP_ANSWERED = 3          # answered questions kept, for the replies in the latest messages
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
        if not agents.finished_reporting(a) or a.status == "processing":
            live_workers.add(a.id)
            live_rows.append((f"- {a.id} ({a.profile}, {ws.branch if ws else 'no branch'}): "
                              f"{a.pipeline or a.status}; {_clip(a.task, RESULT_CHARS)}", -1))
        if ws and ws.branch:
            if ws.branch not in branches or a.id in live_workers:
                branches[ws.branch] = {"state": a.pipeline or a.status, "worker": a.id}

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


def latest_messages(path: Path | None, n: int = LATEST_MESSAGES) -> list[tuple[str, str]]:
    """The chat's last ``n`` text messages, oldest first, as (who, text):
    what was typed in ("message": the person, or brindle's deliveries) and
    what the supervisor said ("you"). Tool calls and their results are left out."""
    if path is None:
        return []
    found = []
    for entry in _tail_entries(path):
        message = entry.get("message")
        if entry.get("isMeta") or entry.get("type") not in ("user", "assistant") or not isinstance(message, dict):
            continue
        text = _text_of(message.get("content")).strip()
        if text and not text.startswith(RESTART_HEAD):
            found.append(("you" if entry["type"] == "assistant" else "message", _clip(text, MESSAGE_CHARS)))
    return found[-n:]


SEED = (f"{RESTART_HEAD} to keep it small: this is a fresh session of the same supervisor "
        "({root_id}), and the earlier chat is gone. Your goal, workers, branches, queued tasks and "
        "messages are all unchanged. Carry on from this state, which brindle keeps up to date "
        "(get_progress, list_agents and read_messages give the details), and from the latest "
        "messages below. Don't redo work the state says is done.")


class SeedTooLarge(ValueError):
    """Current work alone exceeds the safe budget: retain the existing chat."""


def seed_prompt(db: DB, root_id: str, transcript: Path | None = None) -> str:
    """The first prompt of a restarted supervisor: the state and the latest messages."""
    root = db.get_agent(root_id)
    parts = [SEED.format(root_id=root_id)]
    if root and root.task:
        parts.append("## The request this session started from\n" + _clip(root.task, 2000))
    latest = latest_messages(transcript)
    if latest:
        parts.append("## Latest messages\n" + "\n".join(f"- {who}: {text}" for who, text in latest))
    from brindle.config import load_repo_config

    ws = db.get_workspace(root.workspace_id) if root else None
    limit = load_repo_config(ws.repo_root).supervisor_restart_tokens if ws else 0
    budget = min(SEED_BYTES, limit // 4) if limit else SEED_BYTES
    remaining = budget - len("\n\n".join(parts).encode()) - 2
    parts.insert(2 if root and root.task else 1, render(db, root_id, max_bytes=remaining))
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
    from brindle.config import load_repo_config

    refresh(db, agent.id)
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return False
    tokens = context_tokens(Path(path))
    ws = db.get_workspace(agent.workspace_id)
    limit = load_repo_config(ws.repo_root).supervisor_restart_tokens
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
