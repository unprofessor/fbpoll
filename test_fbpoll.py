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


def plugin_html(posts):
    """Page Plugin markup as served since Nov 2026: posts arrive as a JSON prop
    of PagePluginV2.react, with no data-utime or permalinks in the DOM."""
    props = {"pageID": "100064439029478", "pageName": "Lion Surplus", "timelinePosts": posts}
    blob = json.dumps(props).replace("/", "\\/")  # Facebook escapes slashes
    return (
        '<html><body><script>requireLazy(["ServerJS"],function(s){s.handle('
        '{"require":[["react-xhp","constructAndRenderComponentIntoComment_DO_NOT_USE",'
        '["PagePluginV2.react"],[{"props":' + blob + '}]]]})});</script></body></html>'
    )


PHOTO = "https://scontent-lga3-3.xx.fbcdn.net/v/t39.30808-6/1_2_n.jpg?stp=a&oh=b"
ALBUM = [
    "https://scontent-lga3-1.xx.fbcdn.net/v/t39.30808-6/3_4_n.jpg?x=1",
    "https://scontent-lga3-1.xx.fbcdn.net/v/t39.30808-6/5_6_n.jpg?x=2",
]


def timeline_post(message, created, kind="photo", **kw):
    p = {
        "message": message, "createdTime": created, "attachmentType": kind,
        "photoURL": PHOTO, "albumPhotoURLs": None, "albumPhotoCount": None,
        "attachedStory": None, "reactionCount": 1, "commentCount": 0, "shareCount": 0,
    }
    p.update(kw)
    return p


class TimelinePostsTest(unittest.TestCase):
    def test_extracts_text_timestamp_and_photo(self):
        html = plugin_html([timeline_post("Bleacher seats $100", 1794577744)])
        self.assertEqual(
            fbpoll.parse_timeline_posts(html),
            [{"url": None, "utime": 1794577744, "text": "Bleacher seats $100", "images": [PHOTO]}],
        )

    def test_album_uses_all_album_photos(self):
        html = plugin_html([timeline_post("Laptops", 1794572512, "album", albumPhotoURLs=ALBUM, albumPhotoCount=4)])
        (p,) = fbpoll.parse_timeline_posts(html)
        self.assertEqual(p["images"], ALBUM)

    def test_preserves_order_and_multiline_text(self):
        html = plugin_html([
            timeline_post("newest\nline two", 2),
            timeline_post("older", 1, photoURL=None),
        ])
        posts = fbpoll.parse_timeline_posts(html)
        self.assertEqual([p["utime"] for p in posts], [2, 1])
        self.assertEqual(posts[0]["text"], "newest\nline two")
        self.assertEqual(posts[1]["images"], [])

    def test_skips_empty_posts(self):
        html = plugin_html([timeline_post("", 3, photoURL=None)])
        self.assertEqual(fbpoll.parse_timeline_posts(html), [])

    def test_missing_or_garbled_blob_yields_nothing(self):
        self.assertEqual(fbpoll.parse_timeline_posts("<html>old markup</html>"), [])
        self.assertEqual(fbpoll.parse_timeline_posts('"timelinePosts":[{"message":'), [])
        self.assertEqual(fbpoll.parse_timeline_posts('"timelinePosts":null'), [])

    def test_timestamp_matches_existing_dedupe_key(self):
        # Old DOM path keyed posts as utime:<data-utime>; createdTime is the same
        # epoch, so posts seen before the switch must not be re-announced.
        state = {"posts": {}}
        fbpoll.merge(state, [post(OLD_URL, utime=1794572512)], "t1", 200)
        scraped = fbpoll.parse_timeline_posts(plugin_html([timeline_post(FRIDAY_TEXT, 1794572512)]))
        self.assertEqual(fbpoll.merge(state, scraped, "t2", 200), [])
        (rec,) = state["posts"].values()
        self.assertEqual(rec["url"], OLD_URL)  # a None url must not clobber the old link


if __name__ == "__main__":
    unittest.main()
