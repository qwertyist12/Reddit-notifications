"""Exercise the OAuth + request paths against a local stand-in for Reddit."""

import unittest

import aiohttp
from aiohttp import web

from bot import reddit as reddit_module
from bot.config import Config
from bot.reddit import RedditAuthError, RedditClient, RedditError


def make_config(**overrides) -> Config:
    kwargs = dict(
        discord_token="t",
        guild_ids=[],
        reddit_client_id="id",
        reddit_client_secret="secret",
        reddit_username=None,
        reddit_password=None,
        reddit_user_agent="test/1.0",
        poll_interval=15,
        subreddits_per_request=25,
        max_post_age=3600,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def listing(*posts):
    return {"data": {"children": [{"kind": "t3", "data": p} for p in posts]}}


class FakeReddit:
    """A tiny HTTP server that behaves like the bits of Reddit we call."""

    def __init__(self):
        self.token_requests = []
        self.api_requests = []
        self.token_status = 200
        self.new_status = 200
        self.expire_first_token = False
        self._issued = 0

    async def token(self, request: web.Request) -> web.Response:
        body = await request.post()
        self.token_requests.append(
            {
                "grant_type": body.get("grant_type"),
                "username": body.get("username"),
                "device_id": body.get("device_id"),
                "auth": request.headers.get("Authorization"),
                "user_agent": request.headers.get("User-Agent"),
            }
        )
        if self.token_status != 200:
            return web.Response(status=self.token_status, text="nope")
        self._issued += 1
        return web.json_response(
            {"access_token": f"token-{self._issued}", "expires_in": 3600}
        )

    async def new(self, request: web.Request) -> web.Response:
        self.api_requests.append(
            {
                "path": request.path,
                "query": dict(request.query),
                "auth": request.headers.get("Authorization"),
                "user_agent": request.headers.get("User-Agent"),
            }
        )
        # Simulate an expired token: reject the first bearer once.
        if self.expire_first_token and request.headers.get("Authorization") == "Bearer token-1":
            return web.Response(status=401, text="expired")
        if self.new_status != 200:
            return web.Response(status=self.new_status, text="error")
        headers = {"x-ratelimit-remaining": "42.0", "x-ratelimit-reset": "300"}
        return web.json_response(
            listing(
                {"id": "one", "subreddit": "python", "title": "First"},
                {"id": "two", "subreddit": "rust", "title": "Second"},
            ),
            headers=headers,
        )

    async def about(self, request: web.Request) -> web.Response:
        name = request.match_info["subreddit"]
        if name == "doesnotexist":
            return web.Response(status=404, text="not found")
        return web.json_response({"data": {"display_name": name}})


class RedditClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeReddit()
        app = web.Application()
        app.router.add_post("/api/v1/access_token", self.fake.token)
        app.router.add_get("/r/{subreddit}/new", self.fake.new)
        app.router.add_get("/r/{subreddit}/about", self.fake.about)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        self._originals = (reddit_module.TOKEN_URL, reddit_module.API_BASE)
        reddit_module.TOKEN_URL = f"{base}/api/v1/access_token"
        reddit_module.API_BASE = base

        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()
        reddit_module.TOKEN_URL, reddit_module.API_BASE = self._originals

    def client(self, **config_overrides) -> RedditClient:
        return RedditClient(make_config(**config_overrides), self.session)

    async def test_fetch_new_returns_post_data(self):
        posts = await self.client().fetch_new(["python", "rust"])
        self.assertEqual([p["id"] for p in posts], ["one", "two"])

    async def test_subreddits_are_joined_into_one_path(self):
        await self.client().fetch_new(["python", "rust", "golang"])
        self.assertEqual(self.fake.api_requests[0]["path"], "/r/python+rust+golang/new")

    async def test_requests_carry_bearer_token_and_user_agent(self):
        await self.client().fetch_new(["python"])
        request = self.fake.api_requests[0]
        self.assertEqual(request["auth"], "Bearer token-1")
        self.assertEqual(request["user_agent"], "test/1.0")

    async def test_token_is_reused_across_calls(self):
        client = self.client()
        await client.fetch_new(["python"])
        await client.fetch_new(["python"])
        self.assertEqual(len(self.fake.token_requests), 1)

    async def test_a_401_refreshes_the_token_and_retries_once(self):
        self.fake.expire_first_token = True
        posts = await self.client().fetch_new(["python"])
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(self.fake.token_requests), 2)

    async def test_client_credentials_grant_when_a_secret_is_set(self):
        await self.client().fetch_new(["python"])
        self.assertEqual(self.fake.token_requests[0]["grant_type"], "client_credentials")

    async def test_installed_client_grant_when_there_is_no_secret(self):
        await self.client(reddit_client_secret="").fetch_new(["python"])
        request = self.fake.token_requests[0]
        self.assertEqual(request["grant_type"], reddit_module.INSTALLED_CLIENT_GRANT)
        self.assertEqual(len(request["device_id"]), 30)

    async def test_password_grant_when_an_account_is_configured(self):
        client = self.client(reddit_username="me", reddit_password="pw")
        await client.fetch_new(["python"])
        request = self.fake.token_requests[0]
        self.assertEqual(request["grant_type"], "password")
        self.assertEqual(request["username"], "me")

    async def test_bad_credentials_raise_a_clear_auth_error(self):
        self.fake.token_status = 401
        with self.assertRaises(RedditAuthError) as ctx:
            await self.client().fetch_new(["python"])
        self.assertIn("REDDIT_CLIENT_ID", str(ctx.exception))

    async def test_server_errors_surface_as_reddit_errors(self):
        self.fake.new_status = 503
        with self.assertRaises(RedditError):
            await self.client().fetch_new(["python"])

    async def test_rate_limit_headers_are_recorded(self):
        client = self.client()
        await client.fetch_new(["python"])
        self.assertEqual(client.rate_limit_remaining, 42.0)
        self.assertEqual(client.rate_limit_reset, 300.0)

    async def test_empty_subreddit_list_makes_no_request(self):
        self.assertEqual(await self.client().fetch_new([]), [])
        self.assertEqual(self.fake.api_requests, [])

    async def test_subreddit_existence_check(self):
        client = self.client()
        self.assertTrue(await client.subreddit_exists("python"))
        self.assertFalse(await client.subreddit_exists("doesnotexist"))


if __name__ == "__main__":
    unittest.main()
