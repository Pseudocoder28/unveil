"""PDF detector.

For every run of text on every page we ask: could a person reading this page actually
see it? Text fails that test when it is

* drawn in "invisible" render mode (Tr 3),
* fully or almost fully transparent,
* too small to read (under 3pt),
* placed outside the page box,
* the same colour as whatever is behind it (white-on-white, the classic résumé trick), or
* painted over by an image or shape.

Whether a colour is "the same as the background" is decided from the rendered page, not
by assuming white paper, so white text on a white image or grey text on a grey box are
caught too.

Also checked: metadata/XMP, annotations and form fields, embedded files, JavaScript and
launch actions, hidden layers, and invisible Unicode in the text stream.
"""

from __future__ import annotations

import re
from collections import defaultdict

import numpy as np
import pymupdf
from lxml import etree

from ..models import Finding, ScanResult, sha256_file
from ..patterns import categories, match_injection
from .common import clip, hidden_text_finding, metadata_finding, unicode_findings

ZOOM = 2.0               # analysis render scale (144 dpi)
TINY_PT = 3.0            # text smaller than this is not human-readable at normal zoom
MIN_OPACITY = 0.1
LOW_CONTRAST = 1.35      # WCAG contrast ratio below which text is effectively invisible

TECHNIQUE_TITLES = {
    "invisible-text": "Text drawn in invisible render mode",
    "transparent-text": "Fully transparent text",
    "hidden-layer-text": "Text on a hidden layer",
    "off-page-text": "Text placed outside the visible page",
    "tiny-text": "Microscopic text (too small to read)",
    "low-contrast-text": "Text coloured to match its background (e.g. white on white)",
    "covered-text": "Text hidden underneath an image or shape",
}


def _rgb255(color, colorspace: int) -> np.ndarray:
    c = tuple(color or (0,))
    if colorspace == 1 or len(c) == 1:
        rgb = (c[0], c[0], c[0])
    elif colorspace == 4 or len(c) == 4:
        cy, m, y, k = c
        rgb = ((1 - cy) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    else:
        rgb = c[:3]
    return np.array([round(v * 255) for v in rgb], dtype=float)


def _luminance(rgb: np.ndarray) -> float:
    def ch(v):
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: np.ndarray, b: np.ndarray) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _hex(rgb: np.ndarray) -> str:
    return "#" + "".join(f"{int(v):02X}" for v in rgb)


def _visibility(img: np.ndarray, rect: pymupdf.Rect, text_rgb: np.ndarray):
    """Return (status, background_rgb). status is 'ok', 'low-contrast' or 'covered'."""
    h, w, _ = img.shape
    x0, y0 = max(0, int(rect.x0)), max(0, int(rect.y0))
    x1, y1 = min(w, int(np.ceil(rect.x1))), min(h, int(np.ceil(rect.y1)))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return "ok", None
    pad = max(2, int(0.35 * (y1 - y0)))
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    outer = img[Y0:Y1, X0:X1]
    mask = np.ones(outer.shape[:2], dtype=bool)
    mask[y0 - Y0:y1 - Y0, x0 - X0:x1 - X0] = False
    ring = outer[mask].astype(float)
    inner = img[y0:y1, x0:x1].reshape(-1, 3).astype(float)
    if inner.size == 0:
        return "ok", None
    bg = np.median(ring if len(ring) else inner, axis=0)

    near_bg = np.mean(np.max(np.abs(inner - bg), axis=1) <= 24)
    if contrast_ratio(text_rgb, bg) < LOW_CONTRAST and near_bg > 0.97:
        return "low-contrast", bg

    # Is the text colour present anywhere inside its own box? If not, something is on top.
    if inner.shape[0] >= 40 and contrast_ratio(text_rgb, bg) >= LOW_CONTRAST:
        ink = np.mean(np.linalg.norm(inner - text_rgb, axis=1) < 90)
        if ink < 0.004:
            return "covered", bg
    return "ok", bg


def _off_layers(doc: pymupdf.Document) -> set[str]:
    try:
        return {v.get("name", "") for v in doc.get_ocgs().values() if not v.get("on", True)}
    except Exception:
        return set()


def _image_coverage(page: pymupdf.Page) -> float:
    area = page.rect.get_area() or 1
    covered = 0.0
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"]) & page.rect
            covered += r.get_area()
    except Exception:
        return 0.0
    return min(1.0, covered / area)


def _xmp_text(xml: str) -> str:
    if not xml:
        return ""
    try:
        root = etree.fromstring(xml.encode("utf-8", "ignore"))
        return " ".join(t.strip() for t in root.itertext() if t and t.strip())
    except Exception:
        return re.sub(r"<[^>]+>", " ", xml)


