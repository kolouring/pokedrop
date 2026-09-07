"""Configuration, loaded from environment variables / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, "") or default
    return [p.strip() for p in raw.split(",") if p.strip()]


# Target's public web API key. This is the key target.com itself ships to
# browsers; it is not a secret and not tied to an account. If Target rotates
# it, grab the current one from a target.com network request (any call to
# redsky.target.com carries `key=` in the query string) and set REDSKY_KEY.
DEFAULT_REDSKY_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"

# Target category node IDs, verified against the live API.
#
# Keyword search is close to useless here: searching "pokemon" returns ZERO
# products and instead hands back a redirect to node 4yka5. Category browse is
# the reliable path, so these nodes -- not keywords -- do the real work.
#
#   4yka5  Pokemon (the brand hub; where TCG product lands)
#   x6ax5  Pokemon Toys
#   5xtg5  Trading Cards (catches TCG SKUs filed outside the brand hub)
DEFAULT_CATEGORIES = ["4yka5", "x6ax5", "5xtg5"]

# Supplementary only. Any of these that RedSky redirects to a category are
# followed automatically -- see RedSkyClient.search().
DEFAULT_KEYWORDS = [
    "pokemon elite trainer box",
    "pokemon booster bundle",
]


@dataclass
class Config:
    # --- Discord ---
    discord_token: str = field(default_factory=lambda: os.getenv("DISCORD_TOKEN", ""))
    guild_id: int = field(default_factory=lambda: _int("DISCORD_GUILD_ID", 0))
    default_channel_id: int = field(default_factory=lambda: _int("DISCORD_CHANNEL_ID", 0))
    webhook_url: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", ""))

    # --- Target / RedSky ---
    redsky_key: str = field(
        default_factory=lambda: os.getenv("REDSKY_KEY") or DEFAULT_REDSKY_KEY
    )
    zip_code: str = field(default_factory=lambda: os.getenv("TARGET_ZIP", "55403"))
    state: str = field(default_factory=lambda: os.getenv("TARGET_STATE", "MN"))
    # Primary store used for pricing context on every call.
    store_id: str = field(default_factory=lambda: os.getenv("TARGET_STORE_ID", "1357"))
    # Extra stores to check in-store pickup stock against.
    extra_store_ids: list[str] = field(
        default_factory=lambda: _list("TARGET_EXTRA_STORE_IDS")
    )

    # --- What to watch ---
    categories: list[str] = field(
        default_factory=lambda: _list("WATCH_CATEGORIES") or list(DEFAULT_CATEGORIES)
    )
    keywords: list[str] = field(
        default_factory=lambda: _list("WATCH_KEYWORDS") or list(DEFAULT_KEYWORDS)
    )
    # Only treat a discovered SKU as Pokemon if its title matches this.
    title_filter: str = field(
        default_factory=lambda: os.getenv("TITLE_FILTER", "pokemon|pokémon")
    )

    # --- Timing (seconds) ---
    status_interval: int = field(default_factory=lambda: _int("STATUS_INTERVAL", 90))
    discovery_interval: int = field(
        default_factory=lambda: _int("DISCOVERY_INTERVAL", 900)
    )
    street_date_interval: int = field(
        default_factory=lambda: _int("STREET_DATE_INTERVAL", 3600)
    )
    # Warn this many hours before a street date lands.
    street_date_warn_hours: int = field(
        default_factory=lambda: _int("STREET_DATE_WARN_HOURS", 48)
    )

    request_timeout: float = 20.0
    max_concurrent_requests: int = field(
        default_factory=lambda: _int("MAX_CONCURRENT_REQUESTS", 4)
    )

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/pokedrop.db"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def validate(self) -> None:
        if not self.discord_token and not self.webhook_url:
            raise SystemExit(
                "Set DISCORD_TOKEN (full bot) or DISCORD_WEBHOOK_URL (webhook-only mode) "
                "in your .env file. See .env.example."
            )
        if self.discord_token and not self.default_channel_id:
            raise SystemExit("DISCORD_CHANNEL_ID is required when using DISCORD_TOKEN.")


config = Config()
