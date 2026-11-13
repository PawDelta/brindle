"""A planning doc as a design doc for one person:
a series of revisions, comment threads anchored in it, suggestions
and direct edits. brindle.plans keeps what a plan *does* (it holds the
session's assigns until the person approves it); this module keeps what the
person reads and writes on it.

Revisions. Every revision is a ``plan_revisions`` row: its lede (the text
before the first section), its sections, each ``markdown`` or a diagram
(``mermaid`` or ``svg`` source, with an optional caption), its decisions and
its split. The supervisor's proposals are revisions; so is each change the
person makes (a direct edit, or a suggestion they accept). The ``plans`` row
holds the current one, as before, so everything that reads a plan's
markdown, decisions or split keeps working. A plan from before revisions
were kept has no row: its current revision is read from the ``plans`` row
(``revision``), so old plans stay readable and editable.

Threads. A comment anchors to a text range of a section (``text``: the
section, the range and the quoted text), a diagram section (``diagram``), a
decision (``decision``, optionally one option) or, as the step-by-step
reader did, a whole section or the ask, lede or split (``section``). Replies
name their thread; a comment is resolved and reopened. A suggestion is a
thread that replaces its anchored text with ``after``; the person, the doc's
author, accepts it (a new revision) or rejects it. Anchors are checked
against the revision they are written on and found again at read time in the
current one (``current``, ``moved`` or, when the text is gone, ``detached``).

Changes are made only while the plan is open: a redirected plan is the
supervisor's to revise, an approved one is building. Comments and votes are
recorded in any state. Request changes carries the open threads the person
wrote since their last verdict, and their edits, to the supervisor."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from brindle import bus, visuals
from brindle.db import DB
from brindle.service.errors import ServiceError

PERSON = "person"
SUPERVISOR = "supervisor"
TEAMMATE = "teammate:"       # + their account: comments and votes pulled from the Library (brindle.pro.shared_docs)
KINDS = ("markdown", "mermaid", "svg")
DIAGRAMS = ("mermaid", "svg")
LEDE = "lede"
MAX_SECTIONS = 60
MAX_SECTION = 64 * 1024      # one section's text or diagram source
MAX_QUOTE = 4000             # the anchored text of a comment or suggestion
MAX_THREADS = 500            # threads and replies on one plan
MAX_SNIPPET = 200            # quoted text in the supervisor's message
CONTEXT = 32                 # text kept either side of an insertion point, to find it again
# Section ids the comment anchors already use for something else.
RESERVED = {"ask", "lede", "split"}
# What an SVG diagram may hold: drawing elements and presentation attributes
# only. Nothing that runs (scripts, event handlers, animation that can set an
# attribute), styles (CSS can load) or loads (images, links, foreign content).
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_SVG_ELEMENTS = frozenset({
    "svg", "g", "defs", "title", "desc", "symbol", "use", "path", "rect", "circle", "ellipse", "line",
    "polyline", "polygon", "text", "tspan", "textPath", "marker", "linearGradient", "radialGradient", "stop",
    "clipPath", "mask", "pattern"})
_SVG_ATTRIBUTES = frozenset({
    "id", "class", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "fx", "fy", "d", "points", "pathLength", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
    "stroke-opacity", "stroke-dasharray", "stroke-dashoffset", "stroke-linecap", "stroke-linejoin",
    "stroke-miterlimit", "opacity", "transform", "font-family", "font-size", "font-weight", "font-style",
    "text-anchor", "dominant-baseline", "alignment-baseline", "baseline-shift", "letter-spacing",
    "word-spacing", "dx", "dy", "rotate", "textLength", "lengthAdjust", "preserveAspectRatio", "offset",
    "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod", "marker-start",
    "marker-mid", "marker-end", "markerWidth", "markerHeight", "markerUnits", "refX", "refY", "orient",
    "clip-path", "clip-rule", "clipPathUnits", "mask", "maskUnits", "maskContentUnits", "patternUnits",
    "patternContentUnits", "patternTransform", "visibility", "display", "version", "role", "aria-label",
    "aria-hidden", "vector-effect", "paint-order", "startOffset", "href"})
# Attribute values a renderer may read as CSS: a strict grammar, so nothing
# can be spelled in a way the check doesn't see. No backslash (CSS escapes),
# no slash (CSS comments), no colon or semicolon; the only functions are
# colours, transforms and url(), and url() names something in the drawing.
_SVG_VALUE = re.compile(r"[A-Za-z0-9#%.,+\-\s()'\"_]*")
_SVG_FUNCTION = re.compile(r"([A-Za-z_-]*)\s*\(")
_SVG_FUNCTIONS = frozenset({"url", "rgb", "rgba", "hsl", "hsla", "matrix", "translate", "scale", "rotate",
                            "skewx", "skewy"})
_SVG_FREE_TEXT = frozenset({"aria-label"})   # read as text, never as CSS


# -- revisions --------------------------------------------------------------------------


def _decode(row) -> dict:
    r = dict(row)
    for key in ("sections", "decisions", "split", "changed"):
        r[key] = json.loads(r[key])
    return r


def from_markdown(markdown: str) -> tuple[str, list[dict]]:
    """A supervisor's markdown as the lede and its ``##`` sections, all markdown."""
    from brindle import plans

    lede, parts = plans.sections(markdown)
    return lede, [{**s, "kind": "markdown", "caption": None} for s in parts]


