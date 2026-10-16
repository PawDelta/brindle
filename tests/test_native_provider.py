"""The native provider in brindle's pipeline: spawn, the pane runner, brindle's
tools in-process, reminders to report, messages, resume, usage."""

import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, usage, workspaces
from brindle.db import Agent
from brindle.native import runner
from brindle.native.runner import run_native
from brindle.profiles import load_profile
from test_native_loop import FakeEndpoint, anthropic_reply, openai_reply


@pytest.fixture
def fake():
    ep = FakeEndpoint()
    yield ep
    ep.close()


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


@pytest.fixture
def local_profile(repo, fake):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "local.md").write_text(
        f"---\nname: local\ndescription: a local model\nprovider: native\napi: openai\n"
        f"base_url: {fake.base_url}/v1\nmodel: tiny\ncontext_tokens: 8k\n"
        "permission_mode: acceptEdits\n"
        "allowed_tools: Bash(git add:*), Bash(git commit:*), Bash(git status:*), Bash(echo:*)\n"
        "env.OLLAMA_HOST: http://x\n---\nYou are a local worker.\n"
    )
    return "local"


def native_agent(db, ws, *, mode="assign", status="idle", agent_id="n1", parent=None, profile="local"):
    a = Agent(agent_id, ws.id, profile, "native", parent, mode, status, "@0", None, time.time(), headless=1)
    db.add_agent(a)
    return a


# -- profile and spawn ------------------------------------------------------------


def test_endpoint_needs_a_base_url_a_model_and_its_key(monkeypatch):
    from dataclasses import replace

    p = load_profile("developer")
    with pytest.raises(ValueError, match="no base_url"):
        runner.endpoint_for(replace(p, provider="native"))
    p = replace(p, provider="native", base_url="http://h/v1", model="m", api_key_env="MY_KEY")
    with pytest.raises(ValueError, match="MY_KEY"):
        runner.endpoint_for(p)
    monkeypatch.setenv("MY_KEY", "k")
    assert runner.endpoint_for(p).api_key == "k"
    with pytest.raises(ValueError, match="api must be"):
        runner.endpoint_for(replace(p, api="grpc"))


def test_spawn_runs_native_workers_headless_through_the_runner(db, ws, local_profile, monkeypatch):
    opened = {}

    def fake_open(db_, agent, ws_, name, argv, watch_pane):
        opened["argv"] = argv
        return "@9"

    monkeypatch.setattr(agents, "_open_window", fake_open)
    a = agents.spawn(db, ws, local_profile, prompt="add a README", mode="assign", done_when="README.md exists")
    assert a.provider == "native" and a.headless == 1
    assert opened["argv"][-2:] == ["_native", a.id]
    assert db.get_agent(a.id).status == "processing"
    queued = db.pop_pending(a.id).body
    assert queued.startswith("add a README") and "Finish line: README.md exists" in queued
    assert "report_result" in queued and "/goal" not in queued


@pytest.mark.parametrize("models, expected", [(["tiny"], "local"), ([], "developer-light")])
def test_spawn_falls_back_to_hosted_when_the_local_model_isnt_served(db, ws, repo, local_profile, fake,
                                                                     monkeypatch, models, expected):
    f = repo / ".brindle" / "agents" / "local.md"
    f.write_text(f.read_text().replace("context_tokens:", "fallback: developer-light\ncontext_tokens:"))
    fake.models = models
    monkeypatch.setattr(agents, "_open_window", lambda *a: "@9")
    assert agents.spawn(db, ws, local_profile, prompt="add a README", mode="assign").profile == expected


# -- the runner ------------------------------------------------------------------------

