"""Persistence for watches and for the set of posts we have already announced.

Everything lives in two small JSON files so the bot can be restarted (or moved
to another host) without re-announcing posts or losing subscriptions.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

# How many post ids to remember per subreddit. /new tops out at 100 per page,
# so a few hundred is ample to survive bursts without the file growing forever.
SEEN_PER_SUBREDDIT = 300


def normalize_subreddit(name: str) -> str:
    """Turn any of `r/Python`, `/r/python`, `https://reddit.com/r/python/` into `python`."""
    value = name.strip()
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value.split("//", 1)[1]
            value = value.split("/", 1)[1] if "/" in value else ""
    value = value.strip("/")
    for prefix in ("r/", "R/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.strip("/").lower()


@dataclass
class Watch:
    """One subscription: a subreddit, where to post about it, and who to ping."""

    id: str
    subreddit: str
    channel_id: int
    # When set, notifications go to this user's DMs instead of `channel_id`.
    dm_user_id: int | None = None
    mention: str | None = None
    created_by: int | None = None
    guild_id: int | None = None
    keywords: list[str] = field(default_factory=list)
    flair: str | None = None
    author: str | None = None

    @classmethod
    def create(
        cls,
        *,
        subreddit: str,
        channel_id: int,
        dm_user_id: int | None,
        mention: str | None,
        created_by: int | None,
        guild_id: int | None,
        keywords: Iterable[str] = (),
        flair: str | None = None,
        author: str | None = None,
    ) -> "Watch":
        return cls(
            id=uuid.uuid4().hex[:12],
            subreddit=normalize_subreddit(subreddit),
            channel_id=channel_id,
            dm_user_id=dm_user_id,
            mention=mention,
            created_by=created_by,
            guild_id=guild_id,
            keywords=[k.strip().lower() for k in keywords if k.strip()],
            flair=(flair.strip().lower() or None) if flair else None,
            author=(normalize_author(author) or None) if author else None,
        )

    def matches(self, post: dict[str, Any]) -> bool:
        """Apply the optional keyword / flair / author filters to a post."""
        if self.author:
            if (post.get("author") or "").lower() != self.author:
                return False
        if self.flair:
            flair_text = (post.get("link_flair_text") or "").lower()
            if self.flair not in flair_text:
                return False
        if self.keywords:
            haystack = " ".join(
                (
                    post.get("title") or "",
                    post.get("selftext") or "",
                    post.get("link_flair_text") or "",
                )
            ).lower()
            if not any(keyword in haystack for keyword in self.keywords):
                return False
        return True

    @property
    def is_dm(self) -> bool:
        return self.dm_user_id is not None

    def describe_filters(self) -> str:
        parts = []
        if self.keywords:
            parts.append("keywords: " + ", ".join(self.keywords))
        if self.flair:
            parts.append(f"flair: {self.flair}")
        if self.author:
            parts.append(f"author: u/{self.author}")
        return " · ".join(parts) if parts else "no filters"


def normalize_author(name: str) -> str:
    value = name.strip().lstrip("/")
    for prefix in ("u/", "U/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.lower()


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


class Store:
    """Watches plus per-subreddit seen-post ids, persisted as JSON."""

    def __init__(self, data_dir: Path) -> None:
        self._dir = Path(data_dir)
        self._watches_path = self._dir / "watches.json"
        self._seen_path = self._dir / "seen.json"
        self._lock = asyncio.Lock()
        self._watches: dict[str, Watch] = {}
        # subreddit -> ordered mapping of post id -> None (an insertion-ordered set)
        self._seen: dict[str, "OrderedDict[str, None]"] = {}
        self._seen_dirty = False

    # ---------------------------------------------------------------- loading

    def load(self) -> None:
        if self._watches_path.exists():
            raw = json.loads(self._watches_path.read_text(encoding="utf-8") or "[]")
            for item in raw:
                watch = Watch(**item)
                self._watches[watch.id] = watch
        if self._seen_path.exists():
            raw = json.loads(self._seen_path.read_text(encoding="utf-8") or "{}")
            for subreddit, ids in raw.items():
                self._seen[subreddit] = OrderedDict((post_id, None) for post_id in ids)

    # --------------------------------------------------------------- watches

    def watches(self) -> list[Watch]:
        return list(self._watches.values())

    def subreddits(self) -> list[str]:
        return sorted({watch.subreddit for watch in self._watches.values()})

    def watches_for(self, subreddit: str) -> list[Watch]:
        subreddit = subreddit.lower()
        return [w for w in self._watches.values() if w.subreddit == subreddit]

    def watches_in_channel(self, channel_id: int) -> list[Watch]:
        return [w for w in self._watches.values() if w.channel_id == channel_id]

    def find_duplicate(self, watch: Watch) -> Watch | None:
        """An identical subscription in the same channel for the same target."""
        for existing in self._watches.values():
            if (
                existing.subreddit == watch.subreddit
                and existing.channel_id == watch.channel_id
                and existing.dm_user_id == watch.dm_user_id
                and existing.mention == watch.mention
                and existing.keywords == watch.keywords
                and existing.flair == watch.flair
                and existing.author == watch.author
            ):
                return existing
        return None

    async def add_watch(self, watch: Watch) -> None:
        async with self._lock:
            self._watches[watch.id] = watch
            self._save_watches()

    async def remove_watch(self, watch_id: str) -> Watch | None:
        async with self._lock:
            watch = self._watches.pop(watch_id, None)
            if watch is not None:
                self._save_watches()
                # Drop seen state for a subreddit nobody watches any more.
                if not self.watches_for(watch.subreddit):
                    self._seen.pop(watch.subreddit, None)
                    self._seen_dirty = True
            return watch

    def _save_watches(self) -> None:
        _atomic_write(self._watches_path, [asdict(w) for w in self._watches.values()])

    # ------------------------------------------------------------ seen posts

    def is_known_subreddit(self, subreddit: str) -> bool:
        """False the first time we poll a subreddit, so we can seed without pinging."""
        return subreddit in self._seen

    def has_seen(self, subreddit: str, post_id: str) -> bool:
        bucket = self._seen.get(subreddit)
        return bucket is not None and post_id in bucket

    def mark_seen(self, subreddit: str, post_ids: Iterable[str]) -> None:
        bucket = self._seen.setdefault(subreddit, OrderedDict())
        for post_id in post_ids:
            bucket.pop(post_id, None)
            bucket[post_id] = None
        while len(bucket) > SEEN_PER_SUBREDDIT:
            bucket.popitem(last=False)
        self._seen_dirty = True

    async def flush_seen(self) -> None:
        if not self._seen_dirty:
            return
        async with self._lock:
            _atomic_write(
                self._seen_path,
                {subreddit: list(ids) for subreddit, ids in self._seen.items()},
            )
            self._seen_dirty = False
