"""The polling loop: pull `/new`, work out what is actually new, hand it off."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from .config import Config
from .reddit import RedditAuthError, RedditClient, RedditError
from .store import Store, Watch

log = logging.getLogger(__name__)

DeliverFn = Callable[[Watch, dict[str, Any]], Awaitable[None]]

# Consecutive-failure backoff, in seconds.
BACKOFF_STEPS = (5, 15, 30, 60, 120, 300)


@dataclass
class Stats:
    started_at: float = field(default_factory=time.time)
    polls: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    posts_seen: int = 0
    notifications_sent: int = 0
    last_poll_at: float | None = None
    last_poll_duration: float | None = None
    last_error: str | None = None
    last_detection_latency: float | None = None

    def record_latency(self, seconds: float) -> None:
        self.last_detection_latency = seconds


class Watcher:
    def __init__(
        self,
        config: Config,
        store: Store,
        reddit: RedditClient,
        deliver: DeliverFn,
    ) -> None:
        self._config = config
        self._store = store
        self._reddit = reddit
        self._deliver = deliver
        self.stats = Stats()
        self._wake = asyncio.Event()
        # Posts created before this are treated as backlog and never announced.
        self._floor = time.time() - config.max_post_age

    def nudge(self) -> None:
        """Ask the loop to poll immediately, e.g. right after a new watch is added."""
        self._wake.set()

    async def run(self) -> None:
        log.info(
            "Watcher started: interval=%ss, batch=%s subreddits/request",
            self._config.poll_interval,
            self._config.subreddits_per_request,
        )
        while True:
            delay = self._config.poll_interval
            try:
                await self._poll_once()
                self.stats.consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except RedditAuthError as exc:
                # Credentials are wrong; hammering Reddit will not fix them.
                self.stats.errors += 1
                self.stats.consecutive_errors += 1
                self.stats.last_error = str(exc)
                log.error("Reddit authentication failed: %s", exc)
                delay = max(self._config.poll_interval, 300)
            except (RedditError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.stats.errors += 1
                self.stats.consecutive_errors += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                delay = self._backoff_delay()
                log.warning("Poll failed (%s); retrying in %ss", exc, delay)
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self.stats.errors += 1
                self.stats.consecutive_errors += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                delay = self._backoff_delay()
                log.exception("Unexpected error in poll loop; retrying in %ss", delay)

            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake.clear()

    def _backoff_delay(self) -> int:
        index = min(self.stats.consecutive_errors, len(BACKOFF_STEPS)) - 1
        return max(self._config.poll_interval, BACKOFF_STEPS[max(index, 0)])

    async def _poll_once(self) -> None:
        subreddits = self._store.subreddits()
        if not subreddits:
            return

        started = time.monotonic()
        batch_size = self._config.subreddits_per_request
        batches = [
            subreddits[i : i + batch_size] for i in range(0, len(subreddits), batch_size)
        ]

        for batch in batches:
            posts = await self._reddit.fetch_new(batch)
            await self._handle_posts(batch, posts)

        self.stats.polls += 1
        self.stats.last_poll_at = time.time()
        self.stats.last_poll_duration = time.monotonic() - started
        await self._store.flush_seen()

    async def _handle_posts(self, batch: list[str], posts: list[dict[str, Any]]) -> None:
        # Group by subreddit so a first-time subreddit can be seeded on its own.
        by_subreddit: dict[str, list[dict[str, Any]]] = {sub: [] for sub in batch}
        for post in posts:
            name = (post.get("subreddit") or "").lower()
            by_subreddit.setdefault(name, []).append(post)

        for subreddit, subreddit_posts in by_subreddit.items():
            ids = [post["id"] for post in subreddit_posts]

            if not self._store.is_known_subreddit(subreddit):
                # First sight: remember what is already there, announce none of it.
                self._store.mark_seen(subreddit, ids)
                log.info("Seeded r/%s with %d existing posts", subreddit, len(ids))
                continue

            fresh = [post for post in subreddit_posts if not self._store.has_seen(subreddit, post["id"])]
            self._store.mark_seen(subreddit, ids)
            if not fresh:
                continue

            self.stats.posts_seen += len(fresh)
            if len(fresh) >= 100:
                log.warning(
                    "r/%s returned a full page of unseen posts; posts may have been "
                    "missed. Lower POLL_INTERVAL or SUBREDDITS_PER_REQUEST.",
                    subreddit,
                )

            # Oldest first, so a burst arrives in Discord in chronological order.
            for post in sorted(fresh, key=lambda p: p.get("created_utc") or 0):
                await self._dispatch(subreddit, post)

    async def _dispatch(self, subreddit: str, post: dict[str, Any]) -> None:
        created = float(post.get("created_utc") or 0)
        if created and created < self._floor:
            return  # backlog from before this process started

        if created:
            self.stats.record_latency(max(time.time() - created, 0.0))

        for watch in self._store.watches_for(subreddit):
            if not watch.matches(post):
                continue
            try:
                await self._deliver(watch, post)
                self.stats.notifications_sent += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad channel must not stop the rest
                log.exception("Failed to deliver r/%s post %s", subreddit, post.get("id"))
