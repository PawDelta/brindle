"""Regression tests for corporate usage parsing, unknown usage, and limit-error failover.

Every test runs on fakes and a fixed clock (NOW), never the network or real CLIs.
"""

import json
import subprocess
import time
from datetime import datetime, timezone

import pytest

from brindle import (
    agents,
    autopilot,
    company_login,
    policy,
    preflight,
    provider_health,
    provider_homes,
    provider_switch,
    providers,
    quota,
    spawn,
    subscription_usage,
    tmux,
    workspaces,
)
from brindle.config import RepoConfig
from brindle.db import Agent

# 2026-11-21T12:00:00Z — matches the Nov/Dec reset dates in corporate fixtures.
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
DAY = 24 * HOUR


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(quota.time, "time", lambda: NOW)
    monkeypatch.setattr(provider_health, "_clock", lambda: NOW)


def _claude_login(tmp_path, monkeypatch, account: dict):
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"oauthAccount": account}), encoding="utf-8")
    monkeypatch.setattr(providers, "claude_global_config", lambda: str(cfg))
    monkeypatch.setattr(provider_homes, "launch_env", lambda provider: {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    monkeypatch.setattr(quota, "_key_billed", lambda provider: False)
    return cfg


def _write_codex_rollout(tmp_path, monkeypatch, events: list[dict], name: str = "rollout-2026-11-21T12-00-00-corp.jsonl"):
    home = tmp_path / "codex-home"
    d = home / "sessions" / "2026" / "11" / "21"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    monkeypatch.setattr(providers, "codex_home", lambda: str(home))
    return path


def _session(db, repo, provider: str = "claude", root_id: str = "root"):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent(root_id, ws.id, "supervisor", provider, None, "interactive",
                       "running", "@1", None, NOW))
    db.add_autopilot(root_id)
    return ws


def test_claude_corporate_cli_usage_parses_org_spend_and_no_weekly(tmp_path, brindle_home, clock, monkeypatch):
    """Enterprise/Team `/usage` output: org-scoped windows are plan-wide (not one-model caps),
    spend ratios compute exact percent, date-only resets parse, and no-weekly accounts work."""
    _claude_login(tmp_path, monkeypatch, {"organizationUuid": "org-ent-1", "emailAddress": "dev@corp.example"})

    org_text = (
        "Current week (organization): 85% used · resets Nov 23 at 9am (UTC)\n"
        "Extra usage: $150.00 / $200.00 spent · resets Dec 1 (UTC)\n"
        "Current week (Fable): 12% used · resets Nov 23 at 9am (UTC)\n"
    )
    windows = quota.parse_claude_usage(org_text, now=NOW)
    plan = [w for w in windows if w.name is None]
    caps = [w for w in windows if w.name is not None]

    assert [(w.used, w.minutes) for w in plan] == [(85.0, 10080), (75.0, 43200)]
    assert all(w.resets_at is not None and w.resets_at > NOW for w in plan)
    assert [(w.name, w.used) for w in caps] == [("Fable", 12.0)]

    # Recorded under the SSO org identity (no accountUuid required when organizationUuid is present).
    quota.record("claude", windows, source="claude /usage")
    assert quota.fullest(quota.get("claude", now=NOW)).used == 85.0
    assert quota.known_headroom("claude") == 15.0

    # Corporate account with only a monthly spend limit (no weekly window at all).
    spend_only = "Monthly spend: $90.00 / $120.00 · resets Dec 1 (UTC)\n"
    only_windows = quota.parse_claude_usage(spend_only, now=NOW)
    assert len(only_windows) == 1 and only_windows[0].used == 75.0 and only_windows[0].minutes == 43200
    assert only_windows[0].resets_at == datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    quota.record("claude", only_windows, source="claude /usage")
    u = autopilot.usage()
    assert u is not None and u["window"] == "monthly" and u["used"] == 75.0


def test_claude_corporate_status_line_iso_reset_spend_and_extra_usage_overage(tmp_path, brindle_home, clock, monkeypatch):
    """Status-line payloads with ISO resets, spend ratios, and extra_usage overage parse accurately
    and do not fail over early while paid extra usage still has room."""
    _claude_login(tmp_path, monkeypatch, {"accountUuid": "acct-1", "organizationUuid": "org-ent-1"})

    # Base 5h window is at 100%, but extra_usage is enabled at $40 / $200 (20%) -> NOT limited yet.
    assert quota.record_claude({
        "rate_limits": {
            "five_hour": {"used_percentage": 100, "resets_at": "2026-11-21T15:00:00Z"},
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 40.0,
                "monthly_limit": 200.0,
                "resets_at": "2026-12-01T00:00:00Z",
            },
        }
    })
    assert provider_health.limit("claude", now=NOW).ok
    assert quota.known_headroom("claude") == 80.0

    # Once the extra_usage spend cap itself reaches 100%, Claude IS limited until its ISO reset.
    assert quota.record_claude({
        "rate_limits": {
            "five_hour": {"used_percentage": 100, "resets_at": "2026-11-21T15:00:00Z"},
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 200.0,
                "monthly_limit": 200.0,
                "resets_at": "2026-12-01T00:00:00Z",
            },
        }
    })
    health = provider_health.limit("claude", now=NOW)
    assert health.state == provider_health.LIMITED
    assert health.until == datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc).timestamp()


