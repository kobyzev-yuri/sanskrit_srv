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


class LlmRateLimitError(RuntimeError):
    """Upstream 429 after retries (OpenRouter pool or Gemini AI Studio RPM)."""

    code = "llm_rate_limit"


GEMINI_RATE_LIMIT_MSG = (
    "Google временно не принимает запрос к Gemini (перегрузка, не ваш лимит). "
    "Подождите около минуты и повторите."
)
GEMINI_CREDITS_MSG = (
    "На проекте Google AI Studio закончились предоплаченные кредиты. "
    "Пополните баланс: https://aistudio.google.com (Projects → billing)."
)


def format_wait_ru(seconds: int) -> str:
    sec = max(1, int(seconds))
    if sec < 90:
        return f"{sec} сек"
    minutes = max(1, round(sec / 60))
    if minutes < 90:
        return f"{minutes} мин"
    hours = max(1, round(minutes / 60))
    return f"{hours} ч"


def seconds_until_pacific_midnight() -> int:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((nxt - now).total_seconds()))


def is_gemini_daily_quota(body: str, retry_after_s: int | None = None) -> bool:
    """True for free-tier RPD (not prepaid credits, not RPM)."""
    if is_quota_response(429, body or ""):
        return False
    low = (body or "").lower()
    daily = any(
        m in low
        for m in (
            "perday",
            "per_day",
            "requestsperday",
            "generatecontentrequestperday",
            "requests per day",
        )
    )
    per_min = any(
        m in low
        for m in (
            "perminute",
            "per_minute",
            "requestsperminute",
            "generatecontentrequestperminute",
            "requests per minute",
        )
    )
    wait = int(retry_after_s) if retry_after_s and retry_after_s > 0 else None
    long_wait = wait is not None and wait > 180
    return bool(daily or (long_wait and not per_min))


def gemini_quota_wait_message(
    body: str,
    retry_after_s: int | None = None,
    *,
    keys_tried: int | None = None,
) -> str:
    """Human wait hint for Studio 429 (Free RPM/TPM/RPD, not prepaid credits)."""
    low = (body or "").lower()
    wait = int(retry_after_s) if retry_after_s and retry_after_s > 0 else None
    tokens = any(
        m in low
        for m in (
            "tokensperminute",
            "tokens per minute",
            "inputtokencount",
        )
    )
    n_keys = int(keys_tried) if keys_tried and keys_tried > 1 else 0
    if is_gemini_daily_quota(body, retry_after_s):
        wait_s = wait or seconds_until_pacific_midnight()
        if n_keys:
            return (
                f"Суточный лимит бесплатного Gemini на всех {n_keys} ключах AI Studio "
                f"(по 20 запросов). Подождите {format_wait_ru(wait_s)} "
                "до полуночи по Калифорнии."
            )
        return (
            "Суточный лимит бесплатного Gemini (20 запросов на ключ). "
            f"Подождите {format_wait_ru(wait_s)} и повторите "
            "(сброс в полночь по Калифорнии)."
        )
    if tokens:
        wait_s = wait or 60
        return (
            "Лимит токенов в минуту на бесплатном Flash. "
            f"Подождите {format_wait_ru(wait_s)} и повторите."
        )
    wait_s = wait or 60
    if "exceeded your current quota" in low:
        return (
            "Лимит бесплатного Gemini Flash. "
            f"Подождите {format_wait_ru(wait_s)} (5 запросов в минуту). "
            "Если за сегодня уже много страниц — суточный лимит 20 запросов, "
            f"ждите {format_wait_ru(seconds_until_pacific_midnight())} "
            "до полуночи по Калифорнии."
        )
    return (
        "Лимит бесплатного Gemini: 5 запросов в минуту. "
        f"Подождите {format_wait_ru(wait_s)} и повторите."
    )


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
        "prepayment credits",
        "credits are depleted",
        "insufficient credits",
        "daily request limit",
        "250 requests per day",
    )
    return any(m in low for m in markers)


