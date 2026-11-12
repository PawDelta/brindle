"""Inert, self-contained snapshots for downloads and cloud exports.

The rendered DOM is untrusted, just like the published page. Only explicit
elements and attributes survive; neither CSS nor SVG can fetch anything.
"""

from __future__ import annotations

import base64
import re
from html import escape
from html.parser import HTMLParser

from brindle import visuals
from brindle.db import DB
from brindle.service.errors import ServiceError

TAGS = set(("main section article header footer nav div span p br hr h1 h2 h3 h4 h5 h6 "
            "ul ol li dl dt dd table thead tbody tfoot tr th td caption colgroup col pre code blockquote "
            "strong em b i u s small sub sup mark abbr kbd samp time a details summary figure figcaption img "
            "svg g path rect circle ellipse line polyline polygon text tspan defs marker "
            "lineargradient radialgradient stop").split())
VOID = {"br", "hr", "img", "col"}
DROP = {"script", "title", "iframe", "object", "embed", "template", "math", "foreignobject"}
ATTRS = set(("title alt width height colspan rowspan role aria-label viewbox d x y x1 x2 y1 y2 cx cy r rx ry "
             "points dx dy fill stroke stroke-width stroke-dasharray stroke-dashoffset stroke-linecap "
             "stroke-linejoin stroke-opacity fill-opacity opacity transform offset stop-color stop-opacity "
             "font-size font-weight font-family font-style letter-spacing text-anchor dominant-baseline "
             "refx refy markerwidth markerheight markerunits orient preserveaspectratio gradientunits").split())
# Attributes that may point at an element of the export itself, as url(#id) and nothing else.
REFS = {"fill", "stroke", "marker-start", "marker-mid", "marker-end"}
# SVG names are case sensitive in standalone HTML.
SVG_CASE = {name.lower(): name for name in ("viewBox", "refX", "refY", "markerWidth", "markerHeight", "markerUnits",
                                            "preserveAspectRatio", "gradientUnits")}
SIDES = ("top", "right", "bottom", "left")
CSS = set(("color background background-color background-image background-size background-position background-repeat "
           "font font-size font-weight font-family font-style font-variant-numeric line-height letter-spacing "
           "text-transform text-align text-decoration text-decoration-line text-decoration-color "
           "text-decoration-style text-overflow white-space word-break overflow-wrap vertical-align "
           "display box-sizing visibility margin padding border border-color border-style border-width "
           "border-radius border-collapse border-spacing width height max-width min-width max-height min-height "
           "aspect-ratio overflow overflow-x overflow-y gap row-gap column-gap grid grid-template "
           "grid-template-columns grid-template-rows grid-template-areas grid-auto-flow grid-auto-columns "
           "grid-auto-rows grid-column grid-row grid-column-start grid-column-end grid-row-start grid-row-end "
           "grid-area place-items place-content place-self flex flex-direction flex-wrap flex-flow flex-grow "
           "flex-shrink flex-basis order align-items align-content align-self justify-content justify-items "
           "justify-self position top right bottom left inset z-index box-shadow opacity list-style "
           "list-style-type list-style-position table-layout caption-side fill fill-opacity stroke stroke-width "
           "stroke-dasharray stroke-dashoffset stroke-linecap stroke-linejoin stroke-opacity stop-color "
           "stop-opacity text-anchor dominant-baseline").split()) | {
    f"{box}-{side}" for box in ("margin", "padding", "border") for side in SIDES} | {
    f"border-{side}-{part}" for side in SIDES for part in ("width", "style", "color")} | {
    f"border-{corner}-radius" for corner in ("top-left", "top-right", "bottom-right", "bottom-left")}
# Functions that compute a colour, a length or a gradient from their arguments and fetch nothing.
FUNCTIONS = ("repeat|minmax|rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color|color-mix|calc|min|max|clamp|"
             "fit-content|linear-gradient|radial-gradient|conic-gradient|repeating-linear-gradient|"
             "repeating-radial-gradient")
