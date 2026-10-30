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


def list_items(lines: list[str]) -> list[str]:
    """Collect wrapped item text without sharing evidence with child items."""
    items: list[list[str]] = []
    stack: list[tuple[int, list[str]]] = []
    blank = True
    for raw in lines:
        line = raw.expandtabs(4)
        indent = len(line) - len(line.lstrip())
        bullet = BULLET.match(line)
        if bullet:
            while stack and indent <= stack[-1][0]:
                stack.pop()
            item = [bullet[1]]
            items.append(item)
            stack.append((indent, item))
        elif not line.strip():
            blank = True
            continue
        elif re.match(r"^ {0,3}#{1,6}\s", line):
            stack.clear()
        else:
            # An outdent leaves a nested item; a blank line also ends an
            # unindented paragraph. Other lines can be lazy continuations.
            while stack and indent <= stack[-1][0] and (blank or len(stack) > 1):
                stack.pop()
            if stack:
                stack[-1][1].append(line.strip())
        blank = False
    return ["\n".join(item) for item in items]


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
        for item in list_items(sections.get(name, [])):
            if not TAG.search(item):
                fail("tags", f"{name} bullet needs an evidence tag: {item}")
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
        try:
            url = urlsplit(target)
        except ValueError:  # malformed destinations cannot link an appendix
            continue
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
