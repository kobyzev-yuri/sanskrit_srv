#!/usr/bin/env python3
"""Compare free OpenRouter vision/text models vs Ox Alpha drafts.

Run on the VPS (has OPENROUTER_API_KEY). Does not print the key.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.services.llm_draft import (  # noqa: E402
    BASE_PROMPT,
    _call_openrouter,
    extract_html_only,
    image_to_jpeg_b64,
)
from app.services.llm_translate import _call_openrouter_text  # noqa: E402
from app.services.translation_style import build_translate_prompt  # noqa: E402

PAGES = [35, 124, 260]
MODELS = [
    "thinkingmachines/inkling:free",
    "thinkingmachines/inkling-small:free",
    "minimax/minimax-m3:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-31b-it:free",
]

SCAN_DIR = Path("/opt/sanskrit_srv/storage/projects/5862e121-b4d4-4909-ab06-30a7ff427067/pages")
REF_DIR = Path("/tmp/or_bench_refs")
OUT_DIR = Path("/tmp/or_bench_out")

# Local fallback when not on VPS
LOCAL = ROOT / "_shape" / "bench_sergey_out" / "random10"
if not SCAN_DIR.is_dir():
    SCAN_DIR = LOCAL / "scans"
if not REF_DIR.is_dir():
    REF_DIR = LOCAL
    OUT_DIR = LOCAL / "or_free_out"


def _grams(s: str, n: int = 4) -> set[str]:
    return {s[i : i + n] for i in range(max(0, len(s) - n + 1))}


def _plain(html: str, kind: str) -> str:
    t = extract_html_only(html or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", "", t)
    if kind == "sa":
        return "".join(c for c in t if "\u0900" <= c <= "\u097f" or c in "।॥ॐऽ")
    return "".join(c for c in t if "\u0400" <= c <= "\u04ff")


def overlap(pred: str, ref: str, kind: str) -> float:
    a, b = _grams(_plain(pred, kind)), _grams(_plain(ref, kind))
    if not a and not b:
        return 0.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def garbage(html: str) -> bool:
    return bool(
        re.search(
            r"The user wants|Let's look|thinking|```|I need to|Judge the scan|As an AI",
            html or "",
            re.I,
        )
    )


def score_html(html: str, ref: str, kind: str) -> dict:
    raw = extract_html_only(html or "")
    ov = round(overlap(raw, ref, kind), 3)
    if kind == "sa":
        chars = sum(1 for c in raw if "\u0900" <= c <= "\u097f")
        tones = len(re.findall(r"[॒॑]", raw))
    else:
        chars = sum(1 for c in raw if "\u0400" <= c <= "\u04ff")
        tones = 0
    has_article = "<article" in raw.lower()
    gab = garbage(raw)
    ok = has_article and chars >= 40 and not gab
    return {
        "ok": ok,
        "overlap_alpha": ov,
        "chars": chars,
        "tones": tones,
        "garbage": gab,
        "has_article": has_article,
        "len": len(raw),
        "alpha_len": len(ref or ""),
    }


def call_digitize(key: str, base: str, model: str, page_no: int, scan: Path) -> tuple[str, dict]:
    image_b64 = image_to_jpeg_b64(scan)
    user_text = (
        BASE_PROMPT
        + f"\n\nPage number: {page_no}.\n"
        "Produce a complete layout-faithful draft for the whole page "
        "(line-by-line classes only; no inline CSS). "
        "Output ONLY the HTML fragment — no English commentary, no step lists."
    )
    return _call_openrouter(key, base, model, user_text, image_b64)


def call_translate(key: str, base: str, model: str, source_html: str) -> tuple[str, dict]:
    prompt = build_translate_prompt(
        source_html=source_html,
        cfg={"style": "interlinear", "english_comments": "replace", "notes": ""},
    )
    return _call_openrouter_text(key, base, model, prompt)


def main() -> None:
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    base = (os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").strip()
    if not key:
        raise SystemExit("OPENROUTER_API_KEY missing")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    for page_no in PAGES:
        scan = SCAN_DIR / f"{page_no:04d}.png"
        dig_ref = (REF_DIR / f"{page_no:04d}_dig.html").read_text(encoding="utf-8")
        src_ref = (REF_DIR / f"{page_no:04d}_src.html").read_text(encoding="utf-8")
        ru_ref = (REF_DIR / f"{page_no:04d}_ru.html").read_text(encoding="utf-8")
        print(f"\n======== page {page_no} scan={scan.is_file()} ========", flush=True)

        for model in MODELS:
            safe = model.replace("/", "_").replace(":", "_")
            # digitize
            t0 = time.time()
            err = None
            html = ""
            usage: dict = {}
            try:
                html, usage = call_digitize(key, base, model, page_no, scan)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:400]
            elapsed = round(time.time() - t0, 1)
            sc = score_html(html, dig_ref, "sa") if html and not err else None
            if html:
                (OUT_DIR / f"{page_no:04d}__dig__{safe}.html").write_text(html, encoding="utf-8")
            mark = "ERR" if err else ("OK" if sc and sc["ok"] else "WEAK")
            print(
                f"  DIG {model:42} {mark:4} ov={sc['overlap_alpha'] if sc else '-':>5} "
                f"sa={sc['chars'] if sc else '-':>4} {elapsed}s {(err or '')[:90]}",
                flush=True,
            )
            rows.append(
                {
                    "task": "digitize",
                    "page_no": page_no,
                    "model": model,
                    "error": err,
                    "elapsed_s": elapsed,
                    "usage": usage,
                    "score": sc,
                }
            )

            # translate from Alpha Sanskrit (fair: same source as Alpha used)
            t0 = time.time()
            err = None
            html = ""
            usage = {}
            try:
                html, usage = call_translate(key, base, model, src_ref)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:400]
            elapsed = round(time.time() - t0, 1)
            sc = score_html(html, ru_ref, "ru") if html and not err else None
            if html:
                (OUT_DIR / f"{page_no:04d}__tr__{safe}.html").write_text(html, encoding="utf-8")
            mark = "ERR" if err else ("OK" if sc and sc["ok"] else "WEAK")
            print(
                f"  TR  {model:42} {mark:4} ov={sc['overlap_alpha'] if sc else '-':>5} "
                f"ru={sc['chars'] if sc else '-':>4} {elapsed}s {(err or '')[:90]}",
                flush=True,
            )
            rows.append(
                {
                    "task": "translate",
                    "page_no": page_no,
                    "model": model,
                    "error": err,
                    "elapsed_s": elapsed,
                    "usage": usage,
                    "score": sc,
                }
            )

    summary = OUT_DIR / "summary.json"
    summary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n======== MEAN overlap vs Ox Alpha ========", flush=True)
    for task in ("digitize", "translate"):
        print(f"-- {task} --")
        for model in MODELS:
            rs = [r for r in rows if r["task"] == task and r["model"] == model]
            ovs = [r["score"]["overlap_alpha"] for r in rs if r.get("score")]
            errs = sum(1 for r in rs if r["error"])
            mean = round(sum(ovs) / len(ovs), 3) if ovs else None
            print(f"  {model:42} mean_ov={mean}  ok={len(ovs)}/{len(rs)}  err={errs}")
    print("wrote", summary)


if __name__ == "__main__":
    main()
