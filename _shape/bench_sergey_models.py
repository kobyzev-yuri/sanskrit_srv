#!/usr/bin/env python3
"""A/B vision models on Sergey Arkhipov hard cases via ProxyAPI.

Usage:
  _shape/.bench_venv/bin/python _shape/bench_sergey_models.py
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path("/home/cnn/.cursor/projects/home-cnn-sanskrit/assets")
OUT_DIR = Path(__file__).resolve().parent / "bench_sergey_out"

# Compact prompt = same fidelity rules as llm_draft.BASE_PROMPT (tones off, gum exact).
PROMPT = """You restore a Devanagari scan into an HTML fragment. IMAGE is ground truth.

FIDELITY OVER MEMORY (critical — diplomatic transcription, not editing):
- Encode ONLY what is printed. Do NOT substitute dictionary / remembered mantra spellings.
- Familiar hymns are highest risk: if the plate prints गणपतिग्ं / ऋतग्ं / ग्ं, keep that — NEVER rewrite as गणपतिं / ऋतं.
- Do NOT rewrite ordinary plate …ं… into …ग्ं. Gum only where half-ग + ं is visible.
- Never insert an extra akṣara for sandhi (wrong: मीश्वर-रस्सर्व with spurious र).

VEDIC SVARA — OMIT ENTIRELY:
- Do NOT emit ॑ (U+0951) or ॒ (U+0952). No HTML <u>, underscore, or Latin combining fakes.

