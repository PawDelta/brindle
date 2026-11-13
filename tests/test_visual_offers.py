import threading
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from brindle import agents, config, mcp_server, visual_offers as offers, visuals
from brindle.daemon.app import build_app
from brindle.daemon.events import Feed
from brindle.db import DB, Agent, Workspace
from brindle.mcp_server import ToolError
from brindle.service.errors import ServiceError


def terminal_input(client, root):
    path = f"/v1/sessions/{root}/visual-input"
    observer = client.post(path, json={"begin": True}).json()["generation"]
    sequence = 0
    def send(text, **fields):
        nonlocal sequence
        sequence += 1
        submitted_at = offers.time.time()
        generation = client.post(path, json={"refresh": observer, "submitted_at": submitted_at}).json()["generation"]
        return client.post(path, json={"text": text, "generation": generation, "sequence": sequence,
                                      "submitted_at": submitted_at, **fields})
    return send


@pytest.fixture
def session(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    monkeypatch.delenv("BRINDLE_AGENT_ID", raising=False)
    monkeypatch.setattr(agents, "is_alive", lambda agent, panes=None: True)
    ws = Workspace("ws", str(tmp_path), "main", "main", "main", None, str(tmp_path), None, "tm", 0)
    db.add_workspace(ws)
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "%1", None, 0, headless=1)
    db.add_agent(boss)
    worker = Agent("worker", ws.id, "developer", "claude", boss.id, "assign", "processing", "%2", None, 0)
    db.add_agent(worker)
    config.set_user("visuals", "on")
    return boss, worker


@pytest.mark.parametrize("case,kind,counts,reason", [
    ("open", "layout", {}, "open_offer"),
    ("cooldown", "layout", {}, "cooldown"),
    ("daily", "layout", {}, "daily_cap"),
    ("duplicate", "layout", {}, "duplicate_question"),
    ("dismissals", "layout", {}, "dismissals"),
    ("off", "layout", {}, "setting_off"),
    ("worker", "layout", {}, "only_supervisors"),
    ("threshold", "options", {"options": 2, "attributes": 2}, "below_threshold"),
    pytest.param("on", "options", {"options": 3}, None, marks=pytest.mark.variant("three options qualify without attributes")),
    pytest.param("two", "options", {"options": 2, "attributes": 3}, None, marks=pytest.mark.variant("two options need three attributes")),
    pytest.param("small_table", "comparison", {"rows": 4, "columns": 2}, "below_threshold", marks=pytest.mark.variant("enough rows but too few columns")),
    pytest.param("table", "comparison", {"rows": 4, "columns": 3}, None, marks=pytest.mark.variant("comparison at both boundaries qualifies")),
    pytest.param("short_flow", "flow", {"steps": 4}, "below_threshold", marks=pytest.mark.variant("four steps without a branch cannot qualify")),
    pytest.param("flow", "flow", {"steps": 5}, None, marks=pytest.mark.variant("five steps qualify without a branch")),
    pytest.param("branch", "flow", {"branches": 1}, None, marks=pytest.mark.variant("a branch qualifies without five steps")),
    pytest.param("short_diff", "diff", {"lines": 12}, "below_threshold", marks=pytest.mark.variant("twelve-line diffs cannot qualify")),
    pytest.param("diff", "diff", {"lines": 13}, None, marks=pytest.mark.variant("a diff must exceed twelve lines")),
    pytest.param("unknown", "status", {}, "below_threshold", marks=pytest.mark.variant("unknown visual kinds fail closed")),
    ("invalid", "options", {"options": -1}, "invalid"),
    pytest.param("expired", "layout", {}, None, marks=pytest.mark.variant("an expired open offer must not prevent a new offer")),
    ("old_dismissals", "layout", {}, None),
])
def test_offer_limits(db, session, monkeypatch, case, kind, counts, reason):
    boss, worker = session
    clock = [1000000.0]
    monkeypatch.setattr(offers.time, "time", lambda: clock[0])
    question = "Which layout?"
    if case in ("open", "cooldown", "daily", "duplicate", "dismissals", "expired", "old_dismissals"):
        total = 4 if case == "daily" else 2 if case in ("dismissals", "old_dismissals") else 1
        for i in range(total):
            oid = offers.offer(db, boss, f"Which previous layout {i}?", "layout")["id"]
            if case not in ("open", "expired"):
                offers.resolve(db, boss.id, oid, case not in ("dismissals", "old_dismissals"))
            clock[0] += 901
        if case == "cooldown":
            clock[0] -= 900
        if case == "duplicate":
            question = " WHICH previous LAYOUT 0! "
        if case == "expired":
            clock[0] += offers.FOLD
        if case == "old_dismissals":
            clock[0] += offers.DAY
    if case == "off":
        config.set_user("visuals", "off")
    if reason == "invalid":
        with pytest.raises(ServiceError):
            offers.offer(db, boss, question, kind, **counts)
        return
    result = offers.offer(db, worker if case == "worker" else boss, question, kind, **counts)
    assert result.get("reason") == reason
    assert result["offered"] is (reason is None)
    if reason is None:
        assert offers.current(db, boss.id)["id"] == result["id"]


