"""An agent's pane gets an allowlisted environment: a SLACK_BOT_TOKEN or
JIRA_API_TOKEN the person's shell exported (and the tmux server inherited)
never reaches it, in either agent_env_mode; what else the allowlist would
drop is only logged in report mode and dropped in enforce mode. The
person's own tmux server is never changed."""

import json
import shutil
from types import SimpleNamespace

import pytest

from brindle import agents, doctor, secrets, tmux

PERSONS = {"SLACK_BOT_TOKEN": "xoxb-secret", "JIRA_API_TOKEN": "jira-secret",
           "HTTPS_PROXY": "http://proxy.corp:3128", "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
           "MY_TOOL_HOME": "/opt/tool", "STRAY_SETTING": "x",
           # secret-looking, but where Claude Code's cloud backends find their identity
           "AWS_SHARED_CREDENTIALS_FILE": "/c/aws", "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1",
           "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": "sa@p.iam"}
CLOUD = ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
         "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT")


def run_pane(session, cwd, out, **kw):
    """Launch ``session``'s pane as an agent's and wait for it (tmux wait-for):
    what its ``env`` printed, by name."""
    t = shutil.which(tmux._bin())
    channel = f"brindle-env-{out.name}"
    tmux.new_window(session, out.name, str(cwd),
                    ["/bin/sh", "-c", f'env > "$0"; {t} wait-for -S {channel}', str(out)],
                    # a profile's env.TEAM_SLACK_TOKEN: set by the launch, not inherited
                    {"BRINDLE_AGENT_ID": "a1", "TEAM_SLACK_TOKEN": "from-profile"},
                    keep=secrets.provider_credentials("claude"), **kw)
    tmux._tmux("wait-for", channel)
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


@pytest.mark.parametrize("mode", ["report", "enforce"])
def test_an_agent_pane_never_sees_the_persons_secrets(tmp_path, monkeypatch, mode):
    repo = tmp_path / "repo"
    (repo / ".brindle").mkdir(parents=True)
    # agent_env adds MY_TOOL_*; listing SLACK_* gets no secret through.
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"agent_env": ["MY_TOOL_*", "SLACK_*"], "agent_env_mode": mode}))
    for k, v in PERSONS.items():
        monkeypatch.setenv(k, v)
        tmux._tmux("set-environment", "-g", k, v)   # the server started from the same shell
    ws = SimpleNamespace(repo_root=str(repo), tmux_session="brindle_allow")
    agent_env, enforce = agents._agent_env_policy(ws)
    tmux.ensure_session(ws.tmux_session, str(tmp_path), {})

    got = run_pane(ws.tmux_session, tmp_path, tmp_path / mode, agent_env=agent_env, enforce=enforce)
    assert not {"SLACK_BOT_TOKEN", "JIRA_API_TOKEN"} & set(got)
    assert got["HTTPS_PROXY"] == PERSONS["HTTPS_PROXY"] and got["SSH_AUTH_SOCK"] == PERSONS["SSH_AUTH_SOCK"]
    assert got["MY_TOOL_HOME"] == "/opt/tool" and got["BRINDLE_AGENT_ID"] == "a1"
    assert "TEAM_SLACK_TOKEN" not in got
    assert {k: got.get(k) for k in CLOUD} == {k: PERSONS[k] for k in CLOUD}
    assert ("STRAY_SETTING" in got) == (mode == "report")
    # The person's own server keeps their variables for their own windows.
    shown = tmux._tmux("show-environment", "-g").stdout
    assert "SLACK_BOT_TOKEN=xoxb-secret" in shown and "JIRA_API_TOKEN=jira-secret" in shown

    # The launch logs the names the allowlist drops (or would), never values; doctor shows them.
    agents._record_env_drops(SimpleNamespace(id="a1"), ws, {"BRINDLE_AGENT_ID": "a1"},
                             secrets.provider_credentials("claude"), None, (), agent_env, enforce)
    logged = secrets.env_report()["repos"][str(repo)]
    assert logged["mode"] == mode and "STRAY_SETTING" in logged["names"]
    assert not {"MY_TOOL_HOME", "HTTPS_PROXY", "SSH_AUTH_SOCK"} & set(logged["names"])
    assert secrets.dropped_for("a1") == (logged["names"] if enforce else [])
    [check] = doctor.agent_env_checks(str(repo))
    assert "STRAY_SETTING" in check.detail and check.level == (doctor.OK if enforce else doctor.WARN)
