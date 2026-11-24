"""Planning docs: a supervisor's plan for a large piece of work, written as
markdown that brindle renders itself (trusted, no frame), read and answered
by the person before any worker starts.

A plan is made only when the person explicitly asked for one (the
``plan``-scoped request grant in brindle.visual_offers): never on the
supervisor's own judgement that a request is large. brindle itself may ask
them, once per big goal (brindle.autopilot.consider_plan_offer): "This looks
big: plan it first?". While that offer is open the session's tasks queue as
behind a plan; Yes grants the plan request and keeps them queued until the
plan is approved, No releases them (``answer_offer``). It is a ``visuals`` row
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
Visuals setting: that only governs offers.

A plan shared for team review (brindle.pro.shared_docs) has a build gate
(``gate``): it opens when every required reviewer and org review rule has
signed off the revision on screen. One sign-off counts for every rule it
meets. A reviewer's or rule signer's open comment or suggestion written after
their sign-off is how they ask for changes (the Library has no verdict of its
own), and blocks it too. While the gate is closed Approve is refused and
assign is refused. A shared plan's queued tasks wait for its approval even
with no app connected: only that approval checks the Library, starts them and
closes the review."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from typing import Any

from brindle import bus, doc_templates, redact, visual_offers, visuals
from brindle.db import DB, Agent, Task
from brindle.service.errors import ServiceError

MAX_MARKDOWN = 256 * 1024
MAX_DECISIONS = 12
MAX_WORKERS = 20
MAX_COMMENTS = 50
MAX_LINES = 8          # an option's + or − lines, each
MAX_FILES = 40         # files an option touches, or a worker changes
MAX_PATH = 300
# Comment anchors besides the ## sections (s1, s2, ...): the restated ask,
# the lede, the split, and each decision (decision:<id>), so the person can
# say something on every step of the reader.
ASK = "ask"
LEDE = "lede"
SPLIT = "split"
DECISION = "decision:"
HOLDING = ("open", "redirected", "writing")
# "writing": the empty draft opened when the person asked for a plan, until the supervisor's
# first proposal fills it. "superseded": a draft the person called off before it was written; closed, not approvable.
WRITING = "writing"
SUPERSEDED = "superseded"
# Withdrawn without approval (cancel): closed, not approvable, holds nothing. The tasks it held wait on each
# one's decision (tasks.plan_holds, plan_cancel_holds), not on the plan.
CANCELLED = "cancelled"
# The session ended without approving the plan; its document and shares stay readable.
CLOSED = "closed"
# A row of the build gate.
SIGNED, CHANGES, WAITING, WAIVED = "signed_off", "changes_requested", "waiting", "waived"
_MET = (SIGNED, WAIVED)
_RULE_STATUS = {"met": SIGNED, "waived": WAIVED}   # the Library's policy_reviewers[].status; anything else waits
CONFLICT = "conflict"       # the plan and the Library's copy both changed since they last matched
# The gate's states that keep a build from starting.
GATE_SHUT = (WAITING, CHANGES, CONFLICT)
# Why the Library couldn't confirm a shared plan's review (plan_shares.unconfirmed), in the person's words.
OFFLINE, SIGN_IN, KEY_UNAVAILABLE, LIBRARY_ERROR, OTHER_ACCOUNT, ACCESS_REFUSED = (
    "offline", "sign_in", "key_unavailable", "library_error", "other_account", "access_refused")
UNCONFIRMED = {
    ACCESS_REFUSED: "The Library refused this account access to the shared plan, so its review can't be "
                    "confirmed. Stop sharing, or edit the reviewers.",
    OFFLINE: "brindle can't reach the Library right now, so it can't confirm this plan's review.",
    SIGN_IN: "Your brindle sign-in has expired, so the Library can't confirm this plan's review.",
    KEY_UNAVAILABLE: "Your organization's encryption key isn't available, so the Library can't confirm this "
                     "plan's review. Contact your admin.",
    LIBRARY_ERROR: "The Library ran into a problem and couldn't confirm this plan's review.",
    OTHER_ACCOUNT: "This plan was shared from another brindle account. Sign in to that account to confirm its review.",
}
# The offer to plan a goal first (goal_doc_requests.offer): plan:open:<id>
# until answered, then plan:accepted (waiting for the plan), plan:planned (a
# plan was proposed) or plan:dismissed. Open and accepted hold the session's
# tasks. The retired analysis-doc offer's values (open:<id>, accepted,
# dismissed) still in old rows mean nothing here: they hold nothing, show no
# card and grant nothing, and (being set) keep that goal from a second offer.
OFFER_OPEN = "plan:open:"
OFFER_ACCEPTED = "plan:accepted"
OFFER_PLANNED = "plan:planned"
OFFER_DISMISSED = "plan:dismissed"
OFFER_YES = ("The person wants to plan this goal first (brindle asked because it looks big). Write a "
             "planning doc with propose_plan before you assign more work; the tasks you assigned meanwhile "
             "start once they approve it.")
OFFER_NO = ("The person chose not to plan this goal first. The tasks you assigned while they decided "
            "start now; carry on.")
log = logging.getLogger(__name__)

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


def _queue_message(c, root_id: str, text: str) -> int:
    """Queue the person's verdict for the supervisor in the verdict's own
    transaction ``c`` (as ``DB.enqueue`` would, without a transaction of its own)."""
    return int(c.execute("INSERT INTO inbox (agent_id, sender_id, body, created_at) VALUES (?,NULL,?,?)",
                         (root_id, redact.mask(text), time.time())).lastrowid)


def _strings(raw: Any, name: str, most: int, limit: int) -> list[str]:
    """An optional list of short strings (an option's trade-offs, a worker's files): blanks dropped."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > most:
        raise ServiceError("invalid", f"{name} must be a list of at most {most}", 400)
    return [t for t in (visuals._text(s, name, limit) for s in raw) if t]


def _option_extras(raw: list, options: list[dict]) -> list[dict]:
    """A decision's options with what the reader shows on their cards: the
    trade-offs (``pros`` as + lines, ``cons`` as − lines) and the files the
    option ``touches``. All optional: an option without them is just its label."""
    for o, r in zip(options, raw, strict=True):
        o["pros"] = _strings(r.get("pros"), "an option's pros", MAX_LINES, visuals.MAX_LABEL)
        o["cons"] = _strings(r.get("cons"), "an option's cons", MAX_LINES, visuals.MAX_LABEL)
        o["touches"] = _strings(r.get("touches"), "an option's touches", MAX_FILES, MAX_PATH)
    return options


