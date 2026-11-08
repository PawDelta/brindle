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
MIN_QUOTE_WORDS = 2     # a lone "yes" or "the" shows nothing about what was asked for
MAX_KEPT_MESSAGES = 8   # per session
MAX_KEPT_CHARS = 4000   # a longer message isn't kept: asking to see something is short


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
            # Their answer on the card is that message's one say, yes or no, however often they typed it.
            c.execute("DELETE FROM visual_person_words WHERE root_id=? AND words IN "
                      "(SELECT words FROM visual_person_words WHERE root_id=? AND source='request')", (root, root))
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


def _unlink(c, root: str, source: str) -> None:
    """The grant or confirmation a typed message raised is gone; the message itself isn't spent."""
    c.execute("UPDATE visual_person_words SET source=NULL WHERE root_id=? AND source=?", (root, source))


def _link(c, root: str, text: str, source: str) -> None:
    c.execute("UPDATE visual_person_words SET source=? WHERE id=(SELECT MAX(id) FROM visual_person_words "
              "WHERE root_id=? AND words=? AND source IS NULL)", (source, root, _words(text)))


def _grant(c, root: str, scope: str = "visual") -> None:
    _unlink(c, root, "grant")
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
_DEFERRED = re.compile(r"\b(?:once|tomorrow|tonight|soon|eventually|whenever|next\s+(?:week|month|year|day|time|sprint|quarter)|(?:I|we|you|they)\s+approve[sd]?)\b", re.I)
_QUALIFIED = re.compile(r"\b(?:if|unless|when|after|before|later|only|maybe|perhaps|not|never|"
                        r"don't|do not|wait|until|provided|assuming)\b|[\"`“”]", re.I)
_CONTEXT = re.compile(r"(?:compare|consider|examine|review)\s+(?:this|that|these|those|the)\s+"
                      r"(?:choices|options|layouts|designs|flow|pipeline|change|decision)[.!?]*", re.I)
_CANCEL_LEAD = r"(?:(?:ok|okay|oh|ah|hmm|well|actually|sorry|no|nope|wait|please)[,.!]?\s+)*"
_CANCEL_ARTIFACT = r"(?:visual(?:ization)?s?|diagrams?|charts?|mockups?|planning doc(?:ument)?s?|plan document|request)\b"
# Names the artifact, or says not to make it: a cancel in any clause of a message.
_CANCEL_REQUEST = re.compile(
    _CANCEL_LEAD + r"(?:(?:cancel|stop|skip|drop|scratch|never\s*mind|forget(?:\s+about)?)\s+(?:(?:the|this|that|my)\s+)?" + _CANCEL_ARTIFACT
    + r"|(?:don['’]?t|do not)\s+(?:make|create|show|draw|build|generate|render)\b(?:\s+(?:me|us|a|an|the|it|this|that))*"
    r"(?:\s+" + _CANCEL_ARTIFACT + r"|[.!?,;]*$)|no\s+(?:need\s+for\s+)?(?:(?:a|an|the)\s+)?" + _CANCEL_ARTIFACT + ")", re.I)
# Says only "stop": it cancels if nothing follows it, unlike "never mind, render it".
_CANCEL_BARE = re.compile(_CANCEL_LEAD + r"(?:never\s*mind|nvm|forget\s+(?:it|that|about\s+it)|scratch\s+that|"
                          r"cancel\s+(?:it|that)|don['’]?t(?:\s+bother)?|stop)(?:\s+(?:it|that|this))?[.!?,;]*$", re.I)
_COURTESY = re.compile(r"(?:thanks|thank you|thx|please|sorry)[.!?,;]*$", re.I)
_NOT = (r"(?<!why )\b(?:not|never|cannot|without|avoid|stop|no need to|rather than|instead of|"
        r"(?:do|does|did|should|must|need)n['’]?t|(?:ca|could|wo|would)n['’]?t(?! you))"
        r"(?: (?:ever|even|bother|need|needed|want|wanted|have|ask|asked|try|go|to|you|i|me|us|just|really|please|actually|yet))*")
