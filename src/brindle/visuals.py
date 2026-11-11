"""Visuals: HTML pages an agent publishes into its session for the person to
read, and optionally answer with one of a fixed set of options.

Rows live in the ``visuals`` table, keyed by the session (its root agent);
each revision's page is ``<repo>/.brindle/visuals/<session>/<id>/r<n>.html``.
A worker publishes into its supervisor's session. The person's answer is
bound to the revision on screen: an answer to an older revision is refused,
never read against a newer revision's options. Only the option id and the
person's own note go back to the supervisor, as a message from the person.

Keep copies the page to ``<repo>/.brindle/kept/visuals/<id>.html`` and a
``kept_visuals`` row, which outlive the session. Removing the session
(its workspace row going, ``DB.delete_workspace``) deletes the rest.

The whole feature is hidden behind :func:`enabled` (the engine's visuals
capability) until it ships: tools and routes refuse, the Inbox shows nothing."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from brindle import bus, providers, redact
from brindle.db import DB, Agent, Workspace
from brindle.service.errors import ServiceError

MAX_BYTES = 2 * 1024 * 1024     # one revision's page
MAX_REVISIONS = 20
MAX_OPTIONS = 12
MAX_TITLE = 120
MAX_QUESTION = 500
MAX_LABEL = 120
MAX_DETAIL = 500
MAX_NOTE = 2000

ID = re.compile(r"v_[0-9a-f]{12}")
OPTION_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SEGMENT = re.compile(r"[A-Za-z0-9_.:-]+")

# What the daemon serves a page with: no network at all, inline script and
# style only, data:/blob: images and fonts. ``sandbox`` makes the document
# an opaque origin even if something loads it outside the app's frame.
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src data: blob:; font-src data:; media-src data: blob:; base-uri 'none'; "
       "form-action 'none'; sandbox allow-scripts")
PAGE_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}


def enabled() -> bool:
    """The visuals capability, read at call time: the ``visuals_enabled`` person
    setting (on by default); BRINDLE_VISUALS=0 overrides it off."""
    from brindle import config

    if os.environ.get("BRINDLE_VISUALS", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return config.visuals_enabled_setting()


def require_enabled() -> None:
    if not enabled():
        raise ServiceError("not_enabled", "visuals aren't enabled in this brindle", 404)


# -- where things live -------------------------------------------------------------


def _private_dir(path: Path) -> Path:
    """``path``, made, with a ``.gitignore`` of ``*`` in it: ``.brindle/`` is
    committed in a repo, so its visuals must ignore themselves."""
    path.mkdir(parents=True, exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        _write_atomic(ignore, b"*\n")
    return path


def store_root(repo_root: str) -> Path:
    return Path(repo_root) / ".brindle" / "visuals"


def kept_root(repo_root: str) -> Path:
    return Path(repo_root) / ".brindle" / "kept" / "visuals"


def _segment(value: str) -> str:
    if not _SEGMENT.fullmatch(value or "") or value in (".", ".."):
        raise ServiceError("invalid", "not a session id", 400)
    return value


def page_path(repo_root: str, root_id: str, visual_id: str, rev: int) -> Path:
    return store_root(repo_root) / _segment(root_id) / _segment(visual_id) / f"r{int(rev)}.html"


def kept_path(repo_root: str, kept_id: str) -> Path:
    return kept_root(repo_root) / f"{_segment(kept_id)}.html"


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a unique temporary file beside it,
    so a failed write never leaves a half page in its place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# -- checks --------------------------------------------------------------------------


def secret_finding(text: str, where: str) -> str | None:
    """Where in ``text`` something looks like a secret (redact's scan), as
    ``"line N of <where>"``; never the value itself."""
    if not text or redact.mask(text) == text:
        return None
    for n, line in enumerate(text.splitlines() or [text], 1):
        if redact.mask(line) != line:
            return f"line {n} of {where}"
    return where      # a secret that spans lines


def _refuse_secrets(fields: list[tuple[str, str | None]]) -> None:
    for where, text in fields:
        found = secret_finding(text or "", where)
        if found:
            raise ServiceError("secret", f"refused: {found} looks like a secret (a token, key or "
                               "password). Take it out and publish again.", 400)


def _text(value: Any, name: str, limit: int, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ServiceError("invalid", f"{name} is required", 400)
        return None
    if not isinstance(value, str):
        raise ServiceError("invalid", f"{name} must be text", 400)
    value = value.strip()
    if len(value) > limit:
        raise ServiceError("invalid", f"{name} is longer than {limit} characters", 400)
    return value


def _options(raw: Any) -> list[dict] | None:
    if raw is None or raw == []:
        return None
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_OPTIONS:
        raise ServiceError("invalid", f"options must be a list of 2 to {MAX_OPTIONS} choices", 400)
    out, seen = [], set()
    for o in raw:
        if not isinstance(o, dict):
            raise ServiceError("invalid", "each option is {id, label, detail?}", 400)
        oid = o.get("id")
        if not isinstance(oid, str) or not OPTION_ID.fullmatch(oid) or oid in seen:
            raise ServiceError("invalid", "each option needs its own id of 1 to 32 letters, digits, "
                               "_ or -", 400)
        seen.add(oid)
        out.append({"id": oid, "label": _text(o.get("label"), "an option's label", MAX_LABEL, True),
                    "detail": _text(o.get("detail"), "an option's detail", MAX_DETAIL),
                    **({"approves": True} if o.get("approves") is True else {})})
    return out


# -- rows -----------------------------------------------------------------------------


def _decode(row) -> dict:
    v = dict(row)
    v["options"] = json.loads(v["options"]) if v["options"] else None
    v["answer"] = json.loads(v["answer"]) if v["answer"] else None
    return v


def get(db: DB, visual_id: str) -> dict:
    row = None
    if isinstance(visual_id, str) and ID.fullmatch(visual_id):
        row = db.conn.execute("SELECT * FROM visuals WHERE id=?", (visual_id,)).fetchone()
    if row is None:
        raise ServiceError("not_found", "no such visual", 404)
    return _decode(row)


def _unlocked(root_id: str) -> None:
    from brindle import identity_lock

    who = identity_lock.locked_by(root_id)
    if who:
        raise ServiceError("locked", identity_lock.message(who), 403)


def for_session(db: DB, root_id: str) -> list[dict]:
    """A session's visuals, newest first."""
    rows = db.conn.execute("SELECT * FROM visuals WHERE root_id=? ORDER BY created_at DESC, id DESC",
                           (root_id,)).fetchall()
    return [_decode(r) for r in rows]


def to_dto(db: DB, v: dict) -> dict:
    from brindle.service.dto import VisualDTO

    answer = v["answer"]
    receiver = db.get_agent(v["root_id"])
    provider = providers.PROVIDERS.get(receiver.provider, providers.Provider()) if receiver else providers.Provider()
    return VisualDTO(
        id=v["id"], session=v["root_id"], title=v["title"], by=v["by_agent"],
        kind="plan" if v["kind"] == "plan" else "visual",   # an older finished-doc page reads as a visual
        rev=v["rev"],
        answer_agent_id=v["root_id"], accepts_files=provider.accepts_files,
        question=v["question"], options=v["options"],
        answer={k: answer[k] for k in ("option", "note", "at", "attachments") if k in answer} if answer else None,
        kept=v["kept_at"] is not None, unread=v["read_rev"] < v["rev"], created_at=v["created_at"],
    ).to_json()


def _session(db: DB, caller: Agent | None) -> tuple[Agent, Workspace]:
    from brindle import autopilot

    root = db.get_agent(autopilot.root_of(db, caller.id)) if caller else None
    ws = db.get_workspace(root.workspace_id) if root else None
    if root is None or ws is None:
        raise ServiceError("invalid", "a visual needs a brindle session to publish into: run this "
                           "from a brindle agent", 400)
    return root, ws


def publish(db: DB, caller: Agent | None, title: str, html: str, question: str | None = None,
            options: list[dict] | None = None, visual_id: str | None = None, kind: str = "visual",
            extra: Callable[[Any, str, int], None] | None = None, asked: str | None = None) -> dict:
    """Publish a new visual into ``caller``'s session, or (``visual_id``) a
    new revision of one of that session's visuals. A new revision is a new
    question: an answer to an earlier one is cleared. ``extra(c, id, rev)``
    writes what goes with the row (a plan's own row) in its transaction.
    ``asked``: the person's own words asking to see it (visual_offers.authorize)."""
    require_enabled()
    root, ws = _session(db, caller)
    title = _text(title, "title", MAX_TITLE, required=True)
    question = _text(question, "question", MAX_QUESTION)
    asked = _text(asked, "asked", MAX_QUESTION)
    options = _options(options)
    if options and not question:
        raise ServiceError("invalid", "options need the question they answer", 400)
    if not isinstance(html, str) or not html.strip():
        raise ServiceError("invalid", "html is required", 400)
    data = html.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ServiceError("too_big", f"the page is {len(data) // 1024} KB; a visual may be at most "
                           f"{MAX_BYTES // (1024 * 1024)} MB", 400)
    _refuse_secrets([("the page", html), ("the title", title), ("the question", question),
                     *((f"option {o['id']}", f"{o['label']}\n{o['detail'] or ''}") for o in options or [])])

    _private_dir(store_root(ws.repo_root))
    if any(o.get("approves") for o in options or []):
        # An approving option belongs to the work in progress when it is shown
        # (None: the session's next goal claims it, see claim_approvals).
        ap = db.get_autopilot(root.id)
        options = [{**o, "goal": ap.goal if ap and ap.goal else None} if o.get("approves") else o
                   for o in options]
    opts = json.dumps(options) if options else None
    # The revision is allocated, its page installed and its row written under
    # one write transaction (BEGIN IMMEDIATE serializes writers across
    # processes), so two publishers never pick the same r<n>. A failure
    # removes only the page this call installed, before the lock is let go.
    with db.tx() as c:
        now = time.time()
        from brindle import visual_offers

        row = c.execute("SELECT root_id, rev, kind, answer, consented_by FROM visuals WHERE id=?",
                        (visual_id,)).fetchone() \
            if isinstance(visual_id, str) and ID.fullmatch(visual_id) else None
        supervisor = caller.id == root.id
        same = row is not None and row["root_id"] == root.id and row["kind"] == kind
        consented = visual_offers.authorize(
            c, root.id, kind, supervisor=supervisor, asked=asked,
            answered=row["answer"] if same else None,
            consented=same and supervisor and row["consented_by"] == caller.id)
        # Only the session's supervisor earns a visual it may keep updating.
        consented_by = caller.id if consented and supervisor and kind == "visual" else None
        if visual_id is None:
            vid, rev = "v_" + secrets.token_hex(6), 1
        else:
            if row is None or row["root_id"] != root.id or row["kind"] != kind:
                raise ServiceError("not_found", "no such visual in this session", 404)
            vid, rev = visual_id, row["rev"] + 1
            if rev > MAX_REVISIONS:
                raise ServiceError("too_many_revisions", f"a visual may have at most {MAX_REVISIONS} "
                                   "revisions; publish a new one", 400)
        path = page_path(ws.repo_root, root.id, vid, rev)
        _write_atomic(path, data)
        try:
            if rev == 1:
                c.execute("INSERT INTO visuals (id, root_id, repo_root, by_agent, kind, title, rev, question,"
                          " options, consented_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (vid, root.id, ws.repo_root, caller.id, kind, title, rev, question, opts,
                           consented_by, now, now))
            else:
                c.execute("UPDATE visuals SET title=?, rev=?, question=?, options=?, answer=NULL,"
                          " by_agent=?, consented_by=COALESCE(consented_by, ?), updated_at=? WHERE id=?",
                          (title, rev, question, opts, caller.id, consented_by, now, vid))
            if extra is not None:
                extra(c, vid, rev)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    bus.publish(db, "visual.published", repo_root=ws.repo_root, root_id=root.id, agent_id=caller.id,
                visual=vid, rev=rev)
    return get(db, vid)


