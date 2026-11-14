"""A fresh supervisor keeps the person's earlier conversations visible."""

import json
import time
from pathlib import Path

from brindle import supervisor_state
from brindle.db import DB
from brindle.web import chat
from fast_repo import repo, repo_template  # noqa: F401
from test_daemon_api import TOKEN, _get
from test_efficiency_supervisor import restarting, session  # noqa: F401
from test_web_chat import request


def test_restarts_keep_ordered_history_after_reopening(db, restarting, tmp_path, monkeypatch):
    calls = restarting()
    [argv] = calls.detached
    old_path = Path(argv[-1])
    calls.helper(argv)
    calls.helper(argv)  # a duplicate helper must not duplicate the conversation
    saved = json.loads(db.mark("boss", "restart-history") or "[]")
    assert [item["session_ref"] for item in saved] == ["old-long-chat"]
    pending = json.loads(chat.api_transcript(request(
        db, "GET", "/api/agents/boss/transcript", {"id": "boss"})).body)
    assert [m["role"] for m in pending["messages"]] == ["user", "assistant"]

    def fresh(ref, text):
        path = tmp_path / f"{ref}.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
        db.update_agent("boss", session_ref=ref, transcript_path=str(path))
        db.set_status("boss", "idle")
        return path

    middle = fresh("middle-chat", "Keep the greeting short.")
    before = json.loads(chat.api_transcript(request(
        db, "GET", "/api/agents/boss/transcript", {"id": "boss"})).body)
    token = supervisor_state._claim(db, db.get_agent("boss"), time.time())
    assert supervisor_state.restart(db, "boss", token, middle)
    penultimate = fresh("penultimate-chat", "Now add a wave.")
    token = supervisor_state._claim(db, db.get_agent("boss"), time.time())
    assert supervisor_state.restart(db, "boss", token, penultimate)
    fresh("fresh-chat", "Then ship it.")
    assert [item["session_ref"] for item in supervisor_state.previous_sessions(db, "boss")] == [
        "old-long-chat", "middle-chat", "penultimate-chat"]
    # A new DB connection models reopening the app/daemon; no in-memory history.
    reopened = DB()
    try:
        response = chat.api_transcript(request(reopened, "GET", "/api/agents/boss/transcript",
                                              {"id": "boss"}, {"after": before["cursor"]}))
        data = json.loads(response.body)
    finally:
        reopened.conn.close()
    assert data["reset"]
    assert [m["text"] for m in data["messages"] if m["role"] != "restart"] == [
        "Ship on Monday.", "Noted, Monday it is.", "Keep the greeting short.", "Now add a wave.", "Then ship it."]
    assert [m["role"] for m in data["messages"]] == [
        "user", "assistant", "restart", "user", "restart", "user", "restart", "user"]
    assert all(m.get("history") for m in data["messages"][:-1])
    assert not data["messages"][-1].get("history")
    code, daemon = _get("/v1/agents/boss/transcript", {"Authorization": f"Bearer {TOKEN}"})
    assert code == 200 and daemon["messages"] == data["messages"]
    code, unchanged = _get("/v1/agents/boss/transcript", {"Authorization": f"Bearer {TOKEN}"},
                           f"after={daemon['cursor']}")
    assert code == 200 and unchanged["messages"] == [] and not unchanged["reset"]
    # A transcript removed outside brindle must not hide the remaining conversations.
    old_path.unlink()
    code, remaining = _get("/v1/agents/boss/transcript", {"Authorization": f"Bearer {TOKEN}"},
                           f"after={daemon['cursor']}")
    assert code == 200 and remaining["reset"]
    assert [m["text"] for m in remaining["messages"] if m["role"] != "restart"] == [
        "Keep the greeting short.", "Now add a wave.", "Then ship it."]
    from brindle import identity_lock

    monkeypatch.setattr(identity_lock, "locked_by", lambda _: "another identity")
    code, denied = _get("/v1/agents/boss/transcript", {"Authorization": f"Bearer {TOKEN}"})
    assert code == 403 and "messages" not in denied
