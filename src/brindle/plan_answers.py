"""Own answers on a plan's decisions: an option row someone typed under the
supervisor's options ("+ Your own answer" on the decision card).

A revision's decisions are immutable, so own answers are layered on top of
them: rows of ``plan_answers``, keyed by the decision's id, each with its
author (the person, or ``teammate:<account>`` pulled from the Library). Each
answer has an id of its own (``own-…``), and a pick names it like any option:
``plan_votes`` holds it, the "You: … · David: …" summary shows it, picking it
counts as decided, and the verdict carries its text to the supervisor.

Several people may add answers to one card; anyone can pick any of them. The
author may edit or remove their answer until someone else has picked it: from
then on it stays as written, since someone chose those words.

A shared plan's answers travel through the Library (docs/shared-designs-api.md
in pawdelta-web, ``/design/answers``): :func:`pull` takes the teammates'
answers from its view and :func:`push` sends the person's. A Library without
``/answers`` refuses them, and they stay on this machine."""

from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Any

from brindle import plan_docs, visuals
from brindle.db import DB
from brindle.redact import mask
from brindle.service.errors import ServiceError

log = logging.getLogger(__name__)

PREFIX = "own-"
ID = re.compile(r"own-[A-Za-z0-9_-]{1,28}")
MAX_TEXT = 500              # characters in one answer
MAX_PER_DECISION = 20       # answers on one decision, everyone's
MAX_PER_AUTHOR = 5          # one person's answers on one decision


def _rows(conn, plan_id: str, *, removed: bool = False) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plan_answers WHERE plan_id=?" + ("" if removed else " AND removed=0")
        + " ORDER BY at, answer_id", (plan_id,))]


def by_decision(conn, p: dict) -> dict[str, list[dict]]:
    """The answers on each of the revision's decisions, oldest first."""
    ids = {d["id"] for d in p["decisions"]}
    out: dict[str, list[dict]] = {}
    for a in _rows(conn, p["id"]):
        if a["decision_id"] in ids:
            out.setdefault(a["decision_id"], []).append(a)
    return out


def choices(conn, p: dict) -> dict[str, set[str]]:
    """What a pick may name on each decision: its options and its own answers."""
    answers = by_decision(conn, p)
    return {d["id"]: {o["id"] for o in d["options"]} | {a["answer_id"] for a in answers.get(d["id"], [])}
            for d in p["decisions"]}


def _pickers(conn, plan_id: str, a: dict) -> list[str]:
    """Who picked answer ``a``, its author left out."""
    return [r["voter"] for r in conn.execute(
        "SELECT voter FROM plan_votes WHERE plan_id=? AND decision_id=? AND option_id=? AND voter<>?",
        (plan_id, a["decision_id"], a["answer_id"], a["author"]))]


def dto(conn, p: dict) -> dict[str, list[dict]]:
    """Each decision's answers as the reader draws them: ``locked`` once someone else picked it."""
    return {decision: [{"id": a["answer_id"], "text": a["text"], "author": a["author"], "at": a["at"],
                        "locked": bool(_pickers(conn, p["id"], a))} for a in rows]
            for decision, rows in by_decision(conn, p).items()}


def chosen(conn, p: dict, picks: dict[str, str]) -> dict[str, dict]:
    """The answers ``picks`` names, by decision: what a verdict quotes. Once
    someone else picked one, only the words the Library agreed to count: an
    edit here it hasn't taken can't reach the supervisor."""
    answers = {a["answer_id"]: a for rows in by_decision(conn, p).values() for a in rows}
    out = {d: answers[o] for d, o in picks.items() if o in answers and answers[o]["decision_id"] == d}
    return {d: {**a, "text": a["sent_text"]} if a["sent_text"] is not None and a["text"] != a["sent_text"]
            and _pickers(conn, p["id"], a) else a for d, a in out.items()}


def new_id() -> str:
    """An answer's id: chosen here, so one written offline goes up as itself."""
    return PREFIX + secrets.token_hex(6)


