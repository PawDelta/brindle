import base64
import json
from html.parser import HTMLParser

import pytest

from brindle import visuals
from brindle.daemon import routes_actions
from test_visuals import SECRET, client, session  # noqa: F401 - shared visuals fixtures


@pytest.mark.parametrize("hostile", [
    "<script>alert(1)</script>",
    pytest.param('<p onclick="alert(1)">bad</p>', marks=pytest.mark.variant("event handler bypass")),
    pytest.param('<a href="javascript:alert(1)">bad</a>', marks=pytest.mark.variant("URL bypass")),
    pytest.param('<div style="background:url(https://evil.test)">bad</div>', marks=pytest.mark.variant("CSS fetch bypass")),
    pytest.param('<svg><script>alert(1)</script></svg>', marks=pytest.mark.variant("SVG script bypass")),
    '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    pytest.param('<meta http-equiv="refresh" content="0;url=https://evil.test">', marks=pytest.mark.variant("refresh bypass")),
    pytest.param('<html onclick="alert(1)"><head><meta http-equiv="Content-Security-Policy" '
                 'content="default-src *"></head><body onload="alert(1)"></body></html>',
                 marks=pytest.mark.variant("root attributes and metadata bypass")),
    '<img src="https://evil.test/tracker"><img src="data:image/svg+xml;base64,AAAA">',
    '<svg><path fill="url(https://evil.test)" d="M0 0"></path></svg>',
    '<style>@import "https://evil.test";</style>',
    '<style>.selected{background:url(https://evil.test);color:green}</style>',
    pytest.param('<style>.selected{background:u/**/rl(https://evil.test)}</style>',
                 marks=pytest.mark.variant("CSS comment URL bypass")),
    pytest.param('<style>.selected{background:\\75rl(https://evil.test)}</style>',
                 marks=pytest.mark.variant("CSS escape URL bypass")),
    '<style>[data-secret]{color:red;background:url(https://evil.test)}</style>',
    pytest.param('<style>@media screen {.selected{color:red}}</style>',
                 marks=pytest.mark.variant("unsupported nested stylesheet")),
    '<!--bad--><object><p>bad</p></object>',
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


@pytest.mark.parametrize("layout", ["class", "mixed", "roots"])
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


@pytest.mark.parametrize("case,status", [
    ("answer", 200), ("answer_free_text", 200), ("answer_secret", 400), ("secret", 400), ("stale", 409), ("unknown", 404),
    ("disabled", 404), ("too_big", 400), ("invalid", 400), ("missing", 400),
    ("free", 402), ("denied", 403), ("google_docs", 200), ("sharepoint", 200),
    ("plan", 200), ("plan_disabled", 200), ("wrong_kind", 400),
])
def test_snapshot_checks_identity_revision_secrets_and_destination(db, session, client, monkeypatch, case, status):
    _, boss, _ = session
    v = visuals.publish(db, boss, "Release", "<p>initial</p>", kind="plan" if case.startswith("plan") else "visual")
    body = {"rev": 1, "html": "<p>rendered</p>"}
    if case in {"answer", "answer_free_text", "answer_secret"}:
        answer = {"option": None if case == "answer_free_text" else "B",
                  "note": SECRET if case == "answer_secret" else "ship <it>"}
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
