from __future__ import annotations

import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import sh

from brindle import agents, config, disk, git, session_sync, spawn, workspaces

_real_gh_lookup = git.gh_credential_helper     # conftest's gh_sign_in_unknown replaces it for each test


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


class _Origin(http.server.SimpleHTTPRequestHandler):
    """A bare repo's files, anonymously (git's dumb protocol); /private wants credentials."""

    def do_GET(self):
        if not self.path.startswith("/private"):
            return super().do_GET()
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="origin"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def asks(tmp_path, monkeypatch):
    """Everything that could raise a dialog is a fake that notes being asked;
    the real keychain helper (git's system config) is out of reach."""
    seen = tmp_path / "asked"
    asker = _script(tmp_path / "asker", f'echo "$*" >> "{seen}"')
    home_config = tmp_path / "gitconfig"
    home_config.write_text(f'[credential]\n\thelper = !{asker}\n[core]\n\taskPass = {asker}\n'
                           f'[user]\n\tname = n\n\temail = a@b.c\n')
    for name, value in (("GIT_CONFIG_GLOBAL", str(home_config)), ("GIT_CONFIG_NOSYSTEM", "1"),
                        ("GIT_ASKPASS", str(asker)), ("SSH_ASKPASS", str(asker)),
                        ("GIT_TERMINAL_PROMPT", "0"), ("NO_PROXY", "127.0.0.1")):
        monkeypatch.setenv(name, value)
    return SimpleNamespace(seen=seen, config=home_config, asker=asker)


