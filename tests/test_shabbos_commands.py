"""Shabbos Mode behaviour: parsing, confirmation, permissions, admin tasks, rendering."""

import pytest

from backend.config import Settings
from backend.logging import AuditLogger
from backend.models import UserContext
from backend.shabbos import render
from backend.shabbos.commands import COMMANDS, LAST_SEARCH_KEY, BuildContext, build_request, build_search
from backend.shabbos.confirm import PENDING_KEY, consume, mint
from backend.shabbos.flags import is_shabbos_user, set_shabbos_mode
from backend.shabbos.parser import UsageError, parse, parse_episode_token, parse_id_token
from backend.shabbos.router import ShabbosRouter
from backend.state import ConversationStore

USER = UserContext(user_id="9001", username="richard", display_name="Richard", is_admin=False, auth_source="test")
OTHER = UserContext(user_id="9002", username="dude", display_name="The Dude", is_admin=False, auth_source="test")


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path}/shabbos.db"


@pytest.fixture
def store(db_url):
    # The admin task summary joins against plex_auth_sessions, which is created by
    # PlexAuthSessionStore -- build it so the task-visibility tests exercise the
    # real query rather than tripping over a missing table.
    from backend.auth_store import PlexAuthSessionStore

    PlexAuthSessionStore(db_url)
    return ConversationStore(db_url)


@pytest.fixture
def settings(tmp_path):
    # Explicitly inert: no real Prowl key, no real friendlynames.json. conftest
    # also enforces this globally, but a test's Settings should never even be
    # capable of touching live state.
    return Settings(
        ombi_base_url="http://127.0.0.1:1",
        plex_base_url="http://127.0.0.1:1",
        prowl_api_key=None,
        friendly_names_path=str(tmp_path / "friendlynames.json"),
    )


def router_for(settings, store, user=USER):
    return ShabbosRouter(settings, store, AuditLogger(path="/dev/null"), user)


# -- the flag ------------------------------------------------------------------


def test_flag_round_trips(store):
    assert is_shabbos_user(store, USER.user_id) is False
    set_shabbos_mode(store, USER.user_id, True)
    assert is_shabbos_user(store, USER.user_id) is True
    set_shabbos_mode(store, USER.user_id, False)
    assert is_shabbos_user(store, USER.user_id) is False


def test_flag_is_per_user(store):
    set_shabbos_mode(store, USER.user_id, True)
    assert is_shabbos_user(store, OTHER.user_id) is False


# -- parsing -------------------------------------------------------------------


def test_command_token_is_case_insensitive_but_arguments_keep_case():
    parsed = parse("/SEARCH The Thing")
    assert parsed.name == "search"
    assert parsed.joined() == "The Thing"


def test_quoted_titles_survive():
    parsed = parse('/fix movie "The Thing" --year 1982')
    assert parsed.args == ["movie", "The Thing"]
    assert parsed.flags == {"year": "1982"}


def test_bare_and_valued_flags():
    parsed = parse("/request tvdb:73244 --all")
    assert parsed.flags == {"all": True}
    assert parse("/seasons The Office --season 2").flags == {"season": "2"}


def test_flag_without_a_value_is_a_usage_error():
    with pytest.raises(UsageError):
        parse("/seasons The Office --season")


def test_episode_and_id_tokens_are_exact_forms_only():
    assert parse_episode_token("s01e02") == (1, 2)
    assert parse_episode_token("S1E2") == (1, 2)
    assert parse_id_token("tmdb:1091") == ("tmdb", 1091)
    for bad in ("season1", "1x02", "e02"):
        with pytest.raises(UsageError):
            parse_episode_token(bad)
    for bad in ("imdb:tt0084787", "1091", "tmdb:"):
        with pytest.raises(UsageError):
            parse_id_token(bad)


def test_no_fuzzy_matching_of_command_names():
    """A near-miss is an error, never a guess."""
    from backend.shabbos.commands import COMMANDS_BY_NAME

    assert COMMANDS_BY_NAME.get("serach") is None
    assert COMMANDS_BY_NAME.get("Search".lower()) is not None