def _decisions(raw: Any, known: set[str] | None = None) -> list[dict]:
    """``known``: the revision's section ids; a decision's ``section`` anchor
    must name one, else (or with none given) it falls to open-questions."""
    from brindle import plan_docs

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
        anchor = d.get("section")
        if not isinstance(anchor, str) or (known is not None and anchor not in known):
            anchor = plan_docs.OPEN_QUESTIONS
        out.append({"id": did, "title": visuals._text(d.get("title"), "a decision's title", visuals.MAX_QUESTION, True),
                    "section": anchor,
                    "options": _option_extras(d["options"], options), "mine": mine,
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
            raise ServiceError("invalid", "each worker in the split is {id, title, after?, files?, test?}", 400)
        wid = w.get("id")
        if not isinstance(wid, str) or not visuals.OPTION_ID.fullmatch(wid) or wid in seen:
            raise ServiceError("invalid", "each worker in the split needs its own id", 400)
        after = w.get("after") or []
        # Only earlier workers: a split is an order, never a cycle.
        if not isinstance(after, list) or any(a not in seen for a in after):
            raise ServiceError("invalid", f"worker {wid} may only come after workers listed before it", 400)
        seen.add(wid)
        out.append({"id": wid, "title": visuals._text(w.get("title"), "a worker's title", visuals.MAX_LABEL, True),
                    "after": list(after),
                    "files": _strings(w.get("files"), "a worker's files", MAX_FILES, MAX_PATH),
                    "test": visuals._text(w.get("test"), "a worker's test", MAX_PATH)})
    return out


# -- rows --------------------------------------------------------------------------------


def _row(db: DB, plan_id: str) -> dict:
    row = None
    if isinstance(plan_id, str) and visuals.ID.fullmatch(plan_id):
        row = db.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise ServiceError("not_found", "no such plan", 404)
    p = dict(row)
    # Older sessions may have closed before plans were closed with them. Present those
    # documents as closed too, without rewriting their contents or shared copies.
    root = db.get_agent(p["root_id"])
    if p["state"] in HOLDING and (root is None or root.dismissed_at is not None or root.status == "done"):
        p["state"] = CLOSED
    p["decisions"] = json.loads(p["decisions"])
    p["split"] = json.loads(p["split"])
    p["decision"] = json.loads(p["decision"]) if p["decision"] else None
    return p


def get(db: DB, plan_id: str) -> dict:
    """The plan and its visual's row, as ``{**plan, "visual": {...}}``, with
    the current revision's ``lede`` and ``sections`` (brindle.plan_docs)."""
    from brindle import plan_docs

    p = _row(db, plan_id)
    p["visual"] = visuals.get(db, p["id"])
    cur = plan_docs.revision(db, p["id"])
    p["lede"], p["sections"] = cur["lede"], cur["sections"]
    return p


def open_offer_id(offer: str | None) -> str | None:
    """The id of an unanswered offer to plan first, from its stored value; None otherwise."""
    return offer[len(OFFER_OPEN):] if offer and offer.startswith(OFFER_OPEN) else None


def close_with_session(db: DB, root_id: str) -> None:
    """Close unfinished plans without approving work or withdrawing shared copies."""
    from brindle import plan_docs

    with db.tx() as c:
        changed = c.execute("UPDATE plans SET state=?, updated_at=? WHERE root_id=?"
                            " AND state IN ('open','redirected','writing') RETURNING id, rev",
                            (CLOSED, time.time(), root_id)).fetchall()
    for row in changed:
        plan_docs._changed(db, get(db, row["id"]), row["rev"])


def open_plan(db: DB, root_id: str) -> dict | None:
    """Session ``root_id``'s plan the person hasn't approved yet, if any."""
    row = db.conn.execute(f"SELECT id FROM plans WHERE root_id=? AND state IN ({','.join('?' * len(HOLDING))}) "
                          "ORDER BY created_at LIMIT 1", (root_id, *HOLDING)).fetchone()
    plan = _row(db, row["id"]) if row else None
    return plan if plan and plan["state"] in HOLDING else None


def _review_rows(review: dict) -> tuple[dict[str, bool], list[dict]] | None:
    """The Library's reviewers as ``{sub: required}`` and its policy rules, or
    None when it hasn't answered or a row can't be read: an unreadable rule
    must keep the gate shut, never drop out of it. The Library always sends
    the rules, an empty list when there are none: no list is no answer."""
    from brindle.pro import review_policies

    people, rules = review.get("reviewers"), review.get("policy_reviewers")
    if not isinstance(people, list) or not isinstance(rules, list):
        return None
    out: dict[str, bool] = {}
    for r in people:
        if not isinstance(r, dict) or not isinstance(r.get("sub"), str) or not r["sub"]:
            return None
        out[r["sub"]] = out.get(r["sub"], False) or bool(r.get("required"))
    if not all(review_policies.readable(r) for r in rules):
        return None
    if not review_policies.pending_agrees(review.get("pending_policies"), rules):
        return None
    return out, rules


def demands_review(review: dict) -> bool:
    """Whether a review state has required reviewers or org rules, or can't be
    read well enough to say it doesn't."""
    read = _review_rows(review)
    return read is None or any(read[0].values()) or bool(read[1])


def _signed(review: dict) -> dict[str, float]:
    """Who signed the Library's head revision, and when: sign-offs on older ones don't count."""
    rows = review.get("sign_offs")
    return {s["sub"]: s["at"] if isinstance(s.get("at"), (int, float)) else 0
            for s in (rows if isinstance(rows, list) else [])
            if isinstance(s, dict) and isinstance(s.get("sub"), str) and s.get("rev") == review.get("rev")}


def tally(people: dict[str, bool], rules: list[dict], signed: dict[str, float], asked: dict[str, float],
          current: bool, you: str | None) -> tuple[list[dict], list[dict], list[dict], int, list[str], list[str]]:
    """Count a design's reviewers and org rules: ``(rows, policies, required,
    met, unmet, changes)``. ``signed`` and ``asked`` say when each person
    signed the head revision and last left an open comment or suggestion;
    ``current``: whether rule results describe the revision on screen."""
    from brindle.pro import review_policies

    def asked_since(sub: str) -> bool:
        return sub in asked and asked[sub] > signed.get(sub, -1)

    def status(sub: str) -> str:
        if asked_since(sub):
            return CHANGES
        return SIGNED if sub in signed else WAITING

    rows = [{"sub": sub, "name": None, "required": required, "status": status(sub), "met_by": [], "changes_by": [],
             "meets": [], "you": sub == you} for sub, required in people.items()]
    by_sub = {r["sub"]: r for r in rows}
    policies = []
    for rule in rules:
        name = review_policies.name(rule)
        state = _RULE_STATUS.get(rule.get("status"), WAITING) if current and review_policies.backed(rule) else WAITING
        met_by = rule["met_by"] if current else []
        # A signer who has since asked for changes no longer counts towards the
        # rule, even one who isn't on the reviewer list. A waiver stands.
        withdrawn = [s for s in met_by if asked_since(s)] if state == SIGNED else []
        approvals = rule["approvals"]
        if withdrawn:
            met_by = [s for s in met_by if s not in withdrawn]
            if len(met_by) < approvals:
                state = CHANGES
        policies.append(review_policies.row(rule, state, met_by, withdrawn if state == CHANGES else []))
        listed = [by_sub[s] for s in rule["on_list"] if s in by_sub and by_sub[s]["required"]]
        # Someone already on the list adds no second sign-off, unless their
        # signing didn't meet the rule (an author can't sign for a team's).
        if listed and approvals == 1 and (state in _MET or any(r["status"] != SIGNED for r in listed)):
            for r in listed:
                r["meets"].append(name)
        else:
            rows.append({"sub": None, "name": name, "required": True, "status": state, "met_by": met_by,
                         "changes_by": withdrawn if state == CHANGES else [], "meets": [], "you": False})
    required = [r for r in rows if r["required"]]
    met = sum(r["status"] in _MET for r in required)
    unmet = [r["sub"] or r["name"] for r in required if r["status"] not in _MET]
    changes = [who for r in required if r["status"] == CHANGES for who in ([r["sub"]] if r["sub"] else r["changes_by"])]
    return rows, policies, required, met, unmet, changes


def _gate(c, p: dict) -> dict | None:
    """Plan ``p``'s build gate, read on connection ``c``; None if it isn't
    shared, or its Team plan has ended and that still holds for whoever is
    signed in (shared_docs.lapse_stands). Sign-offs and rules count only
    while the Library's head is the revision on screen: until a new revision
    is sent they say nothing about it."""
    from brindle.plan_docs import PERSON, TEAMMATE

    row = c.execute("SELECT * FROM plan_shares WHERE plan_id=?", (p["id"],)).fetchone()
    if row is None:
        return None
    from brindle.pro import review_policies, shared_docs

    if shared_docs.lapse_stands(row, p):
        return None

    review = json.loads(row["review"])
    read = _review_rows(review)
    people, rules = read or ({}, [])
    refusal = review.get(review_policies.REFUSED)
    current = (read is not None and not refusal and row["doc_id"] is not None and row["local_rev"] == p["rev"]
               and review.get("rev") == row["remote_rev"])
    signed = _signed(review) if current else {}
    # Pre-sharing notes stay private. Missing or in-flight links still count:
    # an objection awaiting upload must not leave the build gate open.
    asked: dict[str, float] = {}
    for r in c.execute(
        "SELECT author, MAX(created_at) AS at FROM plan_threads WHERE plan_id=? AND parent_id IS NULL"
        " AND status='open' AND question=0 AND (author=? OR author LIKE ?)"
        " AND id NOT IN (SELECT local_id FROM plan_share_threads WHERE plan_id=? AND remote_id<0 AND status<>?)"
        " GROUP BY author", (p["id"], PERSON, TEAMMATE + "%", p["id"], shared_docs.SENDING)):
        sub = row["owner"] if r["author"] == PERSON else r["author"][len(TEAMMATE):]
        asked[sub] = max(asked.get(sub, r["at"]), r["at"])
    rows, policies, required, met, unmet, changes = tally(people, rules, signed, asked, current, row["owner"])
    if p["state"] == "approved":
        state, reason = "closed", "The review is closed: this plan is building."
    elif p["state"] == CLOSED:
        state, reason = "closed", "Closed: its session ended"
    elif p["state"] == "redirected":
        state, reason = CHANGES, "Changes were requested: the build waits for the supervisor's next revision."
    elif (row["doc_id"] is not None and row["local_rev"] != p["rev"] and isinstance(review.get("rev"), int)
          and review["rev"] != row["remote_rev"]):
        state, reason = CONFLICT, (f"A teammate changed this plan in the Library (r{review['rev']}) and it changed "
                                   f"here too, so r{p['rev']} wasn't sent. Neither copy was overwritten.")
    elif refusal:
        state, reason = WAITING, review_policies.refusal_reason(refusal)
    elif changes:
        state, reason = CHANGES, f"Changes requested by {', '.join(changes)}."
    elif row["doc_id"] is None:
        state, reason = WAITING, "This plan is still being saved to the Library."
    elif read is None and isinstance(review.get("reviewers"), list):
        state, reason = WAITING, "brindle can't read this plan's org review rules, so the build stays closed."
    elif read is None:
        state, reason = WAITING, "brindle hasn't heard back from the Library about this plan's review yet."
    elif not current:
        state, reason = WAITING, "This revision hasn't reached the Library yet, so sign-offs on it can't be counted."
    elif unmet:
        state, reason = WAITING, f"Waiting on {', '.join(unmet)} ({met} of {len(required)} required sign-offs)."
    else:
        state, reason = "ready", None
    is_open = p["state"] == "open"
    unconfirmed = None
    # A refusal of access is a way out too: the owner can stop sharing or edit the reviewers.
    held = row["unconfirmed"] or (ACCESS_REFUSED if row["access_refused"] is not None else None)
    if is_open and held is not None:
        unconfirmed = {"reason": held,
                       "message": UNCONFIRMED.get(held, UNCONFIRMED[LIBRARY_ERROR]),
                       "approval_held": row["approve_wanted"] is not None,
                       "can_stop_sharing": shared_docs.may_stop(row, shared_docs.account()),
                       "can_edit_reviewers": row["doc_id"] is not None}
    return {"state": state, "reason": reason, "required": len(required), "signed": met, "waiting_on": unmet,
            "reviewers": rows, "policies": policies, "unconfirmed": unconfirmed,
            "approval_refused": row["approve_refused"] if is_open else None}


def gate(db: DB, p: dict) -> dict | None:
    """Plan ``p``'s build gate as ``{state, reason, required, signed,
    waiting_on, reviewers}``, or None if it isn't shared. ``state``: waiting,
    changes_requested, conflict (the plan and the Library's copy both changed),
    ready, or closed once it is building."""
    return _gate(db.conn, p)


def _team_ended(db: DB, p: dict) -> bool:
    from brindle.pro import shared_docs

    row = db.conn.execute("SELECT * FROM plan_shares WHERE plan_id=?", (p["id"],)).fetchone()
    return row is not None and shared_docs.lapse_stands(row, p)


def gate_shut(db: DB, root_id: str) -> dict | None:
    """Session ``root_id``'s plan (with its ``gate``) while required reviewers
    keep it from building, whether or not the app is connected."""
    if not visuals.enabled() or not root_id:
        return None
    plan = open_plan(db, root_id)
    found = gate(db, plan) if plan else None
    return {**plan, "gate": found} if found and found["state"] in GATE_SHUT else None


def holding(db: DB, root_id: str) -> dict | None:
    """What session ``root_id``'s queued tasks wait on, if anything: its plan
    the person hasn't approved yet or, before one is proposed, brindle's
    offer to plan the goal first, unanswered or accepted (``{"offer": state}``).
    None while the visuals capability is off or the app is disconnected, so
    nothing waits on a verdict nobody can give. This also releases work if
    the app disconnects after opening an offer or redirecting a plan; the
    saved plan and its decision remain available when it reconnects. A plan
    shared for review is held with or without the app: only the approval
    checks the sign-offs with the Library and closes the review."""
    from brindle.daemon import holds

    if not visuals.enabled() or not root_id:
        return None
    plan = open_plan(db, root_id)
    if plan is not None and gate(db, plan) is not None:
        return plan
    if not holds.app_connected():
        return None
    if plan is not None:
        return plan
    row = db.conn.execute("SELECT offer FROM goal_doc_requests WHERE root_id=? AND (offer LIKE ? OR offer=?)",
                          (root_id, OFFER_OPEN + "%", OFFER_ACCEPTED)).fetchone()
    return {"offer": row["offer"]} if row else None


def _queued(db: DB, root_id: str) -> int:
    from brindle import autopilot, tasks

    return sum(1 for t in db.list_tasks(state="pending")
               if t.caller_id and autopilot.root_of(db, t.caller_id) == root_id and tasks.plan_holds(db, t))


def holding_count(db: DB, root_id: str, plan_id: str) -> int:
    """Queued tasks plan ``plan_id`` holds for session ``root_id``: 0 unless it
    is the plan that :func:`holding` names (the one the tasks wait on)."""
    held = holding(db, root_id)
    return _queued(db, root_id) if held and held.get("id") == plan_id else 0


def to_dto(db: DB, p: dict) -> dict:
    from brindle import plan_docs
    from brindle.service.dto import PlanDTO

    return PlanDTO(id=p["id"], session=p["root_id"], title=p["visual"]["title"], rev=p["rev"],
                   state=p["state"], ask=p.get("ask"), markdown=p["markdown"], split=[],
                   verdict=p["decision"], queued=_queued(db, p["root_id"]) if p["state"] in HOLDING else 0,
                   created_at=p["created_at"], gate=gate(db, p), team_ended=_team_ended(db, p),
                   reference=doc_templates.reference_of(db, p["id"]), alongside=alongside(db, p["id"]),
                   **plan_docs.fields(db, p)).to_json()


def assignment(db: DB, caller: Agent, ref: str) -> tuple[str, str]:
    """An explicit plan-id:split-id reference, scoped to the caller's session."""
    from brindle import autopilot

    pid, _, wid = ref.partition(":")
    p = _row(db, pid)
    if p["root_id"] != autopilot.root_of(db, caller.id) or not any(w["id"] == wid for w in p["split"]):
        raise ServiceError("invalid", "plan_worker must name a split in this session (plan-id:split-id)", 400)
    return pid, wid


def _task_state(db: DB, task: Task) -> str:
    """Where ``task`` is, as the plan's reader shows it: queued, working, in review, merged or cancelled."""
    if task.state in ("merged", "cancelled"):
        return task.state
    agent = db.get_agent(task.agent_id) if task.agent_id else None
    if agent and (agent.pipeline in ("reviewing", "ready") or (not agent.pipeline and agent.result)):
        return "in review"
    if agent is None and task.state == "started" and task.agent_id:
        return "cancelled"      # its workspace was removed unmerged (a Stop, remove_workspace)
    return "working" if agent else "queued"


def record_alongside(db: DB, plan_id: str, task_id: str, reason: str) -> None:
    """Task ``task_id`` was assigned outside plan ``plan_id`` for ``reason``: it runs at once and the
    plan's reader lists it (``alongside``)."""
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO plan_alongside (plan_id, task_id, reason, created_at) VALUES (?,?,?,?)",
                  (plan_id, task_id, reason, time.time()))


def alongside(db: DB, plan_id: str) -> list[dict]:
    """The tasks the supervisor assigned outside plan ``plan_id`` while it was open, oldest first:
    each one's title (its brief's first line), state, the reason given and whether Stop can act on it."""
    out = []
    for row in db.conn.execute("SELECT task_id, reason FROM plan_alongside WHERE plan_id=? ORDER BY created_at, rowid",
                               (plan_id,)):
        task = db.get_task(row["task_id"])
        if task is None:
            continue
        state = _task_state(db, task)
        out.append({"task_id": task.id, "title": _brief(task), "state": state, "reason": row["reason"],
                     "can_stop": state not in ("merged", "cancelled")})
    return out


def _brief(task: Task) -> str:
    return next((ln.strip() for ln in task.task_text.splitlines() if ln.strip()), task.id)[:120]


def stop_alongside(db: DB, plan_id: str, task_id: str) -> dict:
    """The person's Stop on a task running alongside plan ``plan_id``: a queued task is cancelled
    (tasks._cancel, which tells its caller), a started one has its workspace removed
    (workspaces.remove: its branch is kept unless merged). Never a kill. Returns the plan."""
    from brindle import plan_docs, tasks

    visuals.require_enabled()
    p = get(db, plan_id)
    visuals._unlocked(p["root_id"])
    if db.conn.execute("SELECT 1 FROM plan_alongside WHERE plan_id=? AND task_id=?", (p["id"], task_id)).fetchone() is None:
        raise ServiceError("not_found", "no such task runs alongside this plan", 404)
    reason = f"stopped by the person from plan {p['id']}'s list of work running alongside it"
    # _cancel reads the task again and cancels only a queued one: a task that started
    # meanwhile falls through and is stopped as a started one.
    if not tasks._cancel(db, task_id, reason):
        _remove_started(db, db.get_task(task_id))
    plan_docs._changed(db, p, p["rev"])
    return get(db, p["id"])


def _remove_started(db: DB, task: Task | None) -> None:
    from brindle import workspaces

    if task is None or task.state in ("merged", "cancelled"):
        raise ServiceError("stopped", "this task has already ended", 409)
    agent = db.get_agent(task.agent_id) if task.state == "started" and task.agent_id else None
    ws = db.get_workspace(agent.workspace_id) if agent else None
    if ws is None:
        raise ServiceError("busy", "this task is starting or its workspace is gone; try again in a moment", 409)
    if ws.id == task.caller_ws_id:
        # isolate=false: the worker works in its supervisor's checkout, which Stop never removes.
        raise ServiceError("shared", "this task works in its supervisor's own checkout, so Stop can't remove it", 409)
    try:
        workspaces.remove(db, ws)
    except workspaces.WorkspaceError as e:
        raise ServiceError("kept", str(e), 409) from e


def _board(db: DB, p: dict) -> list[dict]:
    out = []
    milestones = db.milestones(p["root_id"])
    for card in p["split"]:
        w = dict(card, state="queued", test_state="not run")
        link = db.conn.execute("SELECT task_id FROM plan_tasks WHERE plan_id=? AND worker_id=?",
                               (p["id"], w["id"])).fetchone()
        task = db.get_task(link["task_id"]) if link else None
        if task:
            w.update(task_id=task.id, agent_id=task.agent_id, state=_task_state(db, task))
            # Only results for the named proving command count; an unrelated passing
            # check or milestone must never turn this card green.
            results = [(r["checked_at"], bool(r["ok"])) for r in db.conn.execute(
                "SELECT checked_at, ok FROM task_check_outcomes WHERE task_id=? AND command=?",
                (task.id, w.get("test")))]
            results.extend((m.checked_at, m.status == "passed") for m in milestones
                           if m.checked_at is not None and m.checked_at >= task.created_at
                           and m.title == db.task_milestone(task.id) and m.check_cmd == w.get("test"))
            if results:
                w["test_state"] = "passing" if max(results, key=lambda r: r[0])[1] else "failing"
        out.append(w)
    return out


def doc_tasks(db: DB, doc_id: str) -> list[dict] | None:
    """The split of the plan shared as Library doc ``doc_id``, each task with
    its state as the plan's board has it (queued, working, in review, merged
    or cancelled): what the Library reads Building and Shipped from. None when
    no plan on this machine is that doc."""
    from brindle.service.dto import DesignTaskDTO

    row = db.conn.execute("SELECT plan_id FROM plan_shares WHERE doc_id=?", (doc_id,)).fetchone()
    if row is None:
        return None
    try:
        p = _row(db, row["plan_id"])
    except ServiceError:
        return None
    return [DesignTaskDTO(id=w["id"], title=w.get("title") or w["id"], state=w["state"]).to_json() for w in _board(db, p)]


def _page(title: str, markdown: str) -> str:
    """The plan's page in the visuals store, what Keep copies: brindle's own
    HTML, the markdown escaped as text (it is never parsed as HTML)."""
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{html_lib.escape(title)}</title>"
            "<style>body{font:14px/1.6 system-ui,sans-serif;max-width:720px;margin:24px auto;padding:0 16px}"
            "pre{white-space:pre-wrap;font:inherit}</style></head>"
            f"<body><h1>{html_lib.escape(title)}</h1><pre>{html_lib.escape(markdown)}</pre></body></html>")