def test_accept_and_explicit_ask_allow_one_publish(db, session):
    boss, worker = session
    feed = Feed(db, repo_root=db.get_workspace(boss.workspace_id).repo_root)
    token = "t" * 43
    with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
        assert client.patch("/v1/user-settings", json={"visuals": "bad"}).status_code == 400
        assert client.patch("/v1/user-settings", json={"visuals": "on"}).json()["visuals"] == "on"
        oid = offers.offer(db, boss, "Which layout?", "layout")["id"]
        path = f"/v1/sessions/{boss.id}/visual-offer"
        assert client.get(path).json()["id"] == oid
        assert client.post(path, json={"offer": oid, "accept": True}).json()["accepted"]
        assert any(e["kind"] == "visual.offer_resolved" for e in feed.events())
        assert client.post(path, json={"offer": oid, "accept": True}).status_code == 409
        assert terminal_input(client, boss.id)("").status_code == 200
        messages = db.conn.execute("SELECT body FROM inbox WHERE agent_id=?", (boss.id,)).fetchall()
        assert len(messages) == 1 and "Which layout?" in messages[0]["body"]
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Unasked plan", "<p>no</p>", kind="plan")
        visuals.publish(db, worker, "Layout", "<p>layout</p>")
        with pytest.raises(ServiceError, match="accept an offer"):
            visuals.publish(db, boss, "Another", "<p>no</p>")
        assert client.patch("/v1/user-settings", json={"visuals": "off"}).json()["visuals"] == "off"
        assert any(e["kind"] == "visual.offers_setting" for e in feed.events())
        assert client.get("/v1/user-settings").json()["visuals"] == "off"
        agents.send_message(db, boss.id, "Please make a visual of this layout", person=True)
        assert any(e["kind"] == "visual.offer_resolved" for e in feed.events())
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Not a planning request", "<p>no</p>", kind="plan")
        # A failed publication must preserve the grant for a corrected page.
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Bad", "")
        visuals.publish(db, worker, "Requested", "<p>yes</p>")
        agents.send_message(db, boss.id, "Make a visual", sender_id=worker.id)
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Unrequested", "<p>no</p>")
        agents.handle_hook(db, boss.id, "prompt-submit", {"prompt": "Create a planning doc for this change"})
        visuals.publish(db, worker, "Plan", "<p>plan</p>", kind="plan")
        assert client.patch("/v1/user-settings", json={"visuals_enabled": "off"}).status_code == 200
        assert any(e["kind"] == "visual.offers_setting" for e in feed.events())


@pytest.mark.parametrize("text,scope", [
    ("Show me the release flow visually.", "visual"),
    pytest.param("can you render out what this new structure is again/", "visual",
                 marks=pytest.mark.variant("the owner's exact message from the 2026-11-11 incident")),
    pytest.param("please render possible options", "visual",
                 marks=pytest.mark.variant("render without out or a named artifact")),
    pytest.param('"can you render out what this new structure is again/"', None,
                 marks=pytest.mark.variant("quoted render request must not grant consent")),
    pytest.param("please don't render out what this new structure is again/", None,
                 marks=pytest.mark.variant("declined render request must not grant consent")),
    pytest.param("When I approve, please render out what this new structure is again/", None,
                 marks=pytest.mark.variant("deferred render request must not grant consent")),
    # Surrounding context or an open-ended object no longer waits for a confirmation click:
    # the person's own request authorizes one, as a plain one does.
    pytest.param("I am quoting a sentence.\nPlease render a mockup of the new session button", "visual",
                 marks=pytest.mark.variant("a leading sentence cannot hide the person's actual render request")),
    pytest.param("Create a planning document once I approve the approach.", "plan",
                 marks=pytest.mark.variant("a named planning-document request retains plan scope")),
    pytest.param("Please render a planning document", "plan",
                 marks=pytest.mark.variant("render must preserve the named plan scope")),
])
def test_complete_request_intent(db, session, text, scope):
    boss, _ = session
    config.set_user("visuals", "off")   # the offers setting never governs a request
    agents.send_message(db, boss.id, text, person=True)
    assert offers.current(db, boss.id) is None
    assert offers.request_intent(text) == scope
    if scope is None:
        with pytest.raises(ServiceError):
            visuals.publish(db, boss, "Unrequested", "<p>no</p>")
        return
    visuals.publish(db, boss, "Requested", "<p>yes</p>", kind=scope)
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Again", "<p>no</p>", kind=scope)