def render(lede: str, sections: list[dict]) -> str:
    """A revision as one markdown text (the library's copy, and the plans row)."""
    out = [lede] if lede else []
    for s in sections:
        body = s["markdown"] if s["kind"] == "markdown" else f"```{s['kind']}\n{s['markdown']}\n```"
        if s.get("caption"):
            body += f"\n\n*{s['caption']}*"
        out.append(f"## {s['title']}\n\n{body}".rstrip())
    return "\n\n".join(out).strip()


def _svg(body: str, where: str) -> None:
    """An SVG diagram is shown in the app: drawing only, nothing that runs or
    loads. It is parsed and every element and attribute checked against an
    allowlist, with values as the renderer reads them (entities decoded):
    a reference (``href``, ``url(...)``) may only name something in the
    drawing itself (``#id``). No DTD, so no entity of our own making."""
    from xml.etree import ElementTree

    refused = ServiceError("invalid", f"{where} must be a plain <svg> drawing: no scripts, styles, "
                           "animation, event handlers or links out", 400)
    if "<!" in body.replace("<!--", "") or "<?" in body:
        raise refused
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise refused from None

    def local(name: str, allowed: tuple[str, ...]) -> str | None:
        ns, _, tail = name[1:].partition("}") if name.startswith("{") else ("", "", name)
        return tail if ns in allowed else None

    if local(root.tag, ("", _SVG_NS)) != "svg":
        raise refused
    for el in root.iter():
        tag = local(el.tag, ("", _SVG_NS)) if isinstance(el.tag, str) else None
        if tag not in _SVG_ELEMENTS:
            raise refused
        for name, value in el.attrib.items():
            attr = local(name, ("", _XLINK_NS))
            if attr not in _SVG_ATTRIBUTES or (attr == "href" and not value.strip().startswith("#")):
                raise refused
            if attr not in _SVG_FREE_TEXT and not _svg_value_ok(value):
                raise refused


def _svg_value_ok(value: str) -> bool:
    if not _SVG_VALUE.fullmatch(value):
        return False
    for m in _SVG_FUNCTION.finditer(value):
        name = m.group(1).lower()
        if name not in _SVG_FUNCTIONS:
            return False
        if name == "url" and not value[m.end():].lstrip().lstrip("'\"").startswith("#"):
            return False
    return True


def _body_text(value: Any, name: str, limit: int, required: bool = False) -> str:
    """Text whose whitespace matters (a section's body, an edit's before and after)."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ServiceError("invalid", f"{name} must be text", 400)
    if required and not value.strip():
        raise ServiceError("invalid", f"{name} is required", 400)
    if len(value) > limit:
        raise ServiceError("invalid", f"{name} is longer than {limit} characters", 400)
    return value.replace("\r\n", "\n").replace("\r", "\n")


def parse_sections(raw: Any) -> list[dict]:
    """The supervisor's sections: ``{id?, title, kind?, markdown, caption?}``
    each, ``kind`` markdown (the default), mermaid or svg. Ids default to
    s1, s2, ...; give your own to keep comments on a section across revisions."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SECTIONS:
        raise ServiceError("invalid", f"sections must be a list of 1 to {MAX_SECTIONS}", 400)
    out, seen = [], set()
    for n, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            raise ServiceError("invalid", "each section is {id?, title, kind?, markdown, caption?}", 400)
        sid = s.get("id") or f"s{n}"
        if not isinstance(sid, str) or not visuals.OPTION_ID.fullmatch(sid) or sid in seen or sid in RESERVED:
            raise ServiceError("invalid", "each section needs its own id of 1 to 32 letters, digits, _ or - "
                               "(not ask, lede or split)", 400)
        seen.add(sid)
        kind = s.get("kind") or "markdown"
        if kind not in KINDS:
            raise ServiceError("invalid", f"a section's kind is one of {', '.join(KINDS)}", 400)
        body = _body_text(s.get("markdown"), f"section {sid}", MAX_SECTION, required=kind in DIAGRAMS).strip()
        if kind == "svg":
            _svg(body, f"section {sid}")
        out.append({"id": sid, "title": visuals._text(s.get("title"), "a section's title", visuals.MAX_LABEL, True),
                    "kind": kind, "markdown": body,
                    "caption": visuals._text(s.get("caption"), "a diagram's caption", visuals.MAX_DETAIL)})
    return out