# -- the supervisor proposes ------------------------------------------------------------


def propose(db: DB, caller: Agent | None, markdown: str, decisions: Any = None, split: Any = None,
            title: str | None = None, plan_id: str | None = None, ask: Any = None,
            sections: Any = None, base: Any = None) -> dict:
    """The session's supervisor proposes a plan, or (``plan_id``) a new
    revision of its open or redirected one. A new plan spends the person's
    request (their ask for a planning doc); a revision of the open one needs
    none, and spends a Redirect's if there is one.
    ``ask``: the person's request restated in the supervisor's words, shown
    on every step of the reader for them to correct. ``sections``: the doc
    as sections, markdown or diagrams (brindle.plan_docs.parse_sections);
    then ``markdown`` is only the text before them (the title and lede).
    Without it, the markdown's ``##`` headings make the sections.

    A shared plan with a live draft takes no revision: the supervisor writes
    into the draft, against ``base``, the text :func:`read_draft` handed it
    (plan_docs.supervise), and the plan is open for the person again. The
    result's ``draft_write`` says what went in and what became suggestions."""
    from brindle import plan_docs

    visuals.require_enabled()
    root, _ws = visuals._session(db, caller)
    if caller.id != root.id:
        raise ServiceError("forbidden", "only the session's supervisor proposes a plan", 403)
    if sections is not None:
        parts = plan_docs.parse_sections(sections)
        lede = plan_docs._body_text(markdown, "markdown", MAX_MARKDOWN).strip()
        markdown = plan_docs.render(lede, parts)
    elif not isinstance(markdown, str) or not markdown.strip():
        raise ServiceError("invalid", "markdown is required", 400)
    else:
        markdown = markdown.strip()
        lede, parts = plan_docs.from_markdown(markdown)
    # The empty draft opened when the person asked: the first proposal (with or without its id)
    # fills that plan instead of creating another.
    draft_row = db.conn.execute("SELECT id FROM plans WHERE root_id=? AND state=?", (root.id, WRITING)).fetchone()
    fill = draft_row is not None and plan_id in (None, draft_row["id"])
    if fill:
        plan_id = None
    # The doc format: a new plan starts from the default template, a revision keeps the one
    # its plan was written with; either way a required heading left out comes back, empty.
    where = doc_templates.store()
    fmt = None if plan_id else doc_templates.resolve(where=where)
    template = doc_templates.default_template(fmt) if fmt else (doc_templates.of(db, plan_id) or {}).get("template")
    # With no custom template, a new plan or a revision of one already on the spine follows the
    # design-doc spine (an older free-form plan keeps its sections as they are).
    on_spine = plan_id is not None and any(
        s["id"] in plan_docs.SPINE for s in plan_docs.revision(db, plan_id)["sections"])
    if plan_id is None or on_spine:
        parts = plan_docs.design_doc(parts, force=not template, map_only=bool(template))
        markdown = plan_docs.render(lede, parts)
    own = {s["id"] for s in parts}
    parts, added = doc_templates.apply(template, parts)
    if added:
        # A heading the template brought in empty takes its spine id too (Open questions stays one section).
        parts = plan_docs.canonical_ids(parts, {s["id"] for s in parts} - own)
    if template:
        # An optional extra (audience, architecture, tech stack) with nothing in it is never stored,
        # whether the supervisor left it empty or the template brought it in; a required one stays,
        # empty, for sign-off to ask for.
        required = {doc_templates._key(t["title"]) for t in template["sections"] if t.get("required")}
        kept = [s for s in parts if not plan_docs.empty_extra(s) or doc_templates._key(s["title"]) in required]
        added = added or len(kept) != len(parts)
        parts = kept
    if added:
        markdown = plan_docs.render(lede, parts)
    if len(markdown.encode("utf-8")) > MAX_MARKDOWN:
        raise ServiceError("too_big", f"a plan may be at most {MAX_MARKDOWN // 1024} KB of markdown", 400)
    given = [name for name, raw in (("decisions", decisions), ("split", split), ("ask", ask)) if raw]
    decisions, split = _decisions(decisions, {s["id"] for s in parts}), _split(split)
    if any(d["section"] == plan_docs.OPEN_QUESTIONS for d in decisions) and not any(
            s["id"] == plan_docs.OPEN_QUESTIONS for s in parts):
        # A custom template or an older free-form plan may have no Open questions: unanchored
        # decisions still need a target.
        parts = [*parts, {"id": plan_docs.OPEN_QUESTIONS, "title": plan_docs.TITLES[plan_docs.OPEN_QUESTIONS],
                          "kind": "markdown", "markdown": "None.", "caption": None}]
        markdown = plan_docs.render(lede, parts)
    ask = visuals._text(ask, "ask", visuals.MAX_NOTE)
    title = visuals._text(title, "title", visuals.MAX_TITLE) or _title(markdown) or "Plan"
    visuals._refuse_secrets([
        ("the plan", markdown), ("the title", title), ("the ask", ask),
        *((f"decision {d['id']}", "\n".join([d["title"], d["challenge"] or "",
                                              *(f"{o['label']}\n{o['detail'] or ''}\n" + "\n".join(
                                                  o["pros"] + o["cons"] + o["touches"]) for o in d["options"])]))
          for d in decisions),
        *((f"worker {w['id']}", "\n".join([w["title"], w["test"] or "", *w["files"]])) for w in split)])
    current_plan = open_plan(db, root.id)
    if plan_id is None and current_plan is not None and not fill:
        raise ServiceError("open_plan", f"plan {current_plan['id']} is still open: revise it with plan_id, "
                           "or wait for the person's decision", 409)
    if plan_id is not None:
        current = _row(db, plan_id)
        if current["root_id"] != root.id:
            raise ServiceError("not_found", "no such plan in this session", 404)
        if current["state"] == SUPERSEDED:
            raise ServiceError("superseded", "the person asked for a newer plan; that one is closed", 409)
        if current["state"] == CANCELLED:
            raise ServiceError("cancelled", "that plan was cancelled; propose a new plan instead", 409)
        if current["state"] == CLOSED:
            raise ServiceError("closed", "that plan closed with its session; propose a new plan instead", 409)
        if current["state"] not in HOLDING:
            raise ServiceError("approved", "that plan is approved; propose a new plan instead", 409)
    replaced = plan_docs.revision(db, plan_id) if plan_id else None
    previous = {s["id"]: s for s in replaced["sections"]} if replaced else {}
    replica = plan_docs.draft(db, current) if plan_id else None
    if replica is not None and not base:
        raise ServiceError("base_required", "this plan is co-edited in a live draft: read it with "
                           "read_plan and pass the base it gives, so what others wrote since stays", 409)
    drafted: dict = {}
    # A draft holds only text: what else the supervisor changed is named, not applied.
    off_draft = [name for name, new in (("decisions", decisions), ("split", split), ("ask", ask))
                 if name in given and replaced and new != replaced[name]]

    def write_draft(c, vid: str, visual_rev: int) -> None:
        # The draft continues this revision: a freeze since, or an approval, refuses the write.
        if not c.execute("UPDATE plans SET state='open', decision=NULL, updated_at=? WHERE id=? AND rev=?"
                         " AND state IN ('open','redirected')", (time.time(), vid, current["rev"])).rowcount:
            raise ServiceError("stale", "the plan changed meanwhile; read it again with read_plan", 409)
        drafted.update(plan_docs.supervise(c, current, replica, base, replaced, lede, parts, off_draft))

    settled: list[int] = []
    numbered: list[bool] = []

    def write(c, vid: str, visual_rev: int) -> None:
        now = time.time()
        # The plan's revisions count the person's own edits too, so they run
        # ahead of its visual's (the supervisor's publications).
        rev = 1 if visual_rev == 1 or fill else c.execute("SELECT rev FROM plans WHERE id=?", (vid,)).fetchone()[0] + 1
        args = (rev, markdown, json.dumps(decisions), json.dumps(split), ask, now)
        if fill:
            if not c.execute("UPDATE plans SET rev=?, markdown=?, decisions=?, split=?, ask=?, updated_at=?,"
                             " state='open' WHERE id=? AND state=?", (*args, vid, WRITING)).rowcount:
                raise ServiceError("stale", "the draft changed meanwhile; propose again", 409)
        if visual_rev == 1 or fill:
            if not fill:
                c.execute("INSERT INTO plans (id, root_id, rev, markdown, decisions, split, ask, state, created_at,"
                          " updated_at) VALUES (?,?,?,?,?,?,?,'open',?,?)", (vid, root.id, *args[:5], now, now))
            numbered.append(doc_templates.start(c, vid, template, fmt and fmt["naming"]["reference"], now))
            # The plan now holds the session's tasks: the offer to plan first is settled
            # (an open one too: the person asked for a plan in their own words).
            # A plan asked for directly, with no offer before it, settles the goal too, so growing
            # the goal later never offers to plan what already has a plan.
            c.execute("INSERT OR IGNORE INTO goal_doc_requests (root_id, requested) "
                      "SELECT root_id, 0 FROM autopilot WHERE root_id=?", (root.id,))
            settled.append(c.execute(
                "UPDATE goal_doc_requests SET offer=? WHERE root_id=? AND (offer IS NULL OR offer LIKE ? OR offer=?)",
                (OFFER_PLANNED, root.id, OFFER_OPEN + "%", OFFER_ACCEPTED)).rowcount)
        else:
            # Only a plan still waiting on the person takes a revision: one
            # approved since the check above stays approved.
            changed = c.execute("UPDATE plans SET rev=?, markdown=?, decisions=?, split=?, ask=?, updated_at=?,"
                                " state='open',"
                                " decision=NULL WHERE id=? AND state IN ('open','redirected')", (*args, vid)).rowcount
            if not changed:
                raise ServiceError("approved", "that plan is approved; propose a new plan instead", 409)
            plan_docs.keep(c, replaced)
        plan_docs.insert_revision(c, vid, rev, plan_docs.SUPERVISOR, "proposal", lede, parts, markdown, decisions,
                                  split, ask, [s["id"] for s in parts if previous.get(s["id"]) != s], now)

    try:
        v = visuals.publish(db, caller, title, _page(title, markdown),
                            visual_id=draft_row["id"] if fill else plan_id, kind="plan",
                            extra=write if replica is None else write_draft)
    except ServiceError as e:
        if e.code == "visual_request_required":
            raise ServiceError("plan_request_required", "a plan needs the person to ask for one: propose_plan "
                               "only after they asked for a plan, said yes to planning the goal "
                               "first, or redirected this one", 403) from e
        raise
    if any(numbered):
        # Taken once the plan exists, so a refused proposal never spends a number.
        doc_templates.number(db, v["id"], where)
    if any(settled):
        bus.publish_agent(db, "autopilot.goal", root.id)   # the offer's card goes
    if replica is not None:
        p = get(db, v["id"])
        plan_docs._changed(db, p, p["rev"])
        return {**p, "draft_write": drafted}
    p = get(db, v["id"])
    plan_docs._changed(db, p, p["rev"])
    return p


