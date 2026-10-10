"""Pausing agents when their provider's sign-in goes, resuming them when it's back."""

from __future__ import annotations

import pytest

from brindle import agents, company_identity, signin_pause
from brindle.company_identity import Identity
from brindle.db import Agent, Workspace
from brindle.pro.team_policy import AgentSetup, AwsSetup, ClaudeSetup

AWS = AwsSetup("https://acme.awsapps.com/start", "us-east-1", "123456789012", "BrindleWorker", "us-west-2")

OK = Identity("claude", "Claude: org Acme", ident="claude org Acme (org-1)")
PERSONAL = Identity("claude", "Claude: org Me", ident="claude org Me (org-9)")
OUT = Identity("claude", "Claude: signed out", signed_out=True)
UNKNOWN = Identity("claude", "claude isn't installed", determined=False)
WRONG = Identity("claude", "Claude: org Other", problem="Claude is signed in to Other",
                 ident="claude org Other (org-2)")
CODEX_OK = Identity("codex", "Codex: signed in")
CODEX_OUT = Identity("codex", "Codex: not signed in", signed_out=True)
AWS_OK = Identity("claude", "AWS account 123456789012, role BrindleWorker",
                  ident="AWS account 123456789012, role BrindleWorker")
NO_ID = signin_pause.NO_IDENTITY


class World:
    """Faked probes, pause, resume and messages; ``db`` holds real agent rows."""

    def __init__(self, db, monkeypatch):
        self.db = db
        db.add_workspace(Workspace("ws", "/repo", "ws", "worktree", "feat/x", "main", "/repo/ws",
                                   None, "brindle_ws", 0.0))
        self.claude, self.codex = OK, CODEX_OK
        self.enforce, self.route, self.org_id = False, None, None
        self.fp = "fp1"
        self.claude_probes = self.codex_probes = 0
        self.resumed, self.messages = [], []
        self.fail_resume = False
        monkeypatch.setattr(company_identity, "_setup", self.setup)
        monkeypatch.setattr(company_identity, "_local_setup", lambda: None)
        monkeypatch.setattr(company_identity, "claude_identity", self.probe_claude)
        monkeypatch.setattr(company_identity, "codex_identity", self.probe_codex)
        monkeypatch.setattr(company_identity, "_fingerprint", lambda route: self.fp)
        monkeypatch.setattr(agents, "pause_worker", lambda db, a: db.set_status(a.id, "paused"))
        monkeypatch.setattr(agents, "resume", self.resume)
        monkeypatch.setattr(agents, "send_message", self.send)

    def setup(self):
        claude = ClaudeSetup(route=self.route, org_id=self.org_id, aws=AWS) if self.route or self.org_id else None
        return "acme", AgentSetup(claude=claude, codex=None, enforce=self.enforce)

    def probe_claude(self, org_id, claude, cache=True):
        self.claude_probes += 1
        return self.claude

    def probe_codex(self, cache=True):
        self.codex_probes += 1
        return self.codex

    def resume(self, db, root_id, *, watch_pane=True, only=None):
        if self.fail_resume:
            raise RuntimeError("the session failed to restart")
        out =[a for a in agents.tree(db, root_id) if a.status == "paused" and (only is None or a.id in only)]
        for a in out:
            db.set_status(a.id, "processing")
            signin_pause.record_launch(a)   # as agents._launch does: a resume records who it runs as
        self.resumed += [a.id for a in out]
        return out

    def send(self, db, to, body, sender_id=None):
        if (db.get_agent(to) or Agent("x", "w", "p", "claude", None, "assign", "paused", "", None, 0)).status == "paused":
            raise agents.AgentError("paused")
        self.messages.append((to, body))

    def agent(self, aid, provider="claude", parent="chat", status="processing"):
        self.db.add_agent(Agent(aid, "ws", "developer", provider, parent, "assign", status, "", None, 0.0))
        return aid

    def status(self, aid):
        return self.db.get_agent(aid).status


_REAL_CHECK = signin_pause.check


