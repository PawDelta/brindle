import re
import subprocess

from typer.testing import CliRunner

from brindle import ci, ci_providers, cli


def _job(text, name):
    jobs = text[text.index("\njobs:\n"):]
    start = jobs.index(f"\n  {name}:\n")
    nxt = [m.start() for m in re.finditer(r"(?m)^  \w+:\n", jobs) if m.start() > start + 1]
    return jobs[start:nxt[0] if nxt else len(jobs)]


def _steps(job):
    return job.split("\n      - ")[1:]


def _step(job, name):
    return next(s for s in _steps(job) if f"name: {name}\n" in s)


def _env_keys(step):
    return set(re.findall(r"(?m)^          (\w+): \$\{\{ secrets\.\w+ \}\}$", step))


def test_claude_code_is_always_installed():
    run_job = _job(ci.workflow_text(), "run")
    step = _step(run_job, "Install Claude Code")
    assert "npm install -g @anthropic-ai/claude-code" in step
    assert "if:" not in step and "secrets." not in step


def test_codex_is_installed_only_when_its_key_is_a_secret():
    run_job = _job(ci.workflow_text(), "run")
    detect = _step(run_job, "Which agent CLIs to install")
    assert "id: providers" in detect
    assert _env_keys(detect) == {"OPENAI_API_KEY", "CODEX_API_KEY"}
    when = "if: steps.providers.outputs.codex == 'true'"
    install = _step(run_job, "Install Codex")
    assert when in install and "npm install -g @openai/codex" in install
    # npm's install scripts never see the keys.
    assert "env:" not in install and "API_KEY" not in install
    login = _step(run_job, "Sign in to Codex")
    assert when in login and "npm" not in login
    assert _env_keys(login) == {"OPENAI_API_KEY", "CODEX_API_KEY"}
    # The key goes in on stdin, never on the command line.
    assert "| codex login --with-api-key" in login
    # Detection, install, sign-in, all before brindle starts agents.
    assert (run_job.index("Which agent CLIs") < run_job.index("Install Codex")
            < run_job.index("Sign in to Codex") < run_job.index("brindle ci run --issue"))


def test_the_stored_codex_key_is_called_out():
    text = ci.workflow_text()
    assert "~/.codex/auth.json" in text and "scoped to CI" in text
    assert "~/.codex/auth.json" in ci_providers.__doc__ and "scoped to CI" in ci_providers.__doc__


def test_keys_go_only_where_they_are_needed():
    text = ci.workflow_text()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        assert f"secrets.{key}" not in _job(text, "entitle")
        assert f"secrets.{key}" not in _job(text, "publish")
        # Detection and sign-in only: not the install, nor the step that runs the agents.
        assert text.count(f"secrets.{key}") == 2
    run_job = _job(text, "run")
    assert "OPENAI_API_KEY" not in _step(run_job, "Run brindle")
    assert "ANTHROPIC_API_KEY" not in _step(run_job, "Install Claude Code")
    assert "GEMINI_API_KEY" not in text and "agy" not in text   # Antigravity isn't installed


def _detect_script():
    detect = _step(_job(ci.workflow_text(), "run"), "Which agent CLIs to install")
    body = detect.split("run: |\n", 1)[1]
    return "\n".join(line[10:] for line in body.splitlines())


def _detect(tmp_path, **env):
    out = tmp_path / "out"
    out.write_text("")
    res = subprocess.run(["bash", "-c", _detect_script()], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(out), **env})
    return res, out.read_text()


def test_a_missing_secret_skips_codex_cleanly(tmp_path):
    # GitHub hands a missing secret over as an empty string.
    res, outputs = _detect(tmp_path, OPENAI_API_KEY="", CODEX_API_KEY="")
    assert res.returncode == 0 and outputs == ""
    assert "skipping Codex" in res.stdout


def test_either_key_installs_codex_without_echoing_it(tmp_path):
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        res, outputs = _detect(tmp_path, **{key: "sk-secret-value"})
        assert res.returncode == 0 and outputs.strip() == "codex=true"
        assert "sk-secret-value" not in res.stdout + res.stderr + outputs


def test_the_template_still_formats_and_no_braces_leak():
    text = ci.workflow_text("my label")
    assert "{providers}" not in text and "${{ secrets.OPENAI_API_KEY }}" in text
    assert "${{{{" not in text


# -- doctor -------------------------------------------------------------------------

SECRET = "sk-do-not-print-me-123"


def _which(installed):
    return lambda name: f"/usr/bin/{name}" if name in installed else None


def _doctor(tmp_path, env, installed=("claude",), entitlement=None):
    lines = []
    env = {"HOME": str(tmp_path), **env}
    code = ci_providers.doctor(entitlement, environ=env, which=_which(installed), echo=lines.append)
    return code, "\n".join(lines)


def test_doctor_reports_clis_and_keys_without_values(tmp_path):
    env = {"ANTHROPIC_API_KEY": SECRET, "OPENAI_API_KEY": SECRET + "o", "BRINDLE_PRO_TOKEN": "cpc_" + SECRET}
    code, out = _doctor(tmp_path, env, installed=("claude", "codex"))
    assert code == 0
    assert SECRET not in out and "cpc_" not in out
    assert re.search(r"Claude Code\s+installed \(/usr/bin/claude\); ANTHROPIC_API_KEY set", out)
    assert re.search(r"Codex\s+installed \(/usr/bin/codex\); OPENAI_API_KEY set", out)
    assert re.search(r"Antigravity\s+not installed; no GEMINI_API_KEY", out)
    assert "BRINDLE_PRO_TOKEN  set" in out
    assert "routing can pick: Claude Code, Codex" in out


def test_doctor_sees_codex_stored_login(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "%s"}' % SECRET)
    code, out = _doctor(tmp_path, {}, installed=("codex",))
    assert code == 0 and "stored login" in out and SECRET not in out
    assert "routing can pick: Codex" in out


def test_doctor_finds_the_workflow_entitlement(tmp_path):
    ent = tmp_path / "brindle-entitlement" / "entitlement.jwt"
    code, out = _doctor(tmp_path, {"RUNNER_TEMP": str(tmp_path), "ANTHROPIC_API_KEY": SECRET})
    assert "entitlement  missing" in out and "BRINDLE_PRO_TOKEN  not set" in out
    ent.parent.mkdir()
    ent.write_text("eyJ.secret.jwt")
    code, out = _doctor(tmp_path, {"RUNNER_TEMP": str(tmp_path), "ANTHROPIC_API_KEY": SECRET})
    assert code == 0 and f"entitlement  present ({ent})" in out and "eyJ" not in out


def test_doctor_fails_when_nothing_can_run(tmp_path):
    code, out = _doctor(tmp_path, {"OPENAI_API_KEY": SECRET}, installed=("claude",))
    assert code == 1 and "no agent CLI can run here" in out and SECRET not in out
    assert "no file given" in out


def test_ci_doctor_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setattr(ci_providers.shutil, "which", _which(("claude",)))
    res = CliRunner().invoke(cli.app, ["ci", "doctor", "--entitlement", str(tmp_path / "e.jwt")])
    assert res.exit_code == 0, res.output
    assert "ANTHROPIC_API_KEY set" in res.output and SECRET not in res.output
    assert "entitlement  missing" in res.output
    help_text = CliRunner().invoke(cli.app, ["ci", "doctor", "--help"]).output
    assert "|| true" in help_text
