"""Enterprise review policies on a shared plan (brindle Pro). The contract is
"Review policies" in pawdelta-web's docs/shared-designs-api.md.

The Library evaluates an org's rules on every read and write and answers with
one ``policy_reviewers`` row per rule covering the design; a client can't
submit or remove one. The build gate (:func:`brindle.plans.gate`) counts those
rows. This module is what stands between them and the gate:

- :func:`readable` and :func:`pending_agrees` decide whether the rows can be
  believed. A row that can't be read shuts the gate; it never drops out of it.
- :func:`ask` makes a Library call and, when an org rule refuses it or can't be
  read, records that on the plan. The gate stays shut until the Library next
  gives a whole answer, whatever was signed before.
- :func:`override` is an org admin's waiver of a rule, for this revision only,
  or a stand-in for it. The Library checks who may; the person reaches it only
  through the daemon, so no agent can waive a rule.
- :func:`row` is what the doc shows of a rule, waiver and stand-ins included.
"""

from __future__ import annotations

import json
import time
from typing import Any

from brindle import plan_docs, plans, visuals
from brindle.db import DB
from brindle.pro import analysis_docs as ad
from brindle.redact import mask
from brindle.service.errors import ServiceError

REFUSED = "policy_refused"      # the key in a plan's stored review while a rule refuses or can't be read
REFUSALS = {
    "key_unavailable": "This organization's review rules can't be read right now, so the build stays closed.",
    "policy_denied": "An org review policy refused that change, so the build stays closed until the Library "
                     "confirms the review again.",
}
ACCESS_REFUSED = (401, 402, 403, 404)   # the Library's answer that this account may not have this: not an outage
REVIEW_FIELDS = ("reviewers", "policy_reviewers", "pending_policies")
ACTIONS = ("waive", "stand_in")
MAX_REASON = 2000


# -- reading the Library's rows ----------------------------------------------------------


def _subs(raw: Any) -> bool:
    return isinstance(raw, list) and all(isinstance(s, str) for s in raw)


def _named(raw: Any) -> bool:
    return isinstance(raw, str) and bool(raw)


def _strings(raw: Any) -> list[str]:
    return [mask(s) for s in raw if isinstance(s, str)] if isinstance(raw, list) else []


def _number(raw: Any) -> float | None:
    return raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def _text(raw: Any) -> str | None:
    return mask(raw) if isinstance(raw, str) and raw else None


def readable(rule: Any) -> bool:
    """Whether a ``policy_reviewers`` row says enough to be counted."""
    if not isinstance(rule, dict) or not _named(rule.get("org_id")) or not _named(rule.get("policy")):
        return False
    approvals = rule.get("approvals")
    if not isinstance(approvals, int) or isinstance(approvals, bool) or approvals < 1:
        return False
    return _subs(rule.get("on_list")) and _subs(rule.get("met_by"))


def pending_agrees(pending: Any, rules: list[dict]) -> bool:
    """Whether ``pending_policies`` fits the rows: a rule it calls waiting can't
    be read as met. Servers from before the field don't send it."""
    if pending is None:
        return True
    if not isinstance(pending, list):
        return False
    waiting = [(r["org_id"], r["policy"]) for r in rules if r.get("status") not in ("met", "waived")]
    return all(isinstance(p, dict) and (p.get("org_id"), p.get("policy")) in waiting for p in pending)


def _waiver(rule: dict) -> dict | None:
    raw = rule.get("waiver")
    if not isinstance(raw, dict) or not _named(raw.get("by")) or not _named(raw.get("reason")):
        return None
    return {"by": raw["by"], "at": _number(raw.get("at")), "reason": mask(raw["reason"])}


def _stand_ins(rule: dict) -> list[dict]:
    raw = rule.get("stand_ins")
    return [{"stand_in": s["stand_in"], "until": s["until"], "by": _text(s.get("by")), "at": _number(s.get("at"))}
            for s in (raw if isinstance(raw, list) else [])
            if isinstance(s, dict) and _named(s.get("stand_in")) and _number(s.get("until")) is not None]


def _opened(rule: dict) -> list[dict]:
    raw = rule.get("opened_by")
    return [{"sub": s["sub"], "at": _number(s.get("at"))} for s in (raw if isinstance(raw, list) else [])
            if isinstance(s, dict) and _named(s.get("sub"))]


def backed(rule: dict) -> bool:
    """Whether a row's own fields support the status it claims. One that claims
    more than it shows is unreadable, so it counts as waiting."""
    if rule.get("status") == "met":
        return len(set(rule["met_by"])) >= rule["approvals"]
    if rule.get("status") == "waived":
        return _waiver(rule) is not None
    return True


def name(rule: dict) -> str:
    return mask(str(rule.get("name") or rule["policy"]))


