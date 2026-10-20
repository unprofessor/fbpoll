# fbpoll

Turns a public Facebook Page into an Atom feed by rendering Facebook's
logged-out Page Plugin embed in headless Chromium. You never load Facebook
yourself, and no account is involved.

## Setup

With uv (the script carries its own dependency metadata):

    uv run fbpoll.py --help
    uv run --with playwright playwright install chromium

With plain pip:

    pip install playwright && playwright install chromium

With Nix, take Playwright and its browsers from the same nixpkgs so their
versions match:

    nix shell nixpkgs#python3Packages.playwright nixpkgs#playwright-driver.browsers
    export PLAYWRIGHT_BROWSERS_PATH=$(nix eval --raw nixpkgs#playwright-driver.browsers)

## Use

    ./fbpoll.py LionSurplus --feed /srv/feeds/lion-surplus.xml \
        --match '(?i)computer|laptop|desktop|monitor|server|thinkpad|optiplex|dell|lenovo|mac'

- Writes the Atom feed, plus `<feed>.state.json` beside it.
- Prints each new matching post to stdout as one JSON line, ready to pipe
  into ntfy, mail and so on. The first run seeds state silently.
- `--match` filters only the feed and stdout. Every post is still recorded,
  so you can loosen the regex later without losing history.
- Facebook CDN image URLs expire after a few days. If your reader keeps old
  entries, add `--image-dir /srv/feeds/img --image-base-url https://host/feeds/img`.
- Exits with status 2 and leaves the feed untouched when it extracts nothing.
  That's your signal Facebook changed something. Rerun with
  `--debug-dir debug/` to get `page.html`, `screenshot.png` and `extracted.json`.

## systemd timer (twice daily is plenty for this page)

`~/.config/systemd/user/fbpoll.service`

    [Unit]
    Description=Poll Lion Surplus Facebook page

    [Service]
    Type=oneshot
    ExecStart=/bin/sh -c '%h/lion-surplus/fbpoll.py LionSurplus \
      --feed %h/feeds/lion-surplus.xml \
      --match "(?i)computer|laptop|desktop|monitor|server|thinkpad|optiplex" \
      | while read -r l; do curl -s -d "$l" ntfy.sh/YOUR-TOPIC; done'

`~/.config/systemd/user/fbpoll.timer`

    [Timer]
    OnCalendar=*-*-* 07,16:17
    RandomizedDelaySec=20m
    Persistent=true

    [Install]
    WantedBy=timers.target

Enable it with `systemctl --user enable --now fbpoll.timer`. If a run exits
with status 2, systemd marks the unit as failed, and `systemctl --user --failed`
will show it.

## How extraction works

The script ignores Facebook's class names, because they're obfuscated and change
often. Posts are located by timestamp elements (`[data-utime]`) or post permalinks.
Each post's container is the highest ancestor holding only one such marker. That
makes it reasonably resilient, but this is still scraping: expect to revisit it
occasionally.