@pytest.mark.asyncio
async def test_invalid_syntax_returns_usage_not_a_guess(settings, store):
    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    reply = await router.handle(state, "/status The Thing")  # missing movie|show
    assert "Usage:" in reply
    assert "/status movie|show <title>" in reply


@pytest.mark.asyncio
async def test_help_is_generated_from_the_registry(settings, store):
    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    reply = await router.handle(state, "/help")
    for spec in COMMANDS:
        assert spec.usage in reply, f"/help omits {spec.name}"


# -- search -> request by stable id --------------------------------------------


CANDIDATES = [
    {"title": "The Thing", "year": 1982, "type": "movie", "tmdb_id": 1091, "tvdb_id": None},
    {"title": "The Office", "year": 2005, "type": "show", "tvdb_id": 73244, "tmdb_id": 2316, "seasons": 9},
]


def test_request_by_index_resolves_against_the_last_search():
    ctx = BuildContext(user_id=USER.user_id, support_context={LAST_SEARCH_KEY: CANDIDATES})
    movie = build_request(ctx, parse("/request 1"))
    assert movie.tool == "request_movie_for_user"
    assert movie.kwargs == {"tmdb_id": 1091}

    show = build_request(ctx, parse("/request 2 --all"))
    assert show.tool == "request_show_scope_for_user"
    assert show.kwargs == {"tvdb_id": 73244, "scope": "full_series"}


def test_show_request_defaults_to_first_season_not_the_whole_series():
    ctx = BuildContext(user_id=USER.user_id, support_context={LAST_SEARCH_KEY: CANDIDATES})
    show = build_request(ctx, parse("/request 2"))
    assert show.kwargs["scope"] == "first_season"


def test_request_by_stable_id_needs_no_prior_search():
    ctx = BuildContext(user_id=USER.user_id, support_context={})
    assert build_request(ctx, parse("/request tmdb:1091")).kwargs == {"tmdb_id": 1091}
    episode = build_request(ctx, parse("/request tvdb:73244 s02e01"))
    assert episode.tool == "request_episode_for_user"
    assert episode.kwargs == {"tvdb_id": 73244, "season": 2, "episode": 1}


def test_request_index_out_of_range_is_rejected():
    ctx = BuildContext(user_id=USER.user_id, support_context={LAST_SEARCH_KEY: CANDIDATES})
    with pytest.raises(UsageError):
        build_request(ctx, parse("/request 9"))


def test_request_index_without_a_search_is_rejected():
    ctx = BuildContext(user_id=USER.user_id, support_context={})
    with pytest.raises(UsageError):
        build_request(ctx, parse("/request 1"))


def test_search_subcommand_sets_a_media_type_filter():
    ctx = BuildContext(user_id=USER.user_id, support_context={})
    assert build_search(ctx, parse("/search movie The Thing")).result_filter == "movie"
    assert build_search(ctx, parse("/search The Thing")).result_filter is None


@pytest.mark.asyncio
async def test_search_caches_only_whitelisted_fields(settings, store, monkeypatch):
    """The Ombi `raw` passthrough must never enter conversation state."""
    from clients.ombi_client import OmbiClient
    from clients.plex_client import PlexClient

    async def fake_search(self, query):
        return {"query": query, "results": [{**CANDIDATES[0], "raw": {"internal": "secret"}}]}

    async def fake_avail(self, query):
        return {"title": query, "available": False, "matches": []}

    monkeypatch.setattr(OmbiClient, "search_media", fake_search)
    monkeypatch.setattr(PlexClient, "check_availability", fake_avail)

    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    reply = await router.handle(state, "/search The Thing")

    assert "tmdb:1091" in reply
    cached = state.support_context[LAST_SEARCH_KEY]
    assert "raw" not in cached[0]
    assert "secret" not in reply


# -- confirmation tokens -------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_does_not_act_until_confirmed(settings, store):
    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    reply = await router.handle(state, "/fix movie The Thing --year 1982")

    assert "Confirm with:  /confirm" in reply
    pending = state.support_context[PENDING_KEY]
    assert pending["tool"] == "repair_requested_movie"
    assert pending["kwargs"]["title"] == "The Thing"


