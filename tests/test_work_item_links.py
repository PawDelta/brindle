"""Work items on a design: the client over a fake server, cached and fail-soft, and the daemon routes."""
from types import SimpleNamespace

import pytest

from brindle.daemon import routes_actions as actions
from brindle.pro import work_items as wi
from brindle.service.errors import ServiceError

ORG, DOC = "o1", "doc/1"
BASE = "/orgs/o1/analysis-docs/doc%2F1/work-items"


class FakeServer:
    """The pawdelta-web tracker routes for one design: links in memory, states as the tracker reports them."""

    def __init__(self):
        self.links, self.down, self.state = [], False, "Active"
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.down:
            raise ServiceError("upstream", "no answer", 502)
        if (method, path) == ("GET", BASE):
            return {"doc_id": DOC, "items": [dict(l, state=self.state, done=self.state == "Closed") for l in self.links],
                    "connected": {"azure_devops": True}}
        if (method, path) == ("POST", BASE + "/links"):
            if not body["item"].startswith("AB#"):
                raise ServiceError("item_not_found", "No such item", 422)
            made = {"link_id": f"wl_{len(self.links)}", "key": body["item"][3:], "display": body["item"],
                    "target": body["target"], "state": "Active", "done": False, "created": False}
            self.links.append(made)
            return made
        if (method, path) == ("POST", BASE):
            made = [{"link_id": f"wl_{len(self.links) + n}", "key": str(4900 + n), "display": f"AB#{4900 + n}",
                     "target": {"kind": body["kind"], "id": body.get("task")}, "state": None, "done": False,
                     "created": True} for n in range(2 if body["kind"] == "task" else 1)]
            self.links += made
            return {"doc_id": DOC, "created": made}
        if method == "DELETE":
            self.links = [l for l in self.links if not path.endswith(l["link_id"])]
            return {"unlinked": True}
        raise AssertionError((method, path))


def test_link_create_and_status_shown_for_the_design():
    server = FakeServer()
    wi.link(ORG, DOC, "AB#4810", api=server)                           # the epic
    got = wi.create(ORG, DOC, "task", api=server)                      # every task without an item
    assert [i["display"] for i in got["created"]] == ["AB#4900", "AB#4901"]
    server.state = "Closed"
    read = wi.items(ORG, DOC, api=server)
    assert read["unreachable"] is False
    assert read["epic"]["display"] == "AB#4810" and read["epic"]["state"] == "Closed"
    assert (read["done"], read["total"]) == (2, 2)                     # children count, not the epic itself
    assert wi.unlink(ORG, DOC, "wl_1", api=server) == {"unlinked": True}
    assert [i["link_id"] for i in wi.cached(ORG, DOC)["items"]] == ["wl_0", "wl_2"]


def test_a_tracker_refusal_is_raised_and_never_cached():
    server = FakeServer()
    with pytest.raises(ServiceError) as err:
        wi.link(ORG, DOC, "BRN-1", api=server)
    assert err.value.http_status == 422 and wi.cached(ORG, DOC) is None
    with pytest.raises(ValueError):
        wi.link(ORG, DOC, "  ", api=server)


def test_unreachable_server_shows_the_last_read_marked_stale():
    server = FakeServer()
    wi.link(ORG, DOC, "AB#4810", api=server)
    wi.items(ORG, DOC, api=server)
    server.down = True
    got = wi.items(ORG, DOC, api=server)
    assert got["unreachable"] is True and got["epic"]["display"] == "AB#4810" and got["epic"]["stale"] is True
    assert wi.items(ORG, "never/read", api=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))["items"] == []
    # a refusal the person must see (not entitled) is not hidden behind the cache
    with pytest.raises(ServiceError):
        wi.items(ORG, DOC, api=lambda *a, **k: (_ for _ in ()).throw(ServiceError("not_entitled", "no plan", 402)))