def _text(raw: Any) -> str:
    text = visuals._text(raw, "your answer", MAX_TEXT, required=True)
    visuals._refuse_secrets([("your answer", text)])
    return " ".join(text.split())


def _open(db: DB, plan_id: str, rev: Any) -> dict:
    p = plan_docs._plan(db, plan_id)
    if p["state"] != "open":
        plan_docs._editable(p)
    plan_docs._on_screen(p, rev)
    return p


def add(db: DB, plan_id: str, rev: Any, decision: Any, text: Any) -> str:
    """The person adds an answer of their own to a decision, and picks it.
    Returns its id."""
    p = _open(db, plan_id, rev)
    if decision not in {d["id"] for d in p["decisions"]}:
        raise ServiceError("invalid", "answer one of this revision's decisions", 400)
    text = _text(text)
    answer_id = new_id()
    with db.tx() as c:
        rows = [a for a in _rows(c, p["id"]) if a["decision_id"] == decision]
        if len(rows) >= MAX_PER_DECISION:
            raise ServiceError("too_many", f"a decision takes at most {MAX_PER_DECISION} own answers", 400)
        if sum(a["author"] == plan_docs.PERSON for a in rows) >= MAX_PER_AUTHOR:
            raise ServiceError("too_many", f"you can add at most {MAX_PER_AUTHOR} answers of your own here", 400)
        now = time.time()
        c.execute("INSERT INTO plan_answers (plan_id, answer_id, decision_id, author, text, at) VALUES (?,?,?,?,?,?)",
                  (p["id"], answer_id, decision, plan_docs.PERSON, text, now))
        c.execute("INSERT INTO plan_votes (plan_id, decision_id, voter, option_id, rev, at) VALUES (?,?,?,?,?,?)"
                  " ON CONFLICT(plan_id, decision_id, voter) DO UPDATE SET option_id=excluded.option_id,"
                  " rev=excluded.rev, at=excluded.at", (p["id"], decision, plan_docs.PERSON, answer_id, p["rev"], now))
    plan_docs._changed(db, p, p["rev"])
    return answer_id


def _own(c, p: dict, answer_id: Any) -> dict:
    """The person's answer ``answer_id``, while it is still theirs to change."""
    row = c.execute("SELECT * FROM plan_answers WHERE plan_id=? AND answer_id=? AND removed=0",
                    (p["id"], answer_id if isinstance(answer_id, str) else "")).fetchone()
    if row is None:
        raise ServiceError("not_found", "no such answer on this plan", 404)
    a = dict(row)
    if a["author"] != plan_docs.PERSON:
        raise ServiceError("forbidden", "only its author can change an answer", 403)
    if _pickers(c, p["id"], a):
        raise ServiceError("picked", "someone else picked this answer, so it stays as written", 409)
    return a


def edit(db: DB, plan_id: str, rev: Any, answer_id: Any, text: Any) -> None:
    p = _open(db, plan_id, rev)
    text = _text(text)
    with db.tx() as c:
        a = _own(c, p, answer_id)
        c.execute("UPDATE plan_answers SET text=? WHERE plan_id=? AND answer_id=?", (text, p["id"], a["answer_id"]))
    plan_docs._changed(db, p, p["rev"])


def remove(db: DB, plan_id: str, rev: Any, answer_id: Any) -> None:
    """Take the person's answer back, and their pick of it. One the Library
    has stays marked removed until :func:`push` has dropped it there."""
    p = _open(db, plan_id, rev)
    with db.tx() as c:
        a = _own(c, p, answer_id)
        c.execute("DELETE FROM plan_votes WHERE plan_id=? AND decision_id=? AND option_id=?",
                  (p["id"], a["decision_id"], a["answer_id"]))
        if a["sent_text"] is None:
            c.execute("DELETE FROM plan_answers WHERE plan_id=? AND answer_id=?", (p["id"], a["answer_id"]))
        else:
            c.execute("UPDATE plan_answers SET removed=1 WHERE plan_id=? AND answer_id=?", (p["id"], a["answer_id"]))
    plan_docs._changed(db, p, p["rev"])


# -- the Library (brindle.pro.shared_docs) ------------------------------------------------


