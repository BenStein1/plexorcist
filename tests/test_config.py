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


def test_effective_ollama_base_url_fallback(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    settings = Settings()
    assert settings.effective_ollama_base_url == "http://localhost:11434"
