"""Doc format (Settings › Doc format): the shapes a planning doc may take,
how docs are named, and what a missing required section stops.

A *format* is ``{templates, naming, blocks}``:

- ``templates``: ``[{id, name, default, sections: [{title, kind, required,
  hint}]}]``, at most one the default. ``kind`` is text, diagram or table;
  ``hint`` is the line the supervisor is told about what belongs there.
- ``naming``: ``{title_hint, reference}``; ``reference`` is a pattern such
  as ``DES-{number}``, numbered per pattern (DES-0001, ...).
- ``blocks``: what an empty required section stops, a subset of
  ``sign_off`` (the person's Approve, a reviewer's sign-off and the author's
  Freeze) and ``share`` (sharing it to the Library). Writing and review are
  never stopped.

Formats are kept per *level*, each setting only the keys it names; the
format in force is resolved down the levels, the nearest level that sets a
key deciding it. A :class:`FormatStore` keeps them:

- :class:`LocalStore`, a solo person's: ``doc_format`` in their
  ``~/.brindle/config.json``, levels ``org`` and ``team:<id>`` (a team's keys
  over the org's). A doc follows the team it was shared with (:func:`team_of`),
  and references are numbered on this machine.
- :class:`ProStore`, a Team or Enterprise org's: kept by the Library
  (pawdelta-web's backend/pro_api/doc_formats.py) per level of the org's
  structure (``org`` and its node ids). A shared doc follows its design's
  format there, through its folder's levels, and references are taken from
  the Library's counter, so two people never get the same one. While the
  Library can't be read an org has no format: never another org's, nor the
  person's own local one (fail closed).

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
from urllib.parse import quote

from brindle import config
from brindle.db import DB
from brindle.service.errors import ServiceError

KEY = "doc_format"                 # ~/.brindle/config.json
ORG = "org"
TEAM = "team:"
KINDS = ("text", "diagram", "table")
SIGN_OFF, SHARE = "sign_off", "share"
BLOCKS = (SIGN_OFF, SHARE)
FIELDS = ("templates", "naming", "blocks")
NUMBER = "{number}"
MAX_TEMPLATES = 20
MAX_SECTIONS = 30
MAX_NAME = 80
MAX_HINT = 300
MAX_PATTERN = 40
_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_PATTERN = re.compile(r"[A-Za-z0-9_\-./ ]*")
_LEVEL = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
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
    unknown = set(raw) - set(FIELDS)
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


def _checked(found: Any) -> dict:
    if not isinstance(found, dict):
        return {}
    try:
        return check(found)
    except ServiceError:
        return {}       # a stored value that no longer checks sets nothing


def _default() -> dict:
    return {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DEFAULT.items()}


def _level(level: Any) -> str:
    if isinstance(level, str) and _LEVEL.fullmatch(level):
        return level
    raise ServiceError("invalid", "level is org, team:<id> or a level of your organization's structure", 400)


# -- the stores ---------------------------------------------------------------------------


class FormatStore(Protocol):
    """Where the levels' formats are kept. ``source``: "local" (this person's
    own config) or "org" (their organization's, in the Library)."""

    source: str

    def level(self, level: str) -> tuple[dict, int]:
        """What ``level`` itself sets, and the version a save must name."""

    def effective(self, level: str = ORG, doc: str | None = None, folder: dict | None = None) -> dict:
        """The format in force at ``level``, for the Library design ``doc``, or
        for a design filed in Library ``folder`` (``{scope: {kind, id}, id}``)."""

    def save(self, level: str, fmt: dict, version: int | None) -> None:
        """Replace what ``level`` sets."""

    def take(self, db: DB, level: str) -> str | None:
        """The next reference under ``level``'s pattern, taken; None without one."""


class LocalStore:
    """A solo person's format: ``doc_format`` in ~/.brindle/config.json, by level."""

    source = "local"

    def _levels(self) -> dict:
        found = config.user_settings().get(KEY)
        return found if isinstance(found, dict) else {}

    def level(self, level: str) -> tuple[dict, int]:
        return _checked(self._levels().get(level)), 0

    def effective(self, level: str = ORG, doc: str | None = None, folder: dict | None = None) -> dict:
        levels = self._levels()
        out = _default()
        for name in dict.fromkeys([ORG, level]):     # the org, then the level below it
            out.update(_checked(levels.get(name)))
        return out

    def save(self, level: str, fmt: dict, version: int | None) -> None:
        def change(now: Any) -> dict:
            levels = dict(now) if isinstance(now, dict) else {}
            levels[level] = fmt
            return levels

        config.update_user(KEY, change)

    def take(self, db: DB, level: str) -> str | None:
        pattern = self.effective(level)["naming"]["reference"]
        if not pattern:
            return None
        with db.tx() as c:
            c.execute("INSERT INTO doc_references (pattern, next) VALUES (?, 1) ON CONFLICT(pattern) DO NOTHING",
                      (pattern,))
            number = c.execute("SELECT next FROM doc_references WHERE pattern=?", (pattern,)).fetchone()[0]
            c.execute("UPDATE doc_references SET next=? WHERE pattern=?", (number + 1, pattern))
        return reference(pattern, number)


class ProStore:
    """A Team or Enterprise org's format, kept by the Library per level of the
    org's structure (``/orgs/{org}/analysis-docs/format/...``). A read the
    Library doesn't answer is no format (fail closed); a save it refuses
    says why (not an admin, locked by a level above, changed meanwhile)."""

    source = "org"

    def __init__(self, org_id: str):
        self.org_id = org_id

    def _path(self, *tail: str) -> str:
        return "/".join([f"/orgs/{quote(self.org_id, safe='')}/analysis-docs/format",
                         *(quote(t, safe="") for t in tail)])

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        from brindle.pro import analysis_docs as ad

        return ad._call(method, path, body)

    def _read(self, path: str) -> dict | None:
        try:
            status, got = self._call("GET", path)
        except Exception:  # noqa: BLE001 - unreachable or signed out: no format, never someone else's
            log.warning("brindle: couldn't read the org's doc format", exc_info=True)
            return None
        if status != 200:
            log.warning("brindle: the Library answered HTTP %s for the org's doc format", status)
            return None
        return got

    @staticmethod
    def _format(got: dict | None) -> dict:
        """The Library's effective format as this module's, checked; the defaults where it says nothing."""
        out = _default()
        if got:
            out.update(_checked({k: got.get(k) for k in FIELDS}))
        return out

    def level(self, level: str) -> tuple[dict, int]:
        got = self._read(self._path("nodes", level))
        if got is None:
            return {}, 0
        sources = got.get("from") if isinstance(got.get("from"), dict) else {}
        own = _checked({k: got.get(k) for k in FIELDS if sources.get(k) == level})
        version = got.get("version")
        return own, version if isinstance(version, int) and not isinstance(version, bool) else 0

    def effective(self, level: str = ORG, doc: str | None = None, folder: dict | None = None) -> dict:
        if folder is not None:
            scope = folder.get("scope") if isinstance(folder.get("scope"), dict) else {}
            parts = (scope.get("kind"), scope.get("id"), folder.get("id"))
            if not all(isinstance(p, str) and p for p in parts):
                return self._format(None)       # not a folder the Library could name: no format
            return self._format(self._read(self._path("folders", *parts)))
        return self._format(self._read(self._path("designs", doc) if doc else self._path("nodes", level)))

    def save(self, level: str, fmt: dict, version: int | None) -> None:
        from brindle import redact
        from brindle.pro import auth

        if version is None:
            raise ServiceError("invalid", "version (the one read with the format) is required", 400)
        try:
            status, got = self._call("PUT", self._path("nodes", level), {"version": version, "format": fmt})
        except (RuntimeError, auth.TransportError) as e:
            raise ServiceError("upstream", redact.mask(str(e)) or "the Library couldn't be reached", 502) from e
        if status == 200:
            return
        code = got.get("error")         # the Library's {error, error_description, ...}
        if status == 403:
            raise ServiceError("forbidden", "only your organization's admins can change its doc format", 403)
        if status == 422 and code == "locked":
            raise ServiceError("locked", "a level above locks the template setting, so this level can't "
                               "change it", 409, locked_by=got.get("locked_by"))
        if status == 409:
            raise ServiceError("conflict", "your organization's doc format changed meanwhile: reload it", 409)
        if status == 422 and isinstance(got.get("error_description"), str):
            raise ServiceError("invalid", redact.mask(got["error_description"]), 400)
        raise ServiceError("upstream", f"the Library refused it (HTTP {status})", 502)

    def take(self, db: DB, level: str) -> str | None:
        got = None
        try:
            status, got = self._call("POST", self._path("nodes", level, "references"))
        except Exception:  # noqa: BLE001 - the doc goes without a reference rather than a made-up one
            log.warning("brindle: couldn't take a reference from the Library", exc_info=True)
            return None
        ref = got.get("reference") if status == 200 else None
        return ref if isinstance(ref, str) and ref else None


def store() -> FormatStore:
    """The store for the person signed in here: their org's when its plan
    shares design docs (Team and Enterprise), else their own local one."""
    from brindle.pro import license, shared_docs

    try:
        ent = license.current(refresh=False)
    except Exception:  # noqa: BLE001 - not signed in to brindle Pro: a solo person's own format
        return LocalStore()
    return ProStore(ent.org_id) if shared_docs.TEAM_FEATURE in ent.features else LocalStore()


# -- the levels ---------------------------------------------------------------------------


def load(level: str = ORG, where: FormatStore | None = None) -> dict:
    """What one level sets (a level that sets nothing is ``{}``)."""
    return (where or store()).level(_level(level))[0]


def save(level: str, raw: Any, where: FormatStore | None = None, version: int | None = None) -> dict:
    """Replace what ``level`` sets; returns it as checked."""
    level, fmt = _level(level), check(raw)
    (where or store()).save(level, fmt, version)
    return fmt


def resolve(level: str = ORG, where: FormatStore | None = None) -> dict:
    """The format in force at ``level``."""
    return (where or store()).effective(_level(level))


def _shared_doc(db: DB, plan_id: str | None) -> str | None:
    if not plan_id:
        return None
    row = db.conn.execute("SELECT doc_id FROM plan_shares WHERE plan_id=? AND doc_id IS NOT NULL",
                          (plan_id,)).fetchone()
    return row["doc_id"] if row else None


def team_of(db: DB, plan_id: str | None) -> str | None:
    """The team a doc was shared with (a local format's team level), or None."""
    if not plan_id:
        return None
    row = db.conn.execute("SELECT team FROM plan_formats WHERE plan_id=?", (plan_id,)).fetchone()
    return row["team"] if row else None


def set_team(db: DB, plan_id: str, team: str | None) -> None:
    """Record that plan ``plan_id`` was shared with ``team`` (a group), so its
    gates follow that team's format from now on."""
    if not team or not _LEVEL.fullmatch(team):
        return
    with db.tx() as c:
        c.execute("INSERT INTO plan_formats (plan_id, team, created_at) VALUES (?,?,?)"
                  " ON CONFLICT(plan_id) DO UPDATE SET team=excluded.team", (plan_id, team, time.time()))


def format_for(db: DB, plan_id: str | None = None, team: str | None = None, folder: dict | None = None,
               where: FormatStore | None = None) -> dict:
    """The format in force for a doc: for an org's, that of the Library
    ``folder`` it is being filed in, else its design's once shared, else the
    org's; for a solo person's, its team's (``team``, else the one it was
    shared with) over the org level."""
    where = where or store()
    if where.source == "org":
        if folder is not None:
            return where.effective(ORG, folder=folder)
        return where.effective(ORG, doc=_shared_doc(db, plan_id))
    team = team or team_of(db, plan_id)
    return where.effective(TEAM + team if team else ORG)


def default_template(fmt: dict) -> dict | None:
    """The template a new doc starts from: the one marked default, else the only one."""
    templates = fmt["templates"]
    return next((t for t in templates if t["default"]), templates[0] if len(templates) == 1 else None)


def view(db: DB, level: str = ORG) -> dict:
    """Settings › Doc format's view of ``level``: what it sets (and the version
    a save names), the format in force there, the next reference (when this
    machine numbers them), and how many docs each template wrote."""
    level, where = _level(level), store()
    own, version = where.level(level)
    resolved = where.effective(level)
    used = {r[0]: r[1] for r in db.conn.execute(
        "SELECT json_extract(template, '$.id'), count(*) FROM plan_formats WHERE template IS NOT NULL GROUP BY 1")}
    return {"level": level, "format": own, "version": version, "resolved": resolved, "source": where.source,
            "editable": True,
            "next_reference": next_reference(db, resolved["naming"]["reference"]) if where.source == "local" else None,
            "used": {t["id"]: used.get(t["id"], 0) for t in resolved["templates"]}}


# -- references ---------------------------------------------------------------------------


def reference(pattern: str, number: int) -> str:
    return pattern.replace(NUMBER, f"{number:04d}")


def next_reference(db: DB, pattern: str | None) -> str | None:
    """The reference this machine's next new doc would get, without taking it."""
    if not pattern:
        return None
    row = db.conn.execute("SELECT next FROM doc_references WHERE pattern=?", (pattern,)).fetchone()
    return reference(pattern, row["next"] if row else 1)


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


def start(c, plan_id: str, template: dict | None, pattern: str | None, at: float | None = None) -> bool:
    """In the transaction that creates plan ``plan_id``: keep the template it
    was written with. True when it is to get a reference (:func:`number`)."""
    if template is None and not pattern:
        return False
    c.execute("INSERT OR IGNORE INTO plan_formats (plan_id, template, created_at) VALUES (?,?,?)",
              (plan_id, json.dumps(template) if template else None, time.time() if at is None else at))
    return bool(pattern)


def number(db: DB, plan_id: str, where: FormatStore | None = None) -> str | None:
    """Once plan ``plan_id`` exists: take its reference (this machine's
    counter, or the org's in the Library) and keep it. A plan that can't get
    one (the Library unreachable) goes without, rather than a made-up one."""
    ref = (where or store()).take(db, ORG)
    if ref:
        with db.tx() as c:
            c.execute("UPDATE plan_formats SET reference=? WHERE plan_id=? AND reference IS NULL", (ref, plan_id))
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


def require(db: DB, plan_id: str, action: str, sections: list[dict], team: str | None = None,
            folder: dict | None = None) -> None:
    """Refuse ``action`` (sign_off or share) on a plan whose template's
    required sections aren't all filled in ``sections``, when the format in
    force for it (:func:`format_for`; ``team`` and ``folder``: where it is
    being shared to) says a missing one stops it."""
    kept = of(db, plan_id)
    if kept is None or not kept["template"] or action not in format_for(db, plan_id, team, folder)["blocks"]:
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
    fmt = format_for(db, plan_id)
    out = f" Reference: {s['reference']}." if s["reference"] else ""
    told = outline(of(db, plan_id)["template"], fmt["naming"]["title_hint"])
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
