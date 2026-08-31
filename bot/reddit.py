"""A small async Reddit client: OAuth token handling plus batched `/new` reads.

Only the read paths we need are implemented. The important trick for latency is
`fetch_new`: Reddit lets you address several subreddits at once as
`/r/python+rust+golang/new`, so watching thirty subreddits still costs about one
HTTP request per poll and we can afford to poll every few seconds.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from typing import Any

import aiohttp

from .config import Config

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
INSTALLED_CLIENT_GRANT = "https://oauth.reddit.com/grants/installed_client"

# Refresh the token this long before it actually expires.
TOKEN_REFRESH_MARGIN = 120


class RedditError(RuntimeError):
    """A Reddit API call failed in a way the caller should know about."""


class RedditAuthError(RedditError):
    """Credentials were rejected. Retrying will not help until they are fixed."""


class RedditClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        # Reddit requires a device id of 20-30 characters for the installed-client grant.
        self._device_id = uuid.uuid4().hex[:30]
        self.rate_limit_remaining: float | None = None
        self.rate_limit_reset: float | None = None

    # ----------------------------------------------------------------- auth

    async def _ensure_token(self, *, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.time() < self._token_expires_at:
                return self._token

            cfg = self._config
            grant = cfg.grant_type
            if grant == "password":
                data = {
                    "grant_type": "password",
                    "username": cfg.reddit_username,
                    "password": cfg.reddit_password,
                }
            elif grant == "client_credentials":
                data = {"grant_type": "client_credentials"}
            else:
                data = {"grant_type": INSTALLED_CLIENT_GRANT, "device_id": self._device_id}

            # Built by hand rather than via aiohttp.BasicAuth, which is deprecated
            # in aiohttp 3.12+ and removed in 4.0.
            credentials = f"{cfg.reddit_client_id}:{cfg.reddit_client_secret or ''}"
            encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
            headers = {
                "Authorization": f"Basic {encoded}",
                "User-Agent": cfg.reddit_user_agent,
            }

            async with self._session.post(
                TOKEN_URL, data=data, headers=headers
            ) as response:
                body = await response.text()
                if response.status in (401, 403):
                    raise RedditAuthError(
                        f"Reddit rejected the {grant} credentials (HTTP {response.status}). "
                        "Check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET and, if set, "
                        "REDDIT_USERNAME / REDDIT_PASSWORD."
                    )
                if response.status != 200:
                    raise RedditError(f"Token request failed: HTTP {response.status}: {body[:200]}")
                payload = await response.json()

            token = payload.get("access_token")
            if not token:
                raise RedditAuthError(f"Token response contained no access_token: {payload}")

            expires_in = float(payload.get("expires_in", 3600))
            self._token = token
            self._token_expires_at = time.time() + max(expires_in - TOKEN_REFRESH_MARGIN, 60)
            log.info("Obtained Reddit token via %s grant (expires in %.0fs)", grant, expires_in)
            return token

    # ---------------------------------------------------------------- request

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(2):
            token = await self._ensure_token(force=attempt > 0)
            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": self._config.reddit_user_agent,
            }
            async with self._session.get(
                f"{API_BASE}{path}", params=params, headers=headers
            ) as response:
                self._record_rate_limit(response.headers)

                if response.status == 401 and attempt == 0:
                    log.warning("Reddit returned 401; refreshing token and retrying once")
                    continue
                if response.status == 429:
                    retry_after = float(response.headers.get("retry-after", "5") or 5)
                    raise RedditError(f"Rate limited by Reddit; retry after {retry_after:.0f}s")
                if response.status >= 500:
                    raise RedditError(f"Reddit server error: HTTP {response.status}")
                if response.status != 200:
                    text = await response.text()
                    raise RedditError(f"GET {path} failed: HTTP {response.status}: {text[:200]}")
                return await response.json()

        raise RedditError(f"GET {path} failed after refreshing the token")

    def _record_rate_limit(self, headers: Any) -> None:
        try:
            remaining = headers.get("x-ratelimit-remaining")
            reset = headers.get("x-ratelimit-reset")
            if remaining is not None:
                self.rate_limit_remaining = float(remaining)
            if reset is not None:
                self.rate_limit_reset = float(reset)
        except (TypeError, ValueError):
            pass

    # -------------------------------------------------------------- reading

    async def fetch_new(self, subreddits: list[str], limit: int = 100) -> list[dict[str, Any]]:
        """Newest posts across `subreddits`, fetched as one combined request.

        Returns the raw `data` dicts from Reddit, newest first.
        """
        if not subreddits:
            return []
        path = f"/r/{'+'.join(subreddits)}/new"
        payload = await self._get(path, {"limit": limit, "raw_json": 1})
        children = payload.get("data", {}).get("children", [])
        return [child["data"] for child in children if child.get("kind") == "t3"]

    async def subreddit_exists(self, subreddit: str) -> bool:
        """Check that a subreddit is real and readable before we start watching it."""
        try:
            payload = await self._get(f"/r/{subreddit}/about", {"raw_json": 1})
        except RedditError as exc:
            log.info("Existence check for r/%s failed: %s", subreddit, exc)
            return False
        data = payload.get("data", {})
        # Private/banned subreddits still resolve but cannot be read.
        return data.get("display_name", "").lower() == subreddit.lower()