def test_background_fetch_never_asks_for_credentials(tmp_path, asks, monkeypatch):
    public = tmp_path / "public"
    repo = tmp_path / "repo"
    sh(f"git init -q --bare -b main {public} && git init -q -b main {repo}", tmp_path)
    sh(f"git commit -q --allow-empty -m init && git branch feat && git remote add origin {public}"
       " && git push -q origin main feat && git fetch -q origin && git remote set-head origin main"
       " && git config branch.feat.remote origin && git config branch.feat.merge refs/heads/feat", repo)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                             functools.partial(_Origin, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    # As `gh auth setup-git` writes it: a helper for that host only.
    asks.config.write_text(f'{asks.config.read_text()}[credential "{url}"]\n\thelper = !{asks.asker}\n')
    last_fetched = sh("git rev-parse origin/main", repo)
    config.set_local(str(repo), "delete_pushed_branches", True)
    sh(f"git remote set-url origin {url}/private", repo)

    git.merged_into_default(repo, "feat")
    assert not git.upstream_gone(repo, "feat")
    assert git.fast_forward(repo, "main") is None
    assert git.resolve_start_point(repo, "main", True) == "main"
    git.sync(repo, "main")
    assert not session_sync.fetch(str(repo), "main")
    assert disk.delete_pushed_remote(SimpleNamespace(repo_root=str(repo), branch="feat"), last_fetched) is None
    assert not asks.seen.exists()

    # Refused, not offline: a new session starts from what was last fetched, and it is on record.
    assert git.fetch_refused(repo)
    assert session_sync.fresh_start(str(repo)) == last_fetched
    # The person asked for that branch: normal credentials, the same helper asked.
    with pytest.raises(git.GitError):
        git.fetch_remote_branch(repo, "main")
    assert "get" in asks.seen.read_text()

    # Public HTTP and SSH refresh without any helper. The person's own ssh command is kept, and
    # BatchMode goes right after ssh itself, as ssh keeps the first value of an option.
    ssh_log = tmp_path / "ssh-log"
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    fake_body = (f'( : < /dev/tty ) 2>/dev/null && echo HAS-A-TERMINAL >> "{ssh_log}"\n'
                 f'echo "$@" >> "{ssh_log}"\nfor last; do :; done\neval "$last"')
    fake_ssh, wrapper, plink = (_script(fakes / name, fake_body) for name in ("ssh", "wrapper", "plink"))
    alias = fakes / "alias"
    alias.symlink_to(fake_ssh)
    ssh_url = f"ssh://example.invalid{public}"
    for origin, ssh_command, bare_ssh, expected in (
            (f"{url}/public", None, None, None),
            (ssh_url, f"{fake_ssh} -o BatchMode=no", None, "-o BatchMode=yes -o BatchMode=no"),
            (ssh_url, f"{alias} -F /dev/null -o BatchMode=no", None, "-o BatchMode=yes -F /dev/null -o BatchMode=no"),
            (ssh_url, f"/usr/bin/env PROBE=1 {fake_ssh} -F /dev/null", None, "-o BatchMode=yes -F /dev/null"),
            (ssh_url, f"{wrapper} --flag", None, "--flag -o BatchMode=yes"),
            (ssh_url, str(plink), None, ""),
            (ssh_url, None, str(fake_ssh), "-o BatchMode=yes")):
        sh(f"git remote set-url origin {origin}", repo)
        new = sh(f"git commit -q --allow-empty -m next && git push -q {public} HEAD:refs/heads/main"
                 " && git rev-parse HEAD", repo)
        sh("git update-server-info", public)
        sh("git config --unset-all core.sshCommand || true", repo)
        if ssh_command:
            git.run(["config", "core.sshCommand", ssh_command], repo)
        if bare_ssh:
            monkeypatch.setenv("GIT_SSH", bare_ssh)
        ssh_log.write_text("")
        assert session_sync.fetch(str(repo), "main")
        assert sh("git rev-parse origin/main", repo) == new
        assert not git.fetch_refused(repo)
        if expected:
            assert expected in ssh_log.read_text()
        elif expected == "":    # takes no -o
            assert "BatchMode" not in ssh_log.read_text()
    server.shutdown()

    # Started from a terminal, whatever ssh runs has none to ask at (under any name, BatchMode or not).
    inner = tmp_path / "from_a_terminal.py"
    inner.write_text('import sys\nfrom brindle import git\nopen("/dev/tty").close()    # this run has a terminal\n'
                     'sys.exit(0 if git.background_fetch(sys.argv[1], ["--quiet", "origin", "main"]) else 3)\n')
    in_a_pty = f'import os, pty, sys; sys.exit(os.waitstatus_to_exitcode(pty.spawn([sys.executable, "{inner}", "{repo}"])))'
    ssh_log.write_text("")
    sh(f"{sys.executable} -c '{in_a_pty}' < /dev/null", tmp_path)
    assert "BatchMode=yes" in ssh_log.read_text() and "HAS-A-TERMINAL" not in ssh_log.read_text()


def test_agent_panes_sign_in_to_github_through_gh_without_the_keychain_helper(db, tmp_path, asks, monkeypatch):
    repo = tmp_path / "repo"
    sh(f"git init -q -b main {repo} && git -C {repo} commit -q --allow-empty -m init", tmp_path)
    ws = workspaces.adopt_root(db, str(repo))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    signed_in = tmp_path / "signed-in"
    _script(bin_dir / "gh", f'case "$1 $2" in\n"config get") [ -f "{signed_in}" ] && echo me ;;\n'
                            '"auth git-credential") [ "$3" = get ] && printf "username=me\\npassword=from-gh\\n" ;;\nesac')
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(agents, "_KEYCHAIN_HELPER_PLATFORM", True)
    monkeypatch.setattr(git, "gh_credential_helper", _real_gh_lookup)
    monkeypatch.setattr(git, "_gh_seen", {})
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    assert "GIT_CONFIG_PARAMETERS" not in agents.agent_env(ws, "a")    # gh there, not signed in: today's helpers

    signed_in.touch()
    monkeypatch.setattr(git, "_gh_seen", {})
    env = {**os.environ, **agents.agent_env(ws, "a")}
    filled = git.run_env(["credential", "fill"], repo, env, input="protocol=https\nhost=github.com\n\n").stdout
    assert "password=from-gh" in filled
    git.run_env(["credential", "approve"], repo, env, input=filled)
    assert not asks.seen.exists()      # neither read from nor stored in the helper that would be the keychain's
    git.run_env(["credential", "fill"], repo, env, input="protocol=https\nhost=example.com\n\n", check=False)
    assert "get" in asks.seen.read_text()   # another host keeps the person's own helpers

    # gh signed in says nothing of a locked keychain behind its token: a background call has no helper at all.
    effective = git.background(["config", "--get-urlmatch", "credential.helper", "https://github.com"], repo)
    assert effective.stdout.strip() == ""


def test_branch_exists_is_cached(tmp_path, monkeypatch):
    sh("git init -q -b main && git config user.email a@b.c && git config user.name n"
       " && git commit -q --allow-empty -m init", tmp_path)
    calls: list[list[str]] = []
    real = spawn.read

    def counting(argv, **kw):
        calls.append(argv)
        return real(argv, **kw)

    monkeypatch.setattr(spawn, "read", counting)

    assert git.branch_exists(tmp_path, "main")
    assert not git.branch_exists(tmp_path, "nope")
    assert git.branch_exists(tmp_path, "main")
    assert len(calls) == 1

    git.run(["branch", "feat"], tmp_path)           # brindle creates: seen at once
    assert git.branch_exists(tmp_path, "feat")
    git.run(["branch", "-D", "feat"], tmp_path)     # brindle deletes: seen at once
    assert not git.branch_exists(tmp_path, "feat")

    # A real git.run branch create landing after a lookup took its snapshot but
    # before it published: the stale listing is discarded, the lookup re-reads,
    # and only the fresh listing is cached. spawn is faked; git.run is real.
    names = {"main"}
    listings: list[int] = []
    snapshot_taken, mutation_done = threading.Event(), threading.Event()

    def fake_read(argv, **kw):
        assert argv[:2] == ["git", "for-each-ref"]
        listings.append(1)
        seen = sorted(names)    # the snapshot, taken before any mutation
        if len(listings) == 1:
            snapshot_taken.set()
            assert mutation_done.wait(5)
        return subprocess.CompletedProcess(argv, 0, "".join(f"refs/heads/{n}\n" for n in seen), "")

    def fake_action(argv, **kw):
        assert argv == ["git", "branch", "feat"]
        names.add("feat")
        return subprocess.CompletedProcess(argv, 0, "", "")

    git.invalidate_branches()
    monkeypatch.setattr(spawn, "read", fake_read)
    monkeypatch.setattr(spawn, "action", fake_action)
    seen_by_lookup: list[bool] = []
    lookup = threading.Thread(target=lambda: seen_by_lookup.append(git.branch_exists(tmp_path, "feat")))
    lookup.start()
    assert snapshot_taken.wait(5)
    git.run(["branch", "feat"], tmp_path)
    mutation_done.set()
    lookup.join()
    assert seen_by_lookup == [True]
    assert git.branch_exists(tmp_path, "feat")
    assert len(listings) == 2


def test_missing_developer_tools_is_explained_before_git_runs(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brindle import clone
    from brindle.cli import app
    from brindle.service.errors import from_exception

    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "xcode-select").write_text("#!/bin/sh\nexit 2\n")
    (fake / "xcode-select").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setattr(spawn.sys, "platform", "darwin")
    real_which = shutil.which
    monkeypatch.setattr(spawn.shutil, "which", lambda n, *a, **k: "/usr/bin/git" if n == "git" else real_which(n, *a, **k))
    monkeypatch.setattr(spawn, "_tools_checked", False)
    monkeypatch.chdir(tmp_path)
    popen = subprocess.Popen
    started: list[list[str]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, *a, **k: (started.append(argv), popen(argv, *a, **k))[1])
    # git.run: typed, mapped to its own API code (not "conflict"), before git starts
    with pytest.raises(git.GitToolsMissing, match=r"xcode-select --install") as caught:
        git.run(["status"], tmp_path)
    assert from_exception(caught.value).code == "developer_tools_missing"
    # the clone job starts git itself, and the daemon maps the same way
    with pytest.raises(spawn.ToolsMissing) as cloned:
        clone.clone("https://example.com/x.git", tmp_path / "x", lambda line: None)
    assert from_exception(cloned.value).code == "developer_tools_missing"
    # brindle init keeps the install guidance instead of suggesting `git init`
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code != 0 and "xcode-select --install" in result.output and "git init" not in result.output
    assert all(argv[0] != "git" for argv in started)
    (fake / "xcode-select").write_text("#!/bin/sh\necho /Library/Developer\n")
    spawn.ensure_git()
    assert spawn._tools_checked
