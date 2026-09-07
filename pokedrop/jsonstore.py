"""A JSON-file store that mirrors `Database`'s interface.

Why this exists: GitHub Actions runners are wiped after every run, so state has
to live in the repo. A committed JSON file is the right shape for that -- it
diffs readably, you can open it on github.com and see exactly which SKUs are
tracked, and it survives forever. A committed SQLite binary would do neither.

Keys are written sorted so a run that changes one SKU produces a one-line diff
rather than a reshuffled file.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any

from .target_api import Product

VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _d(value: date | None) -> str | None:
    return value.isoformat() if value else None


class JsonStore:
    """Async to match Database, but every operation is in-memory until save()."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.products: dict[str, dict] = {}
        self.states: dict[str, dict] = {}
        # Bookkeeping that is not a product: last discovery sweep, etc.
        self.meta: dict[str, Any] = {}
        # Lets the workflow skip an empty commit when a run changed nothing.
        self.dirty = False

    # ------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # A corrupt state file must not wedge the bot forever; start clean.
            return
        self.products = data.get("products", {}) or {}
        self.states = data.get("states", {}) or {}
        self.meta = data.get("meta", {}) or {}

    async def save(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            "version": VERSION,
            "updated_at": _now(),
            "meta": self.meta,
            "products": dict(sorted(self.products.items())),
            "states": dict(sorted(self.states.items())),
        }
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, self.path)

    async def close(self) -> None:
        await self.save()

    # ------------------------------------------------------------- products

    async def known_tcins(self) -> set[str]:
        return set(self.products)

    async def get_product(self, tcin: str) -> dict | None:
        return self.products.get(tcin)

    async def upsert_product(self, p: Product, *, announced: bool | None = None) -> bool:
        existing = self.products.get(p.tcin)
        now = _now()

        if existing is None:
            self.products[p.tcin] = {
                "tcin": p.tcin, "title": p.title, "dpci": p.dpci, "url": p.url,
                "image": p.image, "price": p.price, "street_date": _d(p.street_date),
                "first_seen": now, "last_seen": now,
                "announced": int(bool(announced)), "street_warned": None, "active": 1,
            }
            self.dirty = True
            return True

        before = dict(existing)
        # Only non-empty values overwrite: a sparse summary response must never
        # blank a field a richer PDP response already filled in.
        for key, value in (
            ("title", p.title), ("dpci", p.dpci),
            ("url", p.url), ("image", p.image),
        ):
            if value:
                existing[key] = value
        if p.price is not None:
            existing["price"] = p.price
        if p.street_date:
            existing["street_date"] = _d(p.street_date)
        existing["active"] = 1
        if announced is not None:
            existing["announced"] = int(announced)

        # last_seen changes every run; ignore it when deciding if we should commit.
        compare_before = {k: v for k, v in before.items() if k != "last_seen"}
        compare_after = {k: v for k, v in existing.items() if k != "last_seen"}
        if compare_before != compare_after:
            self.dirty = True
        existing["last_seen"] = now
        return False

    async def mark_announced(self, tcin: str) -> None:
        if tcin in self.products:
            self.products[tcin]["announced"] = 1
            self.dirty = True

    async def set_street_warned(self, tcin: str, when: date) -> None:
        if tcin in self.products:
            self.products[tcin]["street_warned"] = when.isoformat()
            self.dirty = True

    async def active_tcins(self) -> list[str]:
        rows = [r for r in self.products.values() if r.get("active", 1)]
        rows.sort(key=lambda r: r.get("first_seen", ""), reverse=True)
        return [r["tcin"] for r in rows]

    async def upcoming(self, limit: int = 25) -> list[dict]:
        today = date.today().isoformat()
        rows = [
            r for r in self.products.values()
            if r.get("street_date") and r["street_date"] >= today
        ]
        rows.sort(key=lambda r: r["street_date"])
        return rows[:limit]

    async def recent(self, limit: int = 25) -> list[dict]:
        rows = sorted(
            self.products.values(), key=lambda r: r.get("first_seen", ""), reverse=True
        )
        return rows[:limit]

    async def search_products(self, term: str, limit: int = 25) -> list[dict]:
        needle = term.lower()
        rows = [
            r for r in self.products.values()
            if needle in (r.get("title") or "").lower()
            or needle in (r.get("dpci") or "").lower()
            or needle in r["tcin"]
        ]
        rows.sort(key=lambda r: r.get("first_seen", ""), reverse=True)
        return rows[:limit]

    async def count_products(self) -> int:
        return len(self.products)

    # ---------------------------------------------------------------- state

    async def get_state(self, tcin: str) -> dict | None:
        return self.states.get(tcin)

    async def save_state(self, p: Product) -> None:
        record = {
            "state_key": p.state_key(),
            "ship_status": p.ship_status,
            "stores": [
                {"id": s.store_id, "name": s.store_name, "status": s.status, "qty": s.quantity}
                for s in p.stores if s.purchasable
            ],
            "updated_at": _now(),
        }
        previous = self.states.get(p.tcin)
        if not previous or previous.get("state_key") != record["state_key"]:
            self.dirty = True
        self.states[p.tcin] = record

    # ------------------------------------------------------- not applicable

    async def matching_subscriptions(self, p: Product) -> list[dict]:
        # Subscriptions are a bot-mode feature; webhook mode has no users.
        return []
