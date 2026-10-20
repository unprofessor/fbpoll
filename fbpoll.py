#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.40"]
# ///
"""
fbpoll — turn a public Facebook Page into an Atom feed, without logging in.

It loads Facebook's own "Page Plugin" embed (the widget sites use to show a
page's timeline), which renders a public Page's recent posts with no session.
Posts are extracted from the rendered DOM, merged into a small state file,
and written out as an Atom feed. New posts are also printed to stdout, one
JSON object per line, so a cron/systemd job can pipe them to ntfy, mail, etc.

Usage:
    fbpoll.py LionSurplus --feed lion-surplus.xml
    fbpoll.py LionSurplus --feed lion-surplus.xml \
        --match '(?i)computer|laptop|desktop|monitor|server|dell|lenovo|hp |mac'
    fbpoll.py LionSurplus --feed out.xml --debug-dir debug/   # when it breaks

Exit status: 0 on success, 2 if no posts could be extracted (the feed and
state are left untouched, so a broken run never empties your feed).

Facebook markup changes without notice. The extractor avoids class names and
keys off post timestamps / permalinks instead, but if it stops finding posts,
run with --debug-dir and look at page.html and screenshot.png.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_URL = "https://www.facebook.com/plugins/page.php"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
ATOM_NS = "http://www.w3.org/2005/Atom"

# Runs inside the rendered embed. Returns [{id, url, utime, text, images}].
#
# Strategy, most to least specific:
#   1. Post markers are timestamp elements ([data-utime]); failing that,
#      post permalinks (/posts/, permalink.php, story_fbid=); failing that,
#      any photo/video permalink.
#   2. A post's container is the highest ancestor of its marker that holds
#      no other marker. This needs no knowledge of Facebook's class names.
EXTRACT_JS = r"""
() => {
  const POSTISH = /\/posts\/|\/permalink\.php|\/story\.php|story_fbid=/;
  const MEDIA = /\/photos\/|\/photo\.php|\/photo\/|\/videos\/|\/reel\/|[?&]fbid=/;
  const KEEP_PARAMS = new Set(["story_fbid", "fbid", "id", "v"]);

  const norm = (href) => {
    try {
      const u = new URL(href, location.href);
      for (const k of [...u.searchParams.keys()])
        if (!KEEP_PARAMS.has(k)) u.searchParams.delete(k);
      u.hash = "";
      if (u.hostname.endsWith("facebook.com")) u.hostname = "www.facebook.com";
      return u.toString();
    } catch (e) { return null; }
  };
  const hrefOf = (a) => a.getAttribute("href") || "";
  const anchors = [...document.querySelectorAll("a[href]")];

  let markers = [...document.querySelectorAll("[data-utime]")];
  if (!markers.length) markers = anchors.filter(a => POSTISH.test(hrefOf(a)));
  if (!markers.length) markers = anchors.filter(a => MEDIA.test(hrefOf(a)));
  const markerSet = new Set(markers);

  const countMarkers = (el) => {
    let n = markerSet.has(el) ? 1 : 0;
    for (const m of markers) if (m !== el && el.contains(m)) n++;
    return n;
  };

  const containers = [];
  for (const m of markers) {
    let best = m;
    for (let p = m.parentElement; p && p !== document.body; p = p.parentElement) {
      if (countMarkers(p) > 1) break;
      best = p;
    }
    if (!containers.includes(best)) containers.push(best);
  }

  const textOf = (el) => (el.innerText !== undefined ? el.innerText : el.textContent) || "";
  const UI_LINE = /^(like|comment|share|see more|see less|send message|follow|· ?|\d+[km]?( (comments?|shares?|likes?|reactions?))?|write a comment.*|most relevant|all reactions:?)$/i;

  const posts = [];
  for (const c of containers) {
    const links = [...c.querySelectorAll("a[href]")];
    const permalink =
      links.find(a => POSTISH.test(hrefOf(a))) ||
      links.find(a => MEDIA.test(hrefOf(a))) ||
      (c.matches && c.matches("a[href]") ? c : null);
    const url = permalink ? norm(permalink.href) : null;

    const timeEl = c.matches("[data-utime]") ? c : c.querySelector("[data-utime]");
    const utime = timeEl ? parseInt(timeEl.getAttribute("data-utime"), 10) || null : null;

    // Prefer an explicit message node if Facebook still provides one.
    const msg = c.querySelector('[data-testid="post_message"], .userContent, [data-ad-preview="message"]');
    const body = msg || c;
    // Whole-container fallback: drop timestamp/permalink link lines ("October 18", "2h").
    const skip = new Set();
    if (!msg)
      for (const a of c.querySelectorAll("a[href]"))
        if (POSTISH.test(hrefOf(a)) && !a.querySelector("img")) {
          const t = textOf(a).replace(/\s+/g, " ").trim();
          if (t && t.length < 40) skip.add(t);
        }
    const text = textOf(body)
      .split("\n")
      .map(s => s.replace(/\s+/g, " ").trim())
      .filter(s => s && !UI_LINE.test(s) && !skip.has(s))
      .join("\n")
      .trim();

    const images = [];
    for (const img of c.querySelectorAll("img[src]")) {
      const src = img.getAttribute("src");
      if (!/scontent|fbcdn\.net/.test(src)) continue;
      if (/static\.xx\.fbcdn\.net|emoji|rsrc\.php/.test(src)) continue;
      const w = img.naturalWidth || img.width || parseInt(img.getAttribute("width") || "0", 10);
      if (w && w < 100) continue;            // avatars, reaction icons
      if (!images.includes(src)) images.push(src);
    }

    if (!text && !images.length) continue;
    posts.push({ url, utime, text, images });
  }
  return posts;
}
"""


# --------------------------------------------------------------------------- scrape

def plugin_url(page: str, height: int) -> str:
    page_url = page if page.startswith("http") else f"https://www.facebook.com/{page}"
    q = {
        "href": page_url,
        "tabs": "timeline",
        "width": "500",
        "height": str(height),
        "small_header": "true",
        "hide_cover": "true",
        "show_facepile": "false",
        "adapt_container_width": "false",
        "locale": "en_US",
    }
    return f"{PLUGIN_URL}?{urllib.parse.urlencode(q)}"


def scrape(page: str, height: int, scrolls: int, debug_dir: Path | None,
           chromium: str | None = None) -> list[dict]:
    from playwright.sync_api import sync_playwright

    url = plugin_url(page, height)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=chromium)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={"width": 500, "height": height},
        )
        pg = ctx.new_page()
        # Facebook's own script fails to mount the timeline, so capture the
        # renderer's markup and insert it ourselves before extracting.
        timelines: list[str] = []

        def on_response(r):
            if "/plugin/tab/renderer/" in r.url and r.ok:
                body = r.text().removeprefix("for (;;);")
                try:
                    timelines.append(json.loads(body)["payload"]["content"]["markup"]["__html"])
                except (ValueError, KeyError, TypeError):
                    pass

        pg.on("response", on_response)
        pg.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            pg.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass  # long-polling can keep the network busy; carry on

        # The timeline lazy-loads inside its own scroll container.
        for _ in range(scrolls):
            pg.evaluate(
                """() => {
                  for (const el of document.querySelectorAll('*'))
                    if (el.scrollHeight > el.clientHeight + 50) el.scrollTop = el.scrollHeight;
                  window.scrollTo(0, document.body.scrollHeight);
                }"""
            )
            pg.wait_for_timeout(1500)

        # Expand truncated posts where a "See more" control exists.
        for el in pg.get_by_text(re.compile(r"^See more$", re.I)).all()[:20]:
            try:
                el.click(timeout=1000)
            except Exception:
                pass

        if not pg.locator("[data-utime]").count():
            pg.evaluate(
                """(htmls) => {
                  for (const h of htmls) {
                    const d = document.createElement("div");
                    d.innerHTML = h;
                    document.body.appendChild(d);
                  }
                }""",
                timelines,
            )

        posts = pg.evaluate(EXTRACT_JS)

        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "url.txt").write_text(url + "\n")
            (debug_dir / "page.html").write_text(pg.content())
            pg.screenshot(path=str(debug_dir / "screenshot.png"), full_page=True)
            (debug_dir / "extracted.json").write_text(json.dumps(posts, indent=2))

        browser.close()
    return posts


# --------------------------------------------------------------------------- state

def post_id(p: dict) -> str:
    if p.get("url"):
        return p["url"]
    # No permalink: fall back to a content hash, ignoring image URLs (they rotate).
    return "sha1:" + hashlib.sha1(p.get("text", "").encode()).hexdigest()


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"posts": {}}


def save_atomic(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, path)


def merge(state: dict, scraped: list[dict], now: str, keep: int) -> list[dict]:
    """Merge scraped posts into state; return the ones not seen before."""
    known = state["posts"]
    new = []
    for p in scraped:
        pid = post_id(p)
        if pid in known:
            # Refresh text/images (edits, "See more" expansion) but keep first_seen.
            known[pid].update({k: p[k] for k in ("text", "images", "utime") if p.get(k)})
            continue
        rec = {**p, "id": pid, "first_seen": now}
        known[pid] = rec
        new.append(rec)

    # Bound the state file: keep the most recent `keep` posts.
    ordered = sorted(known.values(), key=sort_key, reverse=True)
    state["posts"] = {r["id"]: r for r in ordered[:keep]}
    return new


def sort_key(rec: dict) -> str:
    if rec.get("utime"):
        return datetime.fromtimestamp(rec["utime"], timezone.utc).isoformat()
    return rec["first_seen"]


# --------------------------------------------------------------------------- images

def localize_images(rec: dict, image_dir: Path, base_url: str) -> None:
    """Facebook CDN URLs expire after a few days; keep local copies."""
    out = []
    for src in rec.get("images", []):
        if not src.startswith("http") or "fbcdn" not in src and "scontent" not in src:
            out.append(src)
            continue
        path = urllib.parse.urlparse(src).path
        ext = os.path.splitext(path)[1] or ".jpg"
        name = hashlib.sha1(path.encode()).hexdigest()[:16] + ext
        dest = image_dir / name
        if not dest.exists():
            try:
                req = urllib.request.Request(src, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as r:
                    save_atomic(dest, r.read())
            except Exception as e:
                print(f"warning: image fetch failed: {e}", file=sys.stderr)
                out.append(src)
                continue
        out.append(base_url.rstrip("/") + "/" + name)
    rec["images"] = out


# --------------------------------------------------------------------------- feed

def build_feed(page: str, records: list[dict], now: str) -> bytes:
    ET.register_namespace("", ATOM_NS)
    a = lambda tag: f"{{{ATOM_NS}}}{tag}"  # noqa: E731
    page_url = page if page.startswith("http") else f"https://www.facebook.com/{page}"

    feed = ET.Element(a("feed"))
    ET.SubElement(feed, a("title")).text = f"{page} (Facebook)"
    ET.SubElement(feed, a("id")).text = page_url
    ET.SubElement(feed, a("updated")).text = now
    ET.SubElement(feed, a("link"), href=page_url)
    ET.SubElement(ET.SubElement(feed, a("author")), a("name")).text = page

    for rec in records:
        e = ET.SubElement(feed, a("entry"))
        first_line = (rec.get("text") or "").split("\n", 1)[0]
        title = first_line[:100] + ("…" if len(first_line) > 100 else "")
        ET.SubElement(e, a("title")).text = title or "(photo post)"
        ET.SubElement(e, a("id")).text = rec["id"]
        ET.SubElement(e, a("updated")).text = sort_key(rec)
        ET.SubElement(e, a("published")).text = sort_key(rec)
        if rec.get("url"):
            ET.SubElement(e, a("link"), href=rec["url"])

        parts = [f"<p>{html.escape(rec.get('text', '')).replace(chr(10), '<br/>')}</p>"]
        parts += [f'<p><img src="{html.escape(s, quote=True)}"/></p>' for s in rec.get("images", [])]
        ET.SubElement(e, a("content"), type="html").text = "\n".join(parts)

    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("page", help="Page slug (LionSurplus) or full Page URL")
    ap.add_argument("--feed", type=Path, required=True, help="Atom file to write")
    ap.add_argument("--state", type=Path, help="state JSON (default: <feed>.state.json)")
    ap.add_argument("--match", help="regex; only matching posts go in the feed / stdout")
    ap.add_argument("--keep", type=int, default=200, help="posts to remember (default 200)")
    ap.add_argument("--height", type=int, default=4000, help="embed height in px (default 4000)")
    ap.add_argument("--scrolls", type=int, default=3, help="lazy-load scroll passes (default 3)")
    ap.add_argument("--image-dir", type=Path, help="download post images here")
    ap.add_argument("--image-base-url", help="URL prefix the feed uses for --image-dir")
    ap.add_argument("--chromium", help="browser executable to use instead of Playwright's own build")
    ap.add_argument("--debug-dir", type=Path, help="dump page.html, screenshot, extraction")
    ap.add_argument("--from-json", type=Path, help=argparse.SUPPRESS)  # testing: skip browser
    args = ap.parse_args(argv)

    if args.image_dir and not args.image_base_url:
        ap.error("--image-dir needs --image-base-url")

    state_path = args.state or args.feed.with_name(args.feed.name + ".state.json")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    if args.from_json:
        scraped = json.loads(args.from_json.read_text())
    else:
        scraped = scrape(args.page, args.height, args.scrolls, args.debug_dir, args.chromium)

    if not scraped:
        print("error: no posts extracted; feed left unchanged "
              "(rerun with --debug-dir to see what Facebook served)", file=sys.stderr)
        return 2

    state = load_state(state_path)
    first_run = not state["posts"]
    new = merge(state, scraped, now, args.keep)

    if args.image_dir:
        for rec in new:
            localize_images(rec, args.image_dir, args.image_base_url)

    pattern = re.compile(args.match) if args.match else None
    wanted = lambda r: not pattern or bool(pattern.search(r.get("text", "")))  # noqa: E731

    records = sorted((r for r in state["posts"].values() if wanted(r)), key=sort_key, reverse=True)
    save_atomic(args.feed, build_feed(args.page, records, now))
    save_atomic(state_path, json.dumps(state, indent=2))

    # Don't announce the whole backlog on the very first run.
    if not first_run:
        for rec in sorted(new, key=sort_key):
            if wanted(rec):
                print(json.dumps({k: rec.get(k) for k in ("url", "text", "images", "first_seen")}))

    print(f"{len(scraped)} posts on page, {len(new)} new, {len(records)} in feed",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
