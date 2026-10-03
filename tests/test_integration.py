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
from custom_components.paragony.providers.lidl import build_receipt as build_lidl_receipt
from custom_components.paragony.providers.zabka import build_receipt

PROVIDER = "custom_components.paragony.providers.zabka.ZabkaProvider"
LIDL_PROVIDER = "custom_components.paragony.providers.lidl.LidlProvider"


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
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "phone"})
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


async def test_options_restock_adds_to_todo(hass: HomeAssistant, zabka_fixture) -> None:
    from homeassistant.core import SupportsResponse
    from homeassistant.helpers import entity_registry as er

    receipt = build_receipt(zabka_fixture["eprint"], zabka_fixture["receipt"])
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="zabka_600100200", data={"chain": "zabka", "phone": "600100200", "refresh_token": "x"}
    )
    entry.add_to_hass(hass)

    added: list[dict] = []
    on_list: list[str] = ["mleko 3,2% 1l"]  # już czeka na liście (inna wielkość liter)

    async def get_items(call):
        return {call.data["entity_id"]: {"items": [{"summary": s, "status": "needs_action"} for s in on_list]}}

    async def add_item(call):
        added.append(dict(call.data))
        on_list.append(call.data["item"])

    hass.services.async_register("todo", "get_items", get_items, supports_response=SupportsResponse.ONLY)
    hass.services.async_register("todo", "add_item", add_item)

    with (
        patch(f"{PROVIDER}.async_list_receipt_ids", AsyncMock(return_value=[receipt.external_id])),
        patch(f"{PROVIDER}.async_get_receipt", AsyncMock(return_value=receipt)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        recent = hass.states.get("sensor.zabka_recently_bought_30_days")
        assert recent is not None
        # zakup z fixture jest starszy niż 30 dni
        assert recent.state == "0"

        flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert flow["type"] is FlowResultType.MENU and "edit_product" not in flow["menu_options"]
        flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "todo_list"})
        flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"todo_entity": "todo.zakupy"})
        assert flow["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry.options["todo_entity"] == "todo.zakupy"

        for names, name in ((["CHIPSY SOLONE 140g"], None), (["MLEKO 3,2% 1l"], "Mleko 3,2% 1l")):
            flow = await hass.config_entries.options.async_init(entry.entry_id)
            flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "add_product"})
            flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"item_names": names})
            assert flow["step_id"] == "add_details"
            details = {"interval_days": 7} | ({"name": name} if name else {})
            schema_defaults = {str(k): k.default() for k in flow["data_schema"].schema if hasattr(k, "default")}
            flow = await hass.config_entries.options.async_configure(
                flow["flow_id"], {"name": schema_defaults["name"]} | details
            )
            assert flow["type"] is FlowResultType.CREATE_ENTRY
            await hass.async_block_till_done()

    # Chipsy: zakup 15.01 + 7 dni minął → dodane raz; Mleko już było na liście → bez duplikatu
    assert [a["item"] for a in added] == ["Chipsy solone 140g"]
    assert added[0]["entity_id"] == "todo.zakupy"
    assert added[0]["description"] == "Paragony: ostatnio kupione 15.01, co 7 dni"

    coordinator = entry.runtime_data
    await coordinator.async_update_local()
    assert len(added) == 1  # tylko raz na cykl

    registry = er.async_get(hass)
    entities = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    chips_id = next(pid for pid, p in coordinator.data["restock"].items() if p["name"] == "Chipsy solone 140g")
    number_id = entities[f"{entry.entry_id}_product_{chips_id}_interval"]
    sensor_id = entities[f"{entry.entry_id}_product_{chips_id}_due"]
    assert hass.states.get(sensor_id).state == "2026-01-22"
    assert hass.states.get(sensor_id).attributes["added_to_list_at"] is not None

    await hass.services.async_call("number", "set_value", {"entity_id": number_id, "value": 30}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(number_id).state == "30"
    assert hass.states.get(sensor_id).state == "2026-02-14"

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "remove_products"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"products": [str(chips_id)]})
    await hass.async_block_till_done()
    assert registry.async_get(number_id) is None and registry.async_get(sensor_id) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_lidl_config_flow(hass: HomeAssistant) -> None:
    with (
        patch(
            f"{LIDL_PROVIDER}.async_exchange_code",
            AsyncMock(side_effect=[ProviderAuthError("x"), "lidl-refresh"]),
        ) as exchange,
        patch(f"{LIDL_PROVIDER}.account_id", "konto-1"),
        patch("custom_components.paragony.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "lidl"})
        assert result["step_id"] == "lidl"
        first_url = result["description_placeholders"]["url"]
        assert first_url.startswith("https://accounts.lidl.com/connect/authorize?")

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"callback_url": "zly"})
        assert result["errors"] == {"callback_url": "invalid_auth_code"}
        # po nieudanej próbie generowany jest nowy link (nowe PKCE)
        assert result["description_placeholders"]["url"] != first_url

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": "com.lidlplus.app://callback?code=KOD&scope=openid"}
        )

    assert exchange.await_args.args[0] == "KOD"
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"chain": "lidl", "refresh_token": "lidl-refresh"}
    assert result["result"].unique_id == "lidl_konto-1"


