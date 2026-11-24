"""A minimal .docx writer (no extra dependency): headings and paragraphs with bold / italic runs."""
from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _runs(text: str, size: int | None = None) -> str:
    out = []
    for part in re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text):
        if not part:
            continue
        props = ""
        if part.startswith("**") and part.endswith("**"):
            part, props = part[2:-2], "<w:b/>"
        elif part.startswith("*") and part.endswith("*") and len(part) > 1:
            part, props = part[1:-1], "<w:i/>"
        if size:
            props += f'<w:sz w:val="{size}"/>'
        rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def build(blocks: list[tuple[str, str]]) -> bytes:
    """blocks: ("h1" | "h2" | "p", text). Returns the .docx bytes."""
    body = []
    for kind, text in blocks:
        if kind in ("h1", "h2"):
            body.append(f'<w:p><w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>{_runs("**" + text + "**", 32 if kind == "h1" else 26)}</w:p>')
        else:
            body.append(f'<w:p><w:pPr><w:spacing w:after="160" w:line="300" w:lineRule="auto"/></w:pPr>{_runs(text)}</w:p>')
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{_W}"><w:body>'
           + "".join(body) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def paragraphs(data: bytes) -> list[list[dict]]:
    """Read a .docx: a list of paragraphs, each a list of runs {text, sup}. Superscript runs are
    kept apart because numbered citations are often written as superscript digits."""
    import xml.etree.ElementTree as ET

    ns = {"w": _W}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(f"{{{_W}}}p"):
        runs = []
        for r in p.iter(f"{{{_W}}}r"):
            va = r.find("w:rPr/w:vertAlign", ns)
            sup = va is not None and va.get(f"{{{_W}}}val") == "superscript"
            text = "".join((t.text or "") if t.tag == f"{{{_W}}}t" else "\t" if t.tag == f"{{{_W}}}tab" else ""
                           for t in r)
            if text:
                runs.append({"text": text, "sup": sup})
        out.append(runs)
    return out
