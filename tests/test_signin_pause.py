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

    def resume(self, db, root_id, *, watch_pane=True, only=None, unstarted=None):
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


@pytest.mark.variant("identity: a mismatching account cannot be adopted or cached as the agent owner")
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


@pytest.mark.variant("security: sign-in state stores only identity IDs in a private file")
def test_the_state_file_holds_ids_only_and_is_private(w, brindle_home):
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    path = signin_pause._path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "token" not in path.read_text().lower()


@pytest.mark.parametrize("beside", [
    pytest.param("a second launch", marks=pytest.mark.variant(
        "two launches writing together must retain both identity locks instead of losing the second write")),
    pytest.param("a sign-in adopting agents", marks=pytest.mark.variant(
        "sign-in adoption racing a launch must retain both adopted and launched identity locks")),
])
def test_a_write_beside_a_launch_is_not_lost_and_both_stay_locked_to_their_identity(w, monkeypatch, beside):
    import threading

    from brindle import identity_lock

    w.agent("c1")
    if beside == "a second launch":
        w.agent("c2")
        second = w.db.get_agent("c2")
        other, write = "c2", lambda: signin_pause.record_launch(second)
    else:
        other, write = "u1", lambda: signin_pause.adopt(OK.ident)
        signin_pause._save({"identity": {"u1": "launched before the sign-in"}, "unowned": ["u1"]})
    settled, contended = threading.Event(), threading.Event()   # finished, or held up by the first write

    class Watched:
        """The state lock, saying when a write has to wait for it."""

        def __init__(self):
            self.inner = threading.Lock()

        def __enter__(self):
            if not self.inner.acquire(blocking=False):
                contended.set()
                settled.set()
                self.inner.acquire()

        def __exit__(self, *exc):
            self.inner.release()

    def beside_it():
        try:
            write()
        finally:
            settled.set()

    real, started, reached = signin_pause._save, [], []

    def save(state):
        if not started:     # the launch has read the file: the other write goes as far as it can
            started.append(threading.Thread(target=beside_it))
            started[0].start()
            reached.append(settled.wait(5))
        real(state)

    monkeypatch.setattr(signin_pause, "_state_lock", Watched())
    monkeypatch.setattr(signin_pause, "_save", save)
    signin_pause.record_launch(w.db.get_agent("c1"))
    started[0].join(5)
    assert reached == [True]
    w.claude = PERSONAL     # another account signs in: neither conversation may go on under it
    assert [identity_lock.locked_by(a) for a in ("c1", other)] == [OK.ident, OK.ident]
    assert contended.is_set()


@pytest.mark.variant("a sweep saving its earlier snapshot must not erase a paused agent's concurrent launch identity")
def test_a_launch_during_a_sweep_keeps_its_identity(w, monkeypatch):
    w.agent("c1")
    w.agent("launching", status="paused")   # not one the sweep would record itself
    real = signin_pause.check

    def check(provider, cache=False):
        if not cache:       # the sweep's own look (a launch's asks the cache)
            signin_pause.record_launch(w.db.get_agent("launching"))
        return real(provider, cache)

    monkeypatch.setattr(signin_pause, "check", check)
    signin_pause.run(w.db)
    assert signin_pause._load()["identity"] == {"c1": OK.ident, "launching": OK.ident}


@pytest.mark.parametrize("during, kept, unowned", [
    pytest.param("launch", OK.ident, [], marks=pytest.mark.variant(
        "a stale sweep must not overwrite a concurrent launch's identity with the newly probed account")),
    pytest.param("adopt", OK.ident, [], marks=pytest.mark.variant(
        "a stale sweep must not overwrite the account chosen by a concurrent sign-in adoption")),
    pytest.param("adopted as it was", "old", [], marks=pytest.mark.variant(
        "adoption can clear unowned without changing identity; a stale sweep must still respect ownership")),
    pytest.param("moved, no brindle login", OK.ident, ["x"], marks=pytest.mark.variant(
        "a concurrent unsigned-in adoption leaves unowned intact but its newer identity must survive the sweep")),
])
def test_a_sweep_that_read_too_early_leaves_a_newer_identity_alone(w, monkeypatch, during, kept, unowned):
    from brindle import identity_lock

    w.agent("c1")
    w.agent("x")
    monkeypatch.setattr(signin_pause, "_brindle_signed_in", lambda: True)
    if during != "launch":
        signin_pause._save({"identity": {"x": "old"}, "unowned": ["x"]})
    writes = {"launch": lambda: signin_pause.record_launch(w.db.get_agent("x")),
              "adopt": lambda: signin_pause.adopt(OK.ident),
              "adopted as it was": lambda: signin_pause.adopt("old"),
              "moved, no brindle login": lambda: signin_pause.adopt(OK.ident, signed_in=False)}
    real = signin_pause.check

    def check(provider, cache=False):
        if not cache:       # the sweep has read the file; then the account changes under it
            writes[during]()
            w.claude = PERSONAL
        return real(provider, cache)

    monkeypatch.setattr(signin_pause, "check", check)
    signin_pause.run(w.db)
    state = signin_pause._load()
    assert state["identity"]["x"] == kept and state["unowned"] == unowned
    assert identity_lock.locked_by("x") == kept