def test_runner_does_the_task_and_reports(db, ws, local_profile, fake, monkeypatch):
    monkeypatch.setenv("BRINDLE_AGENT_ID", "n1")
    native_agent(db, ws)
    db.enqueue("n1", "make hello.txt say hi, commit, report", None)
    fake.replies = [
        openai_reply(calls=[("c1", "Write", {"path": "hello.txt", "content": "hi\n"})]),
        openai_reply(calls=[("c2", "Bash", {"command": "git add hello.txt"})]),
        openai_reply(calls=[("c3", "Bash", {"command": "git commit -qm 'add hello'"})]),
        openai_reply(calls=[("c4", "report_result", {"result": "Added hello.txt and committed."})]),
        openai_reply("All done."),
    ]
    assert run_native(db, "n1", exit_when_idle=True) == 0

    a = db.get_agent("n1")
    assert a.result == "Added hello.txt and committed."
    assert a.status == "idle"
    assert (Path(ws.path) / "hello.txt").read_text() == "hi\n"
    assert "add hello" in sh("git log -1 --format=%s", Path(ws.path))
    # The system prompt is the profile plus the harness note; the tools include brindle's.
    first = fake.requests[0]
    assert first["messages"][0]["content"].startswith("You are a local worker.")
    assert "brindle delivered this message" in first["messages"][0]["content"]
    names = {t["function"]["name"] for t in first["tools"]}
    assert {"Read", "Edit", "Bash", "report_result", "send_message", "workspace_diff"} <= names
    assert "submit_review" not in names
    # The tool's reply reached the model.
    assert fake.requests[4]["messages"][-1]["content"] == "result recorded"
    # No reminder was queued: it reported.
    assert db.pending_count("n1") == 0
    # Its transcript and saved conversation are recorded, and usage is readable.
    assert a.transcript_path and Path(a.transcript_path).is_file()
    assert a.session_ref and Path(a.session_ref).is_file()
    u = usage.agent_usage(db, a)
    assert u.input_tokens == 50 and u.output_tokens == 25 and u.model == "fake-1"


def test_a_refused_request_is_retried_on_the_local_model(db, ws, repo, local_profile, fake):
    hosted = FakeEndpoint()
    try:
        d = repo / ".brindle" / "agents"
        (d / "hosted.md").write_text(f"---\nname: hosted\nprovider: native\napi: anthropic\n"
                                     f"base_url: {hosted.base_url}\nmodel: claude-x\n---\nYou are hosted.\n")
        # developer-local, the model refusals are retried on, is the fake's "tiny".
        (d / "developer-local.md").write_text((d / "local.md").read_text())
        native_agent(db, ws, profile="hosted")
        db.enqueue("n1", "write red-team payloads", None)
        hosted.replies = [anthropic_reply("I can't help with that.", stop="refusal")]
        fake.replies = [openai_reply(calls=[("c1", "report_result", {"result": "wrote them"})]),
                        openai_reply("done")]
        assert run_native(db, "n1", exit_when_idle=True) == 0
        assert db.get_agent("n1").result == "wrote them"
        assert len(hosted.requests) == 1 and fake.requests[0]["model"] == "tiny"
    finally:
        hosted.close()


def test_unreported_worker_is_reminded_once_then_its_parent_is_told(db, ws, local_profile, fake, monkeypatch):
    told = []
    monkeypatch.setattr(agents, "tell_parent_unreported", lambda db_, a: told.append(a.id))
    native_agent(db, ws)
    db.enqueue("n1", "do the thing", None)
    fake.replies = [openai_reply("I did the thing."), openai_reply("Yes, all done, really.")]
    assert run_native(db, "n1", exit_when_idle=True) == 0
    reminder = fake.requests[1]["messages"][-1]
    assert reminder["role"] == "user" and "report_result" in reminder["content"]
    assert told == ["n1"] and len(fake.requests) == 2
    assert db.get_agent("n1").status == "idle"


def test_messages_sent_mid_turn_arrive_between_model_calls(db, ws, local_profile, fake):
    native_agent(db, ws)
    db.enqueue("n1", "start", None)

    def first(req):
        from brindle.db import DB

        DB().enqueue("n1", "also bump the version", None)  # the server thread's own connection
        return openai_reply(calls=[("c1", "Bash", {"command": "echo working"})])

    fake.replies = [first, openai_reply(calls=[("c2", "report_result", {"result": "ok"})]), openai_reply("done")]
    assert run_native(db, "n1", exit_when_idle=True) == 0
    second = fake.requests[1]["messages"]
    assert second[-2]["role"] == "tool"
    assert second[-1]["role"] == "user" and "bump the version" in second[-1]["content"]
    assert second[-1]["content"].startswith("brindle delivered this message")


