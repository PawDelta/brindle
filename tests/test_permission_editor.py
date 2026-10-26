"""Real matching and atomic permission editor transactions."""

from dataclasses import replace

import pytest

from brindle import permissions as p
from brindle.service.errors import ServiceError


def spec(match="deploy", decision="allow", **kw):
    return {"kind": "bash", "match": match, "match_type": "prefix", "decision": decision, **kw}


def test_scoped_exclusions_and_ask_reach_every_matching_path(monkeypatch):
    monkeypatch.setattr(p.time, "time", lambda: 100)
    rule = p.Rule.from_dict(spec(repo_root="/repo", expires_at=200,
                               exclusions=[spec("deploy prod", "deny")]))
    req = p.Request("codex", "bash", "Bash", repo_root="/repo", command="deploy prod")
    assert p.decide(req, rules=[rule]).decision == "ask"
    assert p.decide(replace(req, command="deploy dev"), rules=[rule]).decision == "allow"
    assert p.decide(replace(req, command="deploy dev", repo_root=""), rules=[rule]).decision == "ask"
    assert p.decide(replace(req, command="deploy dev", repo_root="/other"), rules=[rule]).decision == "ask"
    ask = p.Rule.from_dict(spec("deploy prod", "ask"))
    broad = p.Rule.from_dict(spec())
    for command in ("deploy prod", "deploy dev && deploy prod", 'deploy prod "some value"'):
        request = replace(req, command=command)
        assert p.decide(request, rules=[broad, ask]).decision == "ask"
        assert p.allowlist_decision(request, [broad, ask]).decision != "allow"
    request = replace(req, command="deploy dev && deploy prod")
    assert p.decide(request, rules=[broad, ask]).rule == ask
    whole_ask = p.Rule("bash", request.command, "exact", "ask")
    assert p.allowlist_decision(request, [broad, whole_ask]).rule == whole_ask
    requests = [replace(req, kind="edit", path="/unknown"),
                replace(req, kind="edit", path="/private")]
    explicit = p.Rule("edit", "/private", "exact", "ask")
    assert p.decide_all(requests, rules=[explicit]).rule == explicit
    file_request = replace(req, kind="write", path="/repo/file")
    file_ask = p.Rule("write", "*", "glob", "ask")
    answer = p.decide(file_request, rules=[file_ask])
    assert p.agy_output(answer, file_request, ["/repo"])["decision"] == "ask"
    assert p.agy_strict_output(answer, file_request, ["/repo"])["decision"] == "deny"
    assert p.mirror_agy([rule, ask, broad], [])["allow"] == []
    monkeypatch.setattr(p.time, "time", lambda: 201)
    assert p.decide(replace(req, command="deploy dev"), rules=[rule]).decision == "ask"
    from brindle import permission_editor as e

    p.reset()
    protected = next(r for r in p.DEFAULT_RULES if r.id == "d-git-push")
    allow = p.Rule("bash", "deploy", "prefix", "allow")
    req = p.Request("codex", "bash", "Bash", command="deploy git push")
    assert p.rule_matches(protected, req) and p.rule_matches(allow, req)
    assert e.overlap(protected, allow) != "no"
    scoped = replace(allow, exclusions=(protected,))
    with p.editing() as store:
        store.rules = [scoped]
    [restored] = p.load_store().rules
    assert not p.rule_matches(restored, req)
    assert p.rule_matches(restored, replace(req, command="deploy dev"))
    builtin_ask = replace(protected, decision="ask")
    assert p.mirror_agy([builtin_ask], []) == {"allow": [], "deny": []}


