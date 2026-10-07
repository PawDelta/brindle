"""Enterprise cost centers (the ``cost_centers`` feature; fails closed).

Two client halves, both inert without the feature:

* Attribution: spend events carry the repo as ``owner/name`` (``repo_slug``) so
  the org can tag spend with the repo's cost center (see ``team_events``).
* Approvals: when a budget refuses work, a member files ``brindle cost request``
  and, once an admin approves, the approved amount is added to that month's
  (or that goal's) limit locally. Requests are tracked in
  ``$BRINDLE_HOME/pro/cost-requests.json`` (0600) and polled on the cull pass
  (``poll``) or on demand (``brindle cost requests``). An approval covers the
  UTC month it was granted in, as on the server.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse

log = logging.getLogger(__name__)

FEATURE = "cost_centers"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
ORG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SCOPES = ("month", "goal")
MAX_USD = 100_000
MAX_TRACKED = 100
MAX_FILE = 256 * 1024
POLL_EVERY = 300.0
FILE = "cost-requests.json"


class CostCenterError(Exception):
    pass


def held() -> bool:
    """True with a verified ``cost_centers`` entitlement; any failure is False."""
    try:
        from brindle.pro import license

        return bool(license.has(FEATURE))
    except Exception:  # noqa: BLE001 - fail closed
        return False


def month_now(now: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(time.time() if now is None else now))


def repo_slug(repo_root: str) -> str | None:
    """``owner/name`` from the origin remote, or None (no remote, or an odd name)."""
    from brindle.pro.orgkey import _git, normalize_remote

    origin = _git(repo_root, "config", "--get", "remote.origin.url")
    norm = normalize_remote(origin) if origin else None
    parts = norm.split("/") if norm else []
    if len(parts) < 3:
        return None
    slug = "/".join(parts[-2:])
    return slug if REPO_RE.match(slug) else None


# -- local state -------------------------------------------------------------------------------


def _load() -> list[dict]:
    from brindle.pro._files import private_dir, read_private

    try:
        raw = read_private(private_dir() / FILE, MAX_FILE)
        data = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001 - unreadable state is empty state
        return []
    return [r for r in data if isinstance(r, dict) and isinstance(r.get("id"), str)] \
        if isinstance(data, list) else []


def _save(rows: list[dict]) -> None:
    from brindle.pro._files import private_dir, write_private

    write_private(private_dir() / FILE, json.dumps(rows[-MAX_TRACKED:]).encode())


def tracked() -> list[dict]:
    return _load()


def grants(now: float | None = None) -> list[dict]:
    """Approved requests that still apply (this UTC month's), as a list of
    ``{"scope", "goal", "usd"}``. Empty without the feature."""
    if not held():
        return []
    month = month_now(now)
    return [{"scope": r.get("scope"), "goal": r.get("goal"), "usd": float(r["amount_usd"])}
            for r in _load()
            if r.get("status") == "approved" and r.get("month") == month
            and isinstance(r.get("amount_usd"), (int, float)) and r["amount_usd"] > 0]


def month_raise(now: float | None = None) -> float:
    return sum(g["usd"] for g in grants(now) if g["scope"] == "month")


def goal_raise(goal: str | None, now: float | None = None) -> float:
    return sum(g["usd"] for g in grants(now) if g["scope"] == "goal" and goal and g["goal"] == goal)


# -- the server ---------------------------------------------------------------------------------


def _org_id() -> str:
    from brindle.pro import license

    ent = license.current(refresh=False)
    if not ORG_RE.match(ent.org_id or ""):
        raise CostCenterError("no org on your plan")
    return ent.org_id


def _call(method: str, path: str, body: dict | None = None, client=None, store=None) -> dict:
    from brindle.pro import auth, credentials

    store = store or credentials.default_store()
    if client is None:
        client = auth.Client((store.load() or {}).get("base_url"))
    try:
        status, resp = auth.authed(client, store, method, path,
                                   auth.JSONBody(body) if body is not None else None)
    except auth.AuthError as e:
        raise CostCenterError(str(e)) from e
    if status not in (200, 201):
        msg = resp.get("error_description") or resp.get("error") if isinstance(resp, dict) else None
        raise CostCenterError(f"the server said HTTP {status}" + (f": {msg}" if msg else ""))
    return resp


def request(usd: float, reason: str, repo_root: str, *, scope: str = "month", goal: str | None = None,
            client=None, store=None, now: float | None = None) -> dict:
    """File an approval request for ``usd`` more; returns the tracked record."""
    if not held():
        raise CostCenterError('cost centers are an Enterprise feature ("cost_centers"); '
                              "your plan doesn't include it. See `brindle account`.")
    if scope not in SCOPES:
        raise CostCenterError(f"scope must be one of {', '.join(SCOPES)}")
    if isinstance(usd, bool) or not 0 < usd <= MAX_USD:
        raise CostCenterError(f"--usd must be above 0 and at most {MAX_USD}")
    if scope == "goal" and not goal:
        raise CostCenterError("name the goal to raise with --goal")
    org = _org_id()
    body: dict = {"amount_usd": float(usd), "note": (reason or "").strip()[:500] or None}
    slug = repo_slug(repo_root)
    if slug:
        body["repo"] = slug
    body = {k: v for k, v in body.items() if v is not None}
    resp = _call("POST", f"/orgs/{urllib.parse.quote(org, safe='')}/cost-approvals", body, client, store)
    rid = resp.get("id")
    if not isinstance(rid, str) or not re.match(r"^[A-Za-z0-9_-]{1,64}$", rid):
        raise CostCenterError("the server's answer had no request id")
    rec = {"id": rid, "org_id": org, "amount_usd": float(usd), "scope": scope, "goal": goal,
           "status": str(resp.get("status") or "pending"), "month": resp.get("month"),
           "cost_center": resp.get("cost_center"), "reason": body.get("note"),
           "asked_at": time.time() if now is None else now}
    _save(_load() + [rec])
    return rec


def poll(*, client=None, store=None, only_due: bool = False, now: float | None = None) -> list[str]:
    """Ask the server about each pending request; approved ones start raising
    the limit. One line per change. Never raises."""
    now = time.time() if now is None else now
    done: list[str] = []
    try:
        if not held():
            return done
        rows = _load()
        pending = [r for r in rows if r.get("status") == "pending"]
        if only_due:
            pending = [r for r in pending if now - float(r.get("polled_at") or 0) >= POLL_EVERY]
        for r in pending:
            r["polled_at"] = now
            try:
                resp = _call("GET", f"/orgs/{urllib.parse.quote(r['org_id'], safe='')}/cost-approvals/"
                                    f"{urllib.parse.quote(r['id'], safe='')}", None, client, store)
            except CostCenterError as e:
                log.info("brindle: cost request %s not checked (%s)", r["id"], e)
                continue
            status = resp.get("status")
            if status in ("approved", "denied"):
                r["status"] = status
                r["month"] = resp.get("month") or r.get("month")
                r["decision_note"] = resp.get("decision_note")
                if status == "approved":
                    amt = resp.get("amount_usd")
                    if isinstance(amt, (int, float)) and not isinstance(amt, bool) and amt > 0:
                        r["amount_usd"] = float(amt)
                    done.append(f"[brindle cost] request {r['id']} approved: +${r['amount_usd']:.2f} "
                                f"on the {r['scope']} budget for {r['month']}")
                else:
                    done.append(f"[brindle cost] request {r['id']} was denied")
        if pending:
            _save(rows)
    except Exception:  # noqa: BLE001 - a poll never breaks the cull pass
        log.info("brindle: cost request poll failed", exc_info=True)
    return done


def describe(rows: list[dict]) -> str:
    if not rows:
        return "no cost requests"
    out = []
    for r in rows:
        extra = f" ({r['decision_note']})" if r.get("decision_note") else ""
        out.append(f"{r['id']}  {r.get('status', '?'):9} ${float(r.get('amount_usd') or 0):.2f} "
                   f"{r.get('scope', 'month')}{extra}  {r.get('reason') or ''}".rstrip())
    return "\n".join(out)


def refusal_hint() -> str:
    """The sentence appended to a budget refusal for an entitled member."""
    return (" To go over, ask your admin: `brindle cost request --usd N --reason \"...\"` "
            "(add `--goal \"<goal's first line>\"` to raise a goal's budget).") if held() else ""
