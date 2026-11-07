"""Person-controlled visual offers and single-use publication grants.

All decisions and grant consumption run in the caller's write transaction.
The user setting permits offers only; it never authorizes publication.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
import unicodedata

from brindle import bus, config, debuglog, visuals
from brindle.db import DB, Agent
from brindle.service.errors import ServiceError

log = logging.getLogger(__name__)
COOLDOWN = 15 * 60
DAY = 24 * 60 * 60
FOLD = 30 * 60


def _expire(c, root: str, now: float) -> None:
    c.execute("UPDATE visual_offers SET state='expired', changed_at=? "
              "WHERE root_id=? AND state='open' AND created_at<=?", (now, root, now - FOLD))


def _dto(row) -> dict | None:
    return ({"id": row["id"], "kind": row["kind"], "counts": json.loads(row["counts"]),
             "expires_at": row["created_at"] + FOLD} if row else None)


def current(db: DB, root: str) -> dict | None:
    visuals.require_enabled()
    visuals._unlocked(root)
    with db.tx() as c:
        _expire(c, root, time.time())
        c.execute("DELETE FROM visual_requests WHERE root_id=? AND created_at<=?", (root, time.time() - FOLD))
        request = c.execute("SELECT * FROM visual_requests WHERE root_id=?", (root,)).fetchone()
        if request:
            return {"id": request["id"], "kind": "request", "counts": {},
                    "confirmation": request["scope"], "expires_at": request["created_at"] + FOLD}
        if config.visuals_setting() != "on":
            return None
        return _dto(c.execute("SELECT * FROM visual_offers WHERE root_id=? AND state='open'",
                              (root,)).fetchone())


def offer(db: DB, caller: Agent | None, question: str, kind: str, **counts: int) -> dict:
    visuals.require_enabled()
    root, ws = visuals._session(db, caller)
    if caller.id != root.id:
        return {"offered": False, "reason": "only_supervisors"}
    question = visuals._text(question, "question", visuals.MAX_QUESTION, True)
    visuals._refuse_secrets([("the question", question)])
    keys = ("options", "attributes", "rows", "columns", "steps", "branches", "lines")
    if any(k not in keys or type(v) is not int or not 0 <= v <= 10000 for k, v in counts.items()):
        raise ServiceError("invalid", "counts must be nonnegative integers", 400)
    n = {k: counts.get(k, 0) for k in keys}
    eligible = {
        "options": n["options"] >= 3 or (n["options"] == 2 and n["attributes"] >= 3),
        "comparison": n["rows"] >= 4 and n["columns"] >= 3,
        "flow": n["steps"] >= 5 or n["branches"] >= 1,
        "diff": n["lines"] > 12,
        "layout": True,
    }.get(kind, False)
    normalized = " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", question).casefold()))
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    now = time.time()
    with db.tx() as c:
        _expire(c, root.id, now)
        history = c.execute("SELECT * FROM visual_offers WHERE root_id=? ORDER BY created_at DESC, rowid DESC",
                            (root.id,)).fetchall()
        reason = None
        if config.visuals_setting() != "on":
            reason = "setting_off"
        elif not eligible:
            reason = "below_threshold"
        elif any(r["state"] == "open" for r in history):
            reason = "open_offer"
        elif any(r["question_hash"] == digest for r in history):
            reason = "duplicate_question"
        elif history and now - history[0]["created_at"] < COOLDOWN:
            reason = "cooldown"
        elif sum(r["created_at"] > now - DAY for r in history) >= 4:
            reason = "daily_cap"
        else:
            decisions = [r for r in history if r["state"] in ("accepted", "dismissed")]
            if len(decisions) >= 2 and all(r["state"] == "dismissed" for r in decisions[:2]) \
                    and now - decisions[0]["changed_at"] < DAY:
                reason = "dismissals"
        if reason:
            return {"offered": False, "reason": reason}
        oid = "o_" + secrets.token_hex(6)
        c.execute("INSERT INTO visual_offers VALUES (?,?,?,?,?,?,?,?,?)",
                  (oid, root.id, digest, question, kind, json.dumps(n), "open", now, now))
    log.debug("visual offer created")
    debuglog.record("visuals", "debug", "offer", {"count": 1})
    bus.publish(db, "visual.offered", repo_root=ws.repo_root, root_id=root.id, offer=oid)
    return {"offered": True, "id": oid}


def resolve(db: DB, root: str, oid: str, accept: bool) -> dict:
    from brindle import agents

    visuals.require_enabled()
    visuals._unlocked(root)
    with db.tx() as c:
        _expire(c, root, time.time())
        request = c.execute("SELECT * FROM visual_requests WHERE id=? AND root_id=? AND created_at>?",
                            (oid, root, time.time() - FOLD)).fetchone()
        row = c.execute("SELECT * FROM visual_offers WHERE id=? AND root_id=? AND state='open'",
                        (oid, root)).fetchone()
        if request is None and (row is None or config.visuals_setting() != "on"):
            raise ServiceError("stale", "this offer is no longer open", 409)
        if request:
            c.execute("DELETE FROM visual_requests WHERE root_id=?", (root,))
            scope = request["scope"]
            message = f"The person confirmed: create the {scope} they mentioned now."
        else:
            c.execute("UPDATE visual_offers SET state=?, changed_at=? WHERE id=?",
                      ("accepted" if accept else "dismissed", time.time(), oid))
            scope = "visual"
            message = f"The person asked for a visual of: {row['question']}"
        if accept:
            _grant(c, root, scope)
            # Queue in the same transaction as the grant: no double delivery on repeated clicks.
            c.execute("INSERT INTO inbox (agent_id, sender_id, body, created_at, held) VALUES (?,NULL,?,?,0)",
                      (root, message, time.time()))
    if accept:
        agents.flush(db, root)
    log.debug("visual offer %s", "accepted" if accept else "dismissed")
    debuglog.record("visuals", "debug", "accept" if accept else "dismiss", {"count": 1})
    bus.publish(db, "visual.offer_resolved", root_id=root, offer=oid)
    return {"accepted": accept}


def _grant(c, root: str, scope: str = "visual") -> None:
    c.execute("INSERT INTO visual_grants (root_id, remaining, explicit, scope) VALUES (?,1,0,?) "
              "ON CONFLICT(root_id) DO UPDATE SET remaining=1, scope=excluded.scope", (root, scope))


_REQUEST = re.compile(
    r"(?:(?:can|could|would|will) you\s+)?(?:please\s+)?"
    r"(?:(?P<visualize>visualize)\b|(?:(?:make|create|show|draw|build|generate|render)\s+(?:(?:me|us)\s+)?|"
    r"(?:I want|I'd like|I would like)\s+)"
    r"(?:(?:a|an|the|this|that)\s+)?(?:(?:simple|interactive|quick|small|new)\s+)*"
    r"(?P<artifact>visual(?:ization)?|diagram|chart|mockup|planning doc(?:ument)?|plan document)\b)", re.I)
_REFERENT = re.compile(r"(?:\s+(?:(?:of|for|about|showing)\s+)?(?:this|that|these|those|it)"
                       r"(?:\s+(?:layout|change|choices|options|flow|pipeline|decision))?)?[.!?]*", re.I)
# "visualize the changes for 0.6.3" / "show me the flow visually": a plain object phrase,
# no joining word that could add a second instruction.
_OBJECT = re.compile(r"(?:\s+(?!(?:and|then|also|but|so|while|or|plus)\b)[\w'.,/-]+){1,20}[.!?]*", re.I)
_SHOW_VISUALLY = re.compile(r"(?:(?:can|could|would|will) you\s+)?(?:please\s+)?show\s+(?:me|us)"
                            r"(?:\s+(?!(?:and|then|also|but|so|while|or|plus)\b)[\w'.,/-]+){1,20}\s+visually[.!?]*", re.I)
# A deferral or approval clause inside an object phrase (not a subject word like "approval flow").
_DEFERRED = re.compile(r"\b(?:once|tomorrow|tonight|soon|eventually|whenever|(?:I|we|you|they)\s+approve[sd]?)\b", re.I)
_QUALIFIED = re.compile(r"\b(?:if|unless|when|after|before|later|only|maybe|perhaps|not|never|"
                        r"don't|do not|wait|until|provided|assuming)\b|[\"`“”]", re.I)
_CONTEXT = re.compile(r"(?:compare|consider|examine|review)\s+(?:this|that|these|those|the)\s+"
                      r"(?:choices|options|layouts|designs|flow|pipeline|change|decision)[.!?]*", re.I)
_CANCEL_REQUEST = re.compile(
    r"^\s*(?:please\s+)?(?:(?:cancel|stop)\s+(?:(?:the|this|that)\s+)?|"
    r"(?:don't|do not)\s+(?:make|create|show|draw|build|generate|render)\s+(?:(?:a|an|the)\s+)?)"
    r"(?:visual|diagram|chart|mockup|planning document|planning doc|plan document|request)\b|"
    r"^\s*(?:never mind|nevermind)[.!?]*\s*$", re.I)


def request_intent(text: str) -> tuple[str | None, bool]:
    """Recognize complete direct request sentences; uncertain intent needs a real confirmation.

    Surrounding context is allowed. Arbitrary trailing clauses, quotations,
    conditions and deferrals cannot authorize anything from a matched prefix.
    """
    found = []
    context_clear = True
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text.strip()):
        match = _REQUEST.match(sentence)
        if match:
            scope = "plan" if (match["artifact"] or "").lower().startswith(("planning", "plan ")) else "visual"
            rest = sentence[match.end():]
            clear = _REFERENT.fullmatch(rest) is not None or (
                match["visualize"] is not None and _OBJECT.fullmatch(rest) is not None and not _DEFERRED.search(rest))
            found.append((scope, clear))
        elif _SHOW_VISUALLY.fullmatch(sentence):
            found.append(("visual", True))
        elif sentence:
            # Unknown discourse may be an example, quotation, or instruction
            # to defer. Recognize the request but let the person confirm it.
            context_clear = context_clear and _CONTEXT.fullmatch(sentence) is not None
    if not found:
        return None, False
    scopes = {scope for scope, _ in found}
    return found[-1][0], context_clear and len(scopes) == 1 and all(clear for _, clear in found) and not _QUALIFIED.search(text)


def begin_input(db: DB, agent: Agent, expires_at: float | None = None) -> int:
    """A new authenticated terminal observer supersedes earlier observers."""
    visuals.require_enabled()
    with db.tx() as c:
        if expires_at is not None and time.time() >= expires_at:
            raise ServiceError("stale", "this input observation expired", 409)
        row = c.execute("INSERT INTO visual_input_state (root_id,generation,sequence,observer) VALUES (?,1,0,1) ON CONFLICT(root_id) "
                        "DO UPDATE SET generation=generation+1, observer=generation+1, sequence=0 RETURNING generation",
                        (agent.id,)).fetchone()
        return row["generation"]


def refresh_input(db: DB, agent: Agent, observer: int, submitted_at: float, expires_at: float | None = None) -> int:
    """Read the current message context without replacing the terminal's registration.

    A direct message invalidates old context snapshots, while the same open
    terminal can snapshot the new context for its next submission. The original
    submission timestamp must survive refresh, including delayed dispatch: a
    refresh cannot relabel old input with a later direct message's context. A replaced
    terminal cannot acquire a fresh context using its old observer identity.
    """
    visuals.require_enabled()
    with db.tx() as c:
        if expires_at is not None and time.time() >= expires_at:
            raise ServiceError("stale", "this input observation expired", 409)
        row = c.execute("SELECT generation,invalidated_at FROM visual_input_state WHERE root_id=? AND observer=?",
                        (agent.id, observer)).fetchone()
        if row is None:
            raise ServiceError("stale", "this terminal observer was superseded", 409)
        if submitted_at <= row["invalidated_at"]:
            raise ServiceError("stale", "this submission predates a direct person message", 409)
        return row["generation"]


def person_spoke(db: DB, agent: Agent, text: str, *, expires_at: float | None = None,
                 observation: tuple[int, int] | None = None, submitted_at: float | None = None,
                 fold_only: bool = False) -> None:
    """Called only for authenticated person input, never for agent messages."""
    if not visuals.enabled():
        return
    root, _ = visuals._session(db, agent)
    if root.id != agent.id:
        return
    scope, clear = request_intent(text)
    with db.tx() as c:
        if expires_at is not None and time.time() >= expires_at:
            raise ServiceError("stale", "this input observation expired", 409)
        if observation is not None:
            generation, sequence = observation
            changed = c.execute("UPDATE visual_input_state SET sequence=? WHERE root_id=? "
                                "AND generation=? AND sequence<? AND invalidated_at<?",
                                (sequence, root.id, generation, sequence, submitted_at)).rowcount
            if not changed:
                raise ServiceError("stale", "this input observation was superseded", 409)
        else:
            # Invalidate snapshots taken before this message, retaining the
            # open observer and its sequence ordering for future submissions.
            c.execute("INSERT INTO visual_input_state (root_id,generation,sequence,observer,invalidated_at) "
                      "VALUES (?,1,0,0,?) ON CONFLICT(root_id) DO UPDATE SET "
                      "generation=generation+1, invalidated_at=MAX(invalidated_at,excluded.invalidated_at)",
                      (root.id, time.time()))
        c.execute("UPDATE visual_offers SET state='answered', changed_at=? WHERE root_id=? AND state='open'",
                  (time.time(), root.id))
        if not fold_only:
            c.execute("DELETE FROM visual_requests WHERE root_id=?", (root.id,))
        # A tool approval or ordinary follow-up does not cancel the person's
        # still unused request. Only a new artifact request or explicit stop does.
        if not fold_only and (scope or _CANCEL_REQUEST.search(text)):
            c.execute("DELETE FROM visual_grants WHERE root_id=?", (root.id,))
        if not fold_only and scope and clear:
            _grant(c, root.id, scope)
        elif not fold_only and scope:
            c.execute("INSERT INTO visual_requests VALUES (?,?,?,?)",
                      (root.id, "r_" + secrets.token_hex(6), scope, time.time()))
    bus.publish(db, "visual.offer_resolved", root_id=root.id)


def consume(c, root: str, kind: str = "visual") -> None:
    changed = c.execute("UPDATE visual_grants SET remaining=remaining-1 WHERE root_id=? AND remaining>0 "
                        "AND scope=?", (root, kind)).rowcount
    if not changed:
        raise ServiceError("visual_request_required", "accept an offer or ask for a visual first", 403)
