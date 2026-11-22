"""A design's share link (brindle Pro and up): a link that lets someone
outside the design's scopes read it, and comment once signed in. The
contract is docs/share-links-api.md in pawdelta-web.

Keyed by the Library's document id, like :mod:`brindle.pro.shared_reader`:
nothing is kept here, each call asks the Library. A design has at most one
link. Copying reuses the one there is unless an explicit scope selection differs,
because making one replaces the old link (which then stops working). Who may
make or revoke a link, and whether an org's policy allows links at all, is the
Library's to say; its refusal is passed on with its own reason.

Free has no Library, so it gets no link: :func:`read` says so without sending
anything, and :func:`copy` and :func:`revoke` refuse before anything is sent.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from brindle.pro import analysis_docs as ad
from brindle.pro import analysis_policy
from brindle.redact import mask
from brindle.service.errors import ServiceError

# An org's link policy refusing (share links off, or kept to its members): shown as the Library words it.
POLICY_REFUSALS = ("share_links_off", "org_only", "sign_in_required", "sso_required")
ACCESS = ("anyone", "org", "off")
NO_PLAN = "share links come with brindle Pro and up"
LINKS_OFF = "Share links are turned off by your organization's admin."


class _Turns:
    """One copy or revoke at a time per design. A copy reads the link and makes
    one only when there is none; a second copy overlapping it would make another,
    which replaces the first's, so the first's URL would already be dead.
    ``queued`` counts those waiting for a design's turn, under ``cond``."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.busy: set[str] = set()
        self.queued: dict[str, int] = {}

    @contextmanager
    def hold(self, doc_id: str) -> Iterator[None]:
        with self.cond:
            self.queued[doc_id] = self.queued.get(doc_id, 0) + 1
            self.cond.notify_all()
            self.cond.wait_for(lambda: doc_id not in self.busy)
            self.queued[doc_id] -= 1
            self.busy.add(doc_id)
        try:
            yield
        finally:
            with self.cond:
                self.busy.discard(doc_id)
                if not self.queued.get(doc_id):
                    self.queued.pop(doc_id, None)
                self.cond.notify_all()


TURNS = _Turns()


def plan_doc(db, plan_id: str, store=None) -> str | None:
    """The Library doc of a plan shared from here by the account signed in now,
    or None (never shared, refused, another account's, or no account)."""
    row = db.conn.execute("SELECT doc_id, owner, access_refused FROM plan_shares WHERE plan_id=?",
                          (plan_id,)).fetchone()
    if row is None or row["doc_id"] is None or row["access_refused"]:
        return None
    try:
        me = ad.require_entitlement(ad._session(None, store)[1]).sub
    except RuntimeError:
        return None
    return row["doc_id"] if row["owner"] == me else None


def _path(doc_id: str) -> str:
    return ad._doc_path(doc_id, "/design/share-link")


def _link(raw: Any) -> dict | None:
    """The Library's link as the app shows it, or None for none (or one this brindle can't read)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("url"), str) or not raw["url"].startswith("https://"):
        return None
    at = raw.get("created_at")
    return {"url": raw["url"],
            "created_at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
            "access": raw["access"] if raw.get("access") in ACCESS else "anyone",
            "permission": raw.get("permission") if raw.get("permission") in ("view", "edit") else "view",
            "scope": raw.get("scope") if isinstance(raw.get("scope"), dict) else None}


def _refused(status: int, resp: dict) -> ServiceError:
    code = resp.get("error") if isinstance(resp.get("error"), str) else None
    why = resp.get("error_description") if isinstance(resp.get("error_description"), str) else None
    if code in POLICY_REFUSALS:
        return ServiceError(code, mask(why or "your organization's link policy doesn't allow this link"), 403)
    if status == 403 and code in (None, "forbidden"):
        return ServiceError("forbidden", "only an editor of this plan can make or revoke its link", 403)
    if status == 404:
        return ServiceError("not_found", "the Library has no such plan", 404)
    if 400 <= status < 500 and status != 401:     # a 401 would read as the daemon's own token refused
        return ServiceError(code if code and code.isidentifier() else "upstream", mask(why or code or f"HTTP {status}"),
                            status)
    return ServiceError("upstream", mask(why or code or f"HTTP {status}"), 502)


def _ask(method: str, doc_id: str, client, store, body=None) -> dict | None:
    client, store = ad._session(client, store)
    try:
        ad.require_entitlement(store)             # Free, signed out or air-gapped: nothing is sent
    except RuntimeError as e:
        raise ServiceError("not_entitled", NO_PLAN, 402) from e
    status, resp = ad._call(method, _path(doc_id), body, client=client, store=store)
    if status != 200:
        raise _refused(status, resp)
    return _link(resp.get("link"))


def available(store=None) -> bool:
    """Whether this account's plan has share links (Pro and up)."""
    try:
        ad.require_entitlement(store)
    except RuntimeError:
        return False
    return True