def mark_read(db: DB, visual_id: str, rev: int | None = None) -> dict:
    """Mark read up to ``rev``, the revision on the person's screen: one
    published since keeps its notice. None (an older app): the current one."""
    v = get(db, visual_id)
    _unlocked(v["root_id"])
    if rev is not None and rev < 1:
        raise ServiceError("invalid", "rev must be a revision of this visual", 400)
    with db.tx() as c:
        c.execute("UPDATE visuals SET read_rev=MAX(read_rev, MIN(rev, COALESCE(?, rev))) WHERE id=?", (rev, v["id"]))
    # Both entry points use this state: the Inbox card and the session's list.
    # Tell connected clients to refresh their Inbox after either opens the reader;
    # ``pending``: whether it still waits there (a question until answered, a
    # revision published since), so a client can drop the card at once when not.
    read = get(db, v["id"])
    pending = (read["answer"] is None and read["dismissed_rev"] < read["rev"]) if read["options"] \
        else read["read_rev"] < read["rev"]
    bus.publish(db, "visual.read", repo_root=v["repo_root"], root_id=v["root_id"], visual=v["id"], pending=pending)
    return read


def page(db: DB, visual_id: str, rev: Any) -> Path:
    """The file of one revision of a visual, for the daemon's page route.
    Only ids and revisions the store knows; nothing from the path is used
    to build the file name but those."""
    v = get(db, visual_id)
    _unlocked(v["root_id"])
    n = int(rev) if isinstance(rev, str) and rev.isdigit() else 0
    if not 1 <= n <= v["rev"]:
        raise ServiceError("not_found", "no such revision", 404)
    path = page_path(v["repo_root"], v["root_id"], v["id"], n)
    if not path.is_file():
        raise ServiceError("not_found", "that revision's page is gone", 404)
    return path


