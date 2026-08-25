"""Layout helpers: book-like HTML classes, figure crops from scan, asset paths."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from PIL import Image

from app.services.storage import project_dir

# <figure class="scan-crop" data-box="x,y,w,h"></figure> — fractions of page image
CROP_RE = re.compile(
    r'<figure([^>]*?)class="([^"]*\bscan-crop\b[^"]*)"([^>]*?)>(.*?)</figure>',
    re.I | re.S,
)
BOX_ATTR_RE = re.compile(r'data-box=["\']([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)["\']', re.I)
FIG_IMG_RE = re.compile(r'<img([^>]*?)data-fig=["\'](\d+)["\']([^>]*?)/?>', re.I)


def figures_dir(project_id: uuid.UUID, page_no: int) -> Path:
    d = project_dir(project_id) / "pages" / f"{page_no:04d}-figs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def extract_embedded_figures(
    pdf_path: Path,
    project_id: uuid.UUID,
    page_no: int,
    *,
    min_side: int = 40,
    max_page_cover: float = 0.72,
) -> list[dict]:
    """Pull non-full-page embedded images from a PDF page. Returns [{index, path, w, h}]."""
    import fitz

    out: list[dict] = []
    with fitz.open(pdf_path) as doc:
        if page_no < 1 or page_no > doc.page_count:
            return out
        page = doc.load_page(page_no - 1)
        page_area = abs(page.rect.width * page.rect.height) or 1.0
        images = page.get_images(full=True)
        dest = figures_dir(project_id, page_no)
        fig_i = 0
        for img in images:
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n >= 5:  # CMYK etc.
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                if pix.width < min_side or pix.height < min_side:
                    continue
                # Rough cover: image pixel box vs page — use bbox if available
                cover = (pix.width * pix.height) / max(page_area * 4, 1)  # heuristic at ~150dpi-ish
                # Prefer bbox from page.get_image_rects
                rects = page.get_image_rects(xref)
                if rects:
                    r = rects[0]
                    cover = abs(r.width * r.height) / page_area
                if cover >= max_page_cover:
                    # Full-page scan plate — skip as "figure"
                    continue
                fig_i += 1
                path = dest / f"emb-{fig_i:02d}.png"
                pix.save(path.as_posix())
                out.append(
                    {
                        "index": fig_i,
                        "path": str(path),
                        "w": pix.width,
                        "h": pix.height,
                        "kind": "embedded",
                    }
                )
            except Exception:  # noqa: BLE001
                continue
    return out


def materialize_scan_crops(
    html: str,
    scan_path: Path,
    project_id: uuid.UUID,
    page_no: int,
    page_id: uuid.UUID,
) -> str:
    """Turn scan-crop figures into real PNGs + <img> tags with API URLs."""
    if not html or not scan_path.exists():
        return html
    dest = figures_dir(project_id, page_no)
    try:
        im = Image.open(scan_path).convert("RGB")
    except Exception:  # noqa: BLE001
        return html
    W, H = im.size
    crop_i = 0

    def repl(match: re.Match) -> str:
        nonlocal crop_i
        tag = match.group(0)
        box_m = BOX_ATTR_RE.search(tag)
        if not box_m:
            return tag
        x, y, w, h = (float(box_m.group(i)) for i in range(1, 5))
        # Reject near-full-page crops (LLM sometimes wraps the whole plate).
        if w * h >= 0.85:
            return tag
        # clamp fractions
        x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
        w, h = max(0.02, min(1.0 - x, w)), max(0.02, min(1.0 - y, h))
        left, top = int(x * W), int(y * H)
        right, bottom = int((x + w) * W), int((y + h) * H)
        if right - left < 8 or bottom - top < 8:
            return tag
        crop_i += 1
        path = dest / f"crop-{crop_i:02d}.png"
        im.crop((left, top, right, bottom)).save(path, format="PNG")
        url = f"/api/v1/pages/{page_id}/figures/crop-{crop_i:02d}.png"
        return f'<figure class="page-figure"><img src="{url}" alt="illustration" /></figure>'

    # Any <figure> that mentions scan-crop (class order / extra attrs vary).
    loose = re.compile(
        r"<figure\b[^>]*\bscan-crop\b[^>]*>.*?</figure>",
        re.I | re.S,
    )
    try:
        return loose.sub(repl, html)
    except Exception:  # noqa: BLE001
        # Never wipe a successful LLM draft because crop materialization failed.
        return html


def rewrite_embedded_fig_srcs(html: str, page_id: uuid.UUID) -> str:
    """data-fig=\"N\" → API figure URL."""

    def repl(m: re.Match) -> str:
        n = int(m.group(2))
        url = f"/api/v1/pages/{page_id}/figures/emb-{n:02d}.png"
        pre, post = m.group(1), m.group(3)
        # drop existing src if any
        pre = re.sub(r'\s*src=["\'][^"\']*["\']', "", pre)
        post = re.sub(r'\s*src=["\'][^"\']*["\']', "", post)
        return f'<img{pre} src="{url}" data-fig="{n}"{post}>'

    return FIG_IMG_RE.sub(repl, html)


