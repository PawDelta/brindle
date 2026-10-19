import json
import time

import pytest

from brindle import agents, workspaces
from brindle.db import Agent
from brindle.native.loop import LoopConfig, NativeAgent
from brindle.native.runner import conversation_path
from brindle.web import chat
from brindle.web.server import Request, App


def request(db, method, path, params, query=None, body=b""):
    return Request(App(None, None), method, path, query or {}, params, {}, body, _db=db)


@pytest.fixture
def headless(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    monkeypatch.setattr(agents, "is_alive", lambda *a, **k: True)
    db.add_agent(Agent("w1", ws.id, "dev", "claude", None, "interactive", "idle",
                       "@1", None, time.time(), headless=1))
    return db.get_agent("w1")


def test_transcript_keeps_chat_text_and_resumes_from_cursor(db, headless, tmp_path):
    lines = [
        {"type": "user", "message": {"content": "fix the bug"}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "On it."},
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "isMeta": True, "message": {"content": "caveat"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n{\"type\": \"us")
    db.update_agent("w1", transcript_path=str(path))
    first = chat.transcript(db.get_agent("w1"))
    assert first["messages"] == [{"role": "user", "text": "fix the bug"},
                                 {"role": "assistant", "text": "On it."},
                                 {"role": "assistant", "text": "Done."}]
    with path.open("a") as f:
        f.write('er", "message": {"content": "thanks"}}\n')
    again = chat.transcript(db.get_agent("w1"), first["cursor"])
    assert again["messages"] == [{"role": "user", "text": "thanks"}]
    with path.open("a") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": "welcome"}}) + "\n")
    third = chat.transcript(db.get_agent("w1"), again["cursor"])
    assert third["messages"] == [{"role": "assistant", "text": "welcome"}]


def test_native_transcript_resets_when_context_folds(db, headless):
    db.update_agent("w1", provider="native")
    agent = db.get_agent("w1")
    offline = type("C", (), {"complete": staticmethod(lambda *a: (_ for _ in ()).throw(OSError()))})()
    loop = NativeAgent(offline, None, None, "sys", config=LoopConfig(context_tokens=50, keep_recent=1))
    loop.messages = [
        {"role": "user", "content": "start task"},
        *[{"role": "assistant", "content": f"step {i} " + "x" * 80} for i in range(5)],
    ]
    loop.save(conversation_path("w1"))
    before = chat.transcript(agent)
    assert not before.get("reset") and len(before["messages"]) == 6

    loop._fit_context()
    loop.messages.append({"role": "assistant", "content": "after fold"})
    loop.save(conversation_path("w1"))
    after = chat.transcript(agent, before["cursor"])
    assert after.get("reset") is True
    assert [m["text"] for m in after["messages"]][-2:] == ["step 4 " + "x" * 80, "after fold"]

    loop.messages.append({"role": "user", "content": "next turn"})
    loop.save(conversation_path("w1"))
    tail = chat.transcript(agent, after["cursor"])
    assert not tail.get("reset") and tail["messages"] == [{"role": "user", "text": "next turn"}]


def test_message_post_reaches_agent_inbox(db, headless):
    resp = chat.api_message(request(db, "POST", "/api/agents/w1/message", {"id": "w1"},
                                    body=json.dumps({"text": "status?"}).encode()))
    assert resp.status == 200 and json.loads(resp.body) == {"status": "delivered"}
    assert db.pending_count("w1") == 1
    resp = chat.api_message(request(db, "POST", "/api/agents/zz/message", {"id": "zz"},
                                    body=b'{"text": "x"}'))
    assert resp.status == 404