def row(rule: dict, state: str, met_by: list[str], changes_by: list[str]) -> dict:
    """A rule as the doc shows it, ``state`` being the gate's verdict on it."""
    when = rule["when"] if isinstance(rule.get("when"), dict) else {}
    needs = rule["needs"] if isinstance(rule.get("needs"), dict) else {}
    eligible = rule.get("eligible")
    return {"org_id": rule["org_id"], "policy": rule["policy"], "name": name(rule), "note": _text(rule.get("note")),
            "when": _text(when.get("kind")), "label": _text(when.get("label")), "paths": _strings(when.get("paths")),
            "matched": _strings(rule.get("matched")), "needs_kind": _text(needs.get("kind")),
            "needs_name": _text(needs.get("name")), "approvals": rule["approvals"],
            "eligible": eligible if isinstance(eligible, int) and not isinstance(eligible, bool) else None,
            "status": state, "met_by": met_by, "changes_by": changes_by,
            "waiver": _waiver(rule) if state == "waived" else None, "stand_ins": _stand_ins(rule),
            "asked_at": _number(rule.get("asked_at")), "opened_by": _opened(rule),
            "set_by": _text(rule.get("set_by")), "set_at": _number(rule.get("set_at"))}


def refusal_reason(refusal: Any) -> str:
    code = refusal.get("code") if isinstance(refusal, dict) else None
    return REFUSALS.get(code, REFUSALS["key_unavailable"])


# -- the Library's refusals --------------------------------------------------------------


def seen(db: DB, p: dict, resp: dict) -> None:
    """Keep for good that an answer about this plan's review had required
    reviewers or org rules, or couldn't be read well enough to say it hadn't,
    whichever answer ends up the stored one: a read or write in between can
    show a reviewer the next answer no longer lists."""
    if not any(k in resp for k in REVIEW_FIELDS):
        return
    if plans.demands_review({k: resp.get(k) for k in REVIEW_FIELDS}):
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET review_asked=1 WHERE plan_id=? AND review_asked=0", (p["id"],))


def refused(db: DB, p: dict, resp: dict, status: int) -> None:
    """Keep the Library's refusal on the plan. An access refusal stands until
    it next gives a whole answer (shared_docs._remember), and bars the ways
    around the review; a rule's refusal also shuts the gate and says why.
    Any other failure says nothing about either and changes nothing here."""
    code = resp.get("error")
    if status in ACCESS_REFUSED:
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET access_refused=? WHERE plan_id=?",
                      (code if isinstance(code, str) and code else str(status), p["id"]))
    if code not in REFUSALS:
        return
    refusal = {"code": code, "org_id": resp.get("org_id"), "policy": resp.get("policy")}
    with db.tx() as c:
        got = c.execute("SELECT review FROM plan_shares WHERE plan_id=?", (p["id"],)).fetchone()
        review = json.loads(got["review"]) if got else None
        changed = review is not None and review.get(REFUSED) != refusal
        if changed:
            c.execute("UPDATE plan_shares SET review=? WHERE plan_id=?",
                      (json.dumps({**review, REFUSED: refusal}, sort_keys=True), p["id"]))
    if changed:
        plan_docs._changed(db, p, p["rev"])


def ask(db: DB, p: dict, method: str, path: str, body: dict | None = None, *, client, store) -> dict:
    """A Library call about plan ``p``'s review: its answer, or ``RuntimeError``
    with a readable reason, after recording a refusal by an org rule."""
    status, resp = ad._call(method, path, body, client=client, store=store)
    if status != 200:
        refused(db, p, resp, status)
        code = resp.get("error") if isinstance(resp.get("error"), str) else None
        raise ad.Refused(resp.get("error_description") or code or f"HTTP {status}", status, code)
    seen(db, p, resp)
    return resp


# -- an admin's override -----------------------------------------------------------------


def _override_body(rev: int, org_id: Any, policy: Any, action: Any, reason: Any, stand_in: Any, until: Any,
                   now: float) -> dict:
    if action not in ACTIONS:
        raise ServiceError("invalid", "action is waive or stand_in", 400)
    if not _named(org_id) or not _named(policy):
        raise ServiceError("invalid", "org_id and policy name the rule", 400)
    why = visuals._text(reason, "a reason", MAX_REASON, True)
    visuals._refuse_secrets([("your reason", why)])
    body = {"rev": rev, "org_id": org_id, "policy": policy, "action": action, "reason": why}
    if action == "waive":
        return body
    if not _named(stand_in):
        raise ServiceError("invalid", "a stand-in names a person", 400)
    if not isinstance(until, int) or isinstance(until, bool) or until <= now:
        raise ServiceError("invalid", "a stand-in needs an end time in the future", 400)
    return {**body, "stand_in": stand_in, "until": until}