def begin(db: DB, root_id: str, asked_at: float | None = None) -> str | None:
    """The person asked for a plan (a message, or Yes to the offer to plan the goal): open the
    plan now, as an empty draft the supervisor is writing, and return its id. The supervisor's
    first propose_plan fills it. A session has one plan: if one is
    open already, asking again just shows it (the supervisor revises it with plan_id). No new
    draft is made while one is open, or when ``asked_at`` (when the person sent the message) is no later than the session's newest
    plan: that is a repeated report of a message already answered."""
    if not visuals.enabled():
        return None
    root = db.get_agent(root_id)
    if root is None:
        return None

    current = open_plan(db, root_id)
    if current is not None:
        # One plan per session: asking again shows the one that is open, and the supervisor
        # revises it with plan_id (the ask's grant stays for that revision).
        bus.publish_agent(db, "plan.requested", root_id, visual=current["id"])
        return current["id"]

    def write(c, vid: str, visual_rev: int) -> None:
        if c.execute("SELECT 1 FROM plans WHERE root_id=? AND state=?", (root_id, WRITING)).fetchone():
            raise ServiceError("exists", "a plan is already being written", 409)
        newest = c.execute("SELECT MAX(created_at) FROM plans WHERE root_id=?", (root_id,)).fetchone()[0]
        if asked_at is not None and newest is not None and asked_at <= newest:
            raise ServiceError("exists", "that ask already has its plan", 409)
        now = time.time()
        c.execute("INSERT INTO plans (id, root_id, rev, markdown, decisions, split, ask, state, created_at,"
                  " updated_at) VALUES (?,?,0,'','[]','[]',NULL,?,?,?)", (vid, root_id, WRITING, now, now))

    try:
        v = visuals.publish(db, root, "Plan", _page("Plan", "The supervisor is writing this plan."),
                            kind="plan", extra=write)
    except ServiceError:
        return None
    with db.tx() as c:
        visual_offers._grant(c, root_id, "plan")    # the one revision that fills it
    bus.publish_agent(db, "autopilot.goal", root_id)
    bus.publish_agent(db, "plan.requested", root_id, visual=v["id"])   # listed already (visual.published); the person opens it
    return v["id"]


