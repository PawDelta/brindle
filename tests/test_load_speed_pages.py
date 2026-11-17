"""The desktop app's pages open without slow work on the request path
(scripts/bench_pages.py times each page's routes on a copy of the DB)."""

import threading
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from brindle import git as git_mod
from brindle import org_permissions
from brindle.daemon import routes_actions as actions
from brindle.daemon import routes_visuals
from brindle.pro import analysis_docs
from brindle.pro import library_folders as folders
from brindle.pro import orgkey
from brindle.pro import work_items as wi

SHA = "a" * 40
SCOPES = [{"kind": "user", "id": "u1", "name": "Me"}, {"kind": "group", "id": "t1", "name": "Team"},
          {"kind": "org", "id": "o1", "name": "Org"}]


def _docs(monkeypatch, api):
    monkeypatch.setattr(actions, "docs_scopes", lambda repo=None: SCOPES)
    monkeypatch.setattr(actions, "docs_api", lambda m, p, b=None, **kw: api(kw.get("scope")))
    return [d for got in actions.docs_everywhere("/analysis-docs", "/r", {}) for d in got["docs"]]


def _folders(monkeypatch, api):
    return [g["folders"][0]["id"] for g in folders.list_all(SCOPES, api=lambda m, p, b=None: {
        "folders": [{"id": api(p.split("scope_id=")[1])["docs"][0]}]})]


def _policies(monkeypatch, api):
    def org_api(method, tail, body=None, *, repo=None, org_id=None):
        return {"org_id": "o1"} if tail == "review-policies" else api(tail)["docs"][0]

    monkeypatch.setattr(actions, "docs_org_api", org_api)
    request = Request({"type": "http", "method": "GET", "path": "/v1/review-policies", "query_string": b"",
                       "headers": []})
    got = routes_visuals.review_policies_read.__wrapped__(None, request)
    return [got["directory"], got["in_review"]]


def _org_approvals(monkeypatch, api):
    seen = {}

    class Account:
        ent = SimpleNamespace(sub="u1", org_id="o1", role="admin", policy_role=None, plan="team")
        policy_reason = org_permissions.Account.policy_reason

        def policy(self):
            seen["policy"] = api("policy")["docs"][0]
            return {"version": 1, "policy": {}}

        def roles(self):
            seen["roles"] = api("roles")["docs"][0]
            return []

        def call(self, method, path, body=None):
            seen["members"] = api("members")["docs"][0]
            return {"members": []}

    monkeypatch.setattr(org_permissions, "Account", Account)
    org_permissions.snapshot()
    return [seen["policy"], seen["roles"], seen["members"]]


def _doc_switches(monkeypatch, api):
    seen = {}

    def library(method, path, body=None, **kw):
        seen[path] = api(path)["docs"][0]
        return {"switches": {}, "scopes": []}

    def policy(org_id, client, store):
        seen["policy"] = api("policy")["docs"][0]
        return {"switches": {}, "groups": {}}

    monkeypatch.setattr(analysis_docs, "_session", lambda client, store: (None, None))
    monkeypatch.setattr(analysis_docs, "require_entitlement", lambda store: SimpleNamespace(
        features={"analysis_docs_team"}, org_id="o1", plan="team"))
    monkeypatch.setattr(analysis_docs, "api", library)
    monkeypatch.setattr(analysis_docs.analysis_policy, "fetch", policy)
    analysis_docs.switch_states()
    return [seen["/me/analysis-docs/settings"], seen["policy"], seen["/analysis-docs/scopes"]]


@pytest.mark.parametrize("read, expected", [
    (_docs, ["user", "team:t1", "org:o1"]),
    (_folders, ["u1", "t1", "o1"]),
    (_policies, ["review-policies/directory", "review-policies/in-review"]),
    (_org_approvals, ["policy", "roles", "members"]),
    (_doc_switches, ["/me/analysis-docs/settings", "policy", "/analysis-docs/scopes"]),
], ids=["library docs", "library folders", "review policies", "org approvals", "doc switches"])
def test_library_pages_ask_the_server_at_once(read, expected, monkeypatch):
    """The Library's docs and folders are one server round trip per scope the
    person reads; Settings › Review policies two after its rules, Org
    approvals three and the doc switches three, none waiting on another:
    those are asked at once, and the answers keep their order. (Asked one
    after another, the first ask would wait on the others at the barrier and
    break it.)"""
    together = threading.Barrier(len(expected))

    def api(scope):
        together.wait(5)
        return {"docs": [scope]}

    assert read(monkeypatch, api) == expected


def _checkout(path, head):
    """A checkout's git files on ``head`` (what brindle reads without git)."""
    (path / ".git" / "refs" / "heads").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (path / ".git" / "refs" / "heads" / "main").write_text(head + "\n")


def test_integrations_page_reads_each_repos_identity_once(tmp_path, monkeypatch):
    """Settings › Integrations maps every repo brindle knows: its root commit
    (a whole-history git walk) is asked once per HEAD, and again when the
    history it is asked of changes: a new commit, a deepened clone, another
    repo at that path."""
    roots = []
    for name in ("alpha", "beta"):
        _checkout(tmp_path / name, "1" * 40)
        roots.append(str(tmp_path / name))
    asked = []

    def git(root, *args):
        asked.append(root)
        return SHA + "\n"

    monkeypatch.setattr(orgkey, "_git", git)
    monkeypatch.setattr(git_mod, "_ONCE", {})
    monkeypatch.setattr(orgkey.OrgKeys, "get", lambda self, org: orgkey.OrgKey(org, "k1", b"\x01" * 32))

    first = wi.local_repos(roots, "org_1")
    assert sorted(asked) == roots
    asked.clear()
    assert wi.local_repos(roots, "org_1") == first
    assert asked == []

    beta = tmp_path / "beta"
    (beta / ".git" / "refs" / "heads" / "main").write_text("2" * 40 + "\n")   # a new commit (an amended root, a merge)
    wi.local_repos(roots, "org_1")
    assert asked == [roots[1]]
    asked.clear()
    (beta / ".git" / "shallow").write_text("3" * 40 + "\n")                   # the clone deepened or made shallow
    wi.local_repos(roots, "org_1")
    assert asked == [roots[1]]
    asked.clear()
    (beta / ".git").rename(tmp_path / "old-git")                              # another repo at the same path
    _checkout(beta, "4" * 40)
    wi.local_repos(roots, "org_1")
    assert asked == [roots[1]]
