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


def _snap_lut(snap: int) -> np.ndarray:
    lut = np.arange(256, dtype=np.uint8)
    if snap > 0:
        lut[: snap + 1] = 0
        lut[255 - snap:] = 255
    return lut


def _snap(arr: np.ndarray, snap: int) -> np.ndarray:
    """Push every channel within `snap` levels of 0/255 to the extreme. A table lookup is
    ~10x faster than the per-pixel min/max test and removes faint marks equally well."""
    return _snap_lut(snap)[arr] if snap > 0 else arr


def _ocr_available() -> bool:
    from shutil import which
    return which("tesseract") is not None


def _is_grayscale(arr: np.ndarray, tol: int = 6) -> bool:
    """True when the page has no real colour (sampled on a 2x2 grid for speed)."""
    s = arr[::2, ::2].astype(np.int16)
    return int(np.abs(s[..., 0] - s[..., 1]).max()) <= tol and int(np.abs(s[..., 1] - s[..., 2]).max()) <= tol


def _palette_png(arr: np.ndarray, colors: int = 64, max_error: float = 2.5) -> bytes | None:
    """Documents with a little colour (a logo, blue headings) fit in a small palette, which
    stores ~45% smaller than full RGB. Returns None when the page is photo-like and a
    palette would visibly change it."""
    import io

    q = Image.fromarray(arr, "RGB").quantize(colors, method=Image.Quantize.FASTOCTREE)
    back = np.asarray(q.convert("RGB"))[::4, ::4].astype(np.int16)
    if float(np.abs(back - arr[::4, ::4]).mean()) > max_error:
        return None
    buf = io.BytesIO()
    q.save(buf, "PNG", compress_level=6)
    return buf.getvalue()


def _render(page: pymupdf.Page, dpi: int) -> pymupdf.Pixmap:
    return page.get_pixmap(dpi=dpi, alpha=False, annots=True, colorspace=pymupdf.csRGB)


def _flatten_chunk(src: str, pnos: list[int], redact_boxes: dict, opts: SanitizeOptions,
                   self_check: bool, use_ocr: bool) -> tuple[bytes, int]:
    """Worker: rasterize a range of pages into a small PDF. Returns (pdf bytes, spans removed)."""
    from .detectors.pdf import page_redact_boxes

    doc = pymupdf.open(src)
    if doc.needs_pass:
        doc.authenticate("")
    out = pymupdf.open()
    removed = 0
    lut = _snap_lut(opts.snap)
    for pno in pnos:
        page = doc[pno]
        boxes = list(redact_boxes.get(pno, []))
        pix = None
        if self_check and not boxes:
            # One render serves both purposes: look for near-invisible text in it, and if
            # there is none (the usual case) it is already the image we keep.
            pix = _render(page, opts.dpi)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            boxes = page_redact_boxes(page, img, opts.dpi / 72)
            if boxes:
                pix = None
        if boxes:
            for b in boxes:
                r = pymupdf.Rect(b)
                inset = min(r.width, r.height) * 0.08
                page.add_redact_annot(r + (inset, inset, -inset, -inset), fill=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            removed += len(boxes)
        if pix is None:
            pix = _render(page, opts.dpi)

        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        if opts.snap > 0:
            arr = lut[arr]
        if _is_grayscale(arr):
            # Black-and-white pages: one channel instead of three (≈3x smaller, faster).
            data, cs = np.ascontiguousarray(arr[..., 1]), pymupdf.csGRAY
        else:
            data, cs = np.ascontiguousarray(arr), pymupdf.csRGB
        clean = pymupdf.Pixmap(cs, pix.width, pix.height, data.tobytes(), False)
        clean.set_dpi(opts.dpi, opts.dpi)

        if use_ocr:
            ocr_pdf = pymupdf.open("pdf", clean.pdfocr_tobytes(compress=True, language="eng"))
            out.insert_pdf(ocr_pdf)
            ocr_pdf.close()
            continue
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        if opts.image_format == "jpeg":
            new.insert_image(new.rect, stream=clean.tobytes("jpeg", jpg_quality=85))
        elif cs is pymupdf.csRGB and (palette := _palette_png(arr)) is not None:
            new.insert_image(new.rect, stream=palette)
        else:
            new.insert_image(new.rect, pixmap=clean)  # stored Flate-compressed by MuPDF
    doc.close()
    data = out.tobytes(deflate=True)
    out.close()
    return data, removed


def flatten_pdf(src: str, dst: str, redact_boxes: dict[int, list] | None = None,
                opts: SanitizeOptions | None = None, self_check: bool = False) -> SanitizeResult:
    """Rasterize every page of `src` into a new image-only PDF at `dst`.

    redact_boxes: near-invisible text found by the scanner, painted out before rendering.
    self_check: look for near-invisible text in the render itself (for PDFs the scanner
    never saw, e.g. a Word file converted on the fly).
    """
    from .parallel import page_chunks, run_chunks

    opts = opts or SanitizeOptions()
    redact_boxes = redact_boxes or {}
    result = SanitizeResult(output=dst)
    use_ocr = opts.ocr and _ocr_available()
    if opts.ocr and not use_ocr:
        result.notes.append("OCR layer skipped: Tesseract is not installed (brew install tesseract).")

    with pymupdf.open(src) as doc:
        n = doc.page_count
    out = pymupdf.open()
    removed = 0
    for part, rm in run_chunks(_flatten_chunk, src, page_chunks(n), redact_boxes, opts, self_check, use_ocr):
        with pymupdf.open("pdf", part) as chunk:
            out.insert_pdf(chunk)
        removed += rm
    result.pages = n

    out.set_metadata({"producer": "unveil (flattened)", "creator": "", "author": "", "title": "",
                      "subject": "", "keywords": ""})
    out.del_xml_metadata()
    out.save(dst, garbage=1, deflate=True)
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
        with tempfile.TemporaryDirectory() as tmp:
            mid = os.path.join(tmp, "visible.pdf")
            _, engine = docx_to_visible_pdf(str(src), mid, opts.docx_engine)
            # Anything still nearly invisible after conversion is painted out too, checked on
            # the same render that becomes the output page (no separate scan pass).
            res = flatten_pdf(mid, str(out_dir / f"{stem}.pdf"), None, opts, self_check=True)
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
            res = flatten_pdf(mid, str(out_dir / f"{stem}.pdf"), None, opts, self_check=True)
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
