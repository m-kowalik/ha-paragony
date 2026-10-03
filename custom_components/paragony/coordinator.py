"""Okresowa synchronizacja paragonów do bazy."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_REFRESH_TOKEN,
    CONF_TODO_ENTITY,
    DOMAIN,
    EVENT_NEW_RECEIPT,
    EVENT_RESTOCK_ADDED,
    RECENT_DAYS,
    UPDATE_INTERVAL,
)
from .db import ReceiptDB
from .models import Receipt
from .restock import evaluate
from .providers.base import ProviderAuthError, ProviderError, ReceiptProvider

_LOGGER = logging.getLogger(__name__)


def receipt_event_data(receipt: Receipt) -> dict:
    """Dane eventu paragony_new_receipt."""
    return {
        "chain": receipt.chain,
        "receipt_id": receipt.external_id,
        "purchased_at": dt_util.as_local(receipt.purchased_at).isoformat(),
        "store": receipt.store_name,
        "total": receipt.total / 100,
        "currency": receipt.currency,
        "items": [
            {"name": i.name, "quantity": i.quantity, "price": i.final_price / 100}
            for i in receipt.items
            if i.kind == "product"
        ],
    }


class ParagonyCoordinator(DataUpdateCoordinator[dict]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, provider: ReceiptProvider, db: ReceiptDB) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{provider.chain}",
            update_interval=UPDATE_INTERVAL,
        )
        self.provider = provider
        self.db = db
        self._restock_lock = asyncio.Lock()

    async def _async_update_data(self) -> dict:
        chain = self.provider.chain
        try:
            known = await self.hass.async_add_executor_job(self.db.known_ids, chain)
            initial_import = not known
            new_ids = [rid for rid in await self.provider.async_list_receipt_ids() if rid not in known]
            for receipt_id in new_ids:
                receipt = await self.provider.async_get_receipt(receipt_id)
                inserted = await self.hass.async_add_executor_job(self.db.insert, receipt)
                # przy pierwszym imporcie całej historii nie zalewamy automatyzacji eventami
                if inserted and not initial_import:
                    self.hass.bus.async_fire(EVENT_NEW_RECEIPT, receipt_event_data(receipt))
            if new_ids:
                _LOGGER.info("%s: zapisano %d nowych paragonów", chain, len(new_ids))
        except ProviderAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ProviderError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            self._persist_refresh_token()

        return await self._async_local_data()

    async def _async_local_data(self) -> dict:
        month_start = dt_util.start_of_local_day().replace(day=1)
        data = await self.hass.async_add_executor_job(self.db.stats, self.provider.chain, month_start)
        data["restock"], data["recent"] = await self._async_restock()
        return data

    async def async_update_local(self) -> None:
        """Przelicza dane z bazy bez odpytywania API sieci (timer, zmiana interwału, paragon ze zdjęcia)."""
        if self.data is None:
            return
        # celowo bez async_set_updated_data — ta przesuwa termin kolejnej synchronizacji
        self.data = await self._async_local_data()
        self.async_update_listeners()

    async def _async_restock(self) -> tuple[dict[int, dict], list[dict]]:
        async with self._restock_lock:
            now = dt_util.now()
            products = await self.hass.async_add_executor_job(self.db.list_tracked, self.config_entry.entry_id)
            recent = await self.hass.async_add_executor_job(
                self.db.recent_products, 50, now - timedelta(days=RECENT_DAYS)
            )
            todo_entity = self.config_entry.options.get(CONF_TODO_ENTITY)
            restock: dict[int, dict] = {}
            for product in products:
                state = evaluate(product, now)
                if state.should_add and todo_entity and await self._async_add_to_list(todo_entity, product, now):
                    product["last_added_at"] = now.isoformat()
                restock[product["id"]] = {**product, "due_date": state.due_date, "days_left": state.days_left}
            return restock, recent

    async def _async_add_to_list(self, todo_entity: str, product: dict, now: datetime) -> bool:
        """Dodaje produkt do listy zakupów, chyba że już na niej czeka. Zwraca True, gdy cykl obsłużony."""
        name = product["name"]
        try:
            response = await self.hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": todo_entity, "status": ["needs_action"]},
                blocking=True,
                return_response=True,
            )
            items = (response or {}).get(todo_entity, {}).get("items", [])
            already_on_list = any(item.get("summary", "").casefold() == name.casefold() for item in items)
            if not already_on_list:
                last = dt_util.as_local(datetime.fromisoformat(product["last_purchased_at"]))
                await self.hass.services.async_call(
                    "todo",
                    "add_item",
                    {
                        "entity_id": todo_entity,
                        "item": name,
                        "description": f"Paragony: ostatnio kupione {last:%d.%m}, co {product['interval_days']} dni",
                    },
                    blocking=True,
                )
        except HomeAssistantError as err:
            _LOGGER.warning("Nie udało się dodać „%s” do %s: %s", name, todo_entity, err)
            return False
        await self.hass.async_add_executor_job(self.db.mark_added, product["id"], now)
        self.hass.bus.async_fire(
            EVENT_RESTOCK_ADDED,
            {"product": name, "todo_entity": todo_entity, "already_on_list": already_on_list},
        )
        _LOGGER.info("„%s” → %s (już na liście: %s)", name, todo_entity, already_on_list)
        return True

    def _persist_refresh_token(self) -> None:
        token = self.provider.refresh_token
        if token and token != self.config_entry.data.get(CONF_REFRESH_TOKEN):
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_REFRESH_TOKEN: token}
            )
