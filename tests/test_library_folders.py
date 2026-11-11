"""Library folders: the engine client and the daemon routes over a fake library."""
from types import SimpleNamespace

import pytest

from brindle.daemon import routes_actions as actions
from brindle.pro import library_folders as folders
from brindle.service.errors import ServiceError


class FakeApi:
    def __init__(self, refuse=()):
        self.calls, self.refuse = [], set(refuse)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if any(r in path for r in self.refuse):
            raise RuntimeError("needs the team plan")
        if method == "GET":
            return {"folders": [{"id": "fld_1", "name": "Cloud"}, "junk", {"name": "no id"}]}
        return {"id": "fld_new"}


def test_list_all_skips_refusing_scopes_but_raises_when_all_refuse():
    scopes = [{"kind": "user", "id": "u1", "name": "Me"}, {"kind": "group", "id": "t1", "name": "Team"}]
    got = folders.list_all(scopes, api=FakeApi(refuse=["scope_id=t1"]))
    assert [(g["scope"]["id"], [f["id"] for f in g["folders"]]) for g in got] == [("u1", ["fld_1"])]
    with pytest.raises(RuntimeError):
        folders.list_all(scopes, api=FakeApi(refuse=["analysis-folders"]))


def test_create_sends_the_scope_and_settings_and_refuses_bad_input():
    api = FakeApi()
    folders.create("group", "t1", "  Cloud ", "shared", "members", api=api)
    assert api.calls == [("POST", "/analysis-folders", {"scope": {"kind": "group", "id": "t1"}, "name": "Cloud",
                                                      "visibility": "shared", "can_add": "members"})]
    for args in (("user", "u1", "  "), ("user", "u1", "x", "public"), ("team", "t1", "x")):
        with pytest.raises(ValueError):
            folders.create(*args, api=api)
    assert len(api.calls) == 1


def test_daemon_routes_resolve_the_scope_and_turn_bad_input_into_400(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(actions, "_docs_entitlement", lambda repo: SimpleNamespace(sub="u1"))
    monkeypatch.setattr(actions, "docs_api", lambda m, p, b=None, repo=None: api(m, p, b))
    actions.create_library_folder(None, {}, {"scope": "team:t1", "name": "Cloud", "visibility": "shared"})
    actions.move_doc_to_folder(None, {"id": "doc/1"}, {"scope": "user", "folder": "fld_1"})
    assert api.calls[0][2]["scope"] == {"kind": "group", "id": "t1"}
    assert api.calls[1] == ("PUT", "/analysis-docs/doc%2F1/design/folder",
                            {"folder": {"scope": {"kind": "user", "id": "u1"}, "id": "fld_1"}})
    with pytest.raises(ServiceError) as err:
        actions.create_library_folder(None, {}, {"scope": "user", "name": " "})
    assert err.value.http_status == 400
