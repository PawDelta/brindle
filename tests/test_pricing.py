"""Model prices, dollar spend (Claude transcripts and Codex rollouts), and
`brindle cost` / `brindle cost report`."""
import datetime as dt
import json
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, cost, git, pricing, savings, usage
from brindle.cli import app
from brindle.db import Agent, Task
from brindle.pricing import Price


def profile(repo, name, provider="claude", model=None, base_url=None):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", "description: test", f"provider: {provider}"]
    if model:
        lines.append(f"model: {model}")
    if base_url:
        lines += ["api: openai", f"base_url: {base_url}"]
    (d / f"{name}.md").write_text("\n".join(lines + ["---", "Do it.", ""]))


def tokens(model=None, input=0, output=0, cache_read=0, cache_creation=0):
    d = {"input": input, "output": output, "cache_read": cache_read, "cache_creation": cache_creation}
    if model:
        d["model"] = model
    return json.dumps(d)


# -- the price table ---------------------------------------------------------------------------------


def test_list_prices_match_the_providers_pages():
    assert pricing.PRICES["claude-sonnet-5-5"] == Price(2, 10, 2.5, pytest.approx(0.2))
    assert pricing.PRICES["claude-opus-5-5"] == Price(4, 20, 5, 0.2)
    assert pricing.PRICES["claude-fable-5-1"] == Price(10, 50, 12.5, 0.25)
    assert pricing.PRICES["claude-haiku-5-5"] == Price(0.10, 0.50, pytest.approx(0.125), pytest.approx(0.01))
    assert pricing.PRICES["claude-haiku-4-5"] == Price(1, 5, 1.25, pytest.approx(0.1))
    # OpenAI lists cache writes (1.25x input) for gpt-6 and gpt-5.6; for older models, none
    assert pricing.PRICES["gpt-6-luna"] == Price(0.10, 0.50, 0.125, 0.01)
    assert pricing.PRICES["gpt-6-astra"] == Price(10, 50, 12.50, 1)
    assert pricing.PRICES["gpt-5.6-sol"] == Price(4, 20, 5, 0.40)
    assert pricing.PRICES["gpt-5.3-codex"] == Price(1.75, 14, 1.75, 0.175)
    assert pricing.PRICES["gpt-5.5"] == Price(5, 30, 5, 0.50)