def insert_revision(c, plan_id: str, rev: int, author: str, source: str, lede: str, sections: list[dict],
                    markdown: str, decisions: list, split: list, ask: str | None, changed: list[str],
                    at: float) -> None:
    c.execute("INSERT INTO plan_revisions (plan_id, rev, author, source, lede, sections, markdown, decisions,"
              " split, ask, changed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (plan_id, rev, author, source, lede, json.dumps(sections), markdown, json.dumps(decisions),
               json.dumps(split), ask, json.dumps(changed), at))


def revision(db: DB, plan_id: str, rev: int | None = None) -> dict:
    """Revision ``rev`` of a plan (default: the current one)."""
    from brindle import plans

    p = plans._row(db, plan_id)
    n = p["rev"] if rev is None else rev
    row = db.conn.execute("SELECT * FROM plan_revisions WHERE plan_id=? AND rev=?", (p["id"], n)).fetchone()
    if row is not None:
        return _decode(row)
    if n != p["rev"]:
        raise ServiceError("not_found", "no such revision", 404)
    # A plan from before revisions were kept: its current revision is the plans row.
    lede, parts = from_markdown(p["markdown"])
    return {"plan_id": p["id"], "rev": n, "author": SUPERVISOR, "source": "proposal", "lede": lede,
            "sections": parts, "markdown": p["markdown"], "decisions": p["decisions"], "split": p["split"],
            "ask": p.get("ask"), "changed": [s["id"] for s in parts], "created_at": p["updated_at"],
            "stored": False}


def keep(c, cur: dict) -> None:
    """In the transaction that replaces revision ``cur``: if it is a plan's
    from before revisions were kept (read from the plans row), store it
    first, so the history keeps it once the plans row moves on."""
    if cur.get("stored", True):
        return
    c.execute("INSERT OR IGNORE INTO plan_revisions (plan_id, rev, author, source, lede, sections, markdown,"
              " decisions, split, ask, changed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (cur["plan_id"], cur["rev"], cur["author"], cur["source"], cur["lede"], json.dumps(cur["sections"]),
               cur["markdown"], json.dumps(cur["decisions"]), json.dumps(cur["split"]), cur["ask"],
               json.dumps(cur["changed"]), cur["created_at"]))


def _history(db: DB, p: dict) -> list[dict]:
    rows = [_decode(r) for r in db.conn.execute(
        "SELECT * FROM plan_revisions WHERE plan_id=? ORDER BY rev", (p["id"],))]
    if not rows or rows[-1]["rev"] != p["rev"]:
        rows.append(revision(db, p["id"]))
    return rows


def _body(cur: dict, sid: str) -> str | None:
    if sid == LEDE:
        return cur["lede"]
    return next((s["markdown"] for s in cur["sections"] if s["id"] == sid), None)


def _section(cur: dict, sid: str) -> dict | None:
    return next((s for s in cur["sections"] if s["id"] == sid), None)


def _locate(body: str, start: Any, quote: str) -> int | None:
    """Where ``quote`` is in ``body``: at ``start`` if it is there, else its
    occurrence nearest ``start`` (offsets from the app are a hint: they may
    count characters differently, or the text may have moved)."""
    hint = start if isinstance(start, int) and not isinstance(start, bool) else 0
    if 0 <= hint <= len(body) and body[hint:hint + len(quote)] == quote:
        return hint
    if not quote:
        return None
    hits = [m.start() for m in re.finditer(re.escape(quote), body)]
    return min(hits, key=lambda i: abs(i - hint)) if hits else None


# -- checks ------------------------------------------------------------------------------


def _plan(db: DB, plan_id: str) -> dict:
    from brindle import plans

    visuals.require_enabled()
    p = plans.get(db, plan_id)
    visuals._unlocked(p["root_id"])
    return p


def _on_screen(p: dict, rev: Any) -> None:
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise ServiceError("invalid", "rev (the revision on screen) is required", 400)
    if rev != p["rev"]:
        raise ServiceError("stale", f"the plan has a newer revision (r{p['rev']}); read that one", 409, rev=p["rev"])


def _editable(p: dict) -> None:
    if p["state"] == "approved":
        raise ServiceError("approved", "this plan is approved and building; it can't change now", 409)
    if p["state"] != "open":
        raise ServiceError("redirected", "the supervisor is revising this plan; edit its next revision", 409)


