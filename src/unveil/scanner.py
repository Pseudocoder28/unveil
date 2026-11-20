"""Pick the right detector for a file."""

from __future__ import annotations

import zipfile
from pathlib import Path

from .models import Finding, ScanResult, sha256_file

PDF_EXT = {".pdf"}
OOXML_EXT = {".docx", ".docm", ".dotx", ".pptx", ".pptm", ".xlsx", ".xlsm"}
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".html", ".htm", ".xhtml", ".rtf", ".xml"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED = PDF_EXT | OOXML_EXT | TEXT_EXT | IMAGE_EXT


def sniff(path: Path) -> str:
    head = path.open("rb").read(8)
    ext = path.suffix.lower()
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK") and zipfile.is_zipfile(path):
        return "ooxml"
    if ext in IMAGE_EXT or head[:4] in (b"\x89PNG", b"GIF8") or head[:3] == b"\xff\xd8\xff":
        return "image"
    if ext in PDF_EXT:
        return "pdf"
    if ext in OOXML_EXT:
        return "ooxml"
    return "text"


def scan_file(path: str | Path) -> ScanResult:
    path = Path(path)
    kind = sniff(path)
    try:
        if kind == "pdf":
            from .detectors.pdf import scan_pdf
            res = scan_pdf(str(path))
        elif kind == "ooxml":
            from .detectors.ooxml import scan_ooxml
            res = scan_ooxml(str(path))
        elif kind == "image":
            from .detectors.image import scan_image
            res = scan_image(str(path))
        else:
            from .detectors.text import scan_text
            res = scan_text(str(path))
    except Exception as exc:  # a malformed file is itself worth reporting
        res = ScanResult(path=str(path), file_type=kind, sha256=sha256_file(path))
        res.findings.append(Finding("parse-error", "medium", "File could not be fully parsed",
                                    f"{type(exc).__name__}: {exc}", "file",
                                    detail="Malformed files can hide content from scanners. Flatten before use."))
    if path.suffix.lower() in (".docx", ".pdf") and kind not in ("ooxml", "pdf"):
        res.findings.append(Finding("extension-mismatch", "medium",
                                    f"File extension {path.suffix} does not match its real format ({kind})",
                                    location="file"))
    return res
