"""The cloud learner: what it sends, what it never sends, and how it falls
back (to no suggestion: the learner only ever runs on the server)."""
import json
import threading
import time

import pytest

from brindle import plugins
from brindle.learning import Outcome, TaskInfo
from brindle.pro import auth, credentials
from brindle.pro import learning as cloud
from brindle.pro.learning import CloudLearner
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, backend, claims, fixed_identity, pro_env, sign, signing_key, token,
)

REPO = "/work/secret-client-project"
TEXT = "Fix the crash in src/zebra_module.py on branch feat/zebra-hotfix"
FILES = ("src/zebra_module.py", "tests/test_zebra.py")
AGENT = "worker-zebra-7"
FORBIDDEN = ("zebra", "src/", "tests/", "secret-client-project", "/work", "feat/", AGENT,
             "claude", "opus", "Fix the crash")
COSTS = {"developer": 2, "reviewer": 3, "cheap": 0}


def task(agent_id=AGENT, profile="developer", weight="heavy", text=TEXT, files=FILES):
    return TaskInfo(repo_root=REPO, task=text, files=tuple(files), weight=weight,
                    agent_id=agent_id, profile=profile, provider="claude", model="claude-opus-4",
                    started_at=time.time() - 60)


@pytest.fixture
def remote(backend):
    """The fake backend plus the hosted-learning endpoints."""
    backend.records, backend.suggests = [], []
    backend.suggest_pick = None
    backend.suggest_note = None

    def record(form, headers):
        if not backend._bearer(headers):
            return 401, {"error": "invalid_token"}
        backend.records.append(dict(form))
        return 200, {"recorded": True, "applied": form["event"] == "merged"}

    def suggest(form, headers):
        if not backend._bearer(headers):
            return 401, {"error": "invalid_token"}
        backend.suggests.append(dict(form))
        pick = backend.suggest_pick or form["default"]
        over = pick != form["default"]
        reply = {"profile": pick, "overrode": over,
                 "reason": "cheaper and as good" if over else None, "key_id": form["key_id"]}
        if backend.suggest_note is not None:
            reply["note"] = backend.suggest_note
        return 200, reply

    backend.routes["POST /learning/record"] = record
    backend.routes["POST /learning/suggest"] = suggest
    return backend


def login_as(backend, store, features=("learning",)):
    t = backend.issue()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + 900, "base_url": BASE,
                "entitlement": sign(backend.key, claims(features=list(features)))})


@pytest.fixture
def parts(tmp_path, remote, fixed_identity):
    store = credentials.FileStore(tmp_path / "pro")
    secrets_ = credentials.FileStore(tmp_path / "pro", account="learning-keys")
    login_as(remote, store)
    return store, secrets_


def make(parts, backend, **kw):
    store, secrets_ = parts
    kw.setdefault("cost", lambda n: COSTS.get(n, 2))
    return CloudLearner(REPO, client=auth.Client(BASE, backend), store=store,
                        key_store=secrets_, start_thread=kw.pop("start_thread", False), **kw)


def no_forbidden(body):
    blob = json.dumps(body)
    for s in FORBIDDEN:
        assert s not in blob, s


# -- payloads ---------------------------------------------------------------------------------


def test_record_payload_keys_and_contents(parts, remote):
    lr = make(parts, remote)
    lr.record(task(), Outcome("reported", wall_seconds=12.5))
    lr.record(task(), Outcome("review", approved=False))
    lr.record(task(), Outcome("escalated"))
    lr.record(task(), Outcome("merged", checks_passed=True, tokens=1234, wall_seconds=61.5))
    lr.flush()
    reported, review, escalated, merged = remote.records
    assert set(review) == {"org_id", "key_id", "repo_key", "agent_ref", "profile", "weight", "features", "event",
                           "approved", "checks_passed", "review_rounds", "escalations",
                           "tokens", "wall_seconds"}
    assert set(reported) == set(escalated) == set(merged) == set(review) - {"approved"}
    assert reported["event"] == "reported" and reported["wall_seconds"] == 12.5
    assert review["approved"] is False and review["event"] == "review"
    assert (review["org_id"], review["key_id"]) == ("org_1", "k1")
    assert merged == {**merged, "profile": "developer", "weight": "heavy", "event": "merged",
                      "checks_passed": True, "review_rounds": 1, "escalations": 1,
                      "tokens": 1234, "wall_seconds": 61.5}
    for body in remote.records:
        no_forbidden(body)
        assert set(body) <= cloud.RECORD_KEYS


