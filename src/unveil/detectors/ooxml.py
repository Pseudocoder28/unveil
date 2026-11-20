"""Word (.docx), PowerPoint (.pptx) and Excel (.xlsx) detector.

Office files are ZIP archives of XML, so we read the XML directly rather than trusting
a library's idea of "the text". That lets us see:

* runs marked hidden (w:vanish), white/background-coloured, microscopic, squashed or
  transparent, resolving Word's style inheritance to get the colour/size really shown;
* text boxes pushed off the page, alt text on images, comments, tracked deletions,
  field codes, speaker notes, hidden spreadsheet sheets;
* metadata (core, app, custom properties), custom XML parts, macros, embedded objects;
* any other part of the archive that contains instruction-like text.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from ..models import Finding, ScanResult, sha256_file
from ..patterns import categories, match_injection
from .common import clip, hidden_text_finding, metadata_finding, unicode_findings

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "op": "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
}
W = "{%s}" % NS["w"]
TINY_HALF_POINTS = 6     # Word sizes are in half-points: 6 = 3pt
TINY_PPTX = 300          # PowerPoint sizes are in hundredths of a point
LOW_CONTRAST = 1.35
EMU_PER_TWIP = 635

TITLES = {
    "hidden-text": "Text formatted as hidden (Word 'Hidden' font option)",
    "low-contrast-text": "Text coloured to match its background (e.g. white on white)",
    "tiny-text": "Microscopic text (too small to read)",
    "squashed-text": "Text squeezed to near-zero width",
    "transparent-text": "Fully transparent text",
    "off-page-text": "Text box placed outside the page",
    "tracked-deletion": "Deleted text still stored in the file (tracked changes)",
    "field-code": "Instruction hidden inside a field code",
}

HIGHLIGHT_RGB = {
    "black": "000000", "blue": "0000FF", "cyan": "00FFFF", "green": "00FF00", "magenta": "FF00FF",
    "red": "FF0000", "yellow": "FFFF00", "white": "FFFFFF", "darkBlue": "000080", "darkCyan": "008080",
    "darkGreen": "008000", "darkMagenta": "800080", "darkRed": "800000", "darkYellow": "808000",
    "darkGray": "808080", "lightGray": "C0C0C0",
}


def _lum(hexrgb: str) -> float:
    vals = [int(hexrgb[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    vals = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in vals]
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _valid_hex(v: str | None) -> str | None:
    if v and re.fullmatch(r"[0-9A-Fa-f]{6}", v):
        return v.upper()
    return None


def _on(el) -> bool:
    """OOXML toggle property: present and not val=0/false/off."""
    if el is None:
        return False
    return el.get(W + "val", "true").lower() not in ("0", "false", "off")


# --------------------------------------------------------------------------------------
# Word style resolution
# --------------------------------------------------------------------------------------

@dataclass
class RunProps:
    vanish: bool = False
    color: str | None = None     # hex, or None for automatic
    sz: int | None = None        # half-points
    shd: str | None = None       # run background hex
    highlight: str | None = None
    scale: int | None = None     # w:w percent
    no_fill: bool = False

    def merged(self, rpr) -> "RunProps":
        if rpr is None:
            return self
        out = RunProps(**self.__dict__)
        v = rpr.find(W + "vanish")
        if v is not None:
            out.vanish = _on(v)
        sv = rpr.find(W + "specVanish")
        if sv is not None and _on(sv):
            out.vanish = True
        c = rpr.find(W + "color")
        if c is not None:
            val = c.get(W + "val")
            out.color = None if (val or "").lower() == "auto" else _valid_hex(val)
        s = rpr.find(W + "sz")
        if s is not None and (s.get(W + "val") or "").isdigit():
            out.sz = int(s.get(W + "val"))
        sh = rpr.find(W + "shd")
        if sh is not None:
            out.shd = _valid_hex(sh.get(W + "fill")) or out.shd
        hl = rpr.find(W + "highlight")
        if hl is not None:
            out.highlight = HIGHLIGHT_RGB.get(hl.get(W + "val", ""))
        sc = rpr.find(W + "w")
        if sc is not None and (sc.get(W + "val") or "").isdigit():
            out.scale = int(sc.get(W + "val"))
        for el in rpr.iter():
            tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
            if tag == "textFill":
                if any(etree.QName(c).localname == "noFill" for c in el.iter() if isinstance(c.tag, str)):
                    out.no_fill = True
                for alpha in el.iter():
                    if isinstance(alpha.tag, str) and etree.QName(alpha).localname == "alpha":
                        try:
                            if int(alpha.get("{http://schemas.microsoft.com/office/word/2010/wordml}val", alpha.get("val", "0"))) >= 95000:
                                out.no_fill = True
                        except ValueError:
                            pass
        return out


class StyleResolver:
    def __init__(self, styles_xml: bytes | None):
        self.default = RunProps()
        self.styles: dict[str, etree._Element] = {}
        if not styles_xml:
            return
        root = etree.fromstring(styles_xml)
        d = root.find("w:docDefaults/w:rPrDefault/w:rPr", NS)
        self.default = RunProps().merged(d)
        for st in root.findall("w:style", NS):
            sid = st.get(W + "styleId")
            if sid:
                self.styles[sid] = st

    def _chain(self, sid: str | None) -> list:
        chain, seen = [], set()
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            st = self.styles[sid]
            chain.append(st)
            b = st.find("w:basedOn", NS)
            sid = b.get(W + "val") if b is not None else None
        return list(reversed(chain))

    def run_props(self, run, para) -> RunProps:
        props = self.default
        ppr = para.find("w:pPr", NS) if para is not None else None
        pstyle = ppr.find("w:pStyle", NS) if ppr is not None else None
        for st in self._chain(pstyle.get(W + "val") if pstyle is not None else None):
            props = props.merged(st.find("w:rPr", NS))
        rpr = run.find("w:rPr", NS)
        rstyle = rpr.find("w:rStyle", NS) if rpr is not None else None
        for st in self._chain(rstyle.get(W + "val") if rstyle is not None else None):
            props = props.merged(st.find("w:rPr", NS))
        return props.merged(rpr)

    def para_shading(self, para) -> str | None:
        ppr = para.find("w:pPr", NS) if para is not None else None
        if ppr is None:
            return None
        sh = ppr.find("w:shd", NS)
        if sh is not None and _valid_hex(sh.get(W + "fill")):
            return _valid_hex(sh.get(W + "fill"))
        pstyle = ppr.find("w:pStyle", NS)
        for st in reversed(self._chain(pstyle.get(W + "val") if pstyle is not None else None)):
            sh = st.find("w:pPr/w:shd", NS)
            if sh is not None and _valid_hex(sh.get(W + "fill")):
                return _valid_hex(sh.get(W + "fill"))
        return None


def run_text(run) -> str:
    parts = []
    for el in run:
        tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
        if tag in ("t", "delText"):
            parts.append(el.text or "")
        elif tag == "tab":
            parts.append("\t")
        elif tag in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def background_for(run, para, resolver: StyleResolver, page_bg: str = "FFFFFF") -> str:
    props = resolver.run_props(run, para)
    if props.highlight:
        return props.highlight
    if props.shd:
        return props.shd
    ps = resolver.para_shading(para)
    if ps:
        return ps
    tc = next((a for a in run.iterancestors(W + "tc")), None)
    if tc is not None:
        sh = tc.find("w:tcPr/w:shd", NS)
        if sh is not None and _valid_hex(sh.get(W + "fill")):
            return _valid_hex(sh.get(W + "fill"))
    return page_bg


def run_hidden_reasons(run, para, resolver: StyleResolver, page_bg: str = "FFFFFF") -> list[tuple[str, str]]:
    """Why a human would not see this run. Empty list = visible."""
    props = resolver.run_props(run, para)
    reasons: list[tuple[str, str]] = []
    if props.vanish:
        reasons.append(("hidden-text", "w:vanish (hidden font attribute)"))
    if props.no_fill:
        reasons.append(("transparent-text", "text fill set to none/transparent"))
    if props.sz is not None and props.sz < TINY_HALF_POINTS:
        reasons.append(("tiny-text", f"{props.sz / 2:g}pt font"))
    if props.scale is not None and props.scale < 20:
        reasons.append(("squashed-text", f"character width {props.scale}%"))
    if props.color:
        bg = background_for(run, para, resolver, page_bg)
        if contrast(props.color, bg) < LOW_CONTRAST:
            reasons.append(("low-contrast-text", f"text colour #{props.color} on background #{bg}"))
    return reasons


# --------------------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------------------

@dataclass
class _Ctx:
    zf: zipfile.ZipFile
    res: ScanResult
    handled: set[str] = field(default_factory=set)

    def read(self, name: str) -> bytes | None:
        try:
            return self.zf.read(name)
        except KeyError:
            return None

    def xml(self, name: str):
        data = self.read(name)
        if data is None:
            return None
        try:
            return etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, huge_tree=True, recover=True))
        except etree.XMLSyntaxError:
            return None


def scan_ooxml(path: str) -> ScanResult:
    names_kind = "docx"
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if any(n.startswith("ppt/") for n in names):
            names_kind = "pptx"
        elif any(n.startswith("xl/") for n in names):
            names_kind = "xlsx"
        res = ScanResult(path=str(path), file_type=names_kind, sha256=sha256_file(path))
        ctx = _Ctx(zf, res)
        _scan_props(ctx)
        _scan_package_extras(ctx)
        if names_kind == "docx":
            _scan_docx(ctx)
        elif names_kind == "pptx":
            _scan_pptx(ctx)
        else:
            _scan_xlsx(ctx)
        _scan_leftover_parts(ctx)
    return res


def _scan_props(ctx: _Ctx) -> None:
    for part in ("docProps/core.xml", "docProps/app.xml"):
        root = ctx.xml(part)
        ctx.handled.add(part)
        if root is None:
            continue
        for el in root:
            if not isinstance(el.tag, str) or not (el.text or "").strip():
                continue
            name = etree.QName(el).localname
            if name in ("created", "modified", "TotalTime", "Pages", "Words", "Characters", "Lines",
                        "Paragraphs", "CharactersWithSpaces", "DocSecurity", "ScaleCrop", "LinksUpToDate",
                        "SharedDoc", "HyperlinksChanged", "AppVersion", "Template", "Application", "revision",
                        "Slides", "Notes", "HiddenSlides", "MMClips", "PresentationFormat"):
                continue
            f = metadata_finding(name, el.text)
            if f:
                ctx.res.findings.append(f)
    root = ctx.xml("docProps/custom.xml")
    ctx.handled.add("docProps/custom.xml")
    if root is not None:
        for prop in root.findall("op:property", NS):
            val = " ".join(t for t in prop.itertext() if t.strip())
            f = metadata_finding(f"custom property '{prop.get('name')}'", val)
            if f:
                if f.severity == "info":
                    f.severity = "low"  # custom properties are rarely legitimate in a CV/essay
                ctx.res.findings.append(f)


def _scan_package_extras(ctx: _Ctx) -> None:
    names = ctx.zf.namelist()
    macros = [n for n in names if n.lower().endswith("vbaproject.bin")]
    if macros:
        ctx.res.findings.append(Finding("macros", "high", "File contains VBA macros", ", ".join(macros), "package"))
    embedded = [n for n in names if "/embeddings/" in n]
    if embedded:
        ctx.res.findings.append(Finding("embedded-object", "medium", "Embedded objects/files inside the document",
                                        ", ".join(embedded[:20]), "package",
                                        detail="AI tools may unpack and read embedded files the reader never sees."))
    for n in names:
        if n.startswith("customXml/") and n.endswith(".xml") and "itemProps" not in n:
            ctx.handled.add(n)
            root = ctx.xml(n)
            if root is None:
                continue
            text = " ".join(t.strip() for t in root.itertext() if t.strip())
            cats = categories(text)
            if cats:
                ctx.res.findings.append(Finding("custom-xml-injection", "critical",
                                                "Instruction hidden in a custom XML data part", clip(text), n,
                                                patterns=cats))


def _scan_alt_text(ctx: _Ctx, root, part: str) -> None:
    for tag in ("wp:docPr", "pic:cNvPr", "p:cNvPr"):
        for el in root.iter("{%s}%s" % (NS[tag.split(":")[0]], tag.split(":")[1])):
            text = " ".join(filter(None, [el.get("title"), el.get("descr")])).strip()
            if not text:
                continue
            cats = categories(text)
            if cats:
                ctx.res.findings.append(Finding("alt-text-injection", "critical",
                                                "Instruction hidden in image alt text", clip(text), part, patterns=cats))
            elif len(text) > 250:
                ctx.res.findings.append(Finding("alt-text", "low", "Unusually long image alt text",
                                                clip(text), part,
                                                detail="Alt text is invisible on the page but read by AI tools."))


def _page_size_emu(root) -> tuple[int, int]:
    pg = root.find(".//w:sectPr/w:pgSz", NS)
    try:
        w = int(pg.get(W + "w")) * EMU_PER_TWIP
        h = int(pg.get(W + "h")) * EMU_PER_TWIP
        return w, h
    except Exception:
        return 12240 * EMU_PER_TWIP, 15840 * EMU_PER_TWIP


def offpage_anchors(root) -> set:
    """Return the set of wp:anchor elements positioned outside the page."""
    pw, ph = _page_size_emu(root)
    out = set()
    for anchor in root.iter("{%s}anchor" % NS["wp"]):
        try:
            x = int(anchor.findtext("wp:positionH/wp:posOffset", default="0", namespaces=NS))
            y = int(anchor.findtext("wp:positionV/wp:posOffset", default="0", namespaces=NS))
            ext = anchor.find("wp:extent", NS)
            cx = int(ext.get("cx", "0")) if ext is not None else 0
            cy = int(ext.get("cy", "0")) if ext is not None else 0
        except ValueError:
            continue
        if x + cx < -EMU_PER_TWIP * 1440 or x > pw * 1.5 or y + cy < -EMU_PER_TWIP * 1440 or y > ph * 1.5:
            out.add(anchor)
    return out


def _scan_docx(ctx: _Ctx) -> None:
    resolver = StyleResolver(ctx.read("word/styles.xml"))
    ctx.handled.add("word/styles.xml")
    parts = sorted(n for n in ctx.zf.namelist() if re.fullmatch(
        r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml", n))
    for part in parts:
        ctx.handled.add(part)
        root = ctx.xml(part)
        if root is None:
            continue
        _scan_alt_text(ctx, root, part)
        offpage = offpage_anchors(root)
        hidden: list[tuple[int, str, str, str]] = []   # para index, technique, text, detail
        visible_text: list[str] = []
        all_text: list[str] = []
        for pi, para in enumerate(root.iter(W + "p")):
            for run in para.iter(W + "r"):
                if next(run.iterancestors(W + "p")) is not para:
                    continue  # belongs to a nested paragraph (text box); handled there
                text = run_text(run)
                if not text:
                    continue
                all_text.append(text)
                reasons = run_hidden_reasons(run, para, resolver)
                if any(a in offpage for a in run.iterancestors("{%s}anchor" % NS["wp"])):
                    reasons.insert(0, ("off-page-text", "anchored text box positioned outside the page"))
                if next(run.iterancestors(W + "del"), None) is not None:
                    reasons = [("tracked-deletion", "inside a tracked deletion")]
                if reasons:
                    hidden.append((pi, reasons[0][0], text, "; ".join(r for _, r in reasons)))
                else:
                    visible_text.append(text)
            visible_text.append("\n")

        for group in _group(hidden):
            tech = group[0][1]
            text = "".join(g[2] for g in group)
            f = hidden_text_finding(tech, TITLES[tech], text,
                                    f"{part} paragraph {group[0][0] + 1}",
                                    detail="; ".join(sorted({g[3] for g in group})))
            if tech == "tracked-deletion" and f.severity in ("high",):
                f.severity = "medium"
            ctx.res.findings.append(f)

        # Field codes (e.g. a { COMMENTS } or custom field) are never shown as typed.
        instr = " ".join((t.text or "") for t in root.iter(W + "instrText"))
        if categories(instr):
            ctx.res.findings.append(Finding("field-code", "critical", TITLES["field-code"], clip(instr), part,
                                            patterns=categories(instr)))

        _visible_and_unicode(ctx, "".join(visible_text), "".join(all_text), part)

    # Comments
    for name in ("word/comments.xml", "word/commentsExtended.xml"):
        ctx.handled.add(name)
    root = ctx.xml("word/comments.xml")
    if root is not None:
        for c in root.findall("w:comment", NS):
            text = " ".join(t.text or "" for t in c.iter(W + "t")).strip()
            if not text:
                continue
            cats = categories(text)
            ctx.res.findings.append(Finding(
                "comment", "critical" if cats else "low", "Review comment stored in the document",
                clip(text), f"comment by {c.get(W + 'author', '?')}", patterns=cats,
                detail="Comments are hidden in normal view and printing but are read by AI tools."))


def _group(hidden):
    groups: list[list] = []
    for item in hidden:
        if groups and groups[-1][-1][1] == item[1] and item[0] - groups[-1][-1][0] <= 1:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _visible_and_unicode(ctx: _Ctx, visible: str, everything: str, location: str) -> None:
    hits = match_injection(visible)
    if hits:
        ctx.res.findings.append(Finding(
            "visible-injection-phrase", "medium", "Visible text reads like an instruction to an AI",
            clip("\n".join(f"• {s}" for _, s in hits[:10])), location,
            patterns=sorted({c for c, _ in hits}),
            detail="Visible, so a careful reader could spot it, but an AI may obey it."))
    ctx.res.findings.extend(unicode_findings(everything, location))


def _scan_pptx(ctx: _Ctx) -> None:
    pres = ctx.xml("ppt/presentation.xml")
    ctx.handled.add("ppt/presentation.xml")
    sw, sh = 12192000, 6858000
    if pres is not None:
        sz = pres.find("p:sldSz", NS)
        if sz is not None:
            sw, sh = int(sz.get("cx", sw)), int(sz.get("cy", sh))
    slides = sorted((n for n in ctx.zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.findall(r"\d+", n)[-1]))
    for part in slides:
        ctx.handled.add(part)
        root = ctx.xml(part)
        if root is None:
            continue
        loc = "slide " + re.findall(r"\d+", part)[-1]
        if root.get("show") in ("0", "false"):
            text = " ".join(t.text or "" for t in root.iter("{%s}t" % NS["a"])).strip()
            if text:
                ctx.res.findings.append(hidden_text_finding("hidden-slide", "Hidden slide (not shown in the presentation)",
                                                            text, loc))
        _scan_alt_text(ctx, root, loc)
        visible, everything = [], []
        for sp in root.iter("{%s}sp" % NS["p"]):
            off = sp.find(".//a:xfrm/a:off", NS)
            ext = sp.find(".//a:xfrm/a:ext", NS)
            offslide = False
            if off is not None and ext is not None:
                x, y = int(off.get("x", 0)), int(off.get("y", 0))
                cx, cy = int(ext.get("cx", 0)), int(ext.get("cy", 0))
                offslide = x + cx <= 0 or y + cy <= 0 or x >= sw or y >= sh
            for r in sp.iter("{%s}r" % NS["a"]):
                t = r.findtext("a:t", default="", namespaces=NS)
                if not t.strip():
                    continue
                everything.append(t)
                rpr = r.find("a:rPr", NS)
                size = int(rpr.get("sz")) if rpr is not None and (rpr.get("sz") or "").isdigit() else None
                if offslide:
                    ctx.res.findings.append(hidden_text_finding("off-page-text", "Text box placed outside the slide", t, loc))
                elif size is not None and size < TINY_PPTX:
                    ctx.res.findings.append(hidden_text_finding("tiny-text", TITLES["tiny-text"], t, loc,
                                                                detail=f"{size / 100:g}pt font"))
                elif rpr is not None and rpr.find("a:noFill", NS) is not None:
                    ctx.res.findings.append(hidden_text_finding("transparent-text", TITLES["transparent-text"], t, loc))
                else:
                    visible.append(t)
        _visible_and_unicode(ctx, " ".join(visible), " ".join(everything), loc)

    for part in sorted(n for n in ctx.zf.namelist() if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n)):
        ctx.handled.add(part)
        root = ctx.xml(part)
        if root is None:
            continue
        text = " ".join(t.text or "" for t in root.iter("{%s}t" % NS["a"])).strip()
        text = re.sub(r"^\d+$", "", text)
        if text and categories(text):
            ctx.res.findings.append(hidden_text_finding("speaker-notes", "Instruction hidden in speaker notes",
                                                        text, part))
    for part in [n for n in ctx.zf.namelist() if n.startswith("ppt/comments/")]:
        ctx.handled.add(part)
        root = ctx.xml(part)
        if root is not None:
            text = " ".join(t.strip() for t in root.itertext() if t.strip())
            if text:
                cats = categories(text)
                ctx.res.findings.append(Finding("comment", "critical" if cats else "low", "Slide comment",
                                                clip(text), part, patterns=cats))


def _scan_xlsx(ctx: _Ctx) -> None:
    wb = ctx.xml("xl/workbook.xml")
    ctx.handled.add("xl/workbook.xml")
    if wb is not None:
        for sheet in wb.iter("{%s}sheet" % NS["s"]):
            if sheet.get("state") in ("hidden", "veryHidden"):
                ctx.res.findings.append(Finding(
                    "hidden-sheet", "high" if sheet.get("state") == "veryHidden" else "medium",
                    f"{'Very hidden' if sheet.get('state') == 'veryHidden' else 'Hidden'} worksheet '{sheet.get('name')}'",
                    "", "workbook", detail="Hidden sheets are read by AI tools that parse the workbook."))
    ss = ctx.xml("xl/sharedStrings.xml")
    ctx.handled.add("xl/sharedStrings.xml")
    if ss is not None:
        text = "\n".join("".join(si.itertext()) for si in ss.findall("s:si", NS))
        _visible_and_unicode(ctx, text, text, "shared strings")


def _scan_leftover_parts(ctx: _Ctx) -> None:
    """Catch-all: any other part of the archive that contains instruction-like text."""
    for name in ctx.zf.namelist():
        if name in ctx.handled or name.endswith("/") or name.lower().endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".emf", ".wmf", ".bin", ".ttf", ".odttf", ".tif", ".tiff", ".svg")):
            continue
        info = ctx.zf.getinfo(name)
        if info.file_size > 5_000_000:
            continue
        data = ctx.read(name) or b""
        if name.endswith((".xml", ".rels")):
            try:
                root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, recover=True))
                text = " ".join(t.strip() for t in root.itertext() if t.strip()) if root is not None else ""
                # attribute values too (descr=, Target=, etc.)
                if root is not None:
                    text += " " + " ".join(v for el in root.iter() if isinstance(el.tag, str) for v in el.attrib.values())
            except etree.XMLSyntaxError:
                text = data.decode("utf-8", "ignore")
        else:
            text = data.decode("utf-8", "ignore")
            if not name.startswith(("word/", "ppt/", "xl/", "docProps/", "customXml/", "_rels/", "[Content_Types]")):
                ctx.res.findings.append(Finding("unexpected-part", "medium", f"Unexpected file inside the document: {name}",
                                                clip(text, 300) if text.isprintable() else "", "package"))
        cats = categories(text)
        if cats:
            ctx.res.findings.append(Finding("hidden-part-injection", "critical",
                                            "Instruction hidden in an internal document part",
                                            clip(" ".join(s for _, s in match_injection(text))), name, patterns=cats))