def test_codex_corporate_rollout_parses_individual_limit_model_cap_credits_and_spend_control(
        tmp_path, brindle_home, clock, monkeypatch):
    """Codex corporate rollouts: individual_limit counts as plan-wide, named model caps stay scoped,
    workspace credits prevent premature cutoff, and spend_control_reached triggers LIMITED."""
    reset = NOW + 3 * DAY

    # 1. Per-model cap (Spark at 100%) must NOT limit general Codex models; individual_limit (72%) does apply.
    _write_codex_rollout(tmp_path, monkeypatch, [
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "limit_id": "codex",
            "limit_name": None,
            "primary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": reset},
            "secondary": None,
            "individual_limit": {"used": 72.0, "limit": 100.0, "window_minutes": 43200, "resets_at": reset},
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "spend_control_reached": False,
            "plan_type": "enterprise",
            "rate_limit_reached_type": None,
        }}},
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "limit_id": "codex_spark",
            "limit_name": "GPT-5.3-Codex-Spark",
            "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": NOW + HOUR},
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "plan_type": "enterprise",
        }}},
    ])
    assert quota.refresh_codex()
    assert quota.known_headroom("codex") == 28.0
    assert provider_health.limit("codex", model="gpt-5.4", now=NOW).ok
    assert provider_health.limit("codex", model="gpt-5.3-codex-spark", now=NOW).state == provider_health.LIMITED

    # 2. Workspace credits covering overage: primary 100% with active balance is NOT limited yet.
    _write_codex_rollout(tmp_path, monkeypatch, [
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "limit_id": "codex",
            "limit_name": None,
            "primary": {"used_percent": 100.0, "window_minutes": 10080, "resets_at": reset},
            "credits": {"has_credits": True, "unlimited": False, "balance": "125.50"},
            "spend_control_reached": False,
            "plan_type": "business",
            "rate_limit_reached_type": None,
        }}},
    ])
    assert quota.refresh_codex()
    assert provider_health.limit("codex", now=NOW).ok

    # 3. Corporate spend_control_reached (with no weekly window!) DOES trigger LIMITED.
    _write_codex_rollout(tmp_path, monkeypatch, [
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "limit_id": "codex",
            "limit_name": None,
            "primary": None,
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "spend_control_reached": True,
            "plan_type": "enterprise",
            "rate_limit_reached_type": "spend_control_reached",
        }}},
    ])
    assert quota.refresh_codex()
    health = provider_health.limit("codex", now=NOW)
    assert health.state == provider_health.LIMITED and health.until is not None and health.until > NOW