def _theirs(decisions: Any) -> list[tuple[str, dict]]:
    out = []
    for d in decisions or []:
        if not isinstance(d, dict) or not isinstance(d.get("id"), str):
            continue
        for a in d.get("answers") or []:
            if (isinstance(a, dict) and isinstance(a.get("id"), str) and ID.fullmatch(a["id"])
                    and isinstance(a.get("text"), str) and isinstance(a.get("author"), str)):
                out.append((d["id"], a))
    return out


def _set(c, plan_id: str, answer_id: str, **fields: Any) -> None:
    c.execute(f"UPDATE plan_answers SET {', '.join(f'{k}=?' for k in fields)} WHERE plan_id=? AND answer_id=?",
              (*fields.values(), plan_id, answer_id))


def _drop(c, plan_id: str, a: dict) -> None:
    c.execute("DELETE FROM plan_answers WHERE plan_id=? AND answer_id=?", (plan_id, a["answer_id"]))
    c.execute("DELETE FROM plan_votes WHERE plan_id=? AND decision_id=? AND option_id=?",
              (plan_id, a["decision_id"], a["answer_id"]))


def reconcile(c, plan_id: str, owner: str, view: dict) -> int:
    """Line the person's answers up with what the Library holds of them on
    ``view``'s revision. ``sent_text`` is the last text both sides agreed on:
    a local edit or removal is still to be sent only while the Library's
    answer is that text and nobody else has picked it. Anything else is the
    Library's to say, and it wins: a change made elsewhere (their account on
    another machine), a change made there at the same time as one here, and
    an answer someone else has picked, which stays as written. One the
    Library held on this revision and no longer does was removed there."""
    rev = view.get("rev")
    if not isinstance(rev, int) or isinstance(rev, bool):
        return 0
    decisions = view.get("decisions") or []
    theirs = {a["id"]: a for _, a in _theirs(decisions) if a["author"] == owner}
    picked = {option for d in decisions if isinstance(d, dict) and isinstance(d.get("votes"), dict)
              for option, subs in d["votes"].items() if isinstance(subs, list) and any(s != owner for s in subs)}
    changes = 0
    for a in _rows(c, plan_id, removed=True):
        if a["author"] != plan_docs.PERSON:
            continue
        got = theirs.get(a["answer_id"])
        if got is None:
            if a["sent_text"] is not None and a["sent_rev"] == rev:
                _drop(c, plan_id, a)
                changes += 1
            continue
        text = mask(got["text"])[:MAX_TEXT]
        pending = bool(a["removed"]) or a["text"] != a["sent_text"]
        if pending and text == a["sent_text"] and a["answer_id"] not in picked:
            continue
        if (a["text"], a["sent_text"], a["sent_rev"], a["removed"]) != (text, text, rev, 0):
            _set(c, plan_id, a["answer_id"], text=text, sent_text=text, sent_rev=rev, removed=0)
            changes += 1
    return changes


def pull(c, plan_id: str, owner: str, view: dict, now: float) -> int:
    """Make the teammates' answers the Library's current ones and line the
    person's up with it (:func:`reconcile`). The person's from elsewhere
    (their account on another machine) come in as theirs."""
    changes = reconcile(c, plan_id, owner, view)
    mine = {a["answer_id"] for a in _rows(c, plan_id, removed=True) if a["author"] == plan_docs.PERSON}
    wanted = {}
    for decision, a in _theirs(view.get("decisions")):
        text, at = mask(a["text"])[:MAX_TEXT], a.get("at") if isinstance(a.get("at"), (int, float)) else now
        if a["author"] != owner:
            wanted[a["id"]] = (decision, plan_docs.TEAMMATE + a["author"], text, float(at))
        elif a["id"] not in mine:
            c.execute("INSERT OR IGNORE INTO plan_answers (plan_id, answer_id, decision_id, author, text, at, sent_text,"
                      " sent_rev) VALUES (?,?,?,?,?,?,?,?)",
                      (plan_id, a["id"], decision, plan_docs.PERSON, text, float(at), text, view.get("rev")))
            changes += 1
    held = {a["answer_id"]: (a["decision_id"], a["author"], a["text"], a["at"])
            for a in _rows(c, plan_id) if a["author"].startswith(plan_docs.TEAMMATE)}
    if held != wanted:
        c.execute("DELETE FROM plan_answers WHERE plan_id=? AND author LIKE ?", (plan_id, plan_docs.TEAMMATE + "%"))
        c.executemany("INSERT OR IGNORE INTO plan_answers (plan_id, answer_id, decision_id, author, text, at)"
                      " VALUES (?,?,?,?,?,?)", [(plan_id, i, *row) for i, row in wanted.items()])
        changes += 1
    return changes


