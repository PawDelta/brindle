"""Enterprise cost centers on the client: repo attribution on spend events, and
the approval flow behind a budget refusal. Nothing happens without the
``cost_centers`` feature."""
import subprocess
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from brindle import budget, cull
from brindle.cli import app
from brindle.events import Event
from brindle.pro import cost_centers, license, team_events
from brindle.pro.orgkey import OrgKey

ORG = "org_ent1"
SLUG = "acme/widgets"


@pytest.fixture
def features(monkeypatch):
    held = {"cost", "cost_centers"}
    monkeypatch.setattr(license, "has", lambda f: f in held)
    monkeypatch.setattr(license, "current", lambda **kw: SimpleNamespace(org_id=ORG, features=frozenset(held)))
    return held


@pytest.fixture
def slug_repo(repo):
    subprocess.run(["git", "remote", "set-url", "origin", f"git@github.com:{SLUG}.git"], cwd=repo, check=True)
    return repo


class Server:
    def __init__(self):
        self.calls, self.status, self.month = [], "pending", cost_centers.month_now()

    def __call__(self, method, path, body=None, client=None, store=None):
        self.calls.append((method, path, body))
        if method == "POST":
            self.amount = body["amount_usd"]
            return {"id": "cr_1", "status": "pending", "month": self.month, "cost_center": "platform"}
        return {"id": "cr_1", "status": self.status, "month": self.month, "amount_usd": self.amount,
                "decision_note": "ok"}


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(cost_centers, "_call", s)
    return s


# -- attribution -------------------------------------------------------------------------------------


def test_repo_slug_from_origin(slug_repo, repo):
    assert cost_centers.repo_slug(str(slug_repo)) == SLUG


def test_repo_slug_none_without_a_remote_name(repo):
    assert cost_centers.repo_slug(str(repo)) is None


def _payload(repo, features_held):
    pe = team_events.ProEvents(str(repo), entitlement=lambda: SimpleNamespace(
        org_id=ORG, features=frozenset(features_held)), start_thread=False)
    key = OrgKey(ORG, "k1", b"k" * 32)
    ev = Event(kind="merge", repo_root=str(repo), agent_id="a1", branch="b", at=time.time())
    return team_events.event_payload(key, "id", ev, pe._slug(str(repo)))


def test_events_carry_repo_with_the_feature(slug_repo, features, monkeypatch):
    monkeypatch.setattr(team_events.OrgKey, "ref", lambda self, s: "ref", raising=False)
    assert _payload(slug_repo, {"team", "cost_centers"})["repo"] == SLUG


def test_events_omit_repo_without_the_feature(slug_repo, features, monkeypatch):
    monkeypatch.setattr(team_events.OrgKey, "ref", lambda self, s: "ref", raising=False)
    assert "repo" not in _payload(slug_repo, {"team"})


# -- requests ----------------------------------------------------------------------------------------


def test_request_files_and_tracks(slug_repo, features, server):
    rec = cost_centers.request(25, "need more", str(slug_repo))
    method, path, body = server.calls[0]
    assert (method, path) == ("POST", f"/orgs/{ORG}/cost-approvals")
    assert body == {"amount_usd": 25.0, "note": "need more", "repo": SLUG}
    assert rec["status"] == "pending" and [r["id"] for r in cost_centers.tracked()] == ["cr_1"]


def test_request_fails_closed_without_the_feature(repo, monkeypatch, server):
    monkeypatch.setattr(license, "has", lambda f: False)
    with pytest.raises(cost_centers.CostCenterError):
        cost_centers.request(5, "x", str(repo))
    assert not server.calls


def test_request_validates_amount_and_goal(repo, features, server):
    for bad in (0, -1, True, 10**9):
        with pytest.raises(cost_centers.CostCenterError):
            cost_centers.request(bad, "x", str(repo))
    with pytest.raises(cost_centers.CostCenterError):
        cost_centers.request(5, "x", str(repo), scope="goal")


def test_approval_raises_the_month_limit(repo, features, server):
    cost_centers.request(25, "need more", str(repo))
    assert cost_centers.month_raise() == 0
    server.status = "approved"
    lines = cost_centers.poll()
    assert lines and "approved" in lines[0]
    assert cost_centers.month_raise() == 25.0
    lim = budget.limits(SimpleNamespace(budget={"month_usd": 100}))
    assert lim.month_usd == 125.0


def test_denied_raises_nothing(repo, features, server):
    cost_centers.request(25, "x", str(repo))
    server.status = "denied"
    assert "denied" in cost_centers.poll()[0]
    assert cost_centers.month_raise() == 0


def test_approval_applies_only_to_its_month(repo, features, server):
    cost_centers.request(25, "x", str(repo))
    server.status, server.month = "approved", "2001-01"
    cost_centers.poll()
    assert cost_centers.month_raise() == 0


def test_goal_approval_raises_only_that_goal(repo, features, server):
    cost_centers.request(10, "x", str(repo), scope="goal", goal="Ship it")
    server.status = "approved"
    cost_centers.poll()
    assert cost_centers.goal_raise("Ship it") == 10.0
    assert cost_centers.goal_raise("Other") == 0 and cost_centers.month_raise() == 0


def test_grants_stop_applying_when_the_feature_is_lost(repo, features, server, monkeypatch):
    cost_centers.request(25, "x", str(repo))
    server.status = "approved"
    cost_centers.poll()
    monkeypatch.setattr(license, "has", lambda f: f == "cost")
    assert cost_centers.month_raise() == 0


def test_poll_survives_server_errors(repo, features, server, monkeypatch):
    cost_centers.request(25, "x", str(repo))

    def boom(*a, **k):
        raise cost_centers.CostCenterError("down")
    monkeypatch.setattr(cost_centers, "_call", boom)
    assert cost_centers.poll() == []
    assert cost_centers.tracked()[0]["status"] == "pending"


def test_cull_poll_is_rate_limited(repo, features, server):
    cost_centers.request(25, "x", str(repo))
    now = time.time()
    cost_centers.poll(only_due=True, now=now)
    n = len(server.calls)
    cost_centers.poll(only_due=True, now=now + 10)
    assert len(server.calls) == n
    cost_centers.poll(only_due=True, now=now + cost_centers.POLL_EVERY + 1)
    assert len(server.calls) == n + 1


# -- refusal and CLI -------------------------------------------------------------------------------------


def test_refusal_offers_the_request_command_only_when_entitled(features, monkeypatch):
    assert "brindle cost request" in budget.refusal(["x"])
    monkeypatch.setattr(license, "has", lambda f: f == "cost")
    assert "brindle cost request" not in budget.refusal(["x"])


def test_cli_request_and_list(slug_repo, features, server, monkeypatch):
    monkeypatch.chdir(slug_repo)
    r = CliRunner().invoke(app, ["cost", "request", "--usd", "25", "--reason", "need more"])
    assert r.exit_code == 0 and "cr_1" in r.output
    server.status = "approved"
    r = CliRunner().invoke(app, ["cost", "requests"])
    assert r.exit_code == 0 and "approved" in r.output


def test_cli_fails_closed_without_the_feature(repo, monkeypatch, server):
    monkeypatch.setattr(license, "has", lambda f: False)
    monkeypatch.chdir(repo)
    assert CliRunner().invoke(app, ["cost", "request", "--usd", "5", "--reason", "x"]).exit_code == 1
    assert CliRunner().invoke(app, ["cost", "requests"]).exit_code == 1
    assert not server.calls