async def test_lidl_setup_and_search(hass: HomeAssistant, lidl_fixture) -> None:
    receipts = {r.external_id: r for r in map(build_lidl_receipt, (lidl_fixture["html"], lidl_fixture["native"]))}
    await hass.config.async_set_time_zone("Europe/Warsaw")
    entry = MockConfigEntry(domain=DOMAIN, unique_id="lidl_konto-1", data={"chain": "lidl", "refresh_token": "old"})
    entry.add_to_hass(hass)

    with (
        patch(f"{LIDL_PROVIDER}.async_list_receipt_ids", AsyncMock(return_value=list(receipts))),
        patch(f"{LIDL_PROVIDER}.async_get_receipt", AsyncMock(side_effect=receipts.__getitem__)),
        patch(f"{LIDL_PROVIDER}.refresh_token", "rotated"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["refresh_token"] == "rotated"

    response = await hass.services.async_call(
        DOMAIN, "search", {"product": "brokuł", "chain": "lidl"}, blocking=True, return_response=True
    )
    assert response["count"] == 1
    assert response["items"][0]["price"] == 6.98
    assert response["items"][0]["purchased_at"].startswith("2025-06-10T18:30:00")


async def test_add_from_image(hass: HomeAssistant) -> None:
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.setup import async_setup_component

    from test_photo import RESPONSE

    await hass.config.async_set_time_zone("Europe/Warsaw")
    tasks: list[dict] = []
    answers = [RESPONSE]

    async def generate_data(call):
        tasks.append(dict(call.data))
        return {"conversation_id": "x", "data": answers[-1]}

    hass.services.async_register("ai_task", "generate_data", generate_data, supports_response=SupportsResponse.ONLY)
    assert await async_setup_component(hass, DOMAIN, {})
    events = []
    hass.bus.async_listen(EVENT_NEW_RECEIPT, events.append)
    image = {"media_content_id": "media-source://media_source/local/paragon.jpg", "media_content_type": "image/jpeg"}

    response = await hass.services.async_call(
        DOMAIN,
        "add_from_image",
        {"image": image, "ai_task_entity": "ai_task.google_ai_task", "dry_run": True},
        blocking=True,
        return_response=True,
    )
    assert tasks[0]["entity_id"] == "ai_task.google_ai_task"
    assert tasks[0]["attachments"] == [image]
    assert response["saved"] is False and response["duplicate_of"] is None
    assert response["receipt"]["chain"] == "biedronka" and response["receipt"]["total"] == 22.47

    response = await hass.services.async_call(DOMAIN, "add_from_image", {"image": image}, blocking=True, return_response=True)
    assert response["saved"] is True
    await hass.async_block_till_done()
    assert len(events) == 1 and events[0].data["store"] == "Biedronka 0000"
    assert "entity_id" not in tasks[1]

    found = await hass.services.async_call(
        DOMAIN, "search", {"chain": "biedronka", "product": "banany"}, blocking=True, return_response=True
    )
    assert found["count"] == 1 and found["items"][0]["price"] == 3.49
    assert found["items"][0]["purchased_at"].startswith("2026-09-30T18:42:00")

    # to samo zdjęcie drugi raz → wykryty duplikat, bez zapisu
    response = await hass.services.async_call(DOMAIN, "add_from_image", {"image": image}, blocking=True, return_response=True)
    assert response["saved"] is False and response["duplicate_of"]["chain"] == "biedronka"
    await hass.async_block_till_done()
    assert len(events) == 1

    # bez return_response akcja też działa (np. z automatyzacji)
    answers.append({"store": "Sklep", "date": "2026-09-01 10:00", "total": 10, "items": [["X", 1, 9, 0, "p"]]})
    with pytest.raises(HomeAssistantError, match="nie zgadza się"):
        await hass.services.async_call(DOMAIN, "add_from_image", {"image": image}, blocking=True)
    await hass.services.async_call(DOMAIN, "add_from_image", {"image": image, "allow_mismatch": True}, blocking=True)
    await hass.async_block_till_done()
    assert len(events) == 2 and events[1].data["chain"] == "inne"


async def test_photo_entry(hass: HomeAssistant) -> None:
    """Wpis „Papierowe paragony”: bez logowania, sensory z paragonów ze zdjęć, opcje produktów cyklicznych."""
    from homeassistant.core import SupportsResponse

    from test_photo import RESPONSE

    await hass.config.async_set_time_zone("Europe/Warsaw")

    async def generate_data(call):
        return {"conversation_id": "x", "data": RESPONSE}

    hass.services.async_register("ai_task", "generate_data", generate_data, supports_response=SupportsResponse.ONLY)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert "photo" in result["menu_options"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "photo"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"chain": "photo"} and result["title"] == "Papierowe paragony"
    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # drugi wpis nie jest potrzebny
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "photo"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"

    image = {"media_content_id": "media-source://media_source/local/paragon.jpg", "media_content_type": "image/jpeg"}
    await hass.services.async_call(DOMAIN, "add_from_image", {"image": image}, blocking=True)
    await hass.async_block_till_done()

    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    last = next(s for eid, s in states.items() if eid.startswith("sensor.papierowe_paragony") and "22.47" == s.state)
    assert last.attributes["chain"] == "biedronka" and last.attributes["store"] == "Biedronka 0000"
    count = next(s for eid, s in states.items() if eid.startswith("sensor.papierowe_paragony") and eid.endswith("count"))
    assert count.state == "1"

    found = await hass.services.async_call(DOMAIN, "search", {"chain": "photo"}, blocking=True, return_response=True)
    assert found["count"] == 4  # bez kaucji

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "add_product"})
    options = flow["data_schema"].schema["item_names"].config["options"]
    assert "MLEKO 3,2% 1L" in [o["value"] for o in options]
    assert await hass.config_entries.async_unload(entry.entry_id)
