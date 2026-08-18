from __future__ import annotations

import pytest

from backend.config import Settings
from backend.models import UserContext
from backend.state import ConversationStore
from tools.catalog import build_toolkit
from tools.request_tools import RequestTools


class FakeOmbi:
    def __init__(self, result: dict) -> None:
        self.result = result

    async def find_user_by_identity(self, username: str) -> dict:
        return {"ok": True, "exists": True, "username": username}

    async def request_movie_for_user(self, **kwargs) -> dict:
        return {**self.result, "username": kwargs["username"], "tmdb_id": kwargs.get("tmdb_id"), "title": "Jaws", "year": 1975}

    async def request_show_scope_for_user(self, **kwargs) -> dict:
        return {**self.result, "username": kwargs["username"], "tvdb_id": kwargs["tvdb_id"], "title": "The Terror"}

    async def request_episode_for_user(self, **kwargs) -> dict:
        return {
            **self.result,
            "username": kwargs["username"],
            "tvdb_id": kwargs["tvdb_id"],
            "season": kwargs["season"],
            "episode": kwargs["episode"],
            "title": "The Terror",
        }


def store(tmp_path) -> ConversationStore:
    return ConversationStore(f"sqlite:///{tmp_path}/history.db")


@pytest.mark.asyncio
async def test_confirmed_request_records_authenticated_user(tmp_path):
    history = store(tmp_path)
    tools = RequestTools(
        FakeOmbi({"ok": True, "status": "requested", "ombi": {"requestId": 72}}),
        store=history,
        user_id="plex-123",
    )

    result = await tools.request_movie_for_user(
        username="geoff",
        tmdb_id=578,
    )

    assert result["history_recorded"] is True
    assert result["history_created"] is True
    assert result["history_id"] > 0
    assert history.list_media_request_history(user_id="plex-123") == [
        {
            "history_id": result["history_id"],
            "user_id": "plex-123",
            "username": "geoff",
            "media_type": "movie",
            "title": "Jaws",
            "year": 1975,
            "tmdb_id": 578,
            "tvdb_id": None,
            "request_scope": "movie",
            "season": None,
            "episode": None,
            "source": "ombi",
            "source_request_id": "72",
            "requested_at": history.list_media_request_history(user_id="plex-123")[0]["requested_at"],
        }
    ]


@pytest.mark.asyncio
async def test_duplicate_source_id_reuses_history_receipt(tmp_path):
    history = store(tmp_path)
    tools = RequestTools(
        FakeOmbi({"ok": True, "status": "requested", "ombi": {"requestId": 72}}),
        store=history,
        user_id="u1",
    )

    first = await tools.request_movie_for_user(username="geoff", tmdb_id=578)
    second = await tools.request_movie_for_user(username="geoff", tmdb_id=578)

    assert second["history_recorded"] is True
    assert second["history_created"] is False
    assert second["history_duplicate"] is True
    assert second["history_id"] == first["history_id"]
    assert len(history.list_media_request_history(user_id="u1")) == 1


@pytest.mark.asyncio
async def test_movie_and_episode_ids_do_not_collide_across_ombi_request_tables(tmp_path):
    history = store(tmp_path)
    movie_tools = RequestTools(
        FakeOmbi({"ok": True, "status": "requested", "ombi": {"requestId": 72}}),
        store=history,
        user_id="u1",
    )
    episode_tools = RequestTools(
        FakeOmbi({"ok": True, "status": "requested", "ombi": {"requestId": 72}}),
        store=history,
        user_id="u1",
    )

    movie = await movie_tools.request_movie_for_user(username="geoff", tmdb_id=578)
    episode = await episode_tools.request_episode_for_user(
        username="geoff", tvdb_id=123, season=1, episode=2
    )

    assert movie["history_id"] != episode["history_id"]
    assert {row["media_type"] for row in history.list_media_request_history(user_id="u1")} == {"movie", "episode"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"ok": False, "status": "error"},
        {"ok": False, "status": "unconfirmed"},
        {"ok": True, "status": "already_requested"},
        {"ok": False, "status": "already_available"},
    ],
)
async def test_non_new_request_outcomes_do_not_write_history(tmp_path, result):
    history = store(tmp_path)
    tools = RequestTools(FakeOmbi(result), store=history, user_id="u1")

    response = await tools.request_movie_for_user(username="geoff", tmdb_id=578)

    assert "history_recorded" not in response
    assert history.list_media_request_history(user_id="u1") == []


def test_request_history_is_user_scoped_and_filterable(tmp_path):
    history = store(tmp_path)
    history.record_media_request(
        user_id="u1", username="one", media_type="movie", request_scope="movie", title="Jaws"
    )
    history.record_media_request(
        user_id="u1", username="one", media_type="show", request_scope="full_series", title="The Terror"
    )
    history.record_media_request(
        user_id="u2", username="two", media_type="movie", request_scope="movie", title="Deep Blue Sea"
    )

    assert [row["title"] for row in history.list_media_request_history(user_id="u1", media_type="movie")] == ["Jaws"]
    assert [row["title"] for row in history.list_media_request_history(user_id="u2")] == ["Deep Blue Sea"]


@pytest.mark.asyncio
async def test_watch_context_adds_request_history_without_calling_it_watched(tmp_path):
    history = store(tmp_path)
    history.record_media_request(
        user_id="u1", username="one", media_type="movie", request_scope="movie", title="Jaws"
    )
    toolkit = build_toolkit(
        Settings(),
        history,
        UserContext(user_id="u1", username="one", display_name="One"),
    )

    class FakeRecommendations:
        async def get_user_watch_context(self, **kwargs):
            return {"resolved": True, "recently_watched": [{"title": "Alien"}]}

    toolkit.recs = FakeRecommendations()

    result = await toolkit.get_user_watch_context()

    assert result["recently_watched"] == [{"title": "Alien"}]
    assert [row["title"] for row in result["recent_request_history"]] == ["Jaws"]


@pytest.mark.asyncio
async def test_get_my_request_history_is_bound_to_authenticated_user(tmp_path):
    history = store(tmp_path)
    history.record_media_request(
        user_id="u1", username="one", media_type="movie", request_scope="movie", title="Jaws"
    )
    history.record_media_request(
        user_id="u2", username="two", media_type="movie", request_scope="movie", title="Deep Blue Sea"
    )
    toolkit = build_toolkit(
        Settings(),
        history,
        UserContext(user_id="u1", username="one", display_name="One"),
    )

    result = await toolkit.get_my_request_history()

    assert result["request_count"] == 1
    assert [row["title"] for row in result["requests"]] == ["Jaws"]
