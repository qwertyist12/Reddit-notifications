import time
import unittest

from bot.formatting import build_embed, permalink


def post(**extra):
    data = {
        "id": "abc",
        "subreddit": "python",
        "title": "A new release",
        "selftext": "",
        "author": "guido",
        "permalink": "/r/python/comments/abc/a_new_release/",
        "url": "https://example.com/release",
        "created_utc": time.time() - 12,
    }
    data.update(extra)
    return data


class FormattingTests(unittest.TestCase):
    def test_permalink_is_absolute(self):
        self.assertEqual(
            permalink(post()), "https://reddit.com/r/python/comments/abc/a_new_release/"
        )

    def test_permalink_falls_back_to_url(self):
        self.assertEqual(permalink(post(permalink="")), "https://example.com/release")

    def test_embed_carries_title_author_and_link(self):
        embed = build_embed(post())
        self.assertEqual(embed.title, "A new release")
        self.assertIn("r/python", embed.author.name)
        self.assertIn("u/guido", embed.author.name)
        self.assertTrue(embed.url.startswith("https://reddit.com/"))

    def test_long_titles_are_truncated_to_discords_limit(self):
        embed = build_embed(post(title="x" * 400))
        self.assertLessEqual(len(embed.title), 256)
        self.assertTrue(embed.title.endswith("…"))

    def test_long_bodies_are_truncated(self):
        embed = build_embed(post(selftext="y" * 4000))
        self.assertLessEqual(len(embed.description), 500)

    def test_link_posts_show_their_destination(self):
        embed = build_embed(post())
        self.assertEqual(embed.description, "https://example.com/release")

    def test_self_posts_show_their_body_instead_of_the_url(self):
        embed = build_embed(
            post(selftext="Here is the changelog", url="https://www.reddit.com/r/python/x")
        )
        self.assertEqual(embed.description, "Here is the changelog")

    def test_flair_becomes_a_field(self):
        embed = build_embed(post(link_flair_text="Release"))
        self.assertIn("Flair", [field.name for field in embed.fields])

    def test_images_are_embedded(self):
        embed = build_embed(post(url="https://i.redd.it/foo.png"))
        self.assertEqual(embed.image.url, "https://i.redd.it/foo.png")

    def test_nsfw_media_is_not_embedded(self):
        embed = build_embed(post(url="https://i.redd.it/foo.png", over_18=True))
        self.assertIsNone(embed.image.url)
        self.assertIn("NSFW", [field.name for field in embed.fields])

    def test_footer_reports_detection_lag(self):
        created = time.time() - 30
        embed = build_embed(post(created_utc=created), detected_at=created + 9)
        self.assertIn("9s after posting", embed.footer.text)

    def test_a_sparse_post_still_renders(self):
        embed = build_embed({"id": "x", "subreddit": "python"})
        self.assertEqual(embed.title, "(no title)")

    def test_embed_stays_within_discords_total_size_limit(self):
        embed = build_embed(post(title="t" * 400, selftext="s" * 6000, link_flair_text="f" * 300))
        self.assertLessEqual(len(embed), 6000)


if __name__ == "__main__":
    unittest.main()
