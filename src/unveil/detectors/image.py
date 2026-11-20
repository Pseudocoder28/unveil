"""Image detector: text smuggled in EXIF / PNG text chunks / XMP, and near-invisible
low-contrast writing in the pixels themselves."""

from __future__ import annotations

import numpy as np
from PIL import ExifTags, Image

from ..models import Finding, ScanResult, sha256_file
from .common import metadata_finding

TEXT_EXIF_TAGS = {"ImageDescription", "UserComment", "XPComment", "XPTitle", "XPSubject", "XPKeywords",
                  "XPAuthor", "Artist", "Copyright", "Software", "Make", "Model", "DocumentName"}


def _decode(v) -> str:
    if isinstance(v, bytes):
        for enc in ("utf-16-le", "utf-8", "latin-1"):
            try:
                s = v.decode(enc).strip("\x00 ")
                if s and all(ch.isprintable() or ch in "\n\t" for ch in s):
                    return s
            except UnicodeDecodeError:
                continue
        return ""
    return str(v)


def scan_image(path: str) -> ScanResult:
    res = ScanResult(path=str(path), file_type="image", sha256=sha256_file(path), pages=1)
    with Image.open(path) as im:
        try:
            exif = im.getexif()
            for tag_id, val in exif.items():
                name = ExifTags.TAGS.get(tag_id, str(tag_id))
                if name in TEXT_EXIF_TAGS:
                    f = metadata_finding(f"EXIF {name}", _decode(val), "image metadata")
                    if f:
                        res.findings.append(f)
            ifd = exif.get_ifd(0x8769)
            for tag_id, val in ifd.items():
                name = ExifTags.TAGS.get(tag_id, str(tag_id))
                if name == "UserComment":
                    f = metadata_finding("EXIF UserComment", _decode(val)[8:] if isinstance(val, bytes) else str(val),
                                         "image metadata")
                    if f:
                        res.findings.append(f)
        except Exception:
            pass
        for key, val in (im.info or {}).items():
            if isinstance(val, (str, bytes)) and key not in ("icc_profile", "exif", "dpi", "transparency"):
                f = metadata_finding(f"{im.format} chunk '{key}'", _decode(val), "image metadata")
                if f:
                    res.findings.append(f)
        faint = _faint_marks(im) if im.format not in ("JPEG", "MPO", "WEBP") else 0.0
        if faint:
            res.findings.append(Finding(
                "faint-pixels", "medium", "Near-invisible marks in the image (possible faint hidden text)",
                "", "image", detail=f"{faint:.2%} of pixels differ from the background by only 1–6 levels. "
                "Vision AIs can sometimes read text a human cannot see. Flattening snaps these to the background."))
    return res


def _faint_marks(im: Image.Image) -> float:
    """Fraction of pixels that are *almost* the dominant background colour but not quite."""
    g = np.asarray(im.convert("L").resize((min(im.width, 800), max(1, int(im.height * min(im.width, 800) / im.width)))),
                   dtype=np.int16)
    vals, counts = np.unique(g, return_counts=True)
    bg = int(vals[np.argmax(counts)])
    if counts.max() / g.size < 0.3:
        return 0.0  # photo-like image, no dominant background
    diff = np.abs(g - bg)
    faint = float(np.mean((diff >= 1) & (diff <= 6)))
    strong = float(np.mean(diff > 6))
    # Anti-aliased edges of normal text produce a few faint pixels; deliberate faint
    # writing produces many relative to the clearly visible ink.
    return faint if faint > 0.005 and faint > 0.5 * strong else 0.0
