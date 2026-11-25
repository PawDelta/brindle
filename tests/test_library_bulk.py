"""Bulk Library actions over the daemon's real per-doc operations and a fake Library."""

import base64
import io
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from brindle.daemon import routes_actions as actions
from brindle.pro import auth, local_library
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixtures
from test_plans import session  # noqa: F401 - fixture
from test_shared_plan_docs import DOC, TEAM, parts  # noqa: F401 - fixture

REPO = "/repo with spaces"


@pytest.fixture
def library(monkeypatch):
    docs = {doc_id: {"folder": "old", "shared": {("group", "old-team")}} for doc_id in ("one", "two")}

    def api(method, path, body=None, **kwargs):
        _, doc_id, *tail = path.removeprefix("/").split("/")
        if doc_id not in docs:
            raise ServiceError("not_found", "Document is missing", 404)
        doc = docs[doc_id]
        if method == "GET" and not tail:
            owner = {"kind": "user", "id": "owner"}
            scopes = [{"kind": kind, "id": scope_id} for kind, scope_id in doc["shared"]]
            return {"scope": owner, "scopes": [dict(owner, name="Owner"), *scopes]}
        if method == "DELETE" and not tail:
            del docs[doc_id]
        elif method == "PUT" and tail == ["design", "folder"]:
            doc["folder"] = body["folder"]["id"]
        elif method == "POST" and tail == ["share"]:
            doc["shared"].add((body["scope"]["kind"], body["scope"]["id"]))
        elif method == "DELETE" and tail[0] == "share":
            doc["shared"].remove(tuple(tail[1:]))
        else:
            pytest.fail(f"unexpected Library operation: {method} {path}")
        return {}

    monkeypatch.setattr(local_library, "get", lambda doc_id: None)
    monkeypatch.setattr(actions, "_docs_entitlement", lambda repo: SimpleNamespace(sub="owner"))
    monkeypatch.setattr(actions, "docs_api", api)
    return docs


@pytest.mark.parametrize("action, options, expected", [
    pytest.param("move", {"folder": "new", "scope": "user"}, {"folder": "new", "shared": {("group", "old-team")}},
                 marks=pytest.mark.variant("bulk move must forward the target folder to each doc")),
    pytest.param("share", {"scope": "team:new-team"},
                 {"folder": "old", "shared": {("group", "old-team"), ("group", "new-team")}},
                 marks=pytest.mark.variant("bulk share must grant the new scope on every doc")),
    pytest.param("unshare", {"scope": "team:old-team"}, {"folder": "old", "shared": set()},
                 marks=pytest.mark.variant("bulk unshare must revoke the given scope on every doc")),
])
def test_bulk_action_updates_each_doc(db, library, monkeypatch, action, options, expected):
    api = actions.docs_api

    def repo_api(method, path, body=None, *, repo):
        if repo != REPO:
            raise ServiceError("not_found", "Document is missing in this repo", 404)
        return api(method, path, body, repo=repo)

    monkeypatch.setattr(actions, "docs_api", repo_api)
    second = "two"
    if action == "move":
        second = "local-two"
        kept = {"doc_id": second, "server_id": "two"}
        monkeypatch.setattr(local_library, "get", {second: kept}.get)
        monkeypatch.setattr(local_library, "update", lambda doc_id, **fields: kept.update(fields))

    got = actions.bulk_docs(db, {}, {"action": action, "ids": ["one", second], "repo": REPO, **options})

    assert got == {"results": [{"id": "one", "ok": True}, {"id": second, "ok": True}]}
    assert library == {"one": expected, "two": expected}
    if action == "move":
        assert local_library.view(kept)["design"]["folder"]["id"] == "new"


@pytest.mark.variant("a missing document must preserve its own error code and allow later deletes")
def test_bulk_delete_continues_after_a_doc_fails(db, library):
    for doc in library.values():
        doc["shared"].clear()
    got = actions.bulk_docs(db, {}, {"action": "delete", "ids": ["one", "missing", "two"]})

    assert [(r["id"], r["ok"], r.get("code")) for r in got["results"]] == [
        ("one", True, None), ("missing", False, "not_found"), ("two", True, None),
    ]
    assert got["results"][1]["error"] and not library


@pytest.mark.parametrize("local", [False, pytest.param(True, marks=pytest.mark.variant(
    "sharing recorded on this Mac also requires confirmation before either copy is deleted"))])
def test_bulk_delete_requires_confirmation_for_an_ordinary_shared_doc(db, library, monkeypatch, local):
    doc_id = "local-one" if local else "one"
    kept = {}
    if local:
        kept[doc_id] = {"server_id": "one", "shared": [{"kind": "group", "id": "old-team"}]}
        library["one"]["shared"].clear()
    monkeypatch.setattr(local_library, "get", kept.get)
    monkeypatch.setattr(local_library, "remove", kept.pop)
    body = {"action": "delete", "ids": [doc_id]}

    refused, = actions.bulk_docs(db, {}, body)["results"]
    assert (refused["id"], refused["ok"], refused.get("code")) == (doc_id, False, "confirm_shared")
    if local:
        assert kept[doc_id]["shared"] and "one" in library
    else:
        assert library["one"]["shared"] == {("group", "old-team")}

    assert actions.bulk_docs(db, {}, {**body, "confirm": True}) == {"results": [{"id": doc_id, "ok": True}]}
    assert "one" not in library and not kept