def _anchor(p: dict, cur: dict, raw: Any) -> dict:
    from brindle import plans

    if not isinstance(raw, dict):
        raise ServiceError("invalid", "a comment needs an anchor: {kind: text|diagram|decision|section, ...}", 400)
    kind = raw.get("kind")
    if kind == "text":
        sid = raw.get("section")
        body = _body(cur, sid) if isinstance(sid, str) else None
        section = _section(cur, sid) if isinstance(sid, str) else None
        if body is None or (section and section["kind"] in DIAGRAMS):
            raise ServiceError("invalid", "a text anchor names a text section of this revision (or the lede)", 400)
        quote = _body_text(raw.get("quote"), "the anchored text", MAX_QUOTE, required=True)
        pos = _locate(body, raw.get("start"), quote)
        if pos is None:
            raise ServiceError("stale", "that text isn't in this revision", 409, rev=p["rev"])
        return {"kind": "text", "section": sid, "start": pos, "end": pos + len(quote), "quote": quote}
    if kind == "diagram":
        section = _section(cur, raw.get("section"))
        if section is None or section["kind"] not in DIAGRAMS:
            raise ServiceError("invalid", "a diagram anchor names a diagram section of this revision", 400)
        return {"kind": "diagram", "section": section["id"]}
    if kind == "decision":
        d = next((d for d in cur["decisions"] if d["id"] == raw.get("decision")), None)
        option = raw.get("option")
        if d is None or (option is not None and option not in {o["id"] for o in d["options"]}):
            raise ServiceError("invalid", "a decision anchor names one of this revision's decisions "
                               "(and, optionally, one of its options)", 400)
        return {"kind": "decision", "decision": d["id"], "option": option}
    if kind == "section":
        if raw.get("section") not in plans.anchors(p):
            raise ServiceError("invalid", "a section anchor names a section, the ask, the lede, the split or "
                               "decision:<id> of this revision", 400)
        return {"kind": "section", "section": raw["section"]}
    raise ServiceError("invalid", "an anchor's kind is text, diagram, decision or section", 400)


def _where(cur: dict, anchor: dict) -> dict:
    """``anchor`` in revision ``cur``: where its text is now, and whether
    it is still ``current``, has ``moved`` or is ``detached`` (gone)."""
    out = dict(anchor, state="current")
    if anchor["kind"] == "text" and not anchor["quote"]:
        return _insertion(cur, anchor, out)
    if anchor["kind"] == "text":
        own = _body(cur, anchor["section"])
        if own is not None and own[anchor["start"]:anchor["end"]] == anchor["quote"]:
            return out
        others = [LEDE, *(s["id"] for s in cur["sections"] if s["kind"] == "markdown")]
        for sid in [anchor["section"], *(s for s in others if s != anchor["section"])]:
            body = _body(cur, sid)
            pos = _locate(body, anchor["start"], anchor["quote"]) if body is not None else None
            if pos is not None:
                return dict(out, section=sid, start=pos, end=pos + len(anchor["quote"]), state="moved")
        return dict(out, state="detached")
    if anchor["kind"] == "diagram":
        ok = (_section(cur, anchor["section"]) or {}).get("kind") in DIAGRAMS
    elif anchor["kind"] == "decision":
        ok = any(d["id"] == anchor["decision"] for d in cur["decisions"])
    else:
        ok = True
    return out if ok else dict(out, state="detached")


def _context(body: str, pos: int) -> dict:
    """The text either side of an insertion point at ``pos``, to find it by later."""
    return {"prefix": body[max(0, pos - CONTEXT):pos], "suffix": body[pos:pos + CONTEXT]}


def _insertion(cur: dict, anchor: dict, out: dict) -> dict:
    """An insertion point has no text of its own: it is found in its section
    by the text either side of it. If that text changed, the point can't be
    placed and is ``detached``, never guessed from a stale offset."""
    body = _body(cur, anchor["section"])
    prefix, suffix, start = anchor.get("prefix"), anchor.get("suffix"), anchor["start"]
    if body is None or prefix is None or suffix is None:
        return dict(out, state="detached")
    needle = prefix + suffix
    if not needle:                       # it was an empty section: only still placeable while empty
        return out if body == "" else dict(out, state="detached")
    if start >= len(prefix) and body[start - len(prefix):start + len(suffix)] == needle:
        return out
    hits = [m.start() + len(prefix) for m in re.finditer(re.escape(needle), body)]
    if not hits:
        return dict(out, state="detached")
    pos = min(hits, key=lambda i: abs(i - start))
    return dict(out, start=pos, end=pos, state="moved")