# What comes just before a quote and takes it back: "don't ever <quote>", or an opening quotation mark.
_NEGATED = re.compile(_NOT + r" $|[\"`“‘«]$|(?:^| )'$")
_NEGATES = re.compile(_NOT + r" \w")    # the quote carries its own "don't"
_IDEOGRAPH = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")


def _cancels(text: str) -> bool:
    """Whether the person is calling off a visual, in whatever words."""
    clauses = [c for c in re.split(r"(?<=[.!?,;])\s+|\n+", text.strip()) if c and not _COURTESY.fullmatch(c)]
    return any(_CANCEL_REQUEST.match(c) for c in clauses) or bool(clauses and _CANCEL_BARE.match(clauses[-1]))


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
                match["visualize"] is not None and _OBJECT.fullmatch(rest) is not None)
            found.append((scope, clear and not _DEFERRED.search(sentence)))
        elif _SHOW_VISUALLY.fullmatch(sentence):
            found.append(("visual", not _DEFERRED.search(sentence)))
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
                 fold_only: bool = False, typed: bool = False, cli_prompt: bool = False) -> None:
    """Called only for authenticated person input, never for agent messages.

    ``typed``: the app watched the person key in all of ``text`` themselves.
    Only then can its words back a publish_visual ``asked``. ``cli_prompt``:
    this is a CLI hook's report of ``text``, which makes words the app staged
    for this same prompt quotable."""
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
            _unlink(c, root.id, "request")
        # A tool approval or ordinary follow-up does not cancel the person's
        # still unused request. Only a new artifact request or explicit stop does.
        cancelled = not fold_only and _cancels(text)
        if not fold_only and (scope or cancelled):
            c.execute("DELETE FROM visual_grants WHERE root_id=?", (root.id,))
            _unlink(c, root.id, "grant")
        if cancelled:
            c.execute("DELETE FROM visual_person_words WHERE root_id=?", (root.id,))
        elif typed and not fold_only:
            _keep_words(c, root.id, text)
        elif cli_prompt:
            c.execute("UPDATE visual_person_words SET staged=0 WHERE id=(SELECT MAX(id) FROM visual_person_words "
                      "WHERE root_id=? AND words=? AND staged=1)", (root.id, _words(text)))
        # Tie the words to what this message raises, so one spends the other.
        if not fold_only and scope and clear:
            _grant(c, root.id, scope)
            _link(c, root.id, text, "grant")
        elif not fold_only and scope:
            c.execute("INSERT INTO visual_requests VALUES (?,?,?,?)",
                      (root.id, "r_" + secrets.token_hex(6), scope, time.time()))
            _link(c, root.id, text, "request")
    bus.publish(db, "visual.offer_resolved", root_id=root.id)


def keep_typed(db: DB, agent: Agent, text: str, expires_at: float, submitted_at: float) -> None:
    """The app watched the person key in ``text``, for a CLI whose prompt hook
    can't say what was typed. Sent before the app lets Enter through: a report
    that fails, comes late or predates newer person input leaves nothing. What
    it leaves is staged: only that prompt's own hook makes the words quotable,
    in the transaction that raises any grant, so a message can't be spent twice."""
    if not visuals.enabled():
        return
    root, _ = visuals._session(db, agent)
    if root.id != agent.id:
        return
    with db.tx() as c:
        if time.time() >= expires_at:
            raise ServiceError("stale", "this input observation expired", 409)
        state = c.execute("SELECT invalidated_at FROM visual_input_state WHERE root_id=?", (root.id,)).fetchone()
        if state and submitted_at <= state["invalidated_at"]:
            raise ServiceError("stale", "this submission predates a direct person message", 409)
        if not _cancels(text):
            _keep_words(c, root.id, text, staged=True)


def purge_words(db: DB) -> None:
    """Drop typed messages past their window, whether or not anyone types again."""
    cutoff = time.time() - FOLD
    if db.conn.execute("SELECT 1 FROM visual_person_words WHERE created_at<=? LIMIT 1", (cutoff,)).fetchone():
        with db.tx() as c:
            c.execute("DELETE FROM visual_person_words WHERE created_at<=?", (cutoff,))


