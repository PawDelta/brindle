"""Model-access errors: a cloud account that can't call a model is named
(cause and one-line fix), routing moves to the next profile of the tier and
says so, and Claude Code's own alias fallback is surfaced."""

import json
import subprocess
import time

import pytest

from brindle import agents, autopilot, ci_adapters, ci_client, cull, model_access, quota, tmux, workspaces
from brindle.ci_adapters import ClaudeAdapter
from brindle.config import load_repo_config
from brindle.db import Agent, Task

from test_ci_adapters import PROMPT, _launch_showing

# (error text, cause key, words the cause names, words the fix names)
ERRORS = [
    ("API Error: 403 Access denied: model anthropic.claude-opus-5-5 is not available for this account",
     "bedrock_quota", "quota", "AWS support case"),
    ("API Error: 400 Model use case details have not been submitted for this account",
     "bedrock_use_case", "use-case form", "15 minutes"),
    ("API Error: 404 The model 'claude-opus-5-5' does not exist",
     "bedrock_endpoint", "endpoint or model ID", "`us.` or `global.`"),
    ("API Error: 429 Quota exceeded for aiplatform.googleapis.com/online_prediction_requests_per_base_model",
     "vertex_quota", "no Claude quota", "48 hours"),
    ("API Error: 403 Model requires data sharing to be enabled for publisher 'anthropic'",
     "vertex_data_sharing", "data sharing", "publisher 'anthropic'"),
    ("API Error: 429 InsufficientQuota: no quota for this deployment",
     "foundry_quota", "InsufficientQuota", "Foundry portal"),
    ("API Error: 400 InvalidModelProviderData: missing organization",
     "foundry_provider_data", "InvalidModelProviderData", "organization, industry and country"),
]


@pytest.mark.parametrize("text, key, cause_words, fix_words", [ERRORS[1], ERRORS[5], ERRORS[6]],
                         ids=["bedrock_use_case", "foundry_quota", "foundry_provider_data"])
def test_each_error_is_classified_with_a_cause_and_a_fix(text, key, cause_words, fix_words):
    cause = model_access.classify(text)
    assert cause is not None and cause.key == key
    assert cause_words in cause.cause and fix_words in cause.fix
    assert cause.line() == f"{cause.cause}. Fix: {cause.fix}"


def screen(error):
    return (f"● {error}\n✻ Cooked for 1s · done 8:19 PM\n" + "─" * 80 + "\n❯\n" + "─" * 80 + "\n")


def test_a_refused_profile_is_skipped_for_the_next_in_its_tier(db, repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)
    why = []
    assert autopilot.choose_profile(db, "boss", str(repo), weight="heavy", why=why)[0] == "developer-heavy"
    swap = model_access.swap(load_repo_config(repo).routing, "heavy", "developer-heavy", ERRORS[3][0])
    assert swap[0].key == "vertex_quota" and swap[1] == "developer-gemini"
    why = []
    name, _ = autopilot.choose_profile(db, "boss", str(repo), weight="heavy", why=why)
    assert name == "developer-gemini"
    assert "model refused for this account" in why[0] and "skipped developer-heavy" in why[0]
    # and the swap is in the run notes, with its fix
    note = model_access.notes()[0]
    assert "developer-heavy" in note and "tasks moved to developer-gemini" in note and "Fix:" in note


def test_a_refusal_recorded_in_the_run_path_shows_in_session_notes(db, repo, monkeypatch):
    """The run path and the doctor check fingerprint under the repo root, like session_event reads."""
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "agents").mkdir(exist_ok=True)
    (repo / ".brindle" / "agents" / "developer.md").write_text(
        "---\nname: developer\nprovider: claude\nenv.AWS_REGION: repo-only-region\n---\nDo the work.\n")
    assert "repo-only-region" in model_access.fingerprint("developer", str(repo))
    assert model_access.fingerprint("developer") != model_access.fingerprint("developer", str(repo))
    _, root, _ = _launch_showing(monkeypatch, db, repo, [screen(ERRORS[2][0])])
    root.profile = "developer"
    db.add_agent(root)
    monkeypatch.setattr(ClaudeAdapter, "alive", lambda self, db_, r: True)
    monkeypatch.setattr(ClaudeAdapter, "provider_error", lambda self, db_, rid: None)
    ci_client.session_event(db, root.id, ClaudeAdapter(), cwd=str(repo))
    assert model_access.refused("developer", str(repo)).key == "bedrock_endpoint"
    event = ci_client.session_event(db, root.id, ClaudeAdapter(), cwd=str(repo))
    assert "model refused for developer" in event["note"]


