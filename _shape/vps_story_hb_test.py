"""Story display + HarfBuzz ToUnicode — run on VPS."""
from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

import fitz
import uharfbuzz as hb
from fontTools.ttLib import TTFont

CSS = """
body {
  font-family: "Noto Serif Devanagari", serif;
  font-size: 14pt;
  color: #1a1814;
  background-color: #f7f2e8;
}
p { margin: 0.2em 0; text-align: center; }
"""

CORPUS = [
    "श्री रामकृष्ण मन्दिरम् - रामकृष्ण मठ",
    "हृदयकमलमध्ये राजितं निर्विकल्पं",
    "प्रकृतिविकृतिशून्यं नित्यमानन्दमूर्तिं",
    "विमलपरमहंसं रामकृष्णं भजामः ॥",
]

html = (
    "<html><body>"
    + "".join(f"<p>{t}</p>" for t in CORPUS)
    + "</body></html>"
)


def tounicode_stream(mapping: dict[int, str]) -> bytes:
    items = sorted(mapping.items())
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    for i in range(0, len(items), 100):
        part = items[i : i + 100]
        lines.append(f"{len(part)} beginbfchar")
        for cid, text in part:
            lines.append(f"<{cid:04X}> <{text.encode('utf-16-be').hex().upper()}>")
        lines.append("endbfchar")
    lines += [
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def main() -> None:
    print("pymupdf", fitz.version)
    font_dirs = [d for d in ("/usr/share/fonts/truetype/noto",) if Path(d).is_dir()]
    mediabox = fitz.Rect(0, 0, 420, 300)
    story = fitz.Story(
        html=html, user_css=CSS, archive=fitz.Archive(*font_dirs) if font_dirs else None
    )
    doc = fitz.open()
    more = True
    while more:
        page = doc.new_page(width=mediabox.width, height=mediabox.height)
        more, _ = story.place(mediabox + (14, 14, -14, -14))
        story.draw(page)

    print("extract before", repr(doc[0].get_text()[:120]))
    doc[0].get_pixmap(matrix=fitz.Matrix(2, 2)).save("/tmp/story_vps.png")

    seen: set[int] = set()
    for page in doc:
        for item in page.get_fonts(full=True):
            xref = item[0]
            if xref in seen:
                continue
            seen.add(xref)
            extracted = doc.extract_font(xref)
            buf = extracted[3] if isinstance(extracted, (tuple, list)) else extracted.get("content")
            if not buf:
                continue
            face = hb.Face(buf)
            font = hb.Font(face)
            mapping: dict[int, str] = {}
            for text in CORPUS:
                b = hb.Buffer()
                b.add_str(text)
                b.guess_segment_properties()
                hb.shape(font, b)
                infos = b.glyph_infos
                for i, info in enumerate(infos):
                    gid = info.codepoint
                    cl = info.cluster
                    end = len(text)
                    for k in range(i + 1, len(infos)):
                        if infos[k].cluster > cl:
                            end = infos[k].cluster
                            break
                    piece = text[cl:end]
                    if piece:
                        mapping[gid] = piece
            tt = TTFont(BytesIO(buf))
            order = tt.getGlyphOrder()
            n2g = {n: i for i, n in enumerate(order)}
            for uni, name in (tt.getBestCmap() or {}).items():
                mapping.setdefault(n2g[name], chr(uni))
            obj = doc.xref_object(xref)
            m = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
            if not m:
                continue
            doc.update_stream(int(m.group(1)), tounicode_stream(mapping))
            print("fixed", item[3], "n=", len(mapping))

    doc.save("/tmp/story_vps_fixed.pdf")
    doc.close()
    d = fitz.open("/tmp/story_vps_fixed.pdf")
    t = d[0].get_text()
    print("extract after:\n", t)
    for s in ["मन्दिरम्", "हृदयकमलमध्ये", "प्रकृति", "भजामः"]:
        print(s, s in t)
    d.close()


if __name__ == "__main__":
    main()
