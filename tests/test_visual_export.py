import base64
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from brindle import visual_export, visuals
from brindle.daemon import routes_actions
from test_visuals import SECRET, client, session  # noqa: F401 - shared visuals fixtures

FIXTURES = Path(__file__).parent / "fixtures" / "visuals"


@pytest.mark.parametrize("hostile", [
    pytest.param("<script>alert(1)</script>", marks=pytest.mark.variant("plain script elements cannot survive a snapshot")),
    pytest.param('<p onclick="alert(1)">bad</p>', marks=pytest.mark.variant("event handler bypass")),
    pytest.param('<a href="javascript:alert(1)">bad</a>', marks=pytest.mark.variant("URL bypass")),
    pytest.param('<div style="background:url(https://evil.test)">bad</div>', marks=pytest.mark.variant("CSS fetch bypass")),
    pytest.param('<svg><script>alert(1)</script></svg>', marks=pytest.mark.variant("SVG script bypass")),
    pytest.param('<iframe srcdoc="<script>alert(1)</script>"></iframe>', marks=pytest.mark.variant("iframe srcdoc cannot smuggle an executable document")),
    pytest.param('<meta http-equiv="refresh" content="0;url=https://evil.test">', marks=pytest.mark.variant("refresh bypass")),
    pytest.param('<html onclick="alert(1)"><head><meta http-equiv="Content-Security-Policy" '
                 'content="default-src *"></head><body onload="alert(1)"></body></html>',
                 marks=pytest.mark.variant("root attributes and metadata bypass")),
    '<img src="https://evil.test/tracker"><img src="data:image/svg+xml;base64,AAAA">',
    pytest.param('<svg><path fill="url(https://evil.test)" d="M0 0"></path></svg>', marks=pytest.mark.variant("SVG paint URLs cannot fetch another document")),
    pytest.param('<style>@import "https://evil.test";</style>', marks=pytest.mark.variant("stylesheet imports cannot fetch external resources")),
    pytest.param('<style>.selected{background:url(https://evil.test);color:green}</style>', marks=pytest.mark.variant("class rules cannot retain a fetching background declaration")),
    pytest.param('<style>.selected{background:u/**/rl(https://evil.test)}</style>',
                 marks=pytest.mark.variant("CSS comment URL bypass")),
    pytest.param('<style>.selected{background:\\75rl(https://evil.test)}</style>',
                 marks=pytest.mark.variant("CSS escape URL bypass")),
    pytest.param('<style>[data-secret]{color:red;background:url(https://evil.test)}</style>', marks=pytest.mark.variant("attribute selectors cannot leak document state through CSS fetches")),
    pytest.param('<style>@media screen {.selected{color:red}}</style>',
                 marks=pytest.mark.variant("unsupported nested stylesheet")),
    pytest.param('<!--bad--><object><p>bad</p></object>', marks=pytest.mark.variant("object fallback content and comments cannot survive active-content stripping")),
    pytest.param('<svg><path marker-end="url(https://evil.test/#a)" fill="url(#a) url(https://evil.test)" d="M0 0"></path></svg>',
                 marks=pytest.mark.variant("SVG reference to another document")),
    pytest.param('<style>@media (prefers-color-scheme:dark){.x{background:url(https://evil.test)}'
                 '@media (prefers-color-scheme:dark){.y{background-image:url(https://evil.test)}}}</style>',
                 marks=pytest.mark.variant("fetch inside a colour-scheme block, and a nested one")),
    pytest.param('<style>.x::before{content:url(https://evil.test)}.y::after{content:"a" url(https://evil.test)}'
                 '.z{font-family:"url(https://evil.test)"}</style>',
                 marks=pytest.mark.variant("pseudo-element content and quoted strings that fetch")),
    pytest.param('<style>.x{color:color-mix(in srgb, red 40%, image-set("https://evil.test"))}</style>',
                 marks=pytest.mark.variant("fetching function inside an allowed colour function")),
])
def test_snapshot_strips_active_content(db, session, client, hostile):
    _, boss, _ = session
    v = visuals.publish(db, boss, "Release", "<p>initial</p>")
    safe = '<p style="color:red" title="&quot;">rendered &amp; safe</p><svg viewBox="0 0 10 10"><circle r="2"></circle></svg>'
    got = client.post(f"/v1/visuals/{v['id']}/snapshot", json={"rev": 1, "html": safe + hostile})
    assert got.status_code == 200
    html = base64.b64decode(got.json()["content_b64"]).decode()
    meta = []
    parser = HTMLParser()
    parser.handle_starttag = lambda tag, attrs: meta.append(dict(attrs)) if tag == "meta" else None
    parser.feed(html)
    assert len(meta) == 2
    assert meta[0] == {"charset": "utf-8"}
    policy = next(m["content"] for m in meta if m.get("http-equiv", "").lower() == "content-security-policy")
    directives = {parts[0]: parts[1:] for rule in policy.split(";") if (parts := rule.split())}
    assert directives["default-src"] == ["'none'"]
    assert directives["script-src"] == ["'none'"]
    assert directives["img-src"] == ["data:"]
    assert directives["style-src"] == ["'unsafe-inline'"]
    assert directives["base-uri"] == directives["form-action"] == ["'none'"]
    assert html.index("<meta") < html.index("<body")
    assert safe in html
    assert not any(s in html for s in ("<script", "onclick", "onload", "javascript:", "url(", "srcdoc", "refresh", "evil.test", "bad--"))


