"""A finished analysis doc, opened in its session's Visuals.

brindle publishes it, on every plan and whatever the storage switch says: when
a worker whose brief asked for a doc reports, and when a supervisor's turn ends
with the goal reached and a doc requested. Each doc is one visual; a changed
doc is a new revision of it, an unchanged one publishes nothing. The Inbox
item that visual becomes is the notification (service/inbox._visual_items).

The doc is agent-written and untrusted: :func:`render` escapes every piece of
it, so the only tags on the page are the ones written here. Links and images
come out as text, a page with nothing to click or load. A doc that is refused
or can't be rendered is logged and left in its folder: it never stops a report
or a merge.
"""

from __future__ import annotations

import html as html_lib
import logging
import re

from brindle import visuals
from brindle.db import DB, Agent, Workspace

log = logging.getLogger(__name__)

_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r" {0,3}(#{1,6})\s+(.*)")
_ITEM = re.compile(r"\s*(?:[-*+]|(\d+)[.)])\s+(.*)")
_QUOTE = re.compile(r" {0,3}>\s?(.*)")
_CELL_RULE = re.compile(r":?-+:?")
# Each pattern stops at the next mark or a fixed length, so a doc full of
# stray marks can't make matching quadratic.
_LINK = re.compile(r"!?\[([^\[\]\n]{0,500})\]\(([^()\s]{0,2000})[^()\n]{0,200}\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![*\w])\*([^\s*](?:[^*]*[^\s*])?)\*(?![*\w])")

STYLE = ("body{font:14px/1.6 system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;"
         "color-scheme:light dark}pre{overflow-x:auto;padding:8px 12px;background:#8882;border-radius:6px}"
         "code{font:12.5px ui-monospace,monospace}table{border-collapse:collapse}"
         "td,th{border:1px solid #8886;padding:4px 8px;text-align:left}"
         "blockquote{margin:0;padding-left:12px;border-left:3px solid #8886}"
         ".file{font:12.5px ui-monospace,monospace;opacity:.7;border-top:1px solid #8886;padding-top:12px}")


def _inline(text: str) -> str:
    """``text`` as inline HTML: escaped first, so what it says can't become a tag."""
    out = []
    for i, part in enumerate(re.split(r"(`[^`\n]*`)", html_lib.escape(text))):
        if i % 2:
            out.append(f"<code>{part[1:-1]}</code>")
            continue
        part = _LINK.sub(lambda m: f"{m[1]} ({m[2]})" if m[2] else m[1], part)
        part = _BOLD.sub(r"<strong>\1</strong>", part)
        out.append(_ITALIC.sub(r"<em>\1</em>", part))
    return "".join(out)


def _is_rule(line: str) -> bool:
    mark = "".join(line.split())
    return len(mark) >= 3 and len(set(mark)) == 1 and mark[0] in "-*_"


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_table(lines: list[str], i: int) -> bool:
    return ("|" in lines[i] and i + 1 < len(lines) and "-" in lines[i + 1]
            and all(_CELL_RULE.fullmatch(c) for c in _cells(lines[i + 1])))


def _starts_block(lines: list[str], i: int) -> bool:
    line = lines[i]
    return bool(not line.strip() or _FENCE.match(line) or _HEADING.match(line) or _ITEM.match(line)
                or _QUOTE.match(line) or _is_rule(line) or _is_table(lines, i))


def _row(tag: str, cells: list[str]) -> str:
    return "<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells) + "</tr>"


def render(markdown: str) -> str:
    """``markdown`` as HTML: the common blocks and inline marks, nothing passed through."""
    lines, out, i = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif fence := _FENCE.match(line):
            body, i = [], i + 1
            while i < len(lines) and not lines[i].strip().startswith(fence[1]):
                body.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{html_lib.escape(chr(10).join(body))}</code></pre>")
        elif head := _HEADING.match(line):
            level = len(head[1])
            out.append(f"<h{level}>{_inline(head[2].strip().rstrip('#').strip())}</h{level}>")
            i += 1
        elif _is_rule(line):
            out.append("<hr>")
            i += 1
        elif _is_table(lines, i):
            rows, i = [_row("th", _cells(line))], i + 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_row("td", _cells(lines[i])))
                i += 1
            out.append(f"<table>{''.join(rows)}</table>")
        elif _ITEM.match(line):
            tag = "ol" if _ITEM.match(line)[1] else "ul"
            items = []
            while i < len(lines):
                item = _ITEM.match(lines[i])
                if item and bool(item[1]) == (tag == "ol"):
                    items.append(item[2])
                elif items and lines[i].startswith((" ", "\t")) and lines[i].strip() and not item:
                    items[-1] += "\n" + lines[i].strip()
                else:
                    break
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + f"</{tag}>")
        elif _QUOTE.match(line):
            quoted = []
            while i < len(lines) and (q := _QUOTE.match(lines[i])):
                quoted.append(q[1])
                i += 1
            out.append(f"<blockquote><p>{_inline(chr(10).join(quoted))}</p></blockquote>")
        else:
            para, i = [line.strip()], i + 1
            while i < len(lines) and not _starts_block(lines, i):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{_inline(chr(10).join(para))}</p>")
    return "\n".join(out)


def _page(title: str, files: list[dict]) -> str:
    """The whole doc as one page; a doc of several files names each of them."""
    parts = []
    for f in files:
        if len(files) > 1:
            parts.append(f'<h2 class="file">{html_lib.escape(f["name"])}</h2>')
        parts.append(render(f["md"]))
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{html_lib.escape(title)}</title>'
            f"<style>{STYLE}</style></head><body>{''.join(parts)}</body></html>")


def _publish(db: DB, agent: Agent, ws: Workspace) -> None:
    from brindle import plans
    from brindle.pro import analysis_brief, analysis_docs

    if not visuals.enabled():
        return
    docs = analysis_docs.collect(ws.repo_root, ws.branch, {})
    if not docs:
        return
    # The folder's top-level files first: the doc itself before its notes.
    files = sorted(docs[0].files, key=lambda f: ("/" in f["name"], f["name"]))
    # What the agent wrote, before escaping can change how a secret looks.
    visuals._refuse_secrets([(f"the doc {f['name']}", f["md"]) for f in files])
    title = next(filter(None, (plans._title(f["md"]) for f in files)), None)
    title = title or f"Analysis doc: {ws.branch}"[:visuals.MAX_TITLE]
    visuals.publish_analysis_doc(db, agent, str(analysis_brief.docs_dir(ws.repo_root, ws.branch)),
                                 title, _page(title, files))


def on_report(db: DB, agent: Agent, ws: Workspace | None) -> None:
    """A worker reported: open the doc its brief asked for. Never raises."""
    try:
        from brindle.pro import analysis_brief

        if ws is not None and analysis_brief.got_context(db, agent.id):
            _publish(db, agent, ws)
    except Exception:  # noqa: BLE001 - a notice, never a reason to lose the report
        log.warning("analysis doc of %s not opened in Visuals", agent.id, exc_info=True)


def on_turn(db: DB, agent: Agent) -> None:
    """A supervisor's turn ended: open the doc of its reached goal, if the
    person requested one (autopilot._goal_doc_rule). Never raises."""
    try:
        ap = db.get_autopilot(agent.id)
        ws = db.get_workspace(agent.workspace_id)
        if ap is not None and ap.analysis_doc and ap.state == "done" and ws is not None:
            _publish(db, agent, ws)
    except Exception:  # noqa: BLE001 - a notice, never a reason to fail a turn
        log.warning("goal analysis doc of %s not opened in Visuals", agent.id, exc_info=True)