def test_codex_terminal_submission_folds_and_authorizes(db, session):
    boss, worker = session
    db.update_agent(boss.id, provider="codex")
    oid = offers.offer(db, db.get_agent(boss.id), "Which layout?", "layout")["id"]
    token = "t" * 43
    with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
        path = f"/v1/sessions/{boss.id}/visual-input"
        send = terminal_input(client, boss.id)
        assert send("2").status_code == 200
        assert offers.current(db, boss.id) is None
        with pytest.raises(ServiceError):
            offers.resolve(db, boss.id, oid, True)
        assert send("Make a visual").status_code == 200
        assert send("").status_code == 200
        assert send("y").status_code == 200
        assert send("Do not change permissions").status_code == 200
        visuals.publish(db, worker, "Codex request", "<p>yes</p>")
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "No second request", "<p>no</p>")
        assert send("Make a visual", expires_at=offers.time.time() - 1).status_code == 409
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Late recording", "<p>no</p>")
        assert send("Make a visual").status_code == 200
        assert send("Don't make a visual").status_code == 200
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Cancelled request", "<p>no</p>")


def test_superseded_terminal_observation_cannot_restore_consent(db, session):
    boss, worker = session
    token = "t" * 43
    with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
        path = f"/v1/sessions/{boss.id}/visual-input"
        # Cancellation reaches the actual dispatch before the older request.
        assert client.post(path, json={"begin": True, "expires_at": offers.time.time() - 1}).status_code == 409
        generation = client.post(path, json={"begin": True}).json()["generation"]
        newer = {"text": "Don't make a visual", "generation": generation, "sequence": 2, "submitted_at": offers.time.time()}
        older = {"text": "Make a visual", "generation": generation, "sequence": 1, "submitted_at": offers.time.time()}
        assert client.post(path, json=newer).status_code == 200
        assert client.post(path, json=older).status_code == 409
        assert client.post(path, json={"text": "Make a visual"}).status_code == 400
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Superseded", "<p>no</p>")
        # A replaced view's larger sequence cannot override the new view either.
        next_generation = client.post(path, json={"begin": True}).json()["generation"]
        assert client.post(path, json={"refresh": generation, "submitted_at": offers.time.time()}).status_code == 409
        assert client.post(path, json={**older, "generation": next_generation}).status_code == 200
        assert client.post(path, json={**newer, "sequence": 99}).status_code == 409
        visuals.publish(db, worker, "Current view", "<p>yes</p>")
        # Direct person messages invalidate snapshots while the same terminal stays open.
        send = terminal_input(client, boss.id)
        assert send("Make a visual").status_code == 200
        old_context = db.conn.execute("SELECT generation FROM visual_input_state WHERE root_id=?", (boss.id,)).fetchone()[0]
        agents.send_message(db, boss.id, "Use blue for the labels", person=True)
        path = f"/v1/sessions/{boss.id}/visual-input"
        # An observation already holding the old snapshot remains superseded.
        assert client.post(path, json={"text": "Make a visual", "generation": old_context, "sequence": 2,
                                       "submitted_at": offers.time.time()}).status_code == 409
        assert send("Don't make a visual").status_code == 200
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Cancelled after direct message", "<p>no</p>")
        agents.send_message(db, boss.id, "Use red for the labels", person=True)
        offers.offer(db, boss, "Which layout?", "layout")
        assert send("2").status_code == 200
        assert offers.current(db, boss.id) is None
        agents.send_message(db, boss.id, "Use green for the labels", person=True)
        assert send("Make a visual").status_code == 200
        visuals.publish(db, worker, "Fresh ask", "<p>yes</p>")
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "No second ask", "<p>no</p>")


def test_refresh_processed_after_direct_cancellation_cannot_relabel_old_ask(db, session, monkeypatch):
    boss, worker = session
    clock = [1000000.0]
    monkeypatch.setattr(offers.time, "time", lambda: clock[0])
    token = "t" * 43
    with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
        path = f"/v1/sessions/{boss.id}/visual-input"
        observer = client.post(path, json={"begin": True}).json()["generation"]
        submitted_at = clock[0]
        # Hold refresh before dispatch/processing, rather than holding a response
        # whose snapshot has already been taken.
        delayed = {"refresh": observer, "submitted_at": submitted_at, "expires_at": submitted_at + .25}
        clock[0] += .05
        agents.send_message(db, boss.id, "Don't make a visual", person=True)
        clock[0] += .05
        assert client.post(path, json=delayed).status_code == 409
        current = db.conn.execute("SELECT generation FROM visual_input_state WHERE root_id=?", (boss.id,)).fetchone()[0]
        old_ask = {"text": "Make a visual", "generation": current, "sequence": 1, "submitted_at": submitted_at}
        assert client.post(path, json=old_ask).status_code == 409
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Old ask after cancellation", "<p>no</p>")
        fresh = clock[0]
        generation = client.post(path, json={"refresh": observer, "submitted_at": fresh}).json()["generation"]
        assert client.post(path, json={**old_ask, "generation": generation, "submitted_at": fresh}).status_code == 200
        visuals.publish(db, worker, "Actually asked afterward", "<p>yes</p>")


