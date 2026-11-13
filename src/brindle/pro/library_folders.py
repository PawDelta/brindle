"""Library folders (private, team, org): the engine's client for the server's
``/analysis-folders`` API (pawdelta-web docs/shared-designs-api.md, "Folders").

Every function takes ``api(method, path, body=None) -> dict``, the call to the
library (analysis_docs.api by default; the daemon passes docs_api so an
unentitled person gets its 402). Folder names and settings are the server's to
keep; nothing is cached here.
"""
from __future__ import annotations

from typing import Callable
from urllib.parse import quote, urlencode

Api = Callable[..., dict]

SCOPE_KINDS = ("user", "group", "org")
VISIBILITIES = ("private", "shared")
CAN_ADD = ("owner", "members")


def _default_api(method: str, path: str, body: dict | None = None) -> dict:
    from brindle.pro import analysis_docs

    return analysis_docs.api(method, path, body)


def _scope(kind: str, scope_id: str) -> dict:
    if kind not in SCOPE_KINDS or not scope_id:
        raise ValueError(f"scope kind is one of {', '.join(SCOPE_KINDS)}, with an id")
    return {"kind": kind, "id": scope_id}


def list_folders(kind: str, scope_id: str, *, api: Api = _default_api) -> list[dict]:
    """The folders the person can see in one scope, as the server lists them."""
    scope = _scope(kind, scope_id)
    got = api("GET", "/analysis-folders?" + urlencode({"scope_kind": scope["kind"], "scope_id": scope["id"]}))
    return [f for f in got.get("folders") or [] if isinstance(f, dict) and isinstance(f.get("id"), str)]


def list_all(scopes: list[dict], *, api: Api = _default_api,
             refusals: tuple[type[Exception], ...] = (RuntimeError,)) -> list[dict]:
    """``[{scope: {kind, id, name}, folders: [...]}]`` for each of ``scopes``
    (the server's scope rows). A scope the server refuses (``refusals``: a
    team without the team feature, say) is left out, unless every one
    refuses: then the first refusal is raised."""
    out, refused = [], None
    for s in scopes:
        try:
            folders = list_folders(s["kind"], s["id"], api=api)
        except refusals as e:
            refused = refused or e
            continue
        out.append({"scope": {"kind": s["kind"], "id": s["id"], "name": s.get("name")}, "folders": folders})
    if refused and not out:
        raise refused
    return out


def create(kind: str, scope_id: str, name: str, visibility: str = "private", can_add: str = "owner", *,
           api: Api = _default_api) -> dict:
    """Make a folder; the creator can always add to it."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a folder needs a name")
    if visibility not in VISIBILITIES or can_add not in CAN_ADD:
        raise ValueError(f"visibility is one of {', '.join(VISIBILITIES)}; can_add one of {', '.join(CAN_ADD)}")
    return api("POST", "/analysis-folders", {"scope": _scope(kind, scope_id), "name": name,
                                            "visibility": visibility, "can_add": can_add})


def move_doc(doc_id: str, kind: str, scope_id: str, folder_id: str, *, api: Api = _default_api) -> dict:
    """Place a design in a folder (the design keeps exactly one)."""
    if not doc_id or not folder_id:
        raise ValueError("a design and a folder are required")
    return api("PUT", f"/analysis-docs/{quote(doc_id, safe='')}/design/folder",
               {"folder": {"scope": _scope(kind, scope_id), "id": folder_id}})
