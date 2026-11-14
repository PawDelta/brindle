"""Doc format (Settings › Doc format): the shapes a planning doc may take,
how docs are named, and what a missing required section stops.

A *format* is ``{templates, naming, blocks}``:

- ``templates``: ``[{id, name, default, sections: [{title, kind, required,
  hint}]}]``, at most one the default. ``kind`` is text, diagram or table;
  ``hint`` is the line the supervisor is told about what belongs there.
- ``naming``: ``{title_hint, reference}``; ``reference`` is a pattern such
  as ``DES-{number}``, numbered per pattern on this machine (DES-0001, ...).
- ``blocks``: what an empty required section stops, a subset of
  ``sign_off`` (the person's Approve, a reviewer's sign-off and the author's
  Freeze) and ``share`` (sharing it to the Library). Writing and review are
  never stopped.

Formats are kept per *level*: ``org`` and ``team:<id>``, a team's keys over
its org's (:func:`resolve`), so a team can default to its own template; a
doc follows the team it was shared with (:func:`team_of`). A
:class:`FormatStore` keeps the levels: a solo person's in their
``~/.brindle/config.json`` (``doc_format``, :class:`LocalStore`); a Team or
Enterprise org's in the Library (:class:`ProStore`), which fails closed to
no format while it can't be read.

A new plan (brindle.plans.propose) starts from the default template: its
headings the supervisor left out are added, empty, in the template's order,
and the plan keeps that template and its reference (``plan_formats``) for
good: a later change to the template never rewrites a doc already written."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol

from brindle import config
from brindle.db import DB
from brindle.service.errors import ServiceError

KEY = "doc_format"                 # ~/.brindle/config.json
ORG = "org"
TEAM = "team:"
KINDS = ("text", "diagram", "table")
SIGN_OFF, SHARE = "sign_off", "share"
BLOCKS = (SIGN_OFF, SHARE)
NUMBER = "{number}"
MAX_TEMPLATES = 20
MAX_SECTIONS = 30
MAX_NAME = 80
MAX_HINT = 300
MAX_PATTERN = 40
_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_PATTERN = re.compile(r"[A-Za-z0-9_\-./ ]*")
_TEAM_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
# Unset in every level: no template, no reference, and a missing section
# (there are none without a template) would stop the sign-off.
DEFAULT: dict = {"templates": [], "naming": {"title_hint": None, "reference": None}, "blocks": [SIGN_OFF]}
log = logging.getLogger(__name__)


# -- checking a format -------------------------------------------------------------------


def _text(value: Any, name: str, limit: int, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ServiceError("invalid", f"{name} is required", 400)
        return None
    if not isinstance(value, str):
        raise ServiceError("invalid", f"{name} must be text", 400)
    value = " ".join(value.split())
    if len(value) > limit:
        raise ServiceError("invalid", f"{name} is longer than {limit} characters", 400)
    return value


def _key(title: str) -> str:
    return " ".join(title.split()).casefold()


def _sections(raw: Any, where: str) -> list[dict]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SECTIONS:
        raise ServiceError("invalid", f"{where} needs 1 to {MAX_SECTIONS} sections", 400)
    out, seen = [], set()
    for s in raw:
        if not isinstance(s, dict):
            raise ServiceError("invalid", "each section is {title, kind, required, hint}", 400)
        title = _text(s.get("title"), "a section's title", MAX_NAME, required=True)
        if _key(title) in seen:
            raise ServiceError("invalid", f"{where} has two sections called {title!r}", 400)
        seen.add(_key(title))
        kind = s.get("kind") or "text"
        if kind not in KINDS:
            raise ServiceError("invalid", f"a section's kind is one of {', '.join(KINDS)}", 400)
        out.append({"title": title, "kind": kind, "required": s.get("required") is True,
                    "hint": _text(s.get("hint"), f"the hint for {title}", MAX_HINT)})
    return out


def _templates(raw: Any) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > MAX_TEMPLATES:
        raise ServiceError("invalid", f"templates must be a list of at most {MAX_TEMPLATES}", 400)
    out, ids = [], set()
    for t in raw:
        if not isinstance(t, dict):
            raise ServiceError("invalid", "each template is {id, name, default, sections}", 400)
        tid = t.get("id")
        if not isinstance(tid, str) or not _ID.fullmatch(tid) or tid in ids:
            raise ServiceError("invalid", "each template needs its own id of 1 to 32 letters, digits, _ or -", 400)
        ids.add(tid)
        name = _text(t.get("name"), "a template's name", MAX_NAME, required=True)
        out.append({"id": tid, "name": name, "default": t.get("default") is True,
                    "sections": _sections(t.get("sections"), name)})
    if sum(t["default"] for t in out) > 1:
        raise ServiceError("invalid", "only one template can be the default", 400)
    return out


def _naming(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ServiceError("invalid", "naming is {title_hint, reference}", 400)
    reference = _text(raw.get("reference"), "the reference pattern", MAX_PATTERN)
    if reference is not None and (reference.count(NUMBER) != 1
                                  or not _PATTERN.fullmatch(reference.replace(NUMBER, ""))):
        raise ServiceError("invalid", f"the reference pattern needs {NUMBER} once, with letters, digits "
                           "and - _ . / around it (like DES-{number})", 400)
    return {"title_hint": _text(raw.get("title_hint"), "the title hint", MAX_HINT), "reference": reference}


def _blocks(raw: Any) -> list[str]:
    if not isinstance(raw, list) or any(b not in BLOCKS for b in raw):
        raise ServiceError("invalid", f"blocks is a list of {', '.join(BLOCKS)}", 400)
    return [b for b in BLOCKS if b in raw]


def check(raw: Any) -> dict:
    """A level's format as stored: only the keys it sets, each checked."""
    if not isinstance(raw, dict):
        raise ServiceError("invalid", "a doc format is {templates?, naming?, blocks?}", 400)
    unknown = set(raw) - {"templates", "naming", "blocks"}
    if unknown:
        raise ServiceError("invalid", f"a doc format has no {', '.join(sorted(unknown))}", 400)
    out: dict = {}
    if raw.get("templates") is not None:
        out["templates"] = _templates(raw["templates"])
    if raw.get("naming") is not None:
        out["naming"] = _naming(raw["naming"])
    if raw.get("blocks") is not None:
        out["blocks"] = _blocks(raw["blocks"])
    return out