def test_initial_person_prompt_is_observed_before_provider_launch(db, repo, monkeypatch):
    from brindle import workspaces
    from brindle.service import sessions

    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    ws = workspaces.adopt_root(db, str(repo))
    launched = []
    def launch(db, agent, ws, **kwargs):
        # Use the real gate at launch time; a hook after launch is too late.
        with db.tx() as c:
            offers.consume(c, agent.id)
        launched.append(agent.id)
    monkeypatch.setattr(agents, "_launch", launch)
    started = sessions.launch(db, ws, provider="codex", prompt="Make a visual")
    assert launched == [started.agent.id]


def test_legacy_unscoped_grants_are_revoked(db, session):
    from brindle.db import DB

    boss, _ = session
    agents.send_message(db, boss.id, "Make a visual", person=True)
    observer = offers.begin_input(db, boss)
    db.conn.execute("ALTER TABLE visual_grants DROP COLUMN scope")
    db.conn.execute("ALTER TABLE visual_input_state DROP COLUMN observer")
    db.conn.execute("ALTER TABLE visual_input_state DROP COLUMN invalidated_at")
    reopened = DB(db.path)
    try:
        assert offers.refresh_input(reopened, boss, observer, offers.time.time()) == observer
        with pytest.raises(ServiceError):
            with reopened.tx() as c:
                offers.consume(c, boss.id)
    finally:
        reopened.conn.close()


def app_vouches(boss, text, expires_at, submitted_at=None):
    token = "t" * 43
    with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
        return client.post(f"/v1/sessions/{boss.id}/visual-input", json={
            "typed_words": text, "expires_at": expires_at, "submitted_at": submitted_at or offers.time.time()})


def typed_into_cli(db, boss, text, vouched=True):
    """A hook CLI in the app: the app vouches for the keys before Enter, then the hook reports the prompt."""
    if vouched:
        assert app_vouches(boss, text, offers.time.time() + 60).status_code == 200
    agents.handle_hook(db, boss.id, "prompt-submit", {"prompt": text})


@pytest.mark.parametrize("typed,asked,terminal", [
    pytest.param("then render first", "render first", False,
                 marks=pytest.mark.variant("the person's own quoted words authorize one visual without an intent grammar match")),
    pytest.param("please send me updated renders", "please send me updated renders", False,
                 marks=pytest.mark.variant("reported refusal")),
    pytest.param("can i see the new session button rendering?", "can i see the new session button rendering?", False,
                 marks=pytest.mark.variant("reported refusal")),
    pytest.param("render the options for pending milestones", "render the options for pending milestones", False,
                 marks=pytest.mark.variant("reported refusal")),
    pytest.param("ok great, then render it", "then render it", False, marks=pytest.mark.variant("reported refusal, inside a longer message")),
    pytest.param("no, render first", "  RENDER\n first ", False, marks=pytest.mark.variant("case and whitespace; no is not a negation of the quote")),
    pytest.param("then render it", "then render it", True,
                 marks=pytest.mark.variant("typed app-terminal input must provide valid asked-word provenance")),
])
def test_asked_quote_accepts_what_the_person_typed(db, session, typed, asked, terminal):
    boss, _ = session
    if terminal:
        token = "t" * 43
        with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
            assert terminal_input(client, boss.id)(typed, typed=True).status_code == 200
    else:
        typed_into_cli(db, boss, typed)
    assert visuals.publish(db, boss, "Asked", "<p>yes</p>", asked=asked)["rev"] == 1


@pytest.mark.variant("real incident: a vouch arriving after its own prompt's hook, logged without the person's words")
def test_typed_words_reach_the_engine(db, session, monkeypatch):
    """A vouch landing after its own prompt's hook is refused, and the debug log says why, not what."""
    monkeypatch.setattr(offers.debuglog, "enabled", lambda: True)
    submitted_at = offers.time.time()
    agents.handle_hook(db, session[0].id, "prompt-submit", {"prompt": "show me again"})
    assert app_vouches(session[0], "show me again", offers.time.time() + 60, submitted_at).status_code == 409
    log = offers.debuglog.log_path().read_text()
    assert "typed words refused: this submission predates" in log and "show me again" not in log


