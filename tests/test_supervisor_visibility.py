import json
import os
import time
from types import SimpleNamespace

from brindle import autopilot, git, mcp_server, tmux, view, watch
from brindle.db import Agent, Workspace


def test_untracked_summary_collapses_noise_and_dirs():
    files = [f".venv/lib/f{i}.py" for i in range(500)] + [
        "pkg/a/b.py", "pkg/c.py", "notes.txt", "web/node_modules/x/y.js", "web/src/z.js"]
    text = git.summarize_untracked(files)
    assert "Untracked files (505)" in text
    assert ".venv/ (500 files)" in text
    assert "pkg/ (2 files)" in text
    assert "web/node_modules/ (1 file)" in text
    assert "  notes.txt" in text
    assert len(text.splitlines()) < 10


def test_external_wait_tool_validation_and_visibility(db, tmp_path, monkeypatch):
    ws = Workspace("ws", str(tmp_path), "work", "main", "main", None, str(tmp_path), None, "", time.time())
    db.add_workspace(ws)
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "",
                       None, time.time()))
    db.add_autopilot("boss")
    db.update_autopilot("boss", goal="Train model")
    db.set_milestones("boss", [("Training", "true", None)])
    db.record_check(db.milestones("boss")[0].id, False, "unfinished")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    for invalid in ({}, {"pid": 0}, {"until": float("inf")}):
        mcp_server.wait_on(**invalid)
        assert db.get_autopilot("boss").external_wait is None
    mcp_server.wait_on(pid=1234, note="GPU training")
    entry = view.autopilot_entry(db, ws.repo_root, root_id="boss")
    assert entry["milestones"][0]["status"] == "waiting"
    for text in (mcp_server.get_progress(), "\n".join(line.text for line in watch.render_autopilot(entry, 80))):
        assert "GPU training" in text and "1234" in text
    db.update_autopilot("boss", external_wait=None)
    assert autopilot.work_state(db, "boss") is None
    assert view.autopilot_entry(db, ws.repo_root, root_id="boss")["wait"] is None


def test_untracked_summary_caps_listing():
    text = git.summarize_untracked([f"d{i}/f" for i in range(100)])
    assert len(text.splitlines()) <= git.MAX_UNTRACKED_LISTED + 2
    assert "and 80 more" in text


def test_last_activity_claude_transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"},
                                                       {"type": "tool_use", "name": "WebFetch"}]}},
        {"type": "user", "message": {"content": "hi"}},
    ]
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n{broken")
    old = time.time() - 40
    os.utime(t, (old, old))
    out = mcp_server._last_activity(SimpleNamespace(transcript_path=str(t)))
    assert out.startswith(" last: WebFetch 4") and out.endswith("s ago")


def test_last_activity_native_and_missing(tmp_path):
    t = tmp_path / "n.jsonl"
    t.write_text(json.dumps({"type": "assistant", "tool_calls": [{"name": "bash"}]}) + "\n")
    assert " last: bash " in mcp_server._last_activity(SimpleNamespace(transcript_path=str(t)))
    assert mcp_server._last_activity(SimpleNamespace(transcript_path=None)) == ""
    assert mcp_server._last_activity(SimpleNamespace(transcript_path=str(tmp_path / "no"))) == ""


def test_list_agents_marks_stale_progress(db, tmp_path, monkeypatch):
    now = time.time()
    ws = Workspace("ws", str(tmp_path), "work", "main", "main", None, str(tmp_path), None, "", now)
    db.add_workspace(ws)
    monkeypatch.setattr(mcp_server, "_caller", lambda db: (None, ws))
    monkeypatch.setattr(mcp_server.time, "time", lambda: now)
    for provider in ("antigravity", "codex", "claude", "native", "shell"):
        t = tmp_path / f"{provider}.jsonl"
        t.write_text("{}\n")
        os.utime(t, (now - 300, now - 300))
        db.add_agent(Agent(provider, ws.id, "dev", provider, None, "assign", "processing", "",
                           None, now - 600, transcript_path=str(t)))
    monkeypatch.setattr("brindle.usage.find_codex_rollout", lambda *a: tmp_path / "codex.jsonl")
    assert mcp_server.list_agents().count("no progress for 5 min") == 5
    for a in db.list_agents(ws.id):
        os.utime(a.transcript_path, (now - 179, now - 179))
    assert "no progress" not in mcp_server.list_agents()
    for a in db.list_agents(ws.id):
        os.utime(a.transcript_path, (now - 300, now - 300))
        db.update_agent(a.id, status="paused")
    assert "no progress" not in mcp_server.list_agents()
    db.update_agent("shell", status="unknown", status_since=now - 600,
                    transcript_path=None, tmux_window="@fake")
    monkeypatch.setattr("brindle.tmux.window_activity", lambda *a, **k: now - 300)
    assert mcp_server.list_agents().count("no progress for 5 min") == 1
    monkeypatch.setattr("brindle.tmux.window_activity", lambda *a, **k: None)
    assert "no progress" not in mcp_server.list_agents()

    def timeout(*a, **k):
        raise tmux.TmuxTimeout("unresponsive server")
    monkeypatch.setattr(tmux, "window_activity", timeout)
    out = mcp_server.list_agents()
    assert len(out.splitlines()) == 5 and "shell dev/shell unknown" in out and "no progress" not in out
