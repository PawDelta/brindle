"""End-to-end check of analysis docs: brindle's real client against a locally
running pawdelta-web backend, nothing faked in between.

Skipped unless ``BRINDLE_E2E_BACKEND`` names a pawdelta-web checkout. The
backend runs under its own virtualenv (``BRINDLE_E2E_BACKEND_PYTHON``, else
the ``.venv`` of the checkout or of the repository it is a worktree of) as
this same file run as a script: it binds 127.0.0.1, configures the service
with its in-memory store, local data keys and the heuristic reader (no AWS,
no model, no network beyond localhost), signs real entitlements for a Pro
user, a Team org with two members in one group and an Enterprise org through
the backend's own test helpers (imported from the checkout, never copied
here), and serves the app. The tests then sign brindle in as those people
and drive the client code: the merge uploader, the daemon's doc routes, the
brief, shared editing and the org policy.
"""

import base64
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

PREFIX = "/api/brindle/v1"
GROUP = "t_" + "e2" * 8
DOC = """# Checkout cache rollout

We implemented the checkout cache in `api/checkout.py` and shipped it behind a flag.

## What changed

- p95 is 120 ms with the cache on
- three replicas share one Redis

## Open

- Is the cache warm after a deploy?
"""


# -- the backend (run by the backend's own interpreter) --------------------------------------------


