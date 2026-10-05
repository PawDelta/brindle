#!/usr/bin/env python3
"""Publish a release snapshot to the public copse-ai repository.

Development happens in this (private) repository. The public repository
hmlerner/copse-ai only gets one commit per release: the released code, README,
license and pyproject.toml, with no development history, tests, CI or config.

    python3 scripts/publish_public.py 0.17.0            # show what would happen
    python3 scripts/publish_public.py 0.17.0 --push     # commit, tag, push, release

It reads the files at this repo's tag ``v<version>``, replaces the public
checkout's contents with them, commits "copse <version>", tags it, pushes, and
creates the GitHub release there with ``.copse/release-notes-<version>.md``.
PyPI publishing stays with this repo's Publish workflow.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

PUBLIC_REPO = "hmlerner/copse-ai"
PUBLIC_PATHS = ["src", "README.md", "LICENSE", "pyproject.toml"]
ROOT = Path(__file__).resolve().parent.parent


def run(args: list[str], cwd: Path, capture: bool = False) -> str:
    out = subprocess.run(args, cwd=cwd, check=True, text=True,
                         capture_output=capture)
    return out.stdout.strip() if capture else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version")
    parser.add_argument("--public", default=os.path.expanduser("~/Projects/copse-ai"),
                        help="local clone of the public repo (cloned if missing)")
    parser.add_argument("--push", action="store_true", help="actually commit, tag, push and release")
    args = parser.parse_args()

    tag = f"v{args.version}"
    notes = ROOT / ".copse" / f"release-notes-{args.version}.md"
    try:
        run(["git", "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}"], ROOT, capture=True)
    except subprocess.CalledProcessError:
        sys.exit(f"no tag {tag} in {ROOT}; release it here first")
    pyproject = run(["git", "show", f"{tag}:pyproject.toml"], ROOT, capture=True)
    if f'version = "{args.version}"' not in pyproject:
        sys.exit(f"{tag}'s pyproject.toml isn't version {args.version}")
    if not notes.is_file():
        sys.exit(f"missing {notes.relative_to(ROOT)}")

    public = Path(args.public)
    if not public.exists():
        print(f"cloning {PUBLIC_REPO} into {public}")
        run(["gh", "repo", "clone", PUBLIC_REPO, str(public)], ROOT)
    if run(["git", "status", "--porcelain"], public, capture=True):
        sys.exit(f"{public} has uncommitted changes; leaving it alone")
    if run(["git", "tag", "--list", tag], public, capture=True):
        sys.exit(f"{PUBLIC_REPO} already has {tag}")

    archive = subprocess.run(["git", "archive", "--format=tar", tag, *PUBLIC_PATHS],
                             cwd=ROOT, check=True, capture_output=True).stdout
    names = tarfile.open(fileobj=io.BytesIO(archive)).getnames()
    print(f"{tag}: {len(names)} entries from {', '.join(PUBLIC_PATHS)}")
    if not args.push:
        print(f"dry run: would replace {public}'s files, commit \"copse {args.version}\", "
              f"tag {tag}, push, and create the release on {PUBLIC_REPO}")
        return 0

    for entry in public.iterdir():
        if entry.name == ".git":
            continue
        shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    tarfile.open(fileobj=io.BytesIO(archive)).extractall(public, filter="data")
    run(["git", "add", "-A"], public)
    run(["git", "commit", "-q", "-m", f"copse {args.version}"], public)
    run(["git", "tag", tag], public)
    run(["git", "push", "-q", "origin", "HEAD", tag], public)
    run(["gh", "release", "create", tag, "-R", PUBLIC_REPO, "--title", f"copse {args.version}",
         "--notes-file", str(notes)], ROOT)
    print(f"published {tag} to {PUBLIC_REPO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
