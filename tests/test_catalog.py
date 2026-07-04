"""Catalog integrity: every legacy tool is covered, schemas validate, gating works."""

import pytest
from pydantic import ValidationError

from backend.config import Settings
from backend.models import UserContext
from tools import schemas
from tools.catalog import (
    ADMIN,
    CATALOG,
    DIRECT_SOURCE_GATED,
    get_spec,
    visible_specs,
)

LEGACY_TOOL_NAMES = {
    # registered in backend/main.py build_agent() before the MCP migration
    "search_media",
    "check_movie_availability",
    "check_movies_availability_batch",
    "check_existing_media_status",
    "check_library_inventory",
    "request_movie_for_user",
    "request_show_scope_for_user",
    "request_episode_for_user",
    "check_movie_request_status",
    "repair_requested_movie",
    "check_show_request_status",
    "get_show_season_status",
    "repair_requested_show",
    "add_requested_show_to_sickchill",
    "check_episode_status",
    "check_episode_file",
    "trigger_sickchill_manual_search",
    "clear_sickchill_ignored_episodes",
    "get_user_watch_context",
    "get_openai_token_usage",
    "run_transmission_maintenance",
    "get_admin_task_summary",
    "send_admin_message",
    "set_admin_motd",
    "clear_admin_motd",
    "broad_jackett_episode_search",
    "broad_jackett_movie_search",
    "send_admin_prowl_notice",
    "add_transmission_candidate",
}


def _user(is_admin: bool = False) -> UserContext:
    return UserContext(user_id="u1", username="ben", display_name="Ben", is_admin=is_admin)


def test_catalog_covers_all_legacy_tools():
    # Superset, not exact match: the catalog has grown beyond the original
    # 29 tools (e.g. resolve_admin_task, friendly-name tools) since the
    # MCP migration, and is expected to keep growing.
    catalog_names = {spec.name for spec in CATALOG}
    assert catalog_names >= LEGACY_TOOL_NAMES


def test_every_schema_forbids_extra_args():
    for spec in CATALOG:
        schema = spec.json_schema()
        assert schema.get("additionalProperties") is False, spec.name


def test_admin_tools_hidden_from_normal_users():
    settings = Settings()
    names = {spec.name for spec in visible_specs(_user(is_admin=False), settings)}
    admin_names = {spec.name for spec in CATALOG if ADMIN in spec.tags}
    assert admin_names, "expected admin-tagged tools in catalog"
    assert not (names & admin_names)


def test_admin_sees_admin_tools():
    settings = Settings()
    names = {spec.name for spec in visible_specs(_user(is_admin=True), settings)}
    assert "get_admin_task_summary" in names
    assert "run_transmission_maintenance" in names


def test_direct_source_gating(monkeypatch):
    settings_off = Settings(movie_direct_source_enabled=False)
    names_off = {spec.name for spec in visible_specs(_user(True), settings_off)}
    assert "broad_jackett_movie_search" not in names_off
    assert "add_transmission_candidate" not in names_off
    # non-gated escalation tools stay visible
    assert "broad_jackett_episode_search" in names_off
    assert "send_admin_prowl_notice" in names_off

    settings_on = Settings(movie_direct_source_enabled=True)
    names_on = {spec.name for spec in visible_specs(_user(True), settings_on)}
    assert "broad_jackett_movie_search" in names_on
    assert "add_transmission_candidate" in names_on


def test_get_spec_lookup():
    assert get_spec("search_media") is not None
    assert get_spec("nope") is None


# --- validation preconditions (formerly prose prompt rules) -----------------


def test_request_movie_requires_id_or_title_year():
    with pytest.raises(ValidationError, match="tmdb_id"):
        schemas.RequestMovieInput.model_validate({"title": "Heat"})
    schemas.RequestMovieInput.model_validate({"title": "Heat", "year": 1995})
    schemas.RequestMovieInput.model_validate({"tmdb_id": 949})


def test_request_show_scope_requires_positive_id_and_known_scope():
    with pytest.raises(ValidationError):
        schemas.RequestShowScopeInput.model_validate({"tvdb_id": 0, "scope": "first_season"})
    with pytest.raises(ValidationError):
        schemas.RequestShowScopeInput.model_validate({"tvdb_id": 12, "scope": "whole_thing"})
    schemas.RequestShowScopeInput.model_validate({"tvdb_id": 12, "scope": "full_series"})


def test_repair_show_scope_needs_targets():
    with pytest.raises(ValidationError, match="season"):
        schemas.RepairShowInput.model_validate({"query": "Oak Island", "scope": "season"})
    with pytest.raises(ValidationError, match="episode"):
        schemas.RepairShowInput.model_validate({"query": "Oak Island", "scope": "episode", "season": 12})
    schemas.RepairShowInput.model_validate({"query": "Oak Island", "scope": "show"})


def test_repair_movie_requires_identity():
    with pytest.raises(ValidationError, match="identity"):
        schemas.RepairMovieInput.model_validate({"issue": "wrong language"})
    schemas.RepairMovieInput.model_validate({"title": "Heat", "issue": "wrong language"})


def test_extra_args_rejected():
    with pytest.raises(ValidationError):
        schemas.SearchMediaInput.model_validate({"query": "Heat", "bogus": 1})


def test_admin_summary_specific_user_requires_user_query():
    with pytest.raises(ValidationError, match="user_query"):
        schemas.AdminTaskSummaryInput.model_validate({"scope": "specific_user"})
    schemas.AdminTaskSummaryInput.model_validate({"scope": "all_users"})


def test_resolve_admin_task_requires_note_id_or_task_query():
    with pytest.raises(ValidationError):
        schemas.ResolveAdminTaskInput.model_validate({})
    schemas.ResolveAdminTaskInput.model_validate({"note_id": 1})
    schemas.ResolveAdminTaskInput.model_validate({"task_query": "Oak Island"})


def test_friendly_name_inputs_enforce_length_bounds():
    with pytest.raises(ValidationError):
        schemas.SetMyFriendlyNameInput.model_validate({"friendly_name": ""})
    with pytest.raises(ValidationError):
        schemas.SetUserFriendlyNameInput.model_validate({"user_query": "steve", "friendly_name": "x" * 61})
    schemas.SetMyFriendlyNameInput.model_validate({"friendly_name": "Steve"})


def test_resolve_admin_task_and_set_user_friendly_name_are_admin_only():
    settings = Settings()
    admin_names = {spec.name for spec in visible_specs(_user(is_admin=True), settings)}
    normal_names = {spec.name for spec in visible_specs(_user(is_admin=False), settings)}
    for name in ("resolve_admin_task", "set_user_friendly_name"):
        assert name in admin_names
        assert name not in normal_names
    # self-service rename is not admin-gated
    assert "set_my_friendly_name" in normal_names