def _serve(backend: str) -> None:
    """Seed the backend and serve it on 127.0.0.1; the first stdout line is
    the JSON the tests need (base URL, signing key, each person's tokens)."""
    import random
    import socket

    sys.path[:0] = [backend, os.path.join(backend, "tests")]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}{PREFIX}"
    os.environ["BRINDLE_ISSUER"] = base         # the issuer brindle checks; the ALB origin stays https

    import conftest  # noqa: F401 - names the stub site export backend.main serves
    import stripe
    import test_brindle_api as api
    import test_brindle_orgs as orgs
    import test_brindle_tiers as tiers
    import uvicorn
    from cryptography.hazmat.primitives.asymmetric import ec

    from backend import main
    from backend.pro_api import runtime
    from backend.pro_api.analysis_reader import HeuristicReader
    from backend.pro_api.keywrap import LocalKeyWrapper
    from backend.pro_api.learning_crypto import LearningCipher, LocalDataKeys
    from backend.pro_api.signing import Keyring, LocalSigner
    from backend.pro_api.store import MemoryStore

    from backend.pro_api import routes
    routes.LIMITS["bearer_ip"] = (100_000, 60)  # every test calls from 127.0.0.1: the per-address limit is no check here

    os.environ.update(api.ENV)
    os.environ["BRINDLE_TEAM_PRICE_ID"] = orgs.TEAM_PRICE
    os.environ["BRINDLE_ENTERPRISE_PRICE_ID"] = tiers.ENTERPRISE_PRICE
    stripe.Customer.create = lambda **kw: {"id": f"cus_{kw['metadata']['org_id']}"}
    alb_key = ec.generate_private_key(ec.SECP256R1())

    class Clock:
        """Real time, held behind it so tokens are never issued in brindle's future."""

        def __init__(self):
            self.offset = -120.0

        def __call__(self):
            return time.time() + self.offset

        def advance(self, seconds):
            self.offset += seconds

    rt = runtime.configure(store=MemoryStore(), keyring=Keyring([LocalSigner()]), clock=Clock(),
                           alb_key_fetcher=lambda region, kid: alb_key.public_key(),
                           key_wrapper=LocalKeyWrapper(), learning_cipher=LearningCipher(LocalDataKeys()),
                           analysis_reader=HeuristicReader())
    rt.test_clock, rt.alb_key, rt.rng = rt.clock, alb_key, random.Random(0)

    def pro(sub, n):
        assert api.stripe_post(api.checkout_completed(api.org_of(rt, sub), eid=f"evt_e2e_{n}",
                                                      customer=f"cus_e2e_{n}")).status_code == 200

    people = {"pro": (api.login(rt, "e2e-pro"), None)}
    pro("e2e-pro", "pro")
    team_org, team = orgs.new_team(rt, "e2e")
    ent_org, ent = orgs.new_team(rt, "e2ent")
    tiers.enterprise(ent_org)
    assert rt.store.put_org_teams(team_org, {"teams": {GROUP: {
        "id": GROUP, "name": "web", "parent": None, "budget_usd": None, "manager": None, "split": "shared",
        "reserve_usd": 0, "grants": [], "legacy_cost_center": None, "created_at": rt.now(),
        "created_by": "e2e-owner"}}}, 0)
    for role in ("admin", "member"):
        rt.store.memberships[(team_org, f"e2e-{role}")]["team"] = GROUP
        pro(f"e2e-{role}", role)
        people[f"team-{role}"] = (team[role], team_org)
    pro("e2ent-admin", "ent")
    people["ent-admin"] = (ent["admin"], ent_org)
    jwk = api.client.get(PREFIX + "/keys").json()["keys"][0]
    print(json.dumps({"base": base, "kid": jwk["kid"], "x": jwk["x"], "team_org": team_org, "ent_org": ent_org,
                      "people": {name: {"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                                        "expires_in": t.get("expires_in", 900), "org_id": org}
                                 for name, (t, org) in people.items()}}), flush=True)
    uvicorn.Server(uvicorn.Config(main.app, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    _serve(sys.argv[1])
    raise SystemExit(0)


# -- the tests ------------------------------------------------------------------------------------

import pytest  # noqa: E402

from pro_fixtures import pro_env  # noqa: E402,F401 - fixture

BACKEND = os.environ.get("BRINDLE_E2E_BACKEND")
pytestmark = pytest.mark.skipif(not BACKEND, reason="set BRINDLE_E2E_BACKEND to a pawdelta-web checkout")


def _backend_python(checkout: Path) -> str:
    named = os.environ.get("BRINDLE_E2E_BACKEND_PYTHON")
    if named:
        return named
    common = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], capture_output=True, text=True).stdout.strip()
    for root in (checkout, Path(common).parent if common else checkout):
        if (root / ".venv" / "bin" / "python").exists():
            return str(root / ".venv" / "bin" / "python")
    pytest.fail(f"no backend virtualenv for {checkout}: set BRINDLE_E2E_BACKEND_PYTHON")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    checkout = Path(BACKEND).resolve()
    log = tmp_path_factory.mktemp("backend") / "backend.log"
    with open(log, "wb") as err:
        proc = subprocess.Popen(
            [_backend_python(checkout), str(Path(__file__).resolve()), str(checkout)], cwd=checkout,
            stdout=subprocess.PIPE, stderr=err, text=True, env={**os.environ, "PYTHONPATH": str(checkout)})
    try:
        line = proc.stdout.readline()
        if not line:
            pytest.fail(f"the backend did not start:\n{log.read_text()[-4000:]}")
        yield json.loads(line)
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def sign_in(server, monkeypatch, pro_env):  # noqa: F811
    """``sign_in(name)``: brindle signed in as that person, with the entitlement
    of their org fetched by the real client; rotated tokens are kept for the next test."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from brindle.pro import auth, credentials, license

    monkeypatch.setenv("BRINDLE_PRO_DEV", "1")
    monkeypatch.setenv("BRINDLE_PRO_BASE_URL", server["base"])
    key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(server["x"] + "=="))
    current = []

    def sign(name: str):
        person = server["people"][name]
        store = credentials.default_store()
        store.save({"base_url": server["base"], "access_token": person["access_token"],
                    "refresh_token": person["refresh_token"],
                    "access_expires_at": time.time() + int(person["expires_in"]) - 120})
        ent = auth.switch_org(auth.Client(server["base"]), store, person["org_id"])
        current[:] = [name]
        return ent

    with license._test_signing_key(server["kid"], key):
        yield sign
        if current:
            saved = credentials.default_store().load() or {}
            server["people"][current[0]].update({k: saved[k] for k in ("access_token", "refresh_token")
                                                 if saved.get(k)})


def daemon_get(handler, path_params=None, **query) -> tuple[int, dict]:
    """A daemon read route (routes_read) answering a GET, as the app calls it."""
    from urllib.parse import urlencode

    from starlette.requests import Request

    resp = handler(Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                            "query_string": urlencode(query).encode(), "path_params": path_params or {}}))
    return resp.status_code, json.loads(resp.body)


def merge_upload(db, tmp_path, ws_id: str, branch: str, md: str = DOC) -> tuple:
    """Write the branch's doc where brindle tells agents to and run the merge
    upload (pipeline._deliverables, then the uploader with its sender inline)."""
    from brindle import pipeline
    from brindle.db import Workspace
    from brindle.pro import analysis_docs
    from brindle.pro.analysis_brief import docs_dir

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (tmp_path / ws_id).mkdir()
    ws = Workspace(ws_id, str(repo), branch, "branch", branch, "main", str(tmp_path / ws_id), None, "t", 0.0,
                   session_root=f"sess-{ws_id}")
    db.add_workspace(ws)
    folder = docs_dir(str(repo), branch)
    folder.mkdir(parents=True)
    (folder / "main.md").write_text(md)
    docs = pipeline._deliverables(db, ws)
    assert [d.source for d in docs] == [{"kind": "task", "ref": branch, "session": f"sess-{ws_id}"}]
    up = analysis_docs.AnalysisDocs(str(repo), identity=lambda: "github.com/e2e/repo", notice=lambda _: None,
                                    start_thread=False)
    up.submit(docs)
    assert up.flush()
    return ws, folder


def uploaded(session: str) -> str:
    from brindle.daemon import routes_read

    status, got = daemon_get(routes_read.analysis_docs, scope="user", session=session)
    assert status == 200, got
    assert len(got["docs"]) == 1, got
    return got["docs"][0]["doc_id"]


@pytest.mark.variant("end to end against a live backend; skipped in CI")
def test_import_upload_and_removal(sign_in, db, tmp_path):
    """Add to library, change a file and re-sync, remove: brindle's own client
    (analysis_imports) against the backend's import endpoints."""
    from brindle import kb
    from brindle.pro import analysis_docs
    from brindle.pro import analysis_imports as imports

    sign_in("team-admin")           # not "pro": the backend limits entitlement fetches per user
    assert {s["name"]: s["on"] for s in analysis_docs.switch_states()}["import_upload"]   # on by default
    folder = tmp_path / "imported"
    (folder / "notes").mkdir(parents=True)
    (folder / "notes" / "a.md").write_text("# Import A\n\nFirst imported note.\n")
    (folder / "notes" / "b.md").write_text("# Import B\n\nSecond imported note.\n")
    source = kb.add_source(db, "github.com/e2e/imports", kb.FOLDER, str(folder))

    def tracked() -> dict[str, str]:
        return {r["external_id"]: r["doc_id"] for r in db.conn.execute("SELECT * FROM kb_import_uploads")}

    def doc(doc_id: str) -> tuple[int, dict]:
        return analysis_docs._call("GET", f"/analysis-docs/{doc_id}")

    plan = imports.dry_run(db, source)
    assert plan["allowed"] and [f["name"] for f in plan["files"]] == ["notes/a.md", "notes/b.md"]
    assert imports.add_to_library(db, source, str(tmp_path), plan["token"])["uploaded"] == 2
    ids = tracked()
    assert sorted(ids) == ["notes/a.md", "notes/b.md"] and imports.state(
        db, source, imports.current_identity()) == imports.IN_LIBRARY
    for doc_id in ids.values():
        status, got = doc(doc_id)
        assert status == 200 and got["version"] == 1, got
    assert imports.sync_upload(db, source, str(tmp_path))["sent"] is False         # nothing changed: nothing sent

    (folder / "notes" / "a.md").write_text("# Import A\n\nFirst imported note, edited.\n")
    assert imports.sync_upload(db, source, str(tmp_path))["uploaded"] == 1
    assert tracked() == ids                                                        # updated in place
    status, got = doc(ids["notes/a.md"])
    assert status == 200 and got["version"] == 2 and "edited" in got["files"][0]["md"], got
    assert doc(ids["notes/b.md"])[1]["version"] == 1

    (folder / "notes" / "b.md").unlink()                                           # gone from the folder: pruned
    assert imports.sync_upload(db, source, str(tmp_path))["deleted"] == 1
    assert doc(ids["notes/b.md"])[0] == 404 and doc(ids["notes/a.md"])[0] == 200
    assert tracked() == {"notes/a.md": ids["notes/a.md"]}

    assert imports.remove_from_library(db, source) == {"left": 0}
    assert not imports.in_library(db, source) and tracked() == {}
    for doc_id in ids.values():
        assert doc(doc_id)[0] == 404


@pytest.mark.variant("end to end against a live backend; skipped in CI")
def test_upload_at_merge_read_list_and_scopes(sign_in, db, tmp_path):
    from brindle import knowledge
    from brindle.daemon import routes_read
    from brindle.pro import analysis_docs

    ent = sign_in("pro")
    ws, folder = merge_upload(db, tmp_path, "w-up", "feat/cache")
    doc_id = uploaded("sess-w-up")
    assert not (folder / "main.md").exists()                                  # sent: the local copy goes
    assert (analysis_docs.UPDATED_ITEM, doc_id, "uploaded") in knowledge.receipt(db, "sess-w-up")

    status, got = daemon_get(routes_read.analysis_doc, {"id": doc_id})
    assert status == 200, got
    assert [f["name"] for f in got["files"]] == ["main.md"] and got["version"] == 1
    assert got["scope"] == {"kind": "user", "id": ent.sub}
    assert analysis_docs.read_doc(db, "reader-1", doc_id)["version"] == 1   # pinned in the receipt

    topic = got["topics"][0]
    for query in ({"scope": "user", "topic": topic}, {"topic": topic}, {"session": "sess-w-up"}):
        status, listed = daemon_get(routes_read.analysis_docs, **query)
        assert status == 200, (query, listed)
        assert [d["doc_id"] for d in listed["docs"]] == [doc_id], query
    status, listed = daemon_get(routes_read.analysis_docs, scope="user", topic="no such topic")
    assert status == 200 and listed["docs"] == []

    status, got = daemon_get(routes_read.analysis_doc_scopes)
    assert status == 200, got
    assert got["scopes"] == [{"kind": "user", "id": ent.sub, "name": "Personal", "can_share": True}]

    from typer.testing import CliRunner

    from brindle.cli import app

    listed = CliRunner().invoke(app, ["docs", "list"])
    assert listed.exit_code == 0 and listed.output.startswith(f"{doc_id}  v1  "), listed.output
    shown = CliRunner().invoke(app, ["docs", "show", doc_id])
    assert shown.exit_code == 0 and shown.output.strip() == DOC.strip(), shown.output


@pytest.mark.variant("end to end against a live backend; skipped in CI")
def test_brief_and_map_neighbourhood(sign_in, db, tmp_path):
    from brindle import knowledge
    from brindle.daemon import routes_read
    from brindle.pro import analysis_brief, analysis_docs

    ent = sign_in("pro")
    merge_upload(db, tmp_path, "w-brief", "feat/brief")
    doc_id = uploaded("sess-w-brief")

    task = "Make the checkout cache warm after a deploy"
    block = analysis_brief.server_brief(db, "task-1", task, ["api/checkout.py"])    # assist is on by default
    assert "UNTRUSTED REFERENCE" in block and "checkout" in block.lower()
    assert (analysis_brief.DOC_ITEM, doc_id, "1") in knowledge.receipt(db, "task-1")

    status, got = daemon_get(routes_read.analysis_doc_map, scope_kind="user", scope_id=ent.sub,
                             paths=json.dumps(["api/checkout.py"]), depth="1")
    assert status == 200, got
    assert got["paths"] == ["api/checkout.py"]
    assert doc_id in {n.get("id") for n in got["nodes"]}, got["nodes"]

    from typer.testing import CliRunner

    from brindle.cli import app

    off = CliRunner().invoke(app, ["docs", "off", "--switch", "assist"])          # the person turns briefs off
    assert off.exit_code == 0 and "assist is off" in off.output, off.output
    try:
        assert analysis_brief.server_brief(db, "task-2", task, ["api/checkout.py"]) == ""
    finally:
        assert CliRunner().invoke(app, ["docs", "on", "--switch", "assist"]).exit_code == 0
    assert analysis_brief.server_brief(db, "task-3", task, ["api/checkout.py"])


@pytest.mark.variant("end to end against a live backend; skipped in CI")
def test_share_and_unshare_with_the_team(sign_in, server, db, tmp_path):
    from brindle.daemon import routes_actions, routes_read
    from brindle.pro import analysis_policy, auth, credentials
    from brindle.service.errors import ServiceError

    sign_in("team-member")
    status, got = daemon_get(routes_read.analysis_doc_scopes)
    assert status == 200, got
    assert {"kind": "group", "id": GROUP, "name": "web", "can_share": True} in got["scopes"]
    merge_upload(db, tmp_path, "w-share", "feat/share")
    doc_id = uploaded("sess-w-share")
    status, got = daemon_get(routes_read.analysis_doc_switches)                # the defaults, as the app shows them
    assert status == 200, got
    assert {s["name"]: s["on"] for s in got["switches"]} == {
        "store": True, "share": True, "assist": True, "prompt_analysis": False, "import_upload": True}
    assert not any(s["locked"] for s in got["switches"])
    switch = routes_actions.put_doc_switch                                     # the app's toggle
    assert {s["name"]: s["on"] for s in switch(db, {}, {"name": "share", "on": False})["switches"]}["share"] is False
    with pytest.raises(ServiceError, match="switch_off"):
        routes_actions.share_doc(db, {"id": doc_id}, {"scope": f"team:{GROUP}"})
    switch(db, {}, {"name": "share", "on": True})
    routes_actions.share_doc(db, {"id": doc_id}, {"scope": f"team:{GROUP}"})

    sign_in("team-admin")                                                    # the group's other member
    status, listed = daemon_get(routes_read.analysis_docs, scope=f"team:{GROUP}")
    assert status == 200 and [d["doc_id"] for d in listed["docs"]] == [doc_id], listed
    status, got = daemon_get(routes_read.analysis_doc, {"id": doc_id})
    assert status == 200 and got["files"][0]["md"] == DOC, got

    sign_in("team-member")
    routes_actions.unshare_doc(db, {"id": doc_id}, {"scope": f"team:{GROUP}"})
    sign_in("team-admin")
    status, listed = daemon_get(routes_read.analysis_docs, scope=f"team:{GROUP}")
    assert status == 200 and listed["docs"] == [], listed
    status, _ = daemon_get(routes_read.analysis_doc, {"id": doc_id})
    assert status != 200

    client, store, org = auth.Client(server["base"]), credentials.default_store(), server["team_org"]
    analysis_policy.update(org, client, store, {"groups": {GROUP: {"switches": {"share": "enforced_off"}}}})
    try:                                                                       # the admin locks the group's sharing
        sign_in("team-member")
        status, got = daemon_get(routes_read.analysis_doc_switches)
        assert status == 200 and {"name": "share", "on": False, "choice": "on", "locked": True,
                                  "org": "enforced_off"} in got["switches"], got
        with pytest.raises(ServiceError) as locked:                            # the toggle refuses it
            routes_actions.put_doc_switch(db, {}, {"name": "share", "on": True})
        assert locked.value.http_status == 403
        with pytest.raises(ServiceError, match="switch_off"):                  # as the server does the share
            routes_actions.share_doc(db, {"id": doc_id}, {"scope": f"team:{GROUP}"})
    finally:
        sign_in("team-admin")
        analysis_policy.update(org, client, store, {})


@pytest.mark.variant("end to end against a live backend; skipped in CI")
def test_export_md_docx_and_zip_bytes(sign_in, db, tmp_path):
    from brindle.daemon import routes_actions

    sign_in("pro")
    merge_upload(db, tmp_path, "w-export", "feat/export")
    doc_id = uploaded("sess-w-export")

    def export(fmt):
        got = routes_actions.export_doc(db, {"id": doc_id}, {"format": fmt})
        assert got["filename"] == f"{doc_id}.{fmt}"
        return base64.b64decode(got["content_b64"])

    assert "## What changed" in export("md").decode()
    with zipfile.ZipFile(io.BytesIO(export("docx"))) as z:
        assert "word/document.xml" in z.namelist()
    with zipfile.ZipFile(io.BytesIO(export("zip"))) as z:                # <title slug>/<file name>
        main = [n for n in z.namelist() if n.endswith("/main.md")]
        assert len(main) == 1 and "## What changed" in z.read(main[0]).decode(), z.namelist()
    written = routes_actions.export_doc(db, {"id": doc_id}, {"format": "md", "dir": str(tmp_path)})
    assert Path(written["path"]).read_text().startswith("# Checkout cache rollout")


@pytest.mark.variant("end to end against a live backend; skipped in CI")
def test_org_policy_enforced_off_blocks_upload(sign_in, server, db, tmp_path):
    from brindle.daemon import routes_actions, routes_read
    from brindle.pro import analysis_docs, analysis_policy, auth, credentials
    from brindle.service.errors import ServiceError

    ent = sign_in("ent-admin")
    org = server["ent_org"]
    assert ent.org_id == org and "analysis_docs_enterprise" in ent.features
    client, store = auth.Client(server["base"]), credentials.default_store()
    status, got = daemon_get(routes_read.analysis_doc_policy)
    assert status == 200 and isinstance(got.get("switches"), dict), got
    analysis_policy.update(org, client, store, {"switches": {"store": "enforced_off"}})
    try:
        assert analysis_policy.fetch(org, client, store)["switches"]["store"] == "enforced_off"
        assert analysis_policy.resolve_switch("store", {"store": "on"}, store=store) is False
        status, got = daemon_get(routes_read.analysis_doc_switches)            # shown locked, with the org's value
        assert status == 200 and {"name": "store", "on": False, "choice": None, "locked": True,
                                  "org": "enforced_off"} in got["switches"], got
        with pytest.raises(ServiceError) as locked:                            # and the toggle refuses it
            routes_actions.put_doc_switch(db, {}, {"name": "store", "on": True})
        assert locked.value.http_status == 403
        merge_upload(db, tmp_path, "w-off", "feat/off")
        status, listed = daemon_get(routes_read.analysis_docs, scope="user", session="sess-w-off")
        assert status == 200 and listed["docs"] == [], listed                # the client sent nothing
        status, refused = analysis_docs._call("POST", "/analysis-docs", {   # and the server refuses it anyway
            "scope": {"kind": "org", "id": org}, "files": [{"name": "main.md", "md": DOC}],
            "source": {"kind": "request", "ref": "e2e"}})
        assert (status, refused.get("error")) == (403, "switch_off"), refused
    finally:
        analysis_policy.update(org, client, store, {"switches": {}})


@pytest.mark.variant("end to end against a live backend; skipped in CI")
def test_versions_and_shared_editing_draft_publish(sign_in, db, tmp_path):
    from brindle import pipeline
    from brindle.db import Agent
    from brindle.pro import analysis_docs

    sign_in("pro")
    ws, _ = merge_upload(db, tmp_path, "w-edit", "feat/edit")
    doc_id = uploaded("sess-w-edit")
    db.add_agent(Agent("w-edit-a", ws.id, "developer", "claude", None, "assign", "idle", "@0", None, time.time()))
    other = Agent("w-edit-b", ws.id, "developer", "claude", None, "assign", "idle", "@0", None, time.time())
    db.add_agent(other)
    agent = db.get_agent("w-edit-a")
    assert analysis_docs.read_doc(db, agent.id, doc_id)["version"] == 1
    analysis_docs.read_doc(db, other.id, doc_id)
    assert analysis_docs._newer(db, agent.id) == []                            # POST /analysis-docs/versions

    edited = DOC.replace("- three replicas share one Redis", "- three replicas share one Redis cluster")
    said = analysis_docs.save_draft(db, agent, ws.branch, doc_id, [{"name": "main.md", "md": edited}])
    assert said.startswith(f"Draft of {doc_id} saved on version 1"), said
    pipeline._upload_deliverables(db, ws, [])                                   # the branch merged
    assert analysis_docs.drafts(db, agent.id) == {} and analysis_docs.owed(db) == {}

    got = analysis_docs.read_doc(db, agent.id, doc_id)
    assert got["version"] == 2 and "Redis cluster" in got["files"][0]["md"]
    assert analysis_docs._newer(db, other.id) == [(doc_id, 1, 2)]
    assert "now at 2" in analysis_docs.stale_before_result(db, other.id)

