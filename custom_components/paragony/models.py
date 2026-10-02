"""Wspólny model paragonu dla wszystkich sieci. Kwoty w groszach."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ReceiptItem:
    name: str
    raw_name: str
    quantity: float
    unit: str | None
    unit_price: int
    total_price: int
    discount: int = 0
    kind: str = "product"  # product | deposit

    @property
    def final_price(self) -> int:
        return self.total_price - self.discount


@dataclass
class Receipt:
    chain: str
    external_id: str
    purchased_at: datetime  # UTC, z informacją o strefie
    total: int
    currency: str = "PLN"
    store_name: str | None = None
    store_address: str | None = None
    items: list[ReceiptItem] = field(default_factory=list)
    raw: dict | None = None