def cancel_draft(db: DB, root_id: str) -> None:
    """The person called the plan off before the supervisor wrote it: close the empty draft,
    so it holds no work."""
    with db.tx() as c:
        c.execute("UPDATE plans SET state=?, updated_at=? WHERE root_id=? AND state=?",
                  (SUPERSEDED, time.time(), root_id, WRITING))
    bus.publish_agent(db, "autopilot.goal", root_id)


def read_draft(db: DB, caller: Agent | None, plan_id: str) -> dict:
    """The session's supervisor reads its shared plan's live draft, once the
    person has sent it back (Request changes): the text others write there
    reaches it only then. Returns the doc as propose takes it (lede, sections
    with their ids, decisions, split, ask) and ``base``, the id its revision
    names (plan_docs.hand_over)."""
    from brindle import plan_docs

    visuals.require_enabled()
    root, _ws = visuals._session(db, caller)
    p = _row(db, plan_id)
    if caller.id != root.id or p["root_id"] != root.id:
        raise ServiceError("not_found", "no such plan in this session", 404)
    replica = plan_docs.draft(db, p)
    if replica is None:
        raise ServiceError("no_draft", "this plan has no live draft: revise it from your last revision", 409)
    if p["state"] != "redirected":
        raise ServiceError("not_sent", "the person hasn't sent this plan back to you; wait for Request changes", 409)
    base, cur = plan_docs.hand_over(db, p, replica)
    return {"plan_id": p["id"], "rev": p["rev"], "base": base, "lede": cur["lede"], "ask": cur["ask"],
            "sections": [{k: s[k] for k in ("id", "title", "kind", "markdown", "caption")} for s in cur["sections"]],
            "decisions": cur["decisions"], "split": cur["split"]}


