from datetime import datetime, timezone

from paragony.jpk import clean_name, parse_items
from paragony.providers.zabka import build_receipt, normalize_phone


def test_clean_name():
    assert clean_name("LAYS MAX 120g-C") == "LAYS MAX 120g"
    assert clean_name("MLEKO 3,2% 1l-C") == "MLEKO 3,2% 1l"
    assert clean_name("KAUCJA ZA PUSZKE") == "KAUCJA ZA PUSZKE"


def test_normalize_phone():
    assert normalize_phone("+48 600 100 200") == "600100200"
    assert normalize_phone("600-100-200") == "600100200"


def test_build_receipt(zabka_fixture):
    receipt = build_receipt(zabka_fixture["eprint"], zabka_fixture["receipt"])
    assert receipt.chain == "zabka"
    assert receipt.external_id == zabka_fixture["eprint"]["id"]
    assert receipt.purchased_at == datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert receipt.total == 3196
    assert receipt.store_name == 'Sklep "Żabka" Z0000'
    assert receipt.store_address == "ul. Testowa 1, 00-001 Warszawa"
    assert "header" not in receipt.raw

    products = [i for i in receipt.items if i.kind == "product"]
    deposits = [i for i in receipt.items if i.kind == "deposit"]
    assert [p.name for p in products] == [
        "MLEKO 3,2% 1l", "CHLEB ZYTNI 500g", "WODA MIN 1,5l", "CHIPSY SOLONE 140g", "CHIPSY SOLONE 140g",
    ]
    water = products[2]
    assert (water.quantity, water.unit_price, water.total_price, water.discount, water.final_price) == (2, 300, 600, 100, 500)
    # suma pozycji po rabatach + kaucje = kwota zapłacona
    assert sum(i.final_price for i in receipt.items) == receipt.total
    assert sum(d.final_price for d in deposits) == 100


def test_storno_and_surcharge_skipped_or_applied():
    paragon = {
        "pozycja": [
            {"towar": {"nazwa": "A-A", "cena": 100, "brutto": 100, "ilosc": "1", "jm": "szt.", "oper": False}},
            {"towar": {"nazwa": "B-A", "cena": 200, "brutto": 200, "ilosc": "1", "jm": "szt.", "oper": True}},
            {"rabat": {"nazwa": "B-A", "wart": -50, "oper": True}},
            {"rabat": {"nazwa": "A-A", "wart": 10, "oper": False}},
            {"towar": {"nazwa": "SER-B", "cena": 3999, "brutto": 1234, "ilosc": "0,309", "jm": "kg", "oper": False}},
        ]
    }
    items = parse_items(paragon)
    assert [i.name for i in items] == ["A", "SER"]
    assert items[0].final_price == 110
    assert items[1].quantity == 0.309