# A quoted string (a font name, a pseudo-element's text) that can't escape, close the stylesheet or
# hold an at-rule. A value with "url(" anywhere, even inside a string, is refused outright.
STRING = r""""[^"'\\<>@;{}\r\n]*"|'[^"'\\<>@;{}\r\n]*'"""
SCHEME = re.compile(r"@media\s*\(\s*prefers-color-scheme\s*:\s*(light|dark)\s*\)\s*", re.I)
CSP = ("default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; "
       "base-uri 'none'; form-action 'none'")


def _value(prop: str, value: str) -> bool:
    if prop == "content":
        # Only a pseudo-element's literal text, or none: no counters, attributes or images.
        return value == "none" or re.fullmatch(STRING, value) is not None
    if re.search(r"url\s*\(", value, re.I):
        return False
    value = re.sub(STRING, "_", value)
    if not re.fullmatch(r"[\w\s#.,%+\-/*()]+", value, re.ASCII):
        return False
    # Permit layout/color functions (including nested ones), never url(),
    # image-set(), expression(), var(), or future functions outside this list.
    functions = re.sub(rf"(?<![\w-])(?:{FUNCTIONS})\s*\(", "(", value, flags=re.I)
    return not re.search(r"[\w-]\s*\(", functions, re.ASCII)


def _declarations(css: str, pseudo: bool = False) -> str:
    """Presentation values only: no escapes, URLs, variables or fetching functions."""
    css = re.sub(r"/\*.*?(?:\*/|\Z)", "", css, flags=re.S)
    safe = []
    for declaration in css.split(";"):
        prop, _, value = declaration.partition(":")
        prop, value = prop.strip().lower(), value.strip()
        if (prop in CSS or (pseudo and prop == "content")) and _value(prop, value):
            safe.append(f"{prop}:{value}")
    return ";".join(safe)


def _stylesheet(css: str, media: bool = True) -> str:
    """Flat type/class/id rules (and ::before/::after), and those behind a colour-scheme
    @media; discard other at-rules and complex selectors."""
    rules = []
    start = opening = depth = 0
    quote = ""
    escaped = nested = comment = skip = False
    # Scan complete top-level blocks, never promoting a nested rule out of
    # its condition. Strings and escapes cannot close an unsupported block.
    for index, char in enumerate(css):
        if skip:
            skip = False
            continue
        if comment:
            if css.startswith("*/", index):
                comment, skip = False, True
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if css.startswith("/*", index):
            comment = skip = True
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            if depth == 0:
                opening, nested = index, False
            else:
                nested = True
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0:
                    rules.append((css[start:opening], css[opening + 1:index], nested))
            if depth == 0:
                start = index + 1
        elif char == ";" and depth == 0:
            start = index + 1
    safe = []
    for selector, body, nested in rules:
        selector = re.sub(r"/\*.*?(?:\*/|\Z)", "", selector, flags=re.S).strip()
        if nested:
            # One level of light/dark rules, which hold flat rules only.
            scheme = SCHEME.fullmatch(selector)
            inner = _stylesheet(body, media=False) if media and scheme else ""
            if inner:
                safe.append(f"@media (prefers-color-scheme:{scheme.group(1).lower()}){{{inner}}}")
            continue
        plain = re.sub(r"::?(?:before|after)(?=$|[\s,>+~])", "", selector)
        declarations = _declarations(body, pseudo=plain != selector)
        if declarations and re.fullmatch(r"[A-Za-z0-9_#., >+~*\-]+", plain):
            safe.append(f"{selector}{{{declarations}}}")
    return "".join(safe)


