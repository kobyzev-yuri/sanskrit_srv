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