# -- answers --------------------------------------------------------------------------


def _answer_attachments(db: DB, root_id: str, attachments: Any) -> tuple[list[str], list[str]]:
    """Validate uploaded files before claiming an answer; store names, send CLI references."""
    if attachments is None:
        return [], []
    if not isinstance(attachments, list) or any(not isinstance(p, str) or not p for p in attachments):
        raise ServiceError("invalid", "attachments must be a list of file paths", 400)
    if not attachments:
        return [], []
    receiver = db.get_agent(root_id)
    workspace = db.get_workspace(receiver.workspace_id) if receiver else None
    provider = providers.PROVIDERS.get(receiver.provider, providers.Provider()) if receiver else providers.Provider()
    if not provider.accepts_files:
        raise ServiceError("invalid", "this agent's provider does not accept file attachments", 400)
    if workspace is None:
        raise ServiceError("invalid", "attachment receiver has no workspace", 400)
    # Do not resolve the attachments directory: if it is a symlink out of the
    # workspace, its target must not become an allowed root.
    folder = Path(workspace.path).resolve() / ".brindle" / "attachments"
    names, references = [], []
    for raw in attachments:
        try:
            path = Path(raw)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError
            path = path.resolve(strict=True)
            if not path.is_relative_to(folder) or not path.is_file():
                raise ValueError
        except (OSError, ValueError, RuntimeError):
            raise ServiceError("invalid", "attach a file inside the receiving agent's attachments folder", 400) from None
        names.append(path.name)
        references.append(provider.format_attachment(str(path)))
    return names, references


