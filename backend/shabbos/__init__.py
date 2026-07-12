"""Shabbos Mode: a deterministic, AI-free interface to Plexorcist.

"I don't roll on Shabbos."

For users who do not want to interact with a language model. Commands only, no
conversational interpretation, no model calls at runtime -- not in the request
path, not in a background job, not after the fact.

This package must never import clients.llm_providers, tools.bridge, or
backend.agent. See router.py for the invariant and tests/test_shabbos_isolation.py
for its enforcement.

Deliberately, this __init__ exposes only the flag helpers and imports nothing from
tools.*: tools/admin_tools.py needs the flag helpers, and tools/catalog.py imports
admin_tools, so pulling the router in here would create an import cycle. Import the
router directly from backend.shabbos.router.
"""

from backend.shabbos.flags import SHABBOS_FLAG, is_shabbos_user, set_shabbos_mode

__all__ = ["SHABBOS_FLAG", "is_shabbos_user", "set_shabbos_mode"]
