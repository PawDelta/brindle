#!/usr/bin/env python3
"""Set one version number for brindle and its desktop app.

    scripts/bump_version.py X.Y.Z [--dry-run]
    scripts/bump_version.py --check

The app bundles its engine, so they ship as one version. A bump sets it in
pyproject.toml, the brindle entry of uv.lock, the README's status-bar example,
app/package.json and app/package-lock.json (two spots). --check (run by CI)
fails when app/package.json and pyproject.toml disagree.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?")

# file -> (pattern with groups 1 and 3 around the version, expected matches)
EDITS = {
    "pyproject.toml": (r'(?m)^(version = ")([^"]+)(")', 1),
    "uv.lock": (r'(?m)(^name = "brindle"\nversion = ")([^"]+)(")', 1),
    "README.md": (r"(`brindle )(\d+\.\d+\.\d+[^`\s]*)(`\))", 1),
    "app/package.json": (r'(?m)^(  "version": ")([^"]+)(")', 1),
    "app/package-lock.json": (r'(?m)^(  "version": ")([^"]+)(")|^(    "": \{\n(?:      .*\n)*?      "version": ")([^"]+)(")', 2),
}


def versions(root: Path = ROOT) -> dict[str, str]:
    """The version each file carries now."""
    found = {}
    for name, (pattern, _) in EDITS.items():
        m = re.search(pattern, (root / name).read_text())
        if m is None:
            raise SystemExit(f"{name}: no version found")
        found[name] = next(g for g in m.groups()[1::3] if g)
    return found


def bump(version: str, root: Path = ROOT, dry_run: bool = False) -> list[str]:
    """Write `version` everywhere; returns the files changed."""
    if not SEMVER.fullmatch(version):
        raise SystemExit(f"{version!r} is not X.Y.Z")
    changed = []
    for name, (pattern, expected) in EDITS.items():
        path = root / name
        text = path.read_text()
        done = 0

        def swap(m: re.Match) -> str:
            nonlocal done
            done += 1
            groups = m.groups()
            lead, tail = (groups[0], groups[2]) if groups[0] else (groups[3], groups[5])
            return f"{lead}{version}{tail}"

        new = re.sub(pattern, swap, text)
        if done != expected:
            raise SystemExit(f"{name}: expected {expected} version spot(s), found {done}")
        if new != text:
            changed.append(name)
            if not dry_run:
                path.write_text(new)
    return changed


# The separate numbers brindle had before it moved to one version; the first
# bump retires this pair (and this allowance).
LEGACY_PAIR = {"engine": "0.5.5", "app": "0.2.8"}


def check(root: Path = ROOT, allow_legacy: bool = False) -> str | None:
    """None when the app and the engine agree, else what differs.

    allow_legacy lets CI pass the old separate numbers until the first bump;
    any other mismatch still fails."""
    engine = versions(root)["pyproject.toml"]
    app = versions(root)["app/package.json"]
    if allow_legacy and (engine, app) == (LEGACY_PAIR["engine"], LEGACY_PAIR["app"]):
        return None
    if app != engine:
        return f"app/package.json is {app} but pyproject.toml is {engine}: brindle ships one version (scripts/bump_version.py)"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", nargs="?")
    ap.add_argument("--dry-run", action="store_true", help="say what would change, write nothing")
    ap.add_argument("--check", action="store_true", help="fail if the app and engine versions differ")
    ap.add_argument("--allow-legacy", action="store_true", help="with --check, accept the pre-unification numbers")
    args = ap.parse_args(argv)
    if args.check:
        problem = check(allow_legacy=args.allow_legacy)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        return 0
    if not args.version:
        ap.error("give a version, or --check")
    for name in bump(args.version, dry_run=args.dry_run):
        print(("would set " if args.dry_run else "set ") + f"{name} to {args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