LAYOUT: wrap in <article class="page-style type-md lh-normal" lang="sa">; one printed line → one <p class="sa"> / shloka.
Return ONLY raw HTML starting with <article>. No markdown, no commentary.
"""

MODELS = [
    ("gemini", "gemini-2.5-flash"),
    ("gemini", "gemini-2.5-pro"),
    ("gemini", "gemini-3.1-pro-preview"),
    ("openai", "gpt-4o"),
    ("openai", "gpt-4o-mini"),
]

CASES = [
    {
        "id": "rta_crop",
        "image": ASSETS / "image-d5b588ca-0e82-43e9-a4b2-9300413bea82.png",
        "note": "Sergey crop ऋतग्ं",
        "checks": [
            ("gum_rta", r"ऋतग्ं", True),
            ("not_dict_rta", r"ऋतं(?![ा-ौ्])", False),
            ("not_wrong_gum", r"ऋतगुंँ|ऋतꣳ", False),
        ],
    },
    {
        "id": "ganapati_crop",
        "image": ASSETS / "image-f738fab6-780e-433e-a87f-213fcbf598b9.png",
        "note": "Sergey crop गणपतिग्ं",
        "checks": [
            ("has_ggum", r"ग्ं", True),
            ("not_dict_ganapati", r"गणपतिं", False),
            ("not_wrong_gum", r"गुंँ", False),
        ],
    },
    {
        "id": "stitched_gum",
        "image": OUT_DIR / "stitched_gum.png",
        "note": "stitched Sergey crops: गणपतिग्ं + ऋतग्ं",
        "checks": [
            ("gum_ganapati", r"गणपतिग्ं", True),
            ("not_dict_ganapati", r"गणपतिं", False),
            ("gum_rta", r"ऋतग्ं", True),
            ("not_dict_rta", r"ऋतं(?![ा-ौ्])", False),
        ],
    },
]


def load_env() -> dict[str, str]:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise SystemExit("OPENAI_API_KEY missing in sanskrit_srv/.env")
    return {
        "key": key,
        "gemini_base": os.environ.get("GEMINI_BASE_URL", "https://api.proxyapi.ru/google"),
        "openai_base": os.environ.get("OPENAI_BASE_URL", "https://api.proxyapi.ru/openai/v1"),
    }


def image_to_jpeg_b64(path: Path, max_px: int = 2048) -> str:
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def extract_html_only(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:html)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    low = text.lower()
    start = low.find("<article")
    if start < 0:
        start = low.find("<p")
    if start > 0:
        text = text[start:]
    return text.strip()


def call_gemini(env: dict, model: str, user_text: str, image_b64: str) -> tuple[str, dict]:
    url = f"{env['gemini_base'].rstrip('/')}/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": user_text},
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
    }
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {env['key']}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        raise RuntimeError("empty text")
    usage = data.get("usageMetadata") or {}
    return text, {
        "prompt": usage.get("promptTokenCount"),
        "completion": usage.get("candidatesTokenCount"),
        "total": usage.get("totalTokenCount"),
    }


def call_openai(env: dict, model: str, user_text: str, image_b64: str) -> tuple[str, dict]:
    url = f"{env['openai_base'].rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 8192,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            }
        ],
    }
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {env['key']}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("empty choices")
    text = choices[0].get("message", {}).get("content") or ""
    u = data.get("usage") or {}
    return text, {
        "prompt": u.get("prompt_tokens"),
        "completion": u.get("completion_tokens"),
        "total": u.get("total_tokens"),
    }


def score(html: str, checks: list[tuple[str, str, bool]]) -> dict:
    raw = extract_html_only(html) if "<" in html else html
    results = {}
    ok_n = 0
    for name, pat, want_present in checks:
        found = bool(re.search(pat, raw))
        passed = found if want_present else (not found)
        results[name] = {"pass": passed, "found": found}
        if passed:
            ok_n += 1
    tones = len(re.findall(r"[॒॑]", raw))
    fake_tones = len(re.findall(r"[\u0346\u0304\u0305\u0323\u0303\u0307]", raw))
    results["_meta"] = {
        "checks_ok": ok_n,
        "checks_total": len(checks),
        "real_tones": tones,
        "fake_tones": fake_tones,
        "tone_policy_ok": tones == 0 and fake_tones == 0,
    }
    return results


def snippet_hits(html: str) -> str:
    hits = []
    for pat in (
        r".{0,12}गणपति[^\s<]{0,12}",
        r".{0,8}ऋत[^\s<]{0,10}",
        r".{0,10}मीश्वर[^\s<]{0,16}",
        r".{0,6}ग्ं.{0,6}",
    ):
        m = re.search(pat, html)
        if m:
            hits.append(m.group(0).replace("\n", " "))
    return " | ".join(dict.fromkeys(hits))[:180]


def main() -> None:
    env = load_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []

    for case in CASES:
        img = case["image"]
        if not img.is_file():
            print(f"SKIP missing {img}")
            continue
        max_px = 2048 if min(Image.open(img).size) > 200 else 1024
        image_b64 = image_to_jpeg_b64(img, max_px=max_px)
        user_text = (
            PROMPT
            + f"\nPage number: 1.\nCase: {case['id']} — {case['note']}.\n"
            "Produce HTML for the whole image. Output ONLY the HTML fragment."
        )
        print(f"\n=== CASE {case['id']} ({img.name}) ===", flush=True)

        for network, model in MODELS:
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
                err = str(exc)[:240]
            elapsed = round(time.time() - t0, 1)
            sc = score(html, case["checks"]) if html and not err else None
            out_path = OUT_DIR / f"{case['id']}__{network}__{model.replace('/', '_')}.html"
            if html:
                out_path.write_text(html, encoding="utf-8")
            meta = (sc or {}).get("_meta", {})
            checks_ok = meta.get("checks_ok")
            checks_total = meta.get("checks_total")
            tone_ok = meta.get("tone_policy_ok")
            detail = snippet_hits(html) if html else err
            print(
                f"  {label:40} "
                f"{'ERR' if err else f'{checks_ok}/{checks_total}'} "
                f"tones={'ok' if tone_ok else meta.get('real_tones')} "
                f"{elapsed}s tok={usage.get('total')} "
                f"{detail}",
                flush=True,
            )
            rows.append(
                {
                    "case": case["id"],
                    "model": label,
                    "error": err,
                    "elapsed_s": elapsed,
                    "usage": usage,
                    "score": sc,
                    "snippet": snippet_hits(html) if html else None,
                    "out": str(out_path) if html else None,
                }
            )

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n======== SUMMARY (checks + tone policy) ========", flush=True)
    by_model: dict[str, list] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)
    for model, rs in by_model.items():
        ok = total = tones_ok = cases_n = errs = 0
        for r in rs:
            if r["error"]:
                errs += 1
                continue
            cases_n += 1
            m = r["score"]["_meta"]
            ok += m["checks_ok"]
            total += m["checks_total"]
            tones_ok += int(m["tone_policy_ok"])
        print(
            f"{model:40} checks {ok}/{total}  "
            f"tone-clean {tones_ok}/{cases_n}  errors {errs}",
            flush=True,
        )
    print(f"\nWrote {summary_path}", flush=True)


if __name__ == "__main__":
    main()
