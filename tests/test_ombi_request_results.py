"""Ombi request/response handling.

Every fixture here is shaped like something Ombi actually returned in production on
2026-08-04, when a working Altered Carbon request was reported to the user as
"didn't go through. Nothing was added."
"""

import httpx
import pytest

from backend.shabbos.render import render_request_movie_for_user, render_request_show_scope_for_user
from clients.ombi_client import OmbiClient
from tools.admin_alerts import build_request_alert
from tools.request_tools import _stamp_request_failure

ALTERED_CARBON_TVDB = 332331

# Verbatim body of the POST /api/v1/Request/tv that DID create the request. Note
# result=false with no error of any kind: it is not a failure signal.
PROD_AMBIGUOUS_RESULT = {
    "result": False,
    "isError": False,
    "errorMessage": None,
    "errorCode": None,
    "requestId": 332331,
    "message": None,
}


class FakeOmbi(OmbiClient):
    """OmbiClient with the HTTP floor replaced by routed canned payloads."""

    def __init__(self, get_routes: dict, post_result=None, post_error: Exception | None = None):
        super().__init__(base_url="http://ombi.test", api_key="test-key")
        self._get_routes = get_routes
        self._post_result = post_result
        self._post_error = post_error
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    async def get_json(self, path, params=None, headers=None, timeout=None):
        self.get_calls.append(path)
        if path not in self._get_routes:
            return {}
        value = self._get_routes[path]
        if isinstance(value, Exception):
            raise value
        return value

    async def post_json(self, path, payload, headers=None, timeout=None):
        self.post_calls.append((path, payload))
        if self._post_error is not None:
            raise self._post_error
        return self._post_result


def tv_request_record(tvdb_id=ALTERED_CARBON_TVDB, title="Altered Carbon", **child):
    """A /api/v1/Request/tv record. State lives on childRequests, not the parent."""
    return {
        "id": 41,
        "tvDbId": tvdb_id,
        "title": title,
        "childRequests": [{"id": 42, "approved": False, "available": False, "denied": False, **child}],
    }


# --------------------------------------------------------------------------------
# The headline regression: result=false is not a failure.
# --------------------------------------------------------------------------------


def test_ambiguous_result_alone_is_not_an_error():
    client = FakeOmbi(get_routes={})
    normalized = client._normalize_request_engine_result(
        result=PROD_AMBIGUOUS_RESULT,
        success_status="requested",
        error_context={},
    )
    assert normalized["status"] == "unconfirmed"
    assert normalized["ok"] is False
    # The old code mapped this to "error" via _map_request_error_status.
    assert "error" not in normalized


@pytest.mark.asyncio
async def test_landed_request_is_reported_as_success_not_failure():
    """Ben's actual bug: Ombi said result=false, the show was in the request list."""
    client = FakeOmbi(
        get_routes={
            f"/api/v2/Search/tv/{ALTERED_CARBON_TVDB}": {},
            f"/api/v2/Search/tv/moviedb/{ALTERED_CARBON_TVDB}": {},
            "/api/v1/Request/tv": [tv_request_record()],
        },
        post_result=PROD_AMBIGUOUS_RESULT,
    )

    result = await client.request_show_scope_for_user(
        username="phillip", tvdb_id=ALTERED_CARBON_TVDB, scope="full_series"
    )

    assert result["ok"] is True
    assert result["status"] == "requested"
    assert result["reconciled_by"] == "tvdb_id"
    assert result["title"] == "Altered Carbon"
    assert result["tvdb_id"] == ALTERED_CARBON_TVDB


@pytest.mark.asyncio
async def test_unconfirmed_request_absent_from_ombi_stays_unconfirmed():
    """No record, no invented success -- but say 'unconfirmed', not 'error'."""
    client = FakeOmbi(
        get_routes={"/api/v1/Request/tv": []},
        post_result=PROD_AMBIGUOUS_RESULT,
    )

    result = await client.request_show_scope_for_user(
        username="phillip", tvdb_id=ALTERED_CARBON_TVDB, scope="full_series"
    )

    assert result["ok"] is False
    assert result["status"] == "unconfirmed"
    # get_tv_detail() 204s on a TVDB id in production, so `title` is None here. The
    # prose must fall back to the id -- not "that", and not "Unknown title".
    assert result["title"] is None
    summary = result["user_summary"]
    assert f"TVDB {ALTERED_CARBON_TVDB}" in summary
    assert "for that," not in summary
    assert "Unknown title" not in summary
    # The user has no Ombi access, so the prose must not name it or send them there.
    assert "ombi" not in summary.lower()
    # Same sentence has to survive the model-free Shabbos path.
    rendered = render_request_show_scope_for_user(result)
    assert "Unknown title" not in rendered
    assert f"TVDB {ALTERED_CARBON_TVDB}" in rendered
    assert "ombi" not in rendered.lower()