def _attachments(db: DB, p: dict, raw: Any) -> list[dict]:
    names, refs = visuals._answer_attachments(db, p["root_id"], raw)
    return [{"name": n, "ref": r} for n, r in zip(names, refs, strict=True)]


def _changed(db: DB, p: dict, rev: int) -> None:
    bus.publish(db, "visual.plan_changed", repo_root=p["visual"]["repo_root"], root_id=p["root_id"],
                visual=p["id"], rev=rev)


# -- comments and replies ----------------------------------------------------------------


def _thread(db: DB, p: dict, thread_id: Any) -> dict:
    row = None
    if isinstance(thread_id, int) and not isinstance(thread_id, bool):
        row = db.conn.execute("SELECT * FROM plan_threads WHERE id=? AND plan_id=?", (thread_id, p["id"])).fetchone()
    if row is None:
        raise ServiceError("not_found", "no such thread on this plan", 404)
    return dict(row)


def _add(db: DB, p: dict, rev: int, kind: str, anchor: dict | None, author: str, text: str,
         attachments: list[dict], parent: int | None = None, after: str | None = None, question: bool = False,
         origin: str | None = None) -> int:
    now = time.time()
    with db.tx() as c:
        if c.execute("SELECT COUNT(*) FROM plan_threads WHERE plan_id=?", (p["id"],)).fetchone()[0] >= MAX_THREADS:
            raise ServiceError("too_many", f"a plan may have at most {MAX_THREADS} comments and replies", 400)
        tid = int(c.execute(
            "INSERT INTO plan_threads (plan_id, parent_id, rev, kind, anchor, author, body, after, attachments,"
            " status, created_at, updated_at, question, origin) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?)",
            (p["id"], parent, rev, kind, json.dumps(anchor) if anchor else None, author, text, after,
             json.dumps(attachments), now, now, question, origin)).lastrowid)
    _changed(db, p, p["rev"])
    return tid


def comment(db: DB, plan_id: str, rev: Any = None, anchor: Any = None, text: Any = None,
            attachments: Any = None, *, parent: Any = None, author: str = PERSON, question: bool = False,
            origin: str | None = None) -> int:
    """A comment anchored in revision ``rev`` (the one on screen), or, with
    ``parent``, a reply to that thread. Returns the new thread or reply's id.
    ``origin``: what made it, kept with it so it can be found again (unique)."""
    p = _plan(db, plan_id)
    text = visuals._text(text, "a comment", visuals.MAX_NOTE, required=True)
    visuals._refuse_secrets([("your comment", text)])
    if parent is not None:
        if question:
            raise ServiceError("invalid", "a question starts a thread", 400)
        thread = _thread(db, p, parent)
        if thread["parent_id"] is not None:
            raise ServiceError("invalid", "reply to the thread, not to a reply", 400)
        return _add(db, p, p["rev"], "reply", None, author, text, _attachments(db, p, attachments),
                    parent=thread["id"], origin=origin)
    _on_screen(p, rev)
    where = _anchor(p, revision(db, p["id"]), anchor)
    return _add(db, p, p["rev"], "comment", where, author, text, _attachments(db, p, attachments), question=question,
                origin=origin)


def resolve(db: DB, plan_id: str, thread_id: Any, resolved: bool = True) -> None:
    """Resolve a comment thread, or (``resolved=False``) reopen it."""
    p = _plan(db, plan_id)
    thread = _thread(db, p, thread_id)
    if thread["kind"] != "comment":
        raise ServiceError("invalid", "only a comment thread is resolved; a suggestion is accepted or rejected", 400)
    with db.tx() as c:
        c.execute("UPDATE plan_threads SET status=?, updated_at=? WHERE id=? AND status IN ('open','resolved')",
                  ("resolved" if resolved else "open", time.time(), thread["id"]))
    _changed(db, p, p["rev"])


# -- suggestions and edits --------------------------------------------------------------


def suggest(db: DB, plan_id: str, rev: Any, section: Any, start: Any, before: Any, after: Any,
            text: Any = None, attachments: Any = None, *, author: str = PERSON, origin: str | None = None) -> int:
    """Suggest replacing ``before`` (at ``start`` in ``section`` of revision
    ``rev``; empty to insert there) with ``after``. Returns the thread's id."""
    p = _plan(db, plan_id)
    if p["state"] == "approved":
        _editable(p)
    _on_screen(p, rev)
    cur = revision(db, p["id"])
    before = _body_text(before, "the text to change", MAX_QUOTE)
    after = _body_text(after, "the replacement", MAX_SECTION)
    if before == after:
        raise ServiceError("invalid", "a suggestion changes the text", 400)
    sid, pos = _target(p, cur, section, start, before)
    note = visuals._text(text, "a suggestion's note", visuals.MAX_NOTE) or ""
    visuals._refuse_secrets([("your suggestion", after), ("your note", note)])
    anchor = {"kind": "text", "section": sid, "start": pos, "end": pos + len(before), "quote": before}
    if not before:
        anchor.update(_context(_body(cur, sid), pos))
    return _add(db, p, p["rev"], "suggestion", anchor, author, note, _attachments(db, p, attachments), after=after,
                origin=origin)