def assert_shadow_resolution(tmp_path, monkeypatch, match, protected):
    import json
    from brindle import permission_editor as e

    monkeypatch.setattr(p.time, "time", lambda: 100)
    repo = str(tmp_path)
    p.reset()
    temporary = protected is not None
    old = p.add_rule("bash", "deploy", "allow", "exact" if temporary else "prefix")
    if protected == "org":
        from brindle.pro.team_policy import GrantPolicy, OrgPolicy

        monkeypatch.setattr(e, "policy", lambda repo: OrgPolicy("org", 1, permission_grants=GrantPolicy(
            True, ("bash",), (), 30, 2, deny=("bash exact deploy",))))
    elif protected:
        path = p.repo_rules_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"rules": [spec("deploy", "deny", match_type="exact", expires_at=200)]}))
    rule = (spec("unrelated", "deny", match_type="exact") if protected else
            spec("deploy", "ask", match_type="exact", repo_root=repo, expires_at=200)
            if temporary else spec(match, "ask"))
    before = e.snapshot(repo)
    body = {"repo": repo, "revision": before["revision"], "operation": "upsert",
            "rule": rule, "resolution": "auto"}
    review = e.review(body)
    assert review["can_save"], review
    removed = not temporary and match == "deploy"
    if temporary:
        assert not any(c["type"] == "removed" for c in review["changes"])
    else:
        assert any(c["type"] == ("removed" if removed else "updated") and c["id"] == old.id
                   for c in review["changes"])
    assert p.load_store().rules == [old]
    e.save({**body, "proposal": review["proposal"]})
    rules = p.load_store().rules
    if temporary:
        assert any(r.id == old.id for r in rules)
        request = p.Request("claude", "bash", "Bash", repo_root=repo, command="deploy")
        assert p.decide(request, rules=rules).decision == "ask"
        assert p.decide(replace(request, repo_root="/elsewhere"), rules=rules).decision == "allow"
        monkeypatch.setattr(p.time, "time", lambda: 201)
        assert p.decide(request, rules=rules).decision == ("ask" if protected == "org" else "allow")
        return
    assert (len(rules) == 1) is removed
    assert p.decide(p.Request("claude", "bash", "Bash", command="deploy prod"), rules=rules).decision == "ask"
    assert p.decide(p.Request("claude", "bash", "Bash", command="deploy dev"), rules=rules).decision == (
        "ask" if removed else "allow")


