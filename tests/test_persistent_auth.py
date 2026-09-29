import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import backend.main as main
from backend.ai_engine import GLOBAL_USER_ID, ENGINE_FLAG, LITELLM_MODEL_FLAG, engine_state, litellm_models, selected_engine, set_engine
from backend.auth_store import PlexAuthSessionStore
from backend.config import Settings
from backend.models import ChatRequest, PlexAuthSession
from backend.state import ConversationStore
from backend.main import _build_llm_client, _session_cookie_kwargs
from clients.llm_providers import NvidiaProviderClient
from backend.models import UserContext


def test_session_lifetime_is_fixed_from_last_successful_login():
    now = datetime.utcnow()
    session = PlexAuthSession(session_id="s", user_id="u", username="u", display_name="U", updated_at=now - timedelta(days=179))
    assert not PlexAuthSessionStore.is_expired(session, now)
    assert PlexAuthSessionStore.remaining_lifetime(session, now) > 0
    expired = session.model_copy(update={"updated_at": now - timedelta(days=180)})
    assert PlexAuthSessionStore.is_expired(expired, now)


def test_global_engine_selection_survives_store_reopen(tmp_path):
    db = f"sqlite:///{tmp_path / 'engine.db'}"
    settings = Settings(database_url=db, nvidia_api_key="test-key")
    store = ConversationStore(db)
    assert selected_engine(settings, store) == "configured"
    set_engine(settings, store, "nvidia")
    reopened = ConversationStore(db)
    assert reopened.get_user_flag(GLOBAL_USER_ID, ENGINE_FLAG) == "nvidia"
    assert selected_engine(settings, reopened) == "nvidia"


def test_unconfigured_nvidia_does_not_replace_saved_engine(tmp_path):
    db = f"sqlite:///{tmp_path / 'engine.db'}"
    configured = Settings(database_url=db, nvidia_api_key="test-key")
    store = ConversationStore(db)
    set_engine(configured, store, "configured")
    try:
        set_engine(Settings(database_url=db), store, "nvidia")
    except ValueError:
        pass
    else:
        raise AssertionError("unconfigured NVIDIA selection should fail")
    assert selected_engine(configured, store) == "configured"


def test_litellm_selection_persists_model_without_exposing_key(tmp_path):
    db = f"sqlite:///{tmp_path / 'engine.db'}"
    settings = Settings(database_url=db, litellm_base_url="https://llm.example/v1", litellm_api_key="private-key")
    store = ConversationStore(db)

    result = set_engine(settings, store, "litellm", "team/model")

    assert result["selected"] == "litellm"
    assert result["litellm"]["selected_model"] == "team/model"
    assert "private-key" not in str(result)
    assert store.get_user_flag(GLOBAL_USER_ID, LITELLM_MODEL_FLAG) == "team/model"


@pytest.mark.asyncio
async def test_litellm_model_catalog_uses_server_key_and_sanitizes_errors(monkeypatch):
    import backend.ai_engine as ai_engine

    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "team/b"}, {"id": "team/a"}, {"id": "team/a"}]}

    class Client:
        def __init__(self, *, timeout):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers):
            seen.update(url=url, headers=headers)
            return Response()

    monkeypatch.setattr(ai_engine.httpx, "AsyncClient", Client)
    settings = Settings(litellm_base_url="https://llm.example", litellm_api_key="private-key")
    models, error = await litellm_models(settings)

    assert models == ["team/a", "team/b"]
    assert error is None
    assert seen["url"] == "https://llm.example/v1/models"
    assert seen["headers"] == {"Authorization": "Bearer private-key"}


def test_nvidia_cooldowns_are_shared_in_sqlite_and_sanitized_for_admins(tmp_path):
    db = f"sqlite:///{tmp_path / 'engine.db'}"
    settings = Settings(database_url=db, nvidia_api_key="test-key")
    store = ConversationStore(db)
    set_engine(settings, store, "nvidia")
    client = _build_llm_client(settings, None, store)
    assert isinstance(client, NvidiaProviderClient)
    client._mark_failure("moonshotai/kimi-k3", 503, "private provider response")

    reopened = ConversationStore(db)
    status = engine_state(settings, reopened)["nvidia"]["models"][0]
    assert status["status"] == "cooldown"
    assert "reason" not in status
    assert "private provider response" not in str(engine_state(settings, reopened))


def test_session_cookie_attributes_follow_public_url():
    secure = _session_cookie_kwargs(Settings(base_url="https://plexorcist.example"))
    local = _session_cookie_kwargs(Settings(base_url="http://localhost:8000"), max_age=60)
    assert secure == {
        "max_age": PlexAuthSessionStore.SESSION_MAX_AGE_SECONDS,
        "expires": PlexAuthSessionStore.SESSION_MAX_AGE_SECONDS,
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "secure": True,
    }
    assert local["max_age"] == local["expires"] == 60
    assert local["secure"] is False


