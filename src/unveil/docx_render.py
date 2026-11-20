"""Turn a .docx into a PDF containing only what a reader would see.

Preferred: LibreOffice (`soffice`) converts a pre-cleaned copy with full layout fidelity.
Fallback: a built-in renderer that lays out the visible paragraphs, tables and images
with PyMuPDF's Story engine. The fallback keeps the content and basic formatting but not
the exact layout, which is fine for feeding to an AI.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pymupdf
from lxml import etree

from .detectors.ooxml import NS, W, StyleResolver, offpage_anchors, run_hidden_reasons, run_text

R_EMBED = "{%s}embed" % NS["r"]


def find_soffice() -> str | None:
    for cand in ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        p = shutil.which(cand) or (cand if Path(cand).exists() else None)
        if p:
            return p
    return None


# --------------------------------------------------------------------------------------
# Cleaning a .docx in place (used before LibreOffice conversion)
# --------------------------------------------------------------------------------------

def clean_docx(src: str, dst: str) -> None:
    """Write a copy of src with hidden runs, comments, deletions, alt text and metadata removed."""
    with zipfile.ZipFile(src) as zin:
        resolver = StyleResolver(zin.read("word/styles.xml") if "word/styles.xml" in zin.namelist() else None)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                name = item.filename
                data = zin.read(name)
                if name in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml") or name.startswith("customXml/") \
                        or name.lower().endswith("vbaproject.bin") or "/embeddings/" in name:
                    continue
                if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml", name):
                    data = _clean_part(data, resolver)
                elif name == "word/comments.xml":
                    root = etree.fromstring(data)
                    for c in root.findall("w:comment", NS):
                        root.remove(c)
                    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                zout.writestr(item, data)


def _clean_part(data: bytes, resolver: StyleResolver) -> bytes:
    root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, huge_tree=True))
    for a in offpage_anchors(root):
        drawing = next(a.iterancestors(W + "drawing"), None)
        if drawing is not None and drawing.getparent() is not None:
            drawing.getparent().remove(drawing)
    for tag in ("del", "commentRangeStart", "commentRangeEnd", "commentReference"):
        for el in list(root.iter(W + tag)):
            if el.getparent() is not None:
                el.getparent().remove(el)
    for para in list(root.iter(W + "p")):
        for run in list(para.iter(W + "r")):
            if next(run.iterancestors(W + "p"), None) is not para:
                continue
            if run_hidden_reasons(run, para, resolver) and run.getparent() is not None:
                run.getparent().remove(run)
    for el in root.iter("{%s}docPr" % NS["wp"], "{%s}cNvPr" % NS["pic"]):
        for attr in ("descr", "title"):
            if attr in el.attrib:
                del el.attrib[attr]
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def convert_with_soffice(docx: str, outdir: str) -> str:
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice not found")
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", outdir, docx],
                   check=True, capture_output=True, timeout=180)
    pdf = Path(outdir) / (Path(docx).stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError("LibreOffice produced no PDF")
    return str(pdf)


# --------------------------------------------------------------------------------------
# Built-in visible-only renderer
# --------------------------------------------------------------------------------------

CSS = """
body { font-family: sans-serif; font-size: 11pt; line-height: 1.3; color: #000; }
p { margin: 0 0 6pt 0; }
h1 { font-size: 20pt; margin: 6pt 0 8pt 0; } h2 { font-size: 15pt; margin: 8pt 0 6pt 0; }
h3 { font-size: 13pt; margin: 6pt 0 4pt 0; }
table { border-collapse: collapse; margin: 4pt 0 8pt 0; }
td { border: 0.5pt solid #999; padding: 2pt 4pt; vertical-align: top; }
.box { border: 0.5pt solid #bbb; padding: 4pt; margin: 4pt 0; }
"""


class _Renderer:
    def __init__(self, zf: zipfile.ZipFile, media_dir: Path):
        self.zf = zf
        self.media_dir = media_dir
        self.resolver = StyleResolver(zf.read("word/styles.xml") if "word/styles.xml" in zf.namelist() else None)
        self.rels = self._rels()
        self.offpage: set = set()

    def _rels(self) -> dict[str, str]:
        try:
            root = etree.fromstring(self.zf.read("word/_rels/document.xml.rels"))
        except KeyError:
            return {}
        return {r.get("Id"): r.get("Target") for r in root}

    def _style_name(self, para) -> str:
        ps = para.find("w:pPr/w:pStyle", NS)
        if ps is None:
            return ""
        st = self.resolver.styles.get(ps.get(W + "val"))
        name = st.find("w:name", NS).get(W + "val") if st is not None and st.find("w:name", NS) is not None else ps.get(W + "val")
        return (name or "").lower().replace(" ", "")

    def body(self, root) -> str:
        self.offpage = offpage_anchors(root)
        body = root.find("w:body", NS)
        return "".join(self.block(el) for el in body) if body is not None else ""

    def block(self, el) -> str:
        tag = etree.QName(el).localname
        if tag == "p":
            return self.para(el)
        if tag == "tbl":
            rows = []
            for tr in el.findall("w:tr", NS):
                cells = []
                for tc in tr.findall("w:tc", NS):
                    span = tc.find("w:tcPr/w:gridSpan", NS)
                    colspan = f' colspan="{span.get(W + "val")}"' if span is not None else ""
                    cells.append(f"<td{colspan}>" + "".join(self.block(c) for c in tc) + "</td>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            return "<table>" + "".join(rows) + "</table>"
        if tag == "sdt":
            content = el.find("w:sdtContent", NS)
            return "".join(self.block(c) for c in content) if content is not None else ""
        return ""

    def para(self, p) -> str:
        style = self._style_name(p)
        inner, extras = [], []
        for node in p.iter(W + "r"):
            if next(node.iterancestors(W + "p"), None) is not p:
                continue
            if next(node.iterancestors(W + "del"), None) is not None:
                continue
            if any(a in self.offpage for a in node.iterancestors("{%s}anchor" % NS["wp"])):
                continue
            for drawing in node.iter(W + "drawing"):
                if any(a in self.offpage for a in drawing.iter("{%s}anchor" % NS["wp"])):
                    continue
                inner.append(self.image(drawing))
                for txbx in drawing.iter(W + "txbxContent"):
                    extras.append('<div class="box">' + "".join(self.block(c) for c in txbx) + "</div>")
            if run_hidden_reasons(node, p, self.resolver):
                continue
            text = run_text(node)
            if not text:
                continue
            props = self.resolver.run_props(node, p)
            rpr = node.find("w:rPr", NS)
            s = html.escape(text).replace("\n", "<br/>").replace("\t", "&#160;&#160;&#160;&#160;")
            css = []
            if props.color:
                css.append(f"color:#{props.color}")
            if props.sz and not style.startswith(("heading", "title")):
                css.append(f"font-size:{props.sz / 2:g}pt")
            if props.highlight or props.shd:
                css.append(f"background-color:#{props.highlight or props.shd}")
            if rpr is not None:
                if rpr.find("w:b", NS) is not None and _on(rpr.find("w:b", NS)):
                    s = f"<b>{s}</b>"
                if rpr.find("w:i", NS) is not None and _on(rpr.find("w:i", NS)):
                    s = f"<i>{s}</i>"
                if rpr.find("w:u", NS) is not None and rpr.find("w:u", NS).get(W + "val", "single") != "none":
                    s = f"<u>{s}</u>"
            inner.append(f'<span style="{";".join(css)}">{s}</span>' if css else s)
        content = "".join(inner)
        jc = p.find("w:pPr/w:jc", NS)
        align = jc.get(W + "val") if jc is not None else ""
        attr = f' style="text-align:{ {"both": "justify", "start": "left", "end": "right"}.get(align, align)}"' if align else ""
        if p.find("w:pPr/w:numPr", NS) is not None and content.strip():
            content = "•&#160;" + content
            attr = ' style="margin-left:14pt"'
        if style == "title":
            out = f"<h1{attr}>{content}</h1>"
        elif style.startswith("heading") and style[-1:].isdigit():
            lvl = min(3, int(style[-1]))
            out = f"<h{lvl}{attr}>{content}</h{lvl}>"
        else:
            out = f"<p{attr}>{content or '&#160;'}</p>"
        return out + "".join(extras)

    def image(self, drawing) -> str:
        blip = next(drawing.iter("{%s}blip" % NS["a"]), None)
        if blip is None or blip.get(R_EMBED) not in self.rels:
            return ""
        target = self.rels[blip.get(R_EMBED)]
        member = "word/" + target if not target.startswith("/") else target[1:]
        try:
            data = self.zf.read(member)
        except KeyError:
            return ""
        name = Path(member).name
        (self.media_dir / name).write_bytes(data)
        ext = drawing.find(".//wp:extent", NS)
        width = ""
        if ext is not None and ext.get("cx", "").isdigit():
            width = f' width="{int(ext.get("cx")) / 12700:.0f}"'
        return f'<img src="{name}"{width}/>'


def _on(el) -> bool:
    return el.get(W + "val", "true").lower() not in ("0", "false", "off")


def render_docx_builtin(docx: str, out_pdf: str) -> str:
    with zipfile.ZipFile(docx) as zf, tempfile.TemporaryDirectory() as tmp:
        root = etree.fromstring(zf.read("word/document.xml"),
                                parser=etree.XMLParser(resolve_entities=False, huge_tree=True))
        media = Path(tmp)
        r = _Renderer(zf, media)
        body_html = r.body(root)
        pg = root.find(".//w:sectPr/w:pgSz", NS)
        mar = root.find(".//w:sectPr/w:pgMar", NS)
        width = int(pg.get(W + "w", 12240)) / 20 if pg is not None else 612
        height = int(pg.get(W + "h", 15840)) / 20 if pg is not None else 792
        if pg is not None and pg.get(W + "orient") == "landscape" and width < height:
            width, height = height, width
        m = [int(mar.get(W + k, 1440)) / 20 if mar is not None else 72 for k in ("left", "top", "right", "bottom")]
        mediabox = pymupdf.Rect(0, 0, width, height)
        where = pymupdf.Rect(m[0], m[1], width - m[2], height - m[3])
        story = pymupdf.Story(html=f"<body>{body_html}</body>", user_css=CSS, archive=pymupdf.Archive(str(media)))
        writer = pymupdf.DocumentWriter(out_pdf)
        more = 1
        while more:
            dev = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
        writer.close()
    return out_pdf


def docx_to_visible_pdf(docx: str, out_pdf: str, engine: str = "auto") -> tuple[str, str]:
    """Return (pdf path, engine used)."""
    if engine in ("auto", "libreoffice") and find_soffice():
        with tempfile.TemporaryDirectory() as tmp:
            cleaned = str(Path(tmp) / "cleaned.docx")
            clean_docx(docx, cleaned)
            pdf = convert_with_soffice(cleaned, tmp)
            shutil.copy(pdf, out_pdf)
        return out_pdf, "libreoffice"
    if engine == "libreoffice":
        raise RuntimeError("LibreOffice was requested but is not installed (brew install --cask libreoffice)")
    return render_docx_builtin(docx, out_pdf), "builtin"


def convert_office_with_soffice(path: str, out_pdf: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        pdf = convert_with_soffice(path, tmp)
        shutil.copy(pdf, out_pdf)
    return out_pdf