def override(db: DB, plan_id: str, rev: Any, org_id: Any, policy: Any, action: Any, reason: Any, *,
             stand_in: Any = None, until: Any = None, client=None, store=None, now: float | None = None) -> dict:
    """Waive rule ``policy`` of ``org_id`` for the revision on screen (``rev``),
    or name a stand-in for it until ``until``. The revision is sent first if
    the Library doesn't have it, so a waiver is of what the admin read. Only
    the org's admins are accepted, by the Library. Returns the link."""
    return _admin_post(db, plan_id, rev, "/policy-overrides", lambda: _override_body(
        rev, org_id, policy, action, reason, stand_in, until, time.time() if now is None else now), "overridden",
        client, store)


def ask_again(db: DB, plan_id: str, rev: Any, org_id: Any, policy: Any, *, client=None, store=None) -> dict:
    """Ask the people a waiting rule counts again, on the revision on screen.
    Only the org's admins are accepted, by the Library, which answers 422
    when the rule is already met or waived. Returns the link."""
    def body() -> dict:
        if not _named(org_id) or not _named(policy):
            raise ServiceError("invalid", "org_id and policy name the rule", 400)
        return {"rev": rev, "org_id": org_id, "policy": policy}

    return _admin_post(db, plan_id, rev, "/policy-asks", body, "asked again", client, store)


def _admin_post(db: DB, plan_id: str, rev: Any, tail: str, build, done: str, client, store) -> dict:
    """An admin's write to the Library about the revision on screen. The
    revision is sent first if the Library doesn't have it, so the write is of
    what the admin read, under the Library's number for it."""
    from brindle.pro import shared_docs as sd

    p = plan_docs._plan(db, plan_id)
    sd._review_open(p)
    plan_docs._on_screen(p, rev)
    body = build()
    client, store = ad._session(client, store)
    share = sd.sync(db, p["id"], client=client, store=store)
    if share["local_rev"] != p["rev"]:
        raise RuntimeError(f"r{p['rev']} hasn't reached the Library yet, so its review can't be {done}")
    answer = ask(db, p, "POST", sd._path(share["doc_id"], tail), {**body, "rev": share["remote_rev"]},
                 client=client, store=store)
    if sd._remember(db, p["id"], answer):
        plan_docs._changed(db, p, p["rev"])
    return sd.link(db, p["id"])


# -- before a doc is shared, and the admin's settings ------------------------------------


def design_paths(rev: dict) -> list[str]:
    """The files a revision says it will change: each worker's, and each
    option's ``touches``. The Library matches rules on nothing else."""
    found = [f for w in rev.get("split") or [] for f in w.get("files") or []]
    found += [f for d in rev.get("decisions") or [] for o in d.get("options") or [] for f in o.get("touches") or []]
    return list(dict.fromkeys(f for f in found if isinstance(f, str) and f))


def preview_row(rule: Any) -> dict | None:
    """A rule that would cover a design, as the share panel shows it. None for
    a row that can't be read: it is left out, as the Library is the one to
    hold the gate once the doc is shared."""
    if not isinstance(rule, dict) or not _named(rule.get("policy")) or not _named(rule.get("org_id")):
        return None
    approvals = rule.get("approvals")
    if not isinstance(approvals, int) or isinstance(approvals, bool) or approvals < 1:
        return None
    return row({**rule, "met_by": [], "on_list": []}, "waiting", [], [])


def save_body(raw: Any, *, draft: bool = False) -> dict:
    """``{version, policies}`` for the Library's PUT (``{policies}`` for a
    ``draft`` impact check): what it filled in on a read (``needs.name``,
    ``eligible``, ``set_by``, ``set_at``) is left out."""
    if not isinstance(raw, dict):
        raise ServiceError("invalid", "the body must be an object", 400)
    if not draft and (not isinstance(raw.get("version"), int) or isinstance(raw["version"], bool)):
        raise ServiceError("invalid", "version is the number last read", 400)
    if not _named(raw.get("org_id")):
        raise ServiceError("invalid", "org_id is the organization the rules were read for", 400)
    if not isinstance(raw.get("policies"), list) or not all(isinstance(r, dict) for r in raw["policies"]):
        raise ServiceError("invalid", "policies is a list of rules", 400)
    kept = []
    for r in raw["policies"]:
        needs = r.get("needs")
        kept.append({**{k: v for k, v in r.items() if k not in ("eligible", "set_by", "set_at")},
                     **({"needs": {k: v for k, v in needs.items() if k != "name"}} if isinstance(needs, dict) else {})})
    return {"policies": kept} if draft else {"version": raw["version"], "policies": kept}
