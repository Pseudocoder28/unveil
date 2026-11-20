"""Console, JSON and HTML reports."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import ScanResult

TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))

VERDICT_TEXT = {
    "malicious": "Hidden AI instructions found",
    "suspicious": "Suspicious content found",
    "clean": "No hidden instructions found",
}


# --------------------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------------------

def _c(code: str, s: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return s
    return f"\033[{code}m{s}\033[0m"


SEV_COLOR = {"critical": "1;97;41", "high": "1;31", "medium": "33", "low": "36", "info": "2"}
VERDICT_COLOR = {"malicious": "1;97;41", "suspicious": "1;30;43", "clean": "1;30;42"}


def console(result: ScanResult, sender: dict | None = None, sanitized=None, show_info: bool = False) -> str:
    out = []
    name = Path(result.path).name
    out.append("")
    out.append(_c(VERDICT_COLOR[result.verdict], f" {result.verdict.upper()} ") + f"  {name}  "
               + _c("2", f"risk {result.score}/100 · {result.file_type}"
                    + (f" · {result.pages} page(s)" if result.pages else "")))
    out.append("  " + VERDICT_TEXT[result.verdict])
    if sender:
        st = sender["status"]
        line = f"  Sender {sender['sender']}: {st.upper()}"
        line += f" · {sender['scans']} file(s) scanned, {sender['malicious']} with hidden instructions"
        out.append(_c("1;31" if st == "blocked" else "33" if st == "watch" else "2", line))
        if st == "blocked":
            out.append(_c("1;31", "  ⚠ This sender is on your blocklist."))
        elif st == "watch" and result.verdict == "malicious":
            out.append(_c("33", f"  Block them with:  unveil senders block \"{sender['sender']}\""))
    out.append("")
    shown = [f for f in result.sorted_findings() if show_info or f.severity != "info"]
    for f in shown:
        tag = _c(SEV_COLOR[f.severity], f" {f.severity.upper():8} ")
        out.append(f"{tag} {f.title}  " + _c("2", f"[{f.location}]"))
        if f.text:
            for line in f.text.splitlines()[:6]:
                out.append(_c("2", "           │ ") + line[:160])
        if f.patterns:
            out.append(_c("2", "           ↳ looks like: ") + ", ".join(p.replace("-", " ") for p in f.patterns))
        if f.detail:
            out.append(_c("2", f"           ↳ {f.detail[:200]}"))
    hidden_info = len(result.findings) - len(shown)
    if hidden_info:
        out.append(_c("2", f"  ({hidden_info} informational item(s) hidden; use --all to show)"))
    if not shown:
        out.append(_c("2", "  Nothing hidden detected."))
    if sanitized:
        out.append("")
        out.append(_c("1;32", "  ✔ Safe copy: ") + sanitized.output)
        for n in sanitized.notes:
            out.append(_c("2", f"    {n}"))
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# JSON / HTML
# --------------------------------------------------------------------------------------

def to_json(result: ScanResult, sender: dict | None = None, sanitized=None) -> str:
    d = result.to_dict()
    if sender:
        d["sender"] = sender
    if sanitized:
        d["sanitized"] = {"output": sanitized.output, "notes": sanitized.notes}
    return json.dumps(d, indent=2, ensure_ascii=False)


def evidence_images(result: ScanResult, max_pages: int = 10, dpi: int = 90) -> dict[int, str]:
    """For PDFs: page images with every hidden-text location boxed in red. {page: data URI}."""
    if result.file_type != "pdf":
        return {}
    import pymupdf
    pages: dict[int, list] = {}
    for f in result.findings:
        if f.page and f.bbox and f.severity != "info":
            pages.setdefault(f.page, []).append((f.bbox, f.severity))
    if not pages:
        return {}
    out = {}
    doc = pymupdf.open(result.path)
    for pno in sorted(pages)[:max_pages]:
        page = doc[pno - 1]
        shape = page.new_shape()
        for bbox, sev in pages[pno]:
            r = pymupdf.Rect(bbox) & page.rect
            if r.is_empty:  # off-page text: mark the nearest edge
                b = pymupdf.Rect(bbox)
                r = pymupdf.Rect(min(max(b.x0, 0), page.rect.width - 12), min(max(b.y0, 0), page.rect.height - 12), 0, 0)
                r.x1, r.y1 = r.x0 + 12, r.y0 + 12
            r = r + (-2, -2, 2, 2)
            shape.draw_rect(r)
            shape.finish(color=(0.86, 0.1, 0.1), fill=(1, 0.2, 0.2), fill_opacity=0.18, width=1.4)
        shape.commit()
        png = page.get_pixmap(dpi=dpi).tobytes("png")
        out[pno] = "data:image/png;base64," + base64.b64encode(png).decode()
    doc.close()
    return out


def html(result: ScanResult, sender: dict | None = None, sanitized=None, evidence: bool = True) -> str:
    tpl = _env.get_template("report.html")
    return tpl.render(**context(result, sender, sanitized, evidence))


def context(result: ScanResult, sender: dict | None = None, sanitized=None, evidence: bool = True) -> dict:
    return {
        "r": result,
        "name": Path(result.path).name,
        "verdict_text": VERDICT_TEXT[result.verdict],
        "findings": result.sorted_findings(),
        "counts": result.counts(),
        "sender": sender,
        "sanitized": sanitized,
        "evidence": evidence_images(result) if evidence else {},
    }