# -- the levels ---------------------------------------------------------------------------


def _level(level: Any) -> str:
    if level == ORG or (isinstance(level, str) and level.startswith(TEAM)
                        and _TEAM_ID.fullmatch(level[len(TEAM):])):
        return level
    raise ServiceError("invalid", f"level is {ORG} or {TEAM}<id>", 400)


def _checked(found: Any) -> dict:
    if not isinstance(found, dict):
        return {}
    try:
        return check(found)
    except ServiceError:
        return {}       # a stored value that no longer checks sets nothing


class FormatStore(Protocol):
    """Where the levels' formats are kept. ``source`` is "local" (this
    person's own config) or "org" (their organization's, in the Library);
    ``editable`` says whether this brindle can save it."""

    source: str
    editable: bool

    def load(self, level: str) -> dict: ...

    def save(self, level: str, fmt: dict) -> None: ...


class LocalStore:
    """A solo person's format: ``doc_format`` in ~/.brindle/config.json, by level."""

    source, editable = "local", True

    def load(self, level: str) -> dict:
        found = config.user_settings().get(KEY)
        return _checked(found.get(level) if isinstance(found, dict) else None)

    def save(self, level: str, fmt: dict) -> None:
        def change(now: Any) -> dict:
            levels = dict(now) if isinstance(now, dict) else {}
            levels[level] = fmt
            return levels

        config.update_user(KEY, change)


