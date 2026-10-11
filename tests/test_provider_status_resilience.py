"""Provider status is non-invasive and never leaks diagnostic exception text."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from restored_status import ProviderStatus


def probe(*, clients=None, exhausted=False, snapshot=None, error=None):
    bot = NS()
    client = NS(_clients=clients if clients is not None else [object()],
                is_exhausted=lambda: exhausted)
    async def collect():
        if error:
            raise error
        return NS(provider_status=snapshot)
    return ProviderStatus(bot, "wanderer", client, 123, NS(collect=collect))


def test_normal_status_is_configuration_not_verified_live_health():
    status = asyncio.run(probe().state())
    assert status == "configured"


def test_exhaustion_and_observed_degradation_are_distinct():
    assert asyncio.run(probe(exhausted=True).state()) == "cooldown"
    assert asyncio.run(probe(snapshot="recovering").state()) == "recovering"
    assert asyncio.run(probe(snapshot="degraded").state()) == "degraded"
    assert asyncio.run(probe(clients=[]).state()) == "not_configured"


def test_provider_monitor_failure_does_not_raise_or_include_secret():
    value = asyncio.run(probe(error=RuntimeError("secret-provider-token=private")).state())
    assert value == "diagnostics_unavailable"
    assert "private" not in value


def test_public_status_stays_character_specific_and_safe():
    async def check():
        context = NS(author=NS(id=999), reply=AsyncMock())
        for name in ("scaramouche", "wanderer"):
            s = probe(error=RuntimeError("private"))
            s.name = name
            await s.command(context)
        messages = [c.args[0] for c in context.reply.await_args_list]
        assert all("private" not in m and "diagnostics_unavailable" not in m for m in messages)
        assert messages[0] != messages[1]
    asyncio.run(check())
