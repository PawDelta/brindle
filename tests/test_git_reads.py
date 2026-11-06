from __future__ import annotations

import base64
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
_real_gh_file_check = git.gh_token_in_file     # and this
_SIGNED_IN = "Basic " + base64.b64encode(b"me:from-gh").decode()


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


class _Origin(http.server.SimpleHTTPRequestHandler):
    """A bare repo's files, anonymously (git's dumb protocol); /private wants credentials."""

    def do_GET(self):
        if not self.path.startswith("/private") or self.headers.get("Authorization") == _SIGNED_IN:
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


@pytest.mark.variant(
    "credential-dialog guard: real HTTP/SSH fetches must bypass credential helpers and askpass, "
    "and SSH must lose its controlling terminal even when launched from a PTY; Python coverage cannot see those prompts")
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


# hosts.yml as gh writes it. Only a host-level token keeps gh away from the keyring.
_USERS = "github.com:\n    users:\n        me:\n{per_user}    git_protocol: https\n{host}    user: me\n"
_FILE_TOKEN = "not-a-real-token-just-a-fixture"
_TOKEN_IN_FILE = _USERS.format(per_user=f"            oauth_token: {_FILE_TOKEN}\n",
                               host=f"    oauth_token: {_FILE_TOKEN}\n")
_TOKEN_IN_KEYCHAIN = _USERS.format(per_user="", host="")


@pytest.fixture
def gh_with_private_origin(tmp_path, asks, monkeypatch):
    """A local origin that plays github.com, and a gh that acts as the real one: without a
    token directly under the host in its file, or with an unmigrated host beside it, it goes
    to the keyring (only ever `security -h` here), whenever it runs."""
    private = tmp_path / "private"
    repo = tmp_path / "repo"
    sh(f"git init -q --bare -b main {private} && git init -q -b main {repo}", tmp_path)
    sh(f"git commit -q --allow-empty -m init && git push -q {private} main", repo)
    sh("git update-server-info", private)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Origin, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    sh(f"git remote add origin {url}/private", repo)

    hosts = tmp_path / "gh-config" / "hosts.yml"
    hosts.parent.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(hosts.parent))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh-log"
    revoked = tmp_path / "revoked"
    _script(bin_dir / "gh", f'echo "$*" >> "{log}"\n'
                            f'if ! grep -q "^    oauth_token: [a-z]" "{hosts}" || grep -q "^ghe" "{hosts}"; then\n'
                            f'  /usr/bin/security -h > /dev/null 2>&1 && echo KEYRING-REACHED >> "{log}"\nfi\n'
                            '[ "$1 $2" = "config get" ] && echo me && exit 0\n'
                            f'grep -q "^    oauth_token: [a-z]" "{hosts}" || exit 1\n'
                            '[ "$1 $2 $3" = "auth git-credential get" ] || exit 0\n'
                            f'[ -f "{revoked}" ] && pw=old || pw=from-gh\n'
                            'printf "username=me\\npassword=$pw\\n"')
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(git, "gh_token_in_file", _real_gh_file_check)
    monkeypatch.setattr(git, "gh_credential_helper", _real_gh_lookup)    # a background call must not start this one
    monkeypatch.setattr(git, "_gh_seen", {})
    yield SimpleNamespace(repo=repo, url=url, hosts=hosts, log=log, revoked=revoked, bin=bin_dir)
    server.shutdown()


