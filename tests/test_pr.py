
import pytest
from typer.testing import CliRunner

from brindle import workspaces
from brindle.cli import app

from conftest import sh

runner = CliRunner()


@pytest.fixture
def pr_repo(repo, tmp_path):
    """``repo`` plus a ``release`` branch and a PR head branch ``fix/typo``
    (cut from release) that exist only on origin."""
    other = tmp_path / "other"
    sh(f"git clone -q {tmp_path / 'origin.git'} {other}", tmp_path)
    sh("git switch -qc release && git push -q origin release", other)
    sh("git switch -qc fix/typo", other)
    (other / "fix.txt").write_text("fixed\n")
    sh("git add -A && git commit -qm fix && git push -q origin fix/typo", other)
    return repo


def fake_gh(monkeypatch, head="fix/typo", base="release"):
    calls = []

    def view(repo_path, number):
        calls.append(number)
        return {"headRefName": head, "baseRefName": base}

    monkeypatch.setattr(workspaces, "gh_pr_view", view)
    return calls


def test_create_from_pr_missing_remote_branch(db, pr_repo, monkeypatch):
    fake_gh(monkeypatch, head="nope")
    with pytest.raises(workspaces.WorkspaceError, match="couldn't fetch PR #7"):
        workspaces.create_from_pr(db, str(pr_repo), 7)


def test_gh_failures_are_clear(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    with pytest.raises(workspaces.WorkspaceError, match="not installed"):
        workspaces.gh_pr_view(str(tmp_path), 1)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho 'no pull requests found' >&2\nexit 1\n")
    gh.chmod(0o755)
    with pytest.raises(workspaces.WorkspaceError, match="no pull requests found"):
        workspaces.gh_pr_view(str(tmp_path), 1)


def test_cli_new_pr(db, pr_repo, monkeypatch):
    fake_gh(monkeypatch)
    monkeypatch.chdir(pr_repo)
    result = runner.invoke(app, ["new", "--pr", "42", "--agent", "none"])
    assert result.exit_code == 0, result.output
    ws = workspaces.resolve(db, "fix/typo", cwd=str(pr_repo))
    assert ws.base_branch == "release"


@pytest.mark.parametrize("extra", [["feature"], ["--base", "main"]])
def test_cli_new_pr_rejects_branch_or_base(pr_repo, monkeypatch, extra):
    calls = fake_gh(monkeypatch)
    monkeypatch.chdir(pr_repo)
    result = runner.invoke(app, ["new", "--pr", "42", "--agent", "none", *extra])
    assert result.exit_code == 1
    assert "--pr" in result.output
    assert calls == []
