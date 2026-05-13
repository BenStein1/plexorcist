from functools import lru_cache
from typing import Literal

from pydantic import Field

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _PYDANTIC_SETTINGS_V2 = True
except ImportError:
    from pydantic import BaseSettings

    SettingsConfigDict = None
    _PYDANTIC_SETTINGS_V2 = False


def env_field(*, default: object, env: str):
    if _PYDANTIC_SETTINGS_V2:
        return Field(default=default, alias=env)
    return Field(default=default, env=env)


class Settings(BaseSettings):
    if _PYDANTIC_SETTINGS_V2:
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
        )

    app_name: str = "Plexorcist Concierge"
    environment: Literal["dev", "prod", "test"] = "dev"
    auth_mode: Literal["dev", "dev_impersonate", "plex_oauth", "ombi_session", "header_passthrough"] = "dev_impersonate"
    base_url: str = env_field(default="http://localhost:8000", env="PLEXORCIST_BASE_URL")
    session_secret_key: str = env_field(default="plexorcist-dev-session-secret", env="SESSION_SECRET_KEY")
    plex_client_identifier: str | None = env_field(default=None, env="PLEX_CLIENT_IDENTIFIER")
    plex_client_identifier_store: str = env_field(default="./plex_client_identifier.txt", env="PLEX_CLIENT_IDENTIFIER_STORE")
    plex_auth_product_name: str = env_field(default="Plexorcist Concierge", env="PLEX_AUTH_PRODUCT_NAME")
    ombi_continue_url: str = env_field(default="http://localhost:5000", env="OMBI_CONTINUE_URL")
    openai_model: str = env_field(default="gpt-5-mini", env="OPENAI_MODEL")
    log_level: str = "INFO"
    database_url: str = env_field(default="sqlite:///./plexorcist.db", env="DATABASE_URL")
    dev_user_id: str = env_field(default="dev-user-1", env="DEV_USER_ID")
    dev_username: str = env_field(default="dev-user", env="DEV_USERNAME")
    dev_display_name: str = env_field(default="Dev User", env="DEV_DISPLAY_NAME")
    dev_user_is_admin: bool = env_field(default=True, env="DEV_USER_IS_ADMIN")
    admin_user_id: str | None = env_field(default=None, env="ADMIN_USER_ID")
    admin_display_name: str | None = env_field(default=None, env="ADMIN_DISPLAY_NAME")
    dev_impersonate_user_id: str | None = env_field(default=None, env="DEV_IMPERSONATE_USER_ID")
    dev_impersonate_username: str | None = env_field(default=None, env="DEV_IMPERSONATE_USERNAME")
    dev_impersonate_display_name: str | None = env_field(default=None, env="DEV_IMPERSONATE_DISPLAY_NAME")
    dev_impersonate_is_admin: bool | None = env_field(default=None, env="DEV_IMPERSONATE_IS_ADMIN")
    dev_impersonation_store: str = env_field(default="./dev_impersonation.json", env="DEV_IMPERSONATION_STORE")
    friendly_names_path: str = env_field(default="./friendlynames.json", env="FRIENDLY_NAMES_PATH")

    ombi_base_url: str = env_field(default="http://localhost:5000", env="OMBI_BASE_URL")
    ombi_api_key: str | None = env_field(default=None, env="OMBI_API_KEY")
    sickchill_base_url: str = env_field(default="http://localhost:8081", env="SICKCHILL_BASE_URL")
    sickchill_api_key: str | None = env_field(default=None, env="SICKCHILL_API_KEY")
    sickchill_tv_root: str | None = env_field(default=None, env="SICKCHILL_TV_ROOT")
    radarr_base_url: str = env_field(default="http://localhost:7878", env="RADARR_BASE_URL")
    radarr_api_key: str | None = env_field(default=None, env="RADARR_API_KEY")
    tautulli_base_url: str = env_field(default="http://localhost:8181", env="TAUTULLI_BASE_URL")
    tautulli_api_key: str | None = env_field(default=None, env="TAUTULLI_API_KEY")
    plex_base_url: str = env_field(default="http://localhost:32400", env="PLEX_BASE_URL")
    plex_token: str | None = env_field(default=None, env="PLEX_TOKEN")
    jackett_base_url: str = env_field(default="http://localhost:9117", env="JACKETT_BASE_URL")
    jackett_api_key: str | None = env_field(default=None, env="JACKETT_API_KEY")
    transmission_host: str = env_field(default="http://localhost:9091", env="TRANSMISSION_HOST")
    transmission_user: str | None = env_field(default=None, env="TRANSMISSION_USER")
    transmission_password: str | None = env_field(default=None, env="TRANSMISSION_PASSWORD")
    prowl_api_key: str | None = env_field(default=None, env="PROWL_API_KEY")
    openai_api_key: str | None = env_field(default=None, env="OPENAI_API_KEY")

    long_show_episode_threshold: int = 50
    huge_show_episode_threshold: int = 100
    default_tv_scope: str = "first_season"
    include_specials_by_default: bool = False
    ask_before_full_series: bool = True
    normal_search_hour: int = 3
    wait_hours_after_airdate_before_manual_search: int = 12
    wait_hours_after_manual_search_before_tier3: int = 24
    max_manual_searches_per_episode: int = 1
    tier3_enabled: bool = True
    tier3_user_visible: bool = False
    tier3_require_repeat_complaint: bool = True
    tier3_min_confidence_auto_add: float = 0.90
    tier3_min_confidence_notify_candidates: float = 0.65
    tier3_max_query_variants: int = 8
    tier3_sort_seeders_after_confidence: bool = True
    tier3_max_auto_adds_per_user_per_day: int = 1
    movie_direct_source_enabled: bool = env_field(default=False, env="MOVIE_DIRECT_SOURCE_ENABLED")
    prowl_enabled: bool = True
    notify_on_tier3_success: bool = True
    notify_on_tier3_candidates: bool = True
    notify_on_failed_manual_search: bool = True
    memory_inactivity_minutes: int = env_field(default=2, env="MEMORY_INACTIVITY_MINUTES")
    memory_use_openai_summarizer: bool = env_field(default=True, env="MEMORY_USE_OPENAI_SUMMARIZER")
    memory_compaction_timeout_seconds: int = env_field(default=180, env="MEMORY_COMPACTION_TIMEOUT_SECONDS")
    memory_recent_notes_limit: int = env_field(default=12, env="MEMORY_RECENT_NOTES_LIMIT")
    memory_tier1_keep: int = env_field(default=40, env="MEMORY_TIER1_KEEP")
    memory_tier2_to_tier3_threshold: int = env_field(default=8, env="MEMORY_TIER2_TO_TIER3_THRESHOLD")

    def is_dev_impersonation_mode(self) -> bool:
        return self.auth_mode in {"dev", "dev_impersonate"}

    def is_plex_oauth_mode(self) -> bool:
        return self.auth_mode == "plex_oauth"

    def is_admin_identity(self, user_id: str | None) -> bool:
        expected_user_id = (self.admin_user_id or "").strip()
        actual_user_id = (user_id or "").strip()
        return bool(expected_user_id and actual_user_id == expected_user_id)

    if not _PYDANTIC_SETTINGS_V2:
        class Config:
            env_file = ".env"
            env_file_encoding = "utf-8"
            extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
