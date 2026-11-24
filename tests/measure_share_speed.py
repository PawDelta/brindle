"""Manual benchmark. Run pytest -s tests/measure_share_speed.py from the repo root."""

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Event, Lock
from time import perf_counter
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from brindle.daemon.app import build_app
from brindle.pro import auth, credentials, license
from pro_fixtures import backend, claims, fixed_identity, pro_env, sign, signing_key  # noqa: F401 - fixtures
from test_plans import AUTH, TOKEN, session  # noqa: F401 - fixture
from test_shared_plan_docs import BASE, DESIGN, DOC, TEAM_LOGIN, parts  # noqa: F401 - fixture


@pytest.mark.slow("manual benchmark with simulated Library latency")
def test_measure_share_speed(parts, backend, monkeypatch, tmp_path):
    pid, library, net = parts
    library.author = "user_1"
    owner = net["store"]
    invitee = credentials.FileStore(tmp_path / "pro", account="invitee")
    invitee_tokens = backend.issue()
    invitee.save({**owner.load(), **invitee_tokens, "entitlement": sign(
        backend.key, claims(sub="u_e", features=list(TEAM_LOGIN)))})
    active = owner
    lock = Lock()
    credential_reads = []
    load = credentials.FileStore.load

    def counted_load(store):
        with lock:
            credential_reads.append(1)
        return load(store)

    request = backend.request

    def delayed(method, url, form, headers):
        Event().wait(0.150)
        return request(method, url, form, headers)

    monkeypatch.setattr(backend, "request", delayed)
    monkeypatch.setattr(auth, "UrllibTransport", lambda **kwargs: backend)
    monkeypatch.setattr(auth, "_signed_in_providers", lambda: [])
    monkeypatch.setattr(credentials, "default_store", lambda: active)
    monkeypatch.setattr(credentials.FileStore, "load", counted_load)
    license.clear_cache()

    def grant(form, headers):
        sub = form["scope"]["id"]
        library.permissions[sub] = {"can_comment": True, "can_suggest": True, "can_edit": False}
        if form["sign_off"] == "required":
            library.reviewers.append({"sub": sub, "required": True})
        return 200, {"id": DOC, "design": library.view()}

    def directory(form, headers):
        if headers["Authorization"] == f"Bearer {invitee_tokens['access_token']}":
            return 403, {"error": "forbidden"}
        return 200, {"people": [{"id": "u_e", "display_name": "Dana", "email": "dana@example.test",
                                 "role": "member", "teams": [], "on_plan": "u_e" in library.permissions}]}

    def meta(form, headers):
        scopes = [{"kind": "user", "id": sub} for sub in library.permissions]
        return 200, {"id": DOC, "title": library.title, "scope": {"kind": "user", "id": "user_1"}, "scopes": scopes}

    members = [{"sub": "user_1", "email": "owner@example.test"}, {"sub": "u_e", "email": "dana@example.test"}]
    org = "/orgs/org_1"
    search = f"GET {DESIGN}/people?q=dana&include_unshared=true"
    backend.routes.update({
        f"POST {BASE}/share": grant,
        f"GET {BASE}": meta,
        f"GET {DESIGN}/people": directory,
        search: directory,
        f"GET {org}/members": lambda f, h: (200, {"members": members}),
        f"GET {org}/analysis-docs/policy": lambda f, h: (200, {"switches": {}, "groups": {}}),
        f"GET /analysis-docs/scopes": lambda f, h: (200, {"scopes": [{"kind": "org", "id": "org_1"}]}),
        f"GET /me/analysis-docs/settings": lambda f, h: (200, {"switches": {"store": "off"}}),
        f"POST {org}/analysis-docs/review-policies/preview": lambda f, h: (200, {"policy_reviewers": []}),
        "GET /entitlement": lambda f, h: (200, {"entitlement": sign(backend.key, claims(features=list(TEAM_LOGIN)))}),
    })

    plan = f"/v1/plans/{pid}"
    with TestClient(build_app(TOKEN), headers=AUTH) as client, ThreadPoolExecutor(max_workers=3) as pool:
        access = partial(client.get, plan + "/access")
        preview = partial(client.post, plan + "/policy-preview", json={})
        query = partial(client.get, plan + "/people?" + search.split("?", 1)[1])
        sync = partial(client.post, plan + "/sync", json={})

        def invite(body):
            """The panel's Add: its answer, then its receipt back, which sends it (TestClient waits for that)."""
            def step():
                got = client.post(plan + "/people", json=body)
                client.post(plan + "/invites/receipts", json={"receipts": [got.json()["invite"]["receipt"]]})
                return got
            return step

        add = invite({"sub": "u_e", "access": "comment", "sign_off": True})

        def measure(label, *lanes, before=()):
            def run(steps):
                results = []
                for step, status in steps:
                    started = perf_counter()
                    response = step()
                    assert response.status_code == status, response.text
                    results.append(round(1000 * (perf_counter() - started), 1))
                return results

            backend.calls.clear()
            credential_reads.clear()
            started = perf_counter()
            prelude = run(before)
            leg_times = list(pool.map(run, lanes))
            elapsed = round(1000 * (perf_counter() - started), 1)
            print(f"{label}: {elapsed} ms, prelude ms {prelude}, leg ms {leg_times}, "
                  f"{len(credential_reads)} credential loads, {backend.paths()}")

        def ok(step):
            return step, 200
        measure("First panel, settings cold", [ok(access)], [ok(preview)])
        measure("First panel, settings warm", [ok(access)], [ok(preview)])
        measure("Directory preload for first search", [ok(partial(client.get, "/v1/library/people"))],
                [ok(partial(client.get, "/v1/analysis-docs/scopes"))])
        measure("Unshared plan people endpoint, when called directly", [ok(query)])
        measure("First invite reply", [(add, 202)])
        measure("First invite UI follow-up", [ok(access)], [ok(sync)])
        measure("Shared panel", [ok(access)])
        measure("Shared plan search", [ok(query)])
        with patch.dict(backend.routes, {search: [(403, {"error": "forbidden"})]}):
            measure("Shared plan search with members fallback", [ok(query)])
        measure("Existing doc invite and refresh", [(invite({"sub": "u_second", "access": "comment"}), 202), ok(access)])
        library.open_draft()
        measure("Owner sync, first draft", [ok(sync)])
        measure("Owner sync, cached draft", [ok(sync)])
        owner.save({**owner.load(), "access_expires_at": 0})
        measure("Shared panel with expired access token", [ok(access)])

        active = invitee
        license.clear_cache()
        identify = partial(client.get, f"/v1/library/designs/{DOC}/plan")
        read = partial(client.get, f"/v1/shared-designs/{DOC}")
        org_directory = partial(client.get, "/v1/library/people")
        measure("Invitee open from Library", [ok(read)], [ok(org_directory)], before=[(identify, 404)])
