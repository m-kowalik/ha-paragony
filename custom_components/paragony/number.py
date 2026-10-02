"""Co ile dni kupować śledzony produkt — edytowalne z dashboardu."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ParagonyConfigEntry
from .entity import RestockEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ParagonyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(IntervalNumber(coordinator, entry, pid) for pid in coordinator.data.get("restock", {}))


class IntervalNumber(RestockEntity, NumberEntity):
    _attr_translation_key = "restock_interval"
    _attr_native_min_value = 1
    _attr_native_max_value = 365
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _key = "interval"

    @property
    def native_value(self) -> int | None:
        return self.product["interval_days"] if self.product else None

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.async_add_executor_job(
            lambda: self.coordinator.db.update_tracked(self._product_id, interval_days=int(value))
        )
        await self.coordinator.async_update_restock()