@pytest.mark.parametrize("model, key", [
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    ("claude-opus-5-5[1m]", "claude-opus-5-5"),
    ("anthropic/claude-sonnet-5-5", "claude-sonnet-5-5"),
    ("us.anthropic.claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("Claude-Fable-5-1", "claude-fable-5-1"),
    ("sonnet", "claude-sonnet-5-5"),
    ("openai/gpt-6-luna", "gpt-6-luna"),
])
def test_model_ids_are_normalized(model, key):
    assert pricing.normalize(model) == key
    assert pricing.price_for(model) == pricing.PRICES[key]


@pytest.mark.parametrize("model", [None, "", "gpt-5.7", "claude-opus-4-9", "qwen3-coder:30b",
                                   "<synthetic>"])
def test_unknown_models_have_no_price_not_a_guess(model):
    assert pricing.price_for(model) is None


def test_cost_per_million_tokens():
    p = Price(input=3, output=15, cache_write=3.75, cache_read=0.3)
    assert p.cost(1_000_000, 0, 0, 0) == pytest.approx(3)
    assert p.cost(input_tokens=100_000, output_tokens=10_000, cache_write_tokens=20_000,
                  cache_read_tokens=500_000) == pytest.approx(0.3 + 0.15 + 0.075 + 0.15)


def test_stale_warning_after_90_days():
    assert pricing.stale_warning(pricing.AS_OF + dt.timedelta(days=90)) is None
    warning = pricing.stale_warning(pricing.AS_OF + dt.timedelta(days=91))
    assert warning and pricing.AS_OF.isoformat() in warning and "pricing" in warning


def test_money():
    assert pricing.money(12.345) == "$12.35"
    assert pricing.money(0.004) == "<$0.01"
    assert pricing.money(0) == "$0.00"


# -- overrides ---------------------------------------------------------------------------------------


def test_overrides_add_and_replace_prices():
    extra = pricing.overrides({"my-model": {"input": 1, "output": 2},
                               "Claude-Sonnet-5-5": {"input": 9, "output": 9, "cache_read": 1},
                               "bad": {"input": "x", "output": 1}, "worse": 3, "neg": {"input": -1, "output": 1}})
    assert set(extra) == {"my-model", "claude-sonnet-5-5"}
    assert pricing.price_for("my-model", extra) == Price(1, 2, 1, 1)   # missing cache rates: input rate
    assert pricing.price_for("claude-sonnet-5-5-20260101", extra) == Price(9, 9, 9, 1)
    assert pricing.price_for("claude-haiku-4-5", extra) == pricing.PRICES["claude-haiku-4-5"]


def test_overrides_merge_from_user_repo_and_local_config(repo, brindle_home):
    brindle_home.mkdir(parents=True, exist_ok=True)
    (brindle_home / "config.json").write_text(json.dumps(
        {"pricing": {"a": {"input": 1, "output": 1}, "b": {"input": 1, "output": 1}}}))
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"pricing": {"b": {"input": 2, "output": 2}, "c": {"input": 2, "output": 2}}}))
    (repo / ".brindle" / "config.local.json").write_text(json.dumps(
        {"pricing": {"c": {"input": 3, "output": 3}}}))
    extra = pricing.repo_overrides(str(repo))
    assert {k: v.input for k, v in extra.items()} == {"a": 1, "b": 2, "c": 3}
    assert {k: v.input for k, v in pricing.repo_overrides(None).items()} == {"a": 1, "b": 1}


def test_a_broken_config_means_list_prices_only(repo):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text("{nope")
    assert pricing.repo_overrides(str(repo)) == {}


# -- profiles ----------------------------------------------------------------------------------------


def test_profile_prices(repo):
    profile(repo, "cheap", model="haiku")
    profile(repo, "local", provider="native", model="qwen3-coder:30b",
            base_url="http://localhost:11434/v1")
    profile(repo, "default-model")
    assert pricing.profile_price("cheap", str(repo)) == pricing.PRICES["claude-haiku-4-5"]
    assert pricing.profile_price("local", str(repo)) == pricing.FREE
    assert pricing.profile_price("default-model", str(repo)) is None
    assert pricing.profile_price("no-such-profile", str(repo)) is None


def test_cost_rank_comes_from_real_prices(repo):
    from brindle.pro import learning

    profile(repo, "local", provider="native", model="qwen3-coder:30b",
            base_url="http://127.0.0.1:11434/v1")
    profile(repo, "luna", provider="codex", model="gpt-6-luna")          # $0.50 out
    profile(repo, "sonnet", model="claude-sonnet-5-5")                   # $10 out
    profile(repo, "opus", model="claude-opus-5-5")                       # $20 out
    profile(repo, "pricey-mini", model="house-mini")                     # priced by override
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"pricing": {"house-mini": {"input": 20, "output": 100}}}))
    ranks = {n: learning.cost_rank(n, str(repo)) for n in ("local", "luna", "sonnet", "opus", "pricey-mini")}
    assert ranks == {"local": 0, "luna": 1, "sonnet": 2, "opus": 3, "pricey-mini": 3}


# -- usage in dollars --------------------------------------------------------------------------------


def test_usage_cost():
    u = usage.Usage(input_tokens=1000, output_tokens=2000, cache_read_tokens=100_000,
                    cache_creation_tokens=10_000, model="claude-sonnet-5-5-20260901")
    assert usage.usage_cost(u) == pytest.approx((1000 * 2 + 2000 * 10 + 100_000 * 0.2 + 10_000 * 2.5) / 1e6)
    assert usage.usage_cost(usage.Usage(5, 5, model="mystery")) is None
    assert usage.usage_cost(None) is None
    line = usage.summary_line(u, usage.usage_cost(u))
    assert line.endswith("· sonnet · ~$0.07")


