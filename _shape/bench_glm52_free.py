#!/usr/bin/env python3
"""GLM 5.2 free vs Ox Alpha Russian drafts (text only)."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path("/opt/sanskrit_srv")
sys.path.insert(0, str(ROOT / "backend"))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.services.llm_draft import extract_html_only
from app.services.llm_translate import _call_openrouter_text
from app.services.translation_style import build_translate_prompt

MODEL = "z-ai/glm-5.2:free"
PAGES = [35, 124, 260]
REF = Path("/tmp/or_bench_refs")
OUT = Path("/tmp/or_bench_out")


def grams(s: str, n: int = 4) -> set[str]:
    return {s[i : i + n] for i in range(max(0, len(s) - n + 1))}


def plain(html: str, cyr: bool) -> str:
    t = re.sub(r"<[^>]+>", " ", extract_html_only(html or ""))
    t = re.sub(r"\s+", "", t)
    if cyr:
        return "".join(c for c in t if "\u0400" <= c <= "\u04ff")
    return "".join(c for c in t if "\u0900" <= c <= "\u097f")


def overlap(a: str, b: str, cyr: bool) -> float:
    x, y = grams(plain(a, cyr)), grams(plain(b, cyr))
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def main() -> None:
    key = os.environ["OPENROUTER_API_KEY"].strip()
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for n in PAGES:
        src = (REF / f"{n:04d}_src.html").read_text(encoding="utf-8")
        ru = (REF / f"{n:04d}_ru.html").read_text(encoding="utf-8")
        prompt = build_translate_prompt(
            source_html=src,
            cfg={"style": "interlinear", "english_comments": "replace", "notes": ""},
        )
        t0 = time.time()
        err = None
        html = ""
        usage = {}
        try:
            html, usage = _call_openrouter_text(key, base, MODEL, prompt)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:500]
        elapsed = round(time.time() - t0, 1)
        if html:
            (OUT / f"{n:04d}__tr__glm52free.html").write_text(html, encoding="utf-8")
        raw = extract_html_only(html) if html else ""
        ov_ru = round(overlap(raw, ru, True), 3)
        ov_sa = round(overlap(raw, src, False), 3)
        cyr = sum(1 for c in raw if "\u0400" <= c <= "\u04ff")
        sa = sum(1 for c in raw if "\u0900" <= c <= "\u097f")
        article = "<article" in raw.lower()
        print(
            f"p.{n} {'ERR' if err else 'OK'} {elapsed}s "
            f"article={article} ru_ov={ov_ru} sa_ov_vs_src={ov_sa} "
            f"cyr={cyr} sa={sa} tok={usage.get('total_tokens')} {(err or '')[:120]}",
            flush=True,
        )
        if html:
            # short preview of first Russian paragraph
            m = re.search(r'lang="ru"[^>]*>(.*?)</p>', raw, re.S)
            preview = re.sub(r"\s+", " ", (m.group(1) if m else raw[:180]))[:180]
            print("  ru:", preview, flush=True)
        rows.append(
            {
                "page_no": n,
                "error": err,
                "elapsed_s": elapsed,
                "overlap_ru_vs_alpha": ov_ru,
                "overlap_sa_vs_src": ov_sa,
                "cyr_chars": cyr,
                "sa_chars": sa,
                "has_article": article,
                "usage": usage,
            }
        )
    (OUT / "glm52_summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
