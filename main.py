"""Entry point: `python main.py`."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import aiohttp
import discord

from bot.config import Config, ConfigError
from bot.discord_bot import NotifierBot


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    # This bot never uses voice, so skip discord.py's voice-dependency warnings.
    logging.getLogger("discord.client").setLevel(logging.ERROR)


async def run() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    config.data_dir.mkdir(parents=True, exist_ok=True)
    client = NotifierBot(config)
    try:
        await client.start(config.discord_token)
    except discord.LoginFailure:
        print(
            "Discord rejected DISCORD_TOKEN. Reset the token in the Developer Portal "
            "and update your .env.",
            file=sys.stderr,
        )
        return 2
    except discord.PrivilegedIntentsRequired:
        print(
            "Discord requires privileged intents that this application does not have. "
            "This bot only needs the default intents, so check the Bot page in the "
            "Developer Portal.",
            file=sys.stderr,
        )
        return 2
    except (aiohttp.ClientError, discord.HTTPException) as exc:
        print(f"Could not connect to Discord: {exc}", file=sys.stderr)
        return 1
    finally:
        if not client.is_closed():
            await client.close()
    return 0


def main() -> int:
    setup_logging()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
