"""Sensory podsumowujące zakupy."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ParagonyConfigEntry
from .const import CHAIN_NAMES, CONF_CHAIN, DOMAIN
from .coordinator import ParagonyCoordinator
from .entity import RestockEntity


def _last(data: dict) -> dict | None:
    return data.get("last")


def _last_attrs(data: dict) -> dict:
    last = _last(data)
    if last is None:
        return {}
    return {
        "purchased_at": last["purchased_at"],
        "chain": last["chain"],
        "store": last["store_name"],
        "address": last["store_address"],
        "items": [
            {"name": i["name"], "quantity": i["quantity"], "price": i["final_price"] / 100}
            for i in last["items"]
            if i["kind"] == "product"
        ],
    }


@dataclass(frozen=True, kw_only=True)
class ParagonySensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], float | int | datetime | None]
    attrs_fn: Callable[[dict], dict] = lambda data: {}


SENSORS: tuple[ParagonySensorDescription, ...] = (
    ParagonySensorDescription(
        key="last_purchase",
        translation_key="last_purchase",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="PLN",
        suggested_display_precision=2,
        value_fn=lambda d: _last(d)["total"] / 100 if _last(d) else None,
        attrs_fn=_last_attrs,
    ),
    ParagonySensorDescription(
        key="last_purchase_time",
        translation_key="last_purchase_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: datetime.fromisoformat(_last(d)["purchased_at"]) if _last(d) else None,
    ),
    ParagonySensorDescription(
        key="month_spending",
        translation_key="month_spending",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="PLN",
        suggested_display_precision=2,
        value_fn=lambda d: d["month_total"] / 100,
        attrs_fn=lambda d: {"receipts": d["month_count"]},
    ),
    ParagonySensorDescription(
        key="recent_products",
        translation_key="recent_products",
        value_fn=lambda d: len(d.get("recent", [])),
        attrs_fn=lambda d: {
            "products": [
                {
                    "name": p["name"],
                    "last_purchased_at": p["last_purchased_at"],
                    "times": p["times"],
                }
                for p in d.get("recent", [])
            ]
        },
    ),
    ParagonySensorDescription(
        key="receipt_count",
        translation_key="receipt_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d["count"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ParagonyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [ParagonySensor(coordinator, entry, desc) for desc in SENSORS]
    entities += [RestockSensor(coordinator, entry, pid) for pid in coordinator.data.get("restock", {})]
    async_add_entities(entities)


class ParagonySensor(CoordinatorEntity[ParagonyCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: ParagonySensorDescription

    def __init__(
        self, coordinator: ParagonyCoordinator, entry: ParagonyConfigEntry, description: ParagonySensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        chain = entry.data[CONF_CHAIN]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name=CHAIN_NAMES.get(chain, chain),
            manufacturer="Paragony",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict:
        return self.entity_description.attrs_fn(self.coordinator.data)


class RestockSensor(RestockEntity, SensorEntity):
    """Data, kiedy kupić produkt ponownie."""

    _attr_device_class = SensorDeviceClass.DATE
    _attr_translation_key = "restock_due"
    _key = "due"

    @property
    def native_value(self):
        return self.product["due_date"] if self.product else None

    @property
    def extra_state_attributes(self) -> dict:
        product = self.product
        if product is None:
            return {}
        return {
            "last_purchased_at": product["last_purchased_at"],
            "days_left": product["days_left"],
            "interval_days": product["interval_days"],
            "receipt_names": product["item_names"],
            "added_to_list_at": product["last_added_at"],
        }
