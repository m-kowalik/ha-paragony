from datetime import datetime
from zoneinfo import ZoneInfo

from paragony.providers.lidl import build_receipt, extract_code, login_url

WARSAW = ZoneInfo("Europe/Warsaw")


def _summary(receipt):
    return [(i.name, i.quantity, i.unit, i.unit_price, i.total_price, i.discount, i.kind) for i in receipt.items]


def test_html_receipt(lidl_fixture):
    receipt = build_receipt(lidl_fixture["html"])
    assert receipt.chain == "lidl"
    assert receipt.external_id == "240000000020260115000001"
    # data w API to czas lokalny sklepu
    assert receipt.purchased_at == datetime(2026, 1, 15, 12, 0, tzinfo=WARSAW)
    assert receipt.total == 2799
    assert receipt.store_name == "Lidl Warszawa, ul. Testowa 1"
    assert receipt.store_address == "ul. Testowa 1, 00-001 Warszawa"
    assert _summary(receipt) == [
        ("Woda mineralna 1,5l", 6, "szt.", 200, 1200, 300, "product"),
        ("Filet z kurczaka", 0.5, "kg", 2400, 1200, 0, "product"),
        ("Śmietanka 30%", 1, "szt.", 399, 399, 0, "product"),
        ("Kaucja PET", 6, "szt.", 50, 300, 0, "deposit"),
    ]
    # suma pozycji po rabatach + kaucje = kwota zapłacona
    assert sum(i.final_price for i in receipt.items) == receipt.total


def test_native_receipt(lidl_fixture):
    receipt = build_receipt(lidl_fixture["native"])
    assert receipt.purchased_at == datetime(2025, 6, 10, 18, 30, tzinfo=WARSAW)
    assert receipt.total == 1447
    assert _summary(receipt) == [
        ("Brokuły 500g", 2, "szt.", 699, 1398, 700, "product"),
        ("Marchew luz", 0.25, "kg", 399, 100, 0, "product"),
        ("Napój cola 2l.", 1, "szt.", 599, 599, 0, "product"),
        ("Kaucja", 1, "szt.", 50, 50, 0, "deposit"),
    ]
    assert sum(i.final_price for i in receipt.items) == receipt.total
    assert "operatorId" not in receipt.raw


def test_extract_code_and_login_url():
    url = "com.lidlplus.app://callback?code=ABC123&scope=openid%20profile&session_state=x.y"
    assert extract_code(url) == "ABC123"
    assert extract_code("  ABC123 ") == "ABC123"
    link = login_url("challenge")
    assert link.startswith("https://accounts.lidl.com/connect/authorize?")
    assert "code_challenge=challenge" in link and "Country=PL" in link