def reply(db: DB, caller: Agent | None, plan_id: str, thread_id: Any, text: Any, resolve: bool = False) -> int:
    """The session's supervisor answers a thread the person opened on its
    plan's doc (a comment or a suggestion), optionally resolving a comment
    thread. The person sees the reply on the doc; it never reaches the
    supervisor's own inbox. Returns the reply's id."""
    from brindle import plan_docs

    visuals.require_enabled()
    root, _ws = visuals._session(db, caller)
    p = _row(db, plan_id)
    if caller.id != root.id or p["root_id"] != root.id:
        raise ServiceError("forbidden", "only the session's supervisor answers on its plan", 403)
    thread = plan_docs._thread(db, p, thread_id)
    from brindle.pro import doc_questions

    # A question asked here is answered here, shared or not; on a shared plan
    # the answer goes up as the asker's supervisor's with the next sync. One
    # asked in the Library is answered there, by the supervisor it was given to.
    if thread["question"] and thread["author"] != plan_docs.PERSON:
        from brindle.pro import shared_docs

        shared = shared_docs.link(db, plan_id)
        remote = db.conn.execute("SELECT remote_id FROM plan_share_threads WHERE plan_id=? AND local_id=?"
                                 " AND remote_id>0", (plan_id, thread_id)).fetchone()
        if shared and remote:
            doc_questions.reply(db, caller, shared["doc_id"], remote["remote_id"], text)
            return thread_id
        raise ServiceError("forbidden", "only the asker's supervisor answers a question", 403)
    if resolve and plan_docs._thread(db, p, thread_id)["kind"] != "comment":
        # refuse before replying: a failed call must leave nothing behind
        raise ServiceError("invalid", "only a comment thread is resolved; a suggestion is accepted or rejected", 400)
    rid = plan_docs.comment(db, plan_id, text=text, parent=thread_id, author=plan_docs.SUPERVISOR)
    if thread["question"]:
        db.conn.execute("UPDATE plan_threads SET from_supervisor=1 WHERE id=?", (rid,))
    if resolve:
        plan_docs.resolve(db, plan_id, thread_id)
    if thread["question"]:
        doc_questions.sync_soon(db, plan_id)
    return rid


# -- the person decides -----------------------------------------------------------------


def _picks(raw: Any, decisions: list[dict], allowed: dict[str, set[str]] | None = None) -> dict[str, str]:
    """``allowed``: what each decision's pick may name, own answers included (plan_answers.choices)."""
    if raw is None:
        return {}
    by_id = allowed if allowed is not None else {d["id"]: {o["id"] for o in d["options"]} for d in decisions}
    if not isinstance(raw, dict) or any(k not in by_id or v not in by_id[k] for k, v in raw.items()):
        raise ServiceError("invalid", "a pick names one of this revision's decisions and one of its options", 400)
    return dict(raw)


def anchors(p: dict) -> dict[str, str]:
    """What a comment on revision ``p`` may name, with the title the
    supervisor reads it under: each ``##`` section and, when the plan has
    them, the restated ask, the lede, the split and each decision."""
    lede, parts = (p["lede"], p["sections"]) if "sections" in p else sections(p["markdown"])
    out = {s["id"]: s["title"] for s in parts}
    if p.get("ask"):
        out[ASK] = "What I understood (your restated ask)"
    if lede:
        out[LEDE] = "The overview"
    out.update({DECISION + d["id"]: f"Decision {d['id']} ({d['title']})" for d in p["decisions"]})
    return out


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


def _own_answer(a: dict, teammates: dict[str, str]) -> str:
    """How the supervisor reads a picked own answer: whose, and its words."""
    from brindle.plan_docs import PERSON

    author = a["author"]
    whose = "their own answer" if author == PERSON else \
        f"{teammates.get(author.removeprefix('teammate:'), 'a teammate')}'s own answer"
    return f"{whose}, \"{a['text']}\""


def _message(p: dict, approved: bool, picks: dict, comments: list[dict], note: str | None, titles: dict,
             doc: list[str] = (), files: list[str] = (), drafting: bool = False, answers: dict | None = None,
             teammates: dict[str, str] | None = None) -> str:
    verb = "approved" if approved else "redirected"
    lines = [f"The person {verb} plan {p['id']} (r{p['rev']})."]
    for d in p["decisions"]:
        pick = picks.get(d["id"])
        if pick and (answers or {}).get(d["id"]):
            lines.append(f"Decision {d['id']} ({d['title']}): they chose {_own_answer(answers[d['id']], teammates or {})}"
                         " over your options.")
        elif pick and pick != d["mine"]:
            label = next(o["label"] for o in d["options"] if o["id"] == pick)
            lines.append(f"Decision {d['id']} ({d['title']}): they chose {pick} ({label}) over yours.")
    for c in comments:
        lines.append(f"On \"{titles[c['section']]}\": {c['text']}")
    lines.extend(doc)
    if note:
        lines.append(f"Their note: {note}")
    if files:
        lines.append(" ".join(["Attached to their note:", *files]))
    if approved:
        lines.append("The tasks you assigned behind the plan start now; assign the rest as planned.")
    elif drafting:
        lines.append(f"It is co-edited in a live draft: read it with read_plan(plan_id=\"{p['id']}\"), then revise "
                     f"that text with propose_plan(plan_id=\"{p['id']}\", base=<the base it gives>); what others "
                     "write meanwhile stays. Nothing starts until they approve.")
    else:
        lines.append(f"Revise it with propose_plan(plan_id=\"{p['id']}\"); nothing starts until they approve.")
    return "\n".join(lines)


def _check_with_library(db: DB, p: dict) -> str | None:
    """Before an approval of a shared plan: bring the Library's review state
    up to date. Returns why it couldn't be asked (a key of UNCONFIRMED), and
    the approval is then refused even with no required reviewers on record:
    the last answer can't say nobody was added since. A share whose Team plan
    has ended comes here too: only a lapse the server confirms now lets it
    through, not the one on record."""
    if db.conn.execute("SELECT 1 FROM plan_shares WHERE plan_id=?", (p["id"],)).fetchone() is None:
        return None
    from brindle.pro import shared_docs

    try:
        shared_docs.sync(db, p["id"])
    except Exception as e:  # noqa: BLE001 - whatever stopped it, the review state is unconfirmed
        log.info("brindle: couldn't sync plan %s before its approval", p["id"], exc_info=True)
        return shared_docs.why_unconfirmed(db, p["id"], e)
    return None


def _hold_approval(db: DB, p: dict, why: str, was_ready: bool, wanted: dict) -> bool:
    """Record why an approval couldn't be confirmed and, when the confirmation
    is all that failed, the approval itself: the gate read ready when the
    person pressed, nothing but the outage stands in the way, and it is the
    account that shared the plan. Returns whether the approval is held."""
    from brindle import plan_docs
    from brindle.pro import review_policies

    with db.tx() as c:
        row = c.execute("SELECT review, access_refused FROM plan_shares WHERE plan_id=?", (p["id"],)).fetchone()
        if row is None:
            return False
        refusal = json.loads(row["review"]).get(review_policies.REFUSED)
        held = (was_ready and why != OTHER_ACCOUNT and row["access_refused"] is None
                and (not refusal or refusal.get("code") == "key_unavailable"))
        c.execute("UPDATE plan_shares SET unconfirmed=?, approve_refused=NULL,"
                  " approve_wanted=COALESCE(?, approve_wanted) WHERE plan_id=?",
                  (why, json.dumps(wanted) if held else None, p["id"]))
    plan_docs._changed(db, p, p["rev"])
    return held


def _drop_held_approval(db: DB, p: dict, why: str) -> None:
    from brindle import plan_docs

    with db.tx() as c:
        c.execute("UPDATE plan_shares SET approve_wanted=NULL, approve_refused=? WHERE plan_id=?", (why, p["id"]))
    plan_docs._changed(db, p, p["rev"])


