"""The load-bearing proof: Shabbos Mode never invokes a language model.

The strategy, in order of strength:

1. A SPY that makes *any* model use explode -- constructing an OpenAI/Anthropic
   SDK client, building an LlmClient, or calling generate_response. Every command
   in the registry is then driven through the real router, against the real tool
   handlers, in both the success and the service-failure path. If a model call
   were hiding anywhere -- in a handler, a renderer, an error path, an audit hook
   -- this fails.

2. A completeness test asserting the spy above covers EVERY command in the
   registry, so a new command cannot be added without proving it is AI-free.

3. The background memory sweeper -- the one non-obvious leak -- skips these users.

4. A cheap import tripwire. Secondary: a clean import graph says nothing about
   what the handlers do, which is why (1) is the real proof.
"""

import sys

import httpx
import pytest

from backend.config import Settings
from backend.logging import AuditLogger
from backend.models import ConversationState, UserContext
from backend.shabbos.commands import COMMANDS
from backend.shabbos.flags import set_shabbos_mode
from backend.shabbos.router import ShabbosRouter
from backend.state import ConversationStore

USER = UserContext(
    user_id="9001",
    username="richard",
    display_name="Richard",
    is_admin=False,
    auth_source="test",
)


class ModelInvoked(AssertionError):
    """Raised the instant anything reaches for a language model."""


@pytest.fixture
def no_ai(monkeypatch):
    """Record (and block) every route to a language model.

    It RECORDS rather than merely raising, and the tests assert on the record.
    That distinction matters: both `_compact_conversation_once` and the Shabbos
    router wrap their work in `except Exception`, so a spy that only raised could
    be swallowed and the test would pass while a model was actually being called.
    The counter cannot be swallowed.

    Covers the indirection layers (build_llm_client / generate_response) AND the
    raw SDK constructors, so even a hand-rolled `AsyncOpenAI()` in some handler is
    caught. `backend.main` imported build_llm_client by name and therefore holds
    its own reference -- patching only the source module would leave a hole.
    """
    import anthropic
    import openai

    import backend.main as main
    import clients.llm_providers as llm

    calls: list[str] = []

    def spy(label):
        def boom(*args, **kwargs):
            calls.append(label)
            raise ModelInvoked(f"reached for a language model via {label}")

        return boom

    monkeypatch.setattr(llm, "build_llm_client", spy("llm_providers.build_llm_client"))
    monkeypatch.setattr(main, "build_llm_client", spy("main.build_llm_client"))
    monkeypatch.setattr(openai, "AsyncOpenAI", spy("openai.AsyncOpenAI"))
    monkeypatch.setattr(anthropic, "AsyncAnthropic", spy("anthropic.AsyncAnthropic"))
    for provider in ("OpenAIProviderClient", "AnthropicProviderClient", "OllamaProviderClient"):
        monkeypatch.setattr(getattr(llm, provider), "generate_response", spy(f"{provider}.generate_response"), raising=False)
    return calls


@pytest.fixture
def store(tmp_path):
    return ConversationStore(f"sqlite:///{tmp_path}/shabbos.db")


@pytest.fixture
def settings(tmp_path):
    # Every backend service points at a guaranteed-closed port, so the real tool
    # handlers run for real and genuinely fail at the network boundary.
    # prowl_api_key/friendly_names_path are inert: /issue and /name in the sweep
    # below must never reach a real phone or the real friendlynames.json.
    return Settings(
        ombi_base_url="http://127.0.0.1:1",
        plex_base_url="http://127.0.0.1:1",
        radarr_base_url="http://127.0.0.1:1",
        sickchill_base_url="http://127.0.0.1:1",
        tautulli_base_url="http://127.0.0.1:1",
        jackett_base_url="http://127.0.0.1:1",
        prowl_api_key=None,
        friendly_names_path=str(tmp_path / "friendlynames.json"),
    )


def make_router(settings, store, user=USER):
    return ShabbosRouter(settings, store, AuditLogger(path="/dev/null"), user)