def test_summaries_are_read_in_batches_and_refreshed_when_old():
    server, clock = FakeServer(), [1000.0]
    wi.link(ORG, DOC, "AB#4810", api=server)
    wi.items(ORG, DOC, api=server, now=lambda: clock[0])
    server.calls.clear()
    fresh = wi.summaries(ORG, [DOC], api=server, fetch=10, now=lambda: clock[0])    # fresh: nothing is sent
    assert fresh["items"][DOC]["epic"]["state"] == "Active" and server.calls == []
    server.state = "Closed"                                                           # the tracker moved on
    clock[0] += wi.FRESH_FOR + 1
    assert wi.summaries(ORG, [DOC], api=server, fetch=10, now=lambda: clock[0])["items"][DOC]["epic"]["state"] == "Closed"
    server.down, clock[0] = True, clock[0] + wi.FRESH_FOR + 1                         # unreachable: last value, stale
    assert wi.summaries(ORG, [DOC], api=server, fetch=10, now=lambda: clock[0])["items"][DOC]["stale"] is True
    # more designs than one call reads: the rest come back as remaining and are read by the next call
    server.down = False
    ids = [f"d{n}" for n in range(12)]
    seen = {}

    def api(method, path, body=None):
        seen[path] = True
        return {"items": [], "connected": {}}
    first = wi.summaries(ORG, ids, api=api, fetch=10)
    assert len(first["items"]) == 10 and len(first["remaining"]) == 2
    second = wi.summaries(ORG, ids, api=api, fetch=10)
    assert len(second["items"]) == 12 and second["remaining"] == []


def test_another_account_never_sees_what_was_read_for_the_first():
    server = FakeServer()
    wi.link(ORG, DOC, "AB#4810", api=server, who="a")
    wi.items(ORG, DOC, api=server, who="a")
    assert wi.summaries(ORG, [DOC], who="b")["items"] == {}
    server.down = True
    assert wi.items(ORG, DOC, api=server, who="b")["items"] == []
    assert wi.cached(ORG, DOC, who="a") is not None
    denied = lambda *a, **k: (_ for _ in ()).throw(ServiceError("forbidden", "no", 403))    # noqa: E731
    with pytest.raises(ServiceError):
        wi.items(ORG, DOC, api=denied, who="a")
    assert wi.cached(ORG, DOC, who="a") is None                                       # a refusal drops what was kept


def test_a_write_for_an_org_that_is_no_longer_the_active_one_sends_nothing(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(actions, "_docs_entitlement", lambda repo: SimpleNamespace(org_id="org-B", sub="u1"))
    monkeypatch.setattr(actions, "docs_api", lambda m, p, b=None, repo=None: server(m, p, b))
    for call in (actions.put_tracker_settings, actions.check_tracker, actions.disconnect_tracker):
        with pytest.raises(ServiceError) as err:
            call(None, {"tracker": "jira"}, {"org_id": "org-A", "repos": []})
        assert err.value.http_status == 409
    assert server.calls == []


def test_only_repos_the_server_can_tell_apart_are_offered_for_mapping():
    def key_of(root):
        if root.endswith("broken"):
            raise RuntimeError("no org key")
        return None if root.endswith("bare") else "k" * 64

    got = wi.local_repos(["/src/pawdelta", "/src/bare", "/src/broken", "/src/has space"], "o1", key_of=key_of)
    assert got == [{"repo": "pawdelta", "repo_key": "k" * 64, "repo_root": "/src/pawdelta"}]


def test_daemon_routes_use_the_orgs_api_and_stay_soft_when_it_is_down(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(actions, "_docs_entitlement", lambda repo: SimpleNamespace(org_id=ORG, sub="u1"))
    monkeypatch.setattr(actions, "docs_api", lambda m, p, b=None, repo=None: server(m, p, b))
    made = actions.link_design_work_item(None, {"id": DOC}, {"item": "AB#4810", "target": {"kind": "design"}})
    assert made["display"] == "AB#4810"
    actions.create_design_work_items(None, {"id": DOC}, {"kind": "passage", "section": "s1", "quote": "text"})
    server.down = True
    got = actions.design_work_items(None, {"id": DOC}, {})
    assert got["unreachable"] is True and got["epic"]["display"] == "AB#4810"
    with pytest.raises(ServiceError) as err:
        actions.create_design_work_items(None, {"id": DOC}, {"kind": "nonsense"})
    assert err.value.http_status == 400
