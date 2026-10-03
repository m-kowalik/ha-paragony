"""Paragon ze zdjęcia: polecenie dla AI Task i zamiana odpowiedzi na Receipt (czysta logika, bez HA).

Zdjęcie odczytuje model przez ``ai_task.generate_data``. Odpowiedź to JSON opisany w ``INSTRUCTIONS``.
Celowo nie używamy ``structure``: zagnieżdżona lista pozycji nie przechodzi przez selektory
we wszystkich dostawcach, a sam JSON w tekście działa wszędzie.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata

from .const import CHAIN_OTHER, PHOTO_CHAIN_ALIASES
from .jpk import clean_name
from .models import Receipt, ReceiptItem

INSTRUCTIONS = """\
Odczytaj polski paragon fiskalny ze zdjęcia. Odpowiedz wyłącznie obiektem JSON, bez komentarzy i bez bloku ```:
{
  "store_name": "nazwa sprzedawcy lub sklepu z nagłówka",
  "store_address": "ulica i numer, kod pocztowy miasto" albo null,
  "nip": "NIP sprzedawcy, same cyfry" albo null,
  "purchased_at": "RRRR-MM-DD GG:MM" (data i godzina sprzedaży z paragonu),
  "receipt_number": "numer wydruku / paragonu" albo null,
  "total": suma do zapłaty (SUMA PLN) jako liczba,
  "currency": "PLN",
  "items": [
    {
      "name": "nazwa towaru dokładnie jak na paragonie, bez litery stawki VAT",
      "quantity": ilość (liczba, np. 1 albo 0.456),
      "unit": "szt." albo "kg" albo null,
      "unit_price": cena jednostkowa,
      "total_price": wartość pozycji przed rabatem,
      "discount": suma rabatów do tej pozycji jako liczba dodatnia (0, gdy brak),
      "kind": "product" albo "deposit" (kaucja za opakowanie zwrotne)
    }
  ]
}
Zasady:
- Kwoty jako liczby z kropką dziesiętną (3.49), nie tekst.
- Rabat lub promocja wydrukowana pod pozycją należy do tej pozycji (pole discount), nie jest osobną pozycją.
- Pozycje anulowane (storno) pomiń.
- Kaucje i opakowania zwrotne podaj jako pozycje z "kind": "deposit".
- Nie dodawaj podsumowań VAT, płatności, reszty ani punktów lojalnościowych jako pozycji.
- Jeśli zdjęcie nie przedstawia paragonu, zwróć {"error": "krótki opis po polsku"}.
"""

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


class PhotoReceiptError(ValueError):
    """Odpowiedź modelu nie daje poprawnego paragonu."""


@dataclass
class PhotoResult:
    receipt: Receipt
    items_total: int  # suma pozycji po rabatach (w groszach)

    @property
    def mismatch(self) -> int:
        """Różnica między sumą z paragonu a sumą pozycji (w groszach)."""
        return self.receipt.total - self.items_total


def _ascii_fold(text: str) -> str:
    text = text.casefold().replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def detect_chain(*names: str | None) -> str:
    """Slug sieci na podstawie nazwy sprzedawcy (np. „Jeronimo Martins Polska” → biedronka)."""
    haystack = " ".join(_ascii_fold(name) for name in names if name)
    for chain, aliases in PHOTO_CHAIN_ALIASES.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", haystack) for alias in aliases):
            return chain
    return CHAIN_OTHER


def load_json(data) -> dict:
    """Odpowiedź AI Task: słownik albo tekst z JSON-em (czasem w bloku ```json)."""
    if isinstance(data, dict):
        return data
    if not isinstance(data, str) or not (match := _JSON_BLOCK.search(data)):
        raise PhotoReceiptError("Model nie zwrócił JSON-a z paragonem")
    try:
        result = json.loads(match.group(0))
    except json.JSONDecodeError as err:
        raise PhotoReceiptError(f"Niepoprawny JSON od modelu: {err}") from err
    if not isinstance(result, dict):
        raise PhotoReceiptError("Model nie zwrócił obiektu JSON")
    return result


def _grosze(value, field: str) -> int:
    """3.49 / „3,49” / „3,49 zł” → 349."""
    if value is None or value == "":
        return 0
    text = re.sub(r"[^\d,.\-]", "", str(value)).replace(",", ".")
    try:
        return int((Decimal(text) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation as err:
        raise PhotoReceiptError(f"Niepoprawna kwota w polu {field}: {value!r}") from err


def _quantity(value) -> float:
    if value in (None, ""):
        return 1.0
    try:
        return float(str(value).replace(",", "."))
    except ValueError as err:
        raise PhotoReceiptError(f"Niepoprawna ilość: {value!r}") from err


def _purchased_at(value, tz: tzinfo) -> datetime:
    text = str(value or "").strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise PhotoReceiptError(f"Niepoprawna data zakupu: {value!r}")


def _item(raw: dict) -> ReceiptItem:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise PhotoReceiptError("Pozycja bez nazwy")
    quantity = _quantity(raw.get("quantity"))
    total = _grosze(raw.get("total_price"), "total_price")
    unit_price = _grosze(raw.get("unit_price"), "unit_price") or round(total / quantity if quantity else total)
    unit = raw.get("unit") or None
    return ReceiptItem(
        name=clean_name(name),
        raw_name=name,
        quantity=quantity,
        unit=unit,
        unit_price=unit_price,
        total_price=total,
        # rabat zawsze dodatni, niezależnie od znaku zwróconego przez model
        discount=abs(_grosze(raw.get("discount"), "discount")),
        kind="deposit" if raw.get("kind") == "deposit" else "product",
    )


def external_id(chain: str, purchased_at: datetime, total: int, receipt_number: str | None) -> str:
    """Stały identyfikator: to samo zdjęcie dodane drugi raz nie zdubluje paragonu."""
    key = f"{chain}|{purchased_at:%Y-%m-%dT%H:%M}|{total}|{receipt_number or ''}"
    return "photo-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def build_receipt(data, tz: tzinfo, source: str | None = None) -> PhotoResult:
    """Zamienia odpowiedź modelu na Receipt. ``tz`` to strefa sklepu (data na paragonie jest lokalna)."""
    parsed = load_json(data)
    if parsed.get("error"):
        raise PhotoReceiptError(f"Model nie odczytał paragonu: {parsed['error']}")
    raw_items = parsed.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise PhotoReceiptError("Brak pozycji na paragonie")
    items = [_item(raw) for raw in raw_items if isinstance(raw, dict)]
    items_total = sum(item.final_price for item in items)
    total = _grosze(parsed.get("total"), "total") if parsed.get("total") not in (None, "") else items_total

    purchased_at = _purchased_at(parsed.get("purchased_at"), tz)
    store_name = (parsed.get("store_name") or "").strip() or None
    chain = detect_chain(store_name, parsed.get("chain"))
    receipt_number = str(parsed["receipt_number"]).strip() if parsed.get("receipt_number") else None
    raw = {key: value for key, value in parsed.items() if key != "items"}
    raw["source"] = "photo"
    if source:
        raw["media_content_id"] = source
    return PhotoResult(
        receipt=Receipt(
            chain=chain,
            external_id=external_id(chain, purchased_at, total, receipt_number),
            purchased_at=purchased_at,
            total=total,
            currency=(parsed.get("currency") or "PLN").upper(),
            store_name=store_name,
            store_address=(parsed.get("store_address") or "").strip() or None,
            items=items,
            raw=raw,
        ),
        items_total=items_total,
    )
