import tempfile
import time
import unittest
from pathlib import Path

from bot.config import Config
from bot.reddit import RedditError
from bot.store import Store, Watch
from bot.watcher import (
    REDDIT_REQUESTS_PER_MINUTE,
    Watcher,
    estimated_requests_per_minute,
)


def make_config(tmp: Path, **overrides) -> Config:
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
        data_dir=tmp,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def post(post_id, subreddit="python", *, title="hello", age=0, **extra):
    data = {
        "id": post_id,
        "subreddit": subreddit,
        "title": title,
        "selftext": "",
        "author": "someone",
        "permalink": f"/r/{subreddit}/comments/{post_id}/x/",
        "created_utc": time.time() - age,
    }
    data.update(extra)
    return data


class FakeReddit:
    """Serves scripted pages of /new, newest first, and records the calls."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []
        self.rate_limit_remaining = None

    async def fetch_new(self, subreddits, limit=100):
        self.calls.append(list(subreddits))
        page = self._pages.pop(0) if self._pages else []
        if isinstance(page, Exception):
            raise page
        return [p for p in page if p["subreddit"] in subreddits]


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.delivered = []

    def tearDown(self):
        self._tmp.cleanup()

    async def build(self, pages, watches, **config_overrides):
        store = Store(self.dir)
        store.load()
        for watch in watches:
            await store.add_watch(watch)
        reddit = FakeReddit(pages)

        async def deliver(watch, item):
            self.delivered.append((watch.subreddit, item["id"], watch.id))

        config = make_config(self.dir, **config_overrides)
        return store, reddit, Watcher(config, store, reddit, deliver)

    @staticmethod
    def watch(subreddit="python", **overrides):
        kwargs = dict(
            subreddit=subreddit,
            channel_id=1,
            dm_user_id=None,
            mention="<@1>",
            created_by=1,
            guild_id=1,
        )
        kwargs.update(overrides)
        return Watch.create(**kwargs)

    async def test_first_poll_seeds_without_notifying(self):
        _, _, watcher = await self.build([[post("a"), post("b")]], [self.watch()])
        await watcher._poll_once()
        self.assertEqual(self.delivered, [])

    async def test_second_poll_announces_only_the_new_post(self):
        pages = [[post("a")], [post("b"), post("a")]]
        _, _, watcher = await self.build(pages, [self.watch()])
        await watcher._poll_once()
        await watcher._poll_once()
        self.assertEqual([d[1] for d in self.delivered], ["b"])

    async def test_a_post_is_never_announced_twice(self):
        pages = [[post("a")], [post("b"), post("a")], [post("b"), post("a")]]
        _, _, watcher = await self.build(pages, [self.watch()])
        for _ in range(3):
            await watcher._poll_once()
        self.assertEqual([d[1] for d in self.delivered], ["b"])

    async def test_bursts_are_delivered_oldest_first(self):
        pages = [
            [post("a", age=100)],
            [post("d", age=1), post("c", age=2), post("b", age=3), post("a", age=100)],
        ]
        _, _, watcher = await self.build(pages, [self.watch()])
        await watcher._poll_once()
        await watcher._poll_once()
        self.assertEqual([d[1] for d in self.delivered], ["b", "c", "d"])

    async def test_backlog_older_than_max_post_age_is_skipped(self):
        pages = [[post("a")], [post("old", age=99999), post("a")]]
        _, _, watcher = await self.build(pages, [self.watch()], max_post_age=60)
        await watcher._poll_once()
        await watcher._poll_once()
        self.assertEqual(self.delivered, [])

    async def test_one_post_fans_out_to_every_matching_watch(self):
        watches = [self.watch(), self.watch(channel_id=2)]
        pages = [[post("a")], [post("b"), post("a")]]
        _, _, watcher = await self.build(pages, watches)
        await watcher._poll_once()
        await watcher._poll_once()
        self.assertEqual(len(self.delivered), 2)

    async def test_filters_are_applied_per_watch(self):
        matching = self.watch(keywords=["rust"])
        other = self.watch(channel_id=2, keywords=["golang"])
        pages = [[post("a")], [post("b", title="Rust rewrite"), post("a")]]
        _, _, watcher = await self.build(pages, [matching, other])
        await watcher._poll_once()
        await watcher._poll_once()
        self.assertEqual([d[2] for d in self.delivered], [matching.id])

    async def test_subreddits_are_batched_into_one_request(self):
        watches = [self.watch("python"), self.watch("rust"), self.watch("golang")]
        _, reddit, watcher = await self.build([[]], watches)
        await watcher._poll_once()
        self.assertEqual(reddit.calls, [["golang", "python", "rust"]])

    async def test_batching_respects_the_configured_size(self):
        watches = [self.watch(f"sub{i}") for i in range(5)]
        _, reddit, watcher = await self.build([[], []], watches, subreddits_per_request=3)
        await watcher._poll_once()
        self.assertEqual([len(call) for call in reddit.calls], [3, 2])

    async def test_a_new_subreddit_is_seeded_even_when_others_are_established(self):
        first = self.watch("python")
        store, reddit, watcher = await self.build(
            [[post("a", "python")], [post("b", "rust"), post("c", "python")]], [first]
        )
        await watcher._poll_once()
        await store.add_watch(self.watch("rust"))
        await watcher._poll_once()
        # c is new in an established subreddit; b only seeds r/rust.
        self.assertEqual([d[1] for d in self.delivered], ["c"])

    async def test_poll_errors_do_not_kill_the_loop_state(self):
        pages = [[post("a")], RedditError("boom"), [post("b"), post("a")]]
        _, _, watcher = await self.build(pages, [self.watch()])
        await watcher._poll_once()
        with self.assertRaises(RedditError):
            await watcher._poll_once()
        await watcher._poll_once()
        self.assertEqual([d[1] for d in self.delivered], ["b"])

    async def test_backoff_grows_with_consecutive_failures(self):
        _, _, watcher = await self.build([[]], [self.watch()], poll_interval=5)
        delays = []
        for count in range(1, 8):
            watcher.stats.consecutive_errors = count
            delays.append(watcher._backoff_delay())
        self.assertEqual(delays, sorted(delays))
        self.assertGreaterEqual(delays[-1], delays[0])
        self.assertEqual(delays[-1], 300)

    async def test_stats_track_work_done(self):
        pages = [[post("a")], [post("b"), post("a")]]
        _, _, watcher = await self.build(pages, [self.watch()])
        await watcher._poll_once()
        await watcher._poll_once()
        self.assertEqual(watcher.stats.polls, 2)
        self.assertEqual(watcher.stats.posts_seen, 1)
        self.assertEqual(watcher.stats.notifications_sent, 1)
        self.assertIsNotNone(watcher.stats.last_detection_latency)

    async def test_delivery_failure_does_not_stop_other_watches(self):
        store = Store(self.dir)
        store.load()
        bad = self.watch(channel_id=1)
        good = self.watch(channel_id=2)
        await store.add_watch(bad)
        await store.add_watch(good)

        seen = []

        async def deliver(watch, item):
            if watch.id == bad.id:
                raise RuntimeError("channel deleted")
            seen.append(item["id"])

        reddit = FakeReddit([[post("a")], [post("b"), post("a")]])
        watcher = Watcher(make_config(self.dir), store, reddit, deliver)
        await watcher._poll_once()
        await watcher._poll_once()
        self.assertEqual(seen, ["b"])

    async def test_no_watches_means_no_reddit_traffic(self):
        _, reddit, watcher = await self.build([[]], [])
        await watcher._poll_once()
        self.assertEqual(reddit.calls, [])


class RateBudgetTests(unittest.TestCase):
    def test_one_batch_at_five_seconds_is_twelve_requests_a_minute(self):
        self.assertEqual(estimated_requests_per_minute(25, 5, 25), 12)

    def test_the_default_settings_stay_well_inside_reddits_budget(self):
        # 25 subreddits on the shipped defaults: one batch, 12 req/min of 100.
        rate = estimated_requests_per_minute(25, 5, 25)
        self.assertLess(rate, REDDIT_REQUESTS_PER_MINUTE * 0.8)

    def test_a_partial_batch_still_costs_a_whole_request(self):
        self.assertEqual(estimated_requests_per_minute(26, 5, 25), 24)

    def test_no_subreddits_costs_nothing(self):
        self.assertEqual(estimated_requests_per_minute(0, 5, 25), 0.0)

    def test_many_subreddits_at_the_floor_exceeds_the_budget(self):
        # 200 subreddits = 8 batches = 96 req/min, over the 80% warning line.
        self.assertGreater(
            estimated_requests_per_minute(200, 5, 25), REDDIT_REQUESTS_PER_MINUTE * 0.8
        )

    def test_a_longer_interval_brings_it_back_under(self):
        self.assertLess(
            estimated_requests_per_minute(200, 15, 25), REDDIT_REQUESTS_PER_MINUTE * 0.8
        )


class BudgetWarningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def watcher_with(self, subreddit_count, **config_overrides):
        store = Store(self.dir)
        store.load()
        for i in range(subreddit_count):
            await store.add_watch(
                Watch.create(
                    subreddit=f"sub{i}",
                    channel_id=1,
                    dm_user_id=None,
                    mention=None,
                    created_by=1,
                    guild_id=1,
                )
            )

        async def deliver(watch, item):
            pass

        config = make_config(self.dir, **config_overrides)
        return Watcher(config, store, FakeReddit([]), deliver)

    async def test_no_warning_at_the_default_scale(self):
        watcher = await self.watcher_with(10, poll_interval=5)
        with self.assertNoLogs("bot.watcher", level="WARNING"):
            watcher._check_rate_budget(10)

    async def test_warns_when_the_budget_is_nearly_spent(self):
        watcher = await self.watcher_with(0, poll_interval=5)
        with self.assertLogs("bot.watcher", level="WARNING") as logs:
            watcher._check_rate_budget(200)
        self.assertIn("Reddit", logs.output[0])

    async def test_the_warning_is_not_repeated_every_poll(self):
        watcher = await self.watcher_with(0, poll_interval=5)
        with self.assertLogs("bot.watcher", level="WARNING"):
            watcher._check_rate_budget(200)
        with self.assertNoLogs("bot.watcher", level="WARNING"):
            watcher._check_rate_budget(200)

    async def test_the_warning_can_fire_again_after_recovering(self):
        watcher = await self.watcher_with(0, poll_interval=5)
        with self.assertLogs("bot.watcher", level="WARNING"):
            watcher._check_rate_budget(200)
        watcher._check_rate_budget(10)  # back under the line
        with self.assertLogs("bot.watcher", level="WARNING"):
            watcher._check_rate_budget(200)


if __name__ == "__main__":
    unittest.main()
