"""Tests for fbpoll's post identity and dedupe. Run: python3 -m unittest -v"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import fbpoll

FRIDAY_TEXT = "Happy Friday everyone! Today we have all in one computers."
OLD_URL = "https://www.facebook.com/LionSurplus/posts/pfbid02ZEold"
NEW_URL = "https://www.facebook.com/LionSurplus/posts/pfbid0Vgunew"


def post(url, utime=1792150104, text=FRIDAY_TEXT):
    return {"url": url, "utime": utime, "text": text, "images": []}


class MergeTest(unittest.TestCase):
    def test_rotated_permalink_is_not_a_new_post(self):
        state = {"posts": {}}
        fbpoll.merge(state, [post(OLD_URL)], "t1", 200)
        new = fbpoll.merge(state, [post(NEW_URL)], "t2", 200)
        self.assertEqual(new, [])
        self.assertEqual(len(state["posts"]), 1)

    def test_rotated_permalink_refreshes_link_but_keeps_identity(self):
        state = {"posts": {}}
        fbpoll.merge(state, [post(OLD_URL)], "t1", 200)
        fbpoll.merge(state, [post(NEW_URL)], "t2", 200)
        (rec,) = state["posts"].values()
        self.assertEqual(rec["url"], NEW_URL)       # link keeps working
        self.assertEqual(rec["id"], OLD_URL)        # Atom id stays stable
        self.assertEqual(rec["first_seen"], "t1")

    def test_edited_text_is_not_a_new_post(self):
        state = {"posts": {}}
        fbpoll.merge(state, [post(OLD_URL)], "t1", 200)
        new = fbpoll.merge(state, [post(OLD_URL, text=FRIDAY_TEXT + " Edit!")], "t2", 200)
        self.assertEqual(new, [])
        (rec,) = state["posts"].values()
        self.assertTrue(rec["text"].endswith("Edit!"))

    def test_distinct_posts_are_both_new(self):
        state = {"posts": {}}
        new = fbpoll.merge(state, [post(OLD_URL, 1), post(NEW_URL, 2)], "t1", 200)
        self.assertEqual(len(new), 2)

    def test_posts_without_timestamp_fall_back_to_url(self):
        state = {"posts": {}}
        fbpoll.merge(state, [post(OLD_URL, utime=None)], "t1", 200)
        self.assertEqual(fbpoll.merge(state, [post(OLD_URL, utime=None)], "t2", 200), [])
        self.assertEqual(len(fbpoll.merge(state, [post(NEW_URL, utime=None)], "t3", 200)), 1)


class MigrationTest(unittest.TestCase):
    def legacy_state(self):
        # State as written by the URL-keyed version, including a duplicate
        # caused by a rotated permalink.
        a = {**post(OLD_URL), "id": OLD_URL, "first_seen": "t1"}
        b = {**post(NEW_URL), "id": NEW_URL, "first_seen": "t5"}
        return {"posts": {OLD_URL: a, NEW_URL: b}}

    def test_legacy_duplicates_collapse_to_earliest(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(self.legacy_state()))
            state = fbpoll.load_state(p)
        (rec,) = state["posts"].values()
        self.assertEqual(rec["first_seen"], "t1")
        self.assertEqual(rec["id"], OLD_URL)
        self.assertEqual(rec["url"], NEW_URL)

    def test_legacy_state_does_not_reannounce(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            feed = d / "feed.xml"
            Path(str(feed) + ".state.json").write_text(json.dumps(self.legacy_state()))
            scraped = d / "scraped.json"
            scraped.write_text(json.dumps([post("https://www.facebook.com/LionSurplus/posts/pfbid0third")]))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = fbpoll.main(["LionSurplus", "--feed", str(feed), "--from-json", str(scraped)])
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue(), "")
            saved = json.loads(Path(str(feed) + ".state.json").read_text())
            self.assertEqual(len(saved["posts"]), 1)


if __name__ == "__main__":
    unittest.main()