def _words(text: str) -> str:
    """Differences of case and whitespace only: everything else has to match."""
    return " ".join(text.split()).casefold()


def _word_count(words: str) -> int:
    """Real words only: punctuation isn't one, and each ideograph is (those scripts use no spaces)."""
    return sum(len(_IDEOGRAPH.findall(w)) or any(ch.isalnum() for ch in w) for w in words.split())


def _keep_words(c, root: str, text: str, staged: bool = False) -> None:
    """Keep a typed message for ``asked`` to be checked against, briefly."""
    now = time.time()
    words = _words(text)
    if (_word_count(words) >= MIN_QUOTE_WORDS and len(words) <= MAX_KEPT_CHARS
            and not visuals.secret_finding(text, "the message")):
        c.execute("INSERT INTO visual_person_words (root_id, words, staged, created_at) VALUES (?,?,?,?)",
                  (root, words, int(staged), now))
    c.execute("DELETE FROM visual_person_words WHERE created_at<=?", (now - FOLD,))
    c.execute("DELETE FROM visual_person_words WHERE root_id=? AND id NOT IN "
              "(SELECT id FROM visual_person_words WHERE root_id=? ORDER BY id DESC LIMIT ?)",
              (root, root, MAX_KEPT_MESSAGES))


def _spend_quote(c, root: str, asked: str, now: float) -> bool:
    """Whether ``asked`` is the person's recent words; the message backing it is used up."""
    quote = _words(asked)
    if _word_count(quote) < MIN_QUOTE_WORDS or _NEGATES.search(quote):
        return False
    # Whole words, and not a piece cut from "don't render it".
    pattern = re.compile(rf"(?<!\w){re.escape(quote)}(?!\w)")
    for row in c.execute("SELECT id, words, source FROM visual_person_words WHERE root_id=? AND staged=0 "
                         "AND created_at>? ORDER BY id DESC", (root, now - FOLD)).fetchall():
        if any(not _NEGATED.search(row["words"][:m.start()]) for m in pattern.finditer(row["words"])):
            c.execute("DELETE FROM visual_person_words WHERE id=?", (row["id"],))
            if row["source"] == "grant":
                c.execute("DELETE FROM visual_grants WHERE root_id=?", (root,))
            elif row["source"] == "request":
                c.execute("DELETE FROM visual_requests WHERE root_id=?", (root,))
            return True
    return False


def authorize(c, root: str, kind: str, *, supervisor: bool, asked: str | None = None,
              answered: str | None = None) -> None:
    """Allow one publication or refuse it, in the publishing transaction.

    ``answered`` is the stored answer of the visual being revised. Revising
    clears it, so an answer allows one revision. That and ``asked`` are the
    supervisor's own to use, and neither reaches a planning document."""
    now = time.time()
    if kind == "visual" and supervisor:
        if answered and now - json.loads(answered)["at"] < FOLD:
            return
        if asked is not None and _spend_quote(c, root, asked, now):
            return
    consume(c, root, kind)


def consume(c, root: str, kind: str = "visual") -> None:
    changed = c.execute("UPDATE visual_grants SET remaining=remaining-1 WHERE root_id=? AND remaining>0 "
                        "AND scope=?", (root, kind)).rowcount
    if not changed:
        if kind == "plan":
            raise ServiceError("visual_request_required", "a planning doc needs the person to ask for one, "
                               "or to accept an offer", 403)
        raise ServiceError("visual_request_required",
                           "nothing shows the person asked for this. If they asked to see it, the supervisor "
                           "publishes again with asked=\"...\": at least two of their words, copied exactly from "
                           "one message they typed themselves in the app in the last 30 minutes (not pasted, not "
                           "words they were quoting or saying not to do, and not a message that already allowed a "
                           "publication). Otherwise use offer_visual so they "
                           "can accept an offer, or ask them in text. A worker needs its supervisor to do this "
                           "first.", 403)
    c.execute("DELETE FROM visual_person_words WHERE root_id=? AND source='grant'", (root,))