# /api/v1/pages/<uuid>/figures/crop-01.png  (or emb-01.png)
# UUID group is loose: translate LLM often drops one hex digit.
_FIG_URL_RE = re.compile(
    r"""(/api/v1/pages/)([0-9a-fA-F-]{20,48})(/figures/)((?:emb|crop)-\d{2}\.png)""",
    re.I,
)
_FIG_SRC_ATTR_RE = re.compile(
    r"""src=["'](/api/v1/pages/[0-9a-fA-F-]{20,48}/figures/(?:emb|crop)-\d{2}\.png)["']""",
    re.I,
)
_BLOB_SRC_RE = re.compile(r"""src=["']blob:[^"']+["']""", re.I)


def _figure_urls_from_html(html: str) -> dict[str, str]:
    """Map figure filename → full /api/.../figures/name URL (first wins)."""
    out: dict[str, str] = {}
    for m in _FIG_URL_RE.finditer(html or ""):
        name = m.group(4).lower()
        if name not in out:
            out[name] = f"/api/v1/pages/{m.group(2)}/figures/{m.group(4)}"
    return out


def preserve_figure_srcs(source_html: str, html: str) -> str:
    """Restore correct figure URLs from Sanskrit source into translated/draft HTML.

    The translate LLM often corrupts page UUIDs in <img src> (one missing hex → 404)
    while the left-pane source_html still has working links. Also strips accidental blob: URLs.
    """
    src_map = _figure_urls_from_html(source_html or "")
    if not src_map and not _BLOB_SRC_RE.search(html or ""):
        return html or ""

    out = html or ""

    if src_map:

        def fix_src_attr(m: re.Match) -> str:
            url = m.group(1)
            name_m = re.search(r"/figures/((?:emb|crop)-\d{2}\.png)", url, re.I)
            if not name_m:
                return m.group(0)
            good = src_map.get(name_m.group(1).lower())
            if not good:
                return m.group(0)
            return f'src="{good}"'

        out = _FIG_SRC_ATTR_RE.sub(fix_src_attr, out)

    if src_map and _BLOB_SRC_RE.search(out):
        names = list(src_map.keys())
        i = 0

        def repl_blob(m: re.Match) -> str:
            nonlocal i
            if i < len(names):
                url = src_map[names[i]]
                i += 1
                return f'src="{url}"'
            return 'src=""'

        out = _BLOB_SRC_RE.sub(repl_blob, out)

    return out


def finalize_page_html(
    html: str,
    *,
    scan_path: Path | None,
    project_id: uuid.UUID,
    page_no: int,
    page_id: uuid.UUID,
) -> str:
    from app.services.llm_draft import normalize_vedic_marks

    html = normalize_vedic_marks(html or "")
    html = rewrite_embedded_fig_srcs(html, page_id)
    if scan_path and Path(scan_path).exists():
        html = materialize_scan_crops(html, Path(scan_path), project_id, page_no, page_id)
    return html


def figure_file(project_id: uuid.UUID, page_no: int, name: str) -> Path | None:
    """Resolve a safe figure filename under the page figs dir."""
    if not re.fullmatch(r"(emb|crop)-\d{2}\.png", name):
        return None
    path = figures_dir(project_id, page_no) / name
    return path if path.is_file() else None


def figure_file_from_page_id(page_id: uuid.UUID, name: str) -> Path | None:
    """Resolve a figure via the page UUID in /api/v1/pages/<id>/figures/name."""
    from app.db import get_session_factory
    from app.models import Page

    Session = get_session_factory()
    with Session() as db:
        page = db.get(Page, page_id)
        if page is None:
            return None
        return figure_file(page.project_id, page.page_no, name)


def resolve_figure_path(
    src: str,
    *,
    project_id: uuid.UUID,
    page_no: int,
    source_project_id: uuid.UUID | None = None,
) -> Path | None:
    """Find a crop/emb PNG for export. Translation HTML keeps the source page UUID."""
    if not src:
        return None
    m = re.search(
        r"/api/v1/pages/([0-9a-fA-F-]{32,48})/figures/((?:emb|crop)-\d{2}\.png)",
        src,
        re.I,
    )
    name = m.group(2) if m else Path(src.split("?")[0]).name
    if not re.fullmatch(r"(emb|crop)-\d{2}\.png", name or ""):
        return None
    for pid in (project_id, source_project_id):
        if pid is None:
            continue
        path = figure_file(pid, page_no, name)
        if path is not None:
            return path
    if m:
        try:
            page_id = uuid.UUID(m.group(1))
        except ValueError:
            return None
        return figure_file_from_page_id(page_id, name)
    return None
