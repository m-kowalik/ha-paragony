"""Kiedy kupić produkt ponownie. Moduł niezależny od Home Assistanta."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class RestockState:
    due_date: date | None
    days_left: int | None
    should_add: bool


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def evaluate(product: dict, now: datetime) -> RestockState:
    """Termin ponownego zakupu i decyzja, czy dodać produkt do listy zakupów.

    Produkt trafia na listę raz na cykl: po dodaniu czekamy na nowy paragon z tym produktem.
    `now` musi mieć strefę czasową; termin liczony jest w strefie `now`.
    """
    last_purchase = _parse(product.get("last_purchased_at"))
    if last_purchase is None:
        return RestockState(None, None, False)
    due = last_purchase + timedelta(days=int(product["interval_days"]))
    last_added = _parse(product.get("last_added_at"))
    should_add = now >= due and (last_added is None or last_added < last_purchase)
    due_date = due.astimezone(now.tzinfo).date()
    return RestockState(due_date, (due_date - now.date()).days, should_add)


def bring_name(item_name: str) -> str:
    """Domyślna nazwa na liście zakupów: „NAPOJ DZIK 500ml” → „Napoj dzik 500ml”."""
    lowered = item_name.strip().lower()
    return lowered[:1].upper() + lowered[1:]
