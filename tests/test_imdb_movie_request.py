from __future__ import annotations

import pytest

from tools.request_tools import RequestTools


class FakeOmbi:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def find_user_by_identity(self, username: str) -> dict:
        return {"ok": True, "exists": True, "username": username}

    async def request_movie_for_user(self, **kwargs) -> dict:
        self.requests.append(kwargs)
        return {
            "ok": True,
            "status": "requested",
            "title": kwargs.get("title") or "F/X",
            "year": kwargs.get("year") or 1986,
            "tmdb_id": kwargs.get("tmdb_id"),
            "ombi": {"requestId": 123},
        }


class FakeTmdb:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.lookups: list[str] = []

    async def resolve_movie_by_imdb_id(self, imdb_id: str) -> dict:
        self.lookups.append(imdb_id)
        return dict(self.result)


@pytest.mark.asyncio
async def test_imdb_movie_request_resolves_to_tmdb_before_ombi():
    ombi = FakeOmbi()
    tmdb = FakeTmdb(
        {
            "ok": True,
            "status": "resolved",
            "imdb_id": "tt0089118",
            "tmdb_id": 9873,
            "title": "F/X",
            "year": 1986,
        }
    )
    request_tools = RequestTools(ombi, tmdb=tmdb)

    result = await request_tools.request_movie_for_user(
        username="ben",
        imdb_id="tt0089118",
    )

    assert tmdb.lookups == ["tt0089118"]
    assert ombi.requests == [
        {
            "username": "ben",
            "tmdb_id": 9873,
            "title": "F/X",
            "year": 1986,
        }
    ]
    assert result["ok"] is True
    assert result["imdb_id"] == "tt0089118"
    assert result["tmdb_id"] == 9873
    assert result["resolved_via"] == "imdb_id"


@pytest.mark.asyncio
async def test_imdb_lookup_failure_never_calls_ombi():
    ombi = FakeOmbi()
    tmdb = FakeTmdb(
        {
            "ok": False,
            "status": "not_found",
            "imdb_id": "tt9999999",
            "reason": "TMDB returned no movie for that IMDb id.",
        }
    )
    request_tools = RequestTools(ombi, tmdb=tmdb)

    result = await request_tools.request_movie_for_user(
        username="ben",
        imdb_id="tt9999999",
    )

    assert result["ok"] is False
    assert result["status"] == "not_found"
    assert "service" not in result
    assert "failure_type" not in result
    assert ombi.requests == []


@pytest.mark.asyncio
async def test_tmdb_id_stays_direct_even_when_tmdb_resolver_is_unavailable():
    ombi = FakeOmbi()
    request_tools = RequestTools(ombi, tmdb=None)

    result = await request_tools.request_movie_for_user(
        username="ben",
        tmdb_id=9873,
    )

    assert result["ok"] is True
    assert ombi.requests[0]["tmdb_id"] == 9873