def test_a_free_model_reads_free_in_the_result_line():
    u = usage.Usage(5000, 500, model="qwen3-coder:30b")
    assert usage.usage_cost(u, fallback=pricing.FREE) == 0.0
    assert usage.summary_line(u, 0.0, free=True).endswith("· qwen3-coder · Free")


# -- Codex rollouts ----------------------------------------------------------------------------------


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def rollout(home, session, cwd, started, events, model="gpt-6-luna"):
    day = home / "sessions" / time.strftime("%Y/%m/%d", time.localtime(started))
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', time.localtime(started))}-{session}.jsonl"
    lines = [{"type": "session_meta", "payload": {"id": session, "timestamp": iso(started), "cwd": str(cwd)}},
             {"type": "turn_context", "payload": {"model": model, "cwd": str(cwd)}}]
    for inp, cached, out in events:
        lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                  "cache_write_input_tokens": 0, "output_tokens": out,
                                  "reasoning_output_tokens": 0, "total_tokens": inp + out}}}})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def codex_worker(db, repo, session_ref=None, created=None):
    from brindle import workspaces

    ws = workspaces.create(db, str(repo), "feat-codex").workspace
    a = Agent("cx", ws.id, "developer-codex", "codex", None, "assign", "processing", "", None,
              created or time.time() - 60, session_ref=session_ref)
    db.add_agent(a)
    return a, ws


def test_codex_usage_is_the_rollouts_latest_total(db, repo, tmp_path):
    home = tmp_path / "codex-home"
    a, ws = codex_worker(db, repo)
    path = rollout(home, "01abc", ws.path, a.created_at + 1,
                   [(17075, 6912, 244), (35344, 23040, 290)])
    u = usage.agent_usage(db, a)
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.model) == (35344 - 23040, 23040, 290,
                                                                               "gpt-6-luna")
    assert usage.usage_cost(u) == pytest.approx((12304 * 0.10 + 23040 * 0.01 + 290 * 0.50) / 1e6)
    # more events: the cache picks up from where it stopped, and totals are replaced, not added
    with path.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 50000, "cached_input_tokens": 40000,
                                  "output_tokens": 400}}}}) + "\n")
    u = usage.agent_usage(db, a)
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (10000, 40000, 400)
    assert db.get_usage_cache(str(path))["size"] == path.stat().st_size


def test_codex_cache_writes_are_charged_at_the_write_rate(db, repo, tmp_path):
    home = tmp_path / "codex-home"
    a, ws = codex_worker(db, repo)
    path = rollout(home, "01w", ws.path, a.created_at + 1, [])
    with path.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 1_000_000, "cached_input_tokens": 0,
                                  "cache_write_input_tokens": 400_000, "output_tokens": 0}}}}) + "\n")
    u = usage.agent_usage(db, a)
    assert (u.input_tokens, u.cache_creation_tokens) == (600_000, 400_000)
    assert usage.usage_cost(u) == pytest.approx(600_000 * 0.10 / 1e6 + 400_000 * 0.125 / 1e6)


def test_codex_rollout_by_session_ref_beats_cwd(db, repo, tmp_path):
    home = tmp_path / "codex-home"
    a, ws = codex_worker(db, repo, session_ref="01own")
    rollout(home, "01other", ws.path, a.created_at + 1, [(100, 0, 1)])
    rollout(home, "01own", ws.path, a.created_at + 30, [(500, 0, 5)])
    assert usage.agent_usage(db, a).input_tokens == 500


