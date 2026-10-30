"""Check committed analysis deliverables when the managed pack is active."""

from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote, urlsplit

import yaml

from brindle import git
from brindle.rule_checks import Violation

MAIN = re.compile(r"^\.brindle/deliverables/[^/]+/main\.md$")
BULLET = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(.*)$")
TAG = re.compile(r"\[(?:verified|inference|not checked)\]")


def prose(text: str) -> str:
    """Ignore fenced examples and HTML comments when recognizing structure."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            value = marker[1]
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def check_main(pack: str, path: str, text: str, appendices: list[str]) -> list[Violation]:
    found = []

    def fail(rule: str, detail: str) -> None:
        found.append(Violation(pack, rule, f"{path}: {detail}"))

    front = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", text, re.S)
    try:
        meta = yaml.safe_load(front[1]) if front else None
    except yaml.YAMLError:
        meta = None
    if not isinstance(meta, dict):
        fail("frontmatter", "YAML frontmatter is required")
        meta = {}
    for key in ("title", "question"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            fail(key, f"frontmatter needs a nonempty {key}")
    if "scope" in meta and meta["scope"] not in ("user", "team", "enterprise"):
        fail("scope", "scope must be user, team or enterprise")
    body = prose(text[front.end():] if front else text)
    sections: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if heading and len(heading[1]) <= 2:
            current = heading[2] if len(heading[1]) == 2 else None
            if current is not None:
                sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    for name in ("Results", "Conclusion"):
        if name not in sections:
            fail(name.lower(), f"## {name} section is required")
        for line in sections.get(name, []):
            bullet = BULLET.match(line)
            if bullet and not TAG.search(bullet[1]):
                fail("tags", f"{name} bullet needs an evidence tag: {line.strip()}")
    metrics = "\n".join(BULLET.sub(r"\1", line) for line in sections.get("Results", []))
    if "Results" in sections and not re.search(r"\d", metrics):
        fail("metric", "Results needs a quantified metric")
    if not appendices:
        fail("appendices", "at least one appendices/*.md file is required")
    # Resolve inline and reference-style Markdown links relative to main.md.
    targets = re.findall(r"(?<!!)\[[^\]\n]*\]\(\s*<?([^\s)>]+)>?(?:\s+[^)]*)?\)", body)
    definitions = dict(re.findall(r"^\s{0,3}\[([^\]]+)\]:\s*<?([^\s>]+)>?", body, re.M))
    definitions = {k.casefold(): v for k, v in definitions.items()}
    for label, ref in re.findall(r"(?<!!)\[([^\]]+)\]\[([^\]]*)\]", body):
        target = definitions.get((ref or label).casefold())
        if target:
            targets.append(target)
    for label, target in definitions.items():
        if re.search(r"(?<!!)\[" + re.escape(label) + r"\](?![(:\[])", body, re.I):
            targets.append(target)
    linked = set()
    for target in targets:
        url = urlsplit(target)
        if not url.scheme and not url.netloc and url.path:
            linked.add(posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(url.path))))
    for appendix in appendices:
        if appendix not in linked:
            fail("appendix_links", f"appendix must be linked: {appendix}")
    return found


def check_branch(root: str, files: list[str], pack: str) -> list[Violation]:
    mains = [p for p in files if MAIN.fullmatch(p)]
    if not mains:
        return []
    tracked = git.run(["ls-tree", "-r", "--name-only", "-z", "HEAD", "--",
                       ".brindle/deliverables/"], root).stdout.split("\0")
    found = []
    for path in mains:
        if path not in tracked:  # deleted deliverables do not need a format
            continue
        prefix = posixpath.dirname(path) + "/appendices/"
        appendices = [p for p in tracked if p.startswith(prefix) and p.endswith(".md")
                      and "/" not in p[len(prefix):]]
        text = git.run(["show", f"HEAD:{path}"], root).stdout
        found.extend(check_main(pack, path, text, appendices))
    return found