def test_confirm_asks_for_no_code():
    """There is no shared secret to type -- the user is already authenticated.
    The safety is that /confirm replays the STORED action."""
    from backend.shabbos.commands import COMMANDS_BY_NAME

    assert COMMANDS_BY_NAME["confirm"].usage == "/confirm"


def test_confirm_is_single_use():
    context: dict = {}
    mint(context, user_id=USER.user_id, tool="repair_requested_movie", kwargs={}, description="x")
    assert consume(context, user_id=USER.user_id).tool == "repair_requested_movie"
    with pytest.raises(ValueError):
        consume(context, user_id=USER.user_id)


def test_confirm_is_bound_to_the_user():
    context: dict = {}
    mint(context, user_id=USER.user_id, tool="repair_requested_movie", kwargs={}, description="x")
    with pytest.raises(ValueError):
        consume(context, user_id=OTHER.user_id)


def test_confirm_expires():
    from datetime import timedelta

    import backend.shabbos.confirm as confirm_mod

    context: dict = {}
    mint(context, user_id=USER.user_id, tool="repair_requested_movie", kwargs={}, description="x")

    real_now = confirm_mod._now()
    confirm_mod._now = lambda: real_now + timedelta(minutes=6)
    try:
        with pytest.raises(ValueError, match="expired"):
            consume(context, user_id=USER.user_id)
    finally:
        confirm_mod._now = lambda: confirm_mod.datetime.now(confirm_mod.timezone.utc)


def test_confirm_with_nothing_pending_is_rejected():
    with pytest.raises(ValueError):
        consume({}, user_id=USER.user_id)


@pytest.mark.asyncio
async def test_confirm_executes_the_stored_action_not_new_user_input(settings, store, monkeypatch):
    """The confirmed action is replayed from storage, so the second message
    cannot change what was agreed to."""
    seen: dict = {}

    from tools.movie_repair_tools import MovieRepairTools

    async def fake_repair(self, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "user_summary": "Radarr is re-fetching The Thing (1982)."}

    monkeypatch.setattr(MovieRepairTools, "repair_requested_movie", fake_repair)

    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    await router.handle(state, "/fix movie The Thing --year 1982")
    assert PENDING_KEY in state.support_context

    reply = await router.handle(state, "/confirm")
    assert "re-fetching" in reply
    assert seen["title"] == "The Thing"
    assert seen["year"] == 1982


# -- permissions ---------------------------------------------------------------


def test_admin_tools_are_not_visible_to_a_shabbos_user(settings, store):
    router = router_for(settings, store)
    assert "set_shabbos_mode" not in router.visible
    assert "get_admin_task_summary" not in router.visible
    assert "get_openai_token_usage" not in router.visible


@pytest.mark.asyncio
async def test_a_forged_invocation_of_an_admin_tool_fails_closed(settings, store):
    """Even if a command builder were compromised, visible_specs still gates."""
    from backend.shabbos.commands import Invocation

    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    reply = await router._execute(
        "forged",
        Invocation(tool="get_admin_task_summary", kwargs={"scope": "all_users"}),
        state,
    )
    assert "not available in Shabbos Mode" in reply


@pytest.mark.asyncio
async def test_a_tool_without_a_renderer_fails_closed(settings, store):
    """A tool with no renderer must never be exposed -- never dump a raw dict."""
    from backend.shabbos.commands import Invocation

    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    reply = await router._execute(
        "forged",
        Invocation(tool="check_episode_file", kwargs={"show": "x", "season": 1, "episode": 1}),
        state,
    )
    assert "not available in Shabbos Mode" in reply


