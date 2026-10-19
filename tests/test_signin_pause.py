"""Pausing agents when their provider's sign-in goes, resuming them when it's back."""

from __future__ import annotations

import pytest

from brindle import agents, company_identity, company_login, signin_pause
from brindle.company_identity import Identity
from brindle.db import Agent, Workspace
from brindle.pro.team_policy import AgentSetup, AwsSetup, ClaudeSetup

AWS = AwsSetup("us-west-2", "https://acme.awsapps.com/start", "us-east-1", "123456789012", "BrindleWorker")

OK = Identity("claude", "Claude: org Acme", ident="claude org Acme (org-1)")
PERSONAL = Identity("claude", "Claude: org Me", ident="claude org Me (org-9)")
OUT = Identity("claude", "Claude: signed out", signed_out=True)
UNKNOWN = Identity("claude", "claude isn't installed", determined=False)
WRONG = Identity("claude", "Claude: org Other", problem="Claude is signed in to Other",
                 ident="claude org Other (org-2)")
CODEX_OK = Identity("codex", "Codex: signed in")
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
        # Company login stored the org's route env: panes run on the route.
        monkeypatch.setattr(company_login, "route_in_use", lambda org_id, setup: True)
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
_REAL_ROUTE_IN_USE = company_login.route_in_use


@pytest.fixture
def w(db, monkeypatch):
    monkeypatch.setattr(signin_pause, "check", _REAL_CHECK)
    world = World(db, monkeypatch)
    world.agent("chat", provider="shell", parent=None)   # the person's session: not a checked provider
    return world


def test_undetermined_not_repeated_never_pauses_and_warns_once(w):
    w.agent("c1")
    w.claude = UNKNOWN
    signin_pause.run(w.db)
    signin_pause.run(w.db)
    assert w.status("c1") == "processing"
    assert signin_pause.paused_for_signin() == {}
    assert [to for to, b in w.messages if "can't be verified" in b] == ["chat"]
    signin_pause.reset()        # the next sweep runs in another process: still told once
    signin_pause.run(w.db)
    assert sum("can't be verified" in b for _, b in w.messages) == 1
    w.claude = OK
    for _ in range(signin_pause.SETTLED_SWEEPS):
        signin_pause.run(w.db)
    w.claude = UNKNOWN          # a new spell of it warns again
    signin_pause.run(w.db)
    assert sum("can't be verified" in b for _, b in w.messages) == 2
    for _ in range(3):      # a check flapping under load (times out every other sweep) stays quiet
        w.claude = OK
        signin_pause.run(w.db)
        w.claude = UNKNOWN
        signin_pause.run(w.db)
    assert sum("can't be verified" in b for _, b in w.messages) == 2


def test_an_org_route_not_in_use_checks_the_persons_own_signin(w, monkeypatch):
    # The org sets up a federated Bedrock route, but company login stored no env
    # and the policy doesn't enforce: panes run on the person's own sign-in.
    w.route = "bedrock"
    monkeypatch.setattr(company_login, "route_in_use", _REAL_ROUTE_IN_USE)
    monkeypatch.setattr(company_identity, "claude_identity", lambda org_id, claude, cache=True: (
        Identity("claude", "AWS federated role: no credential fetched yet", determined=False)
        if claude is not None else w.claude))
    w.agent("c1")
    w.claude = PERSONAL
    assert signin_pause.check("claude")[0].ident == PERSONAL.ident
    signin_pause.run(w.db)
    assert w.messages == [] and w.status("c1") == "processing"
    w.claude = OUT              # a real problem with the person's own sign-in still shows
    signin_pause.run(w.db)
    assert w.status("c1") == "paused"


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


def test_the_state_file_holds_ids_only_and_is_private(w, brindle_home):
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    path = signin_pause._path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "token" not in path.read_text().lower()


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


# -- agents launched while no one is signed in to brindle ----------------------------------------

OTHER_ORG = Identity("claude", "Claude: org Other", ident="claude org Other (org-2)")


@pytest.fixture
def brindle_login(monkeypatch):
    """Whether someone is signed in to brindle: ``login["in"]``."""
    login = {"in": False}
    monkeypatch.setattr(company_login, "_logged_in", lambda store=None, client=None: login["in"])
    return login


def test_a_signed_out_agent_is_adopted_by_the_account_that_signs_in(w, brindle_login):
    from brindle import identity_lock

    w.claude = PERSONAL                # no brindle login: Claude runs on the person's own sign-in
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    assert signin_pause._load()["unowned"] == ["c1"]
    brindle_login["in"], w.claude = True, OK     # signing in switches Claude to the org's route
    assert identity_lock.locked_by("c1") == PERSONAL.ident   # what this fixes
    assert signin_pause.adopt(OK.ident) == ["c1"]
    assert signin_pause.recorded_identity("c1") == OK.ident
    assert signin_pause._load()["unowned"] == []
    assert identity_lock.locked_by("c1") is None
    w.claude = OTHER_ORG               # adopted: now it's the account's, and locks for another
    assert identity_lock.locked_by("c1") == OK.ident
    w.claude = PERSONAL                # the identity it launched under no longer owns it either
    assert identity_lock.locked_by("c1") == OK.ident


def test_an_agent_recorded_under_a_brindle_login_is_never_adopted(w, brindle_login):
    from brindle import identity_lock

    brindle_login["in"], w.claude = True, PERSONAL
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    brindle_login["in"] = False
    w.agent("c2")
    signin_pause.run(w.db)             # the sweep records c2 too: no login, so unowned
    assert signin_pause._load()["unowned"] == ["c2"]
    brindle_login["in"], w.claude = True, OK
    assert signin_pause.adopt(OK.ident) == ["c2"]
    assert identity_lock.locked_by("c1") == PERSONAL.ident   # stays locked exactly as before
    assert identity_lock.locked_by("c2") is None
    assert signin_pause.adopt(OTHER_ORG.ident) == []          # adopted once, not by each sign-in
    assert signin_pause.recorded_identity("c2") == OK.ident


def test_a_setup_applied_with_no_brindle_login_leaves_agents_unowned(w, brindle_login):
    w.claude = PERSONAL
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    assert signin_pause.adopt(OK.ident, signed_in=False) == ["c1"]
    assert signin_pause.recorded_identity("c1") == OK.ident
    assert signin_pause.adopt(OTHER_ORG.ident) == ["c1"]     # the brindle sign-in still adopts it
    assert signin_pause._load()["unowned"] == []


def test_an_agent_gone_is_dropped_from_the_unowned(w, brindle_login):
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    w.db.delete_agent("c1")
    signin_pause.run(w.db)
    assert signin_pause._load()["unowned"] == []


def test_an_old_state_file_without_unowned_loads(w, brindle_login):
    import json

    path = signin_pause._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"paused": {}, "identity": {"c1": PERSONAL.ident}, "different": {},
                                "warned": {}}))
    assert signin_pause._load()["unowned"] == []
    assert signin_pause.recorded_identity("c1") == PERSONAL.ident
    assert signin_pause.adopt(OK.ident) == []     # recorded before this: never adopted
    assert signin_pause.recorded_identity("c1") == PERSONAL.ident
    path.write_text(json.dumps({"identity": {}, "unowned": "c1"}))   # unreadable: treated as none
    assert signin_pause._load()["unowned"] == []


def test_adopt_never_raises(w, monkeypatch):
    monkeypatch.setattr(signin_pause, "_load", lambda: 1 / 0)
    assert signin_pause.adopt(OK.ident) == []
    assert signin_pause.adopt("") == []
