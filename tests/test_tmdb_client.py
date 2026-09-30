from __future__ import annotations

import pytest

from clients.tmdb_client import TmdbClient


class FakeTmdb(TmdbClient):
    def __init__(self, payload: dict, api_key: str | None = "test-key") -> None:
        super().__init__(api_key=api_key, base_url="http://tmdb.test")
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def _get_json(self, path: str, *, params: dict[str, str]):
        self.calls.append((path, params))
        return self.payload


@pytest.mark.asyncio
async def test_resolve_movie_by_imdb_id_uses_tmdb_find_endpoint():
    client = FakeTmdb(
        {
            "movie_results": [
                {
                    "id": 9873,
                    "title": "F/X",
                    "release_date": "1986-02-07",
                }
            ]
        }
    )

    result = await client.resolve_movie_by_imdb_id("tt0089118")

    assert result == {
        "ok": True,
        "status": "resolved",
        "imdb_id": "tt0089118",
        "tmdb_id": 9873,
        "title": "F/X",
        "year": 1986,
    }
    assert client.calls == [
        (
            "/3/find/tt0089118",
            {"api_key": "test-key", "external_source": "imdb_id"},
        )
    ]


@pytest.mark.asyncio
async def test_resolve_movie_by_imdb_id_reports_not_found():
    client = FakeTmdb({"movie_results": []})

    result = await client.resolve_movie_by_imdb_id("tt0089118")

    assert result["ok"] is False
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_resolve_movie_by_imdb_id_requires_configured_key_without_network():
    client = FakeTmdb({"movie_results": [{"id": 9873}]}, api_key=None)

    result = await client.resolve_movie_by_imdb_id("tt0089118")

    assert result["ok"] is False
    assert result["status"] == "not_configured"
    assert client.calls == []