def test_mid_turn_message_backs_a_visual(db, session, tmp_path):
    """Claude Code takes a message typed while it works into the running turn: no prompt hook runs,
    only its transcript records it. Its words back ``asked`` once; an agent's note with the same words doesn't."""
    boss, _ = session
    ask = "PLEASE RENDER FOR ME BEFORE YOU IMPLEMENT"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":{"role":"user","content":"plan the reader"}}\n')
    hook = {"transcript_path": str(transcript), "tool_name": "Read", "tool_input": {"file_path": str(transcript)}}
    agents.handle_hook(db, boss.id, "prompt-submit", {**hook, "prompt": "plan the reader"})
    agents.handle_hook(db, boss.id, "pre-tool", hook)
    absorbed = []

    def typed_mid_turn(text, origin="human", mode="prompt"):
        assert app_vouches(boss, text, offers.time.time() + 60).status_code == 200   # keyed in, before Enter
        at = datetime.fromtimestamp(offers.time.time(), timezone.utc).isoformat(timespec="milliseconds")
        absorbed.append({"type": "attachment", "isSidechain": False, "attachment": {
            "type": "queued_command", "prompt": text, "commandMode": mode, "origin": {"kind": origin},
            "timestamp": at.replace("+00:00", "Z")}})

    def turn_takes_them_in():
        with transcript.open("a") as f:
            f.writelines(offers.json.dumps(line) + "\n" for line in absorbed)
        absorbed.clear()
        agents.handle_hook(db, boss.id, "tool-done", hook)
        agents.handle_hook(db, boss.id, "pre-tool", {**hook, "tool_name": "mcp__brindle__publish_visual"})

    typed_mid_turn(ask, "task-notification", "task-notification")
    turn_takes_them_in()
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Not yet", "<p>no</p>", asked=ask)
    typed_mid_turn("never mind")   # one hook takes in both: the cancel ends only what came before it
    typed_mid_turn(ask)
    turn_takes_them_in()
    assert visuals.publish(db, boss, "Directions", "<p>yes</p>", asked=ask)["rev"] == 1
    agents.handle_hook(db, boss.id, "stop", hook)   # read again: the same message isn't counted twice
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Twice", "<p>no</p>", asked=ask)


def _report_came_late(db, boss, worker):
    assert app_vouches(boss, "then render first", offers.time.time() - 1).status_code == 409
    typed_into_cli(db, boss, "then render first", vouched=False)


def _report_lands_after_a_newer_cancel(db, boss, worker):
    submitted_at = offers.time.time()
    agents.send_message(db, boss.id, "Don't make a visual", person=True)
    assert app_vouches(boss, "then render first", offers.time.time() + 60, submitted_at).status_code == 409
    typed_into_cli(db, boss, "then render first", vouched=False)


def _report_with_no_prompt_yet(db, boss, worker):
    assert app_vouches(boss, "render first", offers.time.time() + 60).status_code == 200


def _direct_message_cannot_stand_in_for_the_prompt(db, boss, worker):
    assert app_vouches(boss, "then render first", offers.time.time() + 60).status_code == 200
    agents.send_message(db, boss.id, "then render first", person=True)


def _terminal(**fields):
    def send(db, boss, worker):
        token = "t" * 43
        with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
            terminal_input(client, boss.id)("then render first", **fields)
    return send


def _expired(db, boss, worker):
    typed_into_cli(db, boss, "then render first")
    db.conn.execute("UPDATE visual_person_words SET created_at=created_at-?", (offers.FOLD + 1,))


def _other_session(db, boss, worker):
    db.add_agent(Agent("boss2", boss.workspace_id, "supervisor", "claude", None, "interactive", "idle", "%3", None, 0, headless=1))
    typed_into_cli(db, db.get_agent("boss2"), "render first")


def _says(*texts):
    return lambda db, boss, worker: [typed_into_cli(db, boss, text) for text in texts]


@pytest.mark.parametrize("setup,asked", [
    pytest.param(_says("fix the login bug"), "render first", marks=pytest.mark.variant("an unrelated user message cannot authorize invented asked words")),
    pytest.param(lambda db, boss, worker: agents.send_message(db, boss.id, "render first", sender_id=worker.id), "render first",
                 marks=pytest.mark.variant("worker messages cannot supply the person's asked words")),
    # Use a lead-in outside request_intent's grammar so these exercise asked-word
    # provenance without a separate automatic grant masking a refused quote.
    # A hook prompt nobody vouched for: a paste, a failed report, a terminal outside the app.
    pytest.param(lambda db, boss, worker: typed_into_cli(db, boss, "then render first", vouched=False), "render first",
                 marks=pytest.mark.variant("an unvouched CLI prompt cannot prove the person typed the quote")),
    (_report_came_late, "render first"),
    pytest.param(_report_lands_after_a_newer_cancel, "render first", marks=pytest.mark.variant("a late app report cannot revive consent after a newer cancellation")),
    pytest.param(_report_with_no_prompt_yet, "render first", marks=pytest.mark.variant("an app report cannot grant consent before its prompt arrives")),
    pytest.param(_direct_message_cannot_stand_in_for_the_prompt, "render first", marks=pytest.mark.variant("a direct message cannot stand in for the correlated terminal prompt")),
    pytest.param(_terminal(typed=False), "render first", marks=pytest.mark.variant("pasted terminal content cannot count as the person's typed request")),
    pytest.param(_terminal(), "render first", marks=pytest.mark.variant("raw terminal keystrokes without a correlated prompt cannot authorize publication")),
    pytest.param(lambda db, boss, worker: agents.send_message(db, boss.id, "then render first", person=True), "render first",
                 marks=pytest.mark.variant("a direct send marked person cannot replace app-vouched typed provenance")),
    pytest.param(_says("surrender first please"), "render first", marks=pytest.mark.variant("asked words cannot match inside another word")),
    pytest.param(_says("then render first"), "render", marks=pytest.mark.variant("a one-word quote cannot authorize publication")),
    pytest.param(_says("please don't render it"), "render it", marks=pytest.mark.variant("a quote from a refusal cannot authorize publication")),
    pytest.param(_says("render first", "Don't make a visual"), "render first", marks=pytest.mark.variant("a later refusal must revoke the earlier request")),
    pytest.param(_expired, "render first", marks=pytest.mark.variant("asked words older than the consent window cannot authorize publication")),
    pytest.param(_other_session, "render first", marks=pytest.mark.variant("asked words belong to one supervisor session and cannot authorize another")),
])
def test_asked_quote_refuses_words_that_are_not_the_persons(db, session, setup, asked):
    boss, worker = session
    setup(db, boss, worker)
    with pytest.raises(ServiceError) as refused:
        visuals.publish(db, boss, "Not asked", "<p>no</p>", asked=asked)
    assert refused.value.code == "visual_request_required"