# -- admin tasks ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_creates_an_open_task_the_admin_can_see(settings, store, monkeypatch):
    """Ben's requirement: Shabbos users still show up in the admin task summary,
    written deterministically at command time rather than inferred by a model."""
    from clients.transmission_client import TransmissionClient
    from tools.admin_tools import AdminTools

    # NOTE: conftest already hard-blocks ProwlClient.send_notice for every test.
    # Do not patch a method name here without checking it exists -- an earlier
    # version of this test patched a non-existent `notify` with raising=False,
    # which silently did nothing and sent real pushes to a real phone.
    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    reply = await router.handle(state, "/issue playback-error The Thing freezes at 01:14:32")
    assert "admin" in reply.lower()

    admin = AdminTools(TransmissionClient("http://127.0.0.1:1"), store=store)
    summary = await admin.get_admin_task_summary(scope="all_users")
    contents = [task["content"] for task in summary["tasks"]]
    assert any("freezes at 01:14:32" in content for content in contents), contents


@pytest.mark.asyncio
async def test_issue_note_is_preserved_literally_and_not_classified(settings, store, monkeypatch):
    sent: dict = {}

    from tools.escalation_tools import EscalationTools

    async def fake_notice(self, summary, priority=0, event="Concierge Alert"):
        sent["summary"] = summary
        return {"ok": True}

    monkeypatch.setattr(EscalationTools, "send_admin_prowl_notice", fake_notice)

    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    await router.handle(state, "/issue other it goes green and buzzes at 01:14:32")

    # Verbatim, with only the fixed type token prefixed. No summarizing, no
    # rewording, no classification.
    assert sent["summary"] == "[other] it goes green and buzzes at 01:14:32"


def test_issue_rejects_an_unknown_type():
    from backend.shabbos.commands import build_issue

    ctx = BuildContext(user_id=USER.user_id, support_context={})
    with pytest.raises(UsageError):
        build_issue(ctx, parse("/issue exploded the disc melted"))
    with pytest.raises(UsageError):  # `other` still needs a note
        build_issue(ctx, parse("/issue other"))


@pytest.mark.asyncio
async def test_a_failed_request_becomes_an_open_task(settings, store, monkeypatch):
    from tools.request_tools import RequestTools

    async def fake_request(self, **kwargs):
        return {"ok": False, "status": "error", "title": "The Thing", "user_summary": "Ombi refused."}

    monkeypatch.setattr(RequestTools, "request_movie_for_user", fake_request)

    router = router_for(settings, store)
    state = store.get_or_create(USER.user_id, None)
    await router.handle(state, "/request tmdb:1091")

    from tools.admin_tools import AdminTools
    from clients.transmission_client import TransmissionClient

    admin = AdminTools(TransmissionClient("http://127.0.0.1:1"), store=store)
    summary = await admin.get_admin_task_summary(scope="all_users")
    assert summary["tasks"], "a failed request should leave the admin an open task"


# -- the admin toggle (the ONLY way the feature gets turned on) -----------------


