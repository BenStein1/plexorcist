"""Guards against tests causing real-world side effects.

This exists because of a real incident: a Shabbos test monkeypatched
`ProwlClient.notify` -- a method that does not exist -- with `raising=False`, so
the patch silently did nothing. Combined with a bare `Settings()` reading the real
`.env`, `/issue` sent live Prowl pushes to the admin's actual phone on every test
run, and `/name` wrote a bogus entry into the real friendlynames.json.

Prowl is the only client that reaches a human being, so it gets a hard block in
conftest. These tests prove that block is actually in force, and that the
"convenience" that hid the original bug cannot come back.
"""

import pytest

from backend.config import Settings
from clients.prowl_client import ProwlClient


@pytest.mark.asyncio
async def test_prowl_cannot_send_even_with_a_real_looking_key():
    """The conftest guard is autouse -- this must not reach api.prowlapp.com."""
    client = ProwlClient(api_key="a-real-looking-key-that-must-never-be-used")
    result = await client.send_notice(summary="this must never leave the machine")
    assert result["blocked_by_tests"] is True


def test_prowl_client_still_has_the_method_conftest_patches():
    """If send_notice is renamed, the conftest patch must fail loudly rather than
    silently allow live pushes again. This is the check that was missing."""
    assert hasattr(ProwlClient, "send_notice"), (
        "conftest patches ProwlClient.send_notice; if it was renamed, update conftest "
        "IMMEDIATELY -- otherwise tests will start pushing to a real phone."
    )


def test_settings_default_to_inert_prowl_and_a_throwaway_friendly_names_file():
    """A bare Settings() in a test reads .env. It must not pick up the live key or
    the live friendlynames.json."""
    settings = Settings()
    assert not settings.prowl_api_key, "tests must never load a real Prowl API key"
    assert "test" in settings.friendly_names_path, (
        f"tests must not write to the live friendly-names file (got {settings.friendly_names_path})"
    )
