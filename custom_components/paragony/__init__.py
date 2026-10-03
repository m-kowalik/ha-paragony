"""Paragony — e-paragony z aplikacji sieci handlowych w Home Assistant."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CHAIN_LIDL,
    CHAIN_NAMES,
    CHAIN_PHOTO,
    CHAIN_ZABKA,
    CONF_CHAIN,
    CONF_REFRESH_TOKEN,
    DB_FILENAME,
    DOMAIN,
    EVENT_NEW_RECEIPT,
    RESTOCK_INTERVAL,
    SERVICE_ADD_FROM_IMAGE,
    SERVICE_SEARCH,
    SERVICE_SYNC,
)
from .coordinator import ParagonyCoordinator, receipt_event_data
from .db import ReceiptDB
from .photo import INSTRUCTIONS, PhotoReceiptError, build_receipt as build_photo_receipt
from .providers.lidl import LidlProvider
from .providers.photo import PhotoProvider
from .providers.zabka import ZabkaProvider

PLATFORMS = [Platform.NUMBER, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
DATA_DB = "db"

type ParagonyConfigEntry = ConfigEntry[ParagonyCoordinator]

SEARCH_SCHEMA = vol.Schema(
    {
        vol.Optional("product"): cv.string,
        vol.Optional("date_from"): cv.date,
        vol.Optional("date_to"): cv.date,
        vol.Optional("chain"): vol.In(list(CHAIN_NAMES)),
        vol.Optional("include_deposits", default=False): cv.boolean,
        vol.Optional("limit", default=100): vol.All(vol.Coerce(int), vol.Range(min=1, max=5000)),
    }
)

ADD_FROM_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Required("image"): vol.Schema(
            {vol.Required("media_content_id"): cv.string, vol.Optional("media_content_type"): cv.string},
            extra=vol.ALLOW_EXTRA,
        ),
        vol.Optional("ai_task_entity"): cv.entity_domain("ai_task"),
        vol.Optional("dry_run", default=False): cv.boolean,
        vol.Optional("allow_mismatch", default=False): cv.boolean,
    }
)


async def _async_get_db(hass: HomeAssistant) -> ReceiptDB:
    data = hass.data.setdefault(DOMAIN, {})
    if DATA_DB not in data:
        data[DATA_DB] = await hass.async_add_executor_job(ReceiptDB, hass.config.path(DB_FILENAME))
    return data[DATA_DB]


def _local_start(day: date) -> datetime:
    return dt_util.start_of_local_day(day)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def async_search(call: ServiceCall) -> ServiceResponse:
        db = await _async_get_db(hass)
        date_from: date | None = call.data.get("date_from")
        date_to: date | None = call.data.get("date_to")
        rows = await hass.async_add_executor_job(
            lambda: db.search(
                product=call.data.get("product"),
                start=_local_start(date_from) if date_from else None,
                end=_local_start(date_to + timedelta(days=1)) if date_to else None,
                chain=call.data.get("chain"),
                include_deposits=call.data["include_deposits"],
                limit=call.data["limit"],
            )
        )
        items = [
            {
                "purchased_at": dt_util.as_local(datetime.fromisoformat(row["purchased_at"])).isoformat(),
                "chain": row["chain"],
                "store": row["store_name"],
                "address": row["store_address"],
                "product": row["name"],
                "quantity": row["quantity"],
                "unit": row["unit"],
                "unit_price": row["unit_price"] / 100,
                "price": row["final_price"] / 100,
                "discount": row["discount"] / 100,
            }
            for row in rows
        ]
        return {
            "count": len(items),
            "total": round(sum(item["price"] for item in items), 2),
            "items": items,
        }

    async def async_sync(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_refresh()

    async def async_add_from_image(call: ServiceCall) -> ServiceResponse:
        image = call.data["image"]
        task = {
            "task_name": "paragony_receipt_photo",
            "instructions": INSTRUCTIONS,
            "attachments": [
                {
                    "media_content_id": image["media_content_id"],
                    "media_content_type": image.get("media_content_type") or "image/jpeg",
                }
            ],
        }
        if entity_id := call.data.get("ai_task_entity"):
            task["entity_id"] = entity_id
        if not hass.services.has_service("ai_task", "generate_data"):
            raise ServiceValidationError("Brak integracji AI Task — skonfiguruj encję ai_task (np. Google Gemini)")
        try:
            response = await hass.services.async_call(
                "ai_task", "generate_data", task, blocking=True, return_response=True
            )
        except HomeAssistantError as err:
            hint = " Zwiększ maksymalną liczbę tokenów w ustawieniach encji AI Task." if "MAX_TOKENS" in str(err) else ""
            raise HomeAssistantError(f"AI Task nie odczytał zdjęcia: {err}{hint}") from err
        try:
            result = build_photo_receipt(
                (response or {}).get("data"), dt_util.get_default_time_zone(), image["media_content_id"]
            )
        except PhotoReceiptError as err:
            raise HomeAssistantError(str(err)) from err
        receipt = result.receipt

        db = await _async_get_db(hass)
        duplicate = await hass.async_add_executor_job(db.find_similar, receipt.purchased_at, receipt.total)
        saved = False
        if not call.data["dry_run"] and duplicate is None:
            if result.mismatch and not call.data["allow_mismatch"]:
                raise HomeAssistantError(
                    f"Suma pozycji ({result.items_total / 100:.2f}) nie zgadza się z sumą paragonu "
                    f"({receipt.total / 100:.2f}). Sprawdź wynik z dry_run albo użyj allow_mismatch."
                )
            saved = await hass.async_add_executor_job(db.insert, receipt)
        if saved:
            hass.bus.async_fire(EVENT_NEW_RECEIPT, receipt_event_data(receipt))
            for entry in hass.config_entries.async_loaded_entries(DOMAIN):
                await entry.runtime_data.async_update_local()

        return {
            "saved": saved,
            "duplicate_of": (
                {
                    "chain": duplicate["chain"],
                    "receipt_id": duplicate["external_id"],
                    "store": duplicate["store_name"],
                }
                if duplicate
                else None
            ),
            "mismatch": result.mismatch / 100,
            "receipt": {
                **receipt_event_data(receipt),
                "address": receipt.store_address,
                "items": [
                    {
                        "name": i.name,
                        "kind": i.kind,
                        "quantity": i.quantity,
                        "unit": i.unit,
                        "unit_price": i.unit_price / 100,
                        "discount": i.discount / 100,
                        "price": i.final_price / 100,
                    }
                    for i in receipt.items
                ],
            },
        }

    hass.services.async_register(
        DOMAIN, SERVICE_SEARCH, async_search, schema=SEARCH_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(DOMAIN, SERVICE_SYNC, async_sync)
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_FROM_IMAGE,
        async_add_from_image,
        schema=ADD_FROM_IMAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ParagonyConfigEntry) -> bool:
    db = await _async_get_db(hass)
    session = async_get_clientsession(hass)
    chain = entry.data[CONF_CHAIN]
    if chain == CHAIN_ZABKA:
        provider = ZabkaProvider(session, entry.data[CONF_REFRESH_TOKEN])
    elif chain == CHAIN_LIDL:
        provider = LidlProvider(session, entry.data[CONF_REFRESH_TOKEN])
    elif chain == CHAIN_PHOTO:
        provider = PhotoProvider()
    else:
        raise ValueError(f"Nieobsługiwana sieć: {chain}")

    coordinator = ParagonyCoordinator(hass, entry, provider, db)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def _restock_tick(_now) -> None:
        entry.async_create_background_task(hass, coordinator.async_update_local(), "paragony_restock")

    entry.async_on_unload(async_track_time_interval(hass, _restock_tick, RESTOCK_INTERVAL))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ParagonyConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ParagonyConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    others = [e for e in hass.config_entries.async_loaded_entries(DOMAIN) if e.entry_id != entry.entry_id]
    if unloaded and not others:
        if db := hass.data.get(DOMAIN, {}).pop(DATA_DB, None):
            await hass.async_add_executor_job(db.close)
    return unloaded