class ProStore:
    """A Team or Enterprise org's format, kept by the Library per org and team
    (pawdelta-web), never in this machine's config: one person can belong to
    several orgs. ``ROUTE`` is the Library's doc-format route, ``{org}`` and
    ``{level}`` filled in. Until it is set, or when the Library can't be
    reached, an org has no format here (fail closed: never another org's, nor
    the person's own local one), and saving is refused."""

    ROUTE: str | None = None
    source, editable = "org", False

    def __init__(self, org_id: str):
        self.org_id = org_id
        self.editable = self.ROUTE is not None

    def _path(self, level: str) -> str:
        from urllib.parse import quote

        assert self.ROUTE is not None
        return self.ROUTE.format(org=quote(self.org_id, safe=""), level=quote(level, safe=""))

    def load(self, level: str) -> dict:
        if self.ROUTE is None:
            return {}
        from brindle.pro import analysis_docs as ad

        try:
            status, got = ad._call("GET", self._path(level))
        except Exception:  # noqa: BLE001 - unreachable or signed out: no format, not someone else's
            log.warning("brindle: couldn't read the org's doc format", exc_info=True)
            return {}
        return _checked(got.get("format")) if status == 200 and isinstance(got, dict) else {}

    def save(self, level: str, fmt: dict) -> None:
        if self.ROUTE is None:
            raise ServiceError("org_format", "your organization's doc format is kept in the Library, and this "
                               "brindle can't change it yet", 409)
        from brindle.pro import analysis_docs as ad

        status, got = ad._call("PUT", self._path(level), {"format": fmt})
        if status != 200:
            raise ServiceError("upstream", f"the Library refused it: {got.get('error') or f'HTTP {status}'}", 502)


def store() -> FormatStore:
    """The store for the person signed in here: their org's when its plan
    shares design docs (Team and Enterprise), else their own local one."""
    from brindle.pro import license, shared_docs

    try:
        ent = license.current(refresh=False)
    except Exception:  # noqa: BLE001 - not signed in to brindle Pro: a solo person's own format
        return LocalStore()
    return ProStore(ent.org_id) if shared_docs.TEAM_FEATURE in ent.features else LocalStore()


def load(level: str = ORG, where: FormatStore | None = None) -> dict:
    """What one level sets (a level that sets nothing is ``{}``)."""
    return (where or store()).load(_level(level))


def save(level: str, raw: Any, where: FormatStore | None = None) -> dict:
    """Replace what ``level`` sets; returns it as stored."""
    level, fmt = _level(level), check(raw)
    (where or store()).save(level, fmt)
    return fmt


def resolve(team: str | None = None, where: FormatStore | None = None) -> dict:
    """The format in force: the defaults, the org's keys over them, then the team's."""
    where = where or store()
    out = {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DEFAULT.items()}
    for level in [ORG] + ([TEAM + team] if team else []):
        out.update(where.load(level))
    return out


def team_of(db: DB, plan_id: str | None) -> str | None:
    """The team a doc belongs to: the one it was shared with, else none (the org's format)."""
    if not plan_id:
        return None
    row = db.conn.execute("SELECT team FROM plan_formats WHERE plan_id=?", (plan_id,)).fetchone()
    return row["team"] if row else None


def set_team(db: DB, plan_id: str, team: str | None) -> None:
    """Record that plan ``plan_id`` was shared with ``team`` (a group), so its
    gates follow that team's format from now on."""
    if not team or not _TEAM_ID.fullmatch(team):
        return
    with db.tx() as c:
        c.execute("INSERT INTO plan_formats (plan_id, team, created_at) VALUES (?,?,?)"
                  " ON CONFLICT(plan_id) DO UPDATE SET team=excluded.team", (plan_id, team, time.time()))


def default_template(fmt: dict) -> dict | None:
    """The template a new doc starts from: the one marked default, else the only one."""
    templates = fmt["templates"]
    return next((t for t in templates if t["default"]), templates[0] if len(templates) == 1 else None)


