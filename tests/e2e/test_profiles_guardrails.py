"""End to end: agent profiles and what they add (extends, rule packs, learned
rules, guardrails, secrets), from the files on disk to the command and
environment a worker is launched with. No agent runs and nothing touches the
network: tmux and the CLI's start are faked at the last step."""
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, guardrails, learned_rules, providers, rule_checks, secrets, tmux, workspaces
from brindle.db import Agent
from brindle.pro import license
from brindle.profiles import (
    ProfileError, learned_pack_path, list_profiles, load_profile, load_rule_pack,
    load_rule_packs, rules_prompt,
)


# -- fixtures (this file only) ----------------------------------------------------------------


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature in ("guardrails", "learned_rules"))
    monkeypatch.setattr(guardrails, "_warned", set())


@pytest.fixture
def agents_dir(repo):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def rules_dir(repo):
    d = repo / ".brindle" / "rules"
    d.mkdir(parents=True)
    return d


def add_worker(db, ws, profile, agent_id="w1", provider="claude", mode="assign"):
    a = Agent(agent_id, ws.id, profile, provider, "boss", mode, "idle", "@0", None, time.time())
    db.add_agent(a)
    return a


def commit(root, msg="c"):
    sh(f"git add -A && git commit -qm {msg}", Path(root))


@pytest.fixture
def launched(db, monkeypatch):
    """Launch a worker as brindle does, minus the tmux window and the CLI:
    returns a function (repo, profile) -> what the pane was started with."""
    def launch(repo, profile, provider=None, prompt="Do the thing", done_when=None):
        ws = workspaces.create(db, str(repo), f"b-{profile}".replace("/", "-")).workspace
        seen = {}

        def fake_new_window(session, name, cwd, command, env, tag=None, keep=(), allow=None, deny=()):
            seen.update(command=command, env=env, keep=set(keep), allow=allow, deny=list(deny), cwd=cwd)
            return "%1"

        monkeypatch.setattr(tmux, "new_window", fake_new_window)
        monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
        monkeypatch.setattr(tmux, "apply_theme", lambda *a, **k: None)
        monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)
        monkeypatch.setattr(providers, "trust_folder", lambda *a, **k: None)
        monkeypatch.setattr(providers.Provider, "after_launch", lambda self, target: None)
        monkeypatch.setattr(agents, "ready", lambda db, agent_id: None)
        agent = agents.spawn(db, ws, profile, prompt=prompt, provider_name=provider, mode="assign",
                             done_when=done_when)
        seen["agent"] = agent
        seen["ws"] = ws
        return seen
    return launch


def flag(argv, name):
    return argv[argv.index(name) + 1]


# -- 1. loading: .brindle/agents, ~/.brindle/agents, built-ins ------------------------------------------


def test_frontmatter_comments_blank_values_and_list_forms(repo, agents_dir):
    (agents_dir / "fm.md").write_text(
        "---\n"
        "# a whole-line comment\n"
        "name: fm\n"
        "description: Has x#y inside and a trailing comment   # dropped\n"
        "provider: claude   # claude | codex\n"
        "model:\n"
        "effort: low\n"
        "strict_mcp: yes\n"
        "setting_sources: project,local\n"
        "allowed_tools:\n"
        "  - Bash(git add:*)\n"
        "  - Bash(uv run:*, pytest:*)\n"
        "env.FOO: bar\n"
        "env.BAZ: 'q # not a comment'\n"
        "---\n"
        "Body --- with dashes.\n")
    p = load_profile("fm", str(repo))
    assert p.description == "Has x#y inside and a trailing comment"
    assert p.provider == "claude" and p.model is None and p.effort == "low"
    assert p.strict_mcp is True and p.setting_sources == ["project", "local"]
    assert p.allowed_tools == ["Bash(git add:*)", "Bash(uv run:*, pytest:*)"]
    assert p.env == {"FOO": "bar", "BAZ": "q # not a comment"}
    assert p.prompt == "Body --- with dashes."


