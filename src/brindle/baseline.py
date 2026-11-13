"""Run branch tests over base source, without changing the caller's checkout.

Each command is one regression check. Exit 1 means a failing check; other
nonzero statuses are errors (including pytest collection and usage errors).
Test directories include their fixtures and helpers; colocated test files and
conftest.py are also overlaid. Build and dependency configuration stays at base.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from brindle import git, spawn
from brindle.config import load_repo_config


@dataclass(frozen=True)
class Result:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def outcome(self) -> str:
        return {0: "passes on base (not a regression test)",
                1: "fails on base"}.get(self.returncode, "could not check base")


def base_commit(root: Path, base: str | None) -> str:
    if base is not None:
        return git.out(["rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], root)
    branch = git.current_branch(root)
    target = ((git.get_base(root, branch) if branch else None)
              or os.environ.get("BRINDLE_BASE_BRANCH")
              or load_repo_config(root).base_branch or git.default_branch(root))
    return git.merge_base(root, git.base_ref(root, target))


def _test_file(path: Path) -> bool:
    return (bool({"tests", "test", "__tests__"}.intersection(path.parts[:-1]))
            or path.name == "conftest.py" or path.name.startswith("test_")
            or path.name.endswith("_test.py")
            or bool(re.search(r"\.(test|spec)\.[^.]+$", path.name)))


def _overlay(root: Path, scratch: Path) -> None:
    paths = git.run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root).stdout
    paths += git.run(["ls-files", "-z"], scratch).stdout
    for name in sorted(set(paths.split("\0")) - {""}):
        relative = Path(name)
        if not _test_file(relative):
            continue
        source, dest = root / relative, scratch / relative
        # Never follow a test link into source, or a base link out of scratch.
        if any((root / p).is_symlink() for p in (relative, *relative.parents)):
            raise ValueError(f"test overlay contains a symlink: {name}")
        if any((scratch / p).is_symlink() for p in (relative, *relative.parents)):
            raise ValueError(f"base test path contains a symlink: {name}")
        if source.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        elif dest.is_file():
            dest.unlink()   # a test deleted in the working tree stays deleted


def _environment(root: Path, scratch: Path) -> dict[str, str]:
    env = dict(os.environ)
    bootstrap = scratch.parent / "python"
    bootstrap.mkdir()
    # site runs editable-install .pth files before sitecustomize. Filter both
    # source paths and the specs returned by their import hooks; PEP 660 hooks
    # can retain checkout mappings without any checkout entry in sys.path.
    (bootstrap / "sitecustomize.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        f"_checkout = Path({str(root)!r})\n"
        "def _branch_source(path):\n"
        "    resolved = Path(path).resolve()\n"
        "    return resolved.is_relative_to(_checkout) and not resolved.is_relative_to(_checkout / '.venv')\n"
        "sys.path[:] = [path for path in sys.path if not _branch_source(path)]\n"
        "class _BaseFinder:\n"
        "    def __init__(self, finder):\n"
        "        self.finder = finder\n"
        "    def __getattr__(self, name):\n"
        "        return getattr(self.finder, name)\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        spec = self.finder.find_spec(fullname, path, target)\n"
        "        if spec is None:\n"
        "            return None\n"
        "        if spec.origin not in (None, 'built-in', 'frozen') and _branch_source(spec.origin):\n"
        "            return None\n"
        "        if spec.submodule_search_locations is not None:\n"
        "            locations = [p for p in spec.submodule_search_locations if not _branch_source(p)]\n"
        "            if len(locations) != len(spec.submodule_search_locations):\n"
        "                if not locations:\n"
        "                    return None\n"
        "                spec.submodule_search_locations = locations\n"
        "        return spec\n"
        "sys.meta_path[:] = [_BaseFinder(finder) for finder in sys.meta_path]\n")
    # Inherited Git state and Python paths must not redirect a test to the
    # agent's checkout. Source precedes any editable install in the interpreter.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "PYTHONHOME", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
        env.pop(key, None)
    env["PYTHONPATH"] = os.pathsep.join(map(str, (bootstrap, scratch / "src", scratch)))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    venv = root / ".venv"
    if (venv / "bin" / "python").exists():
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = str(venv / "bin") + os.pathsep + env.get("PATH", "")
    return env


def run(commands: list[list[str]], *, cwd: str | Path | None = None,
        base: str | None = None) -> list[Result]:
    """Check argv commands sequentially; callers should select one test per command.

    Commands run directly, without a shell. Python checks can reuse the current
    dependencies, but import the scratch source. ``uv run`` reuses that environment
    without syncing it, so it cannot rewrite the agent's editable installation.
    """
    if not commands or any(not command for command in commands):
        raise ValueError("provide a test command after --")
    here = Path(cwd or Path.cwd()).resolve()
    root = Path(git.toplevel(here)).resolve()
    commit = base_commit(root, base)
    results = []
    with tempfile.TemporaryDirectory(prefix="brindle-baseline-") as temporary:
        scratch = Path(temporary).resolve() / "tree"
        added = False
        try:
            git.run(["worktree", "add", "--detach", str(scratch), commit], root)
            added = True
            _overlay(root, scratch)
            workdir = scratch / here.relative_to(root)
            if not workdir.is_dir():
                raise ValueError("the current directory does not exist on base; run from the repo root")
            env = _environment(root, scratch)
            for command in commands:
                argv = list(command)
                if argv[:2] == ["uv", "run"] and env.get("VIRTUAL_ENV"):
                    # Keep this invocation from syncing the shared dependencies.
                    argv[2:2] = ["--active", "--no-sync"]
                # Absolute test selectors from this checkout refer to its copy.
                argv[1:] = [arg.replace(str(root) + os.sep, str(scratch) + os.sep)
                            if arg.startswith(str(root) + os.sep) else arg for arg in argv[1:]]
                try:
                    proc = spawn.action(argv, cwd=workdir, env=env, no_terminal=True)
                except OSError as exc:
                    results.append(Result(tuple(command), 127, "", str(exc)))
                else:
                    results.append(Result(tuple(command), proc.returncode, proc.stdout, proc.stderr))
        finally:
            # A failed add can leave a registration; remove only our unique path.
            git.run(["worktree", "remove", "--force", str(scratch)], root, check=added)
    return results


def main(command: list[str], base: str | None = None) -> int:
    """CLI status: 0 proves failure, 1 passed on base, 2 could not run the check."""
    import typer

    try:
        results = run([command], base=base)
    except (git.GitError, ValueError, OSError, spawn.Busy) as exc:
        typer.echo(f"baseline: {exc}", err=True)
        return 2
    for result in results:
        typer.echo(result.stdout, nl=False)
        typer.echo(result.stderr, nl=False, err=True)
        typer.echo(f"{result.outcome}: {shlex.join(result.command)}")
    return max(0 if r.returncode == 1 else 1 if r.returncode == 0 else 2 for r in results)