def answer(db: DB, visual_id: str, rev: Any, option: Any = None, note: Any = None,
           attachments: Any = None) -> dict:
    """The person's answer to the revision on their screen (``rev``). The
    answer is claimed in the row first, so a second answer (a double click,
    another window) is refused instead of delivered twice; if the message
    can't be delivered, the claim is undone and nothing is recorded."""
    from brindle import agents

    v = get(db, visual_id)
    _unlocked(v["root_id"])
    if not v["options"]:
        raise ServiceError("invalid", "this visual asks nothing to answer", 400)
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise ServiceError("invalid", "rev (the revision on screen) is required", 400)
    if rev != v["rev"]:
        raise ServiceError("stale", f"the visual has a newer revision (r{v['rev']}); answer that one", 409,
                           rev=v["rev"])
    note = _text(note, "note", MAX_NOTE)
    if option is None and not note:
        raise ServiceError("invalid", "an option or note is required", 400)
    if option is not None and option not in {o["id"] for o in v["options"]}:
        raise ServiceError("invalid", "not one of this revision's options", 400)
    # The note is stored, can be kept, and goes to the agent: never a secret.
    _refuse_secrets([("your note", note)])
    names, references = _answer_attachments(db, v["root_id"], attachments)
    at = time.time()
    data = {"option": option, "note": note, "at": at, "rev": rev}
    if names:
        data["attachments"] = names
    record = json.dumps(data)
    with db.tx() as c:
        cur = c.execute("UPDATE visuals SET answer=?, read_rev=MAX(read_rev, rev) "
                        "WHERE id=? AND rev=? AND answer IS NULL", (record, v["id"], rev))
        claimed = cur.rowcount == 1
    if not claimed:
        now = get(db, v["id"])
        if now["rev"] != rev:
            raise ServiceError("stale", f"the visual has a newer revision (r{now['rev']}); answer that one",
                               409, rev=now["rev"])
        raise ServiceError("answered", "this revision is already answered", 409)
    message = f"The person answered visual {v['id']} (r{rev})"
    message += f": option {option}." if option is not None else ":"
    if note:
        message += f"\nTheir note: {note}"
    if references:
        message += "\n" + "\n".join(references)
    try:
        agents.send_message(db, v["root_id"], message, person=True)
    except BaseException:
        with db.tx() as c:
            c.execute("UPDATE visuals SET answer=NULL WHERE id=? AND answer=?", (v["id"], record))
            # Keep may have copied the claim while delivery was in flight.
            c.execute("UPDATE kept_visuals SET answer=NULL WHERE id=? AND rev=? AND answer=?",
                      (v["id"], rev, record))
        raise
    with db.tx() as c:
        # Delivered: only now can the answer exempt a goal from planning (until then it may still roll back).
        c.execute("UPDATE visuals SET answer=json_set(answer, '$.delivered', 1) WHERE id=? AND answer=?",
                  (v["id"], record))
        # Only a kept copy of this same revision takes the answer: a copy of
        # an earlier revision keeps what it was saved with until Keep again.
        c.execute("UPDATE kept_visuals SET answer=? WHERE id=? AND rev=?", (record, v["id"], rev))
    settle_offer_for_approval(db, v["root_id"])
    bus.publish(db, "visual.answered", repo_root=v["repo_root"], root_id=v["root_id"], visual=v["id"],
                rev=rev, option=option)
    return get(db, v["id"])


