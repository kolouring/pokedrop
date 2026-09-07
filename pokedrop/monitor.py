"""The watcher. Three loops, each answering a different flavour of
'when is this dropping?'

  discovery  -- a SKU exists on Target that we have never seen before.
                This is the earliest warning you get: Target sets up the
                TCIN and DPCI well before the item is orderable.
  street     -- a known SKU has a street date that is about to land.
  status     -- a known SKU just became orderable (or preorderable, or
                appeared in a nearby store).
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Awaitable, Callable

from .config import Config, config
from .db import Database
from .target_api import Product, RedSkyClient, looks_like_pokemon

log = logging.getLogger(__name__)


class EventKind(str, Enum):
    NEW_LISTING = "new_listing"
    STREET_SOON = "street_soon"
    PREORDER_OPEN = "preorder_open"
    RESTOCK = "restock"
    IN_STORE = "in_store"
    SOLD_OUT = "sold_out"


@dataclass
class Event:
    kind: EventKind
    product: Product
    detail: str = ""

    @property
    def urgent(self) -> bool:
        return self.kind in (
            EventKind.RESTOCK,
            EventKind.PREORDER_OPEN,
            EventKind.IN_STORE,
        )


EventHandler = Callable[[Event], Awaitable[None]]


class Monitor:
    def __init__(
        self,
        db: Database,
        client: RedSkyClient,
        on_event: EventHandler,
        cfg: Config = config,
    ) -> None:
        self.db = db
        self.client = client
        self.on_event = on_event
        self.cfg = cfg
        self._tasks: list[asyncio.Task] = []
        self.last_discovery: datetime | None = None
        self.last_status: datetime | None = None
        self.stats = {"discovered": 0, "events": 0, "polls": 0}

    # ---------------------------------------------------------------- control

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop(self.discovery_pass,
                                           self.cfg.discovery_interval, "discovery")),
            asyncio.create_task(self._loop(self.status_pass,
                                           self.cfg.status_interval, "status")),
            asyncio.create_task(self._loop(self.street_date_pass,
                                           self.cfg.street_date_interval, "street")),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, fn, interval: int, name: str) -> None:
        # Stagger startup so the three loops do not all fire at once.
        await asyncio.sleep(random.uniform(1, 5))
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s pass failed", name)
            # Jitter keeps the request pattern from looking metronomic.
            await asyncio.sleep(interval * random.uniform(0.85, 1.15))

    async def _emit(self, event: Event) -> None:
        self.stats["events"] += 1
        try:
            await self.on_event(event)
        except Exception:
            log.exception("event handler failed for %s", event.product.tcin)

    # -------------------------------------------------------------- discovery

    async def discovery_pass(self) -> None:
        """Sweep search + category listings looking for TCINs we have never seen."""
        seen: dict[str, Product] = {}

        for keyword in self.cfg.keywords:
            for p in await self.client.search(keyword=keyword):
                if looks_like_pokemon(p.title, self.cfg.title_filter):
                    seen.setdefault(p.tcin, p)
            await asyncio.sleep(random.uniform(1.0, 2.5))

        for category in self.cfg.categories:
            for p in await self.client.search(category=category):
                if looks_like_pokemon(p.title, self.cfg.title_filter):
                    seen.setdefault(p.tcin, p)
            await asyncio.sleep(random.uniform(1.0, 2.5))

        known = await self.db.known_tcins()
        first_run = not known
        new_tcins = [t for t in seen if t not in known]

        log.info(
            "discovery: %d pokemon SKUs visible, %d new%s",
            len(seen), len(new_tcins), " (seeding baseline)" if first_run else "",
        )

        for tcin in new_tcins:
            product = seen[tcin]
            # The search payload often omits dpci/street_date; the PDP has both,
            # and those two fields are the whole point of a new-listing alert.
            detail = await self.client.detail(tcin)
            if detail:
                product.dpci = detail.dpci or product.dpci
                product.street_date = detail.street_date or product.street_date
                product.title = detail.title or product.title
                product.image = detail.image or product.image
                product.url = detail.url or product.url
                product.price = detail.price if detail.price is not None else product.price

            # On a cold database everything is "new"; announcing hundreds of
            # SKUs at once is noise, so the first run only seeds the baseline.
            await self.db.upsert_product(product, announced=first_run)
            self.stats["discovered"] += 1

            if not first_run:
                await self._emit(Event(EventKind.NEW_LISTING, product))
                await self.db.mark_announced(tcin)

            await asyncio.sleep(random.uniform(0.5, 1.5))

        # Refresh metadata for everything else we saw.
        for tcin, product in seen.items():
            if tcin not in new_tcins:
                await self.db.upsert_product(product)

        self.last_discovery = datetime.now(timezone.utc)

    # ------------------------------------------------------------ street date

    async def street_date_pass(self) -> None:
        """Warn ahead of a street date so you can be at the keyboard for it."""
        horizon = date.today() + timedelta(hours=self.cfg.street_date_warn_hours)
        for row in await self.db.upcoming(limit=100):
            sd = date.fromisoformat(row["street_date"])
            if sd > horizon:
                continue
            if row["street_warned"] == row["street_date"]:
                continue

            product = Product(
                tcin=row["tcin"], title=row["title"], dpci=row["dpci"],
                url=row["url"], image=row["image"], price=row["price"],
                street_date=sd,
            )
            days = (sd - date.today()).days
            when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
            await self._emit(
                Event(EventKind.STREET_SOON, product, detail=f"Street date is {when}.")
            )
            await self.db.set_street_warned(row["tcin"], sd)

    # ----------------------------------------------------------------- status

    async def status_pass(self) -> None:
        """Poll availability for every tracked SKU and diff against last state."""
        tcins = await self.db.active_tcins()
        if not tcins:
            return

        products = await self.client.summaries(tcins)
        self.stats["polls"] += 1

        for tcin, product in products.items():
            row = await self.db.get_product(tcin)
            if row:
                # Summary responses omit street_date; keep what we already know.
                if not product.street_date and row["street_date"]:
                    product.street_date = date.fromisoformat(row["street_date"])
                product.dpci = product.dpci or row["dpci"]
                product.title = product.title or row["title"]
                product.image = product.image or row["image"]

            await self.db.upsert_product(product)

            prev = await self.db.get_state(tcin)
            await self.db.save_state(product)

            if prev is None:
                continue  # First observation is the baseline, not an event.
            if prev["state_key"] == product.state_key():
                continue

            for event in self._diff(prev, product):
                await self._emit(event)

        self.last_status = datetime.now(timezone.utc)

    def _diff(self, prev, product: Product) -> list[Event]:
        events: list[Event] = []
        was_ship = prev["ship_status"] in {"IN_STOCK", "AVAILABLE"}
        was_pre = str(prev["ship_status"]).startswith("PRE_ORDER_SELLABLE")
        prev_stores = set(
            filter(None, (prev["state_key"].split("|", 1) + [""])[1].split(","))
        )
        now_stores = {s.store_id for s in product.stores if s.purchasable}

        if product.preorder and not was_pre:
            events.append(
                Event(EventKind.PREORDER_OPEN, product, "Preorders just opened.")
            )
        elif product.shippable and not was_ship and not product.preorder:
            events.append(
                Event(EventKind.RESTOCK, product, "Now orderable for shipping.")
            )

        fresh = now_stores - prev_stores
        if fresh:
            names = [s.store_name or s.store_id for s in product.stores
                     if s.store_id in fresh and s.purchasable]
            events.append(
                Event(
                    EventKind.IN_STORE,
                    product,
                    "In stock for pickup at " + ", ".join(names[:5]),
                )
            )

        if (was_ship or was_pre) and not product.shippable and not now_stores:
            events.append(Event(EventKind.SOLD_OUT, product, "Sold out again."))

        return events

    # ------------------------------------------------------------- on-demand

    async def check_now(self, tcin: str) -> Product | None:
        """Fetch a single SKU on request, merging PDP detail into availability."""
        summaries = await self.client.summaries([tcin])
        product = summaries.get(tcin)
        detail = await self.client.detail(tcin)

        if product and detail:
            product.dpci = product.dpci or detail.dpci
            product.street_date = product.street_date or detail.street_date
            product.title = product.title or detail.title
            product.image = product.image or detail.image
            product.price = product.price if product.price is not None else detail.price
        product = product or detail
        if product:
            await self.db.upsert_product(product, announced=True)
        return product
