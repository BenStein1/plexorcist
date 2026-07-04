import os
import sys
from pathlib import Path

# Run tests against a throwaway database, never the live one.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_plexorcist.db")
os.environ.setdefault("ENVIRONMENT", "test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_sessionfinish(session, exitstatus):
    for leftover in ("test_plexorcist.db",):
        try:
            os.unlink(leftover)
        except FileNotFoundError:
            pass