def seed_session(db_url: str, user: UserContext) -> None:
    from datetime import datetime

    from backend.auth_store import PlexAuthSessionStore
    from backend.models import PlexAuthSession

    PlexAuthSessionStore(db_url).save(
        PlexAuthSession(
            session_id=f"sess-{user.user_id}",
            user_id=user.user_id,
            username=user.username,
            display_name=user.display_name,
            is_admin=user.is_admin,
            plex_token="x",
            auth_source="plex_oauth",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )


def admin_tools_for(store, settings):
    from clients.transmission_client import TransmissionClient
    from backend.auth_context import FriendlyNameDirectory
    from tools.admin_tools import AdminTools

    return AdminTools(
        TransmissionClient("http://127.0.0.1:1"),
        store=store,
        friendly_names=FriendlyNameDirectory(settings.friendly_names_path),
    )


@pytest.mark.asyncio
async def test_admin_can_turn_shabbos_mode_on_and_off_by_name(db_url, store, settings):
    """The real activation path: "turn on Shabbos Mode for Richard" resolves a
    named user through the same resolver set_user_friendly_name uses, then flips
    the flag. Without this, the feature is unreachable."""
    seed_session(db_url, USER)
    admin = admin_tools_for(store, settings)

    result = await admin.set_shabbos_mode(user_query="richard", enabled=True)
    assert result["ok"] is True
    assert result["user_id"] == USER.user_id
    assert is_shabbos_user(store, USER.user_id) is True
    assert "no language model" in result["user_summary"].lower()

    result = await admin.set_shabbos_mode(user_query="richard", enabled=False)
    assert result["ok"] is True
    assert is_shabbos_user(store, USER.user_id) is False


@pytest.mark.asyncio
async def test_admin_toggle_fails_gracefully_on_an_unknown_user(db_url, store, settings):
    seed_session(db_url, USER)
    admin = admin_tools_for(store, settings)

    result = await admin.set_shabbos_mode(user_query="nobody-by-that-name", enabled=True)
    assert result["ok"] is False
    assert result["action"] == "set_shabbos_mode"
    assert "user_summary" in result


@pytest.mark.asyncio
async def test_shabbos_diagnostics_report_zero_model_calls(db_url, store, settings, tmp_path):
    """The diagnostic must read the real audit log, not assert a constant."""
    import json

    seed_session(db_url, USER)
    log = tmp_path / "audit.log"
    log.write_text(
        json.dumps({"event_type": "shabbos_command", "user_id": USER.user_id, "command": "search", "ai_invoked": False})
        + "\n"
        + json.dumps({"event_type": "chat_turn", "user_id": "other", "reply": "hi"})
        + "\n"
    )

    from clients.transmission_client import TransmissionClient
    from tools.admin_tools import AdminTools

    admin = AdminTools(TransmissionClient("http://127.0.0.1:1"), store=store, audit_path=str(log))
    diag = await admin.get_shabbos_diagnostics(user_query="richard")

    assert diag["router"] == "deterministic"
    assert diag["commands_run"] == 1  # the chat_turn line is not counted
    assert diag["llm_calls"] == 0
    assert diag["embedding_calls"] == 0
    assert "Clean." in diag["user_summary"]


@pytest.mark.asyncio
async def test_diagnostics_would_shout_if_a_model_had_been_invoked(db_url, store, settings, tmp_path):
    """Proof the diagnostic reports reality rather than a hardcoded zero."""
    import json

    seed_session(db_url, USER)
    log = tmp_path / "audit.log"
    log.write_text(
        json.dumps({"event_type": "shabbos_command", "user_id": USER.user_id, "command": "search", "ai_invoked": True}) + "\n"
    )

    from clients.transmission_client import TransmissionClient
    from tools.admin_tools import AdminTools

    admin = AdminTools(TransmissionClient("http://127.0.0.1:1"), store=store, audit_path=str(log))
    diag = await admin.get_shabbos_diagnostics(user_query="richard")

    assert diag["llm_calls"] == 1
    assert "INVESTIGATE" in diag["user_summary"]


# -- rendering -----------------------------------------------------------------


def test_renderer_never_dumps_internal_passthroughs():
    text = render.render(
        "check_movie_request_status",
        {"query": "The Thing", "exists_in_ombi": True, "status": "available", "title": "The Thing",
         "year": 1982, "raw": {"apiKey": "hunter2"}},
    )
    assert "hunter2" not in text
    assert "The Thing (1982)" in text


def test_watch_context_labels_server_wide_titles_as_not_personal():
    """The catalog tells the model these are server-wide. With no model, the
    renderer must say so -- otherwise it reads as the user's own history."""
    text = render.render(
        "get_user_watch_context",
        {"resolved": True, "recently_watched": [{"title": "Alien"}], "top_movies_30d": [{"title": "Barbie"}], "top_tv_30d": []},
    )
    assert "Alien" in text
    assert "not you" in text  # Barbie is explicitly marked server-wide


def test_unknown_tool_renders_a_fail_closed_message_not_a_dict():
    text = render.render("some_tool_with_no_renderer", {"ok": True, "secret": "x"})
    assert "not available in Shabbos Mode" in text
    assert "secret" not in text


def test_search_renders_stable_ids_for_both_media_types():
    text = render.render("search_media", {"query": "x", "candidates": CANDIDATES})
    assert "tmdb:1091" in text
    assert "tvdb:73244" in text
    assert "/request 1" in text
