"""Testy integracji w prawdziwym Home Assistancie z podmienionym API Żabki."""
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.paragony.const import DOMAIN, EVENT_NEW_RECEIPT
from custom_components.paragony.providers.base import ProviderAuthError
from custom_components.paragony.providers.zabka import build_receipt

PROVIDER = "custom_components.paragony.providers.zabka.ZabkaProvider"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def isolated_config_dir(hass: HomeAssistant, tmp_path):
    """Baza paragony.db w katalogu tymczasowym zamiast współdzielonego testing_config."""
    hass.config.config_dir = str(tmp_path)


async def test_config_flow(hass: HomeAssistant) -> None:
    with (
        patch(f"{PROVIDER}.async_send_code", AsyncMock(return_value=30)) as send,
        patch(f"{PROVIDER}.async_sign_in", AsyncMock(side_effect=[ProviderAuthError("x"), "refresh-123"])),
        patch("custom_components.paragony.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["step_id"] == "phone"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"phone": "12"})
        assert result["errors"] == {"phone": "invalid_phone"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"phone": "+48 600 100 200"})
        assert result["step_id"] == "code"
        send.assert_awaited_once_with("600100200")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
        assert result["errors"] == {"code": "invalid_code"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"chain": "zabka", "phone": "600100200", "refresh_token": "refresh-123"}
    assert result["result"].unique_id == "zabka_600100200"


async def test_setup_sync_sensors_search(hass: HomeAssistant, zabka_fixture) -> None:
    receipt = build_receipt(zabka_fixture["eprint"], zabka_fixture["receipt"])
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="zabka_600100200",
        data={"chain": "zabka", "phone": "600100200", "refresh_token": "old"},
    )
    entry.add_to_hass(hass)
    events = []
    hass.bus.async_listen(EVENT_NEW_RECEIPT, events.append)

    with (
        patch(f"{PROVIDER}.async_list_receipt_ids", AsyncMock(return_value=[receipt.external_id])),
        patch(f"{PROVIDER}.async_get_receipt", AsyncMock(return_value=receipt)),
        patch(f"{PROVIDER}.refresh_token", "new"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["refresh_token"] == "new"
    assert events == []  # pierwszy import historii nie wysyła eventów

    last = hass.states.get("sensor.zabka_ostatni_zakup") or hass.states.get("sensor.zabka_last_purchase")
    assert last is not None, [s.entity_id for s in hass.states.async_all("sensor")]
    assert float(last.state) == 31.96
    assert last.attributes["items"][2] == {"name": "WODA MIN 1,5l", "quantity": 2.0, "price": 5.0}

    response = await hass.services.async_call(
        DOMAIN, "search", {"product": "chipsy"}, blocking=True, return_response=True
    )
    assert response["count"] == 2 and response["total"] == 15.98
    assert response["items"][0]["store"] == 'Sklep "Żabka" Z0000'

    response = await hass.services.async_call(
        DOMAIN, "search", {"date_from": "2026-01-16"}, blocking=True, return_response=True
    )
    assert response["count"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_auth_error_starts_reauth(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="zabka_1", data={"chain": "zabka", "phone": "600100200", "refresh_token": "x"}
    )
    entry.add_to_hass(hass)
    with patch(f"{PROVIDER}.async_list_receipt_ids", AsyncMock(side_effect=ProviderAuthError("expired"))):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert flows and flows[0]["context"]["source"] == "reauth"
