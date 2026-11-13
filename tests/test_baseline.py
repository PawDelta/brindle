"""Baseline checks execute in a disposable worktree, never in the agent's tree."""
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from brindle import git, permissions, spawn
from brindle.cli import app


@pytest.fixture(scope="module")
def baseline_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("baseline-repo")
    git.run(["init", "-b", "main"], root)
    git.run(["config", "user.email", "test@example.com"], root)
    git.run(["config", "user.name", "Test"], root)
    (root / "src").mkdir()
    (root / "src" / "answer.py").write_text("value = 0\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_answer.py").write_text("def test_old():\n    assert False\n")
    (root / "tests" / "removed.txt").write_text("obsolete fixture")
    git.run(["add", "."], root)
    git.run(["commit", "-m", "base"], root)
    git.run(["switch", "-c", "feature"], root)
    git.set_base(root, "feature", "main")
    (root / "src" / "answer.py").write_text("value = 1\n")
    (root / "tests" / "test_answer.py").write_text(
        "from answer import value\n"
        "def test_regression():\n    assert value == 1\n"
        "def test_existing():\n    assert value >= 0\n")
    (root / "tests" / "removed.txt").unlink()
    git.run(["add", "."], root)
    git.run(["commit", "-m", "fix and regression"], root)
    # The base branch advances independently; its tip is not the branch's base.
    advanced = git.out(["commit-tree", git.out(["rev-parse", "HEAD^{tree}"], root),
                        "-p", git.out(["rev-parse", "main"], root), "-m", "base advanced"], root)
    git.run(["update-ref", "refs/heads/main", advanced], root)
    # An editable install must not leak a module introduced only on the branch.
    (root / "src" / "branch_only.py").write_text("value = 1\n")
    proc = spawn.action([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")])
    assert proc.returncode == 0, proc.stderr
    packages = root / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    (packages / "dependencies.pth").write_text("\n".join([*sys.path, str(root / "src")]) + "\n")
    # PEP 660 backends can install finders instead of adding source to sys.path.
    (packages / "dependency.py").write_text("value = 3\n")
    (packages / "editable_hook.py").write_text(
        "import sys\nfrom importlib.util import spec_from_file_location\n"
        "from importlib.machinery import ModuleSpec\n"
        "class Finder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, fullname, path=None, target=None):\n"
        "        if fullname == 'hook_only':\n"
        f"            return spec_from_file_location(fullname, {str(root / 'src' / 'branch_only.py')!r})\n"
        "        if fullname == 'answer':\n"
        f"            return spec_from_file_location(fullname, {str(root / 'src' / 'answer.py')!r})\n"
        "        if fullname == 'editable_dependency':\n"
        f"            return spec_from_file_location(fullname, {str(packages / 'dependency.py')!r})\n"
        "        if fullname == 'branch_namespace':\n"
        "            spec = ModuleSpec(fullname, None, is_package=True)\n"
        f"            spec.submodule_search_locations = [{str(root / 'src')!r}]\n"
        "            return spec\n"
        "sys.meta_path.insert(0, Finder)\n")
    (packages / "editable.pth").write_text("import editable_hook\n")
    return root


def test_cli_checks_base_with_live_tests_without_changing_checkout(baseline_repo, monkeypatch):
    """Catches testing HEAD, dropping dirty tests, and leaking worktree registrations."""
    from brindle import baseline

    root = baseline_repo
    monkeypatch.chdir(root)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONPATH", str(root / "src"))
    # Include staged, unstaged and untracked test inputs. Production remains at base.
    (root / "tests" / "test_answer.py").write_text(
        "from answer import value\n"
        "from pathlib import Path\n"
        "from importlib.util import find_spec\n"
        "from editable_dependency import value as dependency_value\n"
        "def test_regression():\n    assert value == int(Path('tests/expected.txt').read_text())\n"
        "def test_existing():\n    assert value == 0\n"
        "    assert find_spec('branch_only') is None\n"
        "    assert find_spec('hook_only') is None\n"
        "    assert find_spec('branch_namespace') is None\n"
        "    assert dependency_value == 3\n"
        "    assert not Path('tests/removed.txt').exists()\n")
    git.run(["add", "tests/test_answer.py"], root)
    (root / "tests" / "test_answer.py").write_text(
        (root / "tests" / "test_answer.py").read_text() +
        "def test_dirty():\n    assert value == 7\n")
    (root / "tests" / "expected.txt").write_text("1")
    (root / "src" / "answer.py").write_text("value = 7\n")
    before = git.out(["status", "--porcelain"], root), git.out(["diff", "HEAD"], root)
    index = (root / ".git" / "index").read_bytes()
    runner = CliRunner()
    command = [str(root / ".venv" / "bin" / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    failed = runner.invoke(app, ["baseline", "--", *command, "tests/test_answer.py::test_regression"])
    assert failed.exit_code == 0, failed.output
    assert "fails on base" in failed.output
    passed = runner.invoke(app, ["baseline", "--", *command, "tests/test_answer.py::test_existing"])
    assert passed.exit_code == 1, passed.output
    assert "passes on base" in passed.output
    explicit = baseline.run([command + ["tests/test_answer.py::test_regression"],
                             command + ["tests/test_answer.py::test_dirty"],
                             command + ["tests/missing.py"]], cwd=root, base="HEAD")
    assert [r.returncode for r in explicit] == [0, 1, 4]
    assert before == (git.out(["status", "--porcelain"], root), git.out(["diff", "HEAD"], root))
    assert (root / ".git" / "index").read_bytes() == index
    assert (root / "src" / "answer.py").read_text() == "value = 7\n"
    assert len(git.list_worktrees(root)) == 1
    assert not (root / ".git" / "refs" / "stash").exists()


def test_overlay_deletions_and_symlinks(tmp_path, monkeypatch):
    """Deleted tests stay gone, and links cannot copy source into the overlay or escape it."""
    from brindle import baseline

    root, scratch = tmp_path / "branch", tmp_path / "base"
    for directory in (root, scratch):
        (directory / "tests").mkdir(parents=True)
    (scratch / "tests" / "removed.txt").write_text("old")
    (root / "test_new.py").write_text("new")
    (root / "component.spec.ts").write_text("new")
    monkeypatch.setattr(git, "run", lambda args, cwd: SimpleNamespace(
        stdout="tests/removed.txt\0test_new.py\0component.spec.ts\0source.py\0"))
    baseline._overlay(root, scratch)
    assert not (scratch / "tests" / "removed.txt").exists()
    assert (scratch / "test_new.py").read_text() == "new"
    assert (scratch / "component.spec.ts").read_text() == "new"
    (root / "test_new.py").unlink()
    (root / "test_new.py").symlink_to(root / "component.spec.ts")
    with pytest.raises(ValueError):
        baseline._overlay(root, scratch)
    (root / "test_new.py").unlink()
    (root / "test_new.py").write_text("new")
    (scratch / "test_new.py").unlink()
    (scratch / "test_new.py").symlink_to(root / "test_new.py")
    with pytest.raises(ValueError):
        baseline._overlay(root, scratch)


def test_errors_and_interrupts_remove_scratch(tmp_path, monkeypatch):
    """Launch errors are not test failures; cancellation must also remove the worktree."""
    from brindle import baseline

    monkeypatch.setattr(git, "toplevel", lambda cwd: str(tmp_path))
    monkeypatch.setattr(baseline, "base_commit", lambda root, base: "base")
    monkeypatch.setattr(baseline, "_overlay", lambda root, scratch: None)
    active = []

    def worktree(args, root, **kwargs):
        if args[1] == "add":
            active.append(Path(args[3]))
            active[0].mkdir()
        else:
            shutil.rmtree(active.pop())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(git, "run", worktree)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").touch()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / ".git"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "src"))

    def check_environment(argv, *, cwd, env, **kwargs):
        assert argv[:4] == ["uv", "run", "--active", "--no-sync"]
        assert Path(cwd) != tmp_path
        assert "GIT_DIR" not in env
        assert str(tmp_path / "src") not in env["PYTHONPATH"]
        assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
        return SimpleNamespace(returncode=2, stdout="", stderr="collection failed")

    monkeypatch.setattr(spawn, "action", check_environment)
    assert baseline.main(["uv", "run", "pytest"]) == 2
    assert not active

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing executable")

    monkeypatch.setattr(spawn, "action", missing)
    assert baseline.main(["missing"]) == 2
    assert not active
    assert baseline.main([]) == 2

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(spawn, "action", interrupted)
    with pytest.raises(KeyboardInterrupt):
        baseline.run([["pytest"]], cwd=tmp_path)
    assert not active


def test_permission_allows_baseline_but_preserves_nested_denials(tmp_path):
    """Allows the wrapper without broadly allowing every brindle command or hiding denies."""
    req = permissions.Request("codex", "bash", "exec_command", str(tmp_path), str(tmp_path), str(tmp_path),
                              command="brindle baseline -- uv run pytest tests/x.py::test_y")
    assert permissions.decide(req).decision == "allow"
    permissions.add_rule("bash", "pytest ", "deny", match_type="prefix")
    req = replace(req, command="brindle baseline --base HEAD -- pytest tests/x.py")
    assert permissions.decide(req).decision == "deny"
    req = replace(req, command="brindle baseline --base=HEAD -- pytest tests/x.py")
    assert permissions.decide(req).decision == "deny"
    req = replace(req, command="brindle baseline --base HEAD --base=main -- pytest tests/x.py")
    assert permissions.decide(req).decision == "deny"
    req = replace(req, command="brindle baseline -- sh -c 'pytest tests/x.py'")
    assert permissions.decide(req).decision == "deny"
    req = replace(req, command="brindle delete-everything")
    assert permissions.decide(req).decision != "allow"
