"""Runtime configuration, read from the environment (and a .env file if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _guild_ids() -> list[int]:
    raw = os.getenv("DISCORD_GUILD_IDS", "").strip()
    if not raw:
        return []
    ids = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"DISCORD_GUILD_IDS contains a non-numeric id: {chunk!r}") from exc
    return ids


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_ids: list[int]

    reddit_client_id: str
    reddit_client_secret: str
    reddit_username: str | None
    reddit_password: str | None
    reddit_user_agent: str

    poll_interval: int
    subreddits_per_request: int
    max_post_age: int
    data_dir: Path = field(default=Path("data"))

    @property
    def use_password_grant(self) -> bool:
        """True when a Reddit account was supplied, so we can request a user token."""
        return bool(self.reddit_username and self.reddit_password)

    @property
    def grant_type(self) -> str:
        """Which OAuth grant fits the credentials we were given."""
        if self.use_password_grant:
            return "password"
        if self.reddit_client_secret:
            return "client_credentials"
        return "installed_client"

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
        client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()

        # The client secret is optional: "installed app" credentials have none,
        # and we fall back to the installed-client grant in that case.
        missing = [
            name
            for name, value in (
                ("DISCORD_TOKEN", token),
                ("REDDIT_CLIENT_ID", client_id),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill it in."
            )

        user_agent = os.getenv("REDDIT_USER_AGENT", "").strip()
        if not user_agent:
            user_agent = "discord-reddit-notifier/1.0"

        return cls(
            discord_token=token,
            guild_ids=_guild_ids(),
            reddit_client_id=client_id,
            reddit_client_secret=client_secret,
            reddit_username=os.getenv("REDDIT_USERNAME", "").strip() or None,
            reddit_password=os.getenv("REDDIT_PASSWORD", "").strip() or None,
            reddit_user_agent=user_agent,
            poll_interval=_int_env("POLL_INTERVAL", 5, minimum=5),
            subreddits_per_request=_int_env("SUBREDDITS_PER_REQUEST", 25, minimum=1),
            max_post_age=_int_env("MAX_POST_AGE", 3600, minimum=0),
            data_dir=Path(os.getenv("DATA_DIR", "data").strip() or "data"),
        )