def scan_pdf(path: str) -> ScanResult:
    res = ScanResult(path=str(path), file_type="pdf", sha256=sha256_file(path))
    doc = pymupdf.open(path)
    if doc.needs_pass and not doc.authenticate(""):
        res.findings.append(Finding("encrypted", "medium", "PDF is password-protected; contents could not be inspected",
                                    location="document"))
        return res
    res.pages = doc.page_count

    _scan_document_level(doc, res)
    off_layers = _off_layers(doc)

    for pno, page in enumerate(doc):
        _scan_page(page, pno, off_layers, res)
    doc.close()
    return res


def _scan_document_level(doc: pymupdf.Document, res: ScanResult) -> None:
    for key, value in (doc.metadata or {}).items():
        if key in ("format", "encryption", "creationDate", "modDate", "trapped"):
            continue
        f = metadata_finding(key, value or "")
        if f:
            res.findings.append(f)
    try:
        xmp = _xmp_text(doc.get_xml_metadata())
    except Exception:
        xmp = ""
    if xmp and categories(xmp):
        res.findings.append(Finding("metadata-injection", "high", "Instruction hidden in XMP metadata",
                                    clip(xmp), "metadata: XMP", patterns=categories(xmp)))

    # Active content and actions
    js_found, launch_found = False, False
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=False)
        except Exception:
            continue
        if not js_found and ("/JavaScript" in obj or re.search(r"/JS\b", obj)):
            js_found = True
            res.findings.append(Finding("javascript", "high", "PDF contains JavaScript",
                                        clip(obj, 400), f"object {xref}",
                                        detail="Scripts are not needed for a résumé or essay and can alter what is shown."))
        if not launch_found and "/Launch" in obj:
            launch_found = True
            res.findings.append(Finding("launch-action", "high", "PDF tries to launch an external program",
                                        clip(obj, 400), f"object {xref}"))

    for i in range(doc.embfile_count()):
        info = doc.embfile_info(i)
        name = info.get("filename") or info.get("name") or f"#{i}"
        sev, cats, preview = "medium", [], ""
        try:
            data = doc.embfile_get(i)
            if len(data) < 2_000_000:
                preview = data.decode("utf-8", errors="ignore")
                cats = categories(preview)
        except Exception:
            pass
        if cats:
            sev = "critical"
        res.findings.append(Finding("embedded-file", sev, f"Embedded file attachment: {name}",
                                    clip(preview, 600) if cats else "", "attachments", patterns=cats,
                                    detail=f"{info.get('size', '?')} bytes. AI tools may read attachments the viewer never opens."))

    off = _off_layers(doc)
    if off:
        res.findings.append(Finding("hidden-layer", "medium", "Document has hidden (switched-off) layers",
                                    ", ".join(sorted(off)), "layers"))


def _span_text(trace: dict) -> str:
    return "".join(chr(c[0]) for c in trace.get("chars", ()) if c[0] >= 0)


