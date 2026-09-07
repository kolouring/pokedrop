"""SQLite persistence. Holds the discovered SKU catalog, last-seen availability
state, and user subscriptions."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any, Iterable

import aiosqlite

from .target_api import Product

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    tcin          TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    dpci          TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    image         TEXT NOT NULL DEFAULT '',
    price         REAL,
    street_date   TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    announced     INTEGER NOT NULL DEFAULT 0,
    street_warned TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS product_state (
    tcin        TEXT PRIMARY KEY,
    state_key   TEXT NOT NULL,
    ship_status TEXT NOT NULL,
    stores      TEXT NOT NULL DEFAULT '[]',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- 'tcin' | 'keyword'
    value      TEXT NOT NULL,
    guild_id   INTEGER NOT NULL DEFAULT 0,
    channel_id INTEGER NOT NULL DEFAULT 0,
    user_id    INTEGER NOT NULL DEFAULT 0,
    role_id    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(kind, value, guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_active ON products(active);
CREATE INDEX IF NOT EXISTS idx_products_street ON products(street_date);
CREATE INDEX IF NOT EXISTS idx_subs_value ON subscriptions(kind, value);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _d(value: date | None) -> str | None:
    return value.isoformat() if value else None


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was never awaited")
        return self._conn

    # ------------------------------------------------------------- products

    async def known_tcins(self) -> set[str]:
        rows = await self.conn.execute_fetchall("SELECT tcin FROM products")
        return {r["tcin"] for r in rows}

    async def get_product(self, tcin: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM products WHERE tcin = ?", (tcin,))
        return await cur.fetchone()

    async def upsert_product(self, p: Product, *, announced: bool | None = None) -> bool:
        """Insert or refresh a SKU. Returns True if this SKU is brand new."""
        existing = await self.get_product(p.tcin)
        now = _now()
        if existing is None:
            await self.conn.execute(
                """INSERT INTO products
                   (tcin, title, dpci, url, image, price, street_date,
                    first_seen, last_seen, announced)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    p.tcin, p.title, p.dpci, p.url, p.image, p.price,
                    _d(p.street_date), now, now, int(bool(announced)),
                ),
            )
            await self.conn.commit()
            return True

        # Only overwrite with non-empty values; different endpoints return
        # different subsets and we do not want a sparse response to blank a field.
        await self.conn.execute(
            """UPDATE products SET
                 title       = COALESCE(NULLIF(?, ''), title),
                 dpci        = COALESCE(NULLIF(?, ''), dpci),
                 url         = COALESCE(NULLIF(?, ''), url),
                 image       = COALESCE(NULLIF(?, ''), image),
                 price       = COALESCE(?, price),
                 street_date = COALESCE(?, street_date),
                 last_seen   = ?,
                 active      = 1
               WHERE tcin = ?""",
            (p.title, p.dpci, p.url, p.image, p.price, _d(p.street_date), now, p.tcin),
        )
        if announced is not None:
            await self.conn.execute(
                "UPDATE products SET announced = ? WHERE tcin = ?",
                (int(announced), p.tcin),
            )
        await self.conn.commit()
        return False

    async def mark_announced(self, tcin: str) -> None:
        await self.conn.execute(
            "UPDATE products SET announced = 1 WHERE tcin = ?", (tcin,)
        )
        await self.conn.commit()

    async def set_street_warned(self, tcin: str, when: date) -> None:
        await self.conn.execute(
            "UPDATE products SET street_warned = ? WHERE tcin = ?",
            (when.isoformat(), tcin),
        )
        await self.conn.commit()

    async def active_tcins(self) -> list[str]:
        rows = await self.conn.execute_fetchall(
            "SELECT tcin FROM products WHERE active = 1 ORDER BY first_seen DESC"
        )
        return [r["tcin"] for r in rows]

    async def upcoming(self, limit: int = 25) -> list[aiosqlite.Row]:
        return list(
            await self.conn.execute_fetchall(
                """SELECT * FROM products
                   WHERE street_date IS NOT NULL AND street_date >= ?
                   ORDER BY street_date ASC LIMIT ?""",
                (date.today().isoformat(), limit),
            )
        )

    async def recent(self, limit: int = 25) -> list[aiosqlite.Row]:
        return list(
            await self.conn.execute_fetchall(
                "SELECT * FROM products ORDER BY first_seen DESC LIMIT ?", (limit,)
            )
        )

    async def search_products(self, term: str, limit: int = 25) -> list[aiosqlite.Row]:
        like = f"%{term}%"
        return list(
            await self.conn.execute_fetchall(
                """SELECT * FROM products
                   WHERE title LIKE ? OR dpci LIKE ? OR tcin LIKE ?
                   ORDER BY first_seen DESC LIMIT ?""",
                (like, like, like, limit),
            )
        )

    async def count_products(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS n FROM products")
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # ---------------------------------------------------------------- state

    async def get_state(self, tcin: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM product_state WHERE tcin = ?", (tcin,)
        )
        return await cur.fetchone()

    async def save_state(self, p: Product) -> None:
        stores = json.dumps(
            [
                {"id": s.store_id, "name": s.store_name, "status": s.status, "qty": s.quantity}
                for s in p.stores
                if s.purchasable
            ]
        )
        await self.conn.execute(
            """INSERT INTO product_state (tcin, state_key, ship_status, stores, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(tcin) DO UPDATE SET
                 state_key = excluded.state_key,
                 ship_status = excluded.ship_status,
                 stores = excluded.stores,
                 updated_at = excluded.updated_at""",
            (p.tcin, p.state_key(), p.ship_status, stores, _now()),
        )
        await self.conn.commit()

    # -------------------------------------------------------- subscriptions

    async def add_subscription(
        self, kind: str, value: str, *, guild_id: int, channel_id: int,
        user_id: int, role_id: int = 0,
    ) -> bool:
        try:
            await self.conn.execute(
                """INSERT INTO subscriptions
                   (kind, value, guild_id, channel_id, user_id, role_id, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (kind, value.lower(), guild_id, channel_id, user_id, role_id, _now()),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_subscription(
        self, kind: str, value: str, *, guild_id: int, user_id: int
    ) -> bool:
        cur = await self.conn.execute(
            """DELETE FROM subscriptions
               WHERE kind = ? AND value = ? AND guild_id = ? AND user_id = ?""",
            (kind, value.lower(), guild_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_subscriptions(
        self, *, guild_id: int, user_id: int | None = None
    ) -> list[aiosqlite.Row]:
        if user_id is None:
            return list(
                await self.conn.execute_fetchall(
                    "SELECT * FROM subscriptions WHERE guild_id = ? ORDER BY created_at",
                    (guild_id,),
                )
            )
        return list(
            await self.conn.execute_fetchall(
                """SELECT * FROM subscriptions
                   WHERE guild_id = ? AND user_id = ? ORDER BY created_at""",
                (guild_id, user_id),
            )
        )

    async def matching_subscriptions(self, p: Product) -> list[aiosqlite.Row]:
        """Subscriptions that should be pinged for this product."""
        rows = list(
            await self.conn.execute_fetchall(
                "SELECT * FROM subscriptions WHERE kind = 'tcin' AND value = ?",
                (p.tcin.lower(),),
            )
        )
        title = (p.title or "").lower()
        dpci = (p.dpci or "").lower()
        for row in await self.conn.execute_fetchall(
            "SELECT * FROM subscriptions WHERE kind = 'keyword'"
        ):
            needle = row["value"]
            if needle and (needle in title or needle in dpci):
                rows.append(row)
        return rows

    # ------------------------------------------------------------- settings

    async def set_setting(self, key: str, value: Any) -> None:
        await self.conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, json.dumps(value)),
        )
        await self.conn.commit()

    async def get_setting(self, key: str, default: Any = None) -> Any:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default
