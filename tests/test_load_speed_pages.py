"""The desktop app's pages open without slow work on the request path
(scripts/bench_pages.py times each page's routes on a copy of the DB)."""

import threading

import pytest
from starlette.requests import Request

from brindle.daemon import routes_actions as actions
from brindle.daemon import routes_visuals
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


@pytest.mark.parametrize("read, expected", [
    (_docs, ["user", "team:t1", "org:o1"]),
    (_folders, ["u1", "t1", "o1"]),
    (_policies, ["review-policies/directory", "review-policies/in-review"]),
], ids=["library docs", "library folders", "review policies"])
def test_library_pages_ask_the_server_at_once(read, expected, monkeypatch):
    """The Library's docs and folders are one server round trip per scope the
    person reads, and Settings › Review policies two after its rules: those
    are asked at once, and the answers keep their order. (Asked one after
    another, the first ask would wait on the others at the barrier and break
    it.)"""
    together = threading.Barrier(len(expected))

    def api(scope):
        together.wait(5)
        return {"docs": [scope]}

    assert read(monkeypatch, api) == expected


def test_integrations_page_reads_each_repos_identity_once(tmp_path, monkeypatch):
    """Settings › Integrations maps every repo brindle knows: its root commit
    (a whole-history git walk) is asked once, and again only when the repo at
    that path is a new one (a new .git)."""
    roots = []
    for name in ("alpha", "beta"):
        (tmp_path / name / ".git").mkdir(parents=True)
        roots.append(str(tmp_path / name))
    asked = []

    def git(root, *args):
        asked.append(root)
        return SHA + "\n"

    monkeypatch.setattr(orgkey, "_git", git)
    monkeypatch.setattr(orgkey, "_ROOT_COMMITS", {}, raising=False)
    monkeypatch.setattr(orgkey.OrgKeys, "get", lambda self, org: orgkey.OrgKey(org, "k1", b"\x01" * 32))

    first = wi.local_repos(roots, "org_1")
    assert sorted(asked) == roots
    asked.clear()
    assert wi.local_repos(roots, "org_1") == first
    assert asked == []

    (tmp_path / "beta" / ".git").rename(tmp_path / "beta" / "old-git")
    (tmp_path / "beta" / ".git").mkdir()
    wi.local_repos(roots, "org_1")
    assert asked == [roots[1]]