def _target(p: dict, cur: dict, section: Any, start: Any, before: str) -> tuple[str, int]:
    body = _body(cur, section) if isinstance(section, str) else None
    if body is None:
        raise ServiceError("invalid", "name a section of this revision (or the lede)", 400)
    pos = _locate(body, start, before)
    if pos is None:
        raise ServiceError("stale", "that text isn't in this revision", 409, rev=p["rev"])
    return section, pos


def edit(db: DB, plan_id: str, rev: Any, section: Any, start: Any, before: Any, after: Any) -> dict:
    """The person's direct edit: ``before`` at ``start`` in ``section``
    becomes ``after``, as a new revision. Returns that revision."""
    p = _plan(db, plan_id)
    _editable(p)
    _on_screen(p, rev)
    cur = revision(db, p["id"])
    before = _body_text(before, "the text to change", MAX_SECTION)
    after = _body_text(after, "the replacement", MAX_SECTION)
    sid, pos = _target(p, cur, section, start, before)
    return _apply(db, p, cur, sid, pos, before, after, "edit")


def accept(db: DB, plan_id: str, thread_id: Any, rev: Any) -> dict:
    """The author accepts a suggestion: its change, made to the revision on
    screen (``rev``), is a new revision. Returns that revision."""
    p = _plan(db, plan_id)
    _editable(p)
    _on_screen(p, rev)
    thread = _thread(db, p, thread_id)
    if thread["kind"] != "suggestion" or thread["status"] != "open":
        raise ServiceError("invalid", "that isn't an open suggestion", 409)
    cur = revision(db, p["id"])
    where = _where(cur, json.loads(thread["anchor"]))
    if where["state"] == "detached":
        raise ServiceError("stale", "the text this suggestion changes is gone; reject it or suggest again", 409)
    return _apply(db, p, cur, where["section"], where["start"], where["quote"], thread["after"], "suggestion",
                  thread["id"])


def reject(db: DB, plan_id: str, thread_id: Any) -> None:
    p = _plan(db, plan_id)
    thread = _thread(db, p, thread_id)
    with db.tx() as c:
        done = c.execute("UPDATE plan_threads SET status='rejected', updated_at=? WHERE id=? AND kind='suggestion'"
                         " AND status='open'", (time.time(), thread["id"])).rowcount
    if not done:
        raise ServiceError("invalid", "that isn't an open suggestion", 409)
    _changed(db, p, p["rev"])


def _apply(db: DB, p: dict, cur: dict, sid: str, pos: int, before: str, after: str, source: str,
           thread_id: int | None = None) -> dict:
    from brindle import plans

    lede, sections = cur["lede"], [dict(s) for s in cur["sections"]]
    body = _body(cur, sid)
    new = body[:pos] + after + body[pos + len(before):]
    if len(new) > MAX_SECTION:
        raise ServiceError("too_big", f"a section may be at most {MAX_SECTION // 1024} KB", 400)
    if sid == LEDE:
        lede = new
    else:
        section = next(s for s in sections if s["id"] == sid)
        if section["kind"] == "svg":
            _svg(new, f"section {sid}")
        section["markdown"] = new
    markdown = render(lede, sections)
    if len(markdown.encode("utf-8")) > plans.MAX_MARKDOWN:
        raise ServiceError("too_big", f"a plan may be at most {plans.MAX_MARKDOWN // 1024} KB of markdown", 400)
    visuals._refuse_secrets([("your edit", after)])
    now, rev = time.time(), p["rev"] + 1
    with db.tx() as c:
        # Claimed from the revision on screen, while open: a second edit made
        # against the same revision (another window) is refused, never merged blind.
        claimed = c.execute("UPDATE plans SET rev=?, markdown=?, updated_at=? WHERE id=? AND rev=? AND state='open'",
                            (rev, markdown, now, p["id"], p["rev"])).rowcount == 1
        if claimed:
            keep(c, cur)
            insert_revision(c, p["id"], rev, PERSON, source, lede, sections, markdown, cur["decisions"],
                            cur["split"], cur["ask"], [sid], now)
            if thread_id is not None and not c.execute(
                    "UPDATE plan_threads SET status='accepted', updated_at=? WHERE id=? AND status='open'",
                    (now, thread_id)).rowcount:
                raise ServiceError("invalid", "that suggestion was answered meanwhile", 409)
    if not claimed:
        now_p = plans._row(db, p["id"])
        if now_p["rev"] != p["rev"]:
            raise ServiceError("stale", f"the plan has a newer revision (r{now_p['rev']}); read that one", 409,
                               rev=now_p["rev"])
        _editable(now_p)
    _changed(db, p, rev)
    return revision(db, p["id"], rev)


