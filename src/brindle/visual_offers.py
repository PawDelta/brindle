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
MAX_KEPT_CHARS = 32000  # the most the app's input route accepts: a long message can end with an ask


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
        row = c.execute("SELECT * FROM visual_offers WHERE id=? AND root_id=? AND state='open'",
                        (oid, root)).fetchone()
        if row is None or config.visuals_setting() != "on":
            raise ServiceError("stale", "this offer is no longer open", 409)
        c.execute("UPDATE visual_offers SET state=?, changed_at=? WHERE id=?",
                  ("accepted" if accept else "dismissed", time.time(), oid))
        if accept:
            _grant(c, root)
            # Queue in the same transaction as the grant: no double delivery on repeated clicks.
            c.execute("INSERT INTO inbox (agent_id, sender_id, body, created_at, held) VALUES (?,NULL,?,?,0)",
                      (root, f"The person asked for a visual of: {row['question']}", time.time()))
    if accept:
        agents.flush(db, root)
    log.debug("visual offer %s", "accepted" if accept else "dismissed")
    debuglog.record("visuals", "debug", "accept" if accept else "dismiss", {"count": 1})
    bus.publish_agent(db, "visual.offer_resolved", root, offer=oid)
    return {"accepted": accept}


def _unlink(c, root: str, source: str) -> None:
    """The grant a typed message raised is gone; the message itself isn't spent."""
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
    r"(?:visualize\b|(?:(?:make|create|show|draw|build|generate|render)\s+(?:(?:me|us)\s+)?|"
    r"(?:I want|I'd like|I would like)\s+)"
    r"(?:(?:a|an|the|this|that)\s+)?(?:(?:simple|interactive|quick|small|new)\s+)*"
    r"(?P<artifact>visual(?:ization)?|diagram|chart|mockup|planning doc(?:ument)?|plan document)\b)", re.I)
# "show me the flow visually": a plain object phrase, no joining word that could add a second instruction.
_SHOW_VISUALLY = re.compile(r"(?:(?:can|could|would|will) you\s+)?(?:please\s+)?show\s+(?:me|us)"
                            r"(?:\s+(?!(?:and|then|also|but|so|while|or|plus)\b)[\w'.,/-]+){1,20}\s+visually[.!?]*", re.I)
_CANCEL_LEAD = r"(?:(?:ok|okay|oh|ah|hmm|well|actually|sorry|no|nope|wait|please)\W+)*"
_CANCEL_ARTIFACT = r"(?:visual(?:ization)?s?|diagrams?|charts?|mockups?|planning doc(?:ument)?s?|plan document|request)\b"
# Names the artifact, or says not to make it: a cancel in any clause of a message.
_CANCEL_REQUEST = re.compile(
    _CANCEL_LEAD + r"(?:(?:cancel|stop|skip|drop|scratch|never\s*mind|forget(?:\s+about)?)\s+(?:(?:the|this|that|my)\s+)?" + _CANCEL_ARTIFACT
    + r"|(?:don['’]?t|do not)\s+(?:make|create|show|draw|build|generate|render)\b(?:\s+(?:me|us|a|an|the|it|this|that))*"
    r"(?:\s+" + _CANCEL_ARTIFACT + r"|\W*$)|no\s+(?:need\s+for\s+)?(?:(?:a|an|the)\s+)?" + _CANCEL_ARTIFACT + ")", re.I)
# Says only "stop", or "never mind I found it": it cancels unless a new ask follows, as in "never mind, render it".
_CANCEL_BARE = re.compile(_CANCEL_LEAD + r"(?:(?:never\s*mind|nvm)\s+(?:i|i['’]m|i['’]ve|we|we['’]re|it['’]s|its|got|found|figured|all)\b"
                          r"|(?:never\s*mind|nvm|forget\s+(?:it|that|about\s+it)|scratch\s+that|"
                          r"cancel\s+(?:it|that)|don['’]?t(?:\s+bother)?|stop)(?:\s+(?:it|that|this))?\W*$)", re.I)
