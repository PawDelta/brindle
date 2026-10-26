"""Person-only local rule editing, with conservative conflict proofs.

Reviews are deterministic proposed diffs, not writes. Saving recomputes the
proposal against the current store and external restrictions under the same
lock used by hook writers. Organization policy and provider inventories remain
owned by their existing sources; this module cannot publish organization policy.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from dataclasses import asdict, replace

from brindle import permissions as p
from brindle.policy import Decision
from brindle.service.errors import ServiceError


def policy(repo: str):
    from brindle.pro.team_policy import ProPolicy

    return ProPolicy(repo).policy()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _repo(value: object) -> str:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ServiceError("invalid", "repo must be an absolute repository path")
    return os.path.realpath(value)


def catalog() -> list[dict]:
    from brindle.antigravity import tool_names

    actions = [{"id": kind, "kind": kind, "name": kind.title(), "tool": None, "source": "engine"}
               for kind in p.KINDS if kind != "mcp"]
    # Read actual server registration; a configured server name is not tool inventory.
    actions.extend({"id": f"mcp__brindle__{name}", "kind": "mcp", "name": name,
                    "tool": f"mcp__brindle__{name}", "source": "brindle"}
                   for name in sorted(tool_names()))
    return actions


def verify(tool: str) -> dict:
    action = next((a for a in catalog() if a["tool"] == tool), None)
    return {"verified": action is not None, "action": action}


def _context(repo: str) -> tuple[list[p.Rule], dict, list[str]]:
    from brindle import config, permission_grants

    cfg = config.load_repo_config(repo)
    protected = [*p.DEFAULT_RULES, *(replace(r, id=f"repo-{r.id}", repo_root=r.repo_root or repo)
                                   for r in p.repo_rules(repo))]
    limitations = ["Member-role identity is unavailable to permission requests; role scopes cannot be saved.",
                   "Only Brindle's registered MCP inventory is available; other servers cannot be verified.",
                   "Profile restrictions are enforced by each worker and are not editable here.",
                   "OpenCode blocks explicit asks; native workers need an approval callback to prompt.",
                   "Rules are enforced only on provider paths that invoke Brindle's permission hooks.",
                   "Deny and ask rules match command text (after spacing, quoting and leading assignments are "
                   "normalized), so the same program run another way, such as /bin/rm, env rm, bash -c, "
                   "reordered flags or git -C, is not matched; they are guardrails, not a sandbox. "
                   "Commands inside $(...) are checked too, but a substitution's output is unknown, "
                   "so it needs approval unless it is a quoted git commit or tag message "
                   "(-m \"$(...)\" or --message \"$(...)\")."]
    try:
        current = policy(repo)
    except Exception as exc:  # fail closed if authoritative restrictions cannot be read
        current = Decision(False, str(exc))
    managed = not isinstance(current, Decision)
    local = isinstance(current, Decision) and current.allowed
    restrictions = {"editable": local or managed, "managed": managed,
                    "reason": current.reason if isinstance(current, Decision) else
                    "Local edits must only tighten permissions; organization policy remains read-only.",
                    "org_id": None, "policy_version": None, "permission_grants": None,
                    "protected_paths": list(cfg.protected_paths), "permission_policy": cfg.permission_policy,
                    "roles_supported": False, "repo_supported": True, "expiry_supported": True,
                    "checks": list(cfg.checks)}
    if not isinstance(current, Decision):
        restrictions.update(org_id=current.org_id, policy_version=current.version,
                            permission_grants=asdict(current.permission_grants) if current.permission_grants else None,
                            protected_paths=sorted(set([*cfg.protected_paths, *current.protected_paths])))
        if current.permission_grants:
            for spec in current.permission_grants.deny:
                rule = permission_grants.org_rule(spec)
                # This policy was resolved for this repo. It cannot establish
                # restrictions for another repo or organization in the store.
                protected.append(replace(rule, id=f"org-{rule.id}", repo_root=repo))
        limitations.append(restrictions["reason"])
    return protected, restrictions, limitations


def _row(rule: p.Rule, editable: bool = True) -> dict:
    return {**rule.to_dict(), "repo_root": rule.repo_root, "expires_at": rule.expires_at,
            "exclusions": [r.to_dict() for r in rule.exclusions],
            "editable": editable and rule.source in ("user", "learned"),
            "protected": rule.source not in ("user", "learned")}


def _state(repo: str, store: p.Store) -> tuple[dict, list[p.Rule]]:
    protected, restrictions, limitations = _context(repo)
    rows = [_row(r, restrictions["editable"]) for r in [*protected, *store.rules]]
    # Include current checks/restrictions and external denies, not just the file being edited.
    revision = _digest([rows, restrictions])
    return {"revision": revision, "rules": rows, "restrictions": restrictions,
            "limitations": limitations}, protected


def snapshot(repo: str) -> dict:
    state, _ = _state(_repo(repo), p.load_store())
    return {**state, "catalog": catalog(),
            "presets": [{"id": r.id, "name": r.describe(), "rule": _row(r)} for r in p.PRESET_RULES]}


def _pattern_covers(a: p.Rule, b: p.Rule) -> bool:
    if a.match_type == b.match_type and a.match == b.match:
        return True
    if a.match_type == "prefix" and b.match_type in ("exact", "prefix"):
        return b.match.startswith(a.match)
    return a.match_type == "glob" and b.match_type == "exact" and fnmatch.fnmatchcase(b.match, a.match)


def _scope(rule: p.Rule) -> tuple:
    """Matching scope only: ownership and edit metadata do not affect coverage."""
    return (rule.kind, rule.match_type, rule.match, rule.repo_root, rule.expires_at,
            tuple(_scope(r) for r in rule.exclusions))


def covers(a: p.Rule, b: p.Rule) -> bool:
    """A proof that a contains all of b; False also means unproven."""
    if a.kind != b.kind or (a.repo_root and a.repo_root != b.repo_root):
        return False
    if a.expires_at is not None and (b.expires_at is None or a.expires_at < b.expires_at):
        return False
    if a.exclusions:
        return _scope(a) == _scope(b)
    return _pattern_covers(a, b)


def overlap(a: p.Rule, b: p.Rule) -> str:
    """yes / no / unknown. No sampling or guesses about glob intersections."""
    if a.kind != b.kind or (a.repo_root and b.repo_root and a.repo_root != b.repo_root):
        return "no"
    if any(r.expires_at is not None and r.expires_at <= time.time() for r in (a, b)):
        return "no"
    if any(covers(x, b) for x in a.exclusions) or any(covers(x, a) for x in b.exclusions):
        return "no"
    if _pattern_covers(a, b) or _pattern_covers(b, a):
        return "yes"
    if a.match_type in p.BUILTIN_MATCHERS or b.match_type in p.BUILTIN_MATCHERS:
        builtin, other = (a, b) if a.match_type in p.BUILTIN_MATCHERS else (b, a)
        if other.kind == "bash" and other.match_type in ("exact", "prefix"):
            # These matchers inspect git calls even behind a wrapper. The
            # first command word alone cannot prove scopes disjoint.
            if builtin.match_type in ("git-push", "git-force"):
                request = p.Request("", "bash", "Bash", command=other.match)
                if p._builtin_matches(builtin, request, []):
                    return "yes"
                if other.match_type == "exact":
                    return "no"
                witness = replace(request, command=other.match + " git push --force")
                if p._builtin_matches(builtin, witness, []):
                    return "yes"
        return "unknown"
    # Path rules have literal, resolved and relative aliases. Textual disjointness
    # alone cannot prove two path scopes disjoint.
    if a.kind in p.PATH_KINDS:
        return "unknown"
    if a.match_type == "exact" or b.match_type == "exact":
        exact, other = (a, b) if a.match_type == "exact" else (b, a)
        if other.match_type == "glob":
            return "yes" if fnmatch.fnmatchcase(exact.match, other.match) else "no"
        return "no"
    if a.match_type == b.match_type == "prefix":
        return "no"
    if {a.match_type, b.match_type} == {"prefix", "glob"}:
        prefix, glob = (a, b) if a.match_type == "prefix" else (b, a)
        # A witness proves nonempty intersection; failure proves nothing.
        witness = prefix.match + glob.match.replace("*", "").replace("?", "x")
        if fnmatch.fnmatchcase(witness, glob.match):
            return "yes"
    return "unknown"


def _conflicts(rules: list[p.Rule], protected: list[p.Rule]) -> list[dict]:
    out = []
    # Built-in allows are fallback defaults, overridden by any explicit ask/deny.
    # Protected denies participate and can never be carved or deleted.
    peers = [*rules, *(r for r in protected if r.decision == "deny")]
    for i, a in enumerate(peers):
        for b in peers[i + 1:]:
            if a.decision == b.decision or (a.source not in ("user", "learned") and
                                           b.source not in ("user", "learned")):
                continue
            relation = overlap(a, b)
            if relation != "no":
                out.append({"left": a.id, "right": b.id, "overlap": relation,
                            "protected": a.source not in ("user", "learned") or
                            b.source not in ("user", "learned")})
    return out


def _draft(raw: object, repo: str, existing: p.Rule | None = None) -> p.Rule:
    if not isinstance(raw, dict):
        raise ServiceError("invalid", "rule must be an object")
    allowed = {"id", "kind", "match", "match_type", "decision", "repo_root", "expires_at", "exclusions",
               "source", "created", "editable", "protected"}
    if set(raw) - allowed:
        raise ServiceError("invalid", "unsupported rule scope or field")
    rule = p.Rule.from_dict({**raw, "id": ""}, source="user")
    if rule is None:
        raise ServiceError("invalid", "invalid rule or unsupported constraints")
    if rule.repo_root and rule.repo_root != repo:
        raise ServiceError("invalid", "rule repository must match the repository being reviewed")
    if rule.expires_at is not None and rule.expires_at <= time.time():
        raise ServiceError("invalid", "rule has already expired")
    # Inventory can disappear after a rule is saved; changing its decision or
    # expiry does not introduce a new tool target. Compare against store state,
    # not client-supplied metadata, on both review and locked save.
    same_target = existing is not None and (rule.kind, rule.match, rule.match_type) == (
        existing.kind, existing.match, existing.match_type)
    if rule.kind == "mcp" and not same_target and (
            rule.match_type != "exact" or not verify(rule.match)["verified"]):
        raise ServiceError("invalid", "choose an exact tool from the available MCP inventory")
    # Creation timestamps cannot vary between review and save or be supplied by clients.
    return replace(rule, created=0.0)


def _exclusion(rule: p.Rule) -> p.Rule:
    """Persist a scope, without an external rule's ownership or unstable metadata."""
    return replace(rule, id="", source="user", created=0.0,
                   exclusions=tuple(_exclusion(r) for r in rule.exclusions))


