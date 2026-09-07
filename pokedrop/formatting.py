"""Discord embed construction. Kept separate so webhook-only mode and the
full bot render alerts identically."""
from __future__ import annotations

from datetime import date

import discord

from .monitor import Event, EventKind
from .target_api import Product

STYLE: dict[EventKind, tuple[str, str, int]] = {
    #                     emoji  headline                       colour
    EventKind.NEW_LISTING:  ("🆕", "New SKU listed",            0x5865F2),
    EventKind.STREET_SOON:  ("📅", "Dropping soon",             0xFEE75C),
    EventKind.PREORDER_OPEN:("📦", "Preorders open",            0xEB459E),
    EventKind.RESTOCK:      ("🔥", "IN STOCK — ships",          0x57F287),
    EventKind.IN_STORE:     ("🏬", "In stock at a store",       0x57F287),
    EventKind.SOLD_OUT:     ("⚫", "Sold out",                  0x4F545C),
}


def _price(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "—"


def _street(value: date | None) -> str:
    if not value:
        return "—"
    days = (value - date.today()).days
    stamp = value.strftime("%a %b %d, %Y")
    if days > 1:
        return f"{stamp} (in {days} days)"
    if days == 1:
        return f"{stamp} (tomorrow)"
    if days == 0:
        return f"{stamp} (today)"
    return stamp


def product_embed(product: Product, *, title: str, colour: int, detail: str = "") -> discord.Embed:
    embed = discord.Embed(
        title=title[:256],
        description=(detail or None),
        colour=colour,
        url=product.link,
    )
    embed.add_field(
        name="Product",
        value=f"[{(product.title or 'Untitled')[:180]}]({product.link})",
        inline=False,
    )
    embed.add_field(name="TCIN", value=f"`{product.tcin}`", inline=True)
    embed.add_field(name="DPCI (in-store SKU)", value=f"`{product.dpci or '—'}`", inline=True)
    embed.add_field(name="Price", value=_price(product.price), inline=True)
    embed.add_field(name="Street date", value=_street(product.street_date), inline=True)
    embed.add_field(
        name="Ships",
        value=product.ship_status.replace("_", " ").title() if product.ship_status != "UNKNOWN" else "—",
        inline=True,
    )

    available = [s for s in product.stores if s.purchasable]
    if available:
        lines = []
        for s in available[:6]:
            qty = f" — {int(s.quantity)} left" if s.quantity else ""
            lines.append(f"• {s.store_name or s.store_id}{qty}")
        if len(available) > 6:
            lines.append(f"…and {len(available) - 6} more stores")
        embed.add_field(name="Pickup", value="\n".join(lines), inline=False)
    elif product.stores:
        embed.add_field(name="Pickup", value="No nearby stores have it", inline=False)

    if product.image:
        embed.set_thumbnail(url=product.image)
    embed.set_footer(text="Target · RedSky")
    return embed


def event_embed(event: Event) -> discord.Embed:
    emoji, headline, colour = STYLE[event.kind]
    return product_embed(
        event.product,
        title=f"{emoji} {headline}",
        colour=colour,
        detail=event.detail,
    )


def status_embed(product: Product) -> discord.Embed:
    if product.shippable or product.in_store_count:
        title, colour = "🟢 Available", 0x57F287
    elif product.preorder:
        title, colour = "📦 Preorder", 0xEB459E
    else:
        title, colour = "🔴 Not available", 0xED4245
    return product_embed(product, title=title, colour=colour)


def rows_embed(rows, *, title: str, empty: str, show_street: bool = False) -> discord.Embed:
    embed = discord.Embed(title=title, colour=0x5865F2)
    if not rows:
        embed.description = empty
        return embed

    lines = []
    for row in rows[:20]:
        name = (row["title"] or "Untitled")[:70]
        bits = [f"`{row['tcin']}`"]
        if row["dpci"]:
            bits.append(f"DPCI `{row['dpci']}`")
        if show_street and row["street_date"]:
            bits.append(_street(date.fromisoformat(row["street_date"])))
        url = row["url"] or f"https://www.target.com/p/-/A-{row['tcin']}"
        lines.append(f"**[{name}]({url})**\n{' · '.join(bits)}")

    embed.description = "\n\n".join(lines)
    if len(rows) > 20:
        embed.set_footer(text=f"Showing 20 of {len(rows)}")
    return embed