def settle_held_approval(db: DB, plan_id: str) -> None:
    """After a sync the Library answered: apply the Approve it couldn't confirm
    earlier if the gate reads ready for that same revision. If it doesn't, the
    person is told and nothing is approved. While the Team plan's end stands
    there is no gate to read, and the approval keeps waiting."""
    row = db.conn.execute("SELECT approve_wanted FROM plan_shares WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None or row["approve_wanted"] is None:
        return
    wanted = json.loads(row["approve_wanted"])
    p = get(db, plan_id)
    try:
        visuals.require_enabled()
        visuals._unlocked(p["root_id"])
    except ServiceError:        # nothing is decided here for now: a later pass looks again
        return
    found = gate(db, p)
    if found is None:
        return
    version = wanted.get("version")
    if version is not None:
        # A frozen version approved: only that version, as it came in here (a new revision or not), is applied.
        wanted["rev"] = p["rev"] if came_in_as(db, p, version) else None
    if wanted["rev"] != p["rev"]:
        what = f"version {version}" if version is not None else f"r{wanted['rev']}"
        _drop_held_approval(db, p, f"Your approval of {what} wasn't applied: the plan has changed since "
                                   "you approved it. Read the new revision and approve again.")
    elif found["state"] != "ready":
        _drop_held_approval(db, p, f"Your approval wasn't applied: {found['reason']}")
    else:
        try:
            decide(db, plan_id, wanted["rev"], True, wanted["picks"], wanted["comments"], wanted["note"],
                   person=True, attachments=wanted["attachments"], confirmed=True, version=version)
        except ServiceError as e:
            _drop_held_approval(db, p, f"Your approval wasn't applied: {e.message}")


def _version(version: Any) -> int | None:
    """The live version on screen (``LiveDocState.version.id``), when the app names one."""
    if version is None:
        return None
    if not isinstance(version, int) or isinstance(version, bool):
        raise ServiceError("invalid", "version (the frozen version on screen) is a number", 400)
    return version


def caught_up(db: DB, p: dict, rev: int, version: int | None) -> dict:
    """Plan ``p`` again after a sync with the Library, judged on what the
    person had on screen: revision ``rev`` or, on a live doc, the frozen
    ``version``. A version frozen from the live draft can come in as a new
    revision here only when that sync brings it, after the person read and
    signed it: the head that version came in as is what they read. Anything
    else that moved is a newer revision for them to read (``stale``)."""
    now = get(db, p["id"])
    share = db.conn.execute("SELECT remote_rev FROM plan_shares WHERE plan_id=?", (p["id"],)).fetchone()
    if version is not None and share is not None and share["remote_rev"] != version:
        raise ServiceError("stale", f"version {version} isn't the one out for sign-off now; read the plan again",
                           409, rev=now["rev"])
    if now["rev"] == rev or came_in_as(db, now, version):
        return now
    raise ServiceError("stale", f"the plan has a newer revision (r{now['rev']}); read that one", 409, rev=now["rev"])


def came_in_as(db: DB, p: dict, version: int | None) -> bool:
    """Whether plan ``p``'s head is the Library's frozen ``version`` as it came in here."""
    return version is not None and _is_version(db.conn, p["id"], p["rev"], version) is True


def _is_version(c, plan_id: str, rev: int, version: int) -> bool | None:
    """Whether revision ``rev`` is the Library's ``version`` here, read on
    connection ``c``; None for a plan that isn't shared (it has no versions)."""
    share = c.execute("SELECT remote_rev, local_rev FROM plan_shares WHERE plan_id=?", (plan_id,)).fetchone()
    return None if share is None else share["remote_rev"] == version and share["local_rev"] == rev


def decide(db: DB, plan_id: str, rev: Any, approved: bool, picks: Any = None, comments: Any = None,
           note: Any = None, *, person: bool, attachments: Any = None, confirmed: bool = False,
           version: Any = None) -> dict:
    """The person's verdict on the revision on their screen (``rev``):
    approve, or redirect with their picks and per-section comments. Only the
    person (the app, through the daemon's guarded route) decides: anyone
    else is refused.

    One transaction makes the verdict: the plan's state (claimed only from
    'open', so a second click is refused instead of delivered twice), the
    visual's answer, a redirect's grant of the one revision it asks for, and
    the message to the supervisor, queued in its inbox. It all lands or none
    of it does: there is no half-decided state to recover from, and nothing
    starts unless the supervisor's message is queued with it. Pushing the
    message to the supervisor's terminal comes after, and failing that loses
    nothing: it stays queued for its next turn. Then an approval releases the
    session's queued tasks.

    The verdict starts from the decisions the person confirmed on the doc
    (``picks`` given here win), and carries what they wrote on it that the
    supervisor hasn't had (brindle.plan_docs.pending) and the files
    ``attachments`` put on their note.

    If the Library can't confirm the review the approval is refused
    (``gate_unverified``, with a typed ``reason``), and held for
    :func:`settle_held_approval`, whose call passes ``confirmed``: it has just
    heard from the Library, and the gate is still read in the verdict's own
    transaction. Once the Library has answered, the verdict is on the plan as
    that answer left it (:func:`caught_up`): on a live doc, ``version`` (the
    frozen version on screen) approves the revision it came in as."""
    from brindle import agents, plan_docs, tasks

    visuals.require_enabled()
    if not person:
        raise ServiceError("forbidden", "only the person approves or redirects a plan, from the app", 403)
    p = get(db, plan_id)
    visuals._unlocked(p["root_id"])
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise ServiceError("invalid", "rev (the revision on screen) is required", 400)
    version = _version(version)
    if came_in_as(db, p, version):
        rev = p["rev"]          # an earlier sync brought the version on screen in: it is this revision
    if rev != p["rev"]:
        raise ServiceError("stale", f"the plan has a newer revision (r{p['rev']}); read that one", 409, rev=p["rev"])
    if p["state"] == WRITING:
        raise ServiceError("writing", "the supervisor is still writing this plan", 409)
    if p["state"] != "open":
        raise ServiceError("decided", "this revision is already decided", 409)
    if approved and version is None:
        doc_templates.require(db, p["id"], doc_templates.SIGN_OFF, plan_docs.current(db, p)["sections"])
    unconfirmed, was_ready = None, False
    if approved and not confirmed:
        before = gate(db, p)            # what the person pressed Approve on, before asking can change it
        was_ready = before is not None and before["state"] == "ready"
        unconfirmed = _check_with_library(db, p)
        if unconfirmed is None:
            # The sync may have brought in the version on screen as a new revision: judge that one.
            p = caught_up(db, p, rev, version)
            rev = p["rev"]
    if approved and version is not None and unconfirmed is None:
        # A frozen version is approved as its own text, as it came in here, never the draft's cached one.
        doc_templates.require(db, p["id"], doc_templates.SIGN_OFF, plan_docs.revision(db, p["id"])["sections"])
    titles = anchors(p)
    from brindle import plan_answers

    picks = {**plan_docs.confirmed(db, p), **_picks(picks, p["decisions"], plan_answers.choices(db.conn, p))}
    own = plan_answers.chosen(db.conn, p, picks)     # picks that name an own answer: the verdict quotes its words
    comments = _comments(comments, set(titles))
    note = visuals._text(note, "note", visuals.MAX_NOTE)
    doc, doc_record, threads = plan_docs.pending(db, p)
    if not approved and not comments and not note and not doc and not any(
            picks.get(d["id"]) not in (None, d["mine"]) for d in p["decisions"]):
        raise ServiceError("invalid", "say what to change: a comment, a note or a different pick", 400)
    visuals._refuse_secrets([("your note", note), *((f"your comment on {titles[c['section']]}", c["text"])
                                                    for c in comments)])
    if unconfirmed is not None:
        held = _hold_approval(db, p, unconfirmed, was_ready, {"rev": rev, "version": version, "picks": picks,
                                                              "comments": comments, "note": note,
                                                              "attachments": attachments})
        raise ServiceError("gate_unverified", UNCONFIRMED[unconfirmed], 503, reason=unconfirmed, approval_held=held)
    files = plan_docs._attachments(db, p, attachments)
    at = time.time()
    verdict = {"approved": bool(approved), "picks": picks, "comments": comments, "note": note, "at": at, "rev": rev,
               "attachments": [f["name"] for f in files], "answers": {d: a["text"] for d, a in own.items()}}
    record = json.dumps(verdict)
    answer = json.dumps({"option": "approve" if approved else "redirect", "note": note, "at": at, "rev": rev})
    state = "approved" if approved else "redirected"
    text = _message(p, approved, picks, comments, note, titles, doc, [f["ref"] for f in files],
                    drafting=not approved and plan_docs.draft(db, p) is not None, answers=own,
                    teammates=plan_docs.teammate_names(db, p["id"]))
    with db.tx() as c:
        if approved:
            # In the verdict's own transaction: a sync landing a new reviewer can't slip in between.
            shut = _gate(c, p)
            if shut is not None and shut["state"] in GATE_SHUT:
                raise ServiceError("gate_closed", shut["reason"], 409)
            # A frozen version is approved only while it is still this revision here: a share
            # dropped since (no versions any more) or moved on is a plan to read again.
            if version is not None and _is_version(c, p["id"], rev, version) is not True:
                raise ServiceError("stale", f"version {version} isn't the one out for sign-off now; read the plan "
                                   "again", 409, rev=rev)
        claimed = c.execute("UPDATE plans SET state=?, decision=?, updated_at=? WHERE id=? AND rev=? AND state='open'",
                            (state, record, at, p["id"], rev)).rowcount == 1
        if claimed:
            plan_docs.record_event(c, p["id"], f"verdict:{rev}", "sign_off" if approved else "changes",
                                   plan_docs.PERSON, at, rev=rev)
            plan_docs.mark_sent(c, threads, rev)
            c.execute("UPDATE plan_shares SET unconfirmed=NULL, approve_wanted=NULL, approve_refused=NULL"
                      " WHERE plan_id=?", (p["id"],))
            c.execute("UPDATE visuals SET answer=?, read_rev=MAX(read_rev, rev) WHERE id=?", (answer, p["id"]))
            if not approved:
                visual_offers._grant(c, p["root_id"], "plan")   # the one revision the redirect asks for
            queued = _queue_message(c, p["root_id"], text)
    if not claimed:
        now = _row(db, p["id"])
        if now["rev"] != rev:
            raise ServiceError("stale", f"the plan has a newer revision (r{now['rev']}); read that one", 409,
                               rev=now["rev"])
        raise ServiceError("decided", "this revision is already decided", 409)
    bus.publish_agent(db, "message.queued", p["root_id"], message_id=queued, sender_id=None)
    try:
        agents.flush(db, p["root_id"])
    except Exception:  # noqa: BLE001 - it stays queued: the supervisor gets it at its next turn
        log.warning("brindle: couldn't push plan %s's verdict to %s now; it stays queued", p["id"], p["root_id"],
                    exc_info=True)
    bus.publish(db, "visual.answered", repo_root=p["visual"]["repo_root"], root_id=p["root_id"], visual=p["id"],
                rev=rev, option=state)
    if approved:
        if gate(db, p) is None:             # a shared plan's one Library entry is its document
            _to_library(p, picks, comments, note, titles, doc_record, answers=own)
        tasks.on_resumed(db, p["root_id"], why="the person's approval of the plan")
    return get(db, p["id"])


def cancel(db: DB, plan_id: str, rev: Any, *, person: bool, caller: Agent | None = None) -> dict:
    """Close an open or redirected plan as cancelled, approving nothing: the
    person's Cancel (``person``, with the revision on screen as ``rev``), or the
    session's supervisor withdrawing its own (``caller``; ``rev`` optional).
    Whatever the review state, a shared plan's share is dropped
    (shared_docs.drop_share), so its Library doc is unshared at the next pass
    that reaches the Library. It is never sent to the Library (_to_library).

    The tasks it held stay queued, each marked with the plan
    (plan_cancel_holds): they neither start nor get dropped until the
    supervisor decides each one, with start_now or cancel_task. One message
    to the supervisor names them."""
    from brindle import agents, autopilot, plan_docs, tasks
    from brindle.pro import shared_docs

    visuals.require_enabled()
    p = get(db, plan_id)
    if person:
        visuals._unlocked(p["root_id"])
        if not isinstance(rev, int) or isinstance(rev, bool):
            raise ServiceError("invalid", "rev (the revision on screen) is required", 400)
    elif caller is None or caller.id != p["root_id"]:
        raise ServiceError("forbidden", "only the session's supervisor withdraws its plan", 403)
    if rev is not None and rev != p["rev"]:
        raise ServiceError("stale", f"the plan has a newer revision (r{p['rev']}); read that one", 409, rev=p["rev"])
    if p["state"] == WRITING:
        raise ServiceError("writing", "the supervisor is still writing this plan", 409)
    if p["state"] not in ("open", "redirected"):
        raise ServiceError("decided", "this plan is already decided", 409)
    root = p["root_id"]
    at = time.time()
    with db.tx() as c:
        # Selected before the plan's state changes, in the same write transaction: a task queued
        # by an assign whose reply is still going out (unanswered) is held too, and nothing
        # committed after this read can slip past the cancel.
        held = [t for t in db.list_tasks(state="pending") + db.list_tasks(state=tasks.UNANSWERED)
                if t.caller_id and autopilot.root_of(db, t.caller_id) == root and tasks.open_plan_holds(db, t)]
        if not c.execute("UPDATE plans SET state=?, updated_at=? WHERE id=? AND rev=? AND state IN ('open','redirected')",
                         (CANCELLED, at, p["id"], p["rev"])).rowcount:
            raise ServiceError("decided", "this plan is already decided", 409)
        row = c.execute("SELECT * FROM plan_shares WHERE plan_id=?", (p["id"],)).fetchone()
        if row is not None:
            shared_docs.drop_share(c, row)
        for t in held:
            c.execute("INSERT OR REPLACE INTO plan_cancel_holds (task_id, plan_id, created_at)"
                      " SELECT id, ?, ? FROM tasks WHERE id=? AND state IN ('pending', ?)",
                      (p["id"], at, t.id, tasks.UNANSWERED))
        queued = _queue_message(c, root, _cancelled_message(p, held, person))
    bus.publish_agent(db, "message.queued", root, message_id=queued, sender_id=None)
    try:
        agents.flush(db, root)
    except Exception:  # noqa: BLE001 - it stays queued: the supervisor gets it at its next turn
        log.warning("brindle: couldn't push plan %s's cancellation to %s now; it stays queued", p["id"], root,
                    exc_info=True)
    bus.publish_agent(db, "autopilot.goal", root)
    p = get(db, p["id"])
    plan_docs._changed(db, p, p["rev"])
    return p


def _cancelled_message(p: dict, held: list, person: bool) -> str:
    lines = [f"{'The person cancelled' if person else 'You withdrew'} plan {p['id']} (r{p['rev']}): it is closed, "
             "nothing was approved, and it won't go to the Library."]
    if not held:
        return "\n".join([*lines, "It held no queued tasks."])
    lines.append(f"{len(held)} task(s) it held stay queued: none starts or is dropped on its own. Decide each one: "
                 "start_now(task_id) starts it, cancel_task(task_id) gives it up.")
    for t in held:
        brief = next((ln.strip() for ln in t.task_text.splitlines() if ln.strip()), "")[:120]
        lines.append(f"- {t.id} (branch {t.branch or 'none'}): {brief}")
    return "\n".join(lines)


def _record(p: dict, picks: dict, comments: list[dict], note: str | None, titles: dict,
            doc: list[str] = (), answers: dict | None = None) -> str:
    """The approved revision with what the person decided under it."""
    out = [p["markdown"].rstrip(), "", f"## What the person approved (r{p['rev']})", ""]
    for d in p["decisions"]:
        pick = picks.get(d["id"]) or d["mine"]
        label = next((o["label"] for o in d["options"] if o["id"] == pick), pick)
        if (answers or {}).get(d["id"]):
            label = f"Own answer: \"{answers[d['id']]['text']}\""
        out.append(f"- Decision {d['id']} ({d['title']}): {label}")
    for s in p["split"] or []:
        out.append(f"- Worker {s['id']}: {s['title']}")
        if s.get("after"):
            out.append(f"  - after: {', '.join(s['after'])}")
        if s.get("files"):
            out.append(f"  - files: {', '.join(s['files'])}")
        if s.get("test"):
            out.append(f"  - finish line (test): {s['test']}")
    out += [f"- On \"{titles[c['section']]}\": {c['text']}" for c in comments]
    out += [f"- {line}" for line in doc]
    if note:
        out.append(f"- Their note: {note}")
    return "\n".join(out) + "\n"


def _to_library(p: dict, picks: dict, comments: list[dict], note: str | None, titles: dict,
                doc: list[str] = (), answers: dict | None = None) -> None:
    """Store the approved plan in the person's plan library, on brindle's
    servers or on this Mac (Settings › Library location). Entitlement, the
    location and the network are the uploader's to judge; nothing here
    blocks or fails an approval."""
    try:
        from brindle.pro import analysis_docs

        md = _record(p, picks, comments, note, titles, doc, answers)
        doc = analysis_docs.Doc(files=[{"name": f"plan-{p['id']}.md", "md": md}],
                                source={"kind": "goal", "ref": p["id"], "session": p["root_id"]}, plan=True)
        analysis_docs.on_merged(p["visual"]["repo_root"], [doc])
    except Exception:  # noqa: BLE001 - the approval stands
        log.info("brindle: plan %s not queued for the library", p["id"], exc_info=True)


# -- the offer to plan first ------------------------------------------------------------


def answer_offer(db: DB, root_id: str, offer_id: Any, accept: bool) -> None:
    """The person's answer to brindle's offer to plan session ``root_id``'s goal
    first: ``offer_id``, the offer on their screen, so an answer to an older
    goal's offer never settles a newer one. One transaction settles it, and
    on Yes grants the one planning doc it asks for, and queues the message
    telling the supervisor. Yes keeps the tasks behind the offer queued until
    the plan is approved; No releases them."""
    from brindle import agents, tasks

    if not isinstance(offer_id, str) or not offer_id:
        raise ServiceError("invalid", "offer (the offer on screen) is required", 400)
    told = OFFER_YES + doc_templates.planning_brief() if accept else OFFER_NO   # read before the transaction
    with db.tx() as c:
        claimed = c.execute("UPDATE goal_doc_requests SET offer=? WHERE root_id=? AND offer=?",
                            (OFFER_ACCEPTED if accept else OFFER_DISMISSED, root_id,
                             OFFER_OPEN + offer_id)).rowcount == 1
        if claimed:
            if accept:
                visual_offers._grant(c, root_id, "plan")
            queued = _queue_message(c, root_id, told)
    if not claimed:
        raise ServiceError("stale", "this offer is no longer open", 409)
    if accept:
        begin(db, root_id)      # the plan is ready for the person's screen now
    bus.publish_agent(db, "autopilot.goal", root_id)
    bus.publish_agent(db, "message.queued", root_id, message_id=queued, sender_id=None)
    try:
        agents.flush(db, root_id)
    except Exception:  # noqa: BLE001 - it stays queued: the supervisor gets it at its next turn
        log.warning("brindle: couldn't push the plan offer's answer to %s now; it stays queued", root_id,
                    exc_info=True)
    if not accept:
        tasks.on_resumed(db, root_id, why="the person's choice not to plan first")
