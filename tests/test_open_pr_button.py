"""The app's Open PR button (POST /v1/sessions/{root}/pr) on a ready session.

On 2026-11-18 it "didn't work" twice: once while brindle's own preparation was
still running the PR checks (the job failed with a misleading "a merge into or
edit of ... is under way"), then because the repository's pre-push hook refused
the branch (the job failed with git's raw push output). Pressing it must either
open the PR or say plainly why it didn't. Pushes go to a local bare origin and
`gh` is a fake on PATH: nothing real is pushed or opened.
"""

import os
from pathlib import Path

import pytest
from conftest import sh
from test_daemon_actions import _commit, _send, lead  # noqa: F401 - the ready-session fixture
from test_daemon_api import _repo_template, repo  # noqa: F401 - a copied template repo

from brindle import autopilot, bus, config, pipeline
from brindle.daemon import routes_actions


@pytest.fixture
def ready(db, lead, tmp_path, monkeypatch):
    """``lead`` ready for its PR (milestone passed, audit approved on HEAD), its check ``true``,
    and a fake ``gh`` that only records being called."""
    sess, called = Path(lead.path), tmp_path / "gh-called"
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {called}\necho https://github.com/o/r/pull/7\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    autopilot.set_goal(db, "lead", "Ship it", [("tests pass", "true", None)])
    db.record_check(db.milestones("lead")[0].id, True, "", "sha", passed_sha="sha")
    db.update_autopilot("lead", state="done", audit_sha=_commit(sess, "a.py", "a\n"), audit_ok=1)
    config.set_local(lead.repo_root, "checks", ["true"])
    return sess, called


@pytest.mark.parametrize("stream", [
    pytest.param(">&2", id="stderr", marks=pytest.mark.variant("the reported case: the hook refusing on stderr must reach the button")),
    pytest.param("", id="stdout", marks=pytest.mark.variant("a hook's reason on stdout must not be lost")),
])
def test_open_pr_says_why_when_the_pre_push_hook_refuses(db, lead, repo, ready, stream):
    sess, called = ready
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\necho 'pre-push: refusing to push: its history carries private research' {stream}\nexit 1\n")
    hook.chmod(0o755)
    code, reply = _send("POST", "/v1/sessions/lead/pr", {"title": "Ship it"})
    assert code == 202, reply
    failed = bus.tail(db, kinds=("job.done", "job.failed"))[-1]
    error = failed["payload"]["error"]
    assert failed["kind"] == "job.failed" and error["code"] == "push_refused", error
    assert "its history carries private research" in error["message"]
    assert "failed to push some refs" not in error["message"]
    assert not called.exists() and sh("git ls-remote origin sess", sess) == ""


def test_open_pr_while_brindle_prepares_it_says_the_checks_are_running(db, lead, ready):
    sess, called = ready
    key = ("lead", "sess")
    routes_actions._PR_WORK.add(key)
    # Brindle's preparation holds the branch while its checks run.
    with pipeline.base_lock(lead.repo_root, "sess"):
        code, reply = _send("POST", "/v1/sessions/lead/pr", {"title": "Ship it"})
    assert (code, reply["error"]["code"]) == (409, "busy"), reply
    assert bus.tail(db, kinds=("job.done", "job.failed")) == []
    routes_actions._PR_WORK.discard(key)
    # Once it's done, the same press opens the PR.
    code, reply = _send("POST", "/v1/sessions/lead/pr", {"title": "Ship it"})
    assert code == 202, reply
    assert bus.tail(db, kinds=("job.done", "job.failed"))[-1]["payload"]["result"]["url"] == "https://github.com/o/r/pull/7"
    assert sh("git ls-remote origin sess", sess) != ""