class _Snapshot(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.hidden: list[str] = []
        self.styles: list[str] = []
        self.style: list[str] | None = None
        self.root_attrs: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if self.hidden or tag in DROP:
            if tag not in VOID and tag not in {"meta", "link", "input", "embed"}:
                self.hidden.append(tag)
            return
        if tag == "style":
            self.style = []
            return
        if tag not in TAGS and tag not in {"html", "body"}:
            return
        safe = []
        for key, value in attrs:
            if value is None:
                continue
            if key in {"class", "id"}:
                if not re.fullmatch(r"[\w\s-]+", value, re.ASCII):
                    continue
            elif key == "style":
                value = _declarations(value)
            elif key == "src" and tag == "img":
                if not re.fullmatch(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+", value):
                    continue
            elif key in REFS and re.fullmatch(r"url\(#[A-Za-z_][\w-]*\)", value, re.ASCII):
                pass
            elif key not in ATTRS or re.search(r"url|[\\(@]", value, re.I):
                continue
            key = SVG_CASE.get(key, key)
            safe.append(f' {key}="{escape(value, quote=True)}"')
        if tag in {"html", "body"}:
            self.root_attrs.setdefault(tag, "".join(safe))
            return
        self.out.append(f"<{tag}{''.join(safe)}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                del self.hidden[len(self.hidden) - 1 - self.hidden[::-1].index(tag):]
            return
        if tag == "style" and self.style is not None:
            self.styles.append(_stylesheet("".join(self.style)))
            self.style = None
            return
        if tag in self.stack:
            while self.stack:
                top = self.stack.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data):
        if self.hidden:
            return
        if self.style is not None:
            self.style.append(data)
        else:
            self.out.append(escape(data))


def sanitise(html: str, answer: dict | None = None) -> str:
    parser = _Snapshot()
    parser.feed(html)
    parser.close()
    body = "".join(parser.out) + "".join(f"</{tag}>" for tag in reversed(parser.stack))
    if answer:
        picked = f'You picked {escape(answer["option"])} · ' if answer.get("option") is not None else "You picked "
        body += f'<footer>{picked}{escape(answer["note"] or "")}</footer>'
    styles = "".join(parser.styles)
    # No untrusted document wrappers or metadata can precede this policy.
    return (f'<!doctype html><html{parser.root_attrs.get("html", "")}><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{escape(CSP, quote=True)}">'
            f'<style>{styles}</style></head><body{parser.root_attrs.get("body", "")}>{body}</body></html>')


def snapshot(db: DB, visual_id: str, rev: int, html: str, destination: str = "download",
             *, markdown: str | None = None) -> dict:
    """Validate the revision and destination before releasing any export bytes."""
    v = visuals.get(db, visual_id)
    if v["kind"] != "plan":
        visuals.require_enabled()
    visuals._unlocked(v["root_id"])
    if type(rev) is not int or rev != v["rev"]:
        raise ServiceError("stale", "the visual changed; open its new revision", 409)
    if destination not in {"download", "google_docs", "sharepoint"}:
        raise ServiceError("invalid", "unknown export destination")
    if destination != "download":
        from brindle.daemon.routes_actions import docs_org_api

        policy = docs_org_api("GET", "policy", repo=v["repo_root"])
        if destination not in policy.get("export_destinations", ["download"]):
            raise ServiceError("forbidden", "the organisation does not allow this destination", 403)
    content = markdown if markdown is not None else html
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > visuals.MAX_BYTES:
        raise ServiceError("invalid", "the snapshot is missing or too large")
    if markdown is not None and v["kind"] != "plan":
        raise ServiceError("invalid", "only a plan exports markdown")
    answer = v.get("answer")
    visuals._refuse_secrets([("the snapshot", content), ("the title", v["title"]),
                             ("the answer", answer.get("note") if answer else None)])
    if markdown is None:
        content = sanitise(content, answer)
    visuals._refuse_secrets([("the export", content)])
    extension, mime = ("md", "text/markdown") if markdown is not None else ("html", "text/html")
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", v["title"]).strip("-") or "visual"
    return {"filename": f"{name}.{extension}", "mime": mime,
            "content_b64": base64.b64encode(content.encode()).decode()}