_ASKS_AGAIN = re.compile(r"(?:(?:please|just|then|instead|but|and|so|ok|okay|now|(?:can|could|would|will) you)\W+)*"
                         r"(?:render|draw|visuali[sz]e|mock|display|show\s+(?:me|us|it|that|this)|(?:can|could) i see|let me see"
                         r"|(?:make|create|build|generate)\s+(?:(?:me|us|a|an|the|this|that)\s+)*" + _CANCEL_ARTIFACT + r")\b", re.I)
_COURTESY = re.compile(r"(?:thanks|thank you|thx|please|sorry)\W*$", re.I)
_CLAUSES = re.compile(r"(?<=[.!?,;:])\s+|[…。！？，；、]+\s*|\s+[-–—]+\s+|[–—]+\s*|\n+")
# "should you not…" / "shouldn't you…" ask why something hasn't been done: a request, not a negation.
_ASKING = "".join(f"(?<!{a} you )" for a in ("should", "can", "could", "would", "will"))
_NOT = (r"(?<!why )\b(?:" + _ASKING + r"not|never|cannot|without|avoid|stop|no need to|rather than|instead of|"
        r"(?:do|does|did|must|need)n['’]?t|(?:should|ca|could|wo|would)n['’]?t(?! you))"
        r"(?: (?:ever|even|bother|need|needed|want|wanted|have|ask|asked|try|go|to|you|i|me|us|just|really|please|actually|yet))*")
_NEGATED = re.compile(_NOT + " $")     # just before a quote it takes the quote back: "don't ever <quote>"
_NEGATES = re.compile(_NOT + r" \w")    # the quote carries its own "don't"
_NEGATION_REACH = 200                   # characters before a quote that can negate it
# Words someone is mentioning, not saying. An unclosed mark runs to the end; ' inside a word is an apostrophe.
_QUOTED = re.compile("|".join(f"{o}[^{c}]*(?:{c}|$)" for o, c in ('""', "``", "“”", "«»", "「」", "『』", "„“"))
                     + r"|‘(?:[^’]|’(?=\w))*(?:’(?!\w)|$)"
                     + r"|(?<!\w)'(?:[^']|'(?=\w))*'(?!\w)"
                     + r"|(?<!\w)'(?!\d|(?:em|n|cause|tis|twas)\b)(?:[^']|'(?=\w))*$")
_ASK_LEAD = (r"(?:(?:please|kindly|just|then|now|ok|okay|yes|so|and|maybe|perhaps|hey|go ahead and|how about|what about|why not|"
             r"why don['’]t you|(?:can|could|would|will) you|would you mind|do you mind|mind|is it possible to|are you able to|"
             r"i(?:['’]d| would)? (?:want|like|love|need)(?: you)? to|let['’]?s)\W+)*")
# How a quote that holds a quoted name has to open: as a direct request, however polite. Anything else
# ("he said", "the render log contains") is refused; the agent can still quote the person's plain words.
_ASKS_FOR = re.compile(_ASK_LEAD + r"(?:(?:(?:render|draw|sketch|show|mock|display|preview|build|plot|chart|diagram)(?:ing)?|"
                       r"mak(?:e|ing)(?! sure)|creat(?:e|ing)|generat(?:e|ing)|visuali[sz](?:e|ing)|illustrat(?:e|ing))\b"
                       r"|(?:let (?:me|us)|let['’]?s|can i|could i|may i|i(?:['’]d| would)? (?:want|like|love) to)\W+(?:see|view)\b)", re.I)
_IDEOGRAPH = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")


def _cancels(text: str) -> bool:
    """Whether the person is calling off a visual, in whatever words."""
    clauses = [c for c in _CLAUSES.split(text.strip()) if c.strip() and not _COURTESY.fullmatch(c)]
    if any(_CANCEL_REQUEST.match(c) for c in clauses):
        return True
    bare = [i for i, c in enumerate(clauses) if _CANCEL_BARE.match(c)]
    return bool(bare) and not any(_ASKS_AGAIN.match(c) for c in clauses[bare[-1] + 1:])


