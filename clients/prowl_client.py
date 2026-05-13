from __future__ import annotations

import httpx


class ProwlClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    async def send_notice(self, summary: str, priority: int = 0, event: str = "Concierge Alert") -> dict:
        if not self.api_key:
            return {
                "ok": False,
                "error": "missing_api_key",
                "summary": summary,
                "priority": priority,
                "event": event,
            }

        payload = {
            "apikey": self.api_key,
            "application": "Plexorcist",
            "event": event,
            "description": summary,
            "priority": str(priority),
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post("https://api.prowlapp.com/publicapi/add", data=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                return {
                    "ok": False,
                    "summary": summary,
                    "priority": priority,
                    "event": event,
                    "status_code": response.status_code,
                    "error": str(exc),
                    "response_text": response.text,
                }
            return {
                "ok": True,
                "summary": summary,
                "priority": priority,
                "event": event,
                "status_code": response.status_code,
            }