def view(db: DB, level: str = ORG) -> dict:
    """Settings › Doc format's view of ``level``: what it sets, the format in
    force there, the next reference, and how many docs each template wrote."""
    level, where = _level(level), store()
    resolved = resolve(level[len(TEAM):] if level.startswith(TEAM) else None, where)
    used = {r[0]: r[1] for r in db.conn.execute(
        "SELECT json_extract(template, '$.id'), count(*) FROM plan_formats WHERE template IS NOT NULL GROUP BY 1")}
    return {"level": level, "format": where.load(level), "resolved": resolved,
            "source": where.source, "editable": where.editable,
            "next_reference": next_reference(db, resolved["naming"]["reference"]),
            "used": {t["id"]: used.get(t["id"], 0) for t in resolved["templates"]}}


# -- references ---------------------------------------------------------------------------


def reference(pattern: str, number: int) -> str:
    return pattern.replace(NUMBER, f"{number:04d}")


def next_reference(db: DB, pattern: str | None) -> str | None:
    """The reference the next new doc would get, without taking it."""
    if not pattern:
        return None
    row = db.conn.execute("SELECT next FROM doc_references WHERE pattern=?", (pattern,)).fetchone()
    return reference(pattern, row["next"] if row else 1)


def _take(c, pattern: str) -> str:
    """In the caller's transaction: the next number under ``pattern``, taken."""
    c.execute("INSERT INTO doc_references (pattern, next) VALUES (?, 1) ON CONFLICT(pattern) DO NOTHING",
              (pattern,))
    number = c.execute("SELECT next FROM doc_references WHERE pattern=?", (pattern,)).fetchone()[0]
    c.execute("UPDATE doc_references SET next=? WHERE pattern=?", (number + 1, pattern))
    return reference(pattern, number)


# -- a doc's sections ---------------------------------------------------------------------