def settle_offer_for_approval(db: DB, root_id: str) -> None:
    """A goal's planning offer opened while its approving answer was still
    being delivered is settled now and its queued tasks released. Only an
    unanswered offer: an accepted one, a plan or a review hold stays. Run
    after an answer is delivered and after an offer is opened, so whichever
    comes second settles it."""
    from brindle import plans, tasks

    ap = db.get_autopilot(root_id)
    if ap is None or not ap.goal or not ap.plan_offer or not ap.plan_offer.startswith(plans.OFFER_OPEN) \
            or not has_approved_design(db, root_id, ap.goal):
        return
    with db.tx() as c:
        settled = c.execute("UPDATE goal_doc_requests SET offer=? WHERE root_id=? AND offer=?",
                            (plans.OFFER_PLANNED, root_id, ap.plan_offer)).rowcount == 1
    if settled:
        bus.publish_agent(db, "autopilot.goal", root_id)
        tasks.on_resumed(db, root_id, why="the person's approval of the design")


def claim_approvals(db: DB, root_id: str, goal: str) -> None:
    """Bind the approving options published before the session had a goal
    (``publish`` leaves their goal unset) to ``goal``, the work that follows
    them. The goal lives on the option, never in the answer, so a claim can't
    disturb an answer being delivered or rolled back. An option already
    bound to another goal stays there."""
    with db.tx() as c:
        for row in c.execute("SELECT id, options FROM visuals WHERE root_id=? AND kind='visual' "
                             "AND options LIKE '%\"approves\"%'", (root_id,)).fetchall():
            options = json.loads(row["options"])
            if any(o.get("approves") and o.get("goal") is None for o in options):
                bound = [{**o, "goal": goal} if o.get("approves") and o.get("goal") is None else o
                         for o in options]
                c.execute("UPDATE visuals SET options=? WHERE id=?", (json.dumps(bound), row["id"]))


