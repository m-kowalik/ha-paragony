"""Wspólna baza encji śledzonych produktów."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ParagonyCoordinator


class RestockEntity(CoordinatorEntity[ParagonyCoordinator]):
    _attr_has_entity_name = True
    _key: str

    def __init__(self, coordinator: ParagonyCoordinator, entry: ConfigEntry, product_id: int) -> None:
        super().__init__(coordinator)
        self._product_id = product_id
        name = coordinator.data["restock"][product_id]["name"]
        self._attr_unique_id = f"{entry.entry_id}_product_{product_id}_{self._key}"
        self._attr_translation_placeholders = {"product": name}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_restock")},
            name="Zakupy cykliczne",
            manufacturer="Paragony",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def product(self) -> dict | None:
        return self.coordinator.data.get("restock", {}).get(self._product_id)

    @property
    def available(self) -> bool:
        return super().available and self.product is not None
