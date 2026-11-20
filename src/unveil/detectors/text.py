"""Plain text, Markdown, CSV/JSON and HTML detector."""

from __future__ import annotations

import re

from lxml import etree, html as lhtml

from ..models import Finding, ScanResult, sha256_file
from ..patterns import categories
from .common import clip, hidden_text_finding, visible_text_findings

HIDDEN_CSS = [
    (re.compile(r"display\s*:\s*none", re.I), "display:none"),
    (re.compile(r"visibility\s*:\s*hidden", re.I), "visibility:hidden"),
    (re.compile(r"opacity\s*:\s*0(\.0\d*)?\s*(;|$)", re.I), "opacity:0"),
    (re.compile(r"font-size\s*:\s*(0|[0-2](\.\d+)?)(px|pt)?\s*(;|$)", re.I), "font-size ~0"),
    (re.compile(r"color\s*:\s*(#fff\b|#ffffff\b|white\b|transparent\b|rgba\([^)]*,\s*0\s*\))", re.I), "white/transparent colour"),
    (re.compile(r"(left|top|text-indent)\s*:\s*-\d{3,}", re.I), "moved off-screen"),
    (re.compile(r"clip\s*:\s*rect\(\s*0", re.I), "clipped to nothing"),
    (re.compile(r"(width|height)\s*:\s*0(px)?\s*;.*overflow\s*:\s*hidden", re.I), "zero-size box"),
]


def read_text(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def scan_text(path: str) -> ScanResult:
    ext = path.lower().rsplit(".", 1)[-1]
    if ext in ("html", "htm", "xhtml"):
        return scan_html(path)
    res = ScanResult(path=str(path), file_type=ext if ext in ("md", "csv", "json", "txt") else "text",
                     sha256=sha256_file(path))
    text = read_text(path)
    if ext == "md":
        # Markdown comments and link reference definitions do not render.
        for m in re.finditer(r"<!--(.*?)-->", text, re.S):
            res.findings.append(hidden_text_finding("html-comment", "Hidden Markdown/HTML comment", m.group(1), "comment"))
        for m in re.finditer(r"^\s*\[[^\]]+\]:\s*\S+\s+[\"'(](.+)[\"')]\s*$", text, re.M):
            if categories(m.group(1)):
                res.findings.append(hidden_text_finding("link-title", "Instruction in a hidden Markdown link title", m.group(1), "link definition"))
    visible = re.sub(r"<!--.*?-->", " ", text, flags=re.S) if ext == "md" else text
    res.findings.extend(visible_text_findings(visible, "text"))
    if visible != text:
        from .common import unicode_findings
        hidden_only = "".join(re.findall(r"<!--.*?-->", text, flags=re.S))
        res.findings.extend(f for f in unicode_findings(hidden_only, "comment")
                            if f.technique not in {x.technique for x in res.findings})
    return res


def visible_html_text(root) -> tuple[str, list[tuple[str, str, str]]]:
    """Return (visible text, [(technique, text, detail)] hidden pieces)."""
    hidden: list[tuple[str, str, str]] = []
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            if isinstance(el, etree._Comment) and (el.text or "").strip():
                hidden.append(("html-comment", el.text, "HTML comment"))
            continue
        tag = el.tag.lower()
        if tag in ("script", "style", "template", "noscript"):
            if tag in ("template", "noscript") and el.text_content().strip():
                hidden.append(("hidden-element", el.text_content(), f"<{tag}> content"))
            el.drop_tree() if el.getparent() is not None else None
            continue
        if tag == "meta" and el.get("content") and categories(el.get("content")):
            hidden.append(("metadata-injection", el.get("content"), f"<meta name={el.get('name')}>"))
        style = el.get("style") or ""
        reason = "hidden attribute" if el.get("hidden") is not None else None
        for rx, label in HIDDEN_CSS:
            if rx.search(style):
                reason = label
                break
        if reason and el.text_content().strip() and el.getparent() is not None:
            hidden.append(("hidden-element", el.text_content(), reason))
            el.drop_tree()
    for el in root.iter():
        if isinstance(el, etree._Comment) and el.getparent() is not None:
            el.drop_tree()
    text = root.text_content() if hasattr(root, "text_content") else ""
    return re.sub(r"\n\s*\n+", "\n\n", text), hidden


def scan_html(path: str) -> ScanResult:
    res = ScanResult(path=str(path), file_type="html", sha256=sha256_file(path))
    raw = read_text(path)
    root = lhtml.document_fromstring(raw)
    visible, hidden = visible_html_text(root)
    titles = {"html-comment": "Hidden HTML comment", "hidden-element": "Element hidden with CSS/HTML",
              "metadata-injection": "Instruction in a <meta> tag"}
    for tech, text, detail in hidden:
        f = hidden_text_finding(tech, titles[tech], text, "html", detail=detail)
        if tech == "html-comment" and f.severity == "high":
            f.severity = "low"  # comments are common in exported HTML; only flag hard when they instruct
        res.findings.append(f)
    res.findings.extend(visible_text_findings(visible, "visible text"))
    # invisible unicode anywhere in the source
    from .common import unicode_findings
    if not any(f.technique in ("tag-smuggling", "zero-width", "bidi-control", "variation-selector-smuggling") for f in res.findings):
        res.findings.extend(unicode_findings(raw, "html source"))
    return res
