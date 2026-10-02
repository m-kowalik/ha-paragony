"""Paragony — e-paragony z aplikacji sieci handlowych w Home Assistant."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CHAIN_NAMES,
    CHAIN_ZABKA,
    CONF_CHAIN,
    CONF_REFRESH_TOKEN,
    DB_FILENAME,
    DOMAIN,
    SERVICE_SEARCH,
    SERVICE_SYNC,
)
from .coordinator import ParagonyCoordinator
from .db import ReceiptDB
from .providers.zabka import ZabkaProvider

PLATFORMS = [Platform.SENSOR]
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

    hass.services.async_register(
        DOMAIN, SERVICE_SEARCH, async_search, schema=SEARCH_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(DOMAIN, SERVICE_SYNC, async_sync)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ParagonyConfigEntry) -> bool:
    db = await _async_get_db(hass)
    session = async_get_clientsession(hass)
    chain = entry.data[CONF_CHAIN]
    if chain == CHAIN_ZABKA:
        provider = ZabkaProvider(session, entry.data[CONF_REFRESH_TOKEN])
    else:
        raise ValueError(f"Nieobsługiwana sieć: {chain}")

    coordinator = ParagonyCoordinator(hass, entry, provider, db)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ParagonyConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    others = [e for e in hass.config_entries.async_loaded_entries(DOMAIN) if e.entry_id != entry.entry_id]
    if unloaded and not others:
        if db := hass.data.get(DOMAIN, {}).pop(DATA_DB, None):
            await hass.async_add_executor_job(db.close)
    return unloaded