@pytest.fixture
def w(db, monkeypatch):
    monkeypatch.setattr(signin_pause, "check", _REAL_CHECK)
    world = World(db, monkeypatch)
    world.agent("chat", provider="shell", parent=None)   # the person's session: not a checked provider
    return world


def test_logout_pauses_only_that_providers_agents_and_keeps_their_state(w):
    w.agent("c1")
    w.agent("x1", provider="codex")
    w.db.update_agent("c1", session_ref="sess-1", task="build it")
    w.claude = OUT
    done = signin_pause.run(w.db)
    assert w.status("c1") == "paused" and w.status("chat") == "processing"
    assert w.status("x1") == "processing"
    kept = w.db.get_agent("c1")
    assert (kept.session_ref, kept.task, kept.workspace_id) == ("sess-1", "build it", "ws")
    assert signin_pause.paused_for_signin()["c1"]["reason"] == NO_ID   # nothing known to come back under
    assert any("paused agent c1" in line for line in done)


def test_a_running_interactive_chat_is_neither_probed_nor_paused(w):
    w.db.add_agent(Agent("me", "ws", "developer", "claude", None, "interactive", "idle", "", None, 0.0))
    signin_pause.run(w.db)
    assert w.claude_probes == 0
    w.agent("c1", parent="me")
    signin_pause.record_launch(w.db.get_agent("c1"))
    w.claude = OUT
    signin_pause.run(w.db)
    assert w.status("c1") == "paused" and w.status("me") == "idle"
    assert [to for to, _ in w.messages] == ["me"]
    w.claude = OK
    signin_pause.run(w.db)
    assert w.status("c1") == "processing" and w.resumed == ["c1"]


def test_codex_logout_pauses_codex_agents_only(w):
    w.agent("c1")
    w.agent("x1", provider="codex")
    w.codex = CODEX_OUT
    signin_pause.run(w.db)
    assert w.status("x1") == "paused" and w.status("c1") == "processing"
    assert signin_pause.paused_for_signin()["x1"]["reason"] == NO_ID   # no Codex identity is known to return to


def test_logout_tells_the_supervisor_and_the_session_chat(w):
    w.agent("boss", provider="codex", parent="chat")    # another provider's: keeps running
    w.agent("c1", parent="boss")
    w.claude = OUT
    signin_pause.run(w.db)
    assert {to for to, _ in w.messages} == {"boss", "chat"}
    assert all("signed out" in body for _, body in w.messages)
    assert w.status("boss") == "processing"


def test_undetermined_never_pauses_and_warns_once(w):
    w.agent("c1")
    w.claude = UNKNOWN
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    assert w.status("c1") == "processing"
    assert signin_pause.paused_for_signin() == {}
    assert [to for to, b in w.messages if "can't be verified" in b] == ["chat"]
    w.claude = OK
    signin_pause.run(w.db)
    w.claude = UNKNOWN          # a new spell of it warns again
    signin_pause.run(w.db)
    assert sum("can't be verified" in b for _, b in w.messages) == 2


def test_mismatch_pauses_only_with_enforce(w):
    w.route, w.org_id = "subscription", "org-1"    # the org expects an identity to come back under
    w.agent("c1")
    w.claude = WRONG
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    assert w.status("c1") == "processing"
    assert sum("signed in to Other" in b for _, b in w.messages) == 1
    w.enforce = True
    signin_pause.run(w.db)
    assert w.status("c1") == "paused"
    assert "wrong identity" in signin_pause.paused_for_signin()["c1"]["reason"]


def test_recovery_resumes_only_agents_paused_for_signin(w):
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    w.agent("other", status="paused")      # paused by something else
    w.claude = OUT
    signin_pause.run(w.db)
    w.claude = OK
    done = signin_pause.run(w.db)
    assert w.status("c1") == "processing" and w.status("other") == "paused"
    assert "other" not in w.resumed and "c1" in w.resumed
    assert signin_pause.paused_for_signin() == {}
    assert any("resumed agent c1" in line for line in done)