@pytest.mark.parametrize("typed,asked,accepted", [
    pytest.param("should you not open this in app visuals?", "open this in app visuals", True,
                 marks=pytest.mark.variant("real incident: a request phrased as a question")),
    pytest.param("you should not open this in app visuals", "open this in app visuals", False,
                 marks=pytest.mark.variant("bypass attempt: a plain negation must stay refused after the question exemption")),
    pytest.param("would you stop showing me that?", "showing me that", False,
                 marks=pytest.mark.variant("review: 'stop' after an auxiliary + you is still a refusal")),
    pytest.param("could you never show me that again?", "show me that again", False,
                 marks=pytest.mark.variant("review: 'never' after an auxiliary + you is still a refusal")),
    pytest.param("don't you ever open this in app visuals", "open this in app visuals", False,
                 marks=pytest.mark.variant("review: imperative don't you stays a prohibition")),
])
def test_question_form_is_not_a_negation(db, session, typed, asked, accepted):
    boss, _ = session
    typed_into_cli(db, boss, typed)
    if accepted:
        assert visuals.publish(db, boss, "Asked", "<p>yes</p>", asked=asked)["rev"] == 1
    else:
        with pytest.raises(ServiceError):
            visuals.publish(db, boss, "Not asked", "<p>no</p>", asked=asked)


@pytest.mark.variant("a bypass attempt: a worker or a plan can't spend the person's quote")
def test_asked_quote_is_not_for_workers_or_plans(db, session):
    boss, worker = session
    typed_into_cli(db, boss, "then render first")
    with pytest.raises(ServiceError):
        visuals.publish(db, worker, "From a worker", "<p>no</p>", asked="render first")
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "A plan", "<p>no</p>", kind="plan", asked="render first")
    visuals.publish(db, boss, "Still unspent", "<p>yes</p>", asked="render first")


@pytest.mark.variant("a typed message's automatic grant and asked quote must share one publication allowance")
def test_asked_quote_allows_one_publication_per_message(db, session):
    boss, _ = session
    typed_into_cli(db, boss, "then render first")
    typed_into_cli(db, boss, "then render first")
    for title in ("One", "Two"):   # each message is its own ask
        visuals.publish(db, boss, title, "<p>yes</p>", asked="render first")
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Three", "<p>no</p>", asked="render first")
    # A message the wording test granted spends its grant or its quote, never both.
    typed_into_cli(db, boss, "Make a visual")
    visuals.publish(db, boss, "Granted", "<p>yes</p>")
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Again", "<p>no</p>", asked="Make a visual")
    typed_into_cli(db, boss, "Make a visual")
    visuals.publish(db, boss, "Quoted", "<p>yes</p>", asked="Make a visual")
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Again", "<p>no</p>")
    # Between the app's report and its prompt's hook nothing can be spent.
    assert app_vouches(boss, "Make a visual", offers.time.time() + 60).status_code == 200
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Too early", "<p>no</p>", asked="Make a visual")
    typed_into_cli(db, boss, "Make a visual", vouched=False)
    visuals.publish(db, boss, "Then once", "<p>yes</p>", asked="Make a visual")
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Not twice", "<p>no</p>")


def test_asked_quote_words_are_purged_without_more_input(db, session, monkeypatch):
    from types import SimpleNamespace
    import asyncio

    from brindle.daemon import lifecycle, session_review

    boss, _ = session
    typed_into_cli(db, boss, "render first")
    db.conn.execute("UPDATE visual_person_words SET created_at=created_at-?", (offers.FOLD + 1,))
    monkeypatch.setattr(session_review.ReadinessWatch, "sweep_safely", lambda self: None)
    server = SimpleNamespace(should_exit=False)

    async def one_tick(seconds):
        server.should_exit = True

    asyncio.run(lifecycle._watch(server, lifecycle.Activity(build_app("t" * 43, port=8123)), False,
                                 wait=one_tick, clock=offers.time.time))
    assert db.conn.execute("SELECT COUNT(*) FROM visual_person_words").fetchone()[0] == 0


