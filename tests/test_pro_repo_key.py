"""Repo identity (root commit, else origin URL), org keys, and the refs
derived from them, end to end through the cloud learner."""
import hashlib
import hmac
import subprocess
import time

import pytest

from brindle.learning import Outcome, TaskInfo
from brindle.pro import auth, credentials, orgkey
from brindle.pro.learning import CloudLearner
from brindle.pro.orgkey import OrgKey, OrgKeys, normalize_remote, repo_identity
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, backend, claims, pro_env, sign, signing_key,
)

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main"]


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def new_repo(path, *, commit=True, origin=None):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    if commit:
        (path / "README").write_text("hi\n")
        git(path, "add", "README")
        git(path, "commit", "-q", "-m", "root")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


# -- repo identity -----------------------------------------------------------------------------


def test_empty_repos_fall_back_to_the_origin_url(tmp_path):
    a = new_repo(tmp_path / "a", commit=False, origin="git@github.com:Owner/Repo.git")
    b = new_repo(tmp_path / "b", commit=False, origin="https://github.com/Owner/Repo")
    assert repo_identity(str(a)) == repo_identity(str(b)) == "github.com/Owner/Repo"


def test_unusable_origins_are_no_identity():
    assert normalize_remote("") is None


# -- domain separation -------------------------------------------------------------------------------


def test_repo_agent_and_ref_keys_are_domain_separated():
    k = OrgKey("org_1", "k1", b"\x02" * 32)
    x = "a" * 40

    def mac(prefix):
        return hmac.new(k.key, prefix + x.encode(), hashlib.sha256).hexdigest()

    assert k.repo_key(x) == mac(b"brindle-repo-v1:")
    assert k.agent_ref(x) == mac(b"brindle-agent-v1:")[:32]
    assert k.ref(x) == mac(b"brindle-ref-v1:")
    assert len({k.repo_key(x), k.agent_ref(x), k.ref(x), k.ref(x)[:32], k.repo_key(x)[:32]}) == 5


# -- end to end through the cloud learner ---------------------------------------------------------------


@pytest.fixture
def learning_backend(backend):
    backend.records, backend.suggests = [], []

    def record(form, headers):
        backend.records.append(dict(form))
        return 200, {"recorded": True}

    def suggest(form, headers):
        backend.suggests.append(dict(form))
        extra = {"key_id": backend.announce} if getattr(backend, "announce", None) else {}
        return 200, {"profile": form["candidates"][0], **extra}

    backend.routes["POST /learning/record"] = record
    backend.routes["POST /learning/suggest"] = suggest
    return backend


def logged_in(backend, tmp_path, org="org_1"):
    store = credentials.FileStore(tmp_path / "pro")
    t = backend.issue()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + 900, "base_url": BASE,
                "entitlement": sign(backend.key, claims(org_id=org, features=["learning"]))})
    return store


def learner(backend, store, tmp_path, repo):
    return CloudLearner(str(repo), client=auth.Client(BASE, backend), store=store,
                        key_store=credentials.FileStore(tmp_path / "pro", account="learning-keys"),
                        cost=lambda n: 1, start_thread=False)


def task(repo, agent="w1"):
    return TaskInfo(repo_root=str(repo), task="Fix the crash", agent_id=agent, profile="developer")


def test_no_identity_means_nothing_is_sent(learning_backend, tmp_path):
    repo = new_repo(tmp_path / "empty", commit=False)
    store = logged_in(learning_backend, tmp_path)
    lr = learner(learning_backend, store, tmp_path, repo)
    lr.record(task(repo), Outcome("merged"))
    lr.flush()
    assert lr.suggest(task(repo), ["developer", "reviewer"]) is None
    assert learning_backend.records == [] and learning_backend.suggests == []
    assert learning_backend.key_fetches == []


def test_malformed_keys_are_refused(backend, tmp_path):
    store = logged_in(backend, tmp_path)
    ks = credentials.FileStore(tmp_path / "pro", account="learning-keys")
    for body in ({"org_id": "org_1", "key_id": "k1", "key": "c2hvcnQ="},
                 {"org_id": "org_other", "key_id": "k1", "key": "AA" * 22},
                 {"org_id": "org_1", "key_id": "bad id!", "key": "A" * 43 + "="}):
        backend.routes["GET /orgs/org_1/learning-key"] = [(200, body)]
        keys = OrgKeys(store=store, client=auth.Client(BASE, backend), key_store=ks)
        with pytest.raises(orgkey.OrgKeyUnavailable):
            keys.get("org_1")


def test_key_rotated_409_refetches_and_payload_names_the_key(learning_backend, tmp_path):
    repo = new_repo(tmp_path / "r")
    store = logged_in(learning_backend, tmp_path, org="org_team")
    lr = learner(learning_backend, store, tmp_path, repo)
    lr.suggest(task(repo), ["developer", "reviewer"])
    assert (learning_backend.suggests[-1]["org_id"], learning_backend.suggests[-1]["key_id"]) == \
        ("org_team", "k1")
    learning_backend.org_keys["org_team"] = ("k2", b"\x09" * 32)
    ok = learning_backend.routes["POST /learning/suggest"]
    learning_backend.routes["POST /learning/suggest"] = \
        lambda f, h: (409, {"error": "key_rotated", "key_id": "k2"})
    assert lr.suggest(task(repo), ["developer", "reviewer"]) is None     # no suggestion this time
    learning_backend.routes["POST /learning/suggest"] = ok
    lr.suggest(task(repo), ["developer", "reviewer"])
    assert learning_backend.key_fetches == ["org_team", "org_team"]
    assert learning_backend.suggests[-1]["key_id"] == "k2"