def _tightens(before: list[p.Rule], after: list[p.Rule]) -> bool:
    """Prove the local policy cannot become more permissive in any repo.

    Enterprise restrictions belong to the selected repository's policy, so
    they cannot justify removing a global local restriction. Unknown scope
    containment fails closed, just as unknown overlap does during resolution.
    """
    rank = {"allow": 0, "ask": 1, "deny": 2}
    # Existing asks/denies must remain covered by equally strong restrictions.
    for old in before:
        if old.decision != "allow" and not any(
                rank[new.decision] >= rank[old.decision] and covers(new, old) for new in after):
            return False
    # Every resulting allow must fit within a previous local allowance.
    return all(any(old.decision == "allow" and covers(old, new) for old in before)
               for new in after if new.decision == "allow")


def _propose(body: dict, store: p.Store) -> tuple[dict, list[p.Rule]]:
    repo = _repo(body.get("repo"))
    state, protected = _state(repo, store)
    if body.get("revision") != state["revision"]:
        raise ServiceError("conflict", "policy changed during review", 409)
    if not state["restrictions"]["editable"]:
        raise ServiceError("forbidden", state["restrictions"]["reason"], 403)
    operation, resolution, id_ = body.get("operation"), body.get("resolution", "manual"), body.get("id")
    if operation not in ("upsert", "remove") or resolution not in ("manual", "auto", "existing", "new"):
        raise ServiceError("invalid", "unsupported operation or resolution")
    if id_ is not None and not any(r.id == id_ for r in store.rules):
        raise ServiceError("forbidden", "only your editable rules can be changed", 403)
    if operation == "remove" and not id_:
        raise ServiceError("invalid", "removing a rule requires id")
    before = list(store.rules)
    after = [r for r in before if r.id != id_]
    existing = next((r for r in before if r.id == id_), None)
    draft = _draft(body.get("rule"), repo, existing) if operation == "upsert" else None
    if draft:
        # An edited rule keeps its identity even after its pattern changes.
        # New identities therefore include the reviewed revision, rather than
        # reusing a pattern ID that may now belong to a different edited rule.
        identity = id_ if id_ is not None else _digest([state["revision"], draft.to_dict()])[:16]
        draft = replace(draft, id=identity)
        after.append(draft)
    found = _conflicts(after, protected)
    if resolution == "existing":
        after = before
    elif resolution == "new" and draft:
        relevant = [c for c in found if draft.id in (c["left"], c["right"])]
        if any(c["protected"] or c["overlap"] == "unknown" for c in relevant):
            raise ServiceError("conflict", "cannot replace protected or unproven overlapping scopes", 409)
        removed = {c["right"] if c["left"] == draft.id else c["left"] for c in relevant}
        after = [r for r in after if r.id not in removed]
    elif resolution == "auto" and not any(c["overlap"] == "unknown" for c in found):
        rank = {"allow": 0, "ask": 1, "deny": 2}
        # Take all intersections from the original rules, then apply their
        # exclusions together. This keeps a chain of conflicts deterministic.
        additions: dict[str, list[p.Rule]] = {}
        all_by_id = {r.id: r for r in [*protected, *after]}
        for conflict in found:
            a, b = (all_by_id[conflict[k]] for k in ("left", "right"))
            weak, strong = sorted((a, b), key=lambda r: rank[r.decision])
            if weak.source in ("user", "learned"):
                additions.setdefault(weak.id, []).append(_exclusion(strong))
        resolved = []
        for rule in after:
            excluded = [*rule.exclusions, *additions.get(rule.id, [])]
            if any(covers(ex, rule) for ex in excluded):
                continue
            resolved.append(replace(rule, exclusions=tuple(excluded)))
        after = resolved
    # Resolution may add an exclusion level or exceed the per-rule count.
    # Check the complete serialized shape before offering a proposal; save
    # repeats this check under the store lock through this same path.
    if any(p.Rule.from_dict(rule.to_dict()) != rule for rule in after):
        raise ServiceError("conflict", "resolution exceeds supported rule persistence limits", 409)
    if state["restrictions"]["managed"] and not _tightens(before, after):
        raise ServiceError("forbidden", "managed policy permits only proven local tightening", 403)
    # A protected peer is always a deny, and denies win wherever scopes overlap
    # (permissions.decide), so it is shown but leaves nothing to resolve; no
    # edit could ever resolve it, and counting it would block every save.
    unresolved = [c for c in _conflicts(after, protected) if not c["protected"]]
    # Existing conflicts outside this edit are shown too; saving may never claim
    # to have resolved a policy that still contains contradictory scopes.
    can_save = resolution == "existing" or not unresolved
    old = {r.id: r for r in before}
    new = {r.id: r for r in after}
    changes = [{"type": "removed", "id": r.id, "before": _row(r), "after": None}
               for r in before if r.id not in new]
    changes.extend({"type": "updated" if r.id in old else "added", "id": r.id,
                    "before": _row(old[r.id]) if r.id in old else None, "after": _row(r)}
                   for r in after if r.id not in old or r != old[r.id])
    rows = [_row(r) for r in [*protected, *after]]
    proposal = _digest([state["revision"], body.get("operation"), resolution, changes, rows])
    return {"revision": state["revision"], "proposal": proposal, "rules": rows, "changes": changes,
            "conflicts": found, "unresolved": unresolved, "can_save": can_save}, after


def review(body: dict) -> dict:
    return _propose(body, p.load_store())[0]


def save(body: dict) -> dict:
    with p.editing() as store:
        result, after = _propose(body, store)
        if not result["can_save"] or body.get("proposal") != result["proposal"]:
            raise ServiceError("conflict", "review the complete resolved proposal before saving", 409)
        current, _ = _state(_repo(body.get("repo")), store)
        if current["revision"] != result["revision"]:
            raise ServiceError("conflict", "policy changed while preparing the save", 409)
        store.rules = after
        state, _ = _state(_repo(body.get("repo")), store)
    return {**state, "changes": result["changes"]}