@pytest.mark.parametrize("layout", ["mixed", pytest.param("roots", marks=pytest.mark.variant(
    "root html and body classes must survive sanitization without nesting duplicate document roots"))])
def test_snapshot_preserves_safe_class_styling(db, session, client, layout):
    _, boss, _ = session
    v = visuals.publish(db, boss, "Options", "<p>initial</p>")
    selector = "body.options" if layout == "roots" else ".options"
    grid = f"{selector}{{display:grid;grid-template-columns:repeat(2, 1fr);gap:12px}}"
    unsupported = ('@import "https://evil.test"; @media (max-width:600px) {'
                   '.options {display:block}}'
                   '@supports (display:grid) {.ignored {font-family:"}/*";}}'
                   '/* ignored { } */') if layout == "mixed" else ""
    rules = grid + unsupported + '#picked.selected {background:green;color:white}'
    children = '<p>A</p><p id="picked" class="selected">B ✓</p>'
    if layout == "roots":
        rules += 'html.page {font-size:16px} body.options > p {background:green}'
        rendered = (f'<html class="page"><head><style>{rules}</style></head>'
                    f'<body class="options" style="color:white">{children}</body></html>')
    else:
        rendered = f'<html><head><style>{rules}</style></head><body><div class="options">{children}</div></body></html>'
    got = client.post(f"/v1/visuals/{v['id']}/snapshot", json={"rev": 1, "html": rendered})
    assert got.status_code == 200
    html = base64.b64decode(got.json()["content_b64"]).decode()
    if layout == "roots":
        assert '<html class="page">' in html
        assert f'<body class="options" style="color:white">{children}</body>' in html
        assert 'html.page{font-size:16px}' in html
        assert 'body.options > p{background:green}' in html
    else:
        assert '<div class="options">' in html
    assert '<p id="picked" class="selected">B ✓</p>' in html
    assert grid in html
    assert "#picked.selected{background:green;color:white}" in html
    assert "evil.test" not in html and "display:block" not in html
    tags = []
    parser = HTMLParser()
    parser.handle_starttag = lambda tag, attrs: tags.append(tag)
    parser.feed(html)
    assert tags.count("html") == tags.count("body") == 1
    assert html.index("Content-Security-Policy") < html.index("<style>") < html.index("<body")


