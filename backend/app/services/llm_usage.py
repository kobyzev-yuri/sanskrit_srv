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


def parse_anthropic_usage(data: dict) -> dict[str, Any]:
    usage = data.get("usage") or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "usage_raw": usage if isinstance(usage, dict) else {"raw": usage},
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
    user_id: uuid.UUID | None = None,
    key_source: str | None = None,
    key_hint: str | None = None,
) -> LlmUsageEvent:
    from app.services.llm_route import current_creds

    creds = current_creds()
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
        user_id=user_id if user_id is not None else creds.user_id,
        key_source=key_source or creds.key_source or "default",
        key_hint=key_hint if key_hint is not None else creds.key_hint,
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


def _user_bucket_key(event: Any) -> tuple:
    uid = getattr(event, "user_id", None)
    return (
        str(uid) if uid else "",
        str(getattr(event, "key_source", None) or "default"),
        str(getattr(event, "key_hint", None) or ""),
    )


def _accumulate_user_row(acc: dict[tuple, dict[str, Any]], event: Any) -> None:
    key = _user_bucket_key(event)
    row = acc.setdefault(
        key,
        {
            "user_id": str(event.user_id) if getattr(event, "user_id", None) else None,
            "key_source": str(getattr(event, "key_source", None) or "default"),
            "key_hint": getattr(event, "key_hint", None) or None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
            "by_network": {},
        },
    )
    row["prompt_tokens"] += int(getattr(event, "prompt_tokens", 0) or 0)
    row["completion_tokens"] += int(getattr(event, "completion_tokens", 0) or 0)
    row["total_tokens"] += int(getattr(event, "total_tokens", 0) or 0)
    row["calls"] += 1
    net_id = getattr(event, "network", None) or "?"
    net = row["by_network"].setdefault(
        net_id,
        {
            "network": net_id,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        },
    )
    net["prompt_tokens"] += int(getattr(event, "prompt_tokens", 0) or 0)
    net["completion_tokens"] += int(getattr(event, "completion_tokens", 0) or 0)
    net["total_tokens"] += int(getattr(event, "total_tokens", 0) or 0)
    net["calls"] += 1


def _user_rows(db: Session, acc: dict[tuple, dict[str, Any]]) -> list[dict[str, Any]]:
    from app.models import User

    ids: list[uuid.UUID] = []
    for row in acc.values():
        if not row.get("user_id"):
            continue
        try:
            ids.append(uuid.UUID(str(row["user_id"])))
        except ValueError:
            continue
    users: dict[str, Any] = {}
    if ids:
        found = list(db.scalars(select(User).where(User.id.in_(ids))).all())
        for u in found:
            uid = getattr(u, "id", None)
            if uid is None:
                continue
            users[str(uid)] = u
    out = []
    for key in sorted(acc, key=lambda k: (k[0], k[1], k[2])):
        row = acc[key]
        user = users.get(row["user_id"] or "")
        nets = [row["by_network"][k] for k in sorted(row["by_network"])]
        out.append(
            {
                "user_id": row["user_id"],
                "login": getattr(user, "login", None) if user else None,
                "email": getattr(user, "email", None) if user else None,
                "display_name": (
                    user.display_name
                    if user
                    else ("бэкофис" if row["key_source"] == "default" else "неизвестный ключ")
                ),
                "key_source": row["key_source"],
                "key_hint": row["key_hint"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "total_tokens": row["total_tokens"],
                "calls": row["calls"],
                "by_network": nets,
            }
        )
    return out


def user_usage_summary(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    events = list(
        db.scalars(
            select(LlmUsageEvent).where(
                LlmUsageEvent.ok.is_(True),
                LlmUsageEvent.user_id == user_id,
            )
        ).all()
    )
    tot = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    by_user_acc: dict[tuple, dict[str, Any]] = {}
    for e in events:
        tot["prompt_tokens"] += int(e.prompt_tokens or 0)
        tot["completion_tokens"] += int(e.completion_tokens or 0)
        tot["total_tokens"] += int(e.total_tokens or 0)
        tot["calls"] += 1
        _accumulate_user_row(by_user_acc, e)
    return {
        "totals": tot,
        "by_user": _user_rows(db, by_user_acc),
    }


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
    by_user_acc: dict[tuple, dict[str, Any]] = {}

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
        _accumulate_user_row(by_user_acc, e)

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

    from app.services.llm_route import describe_route

    desc = describe_route()
    primary = desc.get("primary") or {}

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
        "route": desc.get("route"),
        "route_label": desc.get("label"),
        "route_model": (
            f"{primary.get('provider')}:{primary.get('model')}"
            if primary
            else None
        ),
        "by_user": _user_rows(db, by_user_acc),
    }


def all_projects_usage_summary(db: Session) -> dict[str, Any]:
    """Admin billing table: prompt (in) / completion (out) per project and network."""
    from app.models import Project
    from app.services.translation_style import project_task

    projects = list(db.scalars(select(Project).order_by(Project.created_at)).all())
    events = list(
        db.scalars(select(LlmUsageEvent).where(LlmUsageEvent.ok.is_(True))).all()
    )
    by_proj: dict[uuid.UUID, dict[str, Any]] = {}
    for p in projects:
        by_proj[p.id] = {
            "project_id": str(p.id),
            "slug": p.slug,
            "title": p.title,
            "task": project_task(p),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
            "by_network": {},
        }
    by_user_acc: dict[tuple, dict[str, Any]] = {}
    for e in events:
        row = by_proj.get(e.project_id)
        if row is None:
            continue
        row["prompt_tokens"] += int(e.prompt_tokens or 0)
        row["completion_tokens"] += int(e.completion_tokens or 0)
        row["total_tokens"] += int(e.total_tokens or 0)
        row["calls"] += 1
        net = row["by_network"].setdefault(
            e.network or "?",
            {
                "network": e.network or "?",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
            },
        )
        net["prompt_tokens"] += int(e.prompt_tokens or 0)
        net["completion_tokens"] += int(e.completion_tokens or 0)
        net["total_tokens"] += int(e.total_tokens or 0)
        net["calls"] += 1
        _accumulate_user_row(by_user_acc, e)

    projects_out = []
    tot = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    by_net_tot: dict[str, dict[str, Any]] = {}
    for p in projects:
        row = by_proj[p.id]
        nets = [row["by_network"][k] for k in sorted(row["by_network"])]
        projects_out.append({**{k: row[k] for k in row if k != "by_network"}, "by_network": nets})
        tot["prompt_tokens"] += row["prompt_tokens"]
        tot["completion_tokens"] += row["completion_tokens"]
        tot["total_tokens"] += row["total_tokens"]
        tot["calls"] += row["calls"]
        for n in nets:
            acc = by_net_tot.setdefault(
                n["network"],
                {
                    "network": n["network"],
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "calls": 0,
                },
            )
            acc["prompt_tokens"] += n["prompt_tokens"]
            acc["completion_tokens"] += n["completion_tokens"]
            acc["total_tokens"] += n["total_tokens"]
            acc["calls"] += n["calls"]

    return {
        "projects": projects_out,
        "totals": tot,
        "by_network": [by_net_tot[k] for k in sorted(by_net_tot)],
        "by_user": _user_rows(db, by_user_acc),
    }