def test_codex_rollout_by_cwd_skips_older_and_other_sessions(db, repo, tmp_path):
    home = tmp_path / "codex-home"
    a, ws = codex_worker(db, repo)
    rollout(home, "01before", ws.path, a.created_at - 600, [(1, 0, 1)])           # an earlier agent's
    rollout(home, "01elsewhere", tmp_path, a.created_at + 1, [(2, 0, 1)])        # another directory
    rollout(home, "01mine", ws.path, a.created_at + 2, [(300, 0, 3)])
    rollout(home, "01reviewer", ws.path, a.created_at + 90, [(4, 0, 1)])         # a later session there
    assert usage.agent_usage(db, a).input_tokens == 300


def test_codex_usage_unknown_without_a_rollout(db, repo):
    a, _ = codex_worker(db, repo)
    assert usage.agent_usage(db, a) is None


def test_codex_notify_records_the_thread_as_session_ref(db, repo, monkeypatch):
    from brindle import quota

    monkeypatch.setattr(quota, "refresh_codex", lambda: False)
    a, _ = codex_worker(db, repo)
    agents.handle_hook(db, a.id, "codex-notify", {"type": "agent-turn-complete", "thread-id": "01t"})
    assert db.get_agent(a.id).session_ref == "01t"


# -- savings in dollars ------------------------------------------------------------------------------


def decide(db, root, n, *, profile, baseline, model, input):
    db.add_routing_decision(root, task_id=f"t{n}", agent_id=f"w{n}", weight="medium",
                            baseline_profile=baseline, profile=profile, learned=True,
                            ts=time.time())
    db.note_routing_outcome(f"w{n}", outcome="merged")
    db.add_history(root, "worker_result", agent_id=f"w{n}", profile=profile,
                   tokens=tokens(model, input=input))


def test_savings_are_in_dollars_when_every_task_can_be_priced(db, repo):
    profile(repo, "luna", provider="codex", model="gpt-6-luna")
    profile(repo, "sonnet", model="claude-sonnet-5-5")
    for n in range(5):
        decide(db, str(repo), n, profile="luna", baseline="sonnet", model="gpt-6-luna",
               input=1_000_000)
    this, _, _ = savings.report(db, str(repo))
    assert this.dollars and this.actual_cost == pytest.approx(0.5) and this.baseline_cost == pytest.approx(10)
    text = savings.describe([this, this, this], str(repo))
    assert "$0.50 against an estimated $10.00" in text and "~95% lower cost" in text
    assert savings.DOLLAR_NOTE in text and savings.ESTIMATE_NOTE not in text


def test_savings_fall_back_to_relative_cost_when_a_price_is_unknown(db, repo):
    profile(repo, "luna", provider="codex", model="gpt-6-luna")
    for n in range(5):
        decide(db, str(repo), n, profile="luna", baseline="no-model-profile", model="gpt-6-luna",
               input=1000)
    this, _, _ = savings.report(db, str(repo))
    assert not this.dollars
    assert savings.ESTIMATE_NOTE in savings.describe([this, this, this], str(repo))


# -- brindle cost ------------------------------------------------------------------------------------


@pytest.fixture
def root(repo, monkeypatch):
    monkeypatch.chdir(repo)
    return git.main_repo_root(str(repo))


def test_cost_summary_prices_each_row_and_keeps_unknown_apart(db, root, repo):
    profile(repo, "local", provider="native", model="qwen3-coder:30b",
            base_url="http://localhost:11434/v1")
    db.add_history(root, "worker_result", agent_id="a", profile="developer",
                   tokens=tokens("claude-sonnet-5-5", input=1_000_000, output=100_000))
    db.add_history(root, "worker_result", agent_id="b", profile="local", tokens=tokens(input=5000))
    db.add_history(root, "worker_result", agent_id="c", profile="developer-codex",
                   tokens=tokens("gpt-9-mystery", input=7000))
    db.add_history(root, "check", agent_id="a")
    s = cost.summary(db, root)
    assert s.total.dollars == pytest.approx(3.0) and s.total.unknown == 7000
    res = CliRunner().invoke(app, ["cost"])
    assert res.exit_code == 0, res.output
    assert "last 30 days: $3.00 (+5k tokens free) (+7k tokens unpriced)" in res.output
    assert "claude-sonnet-5-5" in res.output and "profile local" in res.output
    assert "gpt-9-mystery" in res.output and "unknown" in res.output
    assert pricing.AS_OF.isoformat() in res.output