def _scan_page(page: pymupdf.Page, pno: int, off_layers: set[str], res: ScanResult) -> None:
    loc = f"page {pno + 1}"
    pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False, colorspace=pymupdf.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    page_rect = page.rect
    scanned = _image_coverage(page) > 0.6

    try:
        traces = page.get_texttrace()
    except Exception:
        traces = []

    hidden: list[tuple[int, str, str, pymupdf.Rect, str]] = []  # seqno, technique, text, bbox, detail
    visible_parts: list[str] = []
    all_parts: list[str] = []

    for t in traces:
        text = _span_text(t)
        if not text.strip():
            continue
        all_parts.append(text)
        bbox = pymupdf.Rect(t["bbox"])
        reasons: list[tuple[str, str]] = []
        redact = False

        if t.get("type") == 3:
            reasons.append(("invisible-text", "render mode 3 (invisible)"))
        opacity = t.get("opacity")
        if opacity is not None and opacity < MIN_OPACITY:
            reasons.append(("transparent-text", f"opacity {opacity:.2f}"))
            redact = True
        if t.get("layer") and t["layer"] in off_layers:
            reasons.append(("hidden-layer-text", f"layer '{t['layer']}' is switched off"))

        eff = t.get("size", 12)
        if bbox.height > 0:
            eff = min(eff, bbox.height)
        if eff < TINY_PT:
            reasons.append(("tiny-text", f"{eff:.2f}pt font"))
            redact = True

        on_page = bbox & page_rect
        if bbox.is_empty or on_page.is_empty or on_page.get_area() < 0.2 * max(bbox.get_area(), 1e-6):
            reasons.append(("off-page-text", f"positioned at {tuple(round(v) for v in bbox)} outside page {tuple(round(v) for v in page_rect)}"))
        elif t.get("type") != 3 and eff >= TINY_PT:
            text_rgb = _rgb255(t.get("color"), t.get("colorspace", 3))
            status, bg = _visibility(img, bbox * ZOOM, text_rgb)
            if status == "low-contrast":
                reasons.append(("low-contrast-text", f"text colour {_hex(text_rgb)} on background {_hex(bg)}"))
                redact = True
            elif status == "covered":
                reasons.append(("covered-text", f"text colour {_hex(text_rgb)} not visible inside its box"))
                redact = True

        if reasons:
            hidden.append((t.get("seqno", 0), reasons[0][0], text, bbox, "; ".join(r for _, r in reasons)))
            if redact:
                res.redact_boxes.setdefault(pno, []).append(tuple(bbox))
        else:
            visible_parts.append(text)

    # A scanned page with an invisible OCR layer on top is normal. Report it once, quietly,
    # unless the OCR layer contains something that looks like an instruction.
    if scanned and hidden and all(tech == "invisible-text" for _, tech, *_ in hidden):
        ocr_text = " ".join(h[2] for h in hidden)
        cats = categories(ocr_text)
        res.findings.append(Finding(
            "ocr-layer", "critical" if cats else "info",
            "Invisible OCR text layer over a scanned page" + (" containing AI instructions" if cats else ""),
            clip(ocr_text, 600) if cats else "", loc, page=pno + 1, patterns=cats,
            detail="Normal for scanned documents; removed by flattening either way."))
        hidden = []

    for group in _group(hidden):
        tech = group[0][1]
        text = " ".join(g[2].strip() for g in group)
        bbox = pymupdf.Rect(group[0][3])
        for g in group[1:]:
            bbox |= g[3]
        details = sorted({g[4] for g in group})
        res.findings.append(hidden_text_finding(
            tech, TECHNIQUE_TITLES[tech], text, loc, page=pno + 1, bbox=tuple(round(v, 1) for v in bbox),
            detail="; ".join(details[:4]) + (" …" if len(details) > 4 else "")))

    visible = " ".join(visible_parts)
    hits = match_injection(visible)
    if hits:
        res.findings.append(Finding(
            "visible-injection-phrase", "medium", "Visible text reads like an instruction to an AI",
            clip("\n".join(f"• {s}" for _, s in hits[:10])), loc, page=pno + 1,
            patterns=sorted({c for c, _ in hits}),
            detail="Visible, so a careful reader could spot it, but an AI may obey it."))
    res.findings.extend(unicode_findings("".join(all_parts), loc, page=pno + 1))

    _scan_annotations(page, pno, res)


def _group(hidden):
    """Merge consecutive hidden spans that use the same technique into one finding."""
    groups: list[list] = []
    for item in sorted(hidden, key=lambda h: h[0]):
        if groups and groups[-1][-1][1] == item[1] and item[0] - groups[-1][-1][0] <= 3:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _scan_annotations(page: pymupdf.Page, pno: int, res: ScanResult) -> None:
    loc = f"page {pno + 1}"
    for annot in page.annots() or []:
        info = annot.info or {}
        content = " ".join(filter(None, [info.get("title", ""), info.get("subject", ""), info.get("content", "")])).strip()
        if not content:
            continue
        hidden_flag = bool(annot.flags & (pymupdf.PDF_ANNOT_IS_HIDDEN | pymupdf.PDF_ANNOT_IS_NO_VIEW))
        cats = categories(content)
        sev = "critical" if cats else ("high" if hidden_flag else "low")
        res.findings.append(Finding(
            "annotation-text", sev,
            ("Hidden comment" if hidden_flag else "Comment") + f" / annotation ({annot.type[1]}) with text",
            clip(content), loc, page=pno + 1, bbox=tuple(round(v, 1) for v in annot.rect), patterns=cats,
            detail="Comment/annotation text is not part of the printed page but is extracted by many AI tools."))
    for w in page.widgets() or []:
        parts = [str(w.field_value or ""), str(getattr(w, "field_label", "") or ""), str(getattr(w, "field_name", "") or "")]
        content = " ".join(p for p in parts if p and p not in ("Off", "None")).strip()
        if not content:
            continue
        cats = categories(content)
        hidden_flag = w.rect.is_empty or not (w.rect & page.rect).get_area() or w.rect.height < TINY_PT
        if cats or hidden_flag:
            res.findings.append(Finding(
                "form-field-text", "critical" if cats else "medium", "Form field containing hidden text",
                clip(content), loc, page=pno + 1, patterns=cats))
