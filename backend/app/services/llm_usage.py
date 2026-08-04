"""Record and aggregate LLM token usage per project (billing)."""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import LlmUsageEvent


def parse_gemini_usage(data: dict) -> dict[str, Any]:
    meta = data.get("usageMetadata") or data.get("usage_metadata") or {}
    prompt = int(meta.get("promptTokenCount") or meta.get("prompt_token_count") or 0)
    completion = int(
        meta.get("candidatesTokenCount")
        or meta.get("candidates_token_count")
        or meta.get("outputTokenCount")
        or 0
    )
    total = int(meta.get("totalTokenCount") or meta.get("total_token_count") or (prompt + completion))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "usage_raw": meta if isinstance(meta, dict) else {"raw": meta},
    }


def parse_openai_usage(data: dict) -> dict[str, Any]:
    usage = data.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "usage_raw": usage if isinstance(usage, dict) else {"raw": usage},
    }


def record_usage(
    db: Session,
    *,
    project_id: uuid.UUID,
    network: str,
    model: str,
    usage: dict[str, Any],
    operation: str = "auto_draft",
    page_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> LlmUsageEvent:
    event = LlmUsageEvent(
        project_id=project_id,
        page_id=page_id,
        job_id=job_id,
        network=network,
        model=model,
        operation=operation,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        usage_raw=usage.get("usage_raw") or usage,
        ok=True,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _price_table() -> dict[str, dict[str, float]]:
    raw = (get_settings().llm_price_per_1m or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        out[str(key)] = {
            "in": float(val.get("in") or val.get("prompt") or 0),
            "out": float(val.get("out") or val.get("completion") or 0),
        }
    return out


def estimate_usd(network: str, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    table = _price_table()
    key = f"{network}:{model}"
    rates = table.get(key) or table.get(model)
    if not rates:
        return None
    return (prompt_tokens * rates["in"] + completion_tokens * rates["out"]) / 1_000_000.0


def project_usage_summary(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    events = list(
        db.scalars(
            select(LlmUsageEvent).where(
                LlmUsageEvent.project_id == project_id,
                LlmUsageEvent.ok.is_(True),
            )
        ).all()
    )

    tot_p = tot_c = tot_t = 0
    by_net: dict[str, dict[str, int]] = {}
    by_model: dict[tuple[str, str], dict[str, int]] = {}

    for e in events:
        tot_p += e.prompt_tokens
        tot_c += e.completion_tokens
        tot_t += e.total_tokens
        bn = by_net.setdefault(
            e.network, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        )
        bn["prompt_tokens"] += e.prompt_tokens
        bn["completion_tokens"] += e.completion_tokens
        bn["total_tokens"] += e.total_tokens
        bn["calls"] += 1
        bm = by_model.setdefault(
            (e.network, e.model),
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
        )
        bm["prompt_tokens"] += e.prompt_tokens
        bm["completion_tokens"] += e.completion_tokens
        bm["total_tokens"] += e.total_tokens
        bm["calls"] += 1

    by_model_rows = []
    net_est: dict[str, float] = {}
    est_total = 0.0
    has_any_price = False
    for (network, model), s in sorted(by_model.items()):
        est = estimate_usd(network, model, s["prompt_tokens"], s["completion_tokens"])
        row = {
            "network": network,
            "model": model,
            "prompt_tokens": s["prompt_tokens"],
            "completion_tokens": s["completion_tokens"],
            "total_tokens": s["total_tokens"],
            "calls": s["calls"],
            "est_usd": est,
        }
        by_model_rows.append(row)
        if est is not None:
            has_any_price = True
            est_total += est
            net_est[network] = net_est.get(network, 0.0) + est

    by_network_rows = []
    for network, s in sorted(by_net.items()):
        by_network_rows.append(
            {
                "network": network,
                "prompt_tokens": s["prompt_tokens"],
                "completion_tokens": s["completion_tokens"],
                "total_tokens": s["total_tokens"],
                "calls": s["calls"],
                "est_usd": net_est.get(network) if has_any_price and network in net_est else None,
            }
        )

    return {
        "project_id": str(project_id),
        "totals": {
            "prompt_tokens": tot_p,
            "completion_tokens": tot_c,
            "total_tokens": tot_t,
            "calls": len(events),
        },
        "by_network": by_network_rows,
        "by_model": by_model_rows,
        "est_usd_total": round(est_total, 6) if has_any_price else None,
    }