def test_answered_visual_allows_one_revision(db, session, monkeypatch):
    boss, worker = session
    # A worker's visual, on the person's request: not the supervisor's to keep updating.
    typed_into_cli(db, boss, "Please render a mockup of the new session button")
    choices = [{"id": "more", "label": "None, keep exploring"}, {"id": "a", "label": "A"}]
    v = visuals.publish(db, worker, "Options", "<p>1</p>", "Which?", choices)
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Unanswered", "<p>no</p>", visual_id=v["id"])
    visuals.answer(db, v["id"], 1, "more", "you didnt render anything")
    with pytest.raises(ServiceError):
        visuals.publish(db, worker, "A worker's revision", "<p>no</p>", visual_id=v["id"])
    assert visuals.publish(db, boss, "Options", "<p>2</p>", "Which?", choices, visual_id=v["id"])["rev"] == 2
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Second revision", "<p>no</p>", visual_id=v["id"])
    visuals.answer(db, v["id"], 2, "a")
    now = offers.time.time()
    monkeypatch.setattr(offers.time, "time", lambda: now + offers.FOLD + 1)
    with pytest.raises(ServiceError):
        visuals.publish(db, boss, "Too late", "<p>no</p>", visual_id=v["id"])


def _delivered_prompt(db, boss, worker):
    agents.send_message(db, boss.id, "render first", sender_id=worker.id)
    db.conn.execute("UPDATE inbox SET delivered_at=? WHERE agent_id=?", (offers.time.time(), boss.id))
    agents.handle_hook(db, boss.id, "prompt-submit", {"prompt": "render first"})


