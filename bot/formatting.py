"""Turning a raw Reddit post into something readable in Discord."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import discord

REDDIT_ORANGE = discord.Colour.from_str("#FF4500")

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def permalink(post: dict[str, Any]) -> str:
    link = post.get("permalink") or ""
    return f"https://reddit.com{link}" if link else (post.get("url") or "")


def _image_url(post: dict[str, Any]) -> str | None:
    """Best-effort preview image, without pulling in NSFW or spoilered media."""
    if post.get("over_18") or post.get("spoiler"):
        return None
    url = post.get("url_overridden_by_dest") or post.get("url") or ""
    if url.lower().endswith(IMAGE_SUFFIXES):
        return url
    try:
        source = post["preview"]["images"][0]["source"]["url"]
    except (KeyError, IndexError, TypeError):
        return None
    return source if isinstance(source, str) else None


def build_embed(post: dict[str, Any], *, detected_at: float | None = None) -> discord.Embed:
    created = float(post.get("created_utc") or 0)
    subreddit = post.get("subreddit") or "?"
    author = post.get("author") or "[deleted]"

    embed = discord.Embed(
        title=_truncate(post.get("title") or "(no title)", 256),
        url=permalink(post),
        colour=REDDIT_ORANGE,
        timestamp=dt.datetime.fromtimestamp(created, tz=dt.timezone.utc) if created else None,
    )
    embed.set_author(
        name=f"r/{subreddit} · u/{author}",
        url=f"https://reddit.com/r/{subreddit}/new",
    )

    body = (post.get("selftext") or "").strip()
    if body:
        embed.description = _truncate(body, 500)
    else:
        url = post.get("url") or ""
        # Link posts: show where the link actually goes.
        if url and not url.startswith(f"https://www.reddit.com/r/{subreddit}"):
            embed.description = _truncate(url, 500)

    flair = post.get("link_flair_text")
    if flair:
        embed.add_field(name="Flair", value=_truncate(str(flair), 200), inline=True)
    if post.get("over_18"):
        embed.add_field(name="NSFW", value="yes", inline=True)

    image = _image_url(post)
    if image:
        embed.set_image(url=image)

    footer_bits = [f"r/{subreddit}"]
    if created:
        reference = detected_at if detected_at is not None else time.time()
        delay = max(reference - created, 0.0)
        footer_bits.append(f"spotted {_format_delay(delay)} after posting")
    embed.set_footer(text=" · ".join(footer_bits))
    return embed


def _format_delay(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.1f}h"


def build_message(mention: str | None) -> str:
    """The plain-text line above the embed. Only this part actually pings."""
    return mention or ""
