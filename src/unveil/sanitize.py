"""Flatten a document to screenshots so only what a human can see survives.

Every page is rendered to a picture and a brand-new PDF is built from those pictures.
That throws away the text layer, metadata, annotations, attachments, scripts, layers and
anything placed off-page. Two extra steps close the gaps a plain screenshot leaves:

1. Text the scanner found to be *nearly* invisible (tiny, low-contrast, transparent,
   covered) is removed from the page before it is rendered, because a vision model can
   sometimes read 1pt or #FEFEFE-on-white text that a person cannot.
2. Pixels within a few levels of pure white or pure black are snapped to it, wiping
   faint marks that survive rendering.

Optionally an OCR text layer is added back (needs Tesseract). OCR only reads what is
visibly on the picture, so it cannot reintroduce hidden text.
"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

from .models import ScanResult
from .patterns import strip_invisible


@dataclass
class SanitizeOptions:
    dpi: int = 150
    snap: int = 12            # 0 disables near-white/near-black snapping
    ocr: bool = False         # add an OCR text layer (requires Tesseract)
    image_format: str = "png"  # png (crisp, larger) or jpeg (smaller)
    docx_engine: str = "auto"  # auto | libreoffice | builtin


@dataclass
class SanitizeResult:
    output: str
    pages: int = 0
    notes: list[str] = field(default_factory=list)


def _snap(arr: np.ndarray, snap: int) -> np.ndarray:
    if snap <= 0:
        return arr
    arr = arr.copy()
    near_white = arr.min(axis=2) >= 255 - snap
    near_black = arr.max(axis=2) <= snap
    arr[near_white] = 255
    arr[near_black] = 0
    return arr


def _ocr_available() -> bool:
    from shutil import which
    return which("tesseract") is not None


def flatten_pdf(src: str, dst: str, redact_boxes: dict[int, list] | None = None,
                opts: SanitizeOptions | None = None) -> SanitizeResult:
    opts = opts or SanitizeOptions()
    redact_boxes = redact_boxes or {}
    result = SanitizeResult(output=dst)
    use_ocr = opts.ocr and _ocr_available()
    if opts.ocr and not use_ocr:
        result.notes.append("OCR layer skipped: Tesseract is not installed (brew install tesseract).")

    doc = pymupdf.open(src)
    if doc.needs_pass:
        doc.authenticate("")
    out = pymupdf.open()
    removed = 0
    for pno, page in enumerate(doc):
        boxes = redact_boxes.get(pno, [])
        if boxes:
            for b in boxes:
                r = pymupdf.Rect(b)
                inset = min(r.width, r.height) * 0.08
                page.add_redact_annot(r + (inset, inset, -inset, -inset), fill=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            removed += len(boxes)
        pix = page.get_pixmap(dpi=opts.dpi, alpha=False, annots=True, colorspace=pymupdf.csRGB)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
        arr = _snap(arr, opts.snap)
        img = Image.fromarray(arr, "RGB")

        if use_ocr:
            clean_pix = pymupdf.Pixmap(pymupdf.csRGB, pix.width, pix.height, arr.tobytes(), False)
            clean_pix.set_dpi(opts.dpi, opts.dpi)
            ocr_pdf = pymupdf.open("pdf", clean_pix.pdfocr_tobytes(compress=True, language="eng"))
            out.insert_pdf(ocr_pdf)
            ocr_pdf.close()
            continue

        buf = io.BytesIO()
        if opts.image_format == "jpeg":
            img.save(buf, "JPEG", quality=88, optimize=True)
        else:
            img.save(buf, "PNG", optimize=True)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=buf.getvalue())
    result.pages = doc.page_count
    doc.close()

    out.set_metadata({"producer": "unveil (flattened)", "creator": "", "author": "", "title": "",
                      "subject": "", "keywords": ""})
    out.del_xml_metadata()
    out.save(dst, garbage=4, deflate=True)
    out.close()
    if removed:
        result.notes.append(f"Removed {removed} near-invisible text span(s) before rendering.")
    result.notes.append(f"Rebuilt {result.pages} page(s) as {opts.dpi} dpi images; text layer, metadata, "
                        "comments, attachments and scripts discarded.")
    return result


def sanitize(path: str, scan: ScanResult, out_dir: str | None = None,
             opts: SanitizeOptions | None = None) -> SanitizeResult:
    """Produce the safe version of `path`. Returns where it was written."""
    opts = opts or SanitizeOptions()
    src = Path(path)
    out_dir = Path(out_dir or src.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem + ".unveiled"
    ft = scan.file_type

    if ft == "pdf":
        return flatten_pdf(str(src), str(out_dir / f"{stem}.pdf"), scan.redact_boxes, opts)

    if ft == "docx":
        from .docx_render import docx_to_visible_pdf
        from .detectors.pdf import scan_pdf
        with tempfile.TemporaryDirectory() as tmp:
            mid = os.path.join(tmp, "visible.pdf")
            _, engine = docx_to_visible_pdf(str(src), mid, opts.docx_engine)
            # Second pass: anything still nearly invisible after conversion gets painted out too.
            boxes = scan_pdf(mid).redact_boxes
            res = flatten_pdf(mid, str(out_dir / f"{stem}.pdf"), boxes, opts)
        res.notes.insert(0, "Word file converted with " + (
            "LibreOffice after stripping hidden runs." if engine == "libreoffice" else
            "the built-in visible-text renderer (layout simplified; install LibreOffice for exact layout)."))
        return res

    if ft in ("pptx", "xlsx"):
        from .docx_render import convert_office_with_soffice, find_soffice
        if not find_soffice():
            raise RuntimeError(f"Flattening .{ft} files needs LibreOffice (brew install --cask libreoffice). "
                               "The scan report above is still valid.")
        with tempfile.TemporaryDirectory() as tmp:
            mid = os.path.join(tmp, "converted.pdf")
            convert_office_with_soffice(str(src), mid)
            from .detectors.pdf import scan_pdf
            res = flatten_pdf(mid, str(out_dir / f"{stem}.pdf"), scan_pdf(mid).redact_boxes, opts)
        res.notes.insert(0, f".{ft} converted with LibreOffice. Hidden slides/sheets and notes are not rendered.")
        return res

    if ft == "image":
        with Image.open(src) as im:
            arr = np.asarray(im.convert("RGB"))
        arr = _snap(arr, opts.snap)
        dst = out_dir / f"{stem}.png"
        Image.fromarray(arr, "RGB").save(dst, "PNG")  # fresh file: no EXIF, no text chunks
        return SanitizeResult(str(dst), 1, ["Re-encoded pixels only; all metadata removed; faint pixels snapped."])

    # Text-like formats: emit the visible text with invisible Unicode removed.
    from .detectors.text import read_text, visible_html_text
    raw = read_text(str(src))
    if ft == "html":
        from lxml import html as lhtml
        text, _ = visible_html_text(lhtml.document_fromstring(raw))
    else:
        import re
        text = re.sub(r"<!--.*?-->", "", raw, flags=re.S) if ft == "md" else raw
    dst = out_dir / f"{stem}.txt"
    dst.write_text(strip_invisible(text), encoding="utf-8")
    return SanitizeResult(str(dst), 0, ["Wrote visible text only; hidden elements, comments and invisible "
                                        "Unicode removed."])
