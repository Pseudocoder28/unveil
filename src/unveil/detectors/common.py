"""Helpers every format detector uses to turn raw observations into Findings."""

from __future__ import annotations

from ..models import Finding
from ..patterns import categories, match_injection, scan_invisible_unicode, strip_invisible

UNICODE_TITLES = {
    "tag-smuggling": "Invisible Unicode tag characters (ASCII smuggling)",
    "variation-selector-smuggling": "Data hidden in Unicode variation selectors",
    "zero-width": "Zero-width characters hidden in text",
    "bidi-control": "Bidirectional control characters (text can display differently than it reads)",
}
UNICODE_SEVERITY = {
    "tag-smuggling": "critical",
    "variation-selector-smuggling": "high",
    "zero-width": "medium",
    "bidi-control": "medium",
}


def clip(s: str, n: int = 2000) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + f" … [+{len(s) - n} chars]"


def hidden_text_finding(
    technique: str,
    title: str,
    text: str,
    location: str,
    *,
    page: int | None = None,
    bbox=None,
    detail: str = "",
) -> Finding:
    """Text a human cannot see but an AI will read. Severity scales with what it says."""
    clean = strip_invisible(text).strip()
    cats = categories(clean)
    words = len(clean.split())
    if cats:
        sev = "critical"
    elif sum(ch.isalnum() for ch in clean) < 3:
        sev = "low"
    else:
        sev = "high"
    if not cats and words >= 25:
        detail = (detail + "; " if detail else "") + f"{words} hidden words (possible keyword stuffing)"
    return Finding(technique, sev, title, clip(clean), location, page=page, bbox=bbox,
                   patterns=cats, detail=detail)


def visible_text_findings(text: str, location: str, page: int | None = None) -> list[Finding]:
    """Injection-like phrases in normally visible text, plus invisible Unicode inside it."""
    out: list[Finding] = []
    hits = match_injection(text)
    if hits:
        cats = sorted({c for c, _ in hits})
        snippets = "\n".join(f"• {s}" for _, s in hits[:10])
        out.append(Finding(
            "visible-injection-phrase", "medium",
            "Visible text reads like an instruction to an AI",
            clip(snippets), location, page=page, patterns=cats,
            detail="Visible, so a careful human reader could notice it, but an AI may obey it.",
        ))
    out.extend(unicode_findings(text, location, page))
    return out


def unicode_findings(text: str, location: str, page: int | None = None) -> list[Finding]:
    out = []
    for hit in scan_invisible_unicode(text):
        sev = UNICODE_SEVERITY[hit.kind]
        cats = categories(hit.decoded) if hit.decoded else []
        if cats:
            sev = "critical"
        detail = f"{hit.count} characters"
        out.append(Finding(
            hit.kind, sev, UNICODE_TITLES[hit.kind],
            clip(hit.decoded) if hit.decoded else "", location, page=page,
            patterns=cats, detail=detail + ("; decoded hidden message shown" if hit.decoded else ""),
        ))
    return out


def metadata_finding(field: str, value: str, location_prefix: str = "metadata") -> Finding | None:
    value = (value or "").strip()
    if not value:
        return None
    cats = categories(value)
    if cats:
        return Finding("metadata-injection", "high", f"Instruction hidden in document metadata ({field})",
                       clip(value), f"{location_prefix}: {field}", patterns=cats)
    # Long free-text metadata is unusual and is read by some AI pipelines.
    if len(value) > 200:
        return Finding("metadata-text", "low", f"Unusually long metadata field ({field})",
                       clip(value), f"{location_prefix}: {field}")
    return Finding("metadata", "info", f"Metadata field {field}", clip(value, 300),
                   f"{location_prefix}: {field}")