def test_a_resume_that_raises_is_retried_next_tick_and_stays_listed(w):
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    w.claude = OUT
    signin_pause.run(w.db)
    w.claude = OK
    w.fail_resume = True
    done = signin_pause.run(w.db)
    assert w.status("c1") == "paused" and w.resumed == []
    assert "c1" in signin_pause.paused_for_signin()
    (line,) = signin_pause.checks()
    assert "c1" in line.detail
    assert not any("resumed agent c1" in line for line in done)
    w.fail_resume = False
    signin_pause.run(w.db)           # the next tick retries it
    assert w.status("c1") == "processing" and w.resumed == ["c1"]
    assert signin_pause.paused_for_signin() == {}


def test_a_partly_failed_pause_keeps_the_paused_agents_tracked_and_resumes_them(w, monkeypatch):
    w.agent("c1")
    w.agent("c2")
    signin_pause.record_launch(w.db.get_agent("c1"))
    real_pause = agents.pause_worker

    def pause_c2_fails(db, a):
        if a.id == "c2":
            raise RuntimeError("the worktree is locked")
        real_pause(db, a)

    monkeypatch.setattr(agents, "pause_worker", pause_c2_fails)
    w.claude = OUT
    signin_pause.run(w.db)           # must not raise: the failure is per agent
    assert w.status("c2") == "processing"
    paused = signin_pause.paused_for_signin()
    assert w.status("c1") == "paused" and "c1" in paused and "c2" not in paused
    monkeypatch.setattr(agents, "pause_worker", real_pause)
    w.claude = OK
    signin_pause.run(w.db)
    assert w.status("c1") == "processing" and "c1" in w.resumed
    assert signin_pause.paused_for_signin() == {}


def test_the_pause_notice_lists_only_the_agents_actually_paused(w, monkeypatch, caplog):
    w.agent("c1")
    w.agent("boss", provider="codex", parent="chat")
    w.agent("c2", parent="boss")     # its supervisor must not be told about a pause that didn't happen
    real_pause = agents.pause_worker

    def pause_c2_fails(db, a):
        if a.id == "c2":
            raise RuntimeError("the worktree is locked")
        real_pause(db, a)

    monkeypatch.setattr(agents, "pause_worker", pause_c2_fails)
    w.claude = OUT
    with caplog.at_level("DEBUG", logger=signin_pause.log.name):
        signin_pause.run(w.db)
    notices = [b for to, b in w.messages if to == "chat" and "Pausing" in b]
    assert len(notices) == 1 and notices[0].startswith("[brindle] Pausing 1 Claude agent(s)")
    assert not any(to == "boss" for to, _ in w.messages)        # c2's supervisor: nothing paused there
    assert "couldn't pause agent c2" in caplog.text             # the failure is logged


def test_a_mismatching_identity_is_never_recorded_as_the_agents_own(w):
    w.agent("c1")
    w.claude = WRONG                 # running under another account, not enforced
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    assert "c1" not in signin_pause._load()["identity"]
    assert w.status("c1") == "processing"
    w.claude = OK                    # the company account later: the agent takes that one
    signin_pause.run(w.db)
    assert signin_pause._load()["identity"] == {"c1": OK.ident}


def test_nothing_is_probed_without_running_agents(w):
    w.agent("done1", status="done")
    w.agent("p1", status="paused")
    assert signin_pause.run(w.db) == []
    assert w.claude_probes == 0 and w.codex_probes == 0


def test_only_providers_with_running_agents_are_probed(w):
    w.agent("c1")
    signin_pause.run(w.db)
    assert w.claude_probes == 1 and w.codex_probes == 0


def test_sweep_waits_thirty_seconds_between_checks(w):
    w.agent("c1")
    signin_pause.sweep(w.db, 1000.0)
    signin_pause.sweep(w.db, 1010.0)
    assert w.claude_probes == 1
    signin_pause.sweep(w.db, 1031.0)
    assert w.claude_probes == 2


def test_cloud_route_reprobes_only_when_the_fingerprint_changes(w):
    w.route = "bedrock"
    w.agent("c1")
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    assert w.claude_probes == 1
    w.fp = "fp2"
    signin_pause.run(w.db)
    assert w.claude_probes == 2


