"""Paragon ze zdjęcia: odpowiedź modelu → Receipt (dane syntetyczne)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from paragony.photo import PhotoReceiptError, build_receipt, detect_chain

WARSAW = ZoneInfo("Europe/Warsaw")

RESPONSE = """```json
{
  "store_name": "Jeronimo Martins Polska S.A. Biedronka nr 0000",
  "store_address": "ul. Przykładowa 1, 00-001 Warszawa",
  "nip": "0000000000",
  "purchased_at": "2026-09-30 18:42",
  "receipt_number": "12345",
  "total": "22,47",
  "currency": "PLN",
  "items": [
    {"name": "MLEKO 3,2% 1L-C", "quantity": 2, "unit": "szt.", "unit_price": 3.49, "total_price": 6.98, "discount": 0, "kind": "product"},
    {"name": "BANANY LUZ", "quantity": "0,750", "unit": "kg", "unit_price": "5,99", "total_price": 4.49, "discount": -1.00, "kind": "product"},
    {"name": "PIWO 0,5L BUT", "quantity": 1, "unit": "szt.", "unit_price": 4.50, "total_price": 4.50, "discount": 0, "kind": "product"},
    {"name": "Kaucja butelka", "quantity": 1, "unit": "szt.", "unit_price": 0.50, "total_price": 0.50, "kind": "deposit"},
    {"name": "CHLEB", "quantity": 1, "unit_price": 7.00, "total_price": 7.00}
  ]
}
```"""


def test_build_receipt_from_model_response() -> None:
    result = build_receipt(RESPONSE, WARSAW, "media-source://media_source/local/paragon.jpg")
    receipt = result.receipt

    assert receipt.chain == "biedronka"
    assert receipt.external_id.startswith("photo-")
    assert receipt.purchased_at == datetime(2026, 9, 30, 16, 42, tzinfo=timezone.utc)
    assert receipt.total == 2247
    assert result.items_total == 2247 and result.mismatch == 0

    milk, bananas, _, deposit, bread = receipt.items
    assert (milk.name, milk.raw_name, milk.quantity, milk.unit_price) == ("MLEKO 3,2% 1L", "MLEKO 3,2% 1L-C", 2.0, 349)
    # rabat zawsze dodatni, ilość z przecinkiem
    assert (bananas.quantity, bananas.discount, bananas.final_price) == (0.75, 100, 349)
    assert deposit.kind == "deposit"
    assert bread.quantity == 1.0 and bread.unit is None

    assert receipt.raw["source"] == "photo"
    assert receipt.raw["media_content_id"].endswith("paragon.jpg")
    assert "items" not in receipt.raw


def test_same_photo_gives_same_id() -> None:
    first = build_receipt(RESPONSE, WARSAW).receipt
    second = build_receipt(RESPONSE, WARSAW).receipt
    assert first.external_id == second.external_id


def test_mismatch_reported() -> None:
    result = build_receipt(
        {"store_name": "Sklep", "purchased_at": "2026-09-30", "total": 10, "items": [{"name": "X", "total_price": 9.5}]},
        WARSAW,
    )
    assert result.receipt.chain == "inne"
    assert result.mismatch == 50


@pytest.mark.parametrize(
    ("name", "chain"),
    [
        ("LIDL sp. z o.o. sp. k.", "lidl"),
        ("Sklep Żabka Z1234", "zabka"),
        ("Kaufland Polska Markety", "kaufland"),
        ("Rossmann SDP", "rossmann"),
        ("Dinozaur zabawki", "inne"),  # „dino” tylko jako całe słowo
        (None, "inne"),
    ],
)
def test_detect_chain(name, chain) -> None:
    assert detect_chain(name) == chain


@pytest.mark.parametrize(
    "data",
    [
        "Nie widzę paragonu",
        '{"error": "To zdjęcie kota"}',
        {"store_name": "Sklep", "purchased_at": "2026-09-30", "total": 1, "items": []},
        {"store_name": "Sklep", "purchased_at": "wczoraj", "total": 1, "items": [{"name": "X", "total_price": 1}]},
    ],
)
def test_invalid_responses(data) -> None:
    with pytest.raises(PhotoReceiptError):
        build_receipt(data, WARSAW)
