import pytest

from tools.escalation_tools import EscalationTools


class _UnusedClient:
    pass


class _ProwlSuccess:
    async def send_notice(self, summary: str, priority: int = 0, event: str = "Concierge Alert") -> dict:
        return {"ok": True, "summary": summary, "priority": priority, "event": event, "status_code": 200}


class _ProwlFailure:
    async def send_notice(self, summary: str, priority: int = 0, event: str = "Concierge Alert") -> dict:
        return {"ok": False, "summary": summary, "priority": priority, "event": event, "error": "nope"}


@pytest.mark.asyncio
async def test_user_to_admin_notice_returns_exact_sent_confirmation():
    tools = EscalationTools(_UnusedClient(), _UnusedClient(), _ProwlSuccess(), user_label="Dana (dana1)")

    result = await tools.send_admin_prowl_notice("Playback stopped after the preroll")

    exact = "Dana (dana1): Playback stopped after the preroll"
    assert result["sent_message"] == exact
    assert result["delivery_confirmation"] == {
        "direction": "user_to_admin",
        "status": "sent",
        "recipient_label": "the admin",
        "message": exact,
    }
    assert result["delivery_receipt"] == f"To the admin: “{exact}”"


@pytest.mark.asyncio
async def test_failed_user_to_admin_notice_does_not_claim_delivery():
    tools = EscalationTools(_UnusedClient(), _UnusedClient(), _ProwlFailure(), user_label="Dana (dana1)")

    result = await tools.send_admin_prowl_notice("Playback failed")

    assert result["ok"] is False
    assert result["sent_message"] == "Dana (dana1): Playback failed"
    assert "delivery_confirmation" not in result
