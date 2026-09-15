"""One-off: HarfBuzz ToUnicode rewrite on a few pages of the live translation PDF."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from io import BytesIO
from uuid import UUID

import fitz
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from sqlalchemy import select

from app.db import get_session_factory
from app.models import Page


class Txt(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs) -> None:  # noqa: ANN001
        if tag.lower() in ("script", "style"):
            self.skip += 1
        if tag.lower() in ("p", "h1", "h2", "h3", "br", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:  # noqa: ANN001
        if tag.lower() in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    p = Txt()
    p.feed(html or "")
    p.close()
    return re.sub(r"\n+", "\n", "".join(p.parts)).strip()


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
            hx = text.encode("utf-16-be").hex().upper()
            lines.append(f"<{cid:04X}> <{hx}>")
        lines.append("endbfchar")
    lines += [
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def main() -> None:
    pid = UUID("f202757e-f046-40d8-ae84-893d205ad9f0")
    Session = get_session_factory()
    with Session() as db:
        pages = list(
            db.scalars(select(Page).where(Page.project_id == pid).order_by(Page.page_no)).all()
        )[:5]
        corpus = [html_text(p.current_html or "") for p in pages if p.current_html]
    print("corpus", len(corpus), "chars", sum(len(c) for c in corpus))

    src = fitz.open(
        "/opt/sanskrit_srv/storage/exports/f202757e-f046-40d8-ae84-893d205ad9f0/mantra-pushpam-ru.pdf"
    )
    doc = fitz.open()
    doc.insert_pdf(src, from_page=0, to_page=4)
    src.close()
    print("before", repr(doc[1].get_text()[:180]))

    seen: set[int] = set()
    fixed = 0
    for page in doc:
        for item in page.get_fonts(full=True):
            xref = item[0]
            if xref in seen:
                continue
            seen.add(xref)
            extracted = doc.extract_font(xref)
            buf = (
                extracted[3]
                if isinstance(extracted, (tuple, list))
                else extracted.get("content")
            )
            if not buf:
                print("no buf", item[3])
                continue
            try:
                face = hb.Face(buf)
                font = hb.Font(face)
            except Exception as exc:  # noqa: BLE001
                print("hb fail", item[3], exc)
                continue
            mapping: dict[int, str] = {}
            for text in corpus:
                if not text:
                    continue
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
            try:
                tt = TTFont(BytesIO(buf))
                order = tt.getGlyphOrder()
                n2g = {n: i for i, n in enumerate(order)}
                for uni, name in (tt.getBestCmap() or {}).items():
                    mapping.setdefault(n2g[name], chr(uni))
            except Exception as exc:  # noqa: BLE001
                print("tt fail", item[3], exc)
            obj = doc.xref_object(xref)
            m = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
            print("font", item[3], "map", len(mapping), "tounicode", bool(m))
            if m and mapping:
                doc.update_stream(int(m.group(1)), tounicode_stream(mapping))
                fixed += 1

    print("fixed fonts", fixed)
    t1 = doc[1].get_text()
    print("after p1\n", t1)
    print(
        "checks",
        "रामकृष्ण" in t1,
        "मुम्बई" in t1,
        "ę" in t1,
        "मन्त्रपुष्पम्" in t1,
    )
    print("after p2\n", doc[2].get_text()[:500])
    doc.close()


if __name__ == "__main__":
    main()