def test_a_profile_without_a_closing_fence_is_a_profile_error(repo, agents_dir):
    (agents_dir / "broken.md").write_text("---\nname: broken\nYou forgot the closing fence.\n")
    with pytest.raises(ProfileError, match="never closed"):
        load_profile("broken", str(repo))
    # A child of it, and the listing of everything else, fail or work cleanly too.
    (agents_dir / "kid.md").write_text("---\nname: kid\nextends: broken\n---\n")
    with pytest.raises(ProfileError):
        load_profile("kid", str(repo))
    assert "developer" in {x.name for x in list_profiles(str(repo))}


# -- 3. rule packs ------------------------------------------------------------------------------------


def test_bad_pack_names_and_missing_packs(repo):
    for bad in ("../x", "a/../b", "/etc/passwd", "a b", ""):
        with pytest.raises(ProfileError):
            load_rule_pack(bad, str(repo))
    with pytest.raises(KeyError, match="no rule pack named 'nope/x'"):
        load_rule_pack("nope/x", str(repo))


def test_a_profile_naming_a_missing_pack_stops_the_launch(db, repo, agents_dir, launched):
    (agents_dir / "bad.md").write_text("---\nname: bad\nextends: developer\nrules: nonexistent/pack\n---\n")
    with pytest.raises(KeyError, match="nonexistent/pack"):
        launched(repo, "bad")


# -- 4. packs checked against a real branch ---------------------------------------------------------------


PACKS = {
    "mix": ("---\nname: mix\ndeny_deps: [pickle, left-pad]\nrequire_tests_for: [src/**/*.py]\n"
            "deny_patterns:\n  - shell=True\n  - \\beval\\(\n---\nBe careful.\n"),
}


@pytest.fixture
def mixed(db, repo, agents_dir, rules_dir):
    (rules_dir / "mix.md").write_text(PACKS["mix"])
    (agents_dir / "mixed.md").write_text("---\nname: mixed\nextends: developer\nrules: mix\n---\nMixed.\n")
    ws = workspaces.create(db, str(repo), "feat").workspace
    add_worker(db, ws, "mixed")
    return ws


def test_each_mechanical_rule_fires_with_file_and_line(db, mixed):
    root = Path(mixed.path)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("import json\n\nimport pickle\nsubprocess.run(c, shell=True)\ny = eval(s)\n")
    (root / "package.json").write_text('{\n  "dependencies": {\n    "left-pad": "1.0.0"\n  }\n}\n')
    commit(root)
    result = rule_checks.run(db, mixed)
    assert not result.ok
    by_rule = {}
    for v in result.violations:
        by_rule.setdefault(v.rule, []).append(v.detail)
    assert any(d.startswith("src/a.py:3: pickle imported") for d in by_rule["deny_deps"])
    assert any(d.startswith("package.json:3: left-pad added as a dependency") for d in by_rule["deny_deps"])
    assert any(d.startswith("src/a.py:4: matches /shell=True/") for d in by_rule["deny_patterns"])
    assert any(d.startswith("src/a.py:5: matches /\\beval\\(/") for d in by_rule["deny_patterns"])
    assert "src/a.py changed, but the diff adds or changes no test file" in by_rule["require_tests_for"][0]
    assert result.summary().startswith("FAIL `rules mix`\n- mix (deny_deps): ")


def test_a_worker_whose_profile_vanished_fails_the_gate(db, mixed, agents_dir):
    (agents_dir / "mixed.md").unlink()
    result = rule_checks.run(db, mixed)
    assert not result.ok and "can't be loaded" in result.problem
    assert result.summary().startswith("FAIL `rules`")


# -- 5. learned rules ------------------------------------------------------------------------------------


def _review(db, root, branch, summary, profile="developer"):
    db.add_history(root, "worker_result", agent_id=f"w-{branch}", branch=branch, profile=profile,
                   task="do it", result="done")
    db.add_history(root, "review", agent_id=f"r-{branch}", branch=branch, profile="reviewer",
                   result=f"Review of {branch} (workspace ws-{branch}) at abcdef12: CHANGES REQUESTED\n\n{summary}")