def test_the_fingerprint_names_the_account_never_a_key(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cret")
    fp = model_access.fingerprint("developer")
    assert "AWS_REGION=us-east-1" in fp and "s3cret" not in fp
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert model_access.fingerprint("developer") != fp


def test_refusal_from_another_account_is_not_in_this_sessions_notes(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    model_access.refuse("developer", model_access.BEDROCK_QUOTA)
    assert model_access.notes(["developer"])
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert model_access.notes(["developer"]) == []       # same profile, another account
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert model_access.notes(["reviewer"]) == []        # not this session's profile


def test_no_next_profile_when_the_tier_is_used_up():
    model_access.refuse("developer-heavy", model_access.VERTEX_DATA_SHARING)
    cause, to = model_access.swap({"heavy": ["developer", "developer-heavy"]}, "heavy", "developer",
                                  ERRORS[0][0])
    assert to is None


def test_the_session_report_carries_the_swap_and_the_alias_warning(db, repo, monkeypatch):
    warning = "Opus 5.5 not available — using Opus 4.6 for this session"
    _, root, _ = _launch_showing(monkeypatch, db, repo, [PROMPT + "\n" + warning])
    root.profile = "developer"
    db.add_agent(root)
    monkeypatch.setattr(ClaudeAdapter, "alive", lambda self, db_, r: True)
    monkeypatch.setattr(ClaudeAdapter, "provider_error", lambda self, db_, rid: None)
    model_access.refuse("developer", model_access.BEDROCK_QUOTA, "developer-heavy")
    event = ci_client.session_event(db, root.id, ClaudeAdapter())
    assert "model refused for developer" in event["note"]
    assert "fell back to Opus 4.6" in event["note"] and "Opus 5.5" in event["note"]


def test_a_supervisor_is_told_about_a_refused_worker_and_about_an_alias_fallback(db, repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer-heavy", "claude", "boss", "assign", "idle", "@1", None, time.time()))
    db.add_agent(Agent("w2", ws.id, "developer", "claude", "boss", "assign", "idle", "@2", None,
                       time.time()))
    db.add_task(Task("t1", str(repo), "w1", "boss", ws.id, "developer-heavy", "do it", "assign", 1, None, None,
                     None, None, "started", time.time(), weight="heavy"))
    screens = {"@1": screen(ERRORS[2][0]),
               "@2": "⚠ Opus 5.5 not available — using Opus 4.6 for this session\n" + PROMPT}
    monkeypatch.setattr(tmux, "capture", lambda target, **kw: screens[target])
    monkeypatch.setattr(agents, "pane_owners", lambda db_, panes=None: {})
    for name in ("runs_process", "same_server", "is_alive", "owns_pane"):
        monkeypatch.setattr(agents, name, lambda *a, **kw: True)
    sent = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, sender_id=None: sent.append((to, body)))
    from brindle.pro import route_health
    monkeypatch.setattr(agents, "route_context", lambda db_, a, ws_: ("bedrock", "claude-opus-5-5"))
    monkeypatch.setattr(route_health, "reporting", lambda: True)
    route_health.reset()
    done = cull.note_model_refused(db, time.time(), {})
    assert len(done) == 2
    refused_msg = next(b for _t, b in sent if "w1" in b)
    assert "can't call its model" in refused_msg and "use the `us.`" in refused_msg
    assert "developer-gemini" in refused_msg    # the next profile in its tier
    assert "fell back to Opus 4.6" in next(b for _t, b in sent if "w2" in b)
    assert model_access.refused("developer-heavy").key == "bedrock_endpoint"
    sent.clear()
    assert cull.note_model_refused(db, time.time(), {}) == [] and sent == []   # told once
    # ... and the 404 counts toward route health once, as a code, not the error line
    assert route_health.pending() == {(ws.repo_root, "claude", "bedrock", "claude-opus-5-5", "provider_404"): 1}
    route_health.reset()


def test_the_ci_doctor_model_check_names_each_refusal(repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"routing": {"heavy": ["developer-heavy"]}}))
    monkeypatch.setattr(ci_adapters, "pinned_models",
                        lambda cwd: [("developer", "claude-opus-5-5"), ("developer-heavy", "claude-fable-5-1")])
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "claude-fable-5-1" in argv:
            return subprocess.CompletedProcess(argv, 1, "", ERRORS[4][0])
        return subprocess.CompletedProcess(argv, 0, '{"result": "ok"}', "")

    claude = ClaudeAdapter()
    monkeypatch.setattr(ClaudeAdapter, "available", lambda self, env: (True, ""))
    lines = ci_adapters.check_models({"claude": claude}, {"ANTHROPIC_API_KEY": "k", "PATH": ""}, str(repo),
                                     False, run=fake_run)
    assert len(calls) == 2, "one tiny call per pinned model"
    assert lines[0] == "model claude-opus-5-5 (developer): ok"
    assert "refused" in lines[1] and "publisher 'anthropic'" in lines[1] and "Fix:" in lines[1]
    assert model_access.refused("developer-heavy", str(repo)).key == "vertex_data_sharing"