def test_bulk_delete_requires_confirmation_before_releasing_shared_plan(db, parts, library):
    pid, _, net = parts
    sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a", "required": True}], **net)
    library[DOC] = {"shared": set()}
    library["one"]["shared"].clear()
    body = {"action": "delete", "ids": [DOC, "one"]}

    refused, deleted = actions.bulk_docs(db, {}, body)["results"]
    assert (refused["id"], refused["ok"], refused["code"]) == (DOC, False, "confirm_shared")
    assert deleted == {"id": "one", "ok": True} and "one" not in library
    assert DOC in library and sd.link(db, pid)["doc_id"] == DOC

    assert actions.bulk_docs(db, {}, {**body, "ids": [DOC], "confirm": True}) == {
        "results": [{"id": DOC, "ok": True}],
    }
    assert DOC not in library and sd.link(db, pid) is None


@pytest.mark.parametrize("fmt", ["md", pytest.param("zip", marks=pytest.mark.variant(
    "binary exports keep each document's own archive intact"))])
def test_bulk_export_deduplicates_titles_and_skips_refused_docs(db, monkeypatch, fmt):
    titles = {"one": "Release notes", "two": "Release notes", "refused": "Restricted"}

    def api(method, path, *, repo, query=None):
        if repo != REPO:
            raise ServiceError("not_found", "Document is missing in this repo", 404)
        _, doc_id, *tail = path.removeprefix("/").split("/")
        if not tail:
            return {"title": titles[doc_id]}
        if query["destination"] != "sharepoint":
            raise ServiceError("destination_not_allowed", "Use SharePoint", 403)
        if doc_id == "refused":
            raise ServiceError("not_entitled", "Export is unavailable", 402)
        if query["format"] == "md":
            return {auth.TEXT_KEY: f"# {doc_id}"}
        buf = io.BytesIO()
        with ZipFile(buf, "w") as archive:
            archive.writestr("doc.md", f"# {doc_id}")
        return {auth.BINARY_KEY: buf.getvalue()}

    monkeypatch.setattr(actions, "docs_api", api)
    monkeypatch.setattr(local_library, "get", {"local-two": {"server_id": "two"}}.get)
    got = actions.export_docs(db, {}, {
        "ids": ["one", "refused", "local-two"], "format": fmt, "destination": "sharepoint", "repo": REPO,
    })

    assert got["filename"].endswith(".zip")
    assert got["skipped"] == [{"id": "refused", "code": "not_entitled"}]
    with ZipFile(io.BytesIO(base64.b64decode(got["content_b64"]))) as archive:
        assert archive.namelist() == [f"Release notes.{fmt}", f"Release notes (2).{fmt}"]
        for name, doc_id in zip(archive.namelist(), ("one", "two"), strict=True):
            data = archive.read(name)
            if fmt == "zip":
                with ZipFile(io.BytesIO(data)) as doc_zip:
                    data = doc_zip.read("doc.md")
            assert data.decode() == f"# {doc_id}"


def test_local_only_plan_download_respects_policy_and_exports_saved_markdown(db, monkeypatch):
    kept = {"doc_id": "local-one", "title": "Local plan", "files": [{"name": "plan.md", "md": "# On this device"}]}
    monkeypatch.setattr(local_library, "get", {"local-one": kept}.get)

    def unexpected_doc_call(*args, **kwargs):
        pytest.fail("a local-only plan must not be sent to the Library")

    def policy_api(method, path, *, repo):
        if repo != REPO:
            raise ServiceError("not_found", "Export policy is missing in this repo", 404)
        return {"export_destinations": ["download"]}

    monkeypatch.setattr(actions, "docs_api", unexpected_doc_call)
    monkeypatch.setattr(actions, "docs_org_api", policy_api)
    body = {"ids": ["local-one"], "format": "zip", "repo": REPO}
    assert actions.export_docs(db, {}, {**body, "destination": "sharepoint"})["skipped"] == [
        {"id": "local-one", "code": "destination_not_allowed"},
    ]
    got = actions.export_docs(db, {}, body)
    assert not got["skipped"]
    with ZipFile(io.BytesIO(base64.b64decode(got["content_b64"]))) as archive:
        with ZipFile(io.BytesIO(archive.read("Local plan.zip"))) as plan:
            assert plan.read("plan.md") == b"# On this device"


def test_bulk_routes_reject_over_limit_before_touching_docs(db, monkeypatch):
    def unexpected_upstream(*args, **kwargs):
        pytest.fail("over-limit request reached the Library")

    monkeypatch.setattr(actions, "docs_api", unexpected_upstream)
    ids = ["one"] * 501
    for run, fields in ((actions.bulk_docs, {"action": "delete"}), (actions.export_docs, {"format": "md"})):
        with pytest.raises(ServiceError) as refused:
            run(db, {}, {"ids": ids, **fields})
        assert refused.value.http_status == 400