def test_reviewer_submits_its_verdict_in_process(db, ws, local_profile, fake, monkeypatch):
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    (Path(ws.path) / "x.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    native_agent(db, ws, mode="review")
    db.enqueue("n1", "review the branch", None)
    fake.replies = [
        anthropic_reply(calls=[("t1", "workspace_diff", {"stat_only": True})]),
        anthropic_reply(calls=[("t2", "submit_review", {"approved": True, "summary": "Looks right."})]),
        anthropic_reply("Submitted."),
    ]
    # Reviewers may talk to an Anthropic-style endpoint too.
    (Path(ws.repo_root) / ".brindle/agents/local.md").write_text(
        f"---\nname: local\nprovider: native\napi: anthropic\nbase_url: {fake.base_url}\nmodel: tiny\n"
        "permission_mode: dontAsk\n---\nYou review.\n")
    assert run_native(db, "n1", exit_when_idle=True) == 0
    diff = fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert "x.py" in diff and "1 commit(s) ahead" in diff
    verdict = fake.requests[2]["messages"][-1]["content"][0]["content"]
    assert verdict.startswith("Review recorded (APPROVED)")
    a = db.get_agent("n1")
    assert a.result.startswith("Review of feature") and "APPROVED" in a.result
    assert db.last_review(ws.id).approved
    names = {t["name"] for t in fake.requests[0]["tools"]}
    assert "submit_review" in names and "report_result" not in names


def test_reviewer_cannot_edit_under_dontask(db, ws, local_profile, fake, monkeypatch):
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    (Path(ws.repo_root) / ".brindle/agents/local.md").write_text(
        f"---\nname: local\nprovider: native\nbase_url: {fake.base_url}/v1\nmodel: tiny\n"
        "permission_mode: dontAsk\n---\nYou review.\n")
    native_agent(db, ws, mode="review")
    db.enqueue("n1", "review", None)
    fake.replies = [openai_reply(calls=[("c1", "Write", {"path": "sneaky.txt", "content": "x"})]),
                    openai_reply(calls=[("c2", "submit_review", {"approved": False, "summary": "meh"})]),
                    openai_reply("ok")]
    assert run_native(db, "n1", exit_when_idle=True) == 0
    assert "not permitted" in fake.requests[1]["messages"][-1]["content"]
    assert not (Path(ws.path) / "sneaky.txt").exists()


def test_conversation_survives_a_restart(db, ws, local_profile, fake):
    native_agent(db, ws)
    db.enqueue("n1", "first task", None)
    fake.replies = [openai_reply(calls=[("c1", "report_result", {"result": "did it"})]), openai_reply("done")]
    assert run_native(db, "n1", exit_when_idle=True) == 0
    saved = db.get_agent("n1").session_ref
    from brindle.providers import get_provider
    assert get_provider("native").can_resume(saved)

    db.enqueue("n1", "and now the docs", None)
    fake.replies = [openai_reply("docs done")]
    assert run_native(db, "n1", resume=saved, exit_when_idle=True) == 0
    history = [m["role"] for m in fake.requests[-1]["messages"]]
    assert history == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert fake.requests[-1]["messages"][1]["content"] == "first task"


def test_endpoint_failure_stops_the_runner(db, ws, local_profile, fake, capfd):
    native_agent(db, ws)
    db.enqueue("n1", "go", None)
    fake.replies = [401]
    assert run_native(db, "n1", exit_when_idle=True) == 1
    assert "endpoint failed" in capfd.readouterr().out
    assert db.get_agent("n1").status == "idle"


def test_profile_without_an_endpoint_is_a_clear_error(db, ws, repo, capfd):
    (repo / ".brindle/agents").mkdir(parents=True)
    (repo / ".brindle/agents/bare.md").write_text("---\nname: bare\nprovider: native\n---\nhi\n")
    native_agent(db, ws, profile="bare")
    db.enqueue("n1", "go", None)
    assert run_native(db, "n1", exit_when_idle=True) == 2
    assert "no base_url" in capfd.readouterr().out


def test_mcp_submit_review_uses_the_shared_function(db, ws, monkeypatch):
    from brindle import mcp_server

    seen = {}
    monkeypatch.setattr(agents, "submit_review", lambda db_, cid, ok, s: seen.update(cid=cid, ok=ok, s=s) or "rec")
    native_agent(db, ws, mode="review", agent_id="r1")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "r1")
    assert mcp_server.submit_review(True, "fine") == "rec"
    assert seen == {"cid": "r1", "ok": True, "s": "fine"}