def test_unconfirmed_render_without_a_user_summary_omits_the_title():
    """Defence in depth: no summary, no id, no "Unknown title" placeholder either."""
    rendered = render_request_show_scope_for_user({"ok": False, "status": "unconfirmed"})
    assert rendered == "The request was not confirmed. It may still land — give it a bit before retrying."


@pytest.mark.asyncio
async def test_real_error_is_still_an_error():
    client = FakeOmbi(
        get_routes={"/api/v1/Request/tv": []},
        post_result={
            "result": False,
            "isError": True,
            "errorMessage": "Something went wrong",
            "errorCode": None,
        },
    )

    result = await client.request_show_scope_for_user(
        username="phillip", tvdb_id=ALTERED_CARBON_TVDB, scope="full_series"
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"]["message"] == "Something went wrong"


@pytest.mark.asyncio
async def test_already_requested_error_code_is_a_success():
    client = FakeOmbi(
        get_routes={},
        post_result={
            "result": False,
            "isError": True,
            "errorMessage": "This has already been requested",
            "errorCode": "AlreadyRequested",
        },
    )

    result = await client.request_show_scope_for_user(
        username="phillip", tvdb_id=ALTERED_CARBON_TVDB, scope="full_series"
    )

    assert result["ok"] is True
    assert result["status"] == "already_requested"


@pytest.mark.asyncio
async def test_clean_success_needs_no_reconcile():
    client = FakeOmbi(
        get_routes={},
        post_result={"result": True, "isError": False, "errorMessage": None, "requestId": 7},
    )

    result = await client.request_show_scope_for_user(
        username="phillip", tvdb_id=ALTERED_CARBON_TVDB, scope="full_series"
    )

    assert result["ok"] is True
    assert result["status"] == "requested"
    assert "/api/v1/Request/tv" not in client.get_calls


@pytest.mark.asyncio
async def test_http_500_still_reconciles_by_id():
    """The full-series 500 Ben hit: check the request list before declaring failure."""
    client = FakeOmbi(
        get_routes={"/api/v1/Request/tv": [tv_request_record(approved=True)]},
        post_error=httpx.HTTPStatusError(
            "500", request=httpx.Request("POST", "http://ombi.test/api/v1/Request/tv"),
            response=httpx.Response(500, text="Object reference not set to an instance of an object."),
        ),
    )

    result = await client.request_show_scope_for_user(
        username="phillip", tvdb_id=ALTERED_CARBON_TVDB, scope="full_series"
    )

    assert result["ok"] is True
    assert result["status"] == "approved"


# --------------------------------------------------------------------------------
# Never let a title search vouch for an id we did not ask about.
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_rejects_a_different_show():
    """Asking about 332331 must not be answered with whatever ranked first."""
    client = FakeOmbi(get_routes={"/api/v1/Request/tv": [tv_request_record(tvdb_id=53243, title="Cinta 7 Susun")]})

    async def fake_status(query, username=None, tvdb_id=None):
        return {
            "exists_in_ombi": True,
            "status": "requested",
            "tvdb_id": 53243,
            "title": "Cinta 7 Susun",
            "raw": {},
        }

    client.check_show_request_status = fake_status

    reconciled = await client._reconcile_show_request_failure(
        query="Altered Carbon", tvdb_id=ALTERED_CARBON_TVDB
    )

    assert reconciled is None


@pytest.mark.asyncio
async def test_check_show_request_status_prefers_the_requested_id():
    client = FakeOmbi(get_routes={f"/api/v2/Search/tv/{ALTERED_CARBON_TVDB}": {"title": "Altered Carbon"}})

    async def fake_search(query):
        return {
            "results": [
                {"type": "show", "tvdb_id": 53243, "title": "Cinta 7 Susun", "raw": {}},
                {"type": "show", "tvdb_id": ALTERED_CARBON_TVDB, "title": "Altered Carbon", "raw": {}},
            ],
            "effective_query": query,
        }

    client.search_media = fake_search

    status = await client.check_show_request_status(query="Altered Carbon", tvdb_id=ALTERED_CARBON_TVDB)

    assert status["tvdb_id"] == ALTERED_CARBON_TVDB
    assert status["matched_by"] == "tvdb_id"
    assert status["title"] == "Altered Carbon"


@pytest.mark.asyncio
async def test_denied_request_is_not_dressed_up_as_success():
    client = FakeOmbi(
        get_routes={"/api/v1/Request/tv": [tv_request_record(denied=True)]},
        post_result=PROD_AMBIGUOUS_RESULT,
    )

    result = await client.request_show_scope_for_user(
        username="phillip", tvdb_id=ALTERED_CARBON_TVDB, scope="full_series"
    )

    assert result["ok"] is False
    assert result["status"] == "denied"


# --------------------------------------------------------------------------------
# TVDB ids must not be resolved through the TheMovieDb-keyed endpoint.
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tv_detail_asks_the_tvdb_endpoint_first():
    client = FakeOmbi(
        get_routes={
            f"/api/v2/Search/tv/{ALTERED_CARBON_TVDB}": {"title": "Altered Carbon"},
            f"/api/v2/Search/tv/moviedb/{ALTERED_CARBON_TVDB}": {"title": "Something Else"},
        }
    )

    detail = await client.get_tv_detail(ALTERED_CARBON_TVDB)

    assert detail["title"] == "Altered Carbon"
    assert client.get_calls == [f"/api/v2/Search/tv/{ALTERED_CARBON_TVDB}"]


@pytest.mark.asyncio
async def test_moviedb_fallback_is_flagged_not_trusted():
    client = FakeOmbi(
        get_routes={
            "/api/v2/Search/tv/53243": {},
            "/api/v2/Search/tv/moviedb/53243": {"title": "Cinta 7 Susun", "available": True},
        }
    )

    detail = await client.get_tv_detail(53243)
    usable, unverified = client._split_detail_namespace(detail)

    assert usable == {}
    assert unverified == "Cinta 7 Susun"


@pytest.mark.asyncio
async def test_wrong_namespace_detail_never_gates_or_names_the_show():
    """available=True on an unrelated show must not short-circuit the request."""
    client = FakeOmbi(
        get_routes={
            "/api/v2/Search/tv/53243": {},
            "/api/v2/Search/tv/moviedb/53243": {"title": "Cinta 7 Susun", "available": True},
            "/api/v1/Request/tv": [],
        },
        post_result={"result": True, "isError": False},
    )

    result = await client.request_show_scope_for_user(username="phillip", tvdb_id=53243, scope="full_series")

    assert result["status"] == "requested"
    assert result["title"] is None
    assert result["unresolved_tvdb_id"] is True
    assert result["unverified_title"] == "Cinta 7 Susun"


# --------------------------------------------------------------------------------
# Scope selection. LATENT GUARDS: prod has never returned a populated
# `seasonRequests`, so _build_request_seasons() short-circuits on `[]` and the real
# payload is `firstSeason: true, seasons: []`. These fixtures are hand-fed.
# --------------------------------------------------------------------------------


def test_first_season_skips_specials():
    client = FakeOmbi(get_routes={})
    detail = {
        "seasonRequests": [
            {"seasonNumber": 0, "episodes": [{"episodeNumber": 1}]},
            {"seasonNumber": 1, "episodes": [{"episodeNumber": 1}, {"episodeNumber": 2}]},
        ]
    }

    seasons = client._build_request_seasons(detail, scope="first_season")

    assert [season["seasonNumber"] for season in seasons] == [1]


def test_first_season_falls_back_when_specials_are_all_there_is():
    client = FakeOmbi(get_routes={})
    detail = {"seasonRequests": [{"seasonNumber": 0, "episodes": [{"episodeNumber": 1}]}]}

    seasons = client._build_request_seasons(detail, scope="first_season")

    assert [season["seasonNumber"] for season in seasons] == [0]


# --------------------------------------------------------------------------------
# The movie path shares _normalize_request_engine_result.
# --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_movie_ambiguous_result_reconciles_too():
    client = FakeOmbi(
        get_routes={"/api/v2/Search/movie/603": {"title": "The Matrix"}},
        post_result=PROD_AMBIGUOUS_RESULT,
    )

    async def fake_status(query, username=None, tmdb_id=None):
        return {"exists_in_ombi": True, "status": "requested", "tmdb_id": tmdb_id,
                "matched_by": "tmdb_id", "raw": {"title": "The Matrix"}}

    client.check_movie_request_status = fake_status

    result = await client.request_movie_for_user(username="phillip", tmdb_id=603)

    assert result["ok"] is True
    assert result["status"] == "requested"
    assert result["request_reconciled"] is True


# --------------------------------------------------------------------------------
# A success `message` is not an error message.
# --------------------------------------------------------------------------------

# Verbatim shape of the POST /api/v1/Request/movie that DID create Kat and Ryan's
# Commitments request, 2026-08-10. Unlike the TV engine, the movie engine fills
# `message` on the way out -- and it says the opposite of what it was read as.
PROD_MOVIE_SUCCESS = {
    "result": True,
    "isError": False,
    "errorMessage": None,
    "errorCode": None,
    "message": "The Commitments (1991) has been successfully added!",
    "requestId": 4471,
}


def test_success_message_is_not_an_error():
    """The reported bug: 'the request failed against Ombi: ...successfully added!'"""
    client = FakeOmbi(get_routes={})
    normalized = client._normalize_request_engine_result(
        result=PROD_MOVIE_SUCCESS,
        success_status="requested",
        error_context={"title": "The Commitments", "tmdb_id": 10437},
    )
    assert normalized["ok"] is True
    assert normalized["status"] == "requested"
    assert "error" not in normalized
    assert "user_summary" not in normalized


@pytest.mark.asyncio
async def test_landed_movie_request_never_reaches_the_admin_alert():
    """End to end over the path that paged Ben: tool result must not look like a fault."""
    client = FakeOmbi(
        get_routes={"/api/v2/Search/movie/10437": {"title": "The Commitments"}},
        post_result=PROD_MOVIE_SUCCESS,
    )

    result = await client.request_movie_for_user(username="rmk1900", tmdb_id=10437)

    assert result["ok"] is True
    assert result["status"] == "requested"
    # No reconcile needed: Ombi said yes outright.
    assert "/api/v1/Request/movie/search/The Commitments" not in client.get_calls
    stamped = _stamp_request_failure(result, operation="movie_request")
    assert "failure_type" not in stamped
    assert (
        build_request_alert(
            user_label="Kat and Ryan (rmk1900)",
            name="request_movie_for_user",
            result=stamped,
            subject="The Commitments",
        )
        is None
    )
    assert "successfully added" not in render_request_movie_for_user(stamped)


def test_message_still_classifies_when_ombi_omits_error_message():
    """`message` stays live on a non-success payload -- these two must not degrade
    into 'unconfirmed', least of all the permission refusal, which cannot self-heal
    through the reconcile because no request was ever created."""
    client = FakeOmbi(get_routes={})

    already = client._normalize_request_engine_result(
        result={"result": False, "isError": False, "errorMessage": None,
                "message": "This has already been requested"},
        success_status="requested",
        error_context={},
    )
    assert already["status"] == "already_requested"
    assert already["ok"] is True

    denied = client._normalize_request_engine_result(
        result={"result": False, "isError": False, "errorMessage": None,
                "message": "You do not have the correct permissions to request this"},
        success_status="requested",
        error_context={},
    )
    assert denied["status"] == "permission_denied"
    assert denied["ok"] is False


# --------------------------------------------------------------------------------
# The movie reconcile must match on the id, not on whatever ranked first.
# --------------------------------------------------------------------------------

COMMITMENTS_TMDB = 10437


def movie_request_record(tmdb_id=COMMITMENTS_TMDB, title="The Commitments", **fields):
    """A /api/v1/Request/movie/search/{query} hit, keyed like the real thing.

    Copied from a live response in plexorcist.log. The earlier version of this fixture
    invented a `requested: True` key; a real Ombi movie request has no such field, and
    the invention hid a bug where a landed-but-unapproved request read as no request.
    """
    return {"id": 71, "theMovieDbId": tmdb_id, "title": title,
            "requestStatus": "Common.ProcessingRequest",
            "approved": False, "available": False, "denied": False, **fields}


@pytest.mark.asyncio
async def test_movie_reconcile_rejects_a_different_movie():
    """A title search that answers with some other film must not vouch for this one."""
    client = FakeOmbi(
        get_routes={
            "/api/v2/Search/movie/10437": {"title": "The Commitments"},
            "/api/v1/Request/movie/search/The Commitments": [
                movie_request_record(tmdb_id=99999, title="Commitment")
            ],
        },
        post_result=PROD_AMBIGUOUS_RESULT,
    )

    result = await client.request_movie_for_user(username="rmk1900", tmdb_id=COMMITMENTS_TMDB)

    assert result["ok"] is False
    assert result["status"] == "unconfirmed"


@pytest.mark.asyncio
async def test_movie_reconcile_finds_a_match_that_did_not_rank_first():
    """The opposite failure: id-checking only results[0] would call this unconfirmed
    and page Ben about a request that is sitting right there in the list."""
    client = FakeOmbi(
        get_routes={
            "/api/v2/Search/movie/10437": {"title": "The Commitments"},
            "/api/v1/Request/movie/search/The Commitments": [
                movie_request_record(tmdb_id=99999, title="Commitment"),
                movie_request_record(),
            ],
        },
        post_result=PROD_AMBIGUOUS_RESULT,
    )

    result = await client.request_movie_for_user(username="rmk1900", tmdb_id=COMMITMENTS_TMDB)

    assert result["ok"] is True
    assert result["status"] == "requested"
    assert result["reconciled_by"] == "tmdb_id"
    assert result["title"] == "The Commitments"
    assert result["tmdb_id"] == COMMITMENTS_TMDB


@pytest.mark.asyncio
async def test_movie_reconcile_confirms_a_request_still_awaiting_approval():
    """Ombi auto-approves most of Ben's requests, which is what hid this: a request
    sitting unapproved carries no `requested` key, only requestStatus
    "Common.ProcessingRequest". Read with the search-hit extractor that is no landed
    state, so a request that plainly exists was called unconfirmed and paged Ben."""
    client = FakeOmbi(
        get_routes={
            "/api/v2/Search/movie/10437": {"title": "The Commitments"},
            "/api/v1/Request/movie/search/The Commitments": [movie_request_record()],
        },
        post_result=PROD_AMBIGUOUS_RESULT,
    )

    result = await client.request_movie_for_user(username="rmk1900", tmdb_id=COMMITMENTS_TMDB)

    assert result["ok"] is True
    assert result["status"] == "requested"
    assert result["request_reconciled"] is True
    # A landed request must not acquire the {service, failure_type} shape that pages Ben.
    assert _stamp_request_failure(dict(result), operation="request_movie") == result


@pytest.mark.asyncio
async def test_movie_request_record_states_are_read_off_the_record():
    """The approved/available/denied trio still outranks the "requested" floor."""
    client = FakeOmbi(get_routes={})

    assert client._extract_movie_request_record_status({}) == "missing"
    assert client._extract_movie_request_record_status(movie_request_record()) == "requested"
    assert client._extract_movie_request_record_status(
        movie_request_record(approved=True)) == "approved"
    assert client._extract_movie_request_record_status(
        movie_request_record(approved=True, available=True)) == "available"
    assert client._extract_movie_request_record_status(
        movie_request_record(denied=True)) == "denied"


@pytest.mark.asyncio
async def test_existing_media_status_keeps_the_request_lookup_it_paid_for():
    """check_existing_media_status called check_movie_request_status and then fed the
    whole envelope to a record extractor, which matched none of its keys and said
    "missing" every time -- so an already-requested movie read as never requested."""
    client = FakeOmbi(
        get_routes={
            "/api/v2/Search/movie/10437": {"title": "The Commitments", "releaseDate": "1991-08-14T00:00:00"},
            "/api/v1/Request/movie/search/The Commitments": [
                movie_request_record(approved=True, requestId=4471)
            ],
        },
    )

    async def fake_search(query, **kwargs):
        return {"results": [{"type": "movie", "tmdb_id": COMMITMENTS_TMDB, "title": "The Commitments", "raw": {}}]}

    client.search_media = fake_search

    status = await client.check_existing_media_status(query="The Commitments")
    movie = next(item for item in status["candidates"] if item["type"] == "movie")

    assert movie["requested"] is True
    assert movie["status"] == "approved"
    assert movie["request_id"] == 4471
