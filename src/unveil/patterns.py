"""Prompt-injection phrase patterns and invisible-Unicode ("ASCII smuggling") detection.

These run on *every* piece of text a detector pulls out, hidden or visible. Hidden text
is suspicious on its own; hidden text that also reads like an instruction to an AI is
treated as critical.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_F = re.IGNORECASE | re.MULTILINE

# (category, regex). Kept deliberately specific so ordinary résumé / essay wording
# ("acted as a liaison", "operating system: Linux") does not trip them.
INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction-override", re.compile(
        r"\b(ignore|disregard|forget|override|bypass|skip)\b[^.\n]{0,40}?"
        r"\b(previous|prior|above|earlier|all|any|preceding|other|your|the)\b[^.\n]{0,30}?"
        r"\b(instructions?|prompts?|rules|guidelines|directions|criteria|context|programming)\b", _F)),
    ("instruction-override", re.compile(r"\b(new|updated|real|actual) (instructions?|rules|task|objective)\s*:", _F)),
    ("addresses-the-ai", re.compile(
        r"\b(if|when) (you are|you're) (an? |the )?(ai|a\.i\.|llm|large language model|language model|"
        r"assistant|chat ?bot|gpt|automated|machine|model)\b", _F)),
    ("addresses-the-ai", re.compile(
        r"\b(note|message|instructions?|reminder) (to|for) (the |any )?(ai|llm|language model|assistant|"
        r"chat ?gpt|gpt|claude|gemini|copilot|bard|automated (reviewer|screener|system|grader)|"
        r"reviewer model|screening (tool|software|system)|ats)\b", _F)),
    ("addresses-the-ai", re.compile(
        r"\b(dear|hey|hello|hi|attention|to) (ai|llm|chat ?gpt|gpt-?\d*|claude|gemini|copilot|assistant|"
        r"language model|large language model)\b", _F)),
    ("role-hijack", re.compile(
        r"\byou are (now )?(an? |the )?(helpful |expert |friendly )?(ai|assistant|language model|llm|"
        r"recruiter|hiring manager|grader|evaluator|reviewer|screener|teaching assistant|professor)\b", _F)),
    ("biased-evaluation", re.compile(
        r"\b(rate|rank|score|grade|evaluate|assess|mark|recommend|consider|treat)\b[^.\n]{0,40}?"
        r"\b(candidate|applicant|resume|résumé|cv|student|essay|submission|paper|assignment|this person|"
        r"this document|this proposal|this application)\b[^.\n]{0,40}?"
        r"\b(highly|highest|top|best|excellent|perfect|10\s*/\s*10|100\s*%|a\+|full marks|"
        r"strong(ly)? (hire|recommend|match)|exceptional|qualified|favou?rabl[ey])", _F)),
    ("biased-evaluation", re.compile(
        r"\b(this|the) (candidate|applicant|student|author|submission|writer|applicant's resume) "
        r"(is|would be|will be) (an? |the )?(perfect|ideal|exceptional|excellent|outstanding|best|top|"
        r"strongest|most qualified|great)( fit| match| choice| candidate| hire)?\b", _F)),
    ("biased-evaluation", re.compile(
        r"\b(strongly )?(recommend|advance|shortlist|hire|accept|approve|pass|select|interview)\b[^.\n]{0,20}?"
        r"\b(this|the) (candidate|applicant|student|submission|person|individual)\b", _F)),
    ("biased-evaluation", re.compile(
        r"\b(give|award|assign|grant)\b[^.\n]{0,30}?\b(full marks|top marks|highest (score|grade|rating|mark)|"
        r"an? a\+|a grade of a|10\s*/\s*10|100\s*/\s*100|perfect score|maximum (score|points|marks))", _F)),
    ("concealment", re.compile(
        r"\bdo not (mention|reveal|disclose|tell|say|state|reference|acknowledge|repeat|quote)\b[^.\n]{0,40}?"
        r"\b(this|these|instructions?|prompt|that you|the (hidden|above|following))\b", _F)),
    ("concealment", re.compile(r"\b(hidden|secret|invisible) (instructions?|prompt|message|text)\b", _F)),
    ("prompt-syntax", re.compile(
        r"<\|?\s*(im_start|im_end|system|endoftext|eot_id|start_header_id|end_header_id)\s*\|?>", _F)),
    ("prompt-syntax", re.compile(r"\[/?(INST|SYS)\]|<</?SYS>>", re.MULTILINE)),
    ("prompt-syntax", re.compile(r"###\s*(system|instruction|assistant|response)\b|\bsystem prompt\s*:", _F)),
    ("prompt-syntax", re.compile(r"\b(jailbreak|DAN mode|developer mode (enabled|on))\b", _F)),
    ("data-exfiltration", re.compile(
        r"\b(send|forward|email|e-mail|post|upload|leak|exfiltrate|transmit)\b[^.\n]{0,40}?\b(to|at)\b[^.\n]{0,20}?"
        r"(https?://|www\.|[\w.+-]+@[\w-]+\.[\w.]+)", _F)),
    ("data-exfiltration", re.compile(r"!\[[^\]]*\]\(\s*https?://[^)\s]*\?[^)]*\)", _F)),
    ("output-control", re.compile(
        r"\b(respond|reply|answer|output|print|say|begin your (response|answer)|start your (response|answer))\b"
        r"[^.\n]{0,25}?\bonly\b[^.\n]{0,20}?\bwith\b", _F)),
    ("output-control", re.compile(
        r"\bin your (summary|response|answer|evaluation|review|assessment|report),? "
        r"(always |be sure to |you must )?(mention|state|say|include|emphasi[sz]e|highlight|conclude)", _F)),
]


def match_injection(text: str) -> list[tuple[str, str]]:
    """Return (category, matched snippet) for every injection pattern found in text."""
    if not text or not text.strip():
        return []
    norm = normalize_for_matching(text)
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cat, rx in INJECTION_PATTERNS:
        for m in rx.finditer(norm):
            snippet = m.group(0).strip()
            key = f"{cat}:{snippet.lower()}"
            if key not in seen:
                seen.add(key)
                hits.append((cat, snippet))
    # Drop snippets fully contained in a longer snippet ("to AI" inside "Note to AI").
    return [h for h in hits if not any(h[1] != o[1] and h[1].lower() in o[1].lower() for o in hits)]


def categories(text: str) -> list[str]:
    return sorted({c for c, _ in match_injection(text)})


def normalize_for_matching(text: str) -> str:
    """Undo common evasion tricks before pattern matching: invisible chars, fullwidth
    letters, and letter-by-letter spacing such as 'i g n o r e'."""
    text = strip_invisible(text)
    text = unicodedata.normalize("NFKC", text)
    # Collapse "i g n o r e  a l l" style spacing (5+ single letters separated by single spaces).
    text = re.sub(r"\b(?:\w ){4,}\w\b", lambda m: m.group(0).replace(" ", ""), text)
    return re.sub(r"[ \t]+", " ", text)


# --------------------------------------------------------------------------------------
# Invisible Unicode
# --------------------------------------------------------------------------------------

ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF, 0x180E, 0x00AD}
BIDI = {0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200E, 0x200F}


def _is_tag(cp: int) -> bool:
    return 0xE0000 <= cp <= 0xE007F


def _vs_value(cp: int) -> int | None:
    if 0xFE00 <= cp <= 0xFE0F:
        return cp - 0xFE00
    if 0xE0100 <= cp <= 0xE01EF:
        return cp - 0xE0100 + 16
    return None


def strip_invisible(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if cp in ZERO_WIDTH or cp in BIDI or _is_tag(cp) or _vs_value(cp) is not None:
            continue
        out.append(ch)
    return "".join(out)


@dataclass
class UnicodeHit:
    kind: str        # "tag-smuggling" | "variation-selector-smuggling" | "zero-width" | "bidi-control"
    count: int
    decoded: str = ""


def scan_invisible_unicode(text: str) -> list[UnicodeHit]:
    """Find invisible characters that can carry a hidden message an AI will still read.

    * Unicode "tag" characters (U+E0000..E007F) mirror ASCII and render as nothing;
      LLM tokenizers still see them. We decode the hidden ASCII.
    * Runs of variation selectors can encode arbitrary bytes after a single visible
      character ("emoji smuggling"). We decode them as UTF-8.
    * Zero-width and bidi-control characters are reported when there are enough of them
      to look deliberate.
    """
    if not text:
        return []
    hits: list[UnicodeHit] = []

    tag_chars = [ord(c) for c in text if _is_tag(ord(c))]
    if tag_chars:
        decoded = "".join(chr(cp - 0xE0000) for cp in tag_chars if 0xE0020 <= cp <= 0xE007E)
        hits.append(UnicodeHit("tag-smuggling", len(tag_chars), decoded))

    # Variation selectors: a single one after an emoji is normal. Long runs are not.
    runs: list[list[int]] = []
    cur: list[int] = []
    for c in text:
        v = _vs_value(ord(c))
        if v is not None:
            cur.append(v)
        else:
            if len(cur) > 2:
                runs.append(cur)
            cur = []
    if len(cur) > 2:
        runs.append(cur)
    if runs:
        total = sum(len(r) for r in runs)
        decoded_parts = []
        for r in runs:
            try:
                decoded_parts.append(bytes(r).decode("utf-8", errors="replace"))
            except ValueError:
                pass
        hits.append(UnicodeHit("variation-selector-smuggling", total, " ".join(decoded_parts)))

    zw = 0
    for i, c in enumerate(text):
        cp = ord(c)
        if cp not in ZERO_WIDTH:
            continue
        if cp == 0xFEFF and i == 0:
            continue  # byte-order mark
        if cp == 0x200D and 0 < i < len(text) - 1 and ord(text[i - 1]) > 0x2000 and ord(text[i + 1]) > 0x2000:
            continue  # emoji joiner
        if cp == 0x00AD:
            continue  # soft hyphen is common in typeset text
        zw += 1
    if zw >= 3:
        hits.append(UnicodeHit("zero-width", zw, _decode_zero_width_binary(text)))

    bidi = sum(1 for c in text if ord(c) in BIDI and ord(c) not in (0x200E, 0x200F))
    if bidi >= 1:
        hits.append(UnicodeHit("bidi-control", bidi))
    return hits


def _decode_zero_width_binary(text: str) -> str:
    """Best effort: some tools encode bits as U+200B / U+200C. Return printable ASCII if it decodes."""
    bits = "".join("0" if c == "​" else "1" for c in text if c in ("​", "‌"))
    if len(bits) < 16:
        return ""
    chars = []
    for i in range(0, len(bits) - 7, 8):
        b = int(bits[i:i + 8], 2)
        chars.append(chr(b))
    s = "".join(chars)
    return s if s and sum(ch.isprintable() for ch in s) / len(s) > 0.9 else ""
