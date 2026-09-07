"""One-shot scan mode -- the entry point GitHub Actions calls.

Runs a single pass of each monitor loop against JSON state, posts any alerts
straight to a Discord webhook over plain HTTP, writes the state back, and
exits. No persistent process, no gateway connection, nothing to keep running.

    python -m pokedrop scan
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

from . import formatting
from .config import config
from .jsonstore import JsonStore
from .monitor import Event, EventKind, Monitor
from .target_api import RedSkyClient

log = logging.getLogger(__name__)

STATE_PATH = os.getenv("STATE_PATH", "state/pokedrop.json")

# Discord accepts at most 10 embeds per webhook message.
EMBEDS_PER_MESSAGE = 10
# A cold start can produce a lot of events at once; do not carpet-bomb.
MAX_EVENTS_PER_RUN = 30

# Most urgent first, so a truncated run keeps the alerts that matter.
PRIORITY = {
    EventKind.RESTOCK: 0,
    EventKind.PREORDER_OPEN: 1,
    EventKind.IN_STORE: 2,
    EventKind.STREET_SOON: 3,
    EventKind.NEW_LISTING: 4,
    EventKind.SOLD_OUT: 5,
}


async def post_embeds(client: httpx.AsyncClient, url: str, embeds: list[dict]) -> None:
    """POST one webhook message, honouring Discord's rate limit response."""
    for attempt in range(4):
        resp = await client.post(
            url,
            json={"username": "PokéDrop", "embeds": embeds},
            timeout=20.0,
        )
        if resp.status_code in (200, 204):
            return
        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 2))
            except ValueError:
                wait = 2.0
            log.warning("Discord rate limited; waiting %.1fs", wait)
            await asyncio.sleep(min(wait, 30) + 0.5)
            continue
        if 400 <= resp.status_code < 500:
            # A bad or deleted webhook will never succeed -- do not retry it.
            log.error(
                "Discord rejected the webhook (HTTP %s). Is DISCORD_WEBHOOK_URL "
                "still valid? Body: %s",
                resp.status_code, resp.text[:300],
            )
            return
        await asyncio.sleep(2 * (attempt + 1))
    log.error("Gave up posting a webhook message")


def should_run_discovery(store: JsonStore) -> bool:
    """Discovery sweeps many category pages, so it runs on its own cadence
    rather than on every workflow tick."""
    if not store.products:
        return True  # Cold start: we need a baseline before anything else.
    last = store.meta.get("last_discovery")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when >= timedelta(
        seconds=config.discovery_interval
    )


async def main() -> int:
    webhook_url = config.webhook_url
    if not webhook_url:
        log.error("DISCORD_WEBHOOK_URL is not set. Nothing to post to.")
        return 2

    store = JsonStore(STATE_PATH)
    await store.connect()
    cold_start = not store.products

    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    client = RedSkyClient(config)
    monitor = Monitor(store, client, collect, config)

    try:
        if should_run_discovery(store):
            log.info("running discovery sweep")
            await monitor.discovery_pass()
            store.meta["last_discovery"] = datetime.now(
                timezone.utc
            ).isoformat(timespec="seconds")
            store.dirty = True
        else:
            log.info("skipping discovery (ran recently)")

        await monitor.street_date_pass()
        await monitor.status_pass()
    finally:
        await client.aclose()

    log.info(
        "%d product(s) tracked, %d event(s) this run", len(store.products), len(events)
    )

    if events:
        events.sort(key=lambda e: PRIORITY.get(e.kind, 9))
        dropped = 0
        if len(events) > MAX_EVENTS_PER_RUN:
            dropped = len(events) - MAX_EVENTS_PER_RUN
            events = events[:MAX_EVENTS_PER_RUN]

        async with httpx.AsyncClient() as http:
            for i in range(0, len(events), EMBEDS_PER_MESSAGE):
                chunk = events[i : i + EMBEDS_PER_MESSAGE]
                await post_embeds(
                    http, webhook_url,
                    [formatting.event_embed(e).to_dict() for e in chunk],
                )
                await asyncio.sleep(1.0)

            if dropped:
                await post_embeds(http, webhook_url, [{
                    "title": "…and more",
                    "description": f"{dropped} further change(s) this run were not shown.",
                    "color": 0x4F545C,
                }])

    await store.save()

    # Tell the workflow whether the state file is worth committing.
    if out := os.getenv("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if store.dirty else 'false'}\n")
            fh.write(f"events={len(events)}\n")
            fh.write(f"tracked={len(store.products)}\n")

    if cold_start:
        log.info(
            "Baseline seeded with %d SKU(s). Alerts begin from the next run.",
            len(store.products),
        )
    return 0


def run() -> None:
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(levelname)-7s %(message)s",
    )
    sys.exit(asyncio.run(main()))