def _slug(title: str, taken: set[str]) -> str:
    base = ("t-" + re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-"))[:28].rstrip("-") or "t"
    sid, n = base, 1
    while sid in taken:
        n += 1
        sid = f"{base}-{n}"
    return sid


def apply(template: dict | None, parts: list[dict]) -> tuple[list[dict], bool]:
    """The supervisor's ``parts`` laid out in ``template``'s order: each
    template section where the template puts it (the supervisor's own text
    when they wrote one, else an empty heading), and each section of their
    own kept right after the template section they wrote it after (ones
    before any stay first). Returns the sections and whether that changed them."""
    if not template:
        return parts, False
    wanted = [_key(t["title"]) for t in template["sections"]]
    matched: dict[str, dict] = {}
    extras: dict[str | None, list[dict]] = {None: []}
    after = None
    for s in parts:
        k = _key(s["title"])
        if k in wanted and k not in matched:
            matched[k] = s
            after = k
        else:
            extras.setdefault(after, []).append(s)
    taken = {s["id"] for s in parts}
    out = list(extras[None])
    for t, k in zip(template["sections"], wanted):
        if k not in matched:
            sid = _slug(t["title"], taken)
            taken.add(sid)
            matched[k] = {"id": sid, "title": t["title"], "kind": "markdown", "markdown": "", "caption": None}
        out += [matched[k], *extras.get(k, [])]
    return out, [s["id"] for s in out] != [s["id"] for s in parts]


def missing(template: dict | None, sections: list[dict]) -> list[str]:
    """The template's required sections that are absent or empty in ``sections``."""
    if not template:
        return []
    filled = {_key(s["title"]) for s in sections if (s.get("markdown") or "").strip()}
    return [t["title"] for t in template["sections"] if t["required"] and _key(t["title"]) not in filled]


# -- a plan's format ----------------------------------------------------------------------


def start(c, plan_id: str, template: dict | None, pattern: str | None, at: float | None = None) -> str | None:
    """In the transaction that creates plan ``plan_id``: keep the template
    it was written with and give it the next reference. Returns the reference."""
    if template is None and not pattern:
        return None
    ref = _take(c, pattern) if pattern else None
    c.execute("INSERT OR IGNORE INTO plan_formats (plan_id, template, reference, created_at) VALUES (?,?,?,?)",
              (plan_id, json.dumps(template) if template else None, ref, time.time() if at is None else at))
    return ref


def of(db: DB, plan_id: str) -> dict | None:
    """The template and reference plan ``plan_id`` was written with, or None."""
    row = db.conn.execute("SELECT template, reference FROM plan_formats WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        return None
    return {"template": json.loads(row["template"]) if row["template"] else None, "reference": row["reference"]}


def reference_of(db: DB, plan_id: str) -> str | None:
    """Plan ``plan_id``'s reference (DES-0042), shown beside its title, or None."""
    return (of(db, plan_id) or {}).get("reference")


def require(db: DB, plan_id: str, action: str, sections: list[dict], team: str | None = None) -> None:
    """Refuse ``action`` (sign_off or share) on a plan whose template's
    required sections aren't all filled in ``sections``, when the format in
    force for its team (``team``, else the one it was shared with) says a
    missing one stops it."""
    kept = of(db, plan_id)
    if kept is None or not kept["template"] or action not in resolve(team or team_of(db, plan_id))["blocks"]:
        return
    gone = missing(kept["template"], sections)
    if gone:
        what = "signed off" if action == SIGN_OFF else "shared"
        raise ServiceError("required_sections_missing",
                           f"{', '.join(gone)} {'is' if len(gone) == 1 else 'are'} required by the "
                           f"{kept['template']['name']} template: fill "
                           f"{'it' if len(gone) == 1 else 'them'} in before this is {what}", 409, missing=gone)


def outline(template: dict | None, title_hint: str | None = None) -> str:
    """The template as the supervisor is told it: every section in order,
    with its kind, whether it is required and what belongs there, and the
    title hint."""
    out = f"Title: {title_hint}." if title_hint else ""
    if template:
        parts = [f"{t['title']} ({t['kind']}{', required' if t['required'] else ''})"
                 + (f": {t['hint']}" if t["hint"] else "") for t in template["sections"]]
        out += (" " if out else "") + (f"Write it to the {template['name']} template, its ## sections in this "
                                       f"order: {'; '.join(parts)}. Decisions come last; you may add sections.")
    return out


def brief(db: DB, plan_id: str, markdown: str) -> str:
    """What the supervisor is told after proposing: the plan's reference,
    the template it follows (every section, its kind and what belongs
    there), the title hint, and the required sections still empty."""
    from brindle import plan_docs

    s = summary(db, plan_id, plan_docs.from_markdown(markdown)[1])
    if s is None:
        return ""
    fmt = resolve(team_of(db, plan_id))
    out = f" Reference: {s['reference']}." if s["reference"] else ""
    kept = of(db, plan_id)["template"]
    told = outline(kept, fmt["naming"]["title_hint"])
    out += f" {told}" if told else ""
    if s["missing"]:
        out += (f" Required and still empty: {', '.join(s['missing'])}. Fill them in your next revision"
                + (": an empty one stops the sign-off." if SIGN_OFF in fmt["blocks"] else "."))
    return out


def planning_brief() -> str:
    """The default template, for a supervisor about to write a plan: empty
    without one."""
    fmt = resolve()
    told = outline(default_template(fmt), fmt["naming"]["title_hint"])
    return f" {told}" if told else ""


def summary(db: DB, plan_id: str, sections: list[dict]) -> dict | None:
    """A plan's format for the reader and the supervisor: its reference, its
    template's name and sections, and the required ones still empty."""
    kept = of(db, plan_id)
    if kept is None:
        return None
    template = kept["template"]
    return {"reference": kept["reference"], "template": template["name"] if template else None,
            "sections": template["sections"] if template else [], "missing": missing(template, sections)}
