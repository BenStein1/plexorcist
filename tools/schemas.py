"""Typed input models for every concierge tool.

These models are the single source of truth for tool argument schemas: the
LLM-facing JSON schema is generated from them, and every incoming tool call is
validated against them before the handler runs. Validation failures are
returned to the model as structured errors it can correct, never swallowed.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyInput(ToolInput):
    """Tool takes no arguments."""


# --- Media lookup -----------------------------------------------------------


class SearchMediaInput(ToolInput):
    query: str = Field(description="Title or natural-language description to search for. Start with the user's plain title; do not append actor names, years, or 'movie' unless a plain search already failed.")


class CheckMovieAvailabilityInput(ToolInput):
    title: str = Field(description="Exact movie title to check in Plex.")


class CheckMoviesAvailabilityBatchInput(ToolInput):
    titles: list[str] = Field(description="Concrete movie titles to verify in Plex in one pass.")


class CheckExistingMediaStatusInput(ToolInput):
    query: str = Field(description="Title to check. Use the user's plain wording.")


class CheckLibraryInventoryInput(ToolInput):
    query: str = Field(description="Broad inventory query: an actor, director, collection, franchise, or theme.")


# --- Requests (Ombi) --------------------------------------------------------


class RequestMovieInput(ToolInput):
    tmdb_id: int | None = Field(default=None, ge=1, description="Positive TMDB id. Authoritative when the user provides one.")
    title: str | None = Field(default=None, description="Exact movie title. Must be paired with year.")
    year: int | None = Field(default=None, description="Release year. Required when requesting by title.")

    @model_validator(mode="after")
    def _require_id_or_title_year(self) -> "RequestMovieInput":
        if self.tmdb_id is None and not (self.title and self.year):
            raise ValueError(
                "Provide either a positive tmdb_id, or BOTH title and year. "
                "If you only have a title, ask the user for the release year "
                "or resolve it with search_media first."
            )
        return self


class ShowScope(str, Enum):
    FIRST_SEASON = "first_season"
    LATEST_SEASON = "latest_season"
    FULL_SERIES = "full_series"


class RequestShowScopeInput(ToolInput):
    tvdb_id: int = Field(ge=1, description="Positive TVDB id from a prior search/status tool result or explicitly given by the user. Never invented.")
    scope: ShowScope = Field(description="How much of the show to request.")


class RequestEpisodeInput(ToolInput):
    tvdb_id: int = Field(ge=1, description="Positive TVDB id from a prior tool result or explicitly given by the user. Never invented.")
    season: int = Field(ge=0, description="Season number.")
    episode: int = Field(ge=1, description="Episode number within the season.")


class QueryInput(ToolInput):
    query: str = Field(description="Show or movie title to look up.")


class ShowSeasonStatusInput(ToolInput):
    query: str = Field(description="Show title to list episode status for.")
    season: int | None = Field(default=None, ge=1, description="Limit the listing to one season.")


# --- Repair -----------------------------------------------------------------


class RepairMovieInput(ToolInput):
    title: str | None = Field(default=None, description="Clean movie title ONLY — never the user's complaint sentence or repair instructions.")
    year: int | None = Field(default=None, description="Release year when known.")
    issue: str | None = Field(default=None, description="The user's complaint or desired action in plain language: wrong language, bad copy, redownload requested, deleted from Plex, re-add to Radarr, etc.")
    query: str | None = Field(default=None, description="Fallback fuzzy lookup text when a clean title is genuinely unavailable.")

    @model_validator(mode="after")
    def _require_identity(self) -> "RepairMovieInput":
        if not (self.title or self.query):
            raise ValueError("Provide the movie identity: title (preferred, with year) or query.")
        return self


class RepairScope(str, Enum):
    SHOW = "show"
    SEASON = "season"
    EPISODE = "episode"


class RepairShowInput(ToolInput):
    query: str = Field(description="Show title. Use the best title from Ombi/Plex/conversation context.")
    scope: RepairScope = Field(description="Use 'show' for vague complaints ('broken', 'missing episodes', 'not downloading'). Use 'season'/'episode' only when the user explicitly scoped their complaint.")
    season: int | None = Field(default=None, ge=1, description="Required when scope is 'season' or 'episode'.")
    episode: int | None = Field(default=None, ge=1, description="Required when scope is 'episode'.")
    tvdb_id: int | None = Field(default=None, ge=1, description="Pass ONLY when the user provided it or an earlier tool result returned it. Never invent or reuse from memory of other shows.")

    @model_validator(mode="after")
    def _scope_targets(self) -> "RepairShowInput":
        if self.scope is RepairScope.SEASON and self.season is None:
            raise ValueError("scope='season' requires the season number. Ask the user or use scope='show'.")
        if self.scope is RepairScope.EPISODE and (self.season is None or self.episode is None):
            raise ValueError("scope='episode' requires both season and episode numbers. Ask the user or use scope='show'.")
        return self


class AddShowToSickchillInput(ToolInput):
    query: str = Field(description="Show title as requested in Ombi.")
    tvdb_id: int | None = Field(default=None, ge=1, description="Confirmed TVDB id, when duplicate/ambiguous titles were resolved.")
    season: int | None = Field(default=None, ge=1, description="Pass ONLY when the user explicitly asked to repair a single season; omit to add the full show.")


class EpisodeRefInput(ToolInput):
    show: str = Field(description="Show title.")
    season: int = Field(ge=0, description="Season number.")
    episode: int = Field(ge=1, description="Episode number.")


class EpisodeStatusInput(EpisodeRefInput):
    tvdb_id: int | None = Field(default=None, ge=1, description="TVDB id when a prior tool result or the user provided one.")


class ClearIgnoredInput(ToolInput):
    show: str = Field(description="Show title.")
    season: int | None = Field(default=None, ge=1, description="Limit to one season.")


# --- Admin ------------------------------------------------------------------


class AdminSummaryScope(str, Enum):
    ALL_USERS = "all_users"
    SPECIFIC_USER = "specific_user"


class AdminTaskSummaryInput(ToolInput):
    scope: AdminSummaryScope = Field(description="'all_users' for broad questions like 'any open tasks?' or 'anything need attention?'. 'specific_user' ONLY when the admin names a user, friendly name, username, or user id.")
    user_query: str | None = Field(default=None, description="The named user/friendly name/username/user id, when scope is 'specific_user'.")
    days: int | None = Field(default=None, ge=1, le=365, description="Lookback window in days (default 30).")
    limit: int | None = Field(default=None, ge=1, le=100, description="Max tasks to return (default 20).")

    @model_validator(mode="after")
    def _user_query_when_specific(self) -> "AdminTaskSummaryInput":
        if self.scope is AdminSummaryScope.SPECIFIC_USER and not self.user_query:
            raise ValueError("scope='specific_user' requires user_query naming the user.")
        return self


class SendAdminMessageInput(ToolInput):
    message: str = Field(description="The admin's note in plain language, preserving their intent.")
    user_query: str | None = Field(default=None, description="Recipient when the admin names a user directly.")
    task_query: str | None = Field(default=None, description="Title/issue text when the admin says 'whoever requested X' — the backend resolves the affected user from open tasks. Leave user_query empty in that case. Do not guess the recipient from prior chat prose.")


class SetMotdInput(ToolInput):
    message: str = Field(description="The system-wide notice text.")


# --- Escalation -------------------------------------------------------------


class JackettSearchInput(ToolInput):
    query_variants: list[str] = Field(min_length=1, description="Search query variants to try, best guess first. Do not cap at 1080p; quality is metadata only.")


class ProwlNoticeInput(ToolInput):
    summary: str = Field(description="Short, factual operational notice. Good: 'Steve asked about Oak Island S12E14. Episode wanted, not in Plex. Manual search triggered, no result yet.' Bad: 'Steve says Oak Island is broken.'")
    priority: int | None = Field(default=None, ge=-2, le=2, description="Prowl priority, -2 (low) to 2 (emergency).")


class TransmissionCandidateInput(ToolInput):
    magnet_or_url: str = Field(description="Vetted magnet link or torrent URL.")
    label: str = Field(description="Downloader label for correct post-processing.")