def test_corporate_cloud_route_ignores_personal_subscription_and_unknown_usage_drives_no_failover(
        db, repo, tmp_path, brindle_home, clock, monkeypatch):
    """A corporate Bedrock/Vertex/org-managed route must ignore leftover personal Claude.ai windows;
    403/missing usage reads as 'unknown' and neither triggers nor blocks failover by itself."""
    _claude_login(tmp_path, monkeypatch, {"accountUuid": "personal-1", "organizationUuid": "personal-org"})
    # Seed a full 100% personal window first.
    quota.record("claude", [quota.Window(100.0, NOW + DAY, 10080)], source="status line")
    assert provider_health.limit("claude", now=NOW).state == provider_health.LIMITED

    # Switch to corporate Bedrock via company_login stored env (not os.environ).
    monkeypatch.setattr(company_login, "stored_env", lambda: {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1"})
    runs = []
    monkeypatch.setattr(spawn, "read", lambda argv, **kw: runs.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))

    assert quota.get("claude", now=NOW) is None
    assert quota.status("claude", now=NOW) == "unknown"
    assert quota.known_headroom("claude") is None
    assert not quota.refresh_claude_usage(force=True)
    assert runs == []

    # Unknown usage must NOT trigger failover or block routing (even with an aggressive usage_limit).
    monkeypatch.setattr(preflight, "check", lambda provider, repo_root=None, **kw: [])
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: tmux.PaneSnapshot())
    monkeypatch.setattr(providers, "signed_out", lambda provider, env=None: None)
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr("brindle.local_setup.memory_problem", lambda profile: None)
    monkeypatch.setattr(policy, "check_assign", lambda *a, **k: policy.Decision(allowed=True))
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)

    _session(db, repo, provider="claude")
    assert provider_health.limit("claude", now=NOW).ok
    assert provider_switch.maybe_offer(db, "root", NOW) is None
    assert autopilot._unavailable("developer", RepoConfig(usage_limit=0, limit_headroom=100), str(repo)) is None

    # Unknown usage on Codex (after switching to API-key/null rate_limits rollout) clears stale windows
    # and does NOT block Codex from being chosen as a failover target.
    quota.record("codex", [quota.Window(100.0, NOW + DAY, 10080)], source="codex rollout")
    _write_codex_rollout(tmp_path, monkeypatch, [
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": None}},
    ])
    quota.refresh_codex()
    assert quota.get("codex", now=NOW) is None
    assert quota.status("codex", now=NOW) == "unknown"
    assert provider_switch.pick_target("claude", str(repo), NOW) == "codex"


def test_claude_usage_403_and_unbounded_spend_report_unknown(tmp_path, brindle_home, clock, monkeypatch):
    """HTTP 403, missing fields, and unbounded spend from a corporate account must report unknown,
    never a guessed 0% or 100%."""
    _claude_login(tmp_path, monkeypatch, {"accountUuid": "corp-1", "organizationUuid": "org-ent-2"})
    monkeypatch.setattr(quota, "_claude_usage_at", 0.0)
    monkeypatch.setattr(quota, "_claude_usage_identity", None)
    monkeypatch.setattr(providers, "claude_binary", lambda: "claude")

    # 403 from /usage -> unknown, no windows recorded.
    monkeypatch.setattr(spawn, "read", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "Error: 403 Forbidden: usage endpoint not available for enterprise org"))
    assert not quota.refresh_claude_usage(force=True)
    assert quota.get("claude", now=NOW) is None
    assert quota.status("claude", now=NOW) == "unknown"

    # Unbounded spend ($84 spent, no cap) and missing percentage fields -> unknown.
    assert quota.parse_claude_usage("Extra usage: $84.00 spent · resets Dec 1 (UTC)\n", now=NOW) == []
    assert not quota.record_claude({"rate_limits": {"five_hour": {"used_percentage": None}, "seven_day": {}}})
    assert quota.get("claude", now=NOW) is None
    assert quota.status("claude", now=NOW) == "unknown"


def test_real_limit_error_on_corporate_unknown_usage_still_triggers_failover(
        db, repo, tmp_path, brindle_home, clock, monkeypatch):
    """When corporate usage is unknown (Bedrock / OAuth token / API key / 403), a real quota or
    spend limit error from the CLI must still record LIMITED and trigger failover."""
    # No .claude.json oauthAccount AND Bedrock route active (unknown usage).
    monkeypatch.setattr(providers, "claude_global_config", lambda: str(tmp_path / "missing.claude.json"))
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setattr(preflight, "check", lambda provider, repo_root=None, **kw: [])
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: tmux.PaneSnapshot())
    monkeypatch.setattr(quota, "_refresh_codex_lazily", lambda: None)

    _session(db, repo, provider="claude")
    assert provider_switch.maybe_offer(db, "root", NOW) is None
    assert quota.get("claude", now=NOW) is None

    # A real quota/spend limit stop-failure from the CLI triggers LIMITED and creates a switch offer.
    agents.handle_hook(db, "root", "stop-failure", {
        "error": "rate_limit_error",
        "error_details": "You've hit your organization usage limit · resets 5pm (UTC)",
    })
    health = provider_health.limit("claude", now=NOW)
    assert health.state == provider_health.LIMITED
    assert health.until == datetime(2026, 11, 21, 17, 0, tzinfo=timezone.utc).timestamp()
    assert quota.status("claude", now=NOW) == "limited"
    assert quota.known_headroom("claude") == 0.0

    offer = provider_switch.open_offer(db, "root") or provider_switch.maybe_offer(db, "root", NOW)
    assert offer is not None and offer.from_provider == "claude" and offer.to_provider == "codex"
