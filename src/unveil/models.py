"""Data types shared by every detector, the sanitizer and the reports."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]
SEVERITY_WEIGHT = {"info": 0, "low": 5, "medium": 15, "high": 35, "critical": 60}


@dataclass
class Finding:
    """One suspicious thing found in a file."""

    technique: str          # short machine id, e.g. "white-text", "tiny-text", "metadata"
    severity: str           # info | low | medium | high | critical
    title: str              # one-line human explanation
    text: str = ""          # the hidden / suspicious content itself
    location: str = ""      # "page 2", "word/document.xml paragraph 14", "metadata: Subject"
    page: int | None = None  # 1-based page for PDFs
    bbox: tuple[float, float, float, float] | None = None  # PDF points, for evidence boxes
    patterns: list[str] = field(default_factory=list)      # injection pattern categories matched
    detail: str = ""        # extra technical detail

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.index(self.severity)


@dataclass
class ScanResult:
    path: str
    file_type: str
    sha256: str
    findings: list[Finding] = field(default_factory=list)
    pages: int | None = None
    notes: list[str] = field(default_factory=list)
    # Rectangles (page index 0-based -> list of bbox) the sanitizer must paint out before
    # rasterizing: text that is only *nearly* invisible could survive a screenshot.
    redact_boxes: dict[int, list[tuple[float, float, float, float]]] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return min(100, sum(SEVERITY_WEIGHT[f.severity] for f in self.findings))

    @property
    def verdict(self) -> str:
        if any(f.severity == "critical" for f in self.findings) or self.score >= 60:
            return "malicious"
        if self.score >= 15:
            return "suspicious"
        return "clean"

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in SEVERITY_ORDER}
        for f in self.findings:
            out[f.severity] += 1
        return out

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (-f.rank, f.page or 0))

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("redact_boxes", None)
        d["score"] = self.score
        d["verdict"] = self.verdict
        d["counts"] = self.counts()
        d["findings"] = [asdict(f) for f in self.sorted_findings()]
        return d


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
