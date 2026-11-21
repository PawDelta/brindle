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
    cur = [NOW]
    monkeypatch.setattr(time, "time", lambda: cur[0])
    monkeypatch.setattr(quota.time, "time", lambda: cur[0])
    monkeypatch.setattr(provider_health, "_clock", lambda: cur[0])
    return cur


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
    spend ratios compute exact percent, date-only resets parse, no-weekly accounts work, and
    included vs Extra usage overage follows the same availability rules as status-line data."""
    _claude_login(tmp_path, monkeypatch, {"organizationUuid": "org-ent-1", "emailAddress": "dev@corp.example"})

    org_text = (
        "Current week (organization): 85% used · resets Nov 23 at 9am (UTC)\n"
        "Extra usage: $150.00 / $200.00 spent · resets Dec 1 (UTC)\n"
        "Current week (Fable): 12% used · resets Nov 23 at 9am (UTC)\n"
    )
    windows = quota.parse_claude_usage(org_text, now=NOW)
    plan = [w for w in windows if w.name is None]
    caps = [w for w in windows if w.name is not None]

    assert [(w.used, w.minutes, w.overage) for w in plan] == [(85.0, 10080, False), (75.0, 43200, True)]
    assert all(w.resets_at is not None and w.resets_at > NOW for w in plan)
    assert [(w.name, w.used) for w in caps] == [("Fable", 12.0)]

    # Recorded under the SSO org identity (no accountUuid required when organizationUuid is present).
    quota.record("claude", windows, source="claude /usage")
    assert quota.fullest(quota.get("claude", now=NOW)).used == 85.0
    assert quota.known_headroom("claude") == 15.0

    # Shared included-vs-overage rules on `/usage` output:
    # 1) Included 100% + Extra usage $40/$200 -> overage still serves work (NOT limited).
    overage_text = (
        "Current session: 100% used · resets Nov 21 at 3pm (UTC)\n"
        "Extra usage: $40.00 / $200.00 spent · resets Dec 1 (UTC)\n"
    )
    quota.record("claude", quota.parse_claude_usage(overage_text, now=NOW), source="claude /usage")
    assert provider_health.limit("claude", now=NOW).ok
    assert quota.known_headroom("claude") == 80.0

    # 2) Included 20% + Extra usage $200/$200 -> included still serves work (NOT limited).
    included_text = (
        "Current session: 20% used · resets Nov 21 at 3pm (UTC)\n"
        "Extra usage: $200.00 / $200.00 spent · resets Dec 1 (UTC)\n"
    )
    quota.record("claude", quota.parse_claude_usage(included_text, now=NOW), source="claude /usage")
    assert provider_health.limit("claude", now=NOW).ok
    assert quota.known_headroom("claude") == 80.0

    # 3) Both at 100% -> LIMITED until the earlier reset (3pm UTC), recovering once included resets.
    both_full_text = (
        "Current session: 100% used · resets Nov 21 at 3pm (UTC)\n"
        "Extra usage: $200.00 / $200.00 spent · resets Dec 1 (UTC)\n"
    )
    quota.record("claude", quota.parse_claude_usage(both_full_text, now=NOW), source="claude /usage")
    three_pm = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc).timestamp()
    health = provider_health.limit("claude", now=NOW)
    assert health.state == provider_health.LIMITED and health.until == three_pm
    assert provider_health.limit("claude", now=three_pm + 1).ok

    # Corporate account with only a monthly spend limit (no weekly window at all).
    spend_only = "Monthly spend: $90.00 / $120.00 · resets Dec 1 (UTC)\n"
    only_windows = quota.parse_claude_usage(spend_only, now=NOW)
    assert len(only_windows) == 1 and only_windows[0].used == 75.0 and only_windows[0].minutes == 43200
    assert only_windows[0].resets_at == datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    quota.record("claude", only_windows, source="claude /usage")
    u = autopilot.usage()
    assert u is not None and u["window"] == "monthly" and u["used"] == 75.0


def test_claude_corporate_status_line_iso_reset_spend_and_extra_usage_overage(tmp_path, brindle_home, clock, monkeypatch):
    """Status-line payloads keep included allowance and optional paid extra_usage separate:
    either allowance can serve work, and when both are full recovery happens at the earlier reset."""
    _claude_login(tmp_path, monkeypatch, {"accountUuid": "acct-1", "organizationUuid": "org-ent-1"})
    three_pm = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc).timestamp()

    # 1. Included at 20%, paid extra_usage at 100% -> included allowance still serves work (NOT limited).
    assert quota.record_claude({
        "rate_limits": {
            "five_hour": {"used_percentage": 20, "resets_at": "2026-11-21T15:00:00Z"},
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 200.0,
                "monthly_limit": 200.0,
                "resets_at": "2026-12-01T00:00:00Z",
            },
        }
    })
    assert provider_health.limit("claude", now=NOW).ok
    assert quota.known_headroom("claude") == 80.0

    # 2. Base 5h window is at 100%, but extra_usage is enabled at $40 / $200 (20%) -> NOT limited yet.
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

    # 3. Once BOTH included and extra_usage reach 100%, Claude IS limited until the earlier reset
    # (when the 5h included allowance returns), and recovers once 5h resets even though monthly overage hasn't.
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
    assert health.until == three_pm
    assert provider_health.limit("claude", now=three_pm + 1).ok


def test_codex_corporate_rollout_parses_individual_limit_model_cap_credits_and_spend_control(
        tmp_path, brindle_home, clock, monkeypatch):
    """Codex corporate rollouts: individual_limit counts as plan-wide, named model caps stay scoped,
    workspace credits prevent premature cutoff, spend_control_reached triggers LIMITED, and re-reading
    an unchanged rollout after the original cooldown expires does NOT renew the limit."""
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

    # 3. Corporate spend_control_reached (with no weekly window) triggers LIMITED bound to the event
    # timestamp; advancing the clock past the deadline and re-reading the unchanged rollout recovers.
    _write_codex_rollout(tmp_path, monkeypatch, [
        {"timestamp": "2026-11-21T12:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
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
    expected_deadline = NOW + quota.FALLBACK_COOLDOWN_MINUTES * 60
    health = provider_health.limit("codex", now=NOW)
    assert health.state == provider_health.LIMITED and health.until == expected_deadline

    clock[0] = NOW + DAY
    assert not quota.refresh_codex()
    assert provider_health.limit("codex", now=clock[0]).ok
    assert quota.get("codex", now=clock[0]) is None

    # 4. Even an untimestamped spend_control_reached event pins its first-observed deadline by event
    # identity so repeated refreshes never extend the cooldown past expiry.
    t1 = clock[0]
    _write_codex_rollout(tmp_path, monkeypatch, [
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "limit_id": "codex",
            "limit_name": None,
            "primary": None,
            "secondary": None,
            "spend_control_reached": True,
            "plan_type": "enterprise",
        }}},
    ], name="rollout-2026-11-22T12-00-00-untimestamped.jsonl")
    assert quota.refresh_codex()
    pinned_until = t1 + quota.FALLBACK_COOLDOWN_MINUTES * 60
    assert provider_health.limit("codex", now=t1).until == pinned_until

    clock[0] = t1 + HOUR
    assert quota.refresh_codex()
    assert provider_health.limit("codex", now=clock[0]).until == pinned_until

    clock[0] = t1 + DAY
    assert not quota.refresh_codex()
    assert provider_health.limit("codex", now=clock[0]).ok
    assert quota.get("codex", now=clock[0]) is None


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
    with monkeypatch.context() as mp:
        mp.setattr(spawn, "read", lambda argv, **kw: runs.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
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

    # Corporate Codex gateway present ONLY in launch/profile env (empty daemon os.environ)
    # must ignore a cached 100% personal Codex window and isolate its own real limit errors.
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(quota, "_refresh_codex_lazily", lambda: None)
    quota.record("codex", [quota.Window(100.0, NOW + DAY, 10080)], source="codex rollout")
    assert provider_health.limit("codex", now=NOW).state == provider_health.LIMITED

    corp_env = {"OPENAI_BASE_URL": "https://gw.corp.example/v1"}
    other_env = {"OPENAI_BASE_URL": "https://other-gw.corp.example/v1"}
    assert provider_health.check("codex", env=corp_env, installed=False, now=NOW).ok
    assert quota.status("codex", env=corp_env, now=NOW) == "unknown"
    assert quota.known_headroom("codex", env=corp_env) is None

    # A profile supplying OPENAI_BASE_URL is not blocked by the cached personal Codex window,
    # and a Codex supervisor running it does not fail over on personal Codex usage.
    agents_dir = repo / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "corp-codex.md").write_text(
        "---\nprovider: codex\nenv.OPENAI_BASE_URL: https://gw.corp.example/v1\n---\nCorporate Codex.\n",
        encoding="utf-8",
    )
    assert autopilot._unavailable("corp-codex", RepoConfig(usage_limit=0, limit_headroom=100), str(repo)) is None
    ws_id = db.get_agent("root").workspace_id
    db.add_agent(Agent("codex-sup", ws_id, "corp-codex", "codex", None, "interactive",
                       "running", "@2", None, NOW))
    db.add_autopilot("codex-sup")
    assert provider_switch.out_of_quota(db, "codex-sup", NOW) is None

    # A real limit error on that corporate gateway is isolated to that route.
    agents.handle_hook(db, "codex-sup", "stop-failure", {
        "error": "rate_limit_error",
        "error_details": "spend_control_reached",
    })
    assert provider_health.limit("codex", env=corp_env, now=NOW).state == provider_health.LIMITED
    assert provider_switch.out_of_quota(db, "codex-sup", NOW) is not None
    assert provider_health.limit("codex", env=other_env, now=NOW).ok

    # Unknown usage on personal Codex (after switching to API-key/null rate_limits rollout) clears
    # stale windows and does NOT block Codex from being chosen as a failover target.
    _write_codex_rollout(tmp_path, monkeypatch, [
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": None}},
    ])
    quota.refresh_codex()
    assert quota.get("codex", now=NOW) is None
    assert quota.status("codex", now=NOW) == "unknown"
    assert provider_health.limit("codex", now=NOW).ok
    assert provider_health.limit("codex", env=corp_env, now=NOW).state == provider_health.LIMITED
    assert provider_switch.pick_target("claude", str(repo), NOW) == "codex"


def test_claude_usage_403_and_unbounded_spend_report_unknown(db, repo, tmp_path, brindle_home, clock, monkeypatch):
    """Confirmed HTTP 403 or unusable/unbounded spend from a corporate account invalidates stale
    cached usage (never leaving a stale 100% window to drive failover), while preserving any
    independently recorded real CLI limit error."""
    _claude_login(tmp_path, monkeypatch, {"accountUuid": "corp-1", "organizationUuid": "org-ent-2"})
    monkeypatch.setattr(quota, "_claude_usage_at", 0.0)
    monkeypatch.setattr(quota, "_claude_usage_identity", None)
    monkeypatch.setattr(providers, "claude_binary", lambda: "claude")
    monkeypatch.setattr(preflight, "check", lambda provider, repo_root=None, **kw: [])
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: tmux.PaneSnapshot())
    monkeypatch.setattr(quota, "_refresh_codex_lazily", lambda: None)

    _session(db, repo, provider="claude")
    # Seed a stale 100% cached window that currently drives LIMITED + failover offer.
    quota.record("claude", [quota.Window(100.0, NOW + DAY, 10080)], source="status line")
    assert provider_health.limit("claude", now=NOW).state == provider_health.LIMITED
    assert provider_switch.out_of_quota(db, "root", NOW) is not None

    # Confirmed 403 from /usage -> invalidates stale window; usage becomes unknown and does NOT fail over.
    monkeypatch.setattr(spawn, "read", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "Error: 403 Forbidden: usage endpoint not available for enterprise org"))
    assert not quota.refresh_claude_usage(force=True)
    assert quota.get("claude", now=NOW) is None
    assert quota.status("claude", now=NOW) == "unknown"
    assert quota.known_headroom("claude") is None
    assert provider_health.limit("claude", now=NOW).ok
    assert provider_switch.maybe_offer(db, "root", NOW) is None

    # Confirmed unbounded spend on exit 0 also invalidates stale cached windows.
    quota.record("claude", [quota.Window(100.0, NOW + DAY, 10080)], source="status line")
    monkeypatch.setattr(spawn, "read", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "Extra usage: $84.00 spent · resets Dec 1 (UTC)\n", ""))
    assert not quota.refresh_claude_usage(force=True)
    assert quota.get("claude", now=NOW) is None
    assert provider_health.limit("claude", now=NOW).ok

    # An independently recorded real limit error is preserved across a 403 refresh.
    quota.record_limit("claude", now=NOW)
    monkeypatch.setattr(spawn, "read", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "Error: 403 Forbidden: usage endpoint not available for enterprise org"))
    assert not quota.refresh_claude_usage(force=True)
    assert provider_health.limit("claude", now=NOW).state == provider_health.LIMITED

    # Missing percentage fields in status-line payloads never synthesize 0% or 100%.
    assert quota.parse_claude_usage("Extra usage: $84.00 spent · resets Dec 1 (UTC)\n", now=NOW) == []
    assert not quota.record_claude({"rate_limits": {"five_hour": {"used_percentage": None}, "seven_day": {}}})


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