def test_an_override_carries_the_servers_reason(parts, remote):
    remote.suggest_pick = "reviewer"
    lr = make(parts, remote)
    assert lr.suggest(task(), ["developer", "reviewer"], "developer") == "reviewer"
    assert lr.last_reason == "cheaper and as good"
    assert remote.suggests[0]["default"] == "developer"
    remote.suggest_pick = None
    assert lr.suggest(task(), ["developer", "reviewer"], "developer") == "developer"
    assert lr.last_reason is None


def test_unsafe_profile_names_are_never_sent(parts, remote):
    lr = make(parts, remote)
    lr.record(task(profile="my profile/../x"), Outcome("merged"))
    lr.flush()
    assert remote.records == []
    assert lr.suggest(task(), ["developer", "/etc/passwd"]) is None
    assert remote.suggests == []


def test_records_are_sent_by_the_background_thread(parts, remote, monkeypatch):
    # The send is held until flush is polling, so flush's wait loop always runs.
    polling = threading.Event()
    send = remote.routes["POST /learning/record"]
    remote.routes["POST /learning/record"] = lambda f, h: (polling.wait(5), send(f, h))[1]
    monkeypatch.setattr(cloud.time, "sleep", lambda _: polling.set())
    lr = make(parts, remote, start_thread=True)
    lr.record(task(), Outcome("merged"))
    assert lr.flush(5)
    assert len(remote.records) == 1


def test_the_record_queue_is_bounded(parts, remote, monkeypatch):
    monkeypatch.setattr(cloud, "QUEUE_SIZE", 3)
    lr = make(parts, remote)
    for i in range(5):
        lr.record(task(agent_id=f"w{i}"), Outcome("merged"))
    assert lr.queue.qsize() == 3 and lr.dropped == 2


def test_cost_rank_guesses_from_the_profile(tmp_path):
    ranks = [cloud.cost_rank(n, str(tmp_path))
             for n in ("no-such-profile", "developer", "developer-heavy", "developer-local")]
    assert ranks == [2, 2, 3, 0]


# -- repo_key and agent_ref ---------------------------------------------------------------------------


def test_the_legacy_install_secret_is_never_used(parts, remote, tmp_path):
    legacy = credentials.FileStore(tmp_path / "pro", account=credentials.LEARNING_SECRET)
    legacy.save({"secret": "ab" * 32})
    assert cloud.install_secret(legacy) == bytes.fromhex("ab" * 32)
    remote.key_status = (403, {"error": "entitlement_required"})
    lr = make(parts, remote)
    assert lr.suggest(task(), ["developer", "reviewer"]) is None
    lr.record(task(), Outcome("merged"))
    lr.flush()
    assert remote.records == [] and remote.suggests == []


# -- fallbacks ----------------------------------------------------------------------------------------


def fallback_case(parts, remote):
    lr = make(parts, remote)
    assert lr.suggest(task(), ["developer", "reviewer"]) is None   # brindle routes by weight
    lr.record(task(), Outcome("merged"))
    lr.flush()
    return lr


def test_offline_gives_no_suggestion(parts, remote):
    remote.routes["POST /learning/suggest"] = [auth.TransportError("down")]
    lr = fallback_case(parts, remote)
    assert not lr.active()     # backs off instead of retrying every call
    assert remote.records == []


@pytest.mark.parametrize("status,error", [(429, "rate_limited"), (403, "entitlement_required")])
def test_server_refusals_give_no_suggestion(parts, remote, status, error):
    remote.routes["POST /learning/suggest"] = [(status, {"error": error})]
    assert not fallback_case(parts, remote).active()


