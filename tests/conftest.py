import os
import sys
from pathlib import Path

import pytest

# Run tests against a throwaway database, never the live one.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_plexorcist.db")
os.environ.setdefault("ENVIRONMENT", "test")

# Never let a test reach the real outside world or the real on-disk state.
#
# Settings is pydantic-settings backed, so a bare Settings() in a test READS .env
# and picks up the real PROWL_API_KEY / friendlynames.json. A test that forgot to
# override those once sent live Prowl pushes to Ben's phone and wrote a bogus name
# into the real friendlynames.json. These are set before any Settings is built, so
# the default is inert no matter what a test forgets.
os.environ["PROWL_API_KEY"] = ""
os.environ["FRIENDLY_NAMES_PATH"] = "./test_friendlynames.json"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def never_notify_for_real(monkeypatch):
    """Belt and braces: even an explicit Settings(prowl_api_key="...") cannot push.

    Prowl is the only tool that reaches a human being outside the app, so it gets a
    hard block at the client rather than relying on each test to remember.
    """
    from clients.prowl_client import ProwlClient

    async def blocked(self, summary: str, priority: int = 0, event: str = "Concierge Alert") -> dict:
        return {"ok": True, "blocked_by_tests": True, "summary": summary, "priority": priority, "event": event}

    # No raising=False: if this method is ever renamed, the tests MUST fail loudly
    # rather than silently start pushing to a real phone again.
    monkeypatch.setattr(ProwlClient, "send_notice", blocked)


@pytest.fixture(autouse=True)
def no_inherited_alert_cooldowns():
    """Admin-alert cooldowns are module-level, so they outlive a test.

    Without this, a test asserting "the admin was paged" quietly depends on which
    tests ran before it: an earlier test that produced the same alert key leaves a
    15-minute cooldown behind and the send never happens.
    """
    from tools.admin_alerts import reset_admin_alert_cooldowns

    reset_admin_alert_cooldowns()
    yield
    reset_admin_alert_cooldowns()


def pytest_sessionfinish(session, exitstatus):
    for leftover in ("test_plexorcist.db", "test_friendlynames.json"):
        try:
            os.unlink(leftover)
        except FileNotFoundError:
            pass