def read(doc_id: str, *, client=None, store=None) -> dict:
    """Link and effective access; an org refusal is visible before making a link.

    ``available`` is false (nothing sent) below Pro. An off policy leaves
    revocation available but refuses copying, including links already made.
    """
    client, store = ad._session(client, store)
    if not available(store):
        return {"available": False, "link": None}
    access, org = _access(client, store)
    if access == "off":
        return {"available": True, "scoped": True, "link": None, "access": access, "reason": f"{LINKS_OFF} ({org})"}
    link = _ask("GET", doc_id, client, store)
    if link and link["access"] == "off":
        return {"available": True, "link": link, "access": "off", "reason": LINKS_OFF}
    if org is not None:
        return {"available": True, "scoped": True, "link": link, "access": link["access"] if link else access,
                "reason": f"Set by your organization's admin ({org})."}
    return {"available": True, "link": link}


def _access(client, store) -> tuple[str, str | None]:
    ent = ad.require_entitlement(store)
    if "analysis_docs_enterprise" not in ent.features:
        return "anyone", None
    policy = analysis_policy.fetch(ent.org_id, client, store)
    return policy.get("share_links", "org"), ent.org_id


def copy(doc_id: str, *, scope=None, team=None, permission=None, client=None, store=None) -> dict:
    """Reuse the design's link unless an explicit audience selection differs. Asked again
    in its turn, so copies that overlap all get the one link."""
    with TURNS.hold(doc_id):
        state = read(doc_id, client=client, store=store)
        if not state["available"]:
            raise ServiceError("not_entitled", NO_PLAN, 402)
        if state.get("access") == "off":
            raise ServiceError("share_links_off", state["reason"], 403)
        if scope not in (None, "org", "team") or (scope == "team" and not team) or (scope != "team" and team):
            raise ServiceError("invalid_request", "choose an organization or a team for this link", 422)
        body = {"scope": scope} if scope else None
        if team:
            body["team"] = team
        if permission is not None:
            if permission not in ("view", "edit"):
                raise ServiceError("invalid_request", "choose view or edit for this link", 422)
            body = {**(body or {}), "permission": permission}
        link = state["link"]
        existing = (link or {}).get("scope") or {}
        if not link or (scope is not None and (existing.get("kind") != scope or existing.get("team") != team)):
            link = _ask("POST", doc_id, client, store, body)
        elif permission is not None and link["permission"] != permission:
            link = _ask("PUT", doc_id, client, store, body)
    if link is None:
        raise ServiceError("upstream", "the Library made no link", 502)
    return {**state, "link": link}


def update(doc_id: str, body: dict, *, client=None, store=None) -> dict:
    """Update access without replacing the link's token or creator."""
    with TURNS.hold(doc_id):
        state = read(doc_id, client=client, store=store)
        link = _ask("PUT", doc_id, client, store,
                    {k: v for k, v in body.items() if k in ("scope", "team", "permission")})
    return {**state, "link": link}


def revoke(doc_id: str, *, client=None, store=None) -> dict:
    """Turn the link off, after any copy in flight. Anyone who already opened it keeps what they read."""
    with TURNS.hold(doc_id):
        _ask("DELETE", doc_id, client, store)
    return {"available": True, "link": None}