def test_a_profile_outside_the_candidates_is_ignored(parts, remote):
    remote.routes["POST /learning/suggest"] = [(200, {"profile": "rogue"})]
    assert make(parts, remote).suggest(task(), ["developer", "reviewer"]) is None


def test_report_says_whether_cloud_is_active(parts, remote):
    assert make(parts, remote).report().startswith("cloud learning: active")
    login_as(remote, parts[0], features=())
    assert "inactive" in make(parts, remote).report()


# -- nothing is learned on this machine -------------------------------------------------------------


def test_records_and_suggestions_go_to_the_server(parts, remote):
    lr = make(parts, remote)
    assert lr.suggest(task(), ["developer", "reviewer"]) == "developer"
    lr.record(task(), Outcome("review", approved=True))
    lr.record(task(), Outcome("merged"))
    lr.flush()
    assert [r["event"] for r in remote.records] == ["review", "merged"]
    assert remote.records[-1]["review_rounds"] == 1


# -- wiring --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_plugins():
    plugins.reset()
    yield
    plugins.reset()


def test_brindle_learning_command_explains_auto(repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["learning"])
    assert res.exit_code == 0, res.output
    assert "brindle Pro" in res.output and "brindle account upgrade" in res.output


def test_brindle_learning_command_rejects_unsupported_value(repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"learning": "myplugin"}')
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["learning"])
    assert res.exit_code == 0, res.output
    assert "not a supported value" in res.output


# -- brindle learning seed ------------------------------------------------------------------------------


def seed_history(db, root, n=3):
    """``n`` finished decisions (with task rows holding text and files), plus one still running."""
    from brindle.db import Task

    for i in range(n):
        tid, aid = f"t{i}", f"worker-zebra-{i}"
        db.add_task(Task(id=tid, repo_root=root, agent_id=aid, caller_id=None, caller_ws_id="ws",
                         profile="developer", task_text=TEXT, mode="x", isolate=1, branch="feat/zebra-hotfix",
                         done_when=None, files=json.dumps(list(FILES)), depends_on=None,
                         state="merged", created_at=time.time(), weight="heavy"))
        db.add_routing_decision(root, task_id=tid, agent_id=aid, weight=None, baseline_profile="reviewer",
                                profile="developer", learned=bool(i % 2))
        db.note_routing_outcome(aid, review=True, outcome="merged" if i != 1 else "removed_unmerged")
    db.add_routing_decision(root, task_id="running", agent_id="worker-running", weight="light",
                            baseline_profile="developer", profile="developer", learned=False)


@pytest.fixture
def seed_cli(parts, remote, repo, db, monkeypatch):
    from typer.testing import CliRunner

    from brindle import git
    from brindle.cli import app

    monkeypatch.setattr(cloud, "CloudLearner", lambda root, **kw: make(parts, remote))
    monkeypatch.chdir(repo)
    root = git.main_repo_root(str(repo))
    seed_history(db, root)
    return lambda: CliRunner().invoke(app, ["learning", "seed"]), root


def test_seed_marks_merged_as_checks_passed_and_carries_the_live_agent_ref(seed_cli, parts, remote):
    run, _ = seed_cli
    assert run().exit_code == 0
    by_event = {r["event"]: r for r in remote.records}
    assert by_event["merged"]["checks_passed"] is True
    assert by_event["removed_unmerged"]["checks_passed"] is False
    live = make(parts, remote)
    expect = {live.key(live.org()).agent_ref(f"worker-zebra-{i}") for i in range(3)}
    assert {r["agent_ref"] for r in remote.records} == expect


def test_seed_without_the_learning_entitlement_exits_nonzero(seed_cli, parts, remote):
    run, _ = seed_cli
    login_as(remote, parts[0], features=())
    res = run()
    assert res.exit_code != 0
    assert "learning entitlement" in res.output
    assert remote.records == []