def has_approved_design(db: DB, root_id: str, goal: str) -> bool:
    """Whether the person approved a design for ``goal``: chose, on one of
    session ``root_id``'s visuals, an option its publisher marked ``approves``
    while that goal was the work in progress, with no note. A viewed visual,
    a redirect, any other choice, an option bound to another goal, and an
    answer still being delivered (it may roll back) aren't."""
    for row in db.conn.execute("SELECT options, answer FROM visuals WHERE root_id=? AND kind='visual' "
                               "AND answer IS NOT NULL AND options IS NOT NULL", (root_id,)):
        answer = json.loads(row["answer"])
        if answer.get("delivered") and not answer.get("note") and any(o["id"] == answer.get("option") and o.get("approves")
                                          and o.get("goal") == goal for o in json.loads(row["options"])):
            return True
    return False


def dismiss(db: DB, visual_id: str, rev: Any) -> dict:
    """The person doesn't want to answer the revision on their screen. It
    leaves the Inbox, stays viewable, and the agent is told so it stops
    waiting. Claimed in the row first, so a repeat, or a revision that was
    answered, changes nothing and tells the agent nothing. A newer revision
    asks again."""
    from brindle import agents

    v = get(db, visual_id)
    _unlocked(v["root_id"])
    if not v["options"]:
        raise ServiceError("invalid", "this visual asks nothing to dismiss", 400)
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise ServiceError("invalid", "rev (the revision on screen) is required", 400)
    if rev != v["rev"]:
        raise ServiceError("stale", f"the visual has a newer revision (r{v['rev']}); dismiss that one", 409,
                           rev=v["rev"])
    with db.tx() as c:
        cur = c.execute("UPDATE visuals SET dismissed_rev=rev, read_rev=MAX(read_rev, rev) "
                        "WHERE id=? AND rev=? AND answer IS NULL AND dismissed_rev < rev", (v["id"], rev))
        claimed = cur.rowcount == 1
    if not claimed:
        return get(db, v["id"])
    # The publisher is the one waiting; the session's supervisor hears too when it is another agent.
    text = (f"The person dismissed visual {v['id']} (r{rev}) without answering. "
            "Don't wait for an answer; carry on without one.")
    try:
        agents.send_message(db, v["by_agent"], text, person=True)
    except BaseException:
        with db.tx() as c:
            c.execute("UPDATE visuals SET dismissed_rev=0 WHERE id=? AND dismissed_rev=?", (v["id"], rev))
        raise
    if v["root_id"] != v["by_agent"]:
        # The dismissal is delivered once the publisher has it; a supervisor that can't be told
        # (paused, say) must not undo that or make a repeat tell the publisher again.
        try:
            agents.send_message(db, v["root_id"], text, person=True)
        except Exception:
            pass
    bus.publish(db, "visual.dismissed", repo_root=v["repo_root"], root_id=v["root_id"], visual=v["id"], rev=rev)
    return get(db, v["id"])


def inbox_pending(db: DB, repo_root: str | None) -> list[dict]:
    """Visuals needing attention, oldest first, for the Inbox.

    Questions stay until answered even after being read. A visual without
    answer options is a notice: each published revision stays until read.
    """
    if not enabled():
        return []
    sql = ("SELECT * FROM visuals WHERE ("
           "(options IS NOT NULL AND answer IS NULL AND dismissed_rev < rev) OR "
           "(options IS NULL AND read_rev < rev))")
    args = []
    if repo_root is not None:
        sql += " AND repo_root=?"
        args.append(repo_root)
    return [_decode(r) for r in db.conn.execute(sql + " ORDER BY created_at, id", args)]


# -- keeping --------------------------------------------------------------------------


def _session_name(db: DB, root_id: str) -> str:
    root = db.get_agent(root_id)
    ap = db.get_autopilot(root_id)
    return (root.name if root and root.name else None) or (ap.goal if ap and ap.goal else None) or root_id


