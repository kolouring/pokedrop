"""Discord front end. Runs the monitor as a background task and exposes
slash commands for managing the watchlist."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from . import formatting
from .config import Config, config
from .db import Database
from .monitor import Event, EventKind, Monitor
from .target_api import RedSkyClient

log = logging.getLogger(__name__)


class PokeDrop(commands.Bot):
    def __init__(self, cfg: Config = config) -> None:
        super().__init__(command_prefix="!pokedrop ", intents=discord.Intents.default())
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.client_api = RedSkyClient(cfg)
        self.monitor = Monitor(self.db, self.client_api, self.dispatch_event, cfg)

    async def setup_hook(self) -> None:
        await self.db.connect()
        register_commands(self)
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", self.cfg.guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to an hour)")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        if not self.monitor._tasks:
            self.monitor.start()
            log.info(
                "Monitor started: status every %ss, discovery every %ss",
                self.cfg.status_interval, self.cfg.discovery_interval,
            )
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="Target for Pokémon drops"
            )
        )

    async def close(self) -> None:
        await self.monitor.stop()
        await self.client_api.aclose()
        await self.db.close()
        await super().close()

    # ---------------------------------------------------------------- alerts

    async def _alert_channel(self) -> discord.abc.Messageable | None:
        channel_id = await self.db.get_setting("channel_id", self.cfg.default_channel_id)
        if not channel_id:
            return None
        channel = self.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except discord.HTTPException:
                log.warning("Cannot reach alert channel %s", channel_id)
                return None
        return channel

    async def dispatch_event(self, event: Event) -> None:
        channel = await self._alert_channel()
        if channel is None:
            log.warning("No alert channel configured; dropping %s", event.kind)
            return

        embed = formatting.event_embed(event)
        mentions: list[str] = []

        subs = await self.db.matching_subscriptions(event.product)
        if event.urgent:
            role_id = await self.db.get_setting("role_id", 0)
            if role_id:
                mentions.append(f"<@&{int(role_id)}>")
            for row in subs:
                mention = f"<@{row['user_id']}>"
                if row["user_id"] and mention not in mentions:
                    mentions.append(mention)
                if row["role_id"] and f"<@&{row['role_id']}>" not in mentions:
                    mentions.append(f"<@&{row['role_id']}>")
        elif event.kind in (EventKind.NEW_LISTING, EventKind.STREET_SOON):
            for row in subs:
                mention = f"<@{row['user_id']}>"
                if row["user_id"] and mention not in mentions:
                    mentions.append(mention)

        try:
            await channel.send(content=" ".join(mentions) or None, embed=embed)
        except discord.HTTPException as exc:
            log.warning("Failed to post alert: %s", exc)


# --------------------------------------------------------------------- commands


def register_commands(bot: PokeDrop) -> None:
    tree = bot.tree

    track = app_commands.Group(name="track", description="Watch a product or keyword")
    admin = app_commands.Group(
        name="pokedrop", description="Configure the drop tracker",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ----------------------------------------------------------------- track

    @track.command(name="sku", description="Get pinged about a specific TCIN")
    @app_commands.describe(tcin="Target TCIN, the number after A- in the product URL")
    async def track_sku(interaction: discord.Interaction, tcin: str) -> None:
        await interaction.response.defer(ephemeral=True)
        tcin = tcin.strip().lstrip("Aa-")
        if not tcin.isdigit():
            await interaction.followup.send(
                "That does not look like a TCIN. It is the digits after `A-` in a "
                "target.com product URL, e.g. `https://www.target.com/p/-/A-**94300069**`.",
                ephemeral=True,
            )
            return

        product = await bot.monitor.check_now(tcin)
        if product is None:
            await interaction.followup.send(
                f"Target has no product with TCIN `{tcin}`.", ephemeral=True
            )
            return

        added = await bot.db.add_subscription(
            "tcin", tcin,
            guild_id=interaction.guild_id or 0,
            channel_id=interaction.channel_id or 0,
            user_id=interaction.user.id,
        )
        verb = "Now tracking" if added else "Already tracking"
        await interaction.followup.send(
            content=f"{verb} — you will be pinged when this changes.",
            embed=formatting.status_embed(product),
            ephemeral=True,
        )

    @track.command(name="keyword", description="Get pinged when a matching product drops")
    @app_commands.describe(text="Matched against product titles and DPCIs, e.g. 'elite trainer'")
    async def track_keyword(interaction: discord.Interaction, text: str) -> None:
        text = text.strip()
        if len(text) < 3:
            await interaction.response.send_message(
                "Use at least 3 characters, otherwise it will match everything.",
                ephemeral=True,
            )
            return
        added = await bot.db.add_subscription(
            "keyword", text,
            guild_id=interaction.guild_id or 0,
            channel_id=interaction.channel_id or 0,
            user_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"{'Now tracking' if added else 'Already tracking'} keyword `{text}`.",
            ephemeral=True,
        )

    @track.command(name="remove", description="Stop tracking a TCIN or keyword")
    @app_commands.describe(value="The TCIN or keyword you want to drop")
    async def track_remove(interaction: discord.Interaction, value: str) -> None:
        guild_id = interaction.guild_id or 0
        removed = await bot.db.remove_subscription(
            "tcin", value.strip(), guild_id=guild_id, user_id=interaction.user.id
        ) or await bot.db.remove_subscription(
            "keyword", value.strip(), guild_id=guild_id, user_id=interaction.user.id
        )
        await interaction.response.send_message(
            f"Removed `{value}`." if removed else f"You were not tracking `{value}`.",
            ephemeral=True,
        )

    @track.command(name="list", description="Show what you are tracking")
    async def track_list(interaction: discord.Interaction) -> None:
        rows = await bot.db.list_subscriptions(
            guild_id=interaction.guild_id or 0, user_id=interaction.user.id
        )
        if not rows:
            await interaction.response.send_message(
                "You are not tracking anything yet. Try `/track keyword text:elite trainer`.",
                ephemeral=True,
            )
            return
        lines = [
            f"• {'TCIN' if r['kind'] == 'tcin' else 'Keyword'} `{r['value']}`" for r in rows
        ]
        embed = discord.Embed(
            title="Your watchlist", description="\n".join(lines), colour=0x5865F2
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    tree.add_command(track)

    # ------------------------------------------------------------- lookups

    @tree.command(name="check", description="Check a TCIN's availability right now")
    @app_commands.describe(tcin="Target TCIN, the number after A- in the product URL")
    async def check(interaction: discord.Interaction, tcin: str) -> None:
        await interaction.response.defer()
        product = await bot.monitor.check_now(tcin.strip().lstrip("Aa-"))
        if product is None:
            await interaction.followup.send(f"No product found for TCIN `{tcin}`.")
            return
        await interaction.followup.send(embed=formatting.status_embed(product))

    @tree.command(name="upcoming", description="Pokémon SKUs with a future street date")
    async def upcoming(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        rows = await bot.db.upcoming(limit=25)
        await interaction.followup.send(
            embed=formatting.rows_embed(
                rows,
                title="📅 Upcoming drops",
                empty="Nothing with a known future street date yet. "
                      "Street dates appear once Target publishes them.",
                show_street=True,
            )
        )

    @tree.command(name="recent", description="SKUs the tracker discovered most recently")
    async def recent(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        rows = await bot.db.recent(limit=20)
        await interaction.followup.send(
            embed=formatting.rows_embed(
                rows,
                title="🆕 Recently discovered SKUs",
                empty="Nothing discovered yet — the first sweep may still be running.",
                show_street=True,
            )
        )

    @tree.command(name="find", description="Search discovered SKUs by name, TCIN or DPCI")
    @app_commands.describe(term="Part of a product name, TCIN, or DPCI")
    async def find(interaction: discord.Interaction, term: str) -> None:
        await interaction.response.defer()
        rows = await bot.db.search_products(term.strip(), limit=25)
        await interaction.followup.send(
            embed=formatting.rows_embed(
                rows,
                title=f"🔎 Results for “{term}”",
                empty="No discovered SKUs match that.",
                show_street=True,
            )
        )

    # --------------------------------------------------------------- admin

    @admin.command(name="channel", description="Set the channel alerts are posted to")
    async def set_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await bot.db.set_setting("channel_id", channel.id)
        await interaction.response.send_message(
            f"Alerts will post to {channel.mention}.", ephemeral=True
        )

    @admin.command(name="role", description="Set the role pinged on in-stock alerts")
    async def set_role(interaction: discord.Interaction, role: discord.Role) -> None:
        await bot.db.set_setting("role_id", role.id)
        await interaction.response.send_message(
            f"{role.mention} will be pinged when something goes in stock.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="status", description="Monitor health and counters")
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        total = await bot.db.count_products()
        m = bot.monitor

        def ago(when: datetime | None) -> str:
            if not when:
                return "not yet"
            secs = int((datetime.now(timezone.utc) - when).total_seconds())
            return f"{secs}s ago" if secs < 120 else f"{secs // 60}m ago"

        channel_id = await bot.db.get_setting("channel_id", bot.cfg.default_channel_id)
        embed = discord.Embed(title="PokéDrop status", colour=0x5865F2)
        embed.add_field(name="SKUs tracked", value=str(total), inline=True)
        embed.add_field(name="Events sent", value=str(m.stats["events"]), inline=True)
        embed.add_field(name="Status polls", value=str(m.stats["polls"]), inline=True)
        embed.add_field(name="Last stock check", value=ago(m.last_status), inline=True)
        embed.add_field(name="Last discovery", value=ago(m.last_discovery), inline=True)
        embed.add_field(
            name="Alert channel",
            value=f"<#{int(channel_id)}>" if channel_id else "unset",
            inline=True,
        )
        embed.add_field(
            name="Intervals",
            value=f"stock {bot.cfg.status_interval}s · discovery {bot.cfg.discovery_interval}s",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="scan", description="Run a discovery sweep right now")
    async def scan(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        before = await bot.db.count_products()
        await bot.monitor.discovery_pass()
        after = await bot.db.count_products()
        await interaction.followup.send(
            f"Sweep done. {after - before} new SKU(s); {after} tracked in total.",
            ephemeral=True,
        )

    tree.add_command(admin)


def run() -> None:
    config.validate()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    PokeDrop().run(config.discord_token, log_handler=None)
