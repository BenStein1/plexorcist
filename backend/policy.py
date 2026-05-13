from __future__ import annotations

from dataclasses import dataclass

from backend.config import Settings
from backend.models import MediaType


@dataclass
class ScopeDecision:
    should_ask: bool
    suggested_scope: str
    explanation: str


class PolicyEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def tv_scope_decision(self, seasons: int, episodes: int, is_ongoing: bool) -> ScopeDecision:
        if episodes >= self.settings.huge_show_episode_threshold:
            return ScopeDecision(
                should_ask=True,
                suggested_scope="specific_or_season",
                explanation="This is a large show, so clarify before requesting the full series.",
            )
        if episodes >= self.settings.long_show_episode_threshold or is_ongoing:
            return ScopeDecision(
                should_ask=True,
                suggested_scope=self.settings.default_tv_scope,
                explanation="Use a conservative default for long or ongoing shows.",
            )
        return ScopeDecision(
            should_ask=False,
            suggested_scope="full_series",
            explanation="Short series can be safely requested as a whole.",
        )

    def can_auto_request(self, media_type: MediaType) -> bool:
        return media_type == MediaType.MOVIE
