"""Configuring native workers: profile fields and env lines, the built-in
local profiles, and what `brindle doctor` says about their endpoints."""

import pytest

from brindle import doctor
from brindle.native.client import Endpoint
from brindle.native.runner import probe
from brindle.profiles import load_profile
from test_native_loop import FakeEndpoint


@pytest.fixture
def fake():
    ep = FakeEndpoint()
    yield ep
    ep.close()


def test_profile_env_lines_and_context_sizes(tmp_path):
    d = tmp_path / ".brindle" / "agents"
    d.mkdir(parents=True)
    (d / "glm.md").write_text(
        "---\nname: glm\nprovider: claude\nmodel: glm-4.7-flash\n"
        "env.ANTHROPIC_BASE_URL: https://api.z.ai/api/anthropic\n"
        "env.ANTHROPIC_AUTH_TOKEN: 'k#1'   # quoted, so the # stays\n"
        "env.ANTHROPIC_API_KEY:\n"
        "context_tokens: 128_000\n---\nhi\n")
    p = load_profile("glm", str(tmp_path))
    assert p.env == {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                     "ANTHROPIC_AUTH_TOKEN": "k#1", "ANTHROPIC_API_KEY": ""}
    assert p.context_tokens == 128000
    (d / "k.md").write_text("---\nname: k\ncontext_tokens: 64k\n---\n")
    assert load_profile("k", str(tmp_path)).context_tokens == 64000
    (d / "bad.md").write_text("---\nname: bad\ncontext_tokens: lots\n---\n")
    assert load_profile("bad", str(tmp_path)).context_tokens is None




def test_doctor_checks_each_native_endpoint_once(repo, fake, monkeypatch):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True)
    common = f"provider: native\nbase_url: {fake.base_url}/v1\nmodel: tiny\n"
    (d / "one.md").write_text(f"---\nname: one\n{common}---\nhi\n")
    (d / "two.md").write_text(f"---\nname: two\n{common}---\nhi\n")
    (d / "bare.md").write_text("---\nname: bare\nprovider: native\n---\nhi\n")
    # The built-in local profiles point at Ollama, which isn't running here.
    monkeypatch.setattr("brindle.native.runner.probe",
                        lambda ep, timeout=3.0: (False, "not reachable: refused") if "11434" in ep.base_url
                        else probe(ep, timeout))
    checks = {c.name: c for c in doctor.native_checks(str(repo))}
    assert checks["model tiny"].level == doctor.OK and "one, two" in checks["model tiny"].detail
    assert checks["profile bare"].level == doctor.FAIL and "no base_url" in checks["profile bare"].detail
    local = checks["model qwen3-coder:30b"]
    assert local.level == doctor.WARN and "ollama pull qwen3-coder:30b" in local.detail
    assert "developer-local" in local.detail and "reviewer-local" in local.detail
    # And they're part of the full report.
    monkeypatch.setattr("brindle.updates.notice", lambda **kw: "update-data")
    monkeypatch.setattr(doctor, "_tool", lambda *a, **k: doctor.Check(doctor.OK, a[0], "stub"))
    report = doctor.checks(str(repo))
    assert any(c.detail == "update-data" and c.level == doctor.OK for c in report)
    names = [c.name for c in report]
    assert "model tiny" in names and "profile bare" in names
