"""End-to-end: the real watcher loop, the real client, a stand-in Reddit server."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from bot import reddit as reddit_module
from bot.formatting import build_embed
from bot.reddit import RedditClient
from bot.store import Store, Watch
from bot.watcher import Watcher
from tests.test_reddit_client import make_config


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.posts = []

        async def token(request):
            return web.json_response({"access_token": "tok", "expires_in": 3600})

        async def new(request):
            wanted = set(request.match_info["subreddit"].split("+"))
            children = [
                {"kind": "t3", "data": p} for p in self.posts if p["subreddit"] in wanted
            ]
            return web.json_response({"data": {"children": children}})

        app = web.Application()
        app.router.add_post("/api/v1/access_token", token)
        app.router.add_get("/r/{subreddit}/new", new)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        self._originals = (reddit_module.TOKEN_URL, reddit_module.API_BASE)
        reddit_module.TOKEN_URL = f"http://127.0.0.1:{port}/api/v1/access_token"
        reddit_module.API_BASE = f"http://127.0.0.1:{port}"

        self.session = aiohttp.ClientSession()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()
        self._tmp.cleanup()
        reddit_module.TOKEN_URL, reddit_module.API_BASE = self._originals

    def add_post(self, post_id, subreddit="python", **extra):
        data = {
            "id": post_id,
            "subreddit": subreddit,
            "title": f"Post {post_id}",
            "selftext": "",
            "author": "someone",
            "permalink": f"/r/{subreddit}/comments/{post_id}/x/",
            "created_utc": time.time(),
        }
        data.update(extra)
        self.posts.insert(0, data)  # /new is newest first

    async def test_new_post_reaches_discord_within_one_poll(self):
        store = Store(self.dir)
        store.load()
        await store.add_watch(
            Watch.create(
                subreddit="python",
                channel_id=1,
                dm_user_id=None,
                mention="<@42>",
                created_by=42,
                guild_id=7,
            )
        )

        delivered = []
        got_one = asyncio.Event()

        async def deliver(watch, post):
            # Render exactly as the bot would, so a formatting error would fail here.
            embed = build_embed(post, detected_at=time.time())
            delivered.append((watch.mention, post["id"], embed.title))
            got_one.set()

        config = make_config(poll_interval=1, data_dir=self.dir)
        reddit = RedditClient(config, self.session)
        watcher = Watcher(config, store, reddit, deliver)

        self.add_post("existing")  # already there before we start
        task = asyncio.create_task(watcher.run())
        try:
            # Wait for the seeding poll to land.
            for _ in range(100):
                if watcher.stats.polls >= 1:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(delivered, [], "pre-existing posts must not be announced")

            self.add_post("brandnew")
            watcher.nudge()
            await asyncio.wait_for(got_one.wait(), timeout=5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(delivered, [("<@42>", "brandnew", "Post brandnew")])
        self.assertEqual(watcher.stats.notifications_sent, 1)
        self.assertIsNone(watcher.stats.last_error)

        # State was persisted, so a fresh process would not re-announce it.
        reloaded = Store(self.dir)
        reloaded.load()
        self.assertTrue(reloaded.has_seen("python", "brandnew"))


if __name__ == "__main__":
    unittest.main()