def test_export_keeps_how_the_visual_looked():
    """Two of the owner's visuals (tests/fixtures/visuals): *.page.html is the published page, and
    *.snapshot.html what the app's frame (app/src/screens/Session/Visuals/frame.tsx, computedSnapshot)
    made of it in a real browser: computed light values, dark differences behind @media."""
    def export(name):
        return visual_export.sanitise((FIXTURES / name).read_text())

    def rule(html, element, pseudo="", dark=False):
        name = re.search(element + r'[^>]*?\b(brindle-s\d+)', html).group(1)
        split = html.index("@media (prefers-color-scheme:dark){")
        return re.search(rf"\.{name}{pseudo}\{{([^}}]*)\}}", html[split:] if dark else html[:split]).group(1)

    leaks = export("process-leaks.snapshot.html")
    pill = r'<span class="pill fixed'
    for value in ("color:rgb(4, 110, 80)", "background-color:rgba(4, 110, 80, 0.1)",
                  "border-top-left-radius:999px", "font-size:10.5px", "white-space:nowrap"):
        assert value in rule(leaks, pill)
    assert rule(leaks, pill, dark=True) == "color:rgb(52, 211, 153);background-color:rgba(52, 211, 153, 0.12)"
    assert ".pill{padding:2px 8px}" in leaks
    assert '-apple-system, "system-ui", "SF Pro Text"' in rule(leaks, "<h1")
    assert "text-transform:uppercase;text-align:start" in rule(leaks, "<h2")
    # The arrows and the dashed box: an internal marker and its reference, dashes and colours.
    assert '<marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"' in leaks
    assert 'marker-end="url(#a)"' in leaks
    dashed = r'<rect x="600"[^>]*stroke-dasharray="4 3"'
    assert "stroke:rgb(180, 35, 24)" in rule(leaks, dashed)
    assert "stroke:rgb(248, 113, 113)" in rule(leaks, dashed, dark=True)

    table = export("now-vs-063.snapshot.html")
    old = r'<td class="old'
    for value in ("border-bottom-style:solid", "border-bottom-width:1px", "border-bottom-color:rgba(20, 40, 36, 0.11)"):
        assert value in rule(table, old)
    assert "border-bottom-color:rgba(120, 150, 135, 0.16)" in rule(table, old, dark=True)
    assert rule(table, old, "::before").startswith('content:"0.6.3  ";color:rgb(180, 35, 24)')
    assert rule(table, old, "::before", dark=True) == "color:rgb(248, 113, 113)"
    for value in ("display:table", "overflow-x:hidden", "border-collapse:collapse", "min-width:760px"):
        assert value in rule(table, "<table")
    assert "table{width:100%}" in table

    # A snapshot without computed values (an older app) still keeps its var-free shapes.
    page = export("process-leaks.page.html")
    assert ".pill{font-size:10.5px;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap}" in page
    assert 'stroke-dasharray="4 3"' in page and 'marker-end="url(#a)"' in page
    # layout-probe: the cases the export rehearsal is for, through the sanitiser. A 5vw padding and
    # scheme-dependent columns behind an attribute selector come back as the viewed literals; 25%
    # columns from a kept rule stay as declared; a gradient that differs by scheme and an arrowhead
    # only dark has come back as one copy of the shape per scheme.
    probe = export("layout-probe.snapshot.html")
    split = probe.index("@media (prefers-color-scheme:dark){")
    grid = re.search(r"\.(brindle-z\d+)\{[^}]*padding-left:55px !important;grid-template-columns:194[.\d]+px 389[.\d]+px !important\}",
                     probe[:split]).group(1)
    assert re.search(rf"\.{grid}\{{grid-template-columns:389[.\d]+px 194[.\d]+px !important\}}", probe[split:])
    assert ".half{grid-template-columns:25% 25%}" in probe
    assert not re.search(r'<div class="half[^"]*brindle-z', probe)
    assert re.search(r'<rect class="shape brindle-s\d+ brindle-light"[^>]*fill="url\(#g\)"', probe)
    assert re.search(r'<rect class="shape brindle-s\d+ brindle-dark"[^>]*fill="url\(#h\)"', probe)
    assert re.search(r'<path class="arrow brindle-s\d+ brindle-dark"[^>]*marker-end="url\(#a\)"', probe)
    assert not re.search(r'<path class="arrow brindle-s\d+ brindle-light"[^>]*marker-end', probe)
    assert ".brindle-dark{display:none}" in probe
    assert ".brindle-light{display:none}.brindle-dark{display:inline}" in probe

    # A pseudo-element only the light scheme has is switched off in dark.
    light_only = '.s::before{content:"old"}@media (prefers-color-scheme:dark){.s::before{content:none}}'
    assert light_only in visual_export.sanitise(f"<style>{light_only}</style>")