def test_cost_summary_only_counts_the_window(db, root):
    db.add_history(root, "worker_result", agent_id="a",
                   tokens=tokens("claude-haiku-4-5", output=1_000_000))
    db.conn.execute("UPDATE history SET ts=?", (time.time() - 40 * 86400,))
    db.conn.commit()
    assert cost.summary(db, root).total.dollars == 0
    assert cost.summary(db, root, days=60).total.dollars == pytest.approx(5)


def test_cost_report_fails_closed_without_the_feature(db, root, monkeypatch):
    from brindle.pro import license

    def broken(feature):
        raise RuntimeError("no license store")

    monkeypatch.setattr(license, "has", broken)
    res = CliRunner().invoke(app, ["cost", "report"])
    assert res.exit_code == 1 and "brindle Pro" in res.output
    monkeypatch.setattr(license, "has", lambda feature: False)
    assert CliRunner().invoke(app, ["cost", "report"]).exit_code == 1


def test_cost_report(db, root, repo, monkeypatch):
    from brindle import workspaces
    from brindle.pro import license

    monkeypatch.setattr(license, "has", lambda feature: feature == "cost")
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "",
                       None, time.time()))
    db.add_autopilot("boss")
    db.update_autopilot("boss", goal="Ship the login page\nwith tests")
    db.add_task(Task("t1", root, "w1", "boss", ws.id, "developer", "do it", "assign", 1, "feat-a",
                     None, None, None, "started", time.time()))
    son = lambda **kw: tokens("claude-sonnet-5-5", **kw)  # noqa: E731
    db.add_history(root, "worker_result", agent_id="w1", branch="feat-a", profile="developer",
                   tokens=son(output=100_000))                                           # $1
    db.add_history(root, "review", agent_id="r1", branch="feat-a", profile="reviewer",
                   result="Review of feat-a (workspace w) at abc12345: CHANGES REQUESTED\n\nfix",
                   tokens=son(output=50_000))                                            # $0.50
    db.add_history(root, "review", agent_id="r2", branch="feat-a", profile="reviewer",
                   result="Review of feat-a (workspace w) at def67890: APPROVED\n\nok",
                   tokens=son(output=50_000))                                            # $0.50
    db.add_history(root, "merge", agent_id="boss", branch="feat-a", profile="supervisor",
                   tokens=son(output=100_000))                                           # $1
    db.add_history(root, "worker_result", agent_id="w9", branch="feat-b", profile="developer-codex",
                   tokens=tokens("gpt-6-luna", output=1_000_000))                        # $0.50, not merged
    db.add_history(root, "review", agent_id="r3", branch="feat-b", profile="reviewer",
                   result="Review of feat-b (workspace v) at 1234abcd: APPROVED\n\nok")
    rep = cost.report(db, root)
    assert rep.total.dollars == pytest.approx(3.5)
    assert rep.by_profile["reviewer"].dollars == pytest.approx(1)
    # w1 via its task, and the supervisor's own merge row; the reviewers' agents are gone
    assert rep.by_goal["Ship the login page"].dollars == pytest.approx(2)
    assert rep.by_goal[cost.NO_GOAL].dollars == pytest.approx(1.5)
    assert rep.merged == 1 and rep.per_merged_branch == pytest.approx(3)
    assert rep.reviews == {"developer": [1, 2], "developer-codex": [1, 1]}
    res = CliRunner().invoke(app, ["cost", "report"])
    assert res.exit_code == 0, res.output
    assert "Cost report for" in res.output and "last 30 days: $3.50" in res.output
    assert "By day" in res.output and "By profile" in res.output and "By goal" in res.output
    assert "Merged branches: 1, $3.00 each" in res.output
    assert "developer                    1/2 approved (50%)" in res.output