def set_quota_alert(
    message: str,
    *,
    balance: float | None = None,
    route: str | None = None,
) -> None:
    payload = {
        "active": True,
        "code": "llm_quota",
        "message": message,
        "balance": balance,
        "route": route,
        "updated_at": time.time(),
    }
    path = _alert_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_quota_alert() -> None:
    path = _alert_path()
    if path.exists():
        path.write_text(
            json.dumps(
                {
                    "active": False,
                    "code": None,
                    "message": None,
                    "balance": None,
                    "route": None,
                    "updated_at": time.time(),
                },
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


def alert_matches_route(alert: dict[str, Any], route: str) -> bool:
    """Paywall banner is per gateway. Timeweb 402 must not look like a Studio failure."""
    if not alert.get("active"):
        return False
    stored = str(alert.get("route") or "").strip()
    if stored:
        return stored == route
    msg = str(alert.get("message") or "").lower()
    if any(m in msg for m in ("gemini", "ai studio", "калифорни", "studio")):
        return route == "gemini"
    # Legacy alerts had no route field and were Timeweb / OpenRouter / ProxyAPI.
    return route not in ("gemini",)


def fetch_balance() -> dict[str, Any]:
    """GET ProxyAPI balance. May 403 if key has no balance permission."""
    from app.services.llm_route import effective_proxyapi_key

    key = effective_proxyapi_key()
    if not key:
        return {"ok": False, "error": "OPENAI_API_KEY missing", "balance": None}
    try:
        resp = httpx.get(
            "https://api.proxyapi.ru/proxyapi/balance",
            headers={"Authorization": f"Bearer {key}"},
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
    from app.services.llm_route import current_creds, describe_route, get_route

    creds = current_creds()
    desc = describe_route(effective=True)
    route = get_route()
    primary = desc.get("primary") or {}
    live = f"{desc.get('label') or route} · {primary.get('provider')}:{primary.get('model')}"
    if creds.key_source == "personal":
        live = f"свой ключ · {live}"
    else:
        live = f"бэкофис · {live}"
    if route == "gemini" and creds.key_source != "personal":
        from app.services.gemini_keys import pool_summary

        summ = pool_summary()
        if summ["n"] > 1:
            live = f"{live} · ключи {summ['available']}/{summ['n']}"

    def attach(payload: dict[str, Any]) -> dict[str, Any]:
        payload["route"] = route
        payload["route_label"] = desc.get("label")
        payload["route_model"] = f"{primary.get('provider')}:{primary.get('model')}"
        payload["key_source"] = creds.key_source
        payload["use_default"] = creds.use_default
        payload["gemini_keys"] = desc.get("gemini_keys") or 0
        payload["gemini_keys_available"] = desc.get("gemini_keys_available") or 0
        if not payload.get("message"):
            payload["message"] = live
        elif payload.get("ok") and not payload.get("warning"):
            payload["message"] = live
        return payload

    alert = read_alert()
    if route == "openrouter" and not alert_matches_route(alert, route):
        alert = {"active": False}
    if route == "openrouter":
        or_ok = bool(creds.openrouter_api_key)
        if not or_ok:
            missing = (
                "В кабинете не задан ключ Timeweb AI Gateway."
                if creds.key_source == "personal"
                else "TIMEWEB_API_KEY (или OPENROUTER_API_KEY) не задан в .env."
            )
            return attach(
                {
                    "ok": False,
                    "warning": True,
                    "code": "llm_key",
                    "message": missing,
                    "balance": None,
                    "balance_ok": False,
                    "balance_error": "TIMEWEB_API_KEY missing",
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

    if route == "gemini" and creds.gemini_api_key:
        if alert_matches_route(alert, route):
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
        message = (
            "В кабинете не задан ключ ProxyAPI."
            if creds.key_source == "personal"
            else "Ключ ProxyAPI не задан в .env"
        )
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
    from app.services.llm_route import current_creds, get_route

    creds = current_creds()
    if get_route() == "openrouter":
        return bool(creds.openrouter_api_key)
    if get_route() == "gemini":
        return bool(creds.gemini_api_key or creds.openai_api_key)
    return bool(creds.openai_api_key)
