"""The client-side entitlement gate on setting up brindle CI (``ci`` / ``ci_fix``)."""
import os
import subprocess
import time

import pytest

from brindle import ci_client
from brindle.ci_client import CIError
from brindle.pro import auth, credentials, license
from conftest import sh
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, FakeTransport, backend, pro_env, signing_key, token,
)

REPO = "acme/widgets"
CI_TOKEN = "cpc_" + "c" * 32


def logged_in(tmp_path, backend, entitlement):
    t = backend.issue()
    store = credentials.FileStore(tmp_path / "pro")
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + 900, "entitlement": entitlement, "base_url": BASE})
    return store


# -- the ci gate ----------------------------------------------------------------------------------------


def gate(tmp_path, backend, token, org="org_1", **claims):
    store = logged_in(tmp_path, backend, token(**claims))
    return ci_client.require_ci(store=store, client=auth.Client(BASE, backend), org=org)


def test_ci_gate_fails_closed_on_an_expired_entitlement(tmp_path, backend, token):
    backend.routes["POST /token/refresh"] = [(400, {"error": "invalid_grant"})]
    old = int(time.time()) - 30 * 86400
    with pytest.raises(CIError, match="needs brindle Team"):
        gate(tmp_path, backend, token, features=["ci"], iat=old - 3600, exp=old)


def test_ci_gate_fails_closed_on_anything_unexpected(monkeypatch):
    def boom(**kw):
        raise RuntimeError("keychain exploded")
    monkeypatch.setattr(license, "current", boom)
    with pytest.raises(CIError, match="needs brindle Team: could not check") as e:
        ci_client.require_ci()
    assert "keychain" not in str(e.value) and e.value.code == "not_entitled"


def test_ci_gate_refuses_another_org(tmp_path, backend, token):
    with pytest.raises(CIError, match="brindle account org use org_2"):
        gate(tmp_path, backend, token, org="org_2", features=["ci"])


# -- init behind the gate ---------------------------------------------------------------------------


class Plugin:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport

    def _team_org(self, org):
        return org or "org_1"

    def _client(self, base):
        return auth.Client(BASE, self.transport)


class Proc:
    def __init__(self, out=""):
        self.stdout, self.stderr, self.returncode = out, "", 0


@pytest.fixture
def ci_repo(repo, monkeypatch):
    monkeypatch.chdir(repo)
    sh(f"git remote set-url origin https://github.com/{REPO}.git", repo)
    return repo


def test_init_with_ci_fix_fetches_only_the_fix_workflow(tmp_path, ci_repo, backend, token, monkeypatch):
    from brindle import git as git_mod

    store = logged_in(tmp_path, backend, token(features=["learning", "ci_fix"]))
    monkeypatch.setattr(auth, "create_ci_token", lambda client, s, org, name: {
        "token": CI_TOKEN, "token_id": "ct_1", "org_id": org, "name": name})
    real_run = git_mod.run
    pushed = {}

    def fake_git(args, cwd, check=True):
        if args[0] != "push":
            return real_run(args, cwd, check)
        # init deletes the setup branch once pushed: read what it pushed now
        tree = real_run(["ls-tree", "-r", "--name-only", "HEAD"], cwd).stdout.split()
        pushed.update({p: real_run(["show", f"HEAD:{p}"], cwd).stdout for p in tree if p.startswith(".github/")})
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(git_mod, "run", fake_git)

    def run(argv, **kw):
        if argv[:2] == ["gh", "api"] and argv[2].startswith("repos/"):
            return Proc("true\n")
        if argv[:2] == ["gh", "api"]:
            return Proc("Organization\n")
        if argv[:3] == ["gh", "pr", "create"]:
            return Proc("https://gh.test/pr/1\n")
        return Proc()
    t = FakeTransport({"GET /ci/workflow?kind=fix": [(200, {"text": "name: fix\n"})]})
    ci_client.init(repo=REPO, org=None, providers=["claude"], cwd=str(ci_repo),
                   env={"PATH": os.environ["PATH"]}, run=run, open_url=lambda url: None,
                   account=Plugin(store, backend), client=ci_client.Client(BASE, t), credential="key",
                   say=lambda s: None)
    assert t.paths() == ["GET /ci/workflow?kind=fix"]
    # init leaves the person on their own branch; the workflows went out on the setup branch
    assert pushed == {".github/workflows/brindle-ci-fix.yml": "name: fix\n"}
