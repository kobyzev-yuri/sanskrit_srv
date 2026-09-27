"""Local Tesseract OCR for digitize drafts (KSTS / Bombay Devanagari)."""
from __future__ import annotations

import html
import logging
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

from app.config import get_settings

log = logging.getLogger("sanskrit.tesseract_ocr")

_WS = re.compile(r"\s+")
_CHAR_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
)


def _norm_line(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.translate(_CHAR_MAP)
    return _WS.sub(" ", text).strip()


def tessdata_dir() -> Path:
    settings = get_settings()
    configured = (settings.tessdata_dir or "").strip()
    if configured:
        return Path(configured)
    # backend/app/services/tesseract_ocr.py → backend/tessdata
    return Path(__file__).resolve().parents[2] / "tessdata"


def tesseract_cmd() -> str:
    settings = get_settings()
    cmd = (settings.tesseract_cmd or "").strip() or "tesseract"
    if Path(cmd).is_file():
        return cmd
    found = shutil.which(cmd)
    if found:
        return found
    raise RuntimeError(
        f"tesseract not found ({cmd!r}). Install tesseract-ocr on the server."
    )


def tesseract_available() -> bool:
    try:
        tesseract_cmd()
    except RuntimeError:
        return False
    lang = (get_settings().tesseract_lang or "ksts").split("+")[0]
    return (tessdata_dir() / f"{lang}.traineddata").is_file()


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["TESSDATA_PREFIX"] = str(tessdata_dir())
    return env


def ocr_page_lines(image_path: Path, *, lang: str | None = None, psm: int = 4) -> list[str]:
    """Return non-empty text lines from a page image (TSV level-4 / word join)."""
    settings = get_settings()
    lang = lang or settings.tesseract_lang or "ksts"
    cmd = tesseract_cmd()
    proc = subprocess.run(
        [cmd, str(image_path), "stdout", "-l", lang, "--psm", str(psm), "tsv"],
        capture_output=True,
        text=True,
        env=_env(),
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-400:]
        raise RuntimeError(f"tesseract failed: {err}")

    boxes: dict[tuple[int, int, int], list[str]] = {}
    order: list[tuple[int, int, int]] = []
    for row in (proc.stdout or "").splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12:
            continue
        try:
            level = int(parts[0])
            block, par, line = map(int, parts[2:5])
            conf = float(parts[10])
        except ValueError:
            continue
        key = (block, par, line)
        text = parts[11].strip() if parts[11] else ""
        if level == 4:
            boxes.setdefault(key, [])
            if key not in order:
                order.append(key)
        elif level == 5 and text and conf >= 0:
            boxes.setdefault(key, []).append(text)
            if key not in order:
                order.append(key)

    lines: list[str] = []
    for key in order:
        line = _norm_line(" ".join(boxes.get(key, [])))
        if line:
            lines.append(line)

    # Fallback: plain text if TSV yielded nothing
    if not lines:
        proc2 = subprocess.run(
            [cmd, str(image_path), "stdout", "-l", lang, "--psm", str(psm)],
            capture_output=True,
            text=True,
            env=_env(),
            check=False,
        )
        for ln in (proc2.stdout or "").splitlines():
            line = _norm_line(ln)
            if line:
                lines.append(line)
    return lines


def lines_to_html(lines: list[str], page_no: int) -> str:
    """One printed line → one <p class=\"sa\">, matching digitize editor convention."""
    if not lines:
        return (
            f'<article class="page-style type-md lh-normal" lang="sa" data-page="{page_no}">\n'
            f'  <p class="sa" lang="sa"></p>\n'
            f"</article>\n"
        )
    body = "\n".join(
        f'  <p class="sa" lang="sa">{html.escape(line)}</p>' for line in lines
    )
    return (
        f'<article class="page-style type-md lh-normal" lang="sa" data-page="{page_no}">\n'
        f"{body}\n"
        f"</article>\n"
    )


def ocr_page_html(image_path: Path, page_no: int, *, lang: str | None = None) -> str:
    lines = ocr_page_lines(image_path, lang=lang)
    return lines_to_html(lines, page_no)