# -- decisions ---------------------------------------------------------------------------


def confirm(db: DB, plan_id: str, rev: Any, decision: Any, option: Any, *, voter: str = PERSON) -> None:
    """The person's vote on a decision (Confirm A). It shapes the verdict's
    picks; like today's picks, it decides nothing until they approve or
    request changes."""
    p = _plan(db, plan_id)
    if p["state"] != "open":
        _editable(p)
    _on_screen(p, rev)
    d = next((d for d in p["decisions"] if d["id"] == decision), None)
    if d is None or option not in {o["id"] for o in d["options"]}:
        raise ServiceError("invalid", "confirm one of this revision's decisions and one of its options", 400)
    with db.tx() as c:
        c.execute("INSERT INTO plan_votes (plan_id, decision_id, voter, option_id, rev, at) VALUES (?,?,?,?,?,?)"
                  " ON CONFLICT(plan_id, decision_id, voter) DO UPDATE SET option_id=excluded.option_id,"
                  " rev=excluded.rev, at=excluded.at", (p["id"], d["id"], voter, option, p["rev"], time.time()))
    _changed(db, p, p["rev"])


def _votes(db: DB, p: dict) -> dict[str, dict[str, str]]:
    """{decision: {voter: option}}, for this revision's decisions and options only."""
    valid = {d["id"]: {o["id"] for o in d["options"]} for d in p["decisions"]}
    out: dict[str, dict[str, str]] = {}
    for r in db.conn.execute("SELECT decision_id, voter, option_id FROM plan_votes WHERE plan_id=?", (p["id"],)):
        if r["option_id"] in valid.get(r["decision_id"], ()):
            out.setdefault(r["decision_id"], {})[r["voter"]] = r["option_id"]
    return out


def confirmed(db: DB, p: dict) -> dict[str, str]:
    """The person's confirmed picks, which a verdict starts from."""
    return {d: v[PERSON] for d, v in _votes(db, p).items() if PERSON in v}


# -- what a verdict carries to the supervisor -------------------------------------------


def _snip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_SNIPPET else text[:MAX_SNIPPET - 1] + "…"


def _title(p: dict, cur: dict, anchor: dict) -> str:
    from brindle import plans

    if anchor["kind"] == "decision":
        d = next((d for d in cur["decisions"] if d["id"] == anchor["decision"]), None)
        name = f"Decision {anchor['decision']}" + (f" ({d['title']})" if d else "")
        return name + (f", option {anchor['option']}" if anchor.get("option") else "")
    if anchor["kind"] == "section":
        return plans.anchors(p).get(anchor["section"], anchor["section"])
    if anchor["section"] == LEDE:
        return "The overview"
    s = _section(cur, anchor["section"])
    title = s["title"] if s else anchor["section"]
    return f"the diagram in {title}" if anchor["kind"] == "diagram" else title


def pending(db: DB, p: dict) -> tuple[list[str], list[str], list[int]]:
    """What the person and their teammates wrote on the doc that hasn't
    reached the supervisor: open comments and suggestions, replies, and the
    revisions the person made since the supervisor's last. Returns the
    message's lines (with attachment references), the library record's lines
    (with file names) and the threads to mark sent with the verdict."""
    cur = revision(db, p["id"])
    message, record, ids = [], [], []
    rows = [dict(r) for r in db.conn.execute(
        "SELECT * FROM plan_threads WHERE plan_id=? AND author!=? AND sent_rev IS NULL AND status='open'"
        " AND question=0 AND (parent_id IS NULL OR parent_id NOT IN"
        " (SELECT id FROM plan_threads WHERE question=1))"
        " ORDER BY id", (p["id"], SUPERVISOR))]
    for t in rows:
        files = json.loads(t["attachments"])
        teammate = t["author"][len(TEAMMATE):] if t["author"].startswith(TEAMMATE) else None
        if t["kind"] == "reply":
            who = f"Teammate {teammate}'s" if teammate else "Their"
            line = f"{who} reply on thread #{t['parent_id']}: {t['body']}"
        else:
            where = _where(cur, json.loads(t["anchor"]))
            head = f"On \"{_title(p, cur, where)}\""
            if where["kind"] == "text":
                head += f" at \"{_snip(where['quote'])}\""
            if teammate:
                head += f" (from teammate {teammate})"
            if t["kind"] == "suggestion":
                line = f"{head}: they suggest \"{_snip(t['after'])}\"" + (f" ({t['body']})" if t["body"] else "")
            else:
                line = f"{head}: {t['body']}"
            line += f" [thread #{t['id']}]"
        message.append(" ".join([line, *(f["ref"] for f in files)]))
        record.append(line + "".join(f" (attached: {f['name']})" for f in files))
        ids.append(t["id"])
    last = db.conn.execute("SELECT MAX(rev) FROM plan_revisions WHERE plan_id=? AND author=?",
                           (p["id"], SUPERVISOR)).fetchone()[0] or 0
    for r in db.conn.execute("SELECT author, rev, source, changed FROM plan_revisions WHERE plan_id=?"
                             " AND (author=? OR author LIKE ?) AND rev>? ORDER BY rev",
                             (p["id"], PERSON, TEAMMATE + "%", last)):
        titles = [_title(p, cur, {"kind": "text", "section": s}) for s in json.loads(r["changed"])]
        how = "accepted a suggestion in" if r["source"] == "suggestion" else "edited"
        who = "They" if r["author"] == PERSON else f"Teammate {r['author'][len(TEAMMATE):]}"
        line = f"{who} {how} {', '.join(titles)} (r{r['rev']}); revise from the current text."
        message.append(line)
        record.append(line)
    return message, record, ids


