"""Webhook-only mode: run the monitor with no bot user.

Alerts still post as rich embeds, but there are no slash commands, so the
watchlist is whatever WATCH_KEYWORDS discovers. Useful if you cannot or do
not want to create a bot application.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord

from . import formatting
from .config import config
from .db import Database
from .monitor import Event, Monitor
from .target_api import RedSkyClient

log = logging.getLogger(__name__)


async def main() -> None:
    db = Database(config.db_path)
    await db.connect()
    client = RedSkyClient(config)

    async with aiohttp.ClientSession() as session:
        hook = discord.Webhook.from_url(config.webhook_url, session=session)

        async def on_event(event: Event) -> None:
            try:
                await hook.send(
                    embed=formatting.event_embed(event),
                    username="PokéDrop",
                )
            except discord.HTTPException as exc:
                log.warning("Webhook post failed: %s", exc)

        monitor = Monitor(db, client, on_event, config)
        monitor.start()
        log.info("Webhook monitor running. Ctrl-C to stop.")
        try:
            await asyncio.Event().wait()
        finally:
            await monitor.stop()
            await client.aclose()
            await db.close()


def run() -> None:
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
