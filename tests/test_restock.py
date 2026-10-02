import copy
from datetime import date, datetime, timedelta, timezone

from paragony.db import ReceiptDB
from paragony.providers.zabka import build_receipt
from paragony.restock import bring_name, evaluate

UTC = timezone.utc


def product(last_purchase=None, last_added=None, interval=7):
    return {
        "interval_days": interval,
        "last_purchased_at": last_purchase.isoformat() if last_purchase else None,
        "last_added_at": last_added.isoformat() if last_added else None,
    }


def test_evaluate_before_and_after_due():
    bought = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
    before = evaluate(product(bought), datetime(2026, 10, 5, tzinfo=UTC))
    assert (before.due_date, before.days_left, before.should_add) == (date(2026, 10, 8), 3, False)
    after = evaluate(product(bought), datetime(2026, 10, 8, 19, 0, tzinfo=UTC))
    assert after.should_add and after.days_left == 0


def test_evaluate_only_once_per_cycle():
    bought = datetime(2026, 10, 1, tzinfo=UTC)
    added = datetime(2026, 10, 9, tzinfo=UTC)
    assert not evaluate(product(bought, added), datetime(2026, 10, 30, tzinfo=UTC)).should_add
    # nowy zakup po dodaniu do listy otwiera kolejny cykl
    rebought = datetime(2026, 10, 10, tzinfo=UTC)
    assert evaluate(product(rebought, added), datetime(2026, 10, 18, tzinfo=UTC)).should_add


def test_evaluate_without_purchase():
    state = evaluate(product(None), datetime(2026, 10, 1, tzinfo=UTC))
    assert state.due_date is None and not state.should_add


def test_bring_name():
    assert bring_name("CHIPSY SOLONE 140g") == "Chipsy solone 140g"


def _db_with_history(tmp_path, zabka_fixture, days):
    db = ReceiptDB(str(tmp_path / "p.db"))
    for n, day in enumerate(days):
        receipt = build_receipt(zabka_fixture["eprint"], zabka_fixture["receipt"])
        receipt.external_id, receipt.purchased_at = f"r{n}", day
        db.insert(receipt)
    return db


def test_tracked_crud_recent_and_suggestion(tmp_path, zabka_fixture):
    base = datetime(2026, 1, 1, 12, tzinfo=UTC)
    db = _db_with_history(tmp_path, zabka_fixture, [base, base + timedelta(days=4), base + timedelta(days=10)])

    recent = db.recent_products()
    assert recent[0]["times"] == 3
    assert {r["name"] for r in recent} == {"MLEKO 3,2% 1l", "CHLEB ZYTNI 500g", "WODA MIN 1,5l", "CHIPSY SOLONE 140g"}
    assert db.recent_products(since=base + timedelta(days=5)) and all(
        r["times"] == 1 for r in db.recent_products(since=base + timedelta(days=5))
    )
    assert db.suggest_interval(["MLEKO 3,2% 1l"]) == 5  # odstępy 4 i 6 dni → mediana 5
    assert db.suggest_interval(["NIEZNANY"]) is None

    pid = db.add_tracked("entry", "Mleko", 5, ["MLEKO 3,2% 1l", "MLEKO 3,2% 1l"])
    tracked = db.list_tracked("entry")
    assert tracked[0]["item_names"] == ["MLEKO 3,2% 1l"]
    assert tracked[0]["last_purchased_at"] == (base + timedelta(days=10)).isoformat(timespec="seconds")

    db.update_tracked(pid, name="Mleko 3,2%", interval_days=3, item_names=["MLEKO 3,2% 1l", "CHLEB ZYTNI 500g"])
    db.mark_added(pid, base + timedelta(days=20))
    tracked = db.list_tracked("entry")[0]
    assert (tracked["name"], tracked["interval_days"], len(tracked["item_names"])) == ("Mleko 3,2%", 3, 2)
    assert tracked["last_added_at"].startswith("2026-01-21")
    assert db.list_tracked("other") == []

    db.delete_tracked([pid])
    assert db.list_tracked("entry") == []
    db.close()