def request_intent(text: str) -> str | None:
    """What a sentence of the person's own message asks for ("visual" or "plan"), if one does.

    A recognized request authorizes one, whatever surrounds it: there is no
    second confirmation. Whether they really meant it now is the agent's to
    judge; an explicit cancel (``_cancels``) still takes it back.
    """
    scope = None
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text.strip()):
        match = _REQUEST.match(sentence)
        if match:
            scope = "plan" if (match["artifact"] or "").lower().startswith(("planning", "plan ")) else "visual"
        elif _SHOW_VISUALLY.fullmatch(sentence):
            scope = "visual"
    return scope


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
                 fold_only: bool = False, typed: bool = False, cli_prompt: bool = False,
                 said_at: float | None = None) -> None:
    """Called only for authenticated person input, never for agent messages.

    ``typed``: the app watched the person key in all of ``text`` themselves.
    Only then can its words back a publish_visual ``asked``. ``cli_prompt``:
    this is a CLI hook's report of ``text``, which makes words the app staged
    for this same prompt quotable. ``said_at``: when the person sent ``text``,
    if that was earlier than now (a message read back after the fact): a cancel
    then ends only the words they typed before it."""
    if not visuals.enabled():
        return
    root, ws = visuals._session(db, agent)
    if root.id != agent.id:
        return
    scope = request_intent(text)
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
        # A tool approval or ordinary follow-up does not cancel the person's
        # still unused request. Only a new artifact request or explicit stop does.
        cancelled = not fold_only and _cancels(text)
        if not fold_only and (scope or cancelled):
            c.execute("DELETE FROM visual_grants WHERE root_id=?", (root.id,))
            _unlink(c, root.id, "grant")
        if cancelled:
            c.execute("DELETE FROM visual_person_words WHERE root_id=? AND created_at<=?",
                      (root.id, time.time() if said_at is None else min(said_at, time.time())))
        elif typed and not fold_only:
            _keep_words(c, root.id, text)
        elif cli_prompt:
            c.execute("UPDATE visual_person_words SET staged=0 WHERE id=(SELECT MAX(id) FROM visual_person_words "
                      "WHERE root_id=? AND words=? AND staged=1)", (root.id, _words(text)))
        # Tie the words to what this message raises, so one spends the other.
        # A cancel in the same message wins over the request it calls off.
        if not fold_only and scope and not cancelled:
            _grant(c, root.id, scope)
            _link(c, root.id, text, "grant")
    bus.publish(db, "visual.offer_resolved", repo_root=ws.repo_root, root_id=root.id)


def keep_typed(db: DB, agent: Agent, text: str, expires_at: float, submitted_at: float) -> None:
    """The app watched the person key in ``text``, for a CLI whose prompt hook
    can't say what was typed. Sent before the app lets Enter through: a report
    that fails, comes late or predates newer person input leaves nothing. What
    it leaves is staged: only that prompt's own hook makes the words quotable,
    in the transaction that raises any grant, so a message can't be spent twice."""
    # Each refusal goes to the debug log (why and how late, never the words), so a real one can be seen.
    if not visuals.enabled():
        return _typed_refused(agent, "visuals are off")
    root, _ = visuals._session(db, agent)
    if root.id != agent.id:
        return _typed_refused(agent, "not a session's root")
    now = time.time()
    try:
        with db.tx() as c:
            now = time.time()
            if now >= expires_at:
                raise ServiceError("stale", "this input observation expired", 409)
            state = c.execute("SELECT invalidated_at FROM visual_input_state WHERE root_id=?", (root.id,)).fetchone()
            if state and submitted_at <= state["invalidated_at"]:
                raise ServiceError("stale", "this submission predates a direct person message", 409)
            if _cancels(text):
                kept = "it cancels a request"
            else:
                kept = _keep_words(c, root.id, text, staged=True)
    except ServiceError as e:
        _typed_refused(agent, e.message, late_by=round(now - submitted_at, 3))
        raise
    if kept is not None:
        _typed_refused(agent, kept)


def _typed_refused(agent: Agent, why: str, **detail) -> None:
    debuglog.record("visuals", "warning", f"typed words refused: {why}", {"agent": agent.id, **detail})


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