UNSUPPORTED = (404, 405)    # an older Library's answer to POST /answers: it has no own answers yet


def push(db: DB, plan_id: str, owner: str, head) -> None:
    """Send the person's answers as they are here and not on the Library's
    head: new ones, edited ones and removed ones. Before their picks, which
    may name them. Each write is judged as ``head.write`` judges one: an
    access or org-rule refusal is kept on the plan and bars its review, and
    an answer older than it can't clear it. The one exception is an older
    Library's 404/405 to a new answer: it has no /answers, and the answers
    stay on this machine. When the Library says an answer was picked by
    someone else, the change here is taken back and its answer stands."""
    from brindle.pro import analysis_docs as ad
    from brindle.pro import review_policies
    from brindle.pro import shared_docs as sd

    def write(method: str, tail: str, body: dict | None, unsupported: tuple = ()) -> tuple[int, dict]:
        status, resp = ad._call(method, sd._path(head.doc_id, tail), body, client=head.client, store=head.store)
        resp = resp if isinstance(resp, dict) else {}
        if status == 200:
            head.view, head.refused = resp, False
            review_policies.seen(db, head.p, resp)
        elif status not in unsupported:
            review_policies.refused(db, head.p, resp, status, document=False)
            head.refused = (head.refused or resp.get("error") in review_policies.REFUSALS
                            or status in review_policies.ACCESS_REFUSED)
        if status != 200:
            log.info("shared doc %s: %s %s refused (%s), kept for the next sync", head.doc_id, method, tail,
                     resp.get("error") or status)
        return status, resp

    with db.tx() as c:
        reconcile(c, plan_id, owner, head.view)
    on_head = {d["id"] for d in head.view.get("decisions") or [] if isinstance(d, dict)}
    theirs = {a["id"]: a for _, a in _theirs(head.view.get("decisions")) if a["author"] == owner}
    for a in _rows(db.conn, plan_id, removed=True):
        if a["author"] != plan_docs.PERSON:
            continue
        got, rev = theirs.get(a["answer_id"]), head.rev
        if a["removed"]:
            status, resp = (200, {}) if got is None else write("DELETE", f"/answers/{a['answer_id']}?rev={rev}", None)
            with db.tx() as c:
                if status == 200:
                    c.execute("DELETE FROM plan_answers WHERE plan_id=? AND answer_id=?", (plan_id, a["answer_id"]))
                elif resp.get("error") == "picked":
                    stands = mask(got["text"])[:MAX_TEXT]
                    _set(c, plan_id, a["answer_id"], removed=0, text=stands, sent_text=stands, sent_rev=rev)
            continue
        if a["decision_id"] not in on_head:
            continue
        if got is None:
            status, resp = write("POST", "/answers", {"rev": rev, "decision": a["decision_id"], "id": a["answer_id"],
                                                      "text": a["text"]}, UNSUPPORTED)
        elif got["text"] != a["text"]:
            status, resp = write("PUT", f"/answers/{a['answer_id']}", {"rev": rev, "text": a["text"]})
        else:
            status, resp = 200, {}
        with db.tx() as c:
            if status == 200:
                _set(c, plan_id, a["answer_id"], sent_text=a["text"], sent_rev=rev)
            elif got is not None and resp.get("error") == "picked":
                stands = mask(got["text"])[:MAX_TEXT]
                _set(c, plan_id, a["answer_id"], text=stands, sent_text=stands, sent_rev=rev)
