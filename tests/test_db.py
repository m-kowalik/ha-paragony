from datetime import datetime, timezone

from paragony.db import ReceiptDB
from paragony.providers.zabka import build_receipt


def test_insert_dedup_search_stats(tmp_path, zabka_fixture):
    db = ReceiptDB(str(tmp_path / "p.db"))
    receipt = build_receipt(zabka_fixture["eprint"], zabka_fixture["receipt"])

    assert db.insert(receipt) is True
    assert db.insert(receipt) is False
    assert db.known_ids("zabka") == {receipt.external_id}

    rows = db.search(product="chipsy")
    assert len(rows) == 2 and rows[0]["final_price"] == 699
    assert db.search(product="kaucja") == []
    assert len(db.search(product="kaucja", include_deposits=True)) == 1
    assert db.search(start=datetime(2026, 1, 16, tzinfo=timezone.utc)) == []
    assert len(db.search(end=datetime(2026, 1, 16, tzinfo=timezone.utc))) == 5

    stats = db.stats("zabka", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert stats["count"] == 1
    assert stats["month_total"] == 3196
    assert stats["last"]["total"] == 3196
    assert len(stats["last"]["items"]) == 6
    db.close()
