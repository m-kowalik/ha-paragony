"""Paragon ze zdjęcia: odpowiedź modelu → Receipt (dane syntetyczne)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from paragony.photo import PhotoReceiptError, build_receipt, detect_chain

WARSAW = ZoneInfo("Europe/Warsaw")

RESPONSE = """```json
{"store":"Biedronka 0000","address":"ul. Przykładowa 1, 00-001 Warszawa","nip":"0000000000",\
"date":"2026-09-30 18:42","number":"12345","total":"22,47","items":[\
["MLEKO 3,2% 1L-C",2,6.98,0,"p"],\
["BANANY LUZ","0,750",4.49,-1.00,"p"],\
["PIWO 0,5L BUT",1,4.50,0,"p"],\
["But Plastik kaucja",1,0.50,0,"k"],\
{"name":"CHLEB","total_price":7.00}]}
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
    assert (bananas.unit, bread.unit, bread.quantity, bread.kind) == ("kg", "szt.", 1.0, "product")

    assert receipt.raw["source"] == "photo"
    assert receipt.raw["media_content_id"].endswith("paragon.jpg")
    assert "items" not in receipt.raw


def test_same_photo_gives_same_id() -> None:
    first = build_receipt(RESPONSE, WARSAW).receipt
    second = build_receipt(RESPONSE, WARSAW).receipt
    assert first.external_id == second.external_id


def test_mismatch_reported() -> None:
    result = build_receipt(
        {"store": "Sklep", "date": "2026-09-30", "total": 10, "items": [["X", 1, 9.5, 0, "p"]]},
        WARSAW,
    )
    assert result.receipt.chain == "inne"
    assert result.mismatch == 50


@pytest.mark.parametrize(
    ("name", "chain"),
    [
        ("Jeronimo Martins Polska S.A.", "biedronka"),
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
        {"store": "Sklep", "date": "2026-09-30", "total": 1, "items": []},
        {"store": "Sklep", "date": "wczoraj", "total": 1, "items": [["X", 1, 1, 0, "p"]]},
        {"store": "Sklep", "date": "2026-09-30", "total": 1, "items": ["X"]},
    ],
)
def test_invalid_responses(data) -> None:
    with pytest.raises(PhotoReceiptError):
        build_receipt(data, WARSAW)
