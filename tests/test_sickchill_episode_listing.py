"""SickChill episode enumeration against the shape the real API actually returns.

The previous implementation read `seasonRequests` off the show row. That is an
Ombi field; no SickChill show has it (0 of 849 on the live server), so whole-show
repair enumerated nothing and refused. These tests run against a fixture captured
from the production server so the payload shape is the real one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clients.sickchill_client import SickChillClient


# The whole API envelope as the server sends it, so `_unwrap_data` is exercised too.
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sickchill_show_seasons.json").read_text())
SEASONS = FIXTURE["data"]


def _client() -> SickChillClient:
    return SickChillClient("http://sickchill.invalid:8081", api_key="key")


def _seasons(client: SickChillClient) -> dict[int, dict[int, dict[str, Any]]]:
    return {
        int(season): {int(episode): info for episode, info in episodes.items()}
        for season, episodes in SEASONS.items()
    }


def test_status_ignores_the_quality_parenthetical():
    """"Snatched (Best)" is the same state as "Snatched" -- the suffix records
    which release was taken. Matching the whole string sent 250 of Law & Order's
    episodes to `unsupported_episode_status`, so a show the code had understood
    perfectly well came back reported as a failed repair.
    """
    client = _client()

    assert client._normalize_episode_status("Snatched (Best)") == "snatched"
    assert client._normalize_episode_status("Snatched") == "snatched"
    assert client._normalize_episode_status("Downloaded") == "downloaded"


def test_phantom_rows_are_detected_by_missing_title_and_airdate():
    """A file named S01E101 gets indexed as episode 101 of season 1. Those rows
    have no title and no airdate, which is what identifies them -- deliberately
    not `episode > 100`, since a daytime or anime season really can run that long
    and calling its genuine episodes phantoms would hide real gaps.
    """
    client = _client()
    real = {"name": "Prescription for Death", "airdate": "1990-09-13"}
    phantom = {"name": "", "airdate": "Never"}

    assert client._is_phantom_row(phantom) is True
    assert client._is_phantom_row(real) is False
    # A high episode number with a real title and airdate is a real episode.
    assert client._is_phantom_row({"name": "Episode 130", "airdate": "2014-03-02"}) is False


def test_an_episode_whose_file_is_filed_under_a_phantom_is_not_missing():
    """The SickChill-only fallback, used when Plex cannot be reached. S01E01 has
    no file of its own, but the phantom S01E101 holds it. Re-fetching would
    download a second copy of media already there, so it must never be queued.
    Ben's correction applies to how this is *reported*, not to the guard itself:
    the file is real, so the one thing that must not happen is a re-download.
    """
    client = _client()
    seasons = {
        1: {
            1: {"name": "Pilot", "airdate": "1990-09-13", "status": "Snatched", "location": "", "file_size": 0},
            101: {
                "name": "",
                "airdate": "Never",
                "status": "Downloaded",
                "location": "/media/Law & Order/Season 01/Law & Order - S01E101.mkv",
                "file_size": 366390814,
            },
        }
    }

    result = client.classify_show_episodes(seasons)

    assert result["needs_refetch"] == []
    assert [(row["season"], row["episode"]) for row in result["misfiled_on_disk"]] == [(1, 1)]


def test_a_snatch_that_never_landed_is_only_held_back_when_plex_cannot_answer():
    """Without Plex, a snatch is ambiguous -- dead or still downloading looks the
    same -- so it is surfaced rather than fired off. Once Plex confirms it cannot
    play the episode, the ambiguity is gone: it is a gap, and it is exactly the
    gap the user is complaining about, so it becomes a target.
    """
    client = _client()
    seasons = {
        1: {
            1: {"name": "Pilot", "airdate": "1990-09-13", "status": "Snatched (Best)", "location": "", "file_size": 0}
        }
    }

    assert client.classify_show_episodes(seasons)["needs_refetch"] == []
    assert [(row["season"], row["episode"]) for row in client.classify_show_episodes(seasons)["stalled"]] == [(1, 1)]

    with_plex = client.classify_show_episodes(seasons, plex_present={1: set()})

    assert with_plex["needs_refetch"] == [(1, 1)]
    assert with_plex["stalled"] == []


def test_wanted_and_failed_episodes_are_the_ones_worth_searching():
    client = _client()
    seasons = {
        1: {
            1: {"name": "A", "airdate": "1990-09-13", "status": "Wanted", "location": "", "file_size": 0},
            2: {"name": "B", "airdate": "1990-09-20", "status": "Failed", "location": "", "file_size": 0},
            3: {"name": "C", "airdate": "1990-09-27", "status": "Downloaded", "location": "/m/c.mkv", "file_size": 9},
        }
    }

    result = client.classify_show_episodes(seasons)

    assert result["needs_refetch"] == [(1, 1), (1, 2)]
    assert result["healthy"] == [(1, 3)]


def test_specials_are_left_alone():
    """Season 0 is not what anyone means by "the show is broken"."""
    client = _client()

    result = client.classify_show_episodes(_seasons(_client()))

    assert all(season != 0 for season, _ in result["needs_refetch"])
    assert all(season != 0 for season, _ in result["healthy"])


@pytest.mark.asyncio
async def test_all_episode_numbers_reads_the_real_payload_and_costs_one_call():
    """Per-episode status lookups run ~0.5s each; Law & Order has 948 rows, so
    the obvious loop is a nine-minute repair. One bulk call covers the whole show.
    """
    client = _client()
    calls: list[dict[str, Any]] = []

    async def fake_resolve(indexer_id: int) -> dict[str, Any]:
        return {"show_name": "Law & Order", "indexerid": 72368}

    async def fake_api_get(cmd: str, timeout: float | None = None, **params: Any) -> dict[str, Any]:
        calls.append({"cmd": cmd, **params})
        return FIXTURE

    client._resolve_show_by_indexer_id = fake_resolve  # type: ignore[method-assign]
    client._api_get = fake_api_get  # type: ignore[method-assign]

    result = await client.all_episode_numbers("Law & Order", expected_indexer_id=72368)

    assert result["ok"] is True
    assert [call["cmd"] for call in calls] == ["show.seasons"]
    assert "season" not in calls[0], "fetching one season flips the response shape from nested to flat"
    assert result["diagnosis"]["episode_count"] > 0


@pytest.mark.asyncio
async def test_a_healthy_show_lists_fine_with_nothing_to_repair():
    """"Listed the episodes, none are broken" is a success. Returning ok=False
    for it would make every healthy show read as a backend failure.
    """
    client = _client()

    async def fake_resolve(indexer_id: int) -> dict[str, Any]:
        return {"show_name": "Breaking Bad", "indexerid": 81189}

    async def fake_api_get(cmd: str, timeout: float | None = None, **params: Any) -> dict[str, Any]:
        return {
            "data": {
                "1": {
                    "1": {
                        "name": "Pilot",
                        "airdate": "2008-01-20",
                        "status": "Downloaded",
                        "location": "/m/bb.mkv",
                        "file_size": 100,
                    }
                }
            }
        }

    client._resolve_show_by_indexer_id = fake_resolve  # type: ignore[method-assign]
    client._api_get = fake_api_get  # type: ignore[method-assign]

    result = await client.all_episode_numbers("Breaking Bad", expected_indexer_id=81189)

    assert result["ok"] is True
    assert result["seasons"] == {}
    assert result["reason"] is None
    assert len(result["diagnosis"]["healthy"]) == 1


def test_plex_decides_presence_not_sickchill_status():
    """Ben's correction, encoded. SickChill calls S01E01 a fileless snatch, but
    Plex plays it under the folded S01E101 number that SickChill renamed it to on
    purpose. Diagnosing from SickChill alone reported 269 Law & Order episodes as
    needing a rename; against Plex the show is simply complete.
    """
    client = _client()
    seasons = {
        1: {
            1: {"name": "Pilot", "airdate": "1990-09-13", "status": "Snatched", "location": "", "file_size": 0},
            2: {"name": "Subterranean", "airdate": "1990-09-20", "status": "Wanted", "location": "", "file_size": 0},
        }
    }

    result = client.classify_show_episodes(seasons, plex_present={1: {101, 2}})

    assert result["needs_refetch"] == []
    assert result["healthy"] == [(1, 1), (1, 2)]
    assert result["misfiled_on_disk"] == [], "an episode Plex can play is not a filing problem"


def test_the_fold_never_covers_a_season_that_really_reaches_101():
    """Most shows run 24 episodes a season; daytime and anime do not. If episode
    101 genuinely aired, it is its own episode and cannot also stand in for
    episode 1 -- folding it would hide a real gap behind a real episode.
    """
    client = _client()
    seasons = {
        1: {
            1: {"name": "Ep 1", "airdate": "2014-01-02", "status": "Wanted", "location": "", "file_size": 0},
            101: {"name": "Ep 101", "airdate": "2014-06-02", "status": "Downloaded", "location": "/m/x.mkv", "file_size": 9},
        }
    }

    result = client.classify_show_episodes(seasons, plex_present={1: {101}})

    assert result["needs_refetch"] == [(1, 1)]
    assert result["healthy"] == [(1, 101)]