@pytest.mark.variant("logout during a stale sweep must not make a concurrently launched owned conversation adoptable by another account")
def test_a_sweep_that_read_too_early_cannot_make_an_owned_agent_adoptable(w, monkeypatch):
    from brindle import identity_lock

    w.agent("c1")
    w.agent("x")
    login = [True]
    monkeypatch.setattr(signin_pause, "_brindle_signed_in", lambda: login[0])
    real = signin_pause.check

    def check(provider, cache=False):
        if not cache:       # the sweep has read the file; x launches owned, then brindle signs out
            signin_pause.record_launch(w.db.get_agent("x"))
            login[0] = False
        return real(provider, cache)

    monkeypatch.setattr(signin_pause, "check", check)
    signin_pause.run(w.db)      # it would record the same identity for x, but as nobody's
    assert "x" not in signin_pause._load()["unowned"]
    signin_pause.adopt(PERSONAL.ident)
    w.claude = PERSONAL
    assert identity_lock.locked_by("x") == OK.ident


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


def _an_agent_recorded_under_a_brindle_login_is_never_adopted(w, brindle_login, monkeypatch):
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


def _the_sweep_adopts_when_the_sign_in_check_failed(w, brindle_login, monkeypatch):
    from brindle import identity_lock

    w.claude = PERSONAL
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    brindle_login["in"], w.claude = True, UNKNOWN   # signed in, but its check couldn't tell who
    company_login.Login("acme", None, lambda line: None)._adopt()
    assert signin_pause._load()["unowned"] == ["c1"]
    signin_pause.run(w.db)                          # still undetermined: nothing adopted
    assert signin_pause._load()["unowned"] == ["c1"]
    w.claude = WRONG                                # a mismatch with the policy never adopts
    signin_pause.run(w.db)
    assert signin_pause.recorded_identity("c1") == PERSONAL.ident
    brindle_login["in"], w.claude = False, OK       # nor does a clear identity with no brindle login
    signin_pause.run(w.db)
    assert signin_pause._load()["unowned"] == ["c1"]
    brindle_login["in"] = True                      # the next sweep with a login adopts it
    signin_pause.run(w.db)
    assert signin_pause._load()["unowned"] == []
    assert signin_pause.recorded_identity("c1") == OK.ident
    assert identity_lock.locked_by("c1") is None
    w.claude = OTHER_ORG
    assert identity_lock.locked_by("c1") == OK.ident


def _an_unused_org_route_checks_the_personal_signin(w, brindle_login, monkeypatch):
    w.route = "bedrock"
    monkeypatch.setattr(company_login, "route_in_use", _REAL_ROUTE_IN_USE)
    monkeypatch.setattr(company_identity, "claude_identity", lambda org_id, claude, cache=True: (
        Identity("claude", "AWS federated role", determined=False) if claude is not None else w.claude))
    w.agent("c1")
    w.claude = PERSONAL
    assert signin_pause.check("claude")[0].ident == PERSONAL.ident
    signin_pause.run(w.db)
    assert w.messages == [] and w.status("c1") == "processing"
    w.claude = OUT
    signin_pause.run(w.db)
    assert w.status("c1") == "paused"


def _cloud_signin_resumes_only_after_the_fingerprint_changes(w, brindle_login, monkeypatch):
    brindle_login["in"] = True
    w.route = "bedrock"
    w.agent("c1")
    w.claude = OUT
    signin_pause.run(w.db)
    assert w.status("c1") == "paused"
    w.claude = AWS_OK
    signin_pause.run(w.db)
    assert w.status("c1") == "paused" and w.claude_probes == 1
    w.fp = "fp2"
    signin_pause.run(w.db)
    assert w.status("c1") == "processing" and w.resumed == ["c1"]


def _setup_without_a_brindle_login_keeps_agents_adoptable(w, brindle_login, monkeypatch):
    w.claude = PERSONAL
    w.agent("c1")
    signin_pause.record_launch(w.db.get_agent("c1"))
    assert signin_pause.adopt(OK.ident, signed_in=False) == ["c1"]
    assert signin_pause.recorded_identity("c1") == OK.ident
    assert signin_pause._load()["unowned"] == ["c1"]
    brindle_login["in"] = True
    assert signin_pause.adopt(OTHER_ORG.ident) == ["c1"]
    assert signin_pause.recorded_identity("c1") == OTHER_ORG.ident
    assert signin_pause._load()["unowned"] == []


@pytest.mark.parametrize("scenario", [
    _an_unused_org_route_checks_the_personal_signin,
    _cloud_signin_resumes_only_after_the_fingerprint_changes,
    pytest.param(_setup_without_a_brindle_login_keeps_agents_adoptable,
                 id="setup_without_a_brindle_login_keeps_agents_adoptable",
                 marks=pytest.mark.variant("identity: company setup without a Brindle login cannot lock otherwise adoptable agents")),
    _an_agent_recorded_under_a_brindle_login_is_never_adopted,
    pytest.param(_the_sweep_adopts_when_the_sign_in_check_failed,
                 id="the_sweep_adopts_when_the_sign_in_check_failed",
                 marks=pytest.mark.variant("failure path: a failed launch sign-in check must leave the agent adoptable on a later sweep")),
], ids=lambda f: f.__name__.strip("_"))
def test_unowned_agents(scenario, w, brindle_login, monkeypatch):
    scenario(w, brindle_login, monkeypatch)