def mark_sent(c, ids: list[int], rev: int) -> None:
    """In the verdict's transaction: these threads went to the supervisor with it."""
    if ids:
        c.execute(f"UPDATE plan_threads SET sent_rev=? WHERE id IN ({','.join('?' * len(ids))}) AND sent_rev IS NULL",
                  (rev, *ids))


# -- what the app shows -----------------------------------------------------------------


def _thread_dto(t: dict, cur: dict, replies: list[dict]) -> dict:
    out = {"id": t["id"], "kind": t["kind"], "rev": t["rev"], "author": t["author"], "text": t["body"],
           "anchor": _where(cur, json.loads(t["anchor"])), "attachments": [f["name"] for f in
                                                                          json.loads(t["attachments"])],
           "status": t["status"], "sent": t["sent_rev"] is not None, "at": t["created_at"],
           "question": bool(t["question"]),
           "replies": [{"id": r["id"], "author": r["author"], "text": r["body"], "at": r["created_at"],
                        "sent": r["sent_rev"] is not None, "from_supervisor": bool(r["from_supervisor"]),
                        "attachments": [f["name"] for f in json.loads(r["attachments"])]} for r in replies]}
    if t["kind"] == "suggestion":
        out.update(before=json.loads(t["anchor"])["quote"], after=t["after"])
    return out


def _revision_dto(r: dict) -> dict:
    return {"rev": r["rev"], "author": r["author"], "source": r["source"], "changed": r["changed"],
            "at": r["created_at"]}


def revision_dto(db: DB, plan_id: str, rev: Any) -> dict:
    """One revision's full content, for reading an earlier one."""
    from brindle.service.dto import PlanRevisionDetailDTO

    p = _plan(db, plan_id)
    n = int(rev) if isinstance(rev, str) and rev.isdigit() else 0
    r = revision(db, p["id"], n) if 1 <= n <= p["rev"] else None
    if r is None:
        raise ServiceError("not_found", "no such revision", 404)
    return PlanRevisionDetailDTO(**_revision_dto(r), lede=r["lede"], sections=r["sections"], markdown=r["markdown"],
                                 decisions=r["decisions"], split=r["split"], ask=r["ask"]).to_json()


def fields(db: DB, p: dict) -> dict:
    """The doc's part of a plan's DTO: the current revision's lede and
    sections, its decisions with their votes, every revision, the threads."""
    cur = revision(db, p["id"])
    votes = _votes(db, p)
    decisions = []
    for d in p["decisions"]:
        mine = votes.get(d["id"], {})
        by_option: dict[str, list[str]] = {}
        for voter, option in sorted(mine.items()):
            by_option.setdefault(option, []).append(voter)
        decisions.append({**d, "votes": by_option, "confirmed": mine.get(PERSON)})
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM plan_threads WHERE plan_id=? ORDER BY id", (p["id"],))]
    replies: dict[int, list[dict]] = {}
    for r in rows:
        if r["parent_id"] is not None:
            replies.setdefault(r["parent_id"], []).append(r)
    return {"lede": cur["lede"], "sections": cur["sections"], "decisions": decisions,
            "revisions": [_revision_dto(r) for r in _history(db, p)],
            "threads": [_thread_dto(t, cur, replies.get(t["id"], [])) for t in rows if t["parent_id"] is None],
            "can_edit": p["state"] == "open"}
