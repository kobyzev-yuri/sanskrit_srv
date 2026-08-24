"""LLM / ProxyAPI balance & quota warnings."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

ALERT_FILE = Path("/opt/sanskrit_srv/data/llm_alert.json")
# also allow local/dev
def _alert_path() -> Path:
    settings = get_settings()
    root = Path(settings.storage_root).parent / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "llm_alert.json"


class LlmQuotaError(RuntimeError):
    """ProxyAPI 402 / insufficient balance."""

    code = "llm_quota"


def is_quota_response(status_code: int, body: str) -> bool:
    if status_code == 402:
        return True
    low = (body or "").lower()
    markers = (
        "insufficient balance",
        "insufficient_funds",
        "not enough",
        "недостаточно",
        "не хватает",
        "balance to run",
        "payment required",
    )
    return any(m in low for m in markers)


def set_quota_alert(message: str, *, balance: float | None = None) -> None:
    payload = {
        "active": True,
        "code": "llm_quota",
        "message": message,
        "balance": balance,
        "updated_at": time.time(),
    }
    path = _alert_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_quota_alert() -> None:
    path = _alert_path()
    if path.exists():
        path.write_text(
            json.dumps(
                {"active": False, "code": None, "message": None, "balance": None, "updated_at": time.time()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def read_alert() -> dict[str, Any]:
    path = _alert_path()
    if not path.exists():
        return {"active": False}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"active": False}


def fetch_balance() -> dict[str, Any]:
    """GET ProxyAPI balance. May 403 if key has no balance permission."""
    settings = get_settings()
    if not settings.openai_api_key:
        return {"ok": False, "error": "OPENAI_API_KEY missing", "balance": None}
    try:
        resp = httpx.get(
            "https://api.proxyapi.ru/proxyapi/balance",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "balance": None}
    if resp.status_code != 200:
        return {
            "ok": False,
            "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            "balance": None,
            "status_code": resp.status_code,
        }
    data = resp.json()
    balance = data.get("balance")
    # Warn if balance is very low for vision calls (heuristic).
    low = isinstance(balance, (int, float)) and balance < 15
    if low:
        set_quota_alert(
            f"Мало средств на ProxyAPI: баланс {balance} ₽. Пополните счёт, иначе LLM-запросы вернут 402.",
            balance=float(balance),
        )
    elif isinstance(balance, (int, float)) and balance >= 15:
        # clear only if previous alert was balance-low (not hard 402 mid-job)
        alert = read_alert()
        if alert.get("active") and alert.get("balance") is not None:
            clear_quota_alert()
    return {"ok": True, "balance": balance, "raw": data, "low": low}


def llm_status() -> dict[str, Any]:
    from app.services.llm_route import describe_route, get_route

    desc = describe_route()
    route = get_route()
    primary = desc.get("primary") or {}
    live = f"{desc.get('label') or route} · {primary.get('provider')}:{primary.get('model')}"

    def attach(payload: dict[str, Any]) -> dict[str, Any]:
        payload["route"] = route
        payload["route_label"] = desc.get("label")
        payload["route_model"] = f"{primary.get('provider')}:{primary.get('model')}"
        if not payload.get("message"):
            payload["message"] = live
        elif payload.get("ok") and not payload.get("warning"):
            payload["message"] = live
        return payload

    alert = read_alert()
    if route == "openrouter":
        or_ok = bool((get_settings().openrouter_api_key or "").strip())
        if not or_ok:
            return attach(
                {
                    "ok": False,
                    "warning": True,
                    "code": "llm_key",
                    "message": "OPENROUTER_API_KEY не задан в .env — нужен для Ox Alpha.",
                    "balance": None,
                    "balance_ok": False,
                    "balance_error": "OPENROUTER_API_KEY missing",
                }
            )
        if alert.get("active"):
            return attach(
                {
                    "ok": False,
                    "warning": True,
                    "code": alert.get("code") or "llm_quota",
                    "message": alert.get("message"),
                    "balance": None,
                    "balance_ok": True,
                    "balance_error": None,
                }
            )
        return attach(
            {
                "ok": True,
                "warning": False,
                "code": None,
                "message": live,
                "balance": None,
                "balance_ok": True,
                "balance_error": None,
            }
        )

    bal = fetch_balance()
    active = bool(alert.get("active")) or bool(bal.get("low"))
    message = None
    if alert.get("active"):
        message = alert.get("message")
    elif bal.get("low"):
        message = f"Мало средств на ProxyAPI: баланс {bal.get('balance')} ₽."
    elif not bal.get("ok") and not settings_key_ok():
        message = "Ключ ProxyAPI не задан в .env"
        active = True
    ok_msg = live if bal.get("ok") else (
        "Баланс недоступен (включите «Запрос баланса» у ключа в кабинете ProxyAPI) — квота всё равно отловится по HTTP 402."
    )
    return attach(
        {
            "ok": not active,
            "warning": active,
            "code": "llm_quota" if active else None,
            "message": message or ok_msg,
            "balance": bal.get("balance"),
            "balance_ok": bal.get("ok"),
            "balance_error": None if bal.get("ok") else bal.get("error"),
        }
    )


def settings_key_ok() -> bool:
    s = get_settings()
    from app.services.llm_route import get_route

    if get_route() == "openrouter":
        return bool((s.openrouter_api_key or "").strip())
    return bool(s.openai_api_key)
