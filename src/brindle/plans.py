"""Planning docs: a supervisor's plan for a large piece of work, written as
markdown that brindle renders itself (trusted, no frame), read and answered
by the person before any worker starts.

A plan is made only when the person explicitly asked for one (the
``plan``-scoped request grant in brindle.visual_offers): never on the
supervisor's own judgement that a request is large. It is a ``visuals`` row
of kind ``plan`` (so it lists, keeps and exports like any visual) with a
``plans`` row beside it: the markdown, the decisions that shape it with
their alternatives, and the proposed split into workers.

While a session's plan is open or redirected, every task its agents assign
is queued and stays queued (``holding``; brindle.tasks checks it before any
start). Only the person decides, from the app: Approve releases the queued
tasks; Redirect sends their picks and per-section comments back to the
supervisor as a message from them and lets it publish one new revision. An
approval from an agent is refused. Like the rest of the visualizer, plans
are behind the visuals capability (``visuals.enabled``), but not behind the
Visuals setting: that only governs offers."""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import secrets
import time
from typing import Any

from brindle import bus, procs, visual_offers, visuals
from brindle.db import DB, Agent
from brindle.service.errors import ServiceError

MAX_MARKDOWN = 256 * 1024
MAX_DECISIONS = 12
MAX_WORKERS = 20
MAX_COMMENTS = 50
HOLDING = ("open", "deciding", "redirected")   # 'deciding': a verdict on its way to the supervisor

