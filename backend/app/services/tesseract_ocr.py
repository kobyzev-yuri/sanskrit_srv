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


def _default_psm() -> int:
    """PSM 6 (uniform text block) keeps body+footnotes on KSTS plates.

    PSM 4 (variable column) often dropped the main text above footnotes.
    """
    raw = getattr(get_settings(), "tesseract_psm", 6)
    try:
        psm = int(raw)
    except (TypeError, ValueError):
        psm = 6
    return psm if 0 <= psm <= 13 else 6


def _run_tesseract(image_path: Path, lang: str, psm: int, *extra: str) -> subprocess.CompletedProcess[str]:
    cmd = tesseract_cmd()
    return subprocess.run(
        [cmd, str(image_path), "stdout", "-l", lang, "--psm", str(psm), *extra],
        capture_output=True,
        text=True,
        env=_env(),
        check=False,
    )


def _lines_from_tsv(stdout: str) -> list[str]:
    boxes: dict[tuple[int, int, int], list[str]] = {}
    order: list[tuple[int, int, int]] = []
    for row in (stdout or "").splitlines()[1:]:
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
    return lines


def _lines_from_plain(stdout: str) -> list[str]:
    lines: list[str] = []
    for ln in (stdout or "").splitlines():
        line = _norm_line(ln)
        if line:
            lines.append(line)
    return lines


def _density(lines: list[str]) -> int:
    return sum(len(x) for x in lines)


def ocr_page_lines(image_path: Path, *, lang: str | None = None, psm: int | None = None) -> list[str]:
    """Return non-empty text lines from a page image."""
    settings = get_settings()
    lang = lang or settings.tesseract_lang or "ksts"
    psm = _default_psm() if psm is None else int(psm)

    proc_tsv = _run_tesseract(image_path, lang, psm, "tsv")
    if proc_tsv.returncode != 0:
        err = (proc_tsv.stderr or proc_tsv.stdout or "")[-400:]
        raise RuntimeError(f"tesseract failed: {err}")
    tsv_lines = _lines_from_tsv(proc_tsv.stdout or "")

    proc_plain = _run_tesseract(image_path, lang, psm)
    plain_lines = _lines_from_plain(proc_plain.stdout or "") if proc_plain.returncode == 0 else []

    # Prefer the denser extraction — TSV line-join can drop blocks on mixed layouts.
    if _density(plain_lines) > _density(tsv_lines):
        if tsv_lines and _density(plain_lines) > _density(tsv_lines) * 1.15:
            log.info(
                "ocr %s psm=%s: plain denser (%s chars) than tsv (%s)",
                image_path.name,
                psm,
                _density(plain_lines),
                _density(tsv_lines),
            )
        return plain_lines
    return tsv_lines or plain_lines


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
