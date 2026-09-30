"""Settings tests under pydantic-settings v2."""

from backend.config import Settings


def test_defaults_load():
    settings = Settings()
    assert settings.app_name == "Plexorcist Concierge"
    assert settings.llm_provider in {"openai", "anthropic", "ollama"}
    assert settings.effective_llm_model


def test_env_aliases_apply(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-test")
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "45")
    settings = Settings()
    assert settings.llm_provider == "anthropic"
    assert settings.effective_llm_model == "claude-test"
    assert settings.effective_llm_request_timeout_seconds == 45


def test_tmdb_api_key_env_alias(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-test-key")
    settings = Settings()
    assert settings.tmdb_api_key == "tmdb-test-key"


def test_effective_ollama_base_url_fallback(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    settings = Settings()
    assert settings.effective_ollama_base_url == "http://localhost:11434"


def test_effective_litellm_base_url_adds_v1():
    assert Settings(litellm_base_url="https://models.example/llm").effective_litellm_base_url == "https://models.example/llm/v1"
    assert Settings(litellm_base_url="https://models.example/v1/").effective_litellm_base_url == "https://models.example/v1"
    assert Settings(lite_llm_master_key="master-key").effective_litellm_api_key == "master-key"


def test_blocked_users_path_defaults_to_local_json_file():
    settings = Settings()
    assert settings.blocked_users_path.endswith("blockedusers.json")