def _model(answer):
    def call(prompt):
        import re

        ids = [int(m) for m in re.findall(r"^\[(\d+)\]", prompt.split("New findings:", 1)[1], re.M)]
        return json.dumps({"groups": answer(ids)})
    return call


def test_a_learned_rule_flows_from_findings_to_a_failing_gate(db, repo, pro):
    root = str(repo)
    for b in ("feat/a", "feat/b", "feat/c"):
        _review(db, root, b, "bare except")
    [s] = learned_rules.refresh(db, root, _model(lambda ids: [
        {"title": "bare except", "rule": "Never use a bare except.", "findings": ids,
         "deny_patterns": [r"except\s*:"]}])).suggested
    assert [x.key for x in learned_rules.suggestions(db, root)] == [s.key]
    assert not learned_pack_path(root).exists()          # nothing changes until accepted
    learned_rules.accept(db, root, s.key)

    # Every profile in the repo, even one with no rules, now has the pack...
    assert [p.name for p in load_rule_packs(load_profile("developer", root), root)] == ["learned"]
    assert "Never use a bare except" in rules_prompt(load_rule_packs(load_profile("qa", root), root))
    # ...and it is checked on a real branch.
    ws = workspaces.create(db, root, "feat").workspace
    add_worker(db, ws, "developer")
    (Path(ws.path) / "m.py").write_text("try:\n    f()\nexcept:\n    pass\n")
    commit(ws.path)
    result = rule_checks.run(db, ws)
    assert not result.ok and result.packs == ["learned"]
    assert any("m.py:3" in v.detail for v in result.violations)


def test_learned_patterns_with_a_comment_marker_survive_the_roundtrip(db, repo, pro):
    root = str(repo)
    for b in ("feat/a", "feat/b", "feat/c"):
        _review(db, root, b, "noqa abuse")
    [s] = learned_rules.refresh(db, root, _model(lambda ids: [
        {"title": "noqa", "rule": "No blanket noqa.", "findings": ids,
         "deny_patterns": [r"\s# noqa$", r"a,b"]}])).suggested
    learned_rules.accept(db, root, s.key)
    [pack] = load_rule_packs(load_profile("developer", root), root)
    for pattern in pack.deny_patterns:
        assert pattern in (r"\s# noqa$", r"a,b")
    assert pack.deny_patterns


# -- 6. guardrails ------------------------------------------------------------------------------------


API_DEV = ("---\nname: api-dev\nextends: developer\nwrite_scope: [src/api/**, tests/api/]\n"
           "read_scope: [src/**, tests/**, docs/**]\nenv_allow: [NPM_TOKEN, AWS_*]\n---\nAPI work.\n")


@pytest.fixture
def api_dev(agents_dir):
    (agents_dir / "api-dev.md").write_text(API_DEV)


def test_scope_globs_semantics():
    assert guardrails.in_scope("src/api/x/y.py", ["src/api/**"])
    assert guardrails.in_scope("tests/api/t.py", ["tests/api/"])
    assert guardrails.in_scope("docs/a.md", ["docs"])
    assert guardrails.in_scope("a/b/c.py", ["**/*.py"]) and guardrails.in_scope("c.py", ["**/*.py"])
    assert not guardrails.in_scope("SRC/api/x.py", ["src/api/**"])           # case counts
    assert not guardrails.in_scope("src/apix/x.py", ["src/api"])             # a directory, not a prefix
    assert not guardrails.in_scope("/etc/x.md", ["*.md"])                     # relative globs stay in the worktree
    assert guardrails.in_scope("./src/api/a.py", ["./src/api/"])