@pytest.mark.parametrize("hosts, signs_in, real_sandbox, moved_before_git_starts", [
    pytest.param(_TOKEN_IN_FILE, True, False, False, id="token-in-gh-config"),
    pytest.param(_TOKEN_IN_KEYCHAIN, False, False, False, id="token-in-keychain"),
    pytest.param(_USERS.format(per_user=f"            oauth_token: {_FILE_TOKEN}\n", host=""), False, False, False, id="per-user-only",
                 marks=pytest.mark.variant("gh tries the keyring before a per-user file token")),
    pytest.param(_USERS.format(per_user="", host='    oauth_token: ""\n'), False, False, False, id="empty-host-token",
                 marks=pytest.mark.variant("an empty token falls through to the keyring")),
    pytest.param(_TOKEN_IN_FILE, False, True, True, id="token-moved-to-keychain-after-the-check"),
    pytest.param(f"{_TOKEN_IN_FILE}ghe.example.com:\n    user: me\n", True, True, False,
                 id="unmigrated-keychain-host-beside-it"),
])
def test_background_fetch_uses_signed_in_gh_for_github(tmp_path, asks, gh_with_private_origin, monkeypatch, hosts,
                                                     signs_in, real_sandbox, moved_before_git_starts):
    origin = gh_with_private_origin
    repo = origin.repo
    origin.hosts.write_text(hosts)
    if real_sandbox:
        # However gh comes to want the keyring, the real sandbox must prevent access.
        try:
            sh("/usr/bin/sandbox-exec -p '(version 1)(allow default)' /usr/bin/true", tmp_path)
        except subprocess.CalledProcessError:
            pytest.skip("no sandbox-exec here, or this run is sandboxed already")
        monkeypatch.setattr(git, "_GITHUB_HELPER", f"credential.{origin.url}.helper")
        if moved_before_git_starts:
            real_env = git._background_env

            def token_moves(cwd):
                origin.hosts.write_text(_TOKEN_IN_KEYCHAIN)  # `gh auth login` in another terminal
                return real_env(cwd)

            monkeypatch.setattr(git, "_background_env", token_moves)

        assert git.background_fetch(repo, ["--quiet", "origin", "main"]) == signs_in
        ran = origin.log.read_text()
        assert "auth git-credential get" in ran  # gh did run, and wanted the keyring
        assert "KEYRING-REACHED" not in ran and "config get" not in ran
        assert not asks.seen.exists()
        return

    # Other platforms exercise authentication without Apple's sandbox.
    monkeypatch.setattr(git, "_SANDBOX_EXEC", str(_script(origin.bin / "sandbox-exec", 'shift 2\nexec "$@"')))

    def helper_for(host_url):
        return git.background(["config", "--get-urlmatch", "credential.helper", host_url], repo).stdout.strip()

    assert ("sandbox-exec" in helper_for("https://github.com")) == signs_in
    assert ("auth git-credential" in helper_for("https://github.com")) == signs_in
    assert helper_for("https://example.com") == "" and helper_for("http://github.com") == ""    # never another host's

    # A real fetch, with the helper scoped to the local origin.
    monkeypatch.setattr(git, "_GITHUB_HELPER", f"credential.{origin.url}.helper")
    assert git.background_fetch(repo, ["--quiet", "origin", "main"]) == signs_in
    assert git.fetch_refused(repo) != signs_in
    assert not asks.seen.exists()    # the person's own helper (the keychain's) stays out of it
    if not signs_in:
        assert not origin.log.exists()   # gh isn't even started: nothing for it to answer with
        return
    assert origin.log.read_text().splitlines() == ["auth git-credential get", "auth git-credential store"]

    # gh's answer stops working: polls don't keep starting gh for it.
    origin.revoked.touch()
    assert not git.background_fetch(repo, ["--quiet", "origin", "main"]) and git.fetch_refused(repo)
    used = origin.log.read_text()
    assert not git.background_fetch(repo, ["--quiet", "origin", "main"])
    assert origin.log.read_text() == used
    monkeypatch.setattr(git, "GH_SEEN_SECONDS", 0)     # that short while is over: gh is asked afresh, and works again
    origin.revoked.unlink()
    assert git.background_fetch(repo, ["--quiet", "origin", "main"]) and not git.fetch_refused(repo)

    # gh's own order of where its config lives.
    monkeypatch.delenv("GH_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "gh").mkdir(parents=True)
    (tmp_path / "xdg" / "gh" / "hosts.yml").write_text(hosts)
    assert git.gh_token_in_file()

    # Without the sandbox (not macOS, or Apple took it away) gh isn't used at all.
    monkeypatch.setattr(git, "_GITHUB_HELPER", "credential.https://github.com.helper")
    monkeypatch.setattr(git, "_SANDBOX_EXEC", str(tmp_path / "no-sandbox-exec"))
    assert helper_for("https://github.com") == ""


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
    # brindle init stops on the typed failure (it isn't "not a repo")
    assert CliRunner().invoke(app, ["init"]).exit_code != 0
    # brindle doctor's repository discovery reports it as a failing check
    from itertools import islice

    from brindle import doctor
    groups = list(islice(doctor.stream(None, cwd=str(tmp_path)), 2))
    assert any(c.level == doctor.FAIL for c in groups[1])
    assert all(argv[0] != "git" for argv in started)
    (fake / "xcode-select").write_text("#!/bin/sh\necho /Library/Developer\n")
    spawn.ensure_git()
    assert spawn._tools_checked
