#!/usr/bin/env python3
"""Compare vision models on 10 random Mantra Pushpam scans from the VPS."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from bench_sergey_models import (
    MODELS,
    PROMPT,
    call_gemini,
    call_openai,
    extract_html_only,
    image_to_jpeg_b64,
    load_env,
)

ROOT = Path(__file__).resolve().parent / "bench_sergey_out" / "random10"
SCANS = ROOT / "scans"
REF = ROOT / "ref_html"
OUT = ROOT / "model_out"

# Keep cost reasonable: Flash vs Pro vs GPT-4o (+ mini as cheap baseline)
MODELS_RUN = [
    ("gemini", "gemini-2.5-flash"),
    ("gemini", "gemini-2.5-pro"),
    ("openai", "gpt-4o"),
]


def plain_dev(html: str) -> str:
    t = extract_html_only(html)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", "", t)
    return "".join(c for c in t if "\u0900" <= c <= "\u097f" or c in "।॥ॐऽंँः्ा-ौॆ-ॣ")


def score(html: str, ref_html: str) -> dict:
    raw = extract_html_only(html) if html else ""
    dev = sum(1 for c in raw if "\u0900" <= c <= "\u097f")
    tones = len(re.findall(r"[॒॑]", raw))
    fake = len(re.findall(r"[\u0346\u0304\u0305\u0323\u0303\u0307]", raw))
    garbage = bool(
        re.search(
            r"The user wants|Let's look|thinking|```|Judge the scan|I need to",
            raw,
            re.I,
        )
    )
    has_article = "<article" in raw.lower()
    pred = plain_dev(raw)
    ref = plain_dev(ref_html or "")
    # token overlap vs existing draft (soft — draft itself may be wrong)
    overlap = 0.0
    if pred and ref:
        # char 4-gram Jaccard
        def grams(s: str) -> set[str]:
            return {s[i : i + 4] for i in range(max(0, len(s) - 3))}

        a, b = grams(pred), grams(ref)
        overlap = len(a & b) / len(a | b) if a | b else 0.0
    ok = has_article and dev >= 40 and not garbage and tones == 0 and fake == 0
    return {
        "ok": ok,
        "dev_chars": dev,
        "tones": tones,
        "fake_tones": fake,
        "garbage": garbage,
        "has_article": has_article,
        "overlap_ref": round(overlap, 3),
        "pred_len": len(pred),
        "ref_len": len(ref),
    }


def main() -> None:
    env = load_env()
    pages = json.loads((ROOT / "pages.json").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []

    for page in pages:
        pn = page["page_no"]
        scan = SCANS / f"{pn:04d}.png"
        if not scan.is_file():
            print(f"SKIP missing {scan}", flush=True)
            continue
        ref = (REF / f"{pn:04d}.html").read_text(encoding="utf-8") if (REF / f"{pn:04d}.html").is_file() else ""
        image_b64 = image_to_jpeg_b64(scan, max_px=2048)
        user_text = (
            PROMPT
            + f"\nPage number: {pn}.\n"
            "Produce a complete layout-faithful draft for the whole page. "
            "Output ONLY the HTML fragment."
        )
        print(f"\n=== page {pn:04d} ===", flush=True)

        for network, model in MODELS_RUN:
            label = f"{network}:{model}"
            t0 = time.time()
            err = None
            html = ""
            usage: dict = {}
            try:
                if network == "gemini":
                    html, usage = call_gemini(env, model, user_text, image_b64)
                else:
                    html, usage = call_openai(env, model, user_text, image_b64)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:300]
            elapsed = round(time.time() - t0, 1)
            sc = score(html, ref) if html and not err else None
            out_path = OUT / f"{pn:04d}__{network}__{model.replace('/', '_')}.html"
            if html:
                out_path.write_text(html, encoding="utf-8")
            print(
                f"  {label:32} "
                f"{'ERR' if err else ('OK' if sc and sc['ok'] else 'WEAK')} "
                f"dev={sc['dev_chars'] if sc else '-'} "
                f"ov={sc['overlap_ref'] if sc else '-'} "
                f"tones={sc['tones'] if sc else '-'} "
                f"{elapsed}s tok={usage.get('total')} "
                f"{(err or '')[:80]}",
                flush=True,
            )
            rows.append(
                {
                    "page_no": pn,
                    "model": label,
                    "error": err,
                    "elapsed_s": elapsed,
                    "usage": usage,
                    "score": sc,
                    "out": str(out_path) if html else None,
                }
            )

    summary = ROOT / "summary.json"
    summary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n======== SUMMARY ========", flush=True)
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["model"], []).append(r)
    for model, rs in by.items():
        ok = sum(1 for r in rs if r.get("score") and r["score"]["ok"])
        errs = sum(1 for r in rs if r["error"])
        ovs = [r["score"]["overlap_ref"] for r in rs if r.get("score")]
        tones = sum((r["score"]["tones"] or 0) for r in rs if r.get("score"))
        avg_ov = round(sum(ovs) / len(ovs), 3) if ovs else 0
        tok = sum((r.get("usage") or {}).get("total") or 0 for r in rs)
        sec = round(sum(r["elapsed_s"] for r in rs), 1)
        print(
            f"{model:32} ok {ok}/{len(rs)}  avg_overlap {avg_ov}  "
            f"tones_total {tones}  errors {errs}  {sec}s  tok≈{tok}",
            flush=True,
        )
    print(f"\nWrote {summary}", flush=True)


if __name__ == "__main__":
    main()