def test_the_pretool_hook_denies_out_of_scope_tools(db, repo, api_dev, pro):
    ws = workspaces.create(db, str(repo), "feat").workspace
    agent = add_worker(db, ws, "api-dev")
    root = ws.path

    def decide(tool, **tool_input):
        return agents.pre_tool_decision(db, agent, {"tool_name": tool, "tool_input": tool_input})

    def denied(d):
        return d is not None and "deny" in json.dumps(d)

    assert not denied(decide("Edit", file_path=f"{root}/src/api/a.py"))
    assert denied(decide("Edit", file_path=f"{root}/src/core.py"))
    assert denied(decide("Write", file_path="/etc/passwd"))
    assert denied(decide("Edit", file_path=f"{root}/src/api/../core.py"))
    assert not denied(decide("Read", file_path=f"{root}/docs/a.md"))
    assert denied(decide("Read", file_path=f"{root}/.env"))
    assert denied(decide("Grep", pattern="x"))                           # the worktree root isn't wholly in scope
    assert not denied(decide("Grep", pattern="x", path=f"{root}/src"))
    assert denied(decide("Glob", pattern="../*", path=f"{root}/src"))


def test_secrets_never_reach_a_pane_whatever_env_allow_says():
    inherited = ["PATH", "HOME", "BRINDLE_HOME", "LC_ALL", "NPM_TOKEN", "AWS_REGION", "DATABASE_URL",
                 "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "BRINDLE_PRO_TOKEN", "CI_JOB_TOKEN",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HTTPS_PROXY", "GIT_AUTHOR_NAME"]
    keep = secrets.provider_credentials("claude", "MY_KEY")
    unset = set(secrets.pane_unset(inherited, keep=keep, allow=["NPM_TOKEN", "AWS_*", "GITHUB_TOKEN", "GH_*",
                                                                   "BRINDLE_PRO_TOKEN"]))
    for secret in ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "BRINDLE_PRO_TOKEN", "CI_JOB_TOKEN"):
        assert secret in unset, secret
    assert {"DATABASE_URL", "OPENAI_API_KEY", "HTTPS_PROXY", "GIT_AUTHOR_NAME"} <= unset
    for kept in ("PATH", "HOME", "BRINDLE_HOME", "LC_ALL", "NPM_TOKEN", "AWS_REGION", "ANTHROPIC_API_KEY"):
        assert kept not in unset, kept
    # A profile cannot claim a secret as its api_key_env or env name.
    assert "GITHUB_TOKEN" not in secrets.provider_credentials("native", "GITHUB_TOKEN")
    assert "GITHUB_TOKEN" in secrets.pane_unset(["GITHUB_TOKEN"], keep=["GITHUB_TOKEN"])
    # Every secret is listed even when the pane doesn't have it set.
    assert set(secrets.SECRET_ENV) <= set(secrets.pane_unset([]))