def make_state(store, user=USER):
    return store.get_or_create(user.user_id, None)


# Every command in the registry, with arguments that parse. `/confirm` is fed a
# real token minted by the /fix line above it.
COMMAND_SCRIPT = [
    "/help",
    "/help search",
    "/search The Thing",
    "/search movie The Thing",
    "/search show The Office",
    "/request tmdb:1091",
    "/request tvdb:73244 --all",
    "/request tvdb:73244 s01e02",
    "/status movie The Thing",
    "/status show The Office",
    "/seasons The Office --season 2",
    "/library The Thing",
    "/inventory John Carpenter",
    "/episode The Office s01e02",
    "/fix movie The Thing --year 1982",
    "/fix show The Office --season 2",
    "/watching",
    "/issue playback-error freezes at 01:14:32",
    "/issue other something weird",
    "/name Richard",
    "/whoami",
    "/logout",
    "/confirm",
    "please just find me the thing movie",  # free-form
    "/nonsense",  # unknown command
    "/search",  # usage error
]


@pytest.mark.asyncio
async def test_every_command_runs_without_touching_a_model(no_ai, settings, store):
    """The whole registry, through real handlers, with services down. Zero AI."""
    router = make_router(settings, store)
    state = make_state(store)

    for line in COMMAND_SCRIPT:
        reply = await router.handle(state, line)
        assert isinstance(reply, str) and reply, f"no reply for {line!r}"

    assert no_ai == [], f"a language model was invoked: {no_ai}"


@pytest.mark.asyncio
async def test_every_command_runs_without_touching_a_model_on_the_success_path(no_ai, settings, store, monkeypatch):
    """Same sweep, but with the services ANSWERING.

    The closed-port sweep above only proves the pre-network path is AI-free. Here
    the service clients return realistic payloads, so the tools' success paths and
    every renderer execute for real -- still with zero model calls.
    """
    from clients.ombi_client import OmbiClient
    from clients.plex_client import PlexClient
    from clients.radarr_client import RadarrClient
    from clients.sickchill_client import SickChillClient
    from clients.tautulli_client import TautulliClient

    async def ombi_search(self, query):
        return {
            "query": query,
            "effective_query": query,
            "attempted_queries": [query],
            "source": "ombi",
            "results": [
                {"title": "The Thing", "year": 1982, "type": "movie", "tmdb_id": 1091, "tvdb_id": None, "raw": {"secret": "nope"}},
                {"title": "The Office", "year": 2005, "type": "show", "tvdb_id": 73244, "tmdb_id": 2316, "seasons": 9, "raw": {}},
            ],
        }

    async def ok(*args, **kwargs):
        return {"ok": True, "status": "ok", "title": "The Thing", "year": 1982, "tmdb_id": 1091}

    monkeypatch.setattr(OmbiClient, "search_media", ombi_search)
    monkeypatch.setattr(PlexClient, "check_availability", lambda self, q: _async({"title": q, "available": False, "matches": []}))
    monkeypatch.setattr(PlexClient, "search_catalog", lambda self, q: _async([]))
    monkeypatch.setattr(OmbiClient, "check_existing_media_status", lambda self, query: _async(
        {"query": query, "status": "available", "title": "The Thing", "year": 1982, "type": "movie",
         "tmdb_id": 1091, "fully_available": True, "best_match": {"title": "The Thing", "year": 1982, "type": "movie", "tmdb_id": 1091}}))
    monkeypatch.setattr(OmbiClient, "find_user_by_identity", lambda self, username: _async({"ok": True, "exists": True}))
    monkeypatch.setattr(OmbiClient, "request_movie_for_user", ok)
    monkeypatch.setattr(OmbiClient, "request_show_scope_for_user", ok)
    monkeypatch.setattr(OmbiClient, "request_episode_for_user", ok)
    monkeypatch.setattr(OmbiClient, "check_movie_request_status", lambda self, query, username=None: _async(
        {"query": query, "exists_in_ombi": True, "status": "available", "title": "The Thing", "year": 1982, "raw": {"secret": "nope"}}))
    monkeypatch.setattr(OmbiClient, "check_show_request_status", lambda self, query, username=None: _async(
        {"query": query, "exists_in_ombi": True, "status": "processing", "title": "The Office", "raw": {"secret": "nope"}}))
    monkeypatch.setattr(OmbiClient, "get_show_season_status", lambda self, query, season=None: _async(
        {"query": query, "found": True, "title": "The Office", "tvdb_id": 73244, "season": season,
         "episodes": [{"season": 2, "episode": 1, "title": "The Dundies", "status": "available"}],
         "missing_episodes": [], "processing_episodes": []}))
    monkeypatch.setattr(PlexClient, "check_episode_status", lambda self, **kw: _async({}), raising=False)
    monkeypatch.setattr(SickChillClient, "find_show", lambda self, *a, **k: _async({}), raising=False)
    monkeypatch.setattr(RadarrClient, "lookup_movie", lambda self, *a, **k: _async([]), raising=False)
    monkeypatch.setattr(TautulliClient, "get_user_watch_context", lambda self, **kw: _async(
        {"resolved": True, "recently_watched": [{"title": "Alien"}], "top_movies_30d": [{"title": "Barbie"}],
         "top_tv_30d": [], "year_history_summary": "You watch a lot of horror."}))
    # Prowl is hard-blocked in conftest for every test -- it is the one client that
    # reaches a human being, so it is never patched ad hoc here.

    router = make_router(settings, store)
    state = make_state(store)
    for line in COMMAND_SCRIPT:
        reply = await router.handle(state, line)
        assert isinstance(reply, str) and reply, f"no reply for {line!r}"
        # Whitelisting: the Ombi `raw` passthrough must never surface.
        assert "secret" not in reply, f"internal passthrough leaked in reply to {line!r}"

    assert no_ai == [], f"a language model was invoked: {no_ai}"


