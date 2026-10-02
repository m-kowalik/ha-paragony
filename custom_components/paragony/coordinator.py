"""Okresowa synchronizacja paragonów do bazy."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_REFRESH_TOKEN, DOMAIN, EVENT_NEW_RECEIPT, UPDATE_INTERVAL
from .db import ReceiptDB
from .providers.base import ProviderAuthError, ProviderError, ReceiptProvider

_LOGGER = logging.getLogger(__name__)


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
                    self.hass.bus.async_fire(
                        EVENT_NEW_RECEIPT,
                        {
                            "chain": chain,
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
                        },
                    )
            if new_ids:
                _LOGGER.info("%s: zapisano %d nowych paragonów", chain, len(new_ids))
        except ProviderAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ProviderError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            self._persist_refresh_token()

        month_start = dt_util.start_of_local_day().replace(day=1)
        return await self.hass.async_add_executor_job(self.db.stats, chain, month_start)

    def _persist_refresh_token(self) -> None:
        token = self.provider.refresh_token
        if token and token != self.config_entry.data.get(CONF_REFRESH_TOKEN):
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_REFRESH_TOKEN: token}
            )