def test_cloud_logout_then_login_follows_the_fingerprint(w):
    w.route = "bedrock"
    w.agent("c1")
    w.claude = OUT
    signin_pause.run(w.db)
    assert w.status("c1") == "paused"
    w.claude = AWS_OK
    signin_pause.run(w.db)           # nothing changed on disk: no new probe, still paused
    assert w.status("c1") == "paused" and w.claude_probes == 1
    w.fp = "fp2"
    signin_pause.run(w.db)           # the org expects this AWS account: it resumes on it
    assert w.status("c1") == "processing"


def test_subscription_login_is_probed_every_time(w):
    w.agent("c1")
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    assert w.claude_probes == 2


def test_gateway_and_antigravity_are_not_verified(w, monkeypatch):
    w.route = "gateway"
    w.agent("c1")
    w.agent("g1", provider="antigravity")
    signin_pause.run(w.db)
    assert w.claude_probes == 0 and w.status("g1") == "processing"


# -- who it runs as --------------------------------------------------------------------------


def test_a_personal_login_after_logout_never_resumes_the_company_conversation(w):
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    w.claude = OUT
    signin_pause.run(w.db)
    w.claude = PERSONAL
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    assert w.status("c1") == "paused" and w.resumed == []
    assert "signed in as a different account (claude org Me (org-9))" == \
        signin_pause.paused_for_signin()["c1"]["reason"]
    assert sum("stays paused" in b for _, b in w.messages) == 1    # told once, not every check
    w.claude = OK        # back to the company account: it resumes
    signin_pause.run(w.db)
    assert w.status("c1") == "processing"


def test_logout_then_the_same_company_login_resumes(w):
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    w.claude = OUT
    signin_pause.run(w.db)
    w.claude = OK
    signin_pause.run(w.db)
    assert w.status("c1") == "processing" and "c1" in w.resumed


def test_launch_records_the_identity_without_overwriting_it(w):
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    w.claude = PERSONAL
    signin_pause.record_launch(w.db.get_agent("c1"))
    assert signin_pause._load()["identity"]["c1"] == OK.ident


def test_the_state_file_holds_ids_only_and_is_private(w, brindle_home):
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    path = signin_pause._path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "token" not in path.read_text().lower()


def test_doctor_lists_agents_paused_for_signin(w):
    assert signin_pause.checks() == []
    w.agent("c1")
    w.claude = OUT
    signin_pause.run(w.db)
    (line,) = signin_pause.checks()
    assert "c1" in line.detail and NO_ID in line.detail


def test_unrecorded_agent_resumes_only_on_the_org_expected_identity(w):
    w.route, w.org_id = "subscription", "org-1"
    w.agent("c1")
    w.claude = OUT
    signin_pause.run(w.db)
    entry = signin_pause.paused_for_signin()["c1"]
    assert entry == {"provider": "claude", "reason": "Claude signed out", "identity": "org:org-1"}
    w.claude = PERSONAL              # someone else's login: stays paused
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    assert w.status("c1") == "paused" and w.resumed == []
    w.claude = OK                    # the org's account: resumes
    signin_pause.run(w.db)
    assert w.status("c1") == "processing" and w.resumed == ["c1"]


def test_unrecorded_agent_with_nothing_known_never_auto_resumes_and_resumes_by_hand(w):
    w.agent("c1")
    w.claude = OUT
    signin_pause.run(w.db)
    assert signin_pause.paused_for_signin()["c1"] == {"provider": "claude", "reason": NO_ID, "identity": ""}
    w.claude = OK
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    assert w.status("c1") == "paused" and w.resumed == []
    (line,) = signin_pause.checks()  # shown in doctor
    assert "c1" in line.detail and NO_ID in line.detail
    agents.resume(w.db, "chat", only={"c1"})   # the person resumes it by hand
    assert w.status("c1") == "processing"
    assert signin_pause._load()["identity"]["c1"] == OK.ident   # then-current identity recorded
    signin_pause.run(w.db)
    assert signin_pause.paused_for_signin() == {}
    assert signin_pause.checks() == []