@pytest.mark.asyncio
async def test_admin_engine_routes_enforce_admin_and_same_origin(tmp_path):
    db = f"sqlite:///{tmp_path / 'engine.db'}"
    settings = Settings(database_url=db, base_url="https://plexorcist.example", nvidia_api_key="test-key")
    admin = UserContext(user_id="admin", username="admin", display_name="Admin", is_admin=True)
    ordinary = UserContext(user_id="user", username="user", display_name="User", is_admin=False)
    with pytest.raises(HTTPException) as denied:
        await main.get_admin_ai_engine(ordinary, settings)
    assert denied.value.status_code == 403

    def request(headers):
        return Request({
            "type": "http", "http_version": "1.1", "method": "PUT", "scheme": "http",
            "path": "/api/admin/ai-engine", "raw_path": b"/api/admin/ai-engine", "query_string": b"",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "server": ("127.0.0.1", 5500), "client": ("127.0.0.1", 1234),
        })

    payload = main.AiEngineRequest(engine="nvidia")
    with pytest.raises(HTTPException) as denied:
        await main.put_admin_ai_engine(payload, request({"Content-Type": "application/json"}), admin, settings)
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        await main.put_admin_ai_engine(
            payload,
            request({"Content-Type": "application/json", "Origin": "https://attacker.example"}),
            admin,
            settings,
        )
    assert denied.value.status_code == 403
    result = await main.put_admin_ai_engine(
        payload,
        request({"Content-Type": "application/json", "Origin": "https://plexorcist.example"}),
        admin,
        settings,
    )
    assert result["selected"] == "nvidia"
    assert selected_engine(settings, ConversationStore(db)) == "nvidia"


@pytest.mark.asyncio
async def test_settings_gear_is_admin_only(monkeypatch, tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'ui.db'}", auth_mode="plex_oauth")
    request = Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "https",
        "path": "/", "raw_path": b"/", "query_string": b"",
        "headers": [(b"host", b"plexorcist.example")],
        "server": ("plexorcist.example", 443), "client": ("127.0.0.1", 1234),
    })
    admin = UserContext(user_id="admin", username="admin", display_name="Admin", is_admin=True)
    ordinary = UserContext(user_id="user", username="user", display_name="User", is_admin=False)

    async def render_admin(_request, _settings):
        return admin

    monkeypatch.setattr(main, "_get_current_user_optional", render_admin)
    admin_html = (await main.index(request, settings)).body.decode()
    assert 'id="admin-settings-open"' in admin_html
    assert 'id="admin-settings-dialog"' in admin_html
    assert 'id="admin-litellm-model"' in admin_html
    assert 'class="panel admin-engine"' not in admin_html

    async def render_ordinary(_request, _settings):
        return ordinary

    monkeypatch.setattr(main, "_get_current_user_optional", render_ordinary)
    ordinary_html = (await main.index(request, settings)).body.decode()
    assert 'id="admin-settings-open"' not in ordinary_html
    assert 'id="admin-settings-dialog"' not in ordinary_html
    assert 'id="admin-litellm-model"' not in ordinary_html


@pytest.mark.asyncio
async def test_admin_litellm_model_catalog_and_selection_are_admin_only(monkeypatch, tmp_path):
    db = f"sqlite:///{tmp_path / 'litellm.db'}"
    settings = Settings(
        database_url=db, base_url="https://plexorcist.example",
        litellm_base_url="https://llm.example/v1", litellm_api_key="private-key",
    )
    admin = UserContext(user_id="admin", username="admin", display_name="Admin", is_admin=True)

    async def _models():
        return ["team/model"], None

    monkeypatch.setattr(main, "litellm_models", lambda _settings: _models())

    async def request(method="PUT", headers=None):
        return Request({
            "type": "http", "http_version": "1.1", "method": method,
            "path": "/api/admin/ai-engine", "raw_path": b"/api/admin/ai-engine", "query_string": b"",
            "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
            "server": ("127.0.0.1", 5500), "client": ("127.0.0.1", 1234),
        })

    state = await main.get_admin_ai_engine(admin, settings)
    assert state["litellm"]["models"] == ["team/model"]
    assert "private-key" not in str(state)

    saved = await main.put_admin_ai_engine(
        main.AiEngineRequest(engine="litellm", model="team/model"),
        await request(headers={"Content-Type": "application/json", "Origin": "https://plexorcist.example"}),
        admin, settings,
    )
    assert saved["selected"] == "litellm"
    with pytest.raises(HTTPException) as invalid:
        await main.put_admin_ai_engine(
            main.AiEngineRequest(engine="litellm", model="not-listed"),
            await request(headers={"Content-Type": "application/json", "Origin": "https://plexorcist.example"}),
            admin, settings,
        )
    assert invalid.value.status_code == 400
    assert selected_engine(settings, ConversationStore(db)) == "litellm"


@pytest.mark.asyncio
async def test_chat_stream_disconnect_does_not_cancel_or_repeat_turn(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    calls = 0

    async def slow_turn(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        completed.set()
        return SimpleNamespace(model_dump=lambda **kwargs: {"conversation_id": "c"})

    async def short_wait(tasks, timeout=None):
        await asyncio.sleep(0.01)
        done = {task for task in tasks if task.done()}
        return done, set(tasks) - done

    monkeypatch.setattr(main, "_chat_json", slow_turn)
    monkeypatch.setattr(asyncio, "wait", short_wait)
    request = Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/chat", "raw_path": b"/api/chat", "query_string": b"",
        "headers": [(b"accept", b"application/x-ndjson")],
        "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
    })
    stream = await main.chat(ChatRequest(message="hello"), request=request)
    events = stream.body_iterator
    assert await anext(events) == '{"type":"heartbeat"}\n'
    await started.wait()
    assert await anext(events) == '{"type":"heartbeat"}\n'
    await events.aclose()
    release.set()
    await asyncio.wait_for(completed.wait(), timeout=1)
    await asyncio.sleep(0)
    assert calls == 1