def test_scrub_secrets_removes_secrets_and_empty_keys():
    env = {"GITHUB_TOKEN": "x", "GH_FOO": "y", "PATH": "/bin", "ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "k"}
    removed = secrets.scrub_secrets(env)
    assert set(removed) >= {"GITHUB_TOKEN", "GH_FOO", "ANTHROPIC_API_KEY"}
    assert env == {"PATH": "/bin", "OPENAI_API_KEY": "k"}


# -- 7. what a worker is launched with --------------------------------------------------------------------


def test_a_launched_worker_gets_the_composed_prompt_flags_and_env(repo, agents_dir, rules_dir, launched, pro):
    (rules_dir / "house.md").write_text("---\nname: house\ndeny_patterns: [FIXME]\n---\nHouse rules apply.\n")
    (agents_dir / "api-dev.md").write_text(
        "---\nname: api-dev\nextends: developer\nmodel: opus\neffort: low\nstrict_mcp: true\n"
        "setting_sources: project,local\npermission_mode: acceptEdits\nrules: [house, security/backend]\n"
        "write_scope: [src/api/**]\nread_scope: [src/**]\nenv_allow: [NPM_TOKEN]\n"
        "env.ANTHROPIC_BASE_URL: http://localhost:4000\nenv.MY_FLAG: on\n---\nAPI work only.\n")
    seen = launched(repo, "api-dev", done_when="pytest passes")
    argv = seen["command"]
    # argv is wrapped to pause the session when the CLI exits: the CLI's own args follow "brindle-agent".
    inner = argv[argv.index("brindle-agent") + 1:] if "brindle-agent" in argv else argv
    system = flag(inner, "--append-system-prompt")
    base = load_profile("developer", str(repo)).prompt
    assert system.startswith(base.strip())
    for expected in ("API work only.", "House rules apply.", "## Rules: house", "## Rules: security/backend",
                     "Never build a shell", "Write scope", "`src/api/**`", "Read scope", "`src/**`"):
        assert expected in system, expected
    assert system.index("API work only.") < system.index("House rules apply.") < system.index("Write scope")
    assert flag(inner, "--model") == "opus"
    assert flag(inner, "--effort") == "low"
    assert flag(inner, "--permission-mode") == "acceptEdits"
    assert "--strict-mcp-config" in inner and flag(inner, "--setting-sources") == "project,local"
    allowed = flag(inner, "--allowedTools").split(",")
    assert "mcp__brindle" in allowed and any(t.startswith("Bash(git") for t in allowed)
    settings = json.loads(flag(inner, "--settings"))
    # The hook sees Bash and the tools the write and read scopes guard (plus whatever the permission policy adds).
    hooked = set(settings["hooks"]["PreToolUse"][0]["matcher"].split("|"))
    assert {"Bash", "Edit", "Write", "NotebookEdit", "MultiEdit", "Read", "Glob", "Grep"} <= hooked
    # The task, with its finish line, is the last argument.
    assert inner[-1].count("Do the thing") == 1 and "pytest passes" in inner[-1]
    # Environment: the profile's env lines, brindle's own variables; env_allow handed to the pane.
    env = seen["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000" and env["MY_FLAG"] == "on"
    assert env["BRINDLE_AGENT_ID"] == seen["agent"].id
    assert seen["allow"] == ["NPM_TOKEN"]
    assert {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL"} <= seen["keep"]


def test_the_real_new_window_scrubs_secrets_and_applies_env_allow(repo, monkeypatch):
    monkeypatch.setattr(tmux, "inherited_names", lambda session: [
        "PATH", "NPM_TOKEN", "DATABASE_URL", "GITHUB_TOKEN", "BRINDLE_PRO_TOKEN", "ANTHROPIC_API_KEY"])
    real = {}

    def spy(command, unset):
        real["unset"] = set(unset)
        return command

    monkeypatch.setattr(tmux, "scrubbed", spy)
    monkeypatch.setattr(tmux, "_tmux", lambda *a, **k: type("R", (), {"stdout": "%1\n", "returncode": 0})())
    monkeypatch.setattr(tmux, "set_pane_tag", lambda *a, **k: None)
    tmux.new_window("s", "n", str(repo), ["x"], {}, keep=secrets.provider_credentials("claude"),
                    allow=["NPM_TOKEN"])
    assert {"DATABASE_URL", "GITHUB_TOKEN", "BRINDLE_PRO_TOKEN"} <= real["unset"]
    assert not ({"PATH", "NPM_TOKEN", "ANTHROPIC_API_KEY"} & real["unset"])


def test_a_codex_worker_with_rules_gets_them_in_its_prompt(repo, agents_dir, launched, pro, monkeypatch):
    (agents_dir / "cx.md").write_text(
        "---\nname: cx\nprovider: codex\nrules: security/backend\nwrite_scope: [src/**]\n---\nCodex dev.\n")
    monkeypatch.setattr(providers, "which_cli", lambda *a, **k: "/usr/bin/codex", raising=False)
    try:
        seen = launched(repo, "cx")
    except Exception as e:  # noqa: BLE001 - the CLI isn't installed here; the prompt is what's under test
        pytest.skip(f"codex launch needs the CLI: {e}")
    text = " ".join(seen["command"])
    assert "Rules: security/backend" in text and "Write scope" in text


def test_a_subagent_profile_with_rules_gets_them_in_the_brief(db, repo, agents_dir, rules_dir, pro):
    (agents_dir / "sub.md").write_text(
        "---\nname: sub\nextends: subagent\nrules: tests/only\nwrite_scope: [tests/**]\n---\nSub.\n")
    ws = workspaces.create(db, str(repo), "feat").workspace
    agent = agents.spawn(db, ws, "sub", prompt="Write tests", mode="assign")
    brief = agent.task
    assert "Rules: tests/only" in brief and "Write scope" in brief and "Write tests" in brief
