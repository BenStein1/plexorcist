from __future__ import annotations

from http import HTTPStatus
import re
from typing import Any

import httpx


def classify_http_error(*, service: str, operation: str, exc: httpx.HTTPError) -> dict[str, Any]:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        reason = exc.response.reason_phrase or _http_reason(status)
        return {
            "service": service,
            "operation": operation,
            "failure_type": "http_error",
            "http_status": status,
            "http_reason": reason,
            "error_message": f"HTTP {status} {reason}",
        }
    if isinstance(exc, httpx.TimeoutException):
        return {
            "service": service,
            "operation": operation,
            "failure_type": "timeout",
            "error_message": "request timed out",
        }
    return {
        "service": service,
        "operation": operation,
        "failure_type": "connection_error",
        "error_message": str(exc),
    }


def classify_service_result(
    *,
    service: str,
    operation: str,
    reason: object = None,
    backend_connected: object = None,
) -> dict[str, Any]:
    text = str(reason or "").strip()
    parsed = _parse_http_error_text(text)
    if parsed is not None:
        status, phrase = parsed
        return {
            "service": service,
            "operation": operation,
            "failure_type": "http_error",
            "http_status": status,
            "http_reason": phrase,
            "error_message": f"HTTP {status} {phrase}",
        }
    if backend_connected is False:
        failure_type = "connection_error"
        if "timed out" in text.lower() or "timeout" in text.lower():
            failure_type = "timeout"
        return {
            "service": service,
            "operation": operation,
            "failure_type": failure_type,
            "error_message": text or "connection failed",
        }
    return {
        "service": service,
        "operation": operation,
        "failure_type": "error",
        "error_message": text or "unknown error",
    }


def service_action(error: dict[str, Any]) -> str:
    service = str(error.get("service") or "service").lower()
    failure_type = str(error.get("failure_type") or "error")
    if failure_type == "http_error":
        return f"{service}_endpoint_error"
    if failure_type in {"timeout", "connection_error"}:
        return f"{service}_unreachable"
    return f"{service}_{failure_type}"


def user_error_summary(
    *,
    tool_family: str,
    error: dict[str, Any],
    title: str,
    change_status: str = "Nothing was changed.",
) -> str:
    service = _service_label(str(error.get("service") or "service"))
    operation = str(error.get("operation") or "operation").replace("_", " ")
    failure_type = str(error.get("failure_type") or "error")
    if failure_type == "http_error":
        status = error.get("http_status")
        reason = error.get("http_reason") or _http_reason(status)
        problem = f"HTTP {status} {reason}".strip()
    elif failure_type == "timeout":
        problem = "timeout"
    else:
        problem = str(error.get("error_message") or "connection error")
    return f"{tool_family} for {title} failed while talking to {service} during {operation}: {problem}. {change_status}"


def _http_reason(status: object) -> str:
    try:
        return HTTPStatus(int(status)).phrase
    except Exception:
        return ""


def _parse_http_error_text(text: str) -> tuple[int, str] | None:
    match = re.search(r"(\d{3})\s+([A-Za-z][A-Za-z ]+?)(?:'| for url|$)", text)
    if not match:
        return None
    try:
        status = int(match.group(1))
    except ValueError:
        return None
    phrase = match.group(2).strip() or _http_reason(status)
    return status, phrase


def _service_label(service: str) -> str:
    labels = {
        "ombi": "Ombi",
        "sickchill": "SickChill",
        "radarr": "Radarr",
        "plex": "Plex",
        "tautulli": "Tautulli",
        "prowl": "Prowl",
    }
    return labels.get(service.lower(), service)
