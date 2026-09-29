"""Small, conservative NVIDIA model catalog adapter.

The preferred chain is deliberately code-owned.  A generated catalog can add
emergency models when it is present, but a missing or bad catalog never removes
the preferred chain or prevents startup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PREFERRED_MODELS = (
    "moonshotai/kimi-k3",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3-super-120b-a12b",
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
)
MINIMUM_PARAMETERS = 30_000_000_000
_SPECIALIST_TERMS = ("embed", "embedding", "rerank", "retrieval", "translate", "translation", "safety", "guard", "calibration", "quantum", "speech")
_CODING_TERMS = ("agentic coding", "software engineering", "terminal-style", "terminal tasks", "terminal task")
_BROAD_USE_MARKERS = ("general purpose", "chatbot", "conversational ai", "document understanding", "knowledge work", "instruction following", "language generation")
_GENERAL_TERMS = _BROAD_USE_MARKERS + ("reasoning", "function calling", "agentic")


def _parameter_count(row: dict[str, Any]) -> int | None:
    specs = row.get("specs") if isinstance(row.get("specs"), dict) else {}
    card = row.get("model_card") if isinstance(row.get("model_card"), dict) else {}
    value = specs.get("parameter_count")
    if not isinstance(value, (int, float)):
        value = card.get("parameter_count")
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _blob(row: dict[str, Any]) -> str:
    specs = row.get("specs") if isinstance(row.get("specs"), dict) else {}
    card = row.get("model_card") if isinstance(row.get("model_card"), dict) else {}
    tags = specs.get("tags") if isinstance(specs.get("tags"), list) else []
    return " ".join(str(piece) for piece in [row.get("id"), row.get("name"), specs.get("description"), card.get("best_for"), *tags] if piece).lower()


def _eligible(row: dict[str, Any], preferred: set[str]) -> bool:
    model = str(row.get("id") or "").strip()
    if not model or model in preferred or _parameter_count(row) is None:
        return False
    if _parameter_count(row) <= MINIMUM_PARAMETERS:
        return False
    registry = row.get("api_registry") if isinstance(row.get("api_registry"), dict) else {}
    availability = row.get("availability") if isinstance(row.get("availability"), dict) else {}
    integration = row.get("integration") if isinstance(row.get("integration"), dict) else {}
    specs = row.get("specs") if isinstance(row.get("specs"), dict) else {}
    if registry.get("listed") is not True or str(availability.get("free_endpoint") or "").lower() != "available":
        return False
    inputs = {str(item).lower() for item in specs.get("input_modalities_list", []) or []}
    outputs = {str(item).lower() for item in specs.get("output_modalities_list", []) or []}
    if inputs and "text" not in inputs or outputs and "text" not in outputs:
        return False
    text = _blob(row)
    if integration.get("openai_compatible") is not True and not any(
        marker in text for marker in ("function calling", "tool calling", "tool use", "tool-using")
    ):
        return False
    if any(term in text for term in _SPECIALIST_TERMS):
        return False
    if any(term in text for term in _CODING_TERMS) and not any(term in text for term in _BROAD_USE_MARKERS):
        return False
    return any(term in text for term in _GENERAL_TERMS)


def nvidia_models(catalog_path: str | None = None) -> tuple[str, ...]:
    """Return preferred NVIDIA models followed by safe catalog fallbacks."""
    models = list(PREFERRED_MODELS)
    if not catalog_path:
        return tuple(models)
    try:
        payload = json.loads(Path(catalog_path).expanduser().read_text(encoding="utf-8"))
        rows = payload.get("models")
        if not isinstance(rows, list):
            return tuple(models)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return tuple(models)
    preferred = set(models)
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not _eligible(row, preferred):
            continue
        model = str(row.get("id") or "").strip()
        if model in seen:
            continue
        seen.add(model)
        count = _parameter_count(row)
        if count is not None:
            candidates.append((count, model))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return tuple(models + [model for _, model in candidates])