def _keep_words(c, root: str, text: str, staged: bool = False) -> str | None:
    """Keep a typed message for ``asked`` to be checked against, briefly. Why it wasn't kept, if it wasn't."""
    now = time.time()
    words = _words(text)
    unkept = ("too few words" if _word_count(words) < MIN_QUOTE_WORDS else "too long" if len(words) > MAX_KEPT_CHARS
              else "it looks like a secret" if visuals.secret_finding(text, "the message") else None)
    if unkept is None:
        c.execute("INSERT INTO visual_person_words (root_id, words, staged, created_at) VALUES (?,?,?,?)",
                  (root, words, int(staged), now))
    c.execute("DELETE FROM visual_person_words WHERE created_at<=?", (now - FOLD,))
    c.execute("DELETE FROM visual_person_words WHERE root_id=? AND id NOT IN "
              "(SELECT id FROM visual_person_words WHERE root_id=? ORDER BY id DESC LIMIT ?)",
              (root, root, MAX_KEPT_MESSAGES))
    return unkept


def _says_it_themselves(words: str, span: tuple[int, int], mentioned: list[tuple[int, int]]) -> bool:
    """A quote may hold a whole quoted name if it opens as a request, but not begin
    or end inside one, and enough of it must be the person's own words outside the marks."""
    begin, end = span
    own, at = [], begin
    for start, stop in mentioned:
        if stop <= begin or start >= end:
            continue
        if start < begin or stop > end:
            return False
        own.append(words[at:start])
        at = stop
    holds_a_mention = bool(own)
    own.append(words[at:end])
    return _word_count(" ".join(own)) >= MIN_QUOTE_WORDS and (not holds_a_mention or _ASKS_FOR.match(own[0]) is not None)


def _spend_quote(c, root: str, asked: str, now: float) -> bool:
    """Whether ``asked`` is the person's recent words; the message backing it is used up."""
    quote = _words(asked)
    if _word_count(quote) < MIN_QUOTE_WORDS or _NEGATES.search(quote):
        return False
    # Whole words, and not a piece cut from "don't render it".
    pattern = re.compile(rf"(?<!\w){re.escape(quote)}(?!\w)")
    for row in c.execute("SELECT id, words, source FROM visual_person_words WHERE root_id=? AND staged=0 "
                         "AND created_at>? ORDER BY id DESC", (root, now - FOLD)).fetchall():
        words = row["words"]
        mentioned = [q.span() for q in _QUOTED.finditer(words)]
        if any(not _NEGATED.search(words[max(0, m.start() - _NEGATION_REACH):m.start()])
               and _says_it_themselves(words, m.span(), mentioned)
               for m in pattern.finditer(words)):
            c.execute("DELETE FROM visual_person_words WHERE id=?", (row["id"],))
            if row["source"] == "grant":
                c.execute("DELETE FROM visual_grants WHERE root_id=?", (root,))
            return True
    if c.execute("SELECT 1 FROM visual_person_words WHERE root_id=? AND staged=1 AND created_at>? AND instr(words, ?)",
                 (root, now - FOLD, quote)).fetchone():
        # The app vouched for these words, but no prompt of the CLI's has said it took that message in.
        debuglog.record("visuals", "warning", "asked words refused: their message is still staged", {"agent": root})
    return False


def authorize(c, root: str, kind: str, *, supervisor: bool, asked: str | None = None,
              answered: str | None = None, consented: bool = False) -> bool:
    """Allow one publication or refuse it, in the publishing transaction.
    Whether the person consented to this one (a quote or a grant).

    ``answered`` is the stored answer of the visual being revised. Revising
    clears it, so an answer allows one revision. ``consented``: the visual
    being revised was published by this supervisor with the person's
    consent, so it stays theirs to update. These and ``asked`` are the
    supervisor's own to use, and none reaches a planning document."""
    now = time.time()
    if kind == "visual" and supervisor:
        if consented:
            return True
        if answered and now - json.loads(answered)["at"] < FOLD:
            return False
        if asked is not None and _spend_quote(c, root, asked, now):
            return True
    consume(c, root, kind)
    return True


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
