import json
import tempfile
import unittest
from pathlib import Path

from bot.store import SEEN_PER_SUBREDDIT, Store, Watch, normalize_subreddit


def make_watch(**overrides):
    kwargs = dict(
        subreddit="python",
        channel_id=123,
        dm_user_id=None,
        mention="<@1>",
        created_by=1,
        guild_id=9,
    )
    kwargs.update(overrides)
    return Watch.create(**kwargs)


class NormalizeTests(unittest.TestCase):
    def test_accepts_the_shapes_people_actually_type(self):
        for raw in (
            "python",
            "Python",
            "r/python",
            "/r/python",
            "R/Python/",
            "https://reddit.com/r/python",
            "https://www.reddit.com/r/python/",
        ):
            self.assertEqual(normalize_subreddit(raw), "python", raw)


class WatchFilterTests(unittest.TestCase):
    post = {
        "title": "Show HN: a new async library",
        "selftext": "It handles Backpressure nicely.",
        "link_flair_text": "Showcase",
        "author": "GuidoFan",
    }

    def test_no_filters_matches_everything(self):
        self.assertTrue(make_watch().matches(self.post))

    def test_keyword_matches_title_case_insensitively(self):
        self.assertTrue(make_watch(keywords=["ASYNC"]).matches(self.post))

    def test_keyword_matches_body(self):
        self.assertTrue(make_watch(keywords=["backpressure"]).matches(self.post))

    def test_keyword_miss_is_filtered_out(self):
        self.assertFalse(make_watch(keywords=["rust"]).matches(self.post))

    def test_any_keyword_is_enough(self):
        self.assertTrue(make_watch(keywords=["rust", "async"]).matches(self.post))

    def test_flair_is_a_substring_match(self):
        self.assertTrue(make_watch(flair="showcase").matches(self.post))
        self.assertFalse(make_watch(flair="release").matches(self.post))

    def test_author_ignores_prefix_and_case(self):
        self.assertTrue(make_watch(author="u/guidofan").matches(self.post))
        self.assertFalse(make_watch(author="someone_else").matches(self.post))

    def test_filters_combine_with_and(self):
        watch = make_watch(keywords=["async"], author="nobody")
        self.assertFalse(watch.matches(self.post))

    def test_missing_fields_do_not_crash(self):
        self.assertFalse(make_watch(keywords=["async"]).matches({}))
        self.assertTrue(make_watch().matches({}))


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_watches_survive_a_restart(self):
        store = Store(self.dir)
        store.load()
        watch = make_watch(keywords=["asyncio"])
        await store.add_watch(watch)

        reloaded = Store(self.dir)
        reloaded.load()
        self.assertEqual([w.id for w in reloaded.watches()], [watch.id])
        self.assertEqual(reloaded.watches()[0].keywords, ["asyncio"])
        self.assertEqual(reloaded.subreddits(), ["python"])

    async def test_seen_ids_survive_a_restart(self):
        store = Store(self.dir)
        store.load()
        await store.add_watch(make_watch())
        store.mark_seen("python", ["a", "b"])
        await store.flush_seen()

        reloaded = Store(self.dir)
        reloaded.load()
        self.assertTrue(reloaded.is_known_subreddit("python"))
        self.assertTrue(reloaded.has_seen("python", "a"))
        self.assertFalse(reloaded.has_seen("python", "zzz"))

    async def test_seen_set_is_bounded_and_keeps_the_newest(self):
        store = Store(self.dir)
        store.load()
        store.mark_seen("python", [str(i) for i in range(SEEN_PER_SUBREDDIT + 50)])
        await store.flush_seen()
        saved = json.loads((self.dir / "seen.json").read_text())["python"]
        self.assertEqual(len(saved), SEEN_PER_SUBREDDIT)
        self.assertIn(str(SEEN_PER_SUBREDDIT + 49), saved)
        self.assertNotIn("0", saved)

    async def test_removing_the_last_watch_drops_seen_state(self):
        store = Store(self.dir)
        store.load()
        watch = make_watch()
        await store.add_watch(watch)
        store.mark_seen("python", ["a"])
        await store.remove_watch(watch.id)
        self.assertFalse(store.is_known_subreddit("python"))
        self.assertEqual(store.subreddits(), [])

    async def test_seen_state_kept_while_another_watch_remains(self):
        store = Store(self.dir)
        store.load()
        first = make_watch()
        second = make_watch(channel_id=456)
        await store.add_watch(first)
        await store.add_watch(second)
        store.mark_seen("python", ["a"])
        await store.remove_watch(first.id)
        self.assertTrue(store.is_known_subreddit("python"))

    async def test_duplicate_detection_ignores_id(self):
        store = Store(self.dir)
        store.load()
        await store.add_watch(make_watch())
        self.assertIsNotNone(store.find_duplicate(make_watch()))
        self.assertIsNone(store.find_duplicate(make_watch(channel_id=999)))
        self.assertIsNone(store.find_duplicate(make_watch(keywords=["x"])))

    async def test_watches_for_is_case_insensitive(self):
        store = Store(self.dir)
        store.load()
        await store.add_watch(make_watch(subreddit="r/Python"))
        self.assertEqual(len(store.watches_for("PYTHON")), 1)


if __name__ == "__main__":
    unittest.main()
