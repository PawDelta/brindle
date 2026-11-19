"""A design's share link (brindle Pro and up): a link that lets someone
outside the design's scopes read it, and comment once signed in. The
contract is docs/share-links-api.md in pawdelta-web.

Keyed by the Library's document id, like :mod:`brindle.pro.shared_reader`:
nothing is kept here, each call asks the Library. A design has at most one
link. Copying reuses the one there is and makes one only when there is none,
because making one replaces the old link (which then stops working). Who may
make or revoke a link, and whether an org's policy allows links at all, is the
Library's to say; its refusal is passed on with its own reason.

Free has no Library, so it gets no link: :func:`read` says so without sending
anything, and :func:`copy` and :func:`revoke` refuse before anything is sent.
"""

from __future__ import annotations

from typing import Any

from brindle.pro import analysis_docs as ad
from brindle.redact import mask
from brindle.service.errors import ServiceError

# An org's link policy refusing (share links off, or kept to its members): shown as the Library words it.
POLICY_REFUSALS = ("share_links_off", "org_only", "sign_in_required", "sso_required")
ACCESS = ("anyone", "org", "off")
NO_PLAN = "share links come with brindle Pro and up"


def _path(doc_id: str) -> str:
    return ad._doc_path(doc_id, "/design/share-link")


def _link(raw: Any) -> dict | None:
    """The Library's link as the app shows it, or None for none (or one this brindle can't read)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("url"), str) or not raw["url"].startswith("https://"):
        return None
    at = raw.get("created_at")
    return {"url": raw["url"],
            "created_at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
            "access": raw["access"] if raw.get("access") in ACCESS else "anyone"}


def _refused(status: int, resp: dict) -> ServiceError:
    code = resp.get("error") if isinstance(resp.get("error"), str) else None
    why = resp.get("error_description") if isinstance(resp.get("error_description"), str) else None
    if code in POLICY_REFUSALS:
        return ServiceError(code, mask(why or "your organization's link policy doesn't allow this link"), 403)
    if status == 403 and code in (None, "forbidden"):
        return ServiceError("forbidden", "only an editor of this design can make or revoke its link", 403)
    if status == 404:
        return ServiceError("not_found", "the Library has no such design", 404)
    if 400 <= status < 500 and status != 401:     # a 401 would read as the daemon's own token refused
        return ServiceError(code if code and code.isidentifier() else "upstream", mask(why or code or f"HTTP {status}"),
                            status)
    return ServiceError("upstream", mask(why or code or f"HTTP {status}"), 502)


def _ask(method: str, doc_id: str, client, store) -> dict | None:
    client, store = ad._session(client, store)
    try:
        ad.require_entitlement(store)             # Free, signed out or air-gapped: nothing is sent
    except RuntimeError as e:
        raise ServiceError("not_entitled", NO_PLAN, 402) from e
    status, resp = ad._call(method, _path(doc_id), client=client, store=store)
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
    """``{available, link}``: ``available`` false (nothing sent) below Pro."""
    client, store = ad._session(client, store)
    if not available(store):
        return {"available": False, "link": None}
    return {"available": True, "link": _ask("GET", doc_id, client, store)}


def copy(doc_id: str, *, client=None, store=None) -> dict:
    """The design's link to copy: the one it has, else a new one."""
    link = _ask("GET", doc_id, client, store) or _ask("POST", doc_id, client, store)
    if link is None:
        raise ServiceError("upstream", "the Library made no link", 502)
    return {"available": True, "link": link}


def revoke(doc_id: str, *, client=None, store=None) -> dict:
    """Turn the link off. Anyone who already opened it keeps what they read."""
    _ask("DELETE", doc_id, client, store)
    return {"available": True, "link": None}
