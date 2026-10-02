"""Baza SQLite z paragonami. Metody są synchroniczne — w HA wołać przez executor."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .models import Receipt

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    external_id TEXT NOT NULL,
    purchased_at TEXT NOT NULL,
    store_name TEXT,
    store_address TEXT,
    total INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'PLN',
    raw_json TEXT,
    imported_at TEXT NOT NULL,
    UNIQUE (chain, external_id)
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    raw_name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'product',
    quantity REAL NOT NULL,
    unit TEXT,
    unit_price INTEGER NOT NULL,
    total_price INTEGER NOT NULL,
    discount INTEGER NOT NULL DEFAULT 0,
    final_price INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_purchased_at ON receipts(purchased_at);
CREATE INDEX IF NOT EXISTS idx_items_receipt ON items(receipt_id);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
"""


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _casefold(value: str | None) -> str | None:
    return value.casefold() if value is not None else None


class ReceiptDB:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("casefold", 1, _casefold, deterministic=True)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def known_ids(self, chain: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT external_id FROM receipts WHERE chain = ?", (chain,))
            return {row[0] for row in rows}

    def insert(self, receipt: Receipt) -> bool:
        """Zapisuje paragon z pozycjami. Zwraca False, jeśli już istniał."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO receipts
                   (chain, external_id, purchased_at, store_name, store_address, total, currency, raw_json, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.chain,
                    receipt.external_id,
                    _iso_utc(receipt.purchased_at),
                    receipt.store_name,
                    receipt.store_address,
                    receipt.total,
                    receipt.currency,
                    json.dumps(receipt.raw, ensure_ascii=False) if receipt.raw is not None else None,
                    _iso_utc(datetime.now(timezone.utc)),
                ),
            )
            if cur.rowcount == 0:
                return False
            self._conn.executemany(
                """INSERT INTO items
                   (receipt_id, position, name, raw_name, kind, quantity, unit, unit_price, total_price, discount, final_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        cur.lastrowid, pos, item.name, item.raw_name, item.kind, item.quantity, item.unit,
                        item.unit_price, item.total_price, item.discount, item.final_price,
                    )
                    for pos, item in enumerate(receipt.items)
                ],
            )
            return True

    def search(
        self,
        product: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        chain: str | None = None,
        include_deposits: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """Pozycje paragonów od najnowszych. Nazwa dopasowywana fragmentem, bez wielkości liter."""
        where, args = [], []
        if product:
            where.append("casefold(i.name) LIKE ?")
            args.append(f"%{product.casefold()}%")
        if start:
            where.append("r.purchased_at >= ?")
            args.append(_iso_utc(start))
        if end:
            where.append("r.purchased_at < ?")
            args.append(_iso_utc(end))
        if chain:
            where.append("r.chain = ?")
            args.append(chain)
        if not include_deposits:
            where.append("i.kind = 'product'")
        sql = f"""SELECT r.purchased_at, r.chain, r.store_name, r.store_address, r.external_id,
                         i.name, i.quantity, i.unit, i.unit_price, i.final_price, i.discount
                  FROM items i JOIN receipts r ON r.id = i.receipt_id
                  {'WHERE ' + ' AND '.join(where) if where else ''}
                  ORDER BY r.purchased_at DESC, i.position
                  LIMIT ?"""
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, (*args, limit))]

    def stats(self, chain: str, month_start: datetime) -> dict:
        """Dane dla sensorów: ostatni paragon, liczba paragonów, wydatki od początku miesiąca."""
        with self._lock:
            last = self._conn.execute(
                "SELECT * FROM receipts WHERE chain = ? ORDER BY purchased_at DESC LIMIT 1", (chain,)
            ).fetchone()
            count, month_total, month_count = self._conn.execute(
                """SELECT COUNT(*),
                          COALESCE(SUM(CASE WHEN purchased_at >= ? THEN total END), 0),
                          COUNT(CASE WHEN purchased_at >= ? THEN 1 END)
                   FROM receipts WHERE chain = ?""",
                (_iso_utc(month_start), _iso_utc(month_start), chain),
            ).fetchone()
            last_items = []
            if last is not None:
                last_items = [
                    dict(row)
                    for row in self._conn.execute(
                        """SELECT name, kind, quantity, unit, final_price FROM items
                           WHERE receipt_id = ? ORDER BY position""",
                        (last["id"],),
                    )
                ]
        return {
            "count": count,
            "month_total": month_total,
            "month_count": month_count,
            "last": (
                {
                    "external_id": last["external_id"],
                    "purchased_at": last["purchased_at"],
                    "store_name": last["store_name"],
                    "store_address": last["store_address"],
                    "total": last["total"],
                    "currency": last["currency"],
                    "items": last_items,
                }
                if last is not None
                else None
            ),
        }