def _publishers_race(db, boss, worker):
    outcomes = []

    def publish():
        own = DB()
        try:
            outcomes.append(visuals.publish(own, boss, "Race", "<p>r</p>", asked="render first")["id"])
        except ServiceError as refused:
            outcomes.append(refused.code)
        finally:
            own.conn.close()

    threads = [threading.Thread(target=publish) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(o.startswith("v_") for o in outcomes) == 1


def _worker_calls_the_tool(db, boss, worker):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("BRINDLE_AGENT_ID", worker.id)
        with pytest.raises(ToolError):
            mcp_server.publish_visual("From a worker", "<p>no</p>", asked="render first")


def _restart(db, boss, worker):
    db.conn.close()
    offers.purge_words(DB())


def _stress(why, *row):
    return pytest.param(*row, marks=pytest.mark.variant(why))


FIRST = "render first"
# Exercise the quote path independently of request_intent's automatic grants.
QUOTE_ONLY = "then render first"
LONG_NOTES = "\n".join(f"line {n} of the notes about the migration" for n in range(110))


@pytest.mark.parametrize("said,asked,published", [
    # Words the person never asked with.
    _stress("punctuation is not words", ["hmm . . . ok go"], ". .", False),
    _stress("empty quote", [QUOTE_ONLY], "", False),
    _stress("one ideograph is one word", ["画"], "画", False),
    _stress("a piece cut from a run of ideographs", ["把这个流程画出来"], "流程", False),
    _stress("homoglyph", [QUOTE_ONLY], "rеnder first", False),
    _stress("a worker's message delivered as a prompt", [_delivered_prompt], FIRST, False),
    # Words that take the ask back.
    _stress("the quote carries the don't", ["please don't render it"], "don't render it", False),
    _stress("don't ever", ["don't ever render it"], "render it", False),
    _stress("did not ask", ["i didn't ask you to render it"], "render it", False),
    _stress("instead of", ["write it up instead of render it"], "render it", False),
    _stress("mentioned in backticks", ["the `render first` flag is broken"], FIRST, False),
    _stress("deep inside a mention", ['What does "please render first" mean?'], FIRST, False),
    _stress("the quote brings its own marks", ['What does "render first" mean?'], '"render first"', False),
    _stress("mentioned in single quotes", ["what does 'please render first' mean"], FIRST, False),
    _stress("an unclosed mention", ['he said "please render first'], FIRST, False),
    _stress("an unclosed single quote", ["He said 'render first"], FIRST, False),
    _stress("an unclosed curly quote", ["He said ‘render first"], FIRST, False),
    _stress("an apostrophe inside an unclosed quote", ["He said 'let's render first"], FIRST, False),
    _stress("the marks and one word of the person's own", ['What does "render first" mean?'], '"render first" mean', False),
    _stress("begins outside, ends inside", ['What does "render first" mean?'], 'does "render first', False),
    _stress("a report with a request verb as a noun", ['The render log contains "render first"'], 'The render log contains "render first"', False),
    _stress("a quote opening on a digit", ["He said '2d render first'"], FIRST, False),
    _stress("a quote opening on an elision", ["He said 'tis time to render first'"], FIRST, False),
    _stress("a long message keeps its negation", [LONG_NOTES + "\nThen don't render the options."], "render the options", False),
    # Cancelled, in the person's own words, for a quote and for a grant.
    # Bare cancellation, negated requests, explicit artifacts, trailing clauses,
    # Unicode separators and an unpunctuated explanation take distinct paths.
    *[_stress("cancel then quote", [QUOTE_ONLY, cancel], FIRST, False) for cancel in (
        "actually never mind", "don’t make a visual", "no visual please",
        "thanks for that.\nNever mind the visual.", "Never mind — I found it", "never mind I found it")],
    _stress("cancel then grant", ["Make a visual", "actually never mind"], None, False),
    _stress("cancel with a reason, grant", ["Make a visual", "Never mind, I found the answer."], None, False),
    _stress("cancel with unicode punctuation, grant", ["Make a visual", "Never mind…"], None, False),
    _stress("ask and cancel in one message, grant", ["Make a visual. Don't make a visual."], None, False),
    # Capability, races, restarts.
    _stress("capability off", [lambda db, boss, worker: config.set_user("visuals_enabled", "off"), QUOTE_ONLY], FIRST, False),
    _stress("one quote, eight publishers", [QUOTE_ONLY, _publishers_race], FIRST, False),
    _stress("a worker's refused call leaves the quote", [QUOTE_ONLY, _worker_calls_the_tool], FIRST, True),
    _stress("the offers setting never governs asking", [lambda db, boss, worker: config.set_user("visuals", "off"), QUOTE_ONLY], FIRST, True),
    _stress("a restart keeps the words", [QUOTE_ONLY, _restart], FIRST, True),
    # Real asks these guards must not catch.
    _stress("why not", ["Why not render it?"], "render it", True),
    _stress("can't you", ["can't you just render it?"], "render it", True),
    _stress("negation in an earlier clause", ["I don't get it, render it"], "render it", True),
    _stress("ideographs have no spaces", ["把这个流程画出来"], "把这个流程画出来", True),
    _stress("never mind something else", ["never mind the typo, render it"], "render it", True),
    _stress("never mind, then the ask", ["never mind, render it"], "render it", True),
    _stress("never mind, then a polite ask", ["Never mind — can you show me the flow"], "show me the flow", True),
    _stress("a whole ask holding a quoted name", ['render the "new session" button'], 'render the "new session" button', True),
    _stress("another verb", ["Please sketch the `SessionContext` layout"], "Please sketch the `SessionContext` layout", True),
    _stress("a subordinate clause in the ask", ['Please render what they see on the "new session" screen'], 'Please render what they see on the "new session" screen', True),
    _stress("polite", ['Would you mind rendering the "new session" button?'], 'Would you mind rendering the "new session" button?', True),
    _stress("a decade is not a quotation", ["the '90s layout, render it"], "render it", True),
    _stress("apostrophes are not quotation marks", ["let's see what's wrong, render it"], "render it", True),
    _stress("a long message ending in the ask", [LONG_NOTES + "\nThen render the options."], "render the options", True),
    _stress("stop something else", ["stop the server and render the flow"], "render the flow", True),
    _stress("cancel, then ask again", [QUOTE_ONLY, "never mind", QUOTE_ONLY], FIRST, True),
])
def test_visual_request_stress(db, session, monkeypatch, said, asked, published):
    boss, worker = session
    for step in said:
        if callable(step):
            step(db, boss, worker)
        else:
            typed_into_cli(db, boss, step)
    monkeypatch.setenv("BRINDLE_AGENT_ID", boss.id)
    if published:
        assert mcp_server.publish_visual("Asked", "<p>yes</p>", asked=asked).startswith("Published")
    else:
        with pytest.raises(ToolError):
            mcp_server.publish_visual("Not asked", "<p>no</p>", asked=asked)


@pytest.mark.variant("answering a choice must close its old offer without treating refusals or mentions as consent")
def test_answer_folds_offer_and_setting_never_grants(db, session):
    boss, _ = session
    oid = offers.offer(db, boss, "Which layout?", "layout")["id"]
    agents.send_message(db, boss.id, "Use the second layout", person=True)
    assert offers.current(db, boss.id) is None
    with pytest.raises(ServiceError):
        offers.resolve(db, boss.id, oid, True)
    config.set_user("visuals", "off")
    assert offers.current(db, boss.id) is None
    for text in ("Don't make a visual", "What does make a visual mean?", "Make sure no visual is created"):
        agents.send_message(db, boss.id, text, person=True)
        with pytest.raises(ServiceError):
            visuals.publish(db, boss, "No request", "<p>no</p>")
