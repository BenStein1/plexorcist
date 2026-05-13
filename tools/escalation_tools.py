from __future__ import annotations

from clients.jackett_client import JackettClient
from clients.prowl_client import ProwlClient
from clients.transmission_client import TransmissionClient


class EscalationTools:
    def __init__(
        self,
        jackett: JackettClient,
        transmission: TransmissionClient,
        prowl: ProwlClient,
        user_label: str | None = None,
    ) -> None:
        self.jackett = jackett
        self.transmission = transmission
        self.prowl = prowl
        self.user_label = (user_label or "").strip()

    async def broad_jackett_episode_search(self, query_variants: list[str]) -> dict:
        return await self.jackett.broad_search(query_variants=query_variants)

    async def broad_jackett_movie_search(self, query_variants: list[str]) -> dict:
        return await self.jackett.broad_movie_search(query_variants=query_variants)

    async def add_transmission_candidate(self, magnet_or_url: str, label: str) -> dict:
        return await self.transmission.add_torrent(magnet_or_url=magnet_or_url, label=label)

    async def send_admin_prowl_notice(self, summary: str, priority: int = 0, event: str = "Concierge Alert") -> dict:
        formatted_summary = self._format_admin_summary(summary)
        try:
            return await self.prowl.send_notice(summary=formatted_summary, priority=priority, event=event)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "summary": formatted_summary,
                "priority": priority,
                "event": event,
                "error": str(exc),
            }

    def _format_admin_summary(self, summary: str) -> str:
        summary = summary.strip()
        if not self.user_label:
            return summary
        if summary.startswith(f"{self.user_label}:"):
            return summary
        display_name, username = self._split_user_label(self.user_label)
        for prefix in (
            f"{self.user_label}:",
            f"{self.user_label} -",
            f"{self.user_label} —",
            f"{display_name}:",
            f"{display_name} -",
            f"{display_name} —",
            f"{username}:",
            f"{username} -",
            f"{username} —",
            f"{display_name} ({username}):",
        ):
            if summary.startswith(prefix):
                summary = summary.removeprefix(prefix).strip()
                break
        return f"{self.user_label}: {summary}"

    def _split_user_label(self, label: str) -> tuple[str, str]:
        if "(" in label and label.endswith(")"):
            display_name, username = label.rsplit("(", 1)
            return display_name.strip(), username[:-1].strip()
        return label, ""
