import os
import unittest
from unittest import mock

from bot.config import Config, ConfigError

BASE_ENV = {"DISCORD_TOKEN": "t", "REDDIT_CLIENT_ID": "id"}


def with_env(**extra):
    env = dict(BASE_ENV)
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class ConfigTests(unittest.TestCase):
    def test_poll_interval_defaults_to_five_seconds(self):
        with with_env():
            self.assertEqual(Config.from_env().poll_interval, 5)

    def test_poll_interval_can_be_raised(self):
        with with_env(POLL_INTERVAL="30"):
            self.assertEqual(Config.from_env().poll_interval, 30)

    def test_poll_interval_below_the_floor_is_rejected(self):
        with with_env(POLL_INTERVAL="1"):
            with self.assertRaises(ConfigError) as ctx:
                Config.from_env()
        self.assertIn("POLL_INTERVAL", str(ctx.exception))

    def test_a_non_numeric_interval_is_rejected(self):
        with with_env(POLL_INTERVAL="fast"):
            with self.assertRaises(ConfigError):
                Config.from_env()

    def test_missing_required_values_are_named(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                Config.from_env()
        self.assertIn("DISCORD_TOKEN", str(ctx.exception))
        self.assertIn("REDDIT_CLIENT_ID", str(ctx.exception))

    def test_grant_type_follows_the_credentials(self):
        with with_env():
            self.assertEqual(Config.from_env().grant_type, "installed_client")
        with with_env(REDDIT_CLIENT_SECRET="s"):
            self.assertEqual(Config.from_env().grant_type, "client_credentials")
        with with_env(REDDIT_CLIENT_SECRET="s", REDDIT_USERNAME="u", REDDIT_PASSWORD="p"):
            self.assertEqual(Config.from_env().grant_type, "password")

    def test_guild_ids_are_parsed(self):
        with with_env(DISCORD_GUILD_IDS="1, 2;3"):
            self.assertEqual(Config.from_env().guild_ids, [1, 2, 3])

    def test_bad_guild_ids_are_rejected(self):
        with with_env(DISCORD_GUILD_IDS="not-an-id"):
            with self.assertRaises(ConfigError):
                Config.from_env()


if __name__ == "__main__":
    unittest.main()
