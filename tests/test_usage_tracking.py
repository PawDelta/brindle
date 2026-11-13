import json
import time
from pathlib import Path

import pytest

from brindle import usage, view
from brindle.db import Agent


def _line(msg_id, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0,
          model="claude-sonnet-5", type_="assistant"):
    return json.dumps({
        "type": type_,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }) + "\n"


def write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for line in lines:
            f.write(line)


@pytest.mark.variant("usage accounting: content blocks of one message arriving in separate reads cannot double count tokens")
def test_incremental_reread_dedupes_a_message_split_across_calls(db, tmp_path):
    """A message's later content-block line can land in its own incremental
    read, separate from the call that already counted the message's first
    line; it must still not be double-counted."""
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=10, output_tokens=20)])
    first = usage.transcript_usage(db, str(t))
    assert (first.input_tokens, first.output_tokens) == (10, 20)

    write(t, [_line("m1", input_tokens=10, output_tokens=20)])
    second = usage.transcript_usage(db, str(t))
    assert (second.input_tokens, second.output_tokens) == (10, 20)

    write(t, [_line("m2", input_tokens=1, output_tokens=2)])
    third = usage.transcript_usage(db, str(t))
    assert (third.input_tokens, third.output_tokens) == (11, 22)


def _codex_rollout(home: Path, cwd: Path, session_id: str, started: float) -> Path:
    """A rollout in Codex's current layout: ordinals and timestamps on every
    line, ``event_msg`` wrapping ``token_count``, and a long ``session_meta``."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started))
    d = home / "sessions" / time.strftime("%Y/%m/%d", time.localtime(started))
    path = d / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', time.localtime(started))}-{session_id}.jsonl"
    meta = {"timestamp": f"{stamp}.380Z", "ordinal": 0, "type": "session_meta", "payload": {
        "session_id": session_id, "id": session_id, "timestamp": f"{stamp}.231Z", "cwd": str(cwd),
        "runtime_workspace_roots": [str(cwd)], "originator": "codex-tui", "source": "cli",
        "base_instructions": {"text": "You are Codex. " * 2000}}}
    ctx = {"timestamp": f"{stamp}.400Z", "ordinal": 1, "type": "turn_context",
           "payload": {"turn_id": "t1", "cwd": str(cwd), "model": "gpt-6-luna"}}

    def count(ordinal, inp, cached, out):
        total = {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
                 "output_tokens": out, "reasoning_output_tokens": 0, "total_tokens": inp + out}
        return {"timestamp": f"{stamp}.500Z", "ordinal": ordinal, "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": total, "last_token_usage": total,
                                            "model_context_window": 258400}, "rate_limits": {}}}

    write(path, [json.dumps(e) + "\n" for e in (meta, ctx, count(2, 1000, 800, 50), count(3, 3000, 2500, 90))])
    return path


def test_agent_usage_reads_a_codex_rollout_in_the_current_layout(db, tmp_path, monkeypatch):
    from brindle.db import Workspace

    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    wt = tmp_path / "wt"
    wt.mkdir()
    now = time.time()
    db.add_workspace(Workspace("ws1", str(tmp_path), "wt", "worktree", "feat/x", "main", str(wt),
                               None, "s", now))
    _codex_rollout(home, wt, "01a11457-4d77-7a11-bfed-bdfcf413e415", now + 1)
    # found by its cwd, and by the session id Codex's notify reports
    for ref in (None, "01a11457-4d77-7a11-bfed-bdfcf413e415"):
        a = Agent("a1", "ws1", "developer-codex", "codex", None, "interactive", "idle", "", None, now,
                  session_ref=ref)
        u = usage.agent_usage(db, a)
        assert u is not None and u.total
        assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (500, 2500, 90)
        assert u.model == "gpt-6-luna"


def test_agent_entry_reads_once_then_only_stats(db, tmp_path, monkeypatch):
    from brindle import workspaces

    from conftest import sh

    origin = tmp_path / "origin.git"
    sh(f"git init -q --bare -b main {origin}", tmp_path)
    work = tmp_path / "proj"
    work.mkdir()
    sh("git init -q -b main", work)
    (work / "f.txt").write_text("x")
    sh("git add -A && git commit -qm init", work)
    ws = workspaces.create(db, str(work), "feature").workspace

    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=10, output_tokens=20)])
    a = Agent("a1", ws.id, "developer", "claude", None, "assign", "idle", "", "done", time.time(),
             transcript_path=str(t))
    db.add_agent(a)

    calls = []
    real_parse = usage._parse_new

    def counting_parse(path, offset, last_id):
        calls.append(path)
        return real_parse(path, offset, last_id)

    monkeypatch.setattr(usage, "_parse_new", counting_parse)

    entry1 = view.agent_entry(db, db.get_agent("a1"))
    entry2 = view.agent_entry(db, db.get_agent("a1"))
    assert entry1["tokens"] == entry2["tokens"] == "30 tok"
    assert len(calls) == 1  # the second call found no new bytes: stat only


# -- replaced/truncated files, model label, junk ------------------------------


def test_model_label_is_the_main_transcripts_and_skips_synthetic(db, tmp_path):
    t = tmp_path / "sess.jsonl"
    write(t, [_line("m1", input_tokens=1, model="claude-opus-4-7"),
              _line("m2", input_tokens=1, model="<synthetic>")])
    sub = tmp_path / "sess" / "subagents" / "agent-abc.jsonl"
    write(sub, [_line("m3", input_tokens=1, model="claude-haiku-4-5")])
    u = usage.transcript_usage(db, str(t))
    assert u.input_tokens == 3
    assert u.model == "claude-opus-4-7"


def _planner(input_tokens, output_tokens, cache_read=0, model="gemini-3-pro"):
    ev = {"type": "PLANNER_RESPONSE", "input_tokens": input_tokens,
          "output_tokens": output_tokens, "cache_read_tokens": cache_read}
    if model is not None:
        ev["model"] = model
    return json.dumps(ev) + "\n"


def test_antigravity_usage_is_cached_and_read_incrementally(db, tmp_path):
    t = tmp_path / "transcript.jsonl"
    write(t, [_planner(10, 20, 5)])
    agent = Agent("a1", "ws", "developer", "antigravity", None, "assign", "idle", "", None,
                  time.time(), transcript_path=str(t))

    u = usage.antigravity_agent_usage(db, agent)
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens) == (10, 20, 5)
    assert db.get_usage_cache(str(t))["size"] == t.stat().st_size

    write(t, [_planner(1, 2)])
    u = usage.antigravity_agent_usage(db, agent)
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.model) == (11, 22, 5, "gemini-3-pro")


def test_antigravity_transcript_without_model_uses_worker_launch_model(db, tmp_path, monkeypatch):
    from brindle.db import Workspace

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = time.time()
    db.add_workspace(Workspace("ws1", str(tmp_path), "wt", "worktree", "feat/x", "main",
                               str(tmp_path), None, "s", now))
    prof_dir = tmp_path / ".brindle" / "agents"
    prof_dir.mkdir(parents=True)
    (prof_dir / "agy-low.md").write_text(
        "---\nname: agy-low\ndescription: t\nprovider: antigravity\nmodel: gemini-3.1-pro-low\n---\nGo.\n"
    )
    own = tmp_path / ".gemini" / "antigravity-cli" / "brain" / "sess-1" / ".system_generated" / "logs" / "transcript.jsonl"
    write(own, [_planner(12, 34, 5, model=None)])

    worker = Agent("a1", "ws1", "agy-low", "antigravity", None, "assign", "idle", "", None,
                   now, session_ref="sess-1")
    u = usage.antigravity_agent_usage(db, worker)
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.model) == (12, 34, 5, "gemini-3.1-pro-low")

    t2 = tmp_path / "no-profile-model.jsonl"
    write(t2, [_planner(4, 6, model=None)])
    no_model_worker = Agent("a2", "ws1", "developer", "antigravity", None, "assign", "idle", "", None,
                            now, transcript_path=str(t2))
    assert usage.antigravity_agent_usage(db, no_model_worker).model == "unknown"

    missing_ref = Agent("missing", "ws1", "developer", "antigravity", None, "assign", "idle", "", None,
                        now, session_ref="missing-session")
    no_ref = Agent("unrecorded", "ws1", "developer", "antigravity", None, "assign", "idle", "", None, now)
    assert usage.antigravity_agent_usage(db, missing_ref) is None
    assert usage.antigravity_agent_usage(db, no_ref) is None

