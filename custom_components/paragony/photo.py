"""Paragon ze zdjęcia: polecenie dla AI Task i zamiana odpowiedzi na Receipt (czysta logika, bez HA).

Zdjęcie odczytuje model przez ``ai_task.generate_data``. Odpowiedź to JSON opisany w ``INSTRUCTIONS``.
Celowo nie używamy ``structure``: zagnieżdżona lista pozycji nie przechodzi przez selektory
we wszystkich dostawcach, a sam JSON w tekście działa wszędzie. Pozycje są tablicami,
nie obiektami, bo długi paragon w pełnym formacie przekraczał limit tokenów odpowiedzi (Gemini: MAX_TOKENS).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata

from .const import CHAIN_OTHER, PHOTO_CHAIN_ALIASES, PHOTO_ID_PREFIX
from .jpk import clean_name
from .models import Receipt, ReceiptItem

INSTRUCTIONS = """\
Odczytaj polski paragon ze zdjęcia. Odpowiedz wyłącznie zwięzłym JSON-em w jednej linii, bez spacji \
między elementami, bez komentarzy i bez bloku ```. Format:
{"store":"nazwa sieci lub sklepu (z numerem sklepu, jeśli jest)",\
"address":"adres sklepu, a nie siedziby spółki (gdy brak, adres sprzedawcy)","nip":"same cyfry",\
"date":"RRRR-MM-DD GG:MM","number":"numer paragonu lub transakcji","total":173.26,\
"items":[["nazwa",ilość,wartość,rabat,"p"],...]}
Każda pozycja to tablica [nazwa, ilość, wartość, rabat, typ]:
- nazwa dokładnie jak na paragonie, bez litery stawki VAT,
- ilość jako liczba (1 albo 0.456),
- wartość pozycji przed rabatem (kolumna Wartość),
- rabat jako liczba dodatnia (0, gdy brak); rabat wydrukowany pod pozycją należy do niej,
- typ "p" dla towaru albo "k" dla kaucji / opakowania zwrotnego.
Zasady:
- Kwoty jako liczby z kropką dziesiętną. Brakujące pola tekstowe jako null.
- Pozycje anulowane (storno) pomiń. Nie dodawaj podsumowań VAT, płatności, reszty ani punktów.
- Jeśli zdjęcie nie przedstawia paragonu, zwróć {"error":"krótki opis po polsku"}.
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


def _item(raw) -> ReceiptItem:
    """Pozycja jako tablica [nazwa, ilość, wartość, rabat, typ] albo obiekt z tymi polami."""
    if isinstance(raw, (list, tuple)):
        raw = dict(zip(("name", "quantity", "total_price", "discount", "kind"), raw))
    elif not isinstance(raw, dict):
        raise PhotoReceiptError(f"Niepoprawna pozycja: {raw!r}")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise PhotoReceiptError("Pozycja bez nazwy")
    quantity = _quantity(raw.get("quantity"))
    total = _grosze(raw.get("total_price"), "total_price")
    unit_price = _grosze(raw.get("unit_price"), "unit_price") or round(total / quantity if quantity else total)
    return ReceiptItem(
        name=clean_name(name),
        raw_name=name,
        quantity=quantity,
        unit=raw.get("unit") or ("szt." if quantity.is_integer() else "kg"),
        unit_price=unit_price,
        total_price=total,
        # rabat zawsze dodatni, niezależnie od znaku zwróconego przez model
        discount=abs(_grosze(raw.get("discount"), "discount")),
        kind="deposit" if raw.get("kind") in ("k", "deposit") else "product",
    )


def external_id(chain: str, purchased_at: datetime, total: int, receipt_number: str | None) -> str:
    """Stały identyfikator: to samo zdjęcie dodane drugi raz nie zdubluje paragonu."""
    key = f"{chain}|{purchased_at:%Y-%m-%dT%H:%M}|{total}|{receipt_number or ''}"
    return PHOTO_ID_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:16]


def build_receipt(data, tz: tzinfo, source: str | None = None) -> PhotoResult:
    """Zamienia odpowiedź modelu na Receipt. ``tz`` to strefa sklepu (data na paragonie jest lokalna)."""
    parsed = load_json(data)
    if parsed.get("error"):
        raise PhotoReceiptError(f"Model nie odczytał paragonu: {parsed['error']}")
    raw_items = parsed.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise PhotoReceiptError("Brak pozycji na paragonie")
    items = [_item(raw) for raw in raw_items]
    items_total = sum(item.final_price for item in items)
    total = _grosze(parsed["total"], "total") if parsed.get("total") not in (None, "") else items_total

    purchased_at = _purchased_at(parsed.get("date"), tz)
    store_name = _text(parsed.get("store"))
    chain = detect_chain(store_name)
    receipt_number = _text(parsed.get("number"))
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
            store_name=store_name,
            store_address=_text(parsed.get("address")),
            items=items,
            raw=raw,
        ),
        items_total=items_total,
    )


def _text(value) -> str | None:
    return str(value).strip() or None if value is not None else None
