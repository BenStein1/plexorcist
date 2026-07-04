"""File-backed user blocklist: directory reads and the two enforcement points."""

import json

import pytest
from fastapi import HTTPException

from backend.auth_context import BlockedUsersDirectory, get_optional_user_context, get_user_context
from backend.config import Settings
from backend.models import UserContext


class _FakeProvider:
    def __init__(self, user: UserContext) -> None:
        self._user = user

    async def resolve_user(self, request, x_plex_user, x_plex_user_id, x_plex_display_name, x_plex_is_admin) -> UserContext:
        return self._user


# --- BlockedUsersDirectory -----------------------------------------------------


def test_blocked_users_directory_matches_case_insensitively(tmp_path):
    path = tmp_path / "blockedusers.json"
    path.write_text(json.dumps({"blocked_usernames": ["Helgainaz", "alan2514"]}), encoding="utf-8")
    directory = BlockedUsersDirectory(str(path))

    assert directory.is_blocked("helgainaz")
    assert directory.is_blocked("ALAN2514")
    assert not directory.is_blocked("someoneelse")
    assert not directory.is_blocked(None)


def test_blocked_users_directory_missing_file_blocks_nobody(tmp_path):
    directory = BlockedUsersDirectory(str(tmp_path / "does-not-exist.json"))
    assert not directory.is_blocked("anyone")


def test_blocked_users_directory_reloads_after_file_change(tmp_path):
    path = tmp_path / "blockedusers.json"
    path.write_text(json.dumps({"blocked_usernames": []}), encoding="utf-8")
    directory = BlockedUsersDirectory(str(path))
    assert not directory.is_blocked("newlyblocked")

    path.write_text(json.dumps({"blocked_usernames": ["newlyblocked"]}), encoding="utf-8")
    assert directory.is_blocked("newlyblocked")


# --- enforcement in get_user_context / get_optional_user_context -------------


@pytest.mark.asyncio
async def test_get_user_context_denies_blocked_user(tmp_path, monkeypatch):
    path = tmp_path / "blockedusers.json"
    path.write_text(json.dumps({"blocked_usernames": ["blockedguy"]}), encoding="utf-8")
    settings = Settings(blocked_users_path=str(path))
    provider = _FakeProvider(UserContext(user_id="u1", username="blockedguy", display_name="Blocked Guy"))

    with pytest.raises(HTTPException) as exc_info:
        await get_user_context(
            request=None,
            x_plex_user=None,
            x_plex_user_id=None,
            x_plex_display_name=None,
            x_plex_is_admin=None,
            settings=settings,
            provider=provider,
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_get_user_context_allows_unblocked_user(tmp_path):
    path = tmp_path / "blockedusers.json"
    path.write_text(json.dumps({"blocked_usernames": ["someoneelse"]}), encoding="utf-8")
    settings = Settings(blocked_users_path=str(path))
    user = UserContext(user_id="u1", username="fine_user", display_name="Fine User")
    provider = _FakeProvider(user)

    result = await get_user_context(
        request=None,
        x_plex_user=None,
        x_plex_user_id=None,
        x_plex_display_name=None,
        x_plex_is_admin=None,
        settings=settings,
        provider=provider,
    )
    assert result is user


@pytest.mark.asyncio
async def test_get_optional_user_context_returns_none_for_blocked_user(tmp_path, monkeypatch):
    path = tmp_path / "blockedusers.json"
    path.write_text(json.dumps({"blocked_usernames": ["blockedguy"]}), encoding="utf-8")
    settings = Settings(blocked_users_path=str(path), auth_mode="dev_impersonate")
    provider = _FakeProvider(UserContext(user_id="u1", username="blockedguy", display_name="Blocked Guy"))
    monkeypatch.setattr("backend.auth_context.get_user_context_provider", lambda settings: provider)

    result = await get_optional_user_context(request=None, settings=settings)
    assert result is None
