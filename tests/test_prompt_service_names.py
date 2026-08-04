"""The prompt must not tell the model to name a backend service to a normal user.

Ben's rule: "in plexorcist the user doesnt KNOW about ombi. They just ask the machine
to get it. They cant/wont go to ombi."

A blanket "never name a backend service" line is not enough on its own -- the model
already had one, and still said "Ombi threw a 500", because a *more specific* line
further down told it to explain that Ombi failed. Specific beats general. So these
tests read the assembled non-admin prompt and fail on any instruction that tells the
model to say a service name, and on any example reply that models the leak.

Internal reasoning about Ombi/SickChill/Radarr is fine and stays: the prompt still has
to route. What is banned is being told what to *say*.
"""

from __future__ import annotations

import re

import pytest

from backend.agent import ConciergeAgent
from backend.models import UserContext

# Plex is deliberately absent: users have Plex accounts and know that name.
BACKEND = re.compile(r"\b(ombi|sickchill|radarr|tautulli|jackett|transmission|prowl)\b", re.IGNORECASE)
SPEECH = re.compile(
    r"\b(say|says|saying|tell (?:the user|them|him|her)|explain|describe|report)\b(?P<tail>[^.;]*)",
    re.IGNORECASE,
)
NEGATION = re.compile(r"\b(do not|don't|never|avoid|without naming|instead of)\b", re.IGNORECASE)
# "If they say ..." is the user talking, not the model -- a different speaker entirely.
OTHER_SPEAKER = re.compile(r"\b(they|the user|users|someone|he|she|a user)\s+$", re.IGNORECASE)

ADMIN = UserContext(user_id="1", username="ben", display_name="Ben", is_admin=True)
USER = UserContext(user_id="9001", username="richard", display_name="Richard", is_admin=False)


def leaky_directives(text: str) -> list[str]:
    """Lines that tell the model to say a backend service name.

    A clause is only a leak if the service name comes *after* the speech verb and the
    clause is not a prohibition -- "Do not say something is not in Ombi" is a rule we
    want, "say Ombi failed" is the one that reached a user.
    """
    leaks: list[str] = []
    for line in text.splitlines():
        for match in SPEECH.finditer(line):
            clause_start = max(line.rfind(".", 0, match.start()), line.rfind(";", 0, match.start())) + 1
            preface = line[clause_start : match.start()]
            if NEGATION.search(preface) or OTHER_SPEAKER.search(preface):
                continue
            if BACKEND.search(match.group("tail")):
                leaks.append(line.strip())
                break
    return leaks


def example_replies(text: str) -> list[str]:
    """The prompt's own sample user-facing answers -- what the model copies from."""
    samples = [match.group(1) for match in re.finditer(r'Assistant:\s*"([^"]*)"', text)]
    section = re.search(r"User-facing support examples:\n(.*?)\n\n", text, re.DOTALL)
    if section:
        samples.extend(line.strip("- ").strip() for line in section.group(1).splitlines() if line.strip())
    return samples


def agent() -> ConciergeAgent:
    return ConciergeAgent(
        bridge=None,
        ombi_continue_url="http://ombi.example",
        llm_client=None,
        admin_label="Ben",
        prowl=None,
        movie_direct_source_enabled=False,
    )


def test_the_detector_is_not_vacuous():
    """If this ever stops catching the original bug, the tests below prove nothing."""
    assert leaky_directives("- Explain that Ombi lookup failed but the tool continued.")
    assert leaky_directives("- If it fails, tell the user SickChill is unreachable.")
    assert not leaky_directives("- Do not say something is not in Ombi if the search matched.")
    assert not leaky_directives("- Ombi is the source of truth for what has been requested.")


@pytest.mark.parametrize("direct_source", [False, True])
def test_no_instruction_tells_a_normal_user_the_service_name(direct_source):
    instructions = ConciergeAgent(
        bridge=None,
        ombi_continue_url="http://ombi.example",
        llm_client=None,
        admin_label="Ben",
        prowl=None,
        movie_direct_source_enabled=direct_source,
    )._build_instructions(USER)
    assert leaky_directives(instructions) == []


def test_no_example_reply_shows_a_normal_user_the_service_name():
    for sample in example_replies(agent()._build_instructions(USER)):
        assert not BACKEND.search(sample), f"example reply names a backend: {sample!r}"


def test_the_admin_still_gets_named_services():
    """The fix is role-scoped, not a lobotomy -- Ben needs to know which box broke."""
    instructions = agent()._build_instructions(ADMIN)
    assert "report that exact source and error" in instructions
    assert BACKEND.search(instructions)
    assert leaky_directives(instructions), "admin prompt should still name services in what to say"


def test_the_non_admin_rule_is_actually_present():
    instructions = agent()._build_instructions(USER)
    assert "Never name a backend service" in instructions
    assert "report that exact source and error" not in instructions