def test_save_rechecks_policy_inside_store_lock_and_permission_edit_unblocked(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from brindle import permission_editor as e

    repo = str(tmp_path)
    p.reset()
    body = {"repo": repo, "revision": e.snapshot(repo)["revision"], "operation": "upsert",
            "rule": spec(match_type="exact"), "resolution": "manual"}
    review = e.review(body)
    with pytest.raises(ServiceError) as tampered:
        e.save({**body, "proposal": "invented"})
    assert tampered.value.http_status == 409
    original = p.editing

    @contextmanager
    def changed_at_lock():
        with original() as store:
            path = p.repo_rules_path(repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"rules":[{"kind":"bash","match":"deploy","decision":"deny"}]}')
            yield store

    monkeypatch.setattr(p, "editing", changed_at_lock)
    with pytest.raises(ServiceError) as stale:
        e.save({**body, "proposal": review["proposal"]})
    assert stale.value.http_status == 409
    assert p.load_store().rules == []
    monkeypatch.setattr(p, "editing", original)

    # Uncertain overlap and protected rules cannot authorize a save either.
    p.reset()
    p.add_rule("bash", "deploy * prod", "deny", "glob")
    body = {"repo": repo, "revision": e.snapshot(repo)["revision"], "operation": "upsert",
            "rule": spec("deploy * dev", "allow", match_type="glob"), "resolution": "auto"}
    result = e.review(body)
    assert not result["can_save"] and any(c["overlap"] == "unknown" for c in result["conflicts"])
    body.update(rule=spec("git push", "allow"), resolution="new")
    with pytest.raises(ServiceError):
        e.review(body)
    body.update(operation="remove", id="d-git-push", resolution="manual")
    with pytest.raises(ServiceError):
        e.review(body)
    import json
    from brindle.pro.team_policy import GrantPolicy, OrgPolicy

    p.reset()
    deny = p.add_rule("bash", "deploy", "deny", "exact")
    path = p.repo_rules_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rules": [deny.to_dict()]}))
    state = e.snapshot(str(tmp_path))
    assert len({r["id"] for r in state["rules"]}) == len(state["rules"])
    body = {"repo": str(tmp_path), "revision": state["revision"], "id": deny.id,
            "operation": "upsert", "rule": spec("deploy", "allow", match_type="exact"),
            "resolution": "new"}
    with pytest.raises(ServiceError):
        e.review(body)
    body["resolution"] = "auto"
    reviewed = e.review(body)
    assert reviewed["can_save"]
    e.save({**body, "proposal": reviewed["proposal"]})
    [remaining] = p.load_store().rules
    assert remaining.exclusions[0].repo_root == str(tmp_path)
    assert p.decide(p.Request("codex", "bash", "Bash", repo_root=str(tmp_path),
                              command="deploy")).decision == "deny"
    deny = p.add_rule("bash", "deploy", "deny", "exact")
    managed = OrgPolicy("org", 7, permission_grants=GrantPolicy(
        True, ("bash",), (), 30, 2, deny=("bash exact deploy",)))
    monkeypatch.setattr(e, "policy", lambda repo: managed)
    state = e.snapshot(str(tmp_path))
    assert len({r["id"] for r in state["rules"]}) == len(state["rules"])
    body.update(revision=state["revision"], id=deny.id)
    with pytest.raises(ServiceError) as locked:
        e.review(body)
    assert locked.value.http_status == 403
    body.update(id=None, rule=spec("audit", "deny", match_type="exact"), resolution="auto")
    reviewed = e.review(body)
    assert reviewed["can_save"]
    e.save({**body, "proposal": reviewed["proposal"]})
    assert p.decide(p.Request("codex", "bash", "Bash", command="audit")).decision == "deny"
    # Narrow an existing local allow without publishing or weakening org policy,
    # even beside an allow that overlaps a built-in deny (which always wins).
    p.add_rule("bash", "git ", "allow", "prefix")
    allow = p.add_rule("fetch", "https://", "allow", "prefix")
    body.update(revision=e.snapshot(str(tmp_path))["revision"], id=allow.id,
                rule={"kind": "fetch", "match": "https://safe.example/", "match_type": "prefix",
                      "decision": "allow"})
    reviewed = e.review(body)
    e.save({**body, "proposal": reviewed["proposal"]})
    assert p.decide(p.Request("codex", "fetch", "WebFetch", url="https://other.example/")).decision == "ask"


@pytest.mark.parametrize("count,depth,valid,match,protected", [
    (None, 1, True, None, None), (100, 0, True, None, None), (101, 0, False, None, None),
    (1, 7, True, None, None), (1, 8, False, None, None),
    (None, None, True, "deploy", None), (None, None, True, "deploy prod", None),
    (None, None, True, "deploy", False), (None, None, True, "deploy", True),
    (None, None, True, "deploy", "org"),
])
def test_auto_resolution_preserves_a_peer_with_existing_exclusions(tmp_path, monkeypatch, count, depth, valid, match, protected):
    from brindle import permission_editor as e

    if match is not None:
        assert_shadow_resolution(tmp_path, monkeypatch, match, protected)
        return
    nested = ()
    for index in range(depth):
        nested = (p.Rule("fetch", f"https://private.test/except/{index}", "exact", "deny", exclusions=nested),)
    with p.editing() as store:
        store.rules = ([p.Rule("fetch", "https://", "prefix", "ask", created=50,
                              exclusions=(p.Rule("fetch", "https://public.test", "exact", "deny"),))]
                       if count is None else [
                           p.Rule("fetch", f"https://private.test/{index}", "exact", "deny", exclusions=nested)
                           for index in range(count)])
    original = p.load_store().rules
    before = p.store_path().read_bytes()
    body = {"repo": str(tmp_path), "revision": e.snapshot(str(tmp_path))["revision"], "operation": "upsert",
            "rule": {"kind": "fetch", "match": "https://", "match_type": "prefix", "decision": "allow"},
            "resolution": "auto"}
    if not valid:
        with pytest.raises(ServiceError):
            e.review(body)
        with pytest.raises(ServiceError):
            e.save({**body, "proposal": "unrepresentable"})
        assert p.store_path().read_bytes() == before
        return
    reviewed = e.review(body)
    assert reviewed["can_save"]
    e.save({**body, "proposal": reviewed["proposal"]})
    rules = p.load_store().rules
    assert len(rules) == len(original) + 1
    assert all(rule in rules for rule in original)
    assert rules == [p.Rule.from_dict(r) for r in reviewed["rules"] if r["editable"]]
    assert p.decide(p.Request("codex", "fetch", "WebFetch", url="https://public.test")).decision == "allow"
    assert p.decide(p.Request("codex", "fetch", "WebFetch", url="https://private.test/0")).decision == (
        "ask" if count is None else "deny")


@pytest.mark.parametrize("match_type,match", [
    ("prefix", "mcp__legacy__"),
    pytest.param("glob", "mcp__legacy__*", marks=pytest.mark.variant("persisted legacy MCP glob")),
    pytest.param("exact", "mcp__legacy__tool", marks=pytest.mark.variant("tool removed from inventory")),
])
def test_routes_inventory_verification_edit_remove_and_restrictions(db, tmp_path, monkeypatch, match_type, match):
    from test_daemon_actions import _send
    from brindle import permission_editor as e
    from brindle.policy import Decision

    repo = str(tmp_path)
    p.reset()
    status, state = _send("GET", f"/v1/permission-editor?repo={repo}")
    assert status == 200
    tool = next(c["tool"] for c in state["catalog"] if c["kind"] == "mcp")
    assert _send("POST", "/v1/permission-editor/verify", {"tool": tool})[1]["verified"]
    assert not _send("POST", "/v1/permission-editor/verify", {"tool": "mcp__missing__invented"})[1]["verified"]
    body = {"repo": repo, "revision": state["revision"], "operation": "upsert",
            "rule": spec("build", "deny"), "resolution": "manual"}
    status, review = _send("POST", "/v1/permission-editor/review", body)
    assert status == 200 and review["can_save"]
    status, saved = _send("POST", "/v1/permission-editor/save", {**body, "proposal": review["proposal"]})
    assert status == 200
    assert saved["revision"] == e.snapshot(repo)["revision"], (saved, e.snapshot(repo))
    p.reset()
    old = p.add_rule("bash", "build", "deny", "prefix")
    body.update(revision=e.snapshot(repo)["revision"], id=old.id, rule=spec("build prod", "deny"))
    reviewed = e.review(body)
    assert [(c["type"], c["id"]) for c in reviewed["changes"]] == [("updated", old.id)]
    e.save({**body, "proposal": reviewed["proposal"]})
    assert p.load_store().rules[0].match == "build prod"
    # Recreating the original pattern must not overwrite the edited rule,
    # whose stable ID was originally derived from that pattern.
    recreated = p.add_rule("bash", "build", "deny", "prefix")
    assert recreated.id != old.id
    assert p.add_rule("bash", "build", "deny", "prefix").id == recreated.id
    assert {r.match for r in p.load_store().rules} == {"build", "build prod"}
    assert len(p.load_store().rules) == 2
    body.update(revision=e.snapshot(repo)["revision"], operation="remove", id=old.id)
    reviewed = e.review(body)
    e.save({**body, "proposal": reviewed["proposal"]})
    body.update(revision=e.snapshot(repo)["revision"], operation="remove", id=p.load_store().rules[0].id)
    reviewed = e.review(body)
    e.save({**body, "proposal": reviewed["proposal"]})
    assert not p.load_store().rules
    legacy = p.add_rule("mcp", match, "allow", match_type)
    monkeypatch.setattr(e.time, "time", lambda: 100)
    body.update(revision=e.snapshot(repo)["revision"], operation="upsert", id=legacy.id,
                rule={**legacy.to_dict(), "decision": "ask", "expires_at": 200})
    reviewed = e.review(body)
    e.save({**body, "proposal": reviewed["proposal"]})
    matched = p.decide(p.Request("codex", "mcp", "mcp__legacy__tool"))
    assert matched.decision == "ask" and matched.rule.id == legacy.id
    assert p.load_store().rules[0].expires_at == 200
    body.update(revision=e.snapshot(repo)["revision"], rule={**body["rule"], "match": "mcp__unknown__tool"})
    with pytest.raises(ServiceError):
        e.review(body)
    p.forget(legacy.id)
    # A real preset is directly reviewable, including its display metadata.
    body.update(revision=e.snapshot(repo)["revision"], operation="upsert", id=None,
                rule=next(r["rule"] for r in state["presets"] if r["rule"]["match_type"] == "exact"))
    assert e.review(body)["can_save"]
    body["rule"] = {"kind": "mcp", "match": tool, "match_type": "exact", "decision": "ask"}
    reviewed = e.review(body)
    e.save({**body, "proposal": reviewed["proposal"]})
    body.update(revision=e.snapshot(repo)["revision"])
    body["rule"]["decision"] = "allow"
    assert not e.review(body)["can_save"]
    body["resolution"] = "existing"
    reviewed = e.review(body)
    assert reviewed["changes"] == []
    e.save({**body, "proposal": reviewed["proposal"]})
    assert p.decide(p.Request("claude", "mcp", tool)).decision == "ask"
    body["resolution"] = "new"
    reviewed = e.review(body)
    assert {c["type"] for c in reviewed["changes"]} == {"added", "removed"}
    e.save({**body, "proposal": reviewed["proposal"]})
    assert p.decide(p.Request("claude", "mcp", tool)).decision == "allow"
    body.update(revision=e.snapshot(repo)["revision"])
    body["rule"]["match"] = "mcp__missing__invented"
    with pytest.raises(ServiceError):
        e.review(body)
    body.update(revision=e.snapshot(repo)["revision"], operation="upsert", id=None,
                rule=spec(roles=["admin"]))
    with pytest.raises(ServiceError):
        e.review(body)
    monkeypatch.setattr(e, "policy", lambda repo: Decision(False, "blocked"))
    assert not e.snapshot(repo)["restrictions"]["editable"]
    body.update(revision=e.snapshot(repo)["revision"], rule=spec())
    with pytest.raises(ServiceError):
        e.review(body)