def _async(value):
    async def _coro():
        return value

    return _coro()


def test_command_script_covers_every_registered_command():
    """A new command cannot ship without being proven AI-free above."""
    scripted = {line.split()[0].lstrip("/") for line in COMMAND_SCRIPT if line.startswith("/")}
    registered = {spec.name for spec in COMMANDS}
    missing = registered - scripted
    assert not missing, f"commands not covered by the zero-AI sweep: {sorted(missing)}"


@pytest.mark.asyncio
async def test_free_form_text_never_reaches_a_model(no_ai, settings, store):
    router = make_router(settings, store)
    state = make_state(store)
    reply = await router.handle(state, "hey can you find me something like Alien but funnier")
    assert "only accepts explicit commands" in reply
    assert "/help" in reply
    assert no_ai == [], f"free-form text reached a model: {no_ai}"


@pytest.mark.asyncio
async def test_unknown_command_falls_back_to_help_not_ai(no_ai, settings, store):
    router = make_router(settings, store)
    state = make_state(store)
    reply = await router.handle(state, "/summon something")
    assert "Unknown command" in reply
    assert "/search" in reply  # the help listing
    assert no_ai == [], f"an unknown command reached a model: {no_ai}"


@pytest.mark.asyncio
async def test_service_failure_reports_plainly_and_does_not_fall_back_to_ai(no_ai, settings, store, monkeypatch):
    """Ombi is down. The user is told so. No AI retry, no AI summary."""
    from clients.ombi_client import OmbiClient

    def explode(self, *args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(OmbiClient, "search_media", explode)

    router = make_router(settings, store)
    state = make_state(store)
    reply = await router.handle(state, "/search The Thing")
    assert "Ombi" in reply
    assert "Nothing was changed." in reply
    # The critical half: a service failure must NOT trigger an AI fallback.
    assert no_ai == [], f"a service failure fell back to a model: {no_ai}"


@pytest.mark.asyncio
async def test_sweeper_skips_shabbos_conversations(no_ai, settings, store):
    """The background compactor would otherwise feed their messages to a model."""
    from backend.main import _compact_conversation_once

    set_shabbos_mode(store, USER.user_id, True)
    state = ConversationState(user_id=USER.user_id)
    state.messages.append(__import__("backend.models", fromlist=["ChatMessage"]).ChatMessage(role="user", content="/search The Thing"))
    store.save(state)

    result = await _compact_conversation_once(
        settings=Settings(memory_use_openai_summarizer=True),
        store=store,
        audit=AuditLogger(path="/dev/null"),
        state=state,
        source="test",
    )
    assert result["status"] == "skipped_shabbos"
    assert no_ai == [], f"the sweeper fed a Shabbos conversation to a model: {no_ai}"


@pytest.mark.asyncio
async def test_sweeper_still_compacts_normal_users(no_ai, settings, store):
    """The guard must be user-scoped, not a global kill switch for memory.

    A normal user DOES reach the summarizer. Asserting the spy fired here is what
    proves the skip above is specific to Shabbos users rather than the sweeper
    simply being broken for everyone.
    """
    from backend.main import _compact_conversation_once
    from backend.models import ChatMessage

    state = ConversationState(user_id="5150")
    state.messages.append(ChatMessage(role="user", content="find me Alien"))
    store.save(state)

    await _compact_conversation_once(
        settings=Settings(memory_use_openai_summarizer=True),
        store=store,
        audit=AuditLogger(path="/dev/null"),
        state=state,
        source="test",
    )
    assert no_ai, "a normal user's conversation should still reach the summarizer"


@pytest.mark.asyncio
async def test_api_chat_never_constructs_an_agent_for_a_shabbos_user(no_ai, tmp_path, monkeypatch):
    """The endpoint-level guarantee.

    Even free-form text POSTed straight at /api/chat is answered deterministically:
    the fork is upstream of build_agent(), which is what builds the LlmClient. This
    is why the mode is enforced server-side and the UI is merely cosmetic.
    """
    import backend.main as main
    from backend.models import ChatRequest

    db_url = f"sqlite:///{tmp_path}/endpoint.db"
    settings = Settings(database_url=db_url, ombi_base_url="http://127.0.0.1:1")
    store = ConversationStore(db_url)
    set_shabbos_mode(store, USER.user_id, True)

    def exploding_build_agent(*args, **kwargs):
        raise AssertionError("build_agent must never run for a Shabbos user")

    monkeypatch.setattr(main, "build_agent", exploding_build_agent)

    response = await main.chat(
        ChatRequest(message="hey, find me something fun to watch tonight"),
        user=USER,
        settings=settings,
    )
    assert "only accepts explicit commands" in response.reply
    assert response.tool_calls == []
    assert no_ai == [], f"a language model was invoked: {no_ai}"


@pytest.mark.asyncio
async def test_api_chat_still_builds_the_agent_for_a_normal_user(no_ai, tmp_path, monkeypatch):
    """The fork must be user-scoped -- normal users keep the concierge."""
    import backend.main as main
    from backend.models import ChatRequest

    db_url = f"sqlite:///{tmp_path}/endpoint2.db"
    settings = Settings(database_url=db_url)
    called: list[bool] = []

    def spy_build_agent(*args, **kwargs):
        called.append(True)
        raise RuntimeError("stop here -- we only needed to prove it was reached")

    monkeypatch.setattr(main, "build_agent", spy_build_agent)

    with pytest.raises(RuntimeError):
        await main.chat(ChatRequest(message="find me Alien"), user=USER, settings=settings)
    assert called, "a normal user must still reach build_agent"


def test_import_tripwire_shabbos_package_pulls_in_no_model_code():
    """Cheap secondary check. The zero-call spy above is the real proof."""
    for module in list(sys.modules):
        if module.startswith(("backend", "tools", "clients", "openai", "anthropic")):
            del sys.modules[module]

    import backend.shabbos.router  # noqa: F401

    leaked = [
        module
        for module in ("clients.llm_providers", "tools.bridge", "backend.agent", "openai", "anthropic")
        if module in sys.modules
    ]
    assert not leaked, f"Shabbos router transitively imports model code: {leaked}"