@pytest.mark.parametrize("case,status", [
    pytest.param("answer", 200, marks=pytest.mark.variant("an option answer must be escaped before inclusion in exported HTML")),
    pytest.param("answer_free_text", 200, marks=pytest.mark.variant("a free-text answer without an option must export safely")),
    pytest.param("answer_secret", 400, marks=pytest.mark.variant("a stored answer containing a secret must block export")),
    ("secret", 400), ("stale", 409), ("unknown", 404),
    pytest.param("answer_without_note", 200, id="test_export_answer_without_note",
                 marks=pytest.mark.variant("reported crash: an option picked with no note (review 0.6.3, finding 6)")),
    ("disabled", 404), ("too_big", 400), ("invalid", 400),
    pytest.param("missing", 400, marks=pytest.mark.variant("a snapshot without HTML must fail validation before export")),
    ("free", 402), ("denied", 403),
    pytest.param("google_docs", 200, marks=pytest.mark.variant("Google Docs export requires an explicitly allowed destination")),
    ("sharepoint", 200),
    pytest.param("plan", 200, marks=pytest.mark.variant("a plan snapshot must export Markdown rather than its HTML rendering")),
    ("plan_disabled", 200), ("wrong_kind", 400),
])
def test_snapshot_checks_identity_revision_secrets_and_destination(db, session, client, monkeypatch, case, status):
    _, boss, _ = session
    v = visuals.publish(db, boss, "Release", "<p>initial</p>", kind="plan" if case.startswith("plan") else "visual")
    body = {"rev": 1, "html": "<p>rendered</p>"}
    if case in {"answer", "answer_free_text", "answer_secret", "answer_without_note"}:
        answer = {"option": None if case == "answer_free_text" else "B",
                  "note": None if case == "answer_without_note" else SECRET if case == "answer_secret" else "ship <it>"}
        db.conn.execute("UPDATE visuals SET answer=? WHERE id=?", (json.dumps(answer), v["id"]))
        db.conn.commit()
    if case == "secret":
        body["html"] = SECRET
    if case == "stale":
        body["rev"] = 0
    if case == "unknown":
        v["id"] = "v_missing"
    if case in {"disabled", "plan_disabled"}:
        monkeypatch.setenv("BRINDLE_VISUALS", "0")
    if case == "too_big":
        body["html"] = "x" * (visuals.MAX_BYTES + 1)
    if case == "missing":
        del body["html"]
    if case == "invalid":
        body["destination"] = "elsewhere"
    if case in {"free", "denied", "google_docs", "sharepoint"}:
        body["destination"] = case if case in {"google_docs", "sharepoint"} else "google_docs"
        if case == "free":
            monkeypatch.setattr(routes_actions, "_docs_entitlement", lambda repo: (_ for _ in ()).throw(
                routes_actions.ServiceError("not_entitled", "Pro required", 402)))
        else:
            monkeypatch.setattr(routes_actions, "docs_org_api", lambda *a, **k: {
                "export_destinations": [] if case == "denied" else [case]})
    if case.startswith("plan") or case == "wrong_kind":
        body["markdown"] = "# Release\n\nShip it."
    got = client.post(f"/v1/visuals/{v['id']}/snapshot", json=body)
    assert got.status_code == status
    assert SECRET not in got.text
    if status == 200:
        content = base64.b64decode(got.json()["content_b64"]).decode()
        if case == "answer":
            assert "B · ship &lt;it&gt;" in content
            assert content.index("B · ship &lt;it&gt;") < content.index("</body>")
        elif case == "answer_free_text":
            assert "<footer>You picked ship &lt;it&gt;</footer>" in content
        elif case.startswith("plan"):
            assert content == body["markdown"] and got.json()["filename"].endswith(".md")
        else:
            assert body["html"] in content and got.json()["mime"] == "text/html"