_HEADING = re.compile(r"^##\s+(.*?)\s*#*\s*$")
_TOP = re.compile(r"^#\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


# -- the markdown's sections ------------------------------------------------------------


def sections(markdown: str) -> tuple[str, list[dict]]:
    """The text before the first ``##`` heading (the lede), and each ``##``
    section as ``{id, title, markdown}``: what the person's comments anchor
    to. Headings inside fenced code don't count. Ids are ``s1``, ``s2``, ...
    in order, stable for a revision."""
    lede: list[str] = []
    out: list[dict] = []
    fence = None
    for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        f = _FENCE.match(line)
        if f:
            fence = None if fence == f.group(1) else fence or f.group(1)
        m = None if fence or f else _HEADING.match(line)
        if m:
            out.append({"id": f"s{len(out) + 1}", "title": m.group(1), "lines": []})
        else:
            (out[-1]["lines"] if out else lede).append(line)
    return "\n".join(lede).strip(), [{"id": s["id"], "title": s["title"], "markdown": "\n".join(s["lines"]).strip()}
                                     for s in out]


def _title(markdown: str) -> str | None:
    fence = None
    for line in markdown.splitlines():
        f = _FENCE.match(line)
        if f:
            fence = None if fence == f.group(1) else fence or f.group(1)
            continue
        m = None if fence else _TOP.match(line)
        if m and m.group(1).strip():
            return m.group(1).strip()[:visuals.MAX_TITLE]
    return None


# -- checks ------------------------------------------------------------------------------


def _claimant_alive(claim: str | None) -> bool:
    """Whether the process that claimed a verdict (``<pid>:<nonce>``) still
    runs, so its delivery may still finish. An unreadable claim counts as alive."""
    pid = (claim or "").partition(":")[0]
    return not pid.isdigit() or procs.alive(int(pid))


def _decisions(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_DECISIONS:
        raise ServiceError("invalid", f"decisions must be a list of at most {MAX_DECISIONS}", 400)
    out, seen = [], set()
    for d in raw:
        if not isinstance(d, dict):
            raise ServiceError("invalid", "each decision is {id, title, options, mine?, challenge?}", 400)
        did = d.get("id")
        if not isinstance(did, str) or not visuals.OPTION_ID.fullmatch(did) or did in seen:
            raise ServiceError("invalid", "each decision needs its own id of 1 to 32 letters, digits, _ or -", 400)
        seen.add(did)
        options = visuals._options(d.get("options"))
        if not options:
            raise ServiceError("invalid", f"decision {did} needs 2 or more options", 400)
        mine = d.get("mine")
        if mine is not None and mine not in {o["id"] for o in options}:
            raise ServiceError("invalid", f"decision {did}'s pick isn't one of its options", 400)
        out.append({"id": did, "title": visuals._text(d.get("title"), "a decision's title", visuals.MAX_QUESTION, True),
                    "options": options, "mine": mine,
                    "challenge": visuals._text(d.get("challenge"), "a decision's challenge", visuals.MAX_DETAIL)})
    return out


def _split(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_WORKERS:
        raise ServiceError("invalid", f"split must be a list of at most {MAX_WORKERS} workers", 400)
    out, seen = [], set()
    for w in raw:
        if not isinstance(w, dict):
            raise ServiceError("invalid", "each worker in the split is {id, title, after?}", 400)
        wid = w.get("id")
        if not isinstance(wid, str) or not visuals.OPTION_ID.fullmatch(wid) or wid in seen:
            raise ServiceError("invalid", "each worker in the split needs its own id", 400)
        after = w.get("after") or []
        # Only earlier workers: a split is an order, never a cycle.
        if not isinstance(after, list) or any(a not in seen for a in after):
            raise ServiceError("invalid", f"worker {wid} may only come after workers listed before it", 400)
        seen.add(wid)
        out.append({"id": wid, "title": visuals._text(w.get("title"), "a worker's title", visuals.MAX_LABEL, True),
                    "after": list(after)})
    return out


# -- rows --------------------------------------------------------------------------------


def _row(db: DB, plan_id: str) -> dict:
    row = None
    if isinstance(plan_id, str) and visuals.ID.fullmatch(plan_id):
        row = db.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise ServiceError("not_found", "no such plan", 404)
    p = dict(row)
    p["decisions"] = json.loads(p["decisions"])
    p["split"] = json.loads(p["split"])
    p["decision"] = json.loads(p["decision"]) if p["decision"] else None
    return p


def get(db: DB, plan_id: str) -> dict:
    """The plan and its visual's row, as ``{**plan, "visual": {...}}``."""
    p = _row(db, plan_id)
    p["visual"] = visuals.get(db, p["id"])
    return p


def holding(db: DB, root_id: str) -> dict | None:
    """The plan session ``root_id``'s queued tasks wait on, if any: one the
    person hasn't approved yet. None while the visuals capability is off,
    so nothing waits on a plan nobody can see."""
    if not visuals.enabled() or not root_id:
        return None
    row = db.conn.execute(f"SELECT id FROM plans WHERE root_id=? AND state IN ({','.join('?' * len(HOLDING))}) "
                          "ORDER BY created_at LIMIT 1", (root_id, *HOLDING)).fetchone()
    return _row(db, row["id"]) if row else None


def _queued(db: DB, root_id: str) -> int:
    from brindle import autopilot, tasks

    return sum(1 for t in db.list_tasks(state="pending")
               if t.caller_id and autopilot.root_of(db, t.caller_id) == root_id and tasks.plan_holds(db, t))


def to_dto(db: DB, p: dict) -> dict:
    from brindle.service.dto import PlanDTO

    lede, parts = sections(p["markdown"])
    return PlanDTO(id=p["id"], session=p["root_id"], title=p["visual"]["title"], rev=p["rev"],
                   state="open" if p["state"] == "deciding" else p["state"],
                   markdown=p["markdown"], lede=lede, sections=parts, decisions=p["decisions"], split=p["split"],
                   verdict=None if p["state"] == "deciding" else p["decision"], queued=_queued(db, p["root_id"]) if p["state"] in HOLDING else 0,
                   created_at=p["created_at"]).to_json()


def _page(title: str, markdown: str) -> str:
    """The plan's page in the visuals store, what Keep copies: brindle's own
    HTML, the markdown escaped as text (it is never parsed as HTML)."""
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{html_lib.escape(title)}</title>"
            "<style>body{font:14px/1.6 system-ui,sans-serif;max-width:720px;margin:24px auto;padding:0 16px}"
            "pre{white-space:pre-wrap;font:inherit}</style></head>"
            f"<body><h1>{html_lib.escape(title)}</h1><pre>{html_lib.escape(markdown)}</pre></body></html>")


# -- the supervisor proposes ------------------------------------------------------------


def propose(db: DB, caller: Agent | None, markdown: str, decisions: Any = None, split: Any = None,
            title: str | None = None, plan_id: str | None = None) -> dict:
    """The session's supervisor proposes a plan, or (``plan_id``) a new
    revision of its open or redirected one. Each publication spends the
    person's request: their ask for a planning doc, or their Redirect."""
    visuals.require_enabled()
    root, _ws = visuals._session(db, caller)
    if caller.id != root.id:
        raise ServiceError("forbidden", "only the session's supervisor proposes a plan", 403)
    if not isinstance(markdown, str) or not markdown.strip():
        raise ServiceError("invalid", "markdown is required", 400)
    markdown = markdown.strip()
    if len(markdown.encode("utf-8")) > MAX_MARKDOWN:
        raise ServiceError("too_big", f"a plan may be at most {MAX_MARKDOWN // 1024} KB of markdown", 400)
    decisions, split = _decisions(decisions), _split(split)
    title = visuals._text(title, "title", visuals.MAX_TITLE) or _title(markdown) or "Plan"
    visuals._refuse_secrets([
        ("the plan", markdown), ("the title", title),
        *((f"decision {d['id']}", "\n".join([d["title"], d["challenge"] or "",
                                              *(f"{o['label']}\n{o['detail'] or ''}" for o in d["options"])]))
          for d in decisions),
        *((f"worker {w['id']}", w["title"]) for w in split)])
    open_plan = holding(db, root.id)
    if plan_id is None and open_plan is not None:
        raise ServiceError("open_plan", f"plan {open_plan['id']} is still open: revise it with plan_id, "
                           "or wait for the person's decision", 409)
    if plan_id is not None:
        current = _row(db, plan_id)
        if current["root_id"] != root.id:
            raise ServiceError("not_found", "no such plan in this session", 404)
        if current["state"] not in HOLDING:
            raise ServiceError("approved", "that plan is approved; propose a new plan instead", 409)

    def write(c, vid: str, rev: int) -> None:
        now = time.time()
        args = (rev, markdown, json.dumps(decisions), json.dumps(split), now)
        if rev == 1:
            c.execute("INSERT INTO plans (id, root_id, rev, markdown, decisions, split, state, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,'open',?,?)", (vid, root.id, *args[:4], now, now))
        else:
            # Only a plan still waiting on the person takes a revision: one
            # approved since the check above stays approved.
            changed = c.execute("UPDATE plans SET rev=?, markdown=?, decisions=?, split=?, updated_at=?, state='open',"
                                " decision=NULL WHERE id=? AND state IN ('open','redirected')", (*args, vid)).rowcount
            if not changed:
                raise ServiceError("approved", "that plan is approved; propose a new plan instead", 409)

    try:
        v = visuals.publish(db, caller, title, _page(title, markdown), visual_id=plan_id, kind="plan", extra=write)
    except ServiceError as e:
        if e.code == "visual_request_required":
            raise ServiceError("plan_request_required", "a plan needs the person to ask for one: propose_plan "
                               "only after they asked for a planning doc (or redirected this one)", 403) from e
        raise
    return get(db, v["id"])


# -- the person decides -----------------------------------------------------------------


def _picks(raw: Any, decisions: list[dict]) -> dict[str, str]:
    if raw is None:
        return {}
    by_id = {d["id"]: {o["id"] for o in d["options"]} for d in decisions}
    if not isinstance(raw, dict) or any(k not in by_id or v not in by_id[k] for k, v in raw.items()):
        raise ServiceError("invalid", "a pick names one of this revision's decisions and one of its options", 400)
    return dict(raw)


def _comments(raw: Any, section_ids: set[str]) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_COMMENTS:
        raise ServiceError("invalid", f"comments must be a list of at most {MAX_COMMENTS}", 400)
    out = []
    for c in raw:
        if not isinstance(c, dict) or c.get("section") not in section_ids:
            raise ServiceError("invalid", "a comment names one of this revision's sections", 400)
        text = visuals._text(c.get("text"), "a comment", visuals.MAX_NOTE)
        if text:
            out.append({"section": c["section"], "text": text})
    return out


def _message(p: dict, approved: bool, picks: dict, comments: list[dict], note: str | None, titles: dict) -> str:
    verb = "approved" if approved else "redirected"
    lines = [f"The person {verb} plan {p['id']} (r{p['rev']})."]
    for d in p["decisions"]:
        pick = picks.get(d["id"])
        if pick and pick != d["mine"]:
            label = next(o["label"] for o in d["options"] if o["id"] == pick)
            lines.append(f"Decision {d['id']} ({d['title']}): they chose {pick} ({label}) over yours.")
    for c in comments:
        lines.append(f"On \"{titles[c['section']]}\": {c['text']}")
    if note:
        lines.append(f"Their note: {note}")
    if approved:
        lines.append("The tasks you assigned behind the plan start now; assign the rest as planned.")
    else:
        lines.append(f"Revise it with propose_plan(plan_id=\"{p['id']}\"); nothing starts until they approve.")
    return "\n".join(lines)


def decide(db: DB, plan_id: str, rev: Any, approved: bool, picks: Any = None, comments: Any = None,
           note: Any = None, *, person: bool) -> dict:
    """The person's verdict on the revision on their screen (``rev``):
    approve, or redirect with their picks and per-section comments. Only the
    person (the app, through the daemon's guarded route) decides: anyone
    else is refused. The verdict is claimed in the row first ('deciding',
    which still holds the session's tasks), so a second click is refused
    instead of delivered twice; if the message can't be delivered, the claim
    is undone and the plan is open again, with nothing started. Only once
    the message is delivered is the verdict final: an approval then releases
    the queued tasks, a redirect grants the one revision it asks for."""
    from brindle import agents, tasks

    visuals.require_enabled()
    if not person:
        raise ServiceError("forbidden", "only the person approves or redirects a plan, from the app", 403)
    p = get(db, plan_id)
    visuals._unlocked(p["root_id"])
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise ServiceError("invalid", "rev (the revision on screen) is required", 400)
    if rev != p["rev"]:
        raise ServiceError("stale", f"the plan has a newer revision (r{p['rev']}); read that one", 409, rev=p["rev"])
    if p["state"] not in ("open", "deciding"):   # a lapsed 'deciding' claim may be taken again below
        raise ServiceError("decided", "this revision is already decided", 409)
    _lede, parts = sections(p["markdown"])
    titles = {s["id"]: s["title"] for s in parts}
    picks = _picks(picks, p["decisions"])
    comments = _comments(comments, set(titles))
    note = visuals._text(note, "note", visuals.MAX_NOTE)
    if not approved and not comments and not note and not any(picks.get(d["id"]) not in (None, d["mine"])
                                                              for d in p["decisions"]):
        raise ServiceError("invalid", "say what to change: a comment, a note or a different pick", 400)
    visuals._refuse_secrets([("your note", note), *((f"your comment on {titles[c['section']]}", c["text"])
                                                    for c in comments)])
    at = time.time()
    verdict = {"approved": bool(approved), "picks": picks, "comments": comments, "note": note, "at": at, "rev": rev}
    record = json.dumps(verdict)
    answer = json.dumps({"option": "approve" if approved else "redirect", "note": note, "at": at, "rev": rev})
    state = "approved" if approved else "redirected"
    # Claimed as 'deciding', which still holds the session's tasks: nothing
    # starts until the supervisor has the message and the verdict is final.
    # The claim names this process: only a claim whose process is gone (it
    # died mid-delivery) may be taken over, never one merely slow.
    claim = f"{os.getpid()}:{secrets.token_hex(8)}"
    with db.tx() as c:
        claimed = c.execute("UPDATE plans SET state='deciding', decision=?, claim=?, updated_at=? WHERE id=? AND rev=?"
                            " AND state='open'", (record, claim, at, p["id"], rev)).rowcount == 1
    if not claimed:
        now = _row(db, p["id"])
        if now["rev"] != rev:
            raise ServiceError("stale", f"the plan has a newer revision (r{now['rev']}); read that one", 409,
                               rev=now["rev"])
        if now["state"] != "deciding" or _claimant_alive(now["claim"]):
            raise ServiceError("decided", "this revision is already decided", 409)
        with db.tx() as c:   # compare-and-swap: only that same dead claim
            claimed = c.execute("UPDATE plans SET decision=?, claim=?, updated_at=? WHERE id=? AND rev=? AND "
                                "state='deciding' AND claim IS ?", (record, claim, at, p["id"], rev,
                                                                    now["claim"])).rowcount == 1
        if not claimed:
            raise ServiceError("decided", "this revision is already decided", 409)
    try:
        agents.send_message(db, p["root_id"], _message(p, approved, picks, comments, note, titles), person=True)
    except BaseException:
        with db.tx() as c:
            c.execute("UPDATE plans SET state='open', decision=NULL, claim=NULL WHERE id=? AND state='deciding' "
                      "AND claim=?", (p["id"], claim))
        raise
    with db.tx() as c:
        # Every side effect of the verdict only with the claim still ours.
        finalized = c.execute("UPDATE plans SET state=?, claim=NULL, updated_at=? WHERE id=? AND state='deciding' "
                              "AND claim=?", (state, time.time(), p["id"], claim)).rowcount == 1
        if finalized:
            c.execute("UPDATE visuals SET answer=?, read_rev=MAX(read_rev, rev) WHERE id=?", (answer, p["id"]))
            if not approved:
                visual_offers._grant(c, p["root_id"], "plan")   # the one revision the redirect asks for
    if not finalized:
        raise ServiceError("decided", "this revision was decided elsewhere meanwhile", 409)
    bus.publish(db, "visual.answered", repo_root=p["visual"]["repo_root"], root_id=p["root_id"], visual=p["id"],
                rev=rev, option=state)
    if approved:
        tasks.on_resumed(db, p["root_id"], why="the person's approval of the plan")
    return get(db, p["id"])
