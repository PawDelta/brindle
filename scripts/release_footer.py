#!/usr/bin/env python3
"""Add the docs link to brindle release notes.

    scripts/release_footer.py NOTES.md          append the footer to a notes file
    scripts/release_footer.py --github [--dry-run]
                                                add it to every published GitHub release

Each release page ends with a pointer to the docs and install page, so people
who land on a release find the site. Running it twice changes nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = "hmlerner/brindle-ai"
SITE = "https://pawdelta.com/brindle/"
FOOTER = f"Docs and install: [pawdelta.com/brindle]({SITE}) · Release notes: [pawdelta.com/brindle/changelog]({SITE}changelog)"


def with_footer(body: str) -> str:
    """``body`` ending with FOOTER, unless it already links the site."""
    if "pawdelta.com/brindle" in body:
        return body
    return body.rstrip() + "\n\n---\n\n" + FOOTER + "\n"


def footer_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    new = with_footer(text)
    if new != text:
        path.write_text(new, encoding="utf-8")
    return new != text


def footer_github(dry_run: bool) -> int:
    out = subprocess.run(["gh", "api", "--paginate", f"repos/{REPO}/releases"],
                         check=True, capture_output=True, text=True).stdout
    changed = 0
    for rel in json.loads(out.replace("][", ",")):
        body = rel.get("body") or ""
        new = with_footer(body)
        if new == body:
            continue
        changed += 1
        print(("would update " if dry_run else "updating ") + rel["tag_name"])
        if not dry_run:
            subprocess.run(["gh", "release", "edit", rel["tag_name"], "--repo", REPO,
                            "--notes", new], check=True, capture_output=True)
    print(f"{changed} release(s) {'to update' if dry_run else 'updated'}")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--github":
        return footer_github("--dry-run" in argv[1:])
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    print("added the footer" if footer_file(Path(argv[0])) else "footer already there")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
