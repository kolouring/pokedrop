"""Entry point. `python -m pokedrop`

Runs the full bot when DISCORD_TOKEN is set, otherwise falls back to
webhook-only mode.
"""
from __future__ import annotations

import sys

from .config import config


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "scan":
        # One pass and exit. This is what GitHub Actions runs on a schedule.
        from .oneshot import run
    elif mode == "webhook" or (not config.discord_token and config.webhook_url):
        # Long-running, webhook alerts, no slash commands.
        from .webhook import run
    else:
        # Long-running full bot with slash commands.
        from .bot import run

    run()


if __name__ == "__main__":
    main()
