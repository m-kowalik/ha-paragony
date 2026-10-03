"""Baza SQLite z paragonami. Metody są synchroniczne — w HA wołać przez executor."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from statistics import median

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
CREATE TABLE IF NOT EXISTS tracked_products (
    id INTEGER PRIMARY KEY,
    entry_id TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    interval_days INTEGER NOT NULL,
    last_added_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked_product_names (
    product_id INTEGER NOT NULL REFERENCES tracked_products(id) ON DELETE CASCADE,
    item_name TEXT NOT NULL,
    PRIMARY KEY (product_id, item_name)
);
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

    def find_similar(self, purchased_at: datetime, total: int, minutes: int = 10) -> dict | None:
        """Paragon z tą samą sumą w pobliżu tej godziny (np. ze zdjęcia, gdy jest już e-paragon)."""
        delta = timedelta(minutes=minutes)
        with self._lock:
            row = self._conn.execute(
                """SELECT chain, external_id, purchased_at, store_name FROM receipts
                   WHERE total = ? AND purchased_at BETWEEN ? AND ? LIMIT 1""",
                (total, _iso_utc(purchased_at - delta), _iso_utc(purchased_at + delta)),
            ).fetchone()
        return dict(row) if row is not None else None

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

    # --- produkty ---------------------------------------------------------------

    def recent_products(self, limit: int = 200, since: datetime | None = None) -> list[dict]:
        """Różne produkty z paragonów: ostatni zakup i liczba zakupów, od najnowszych."""
        sql = """SELECT i.name, MAX(r.purchased_at) AS last_purchased_at, COUNT(DISTINCT r.id) AS times
                 FROM items i JOIN receipts r ON r.id = i.receipt_id
                 WHERE i.kind = 'product' {since}
                 GROUP BY i.name ORDER BY last_purchased_at DESC LIMIT ?"""
        args: list = []
        if since:
            sql = sql.format(since="AND r.purchased_at >= ?")
            args.append(_iso_utc(since))
        else:
            sql = sql.format(since="")
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, (*args, limit))]

    def suggest_interval(self, names: list[str]) -> int | None:
        """Mediana odstępów (w dniach) między kolejnymi dniami zakupu produktu."""
        if not names:
            return None
        marks = ",".join("?" * len(names))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT DISTINCT substr(r.purchased_at, 1, 10) AS day
                    FROM items i JOIN receipts r ON r.id = i.receipt_id
                    WHERE i.name IN ({marks}) ORDER BY day""",
                names,
            ).fetchall()
        days = [datetime.fromisoformat(row[0]) for row in rows]
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        return max(1, round(median(gaps))) if gaps else None

    def add_tracked(self, entry_id: str, name: str, interval_days: int, item_names: list[str]) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO tracked_products (entry_id, name, interval_days, created_at) VALUES (?, ?, ?, ?)",
                (entry_id, name, interval_days, _iso_utc(datetime.now(timezone.utc))),
            )
            self._conn.executemany(
                "INSERT INTO tracked_product_names (product_id, item_name) VALUES (?, ?)",
                [(cur.lastrowid, item) for item in dict.fromkeys(item_names)],
            )
            return cur.lastrowid

    def update_tracked(
        self,
        product_id: int,
        *,
        name: str | None = None,
        interval_days: int | None = None,
        item_names: list[str] | None = None,
    ) -> None:
        with self._lock, self._conn:
            if name is not None:
                self._conn.execute("UPDATE tracked_products SET name = ? WHERE id = ?", (name, product_id))
            if interval_days is not None:
                self._conn.execute(
                    "UPDATE tracked_products SET interval_days = ? WHERE id = ?", (interval_days, product_id)
                )
            if item_names is not None:
                self._conn.execute("DELETE FROM tracked_product_names WHERE product_id = ?", (product_id,))
                self._conn.executemany(
                    "INSERT INTO tracked_product_names (product_id, item_name) VALUES (?, ?)",
                    [(product_id, item) for item in dict.fromkeys(item_names)],
                )

    def delete_tracked(self, product_ids: list[int]) -> None:
        with self._lock, self._conn:
            self._conn.executemany("DELETE FROM tracked_products WHERE id = ?", [(pid,) for pid in product_ids])

    def mark_added(self, product_id: int, when: datetime) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tracked_products SET last_added_at = ? WHERE id = ?", (_iso_utc(when), product_id)
            )

    def list_tracked(self, entry_id: str) -> list[dict]:
        """Śledzone produkty z nazwami z paragonów i datą ostatniego zakupu (ze wszystkich sieci)."""
        with self._lock:
            products = [
                dict(row)
                for row in self._conn.execute(
                    """SELECT p.*, (
                           SELECT MAX(r.purchased_at) FROM items i
                           JOIN receipts r ON r.id = i.receipt_id
                           JOIN tracked_product_names n ON n.item_name = i.name
                           WHERE n.product_id = p.id
                       ) AS last_purchased_at
                       FROM tracked_products p WHERE p.entry_id = ? ORDER BY p.name""",
                    (entry_id,),
                )
            ]
            for product in products:
                product["item_names"] = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT item_name FROM tracked_product_names WHERE product_id = ? ORDER BY item_name",
                        (product["id"],),
                    )
                ]
        return products
