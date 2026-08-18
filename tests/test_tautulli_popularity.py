from __future__ import annotations

import pytest

from clients.tautulli_client import TautulliClient


HOME_STATS = [
    {
        "stat_id": "top_movies",
        "rows": [
            {"title": "Jaws", "year": 1975, "rating_key": "1", "total_plays": 20, "users_watched": 3},
            {"title": "Deep Blue Sea", "year": 1999, "rating_key": "2", "total_plays": 12, "users_watched": 8},
        ],
    },
    {
        "stat_id": "popular_movies",
        "rows": [
            {"title": "Deep Blue Sea", "year": 1999, "rating_key": "2", "total_plays": 12, "users_watched": 8},
            {"title": "Jaws", "year": 1975, "rating_key": "1", "total_plays": 20, "users_watched": 3},
        ],
    },
    {
        "stat_id": "top_tv",
        "rows": [
            {"title": "The Big Bang Theory", "rating_key": "3", "total_plays": 84, "users_watched": 1},
            {"title": "Silo", "rating_key": "4", "total_plays": 29, "users_watched": 7},
        ],
    },
    {
        "stat_id": "popular_tv",
        "rows": [
            {"title": "Silo", "rating_key": "4", "total_plays": 29, "users_watched": 7},
            {"title": "The Big Bang Theory", "rating_key": "3", "total_plays": 84, "users_watched": 1},
        ],
    },
]


class FakeTautulli(TautulliClient):
    def __init__(self) -> None:
        super().__init__("http://tautulli.invalid", "key")
        self.calls: list[tuple[str, dict]] = []

    async def _api(self, cmd: str, **params: str) -> object:
        self.calls.append((cmd, params))
        return HOME_STATS


@pytest.mark.asyncio
async def test_popularity_returns_both_rankings_with_both_metrics():
    client = FakeTautulli()

    result = await client.get_plex_popularity(days=30, limit=10)

    assert [row["title"] for row in result["movies_by_plays"]] == ["Jaws", "Deep Blue Sea"]
    assert [row["title"] for row in result["movies_by_unique_viewers"]] == ["Deep Blue Sea", "Jaws"]
    assert [row["title"] for row in result["tv_by_plays"]] == ["The Big Bang Theory", "Silo"]
    assert [row["title"] for row in result["tv_by_unique_viewers"]] == ["Silo", "The Big Bang Theory"]
    assert result["tv_by_plays"][0]["play_count"] == 84
    assert result["tv_by_plays"][0]["unique_viewer_count"] == 1
    assert client.calls == [("get_home_stats", {"time_range": "30", "stats_type": "plays", "stats_count": "10"})]


def test_popularity_merges_blocks_by_title_when_rating_key_is_missing():
    client = FakeTautulli()
    rankings = client._build_top_media(
        [
            {"stat_id": "top_movies", "rows": [{"title": "Jaws", "year": 1975, "total_plays": "9"}]},
            {"stat_id": "popular_movies", "rows": [{"title": "Jaws", "year": 1975, "users_watched": ["a", "b"]}]},
        ]
    )

    assert rankings["movies_by_plays"] == [
        {
            "title": "Jaws",
            "media_type": "movie",
            "year": 1975,
            "rating_key": None,
            "play_count": 9,
            "unique_viewer_count": 2,
            "users_watched": 2,
        }
    ]


def test_popularity_has_stable_tie_breakers():
    client = FakeTautulli()
    rankings = client._build_top_media(
        [
            {
                "stat_id": "top_movies",
                "rows": [
                    {"title": "Zulu", "rating_key": "1", "total_plays": 5, "users_watched": 2},
                    {"title": "Alien", "rating_key": "2", "total_plays": 5, "users_watched": 2},
                ],
            }
        ]
    )

    assert [row["title"] for row in rankings["movies_by_plays"]] == ["Alien", "Zulu"]
