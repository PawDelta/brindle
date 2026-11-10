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

from brindle import plan_docs, visuals
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
            "set_by": _text(rule.get("set_by")), "set_at": _number(rule.get("set_at"))}


def refusal_reason(refusal: Any) -> str:
    code = refusal.get("code") if isinstance(refusal, dict) else None
    return REFUSALS.get(code, REFUSALS["key_unavailable"])


# -- the Library's refusals --------------------------------------------------------------


def refused(db: DB, p: dict, resp: dict) -> None:
    """Keep a rule's refusal on the plan, so its gate is shut and says why.
    Any other failure says nothing about the rules and changes nothing here."""
    code = resp.get("error")
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
        refused(db, p, resp)
        raise RuntimeError(resp.get("error_description") or resp.get("error") or f"HTTP {status}")
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
    from brindle.pro import shared_docs as sd

    p = plan_docs._plan(db, plan_id)
    sd._review_open(p)
    plan_docs._on_screen(p, rev)
    body = _override_body(rev, org_id, policy, action, reason, stand_in, until, time.time() if now is None else now)
    client, store = ad._session(client, store)
    share = sd.sync(db, p["id"], client=client, store=store)
    if share["local_rev"] != p["rev"]:
        raise RuntimeError(f"r{p['rev']} hasn't reached the Library yet, so its review can't be overridden")
    answer = ask(db, p, "POST", sd._path(share["doc_id"], "/policy-overrides"), {**body, "rev": share["remote_rev"]},
                 client=client, store=store)
    if sd._remember(db, p["id"], answer):
        plan_docs._changed(db, p, p["rev"])
    return sd.link(db, p["id"])
