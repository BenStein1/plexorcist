"""LLM token usage reporting, shared by the chat route and the admin tool."""

from __future__ import annotations

from datetime import datetime

from backend.state import ConversationStore

_OPENAI_TOKEN_PRICES_PER_MILLION = {
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.20, "output": 1.25},
}


def estimate_token_cost_usd(model: str, totals: dict) -> float | None:
    prices = _OPENAI_TOKEN_PRICES_PER_MILLION.get(model)
    if not prices:
        return None
    uncached_input = max(0, int(totals.get("uncached_input_tokens") or 0))
    cached_input = max(0, int(totals.get("cached_input_tokens") or 0))
    output = max(0, int(totals.get("output_tokens") or 0))
    return round(
        (
            (uncached_input * float(prices["input"]))
            + (cached_input * float(prices["cached_input"]))
            + (output * float(prices["output"]))
        )
        / 1_000_000,
        6,
    )


def build_token_usage_report(store: ConversationStore, model: str) -> dict:
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    year_start = datetime(now.year, 1, 1)
    mtd = store.summarize_openai_token_usage_since(model=model, since=month_start)
    ytd = store.summarize_openai_token_usage_since(model=model, since=year_start)
    mtd["estimated_cost_usd"] = estimate_token_cost_usd(model, mtd)
    ytd["estimated_cost_usd"] = estimate_token_cost_usd(model, ytd)
    other_models = store.list_openai_usage_other_models_since(model=model, since=year_start)
    return {
        "ok": True,
        "action": "openai_token_usage_summary",
        "model": model,
        "currency": "USD",
        "cost_basis": "estimated_raw_cost_before_credits_or_grants",
        "mtd": mtd,
        "ytd": ytd,
        "other_models_present": bool(other_models),
        "other_models": other_models,
    }