def keep(db: DB, visual_id: str) -> dict:
    """Copy the visual's current revision to the kept store. Keeping a kept
    visual again keeps the revision now on screen."""
    # Read and copy under the same writer lock as answers, revisions and
    # Unkeep, so the page and row always describe the same saved state.
    with db.tx() as c:
        v = get(db, visual_id)
        _unlocked(v["root_id"])
        html = page(db, v["id"], str(v["rev"])).read_text(encoding="utf-8")
        # Everything the kept copy stores, scanned again before anything is written.
        # The session's name is resolved once, scanned, and that same value stored.
        name = _session_name(db, v["root_id"])
        _refuse_secrets([("the page", html), ("the title", v["title"]),
                         ("the answer's note", (v["answer"] or {}).get("note")), ("the session's name", name)])
        _private_dir(kept_root(v["repo_root"]).parent)
        _write_atomic(kept_path(v["repo_root"], v["id"]), html.encode("utf-8"))
        now = time.time()
        answer = json.dumps(v["answer"]) if v["answer"] else None
        c.execute("INSERT INTO kept_visuals (id, title, from_session, from_session_name, repo, rev, answer,"
                  " kept_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
                  " rev=excluded.rev, answer=excluded.answer",
                  (v["id"], v["title"], v["root_id"], name, v["repo_root"],
                   v["rev"], answer, now))
        c.execute("UPDATE visuals SET kept_at=COALESCE(kept_at, ?) WHERE id=?", (now, v["id"]))
    bus.publish(db, "visual.kept", repo_root=v["repo_root"], root_id=v["root_id"], visual=v["id"], kept=True)
    return get(db, v["id"])


def unkeep(db: DB, visual_id: str) -> dict:
    v = get(db, visual_id)
    _unlocked(v["root_id"])
    with db.tx() as c:
        c.execute("DELETE FROM kept_visuals WHERE id=?", (v["id"],))
        c.execute("UPDATE visuals SET kept_at=NULL WHERE id=?", (v["id"],))
        kept_path(v["repo_root"], v["id"]).unlink(missing_ok=True)
    bus.publish(db, "visual.kept", repo_root=v["repo_root"], root_id=v["root_id"], visual=v["id"], kept=False)
    return get(db, v["id"])


def kept(db: DB, repo: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM kept_visuals", []
    if repo is not None:
        sql += " WHERE repo=?"
        args.append(repo)
    return [dict(r) for r in db.conn.execute(sql + " ORDER BY kept_at DESC, id DESC", args)]


def get_kept(db: DB, kept_id: str) -> dict:
    row = None
    if isinstance(kept_id, str) and ID.fullmatch(kept_id):
        row = db.conn.execute("SELECT * FROM kept_visuals WHERE id=?", (kept_id,)).fetchone()
    if row is None:
        raise ServiceError("not_found", "no such kept visual", 404)
    return dict(row)


def kept_to_dto(k: dict) -> dict:
    from brindle.service.dto import KeptVisualDTO

    answer = json.loads(k["answer"]) if k["answer"] else None
    return KeptVisualDTO(
        id=k["id"], title=k["title"], from_session_name=k["from_session_name"], repo=k["repo"],
        answer={key: answer[key] for key in ("option", "note", "at", "attachments") if key in answer} if answer else None,
        kept_at=k["kept_at"],
    ).to_json()


def kept_page(db: DB, kept_id: str) -> Path:
    k = get_kept(db, kept_id)
    path = kept_path(k["repo"], k["id"])
    if not path.is_file():
        raise ServiceError("not_found", "the kept page is gone", 404)
    return path


# -- a session going -------------------------------------------------------------------


def forget_sessions(db: DB, repo_root: str, root_ids: list[str]) -> None:
    """Delete the visuals of sessions being removed: their rows and files.
    Kept copies (their own rows and files) stay. Re-reads the rows to go at
    the moment of deleting, so only what belongs to these sessions goes."""
    for root_id in root_ids:
        if not _SEGMENT.fullmatch(root_id or "") or root_id in (".", ".."):
            continue
        with db.tx() as c:
            gone = [r["id"] for r in c.execute("SELECT id FROM visuals WHERE root_id=?", (root_id,))]
            c.execute("DELETE FROM visuals WHERE root_id=?", (root_id,))
            c.execute("DELETE FROM plans WHERE root_id=?", (root_id,))
        shutil.rmtree(store_root(repo_root) / root_id, ignore_errors=True)
        for vid in gone:
            bus.publish(db, "visual.removed", repo_root=repo_root, root_id=root_id, visual=vid)
