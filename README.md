# My Feed

A self-hosted, single-page news reader that merges several sources into one chronological feed: title, text, images / GIFs / video (shown as a collage, like X or VK), links, bookmarks, phrase search, date and source filters, and a gap-free archive. It runs on your own machine using only the Python 3.9+ standard library. A browser mode (Playwright) is optional and only needed for sites with bot protection and for the Odnoklassniki archive.

## What it is

My Feed is a small local web app that gathers news from the sites, Telegram channels and Odnoklassniki groups you follow into **one chronological feed**, so you do not have to visit each source separately. It is meant for personal use: it runs only on your computer, needs no account, and keeps everything in a local SQLite file.

## How it works

1. A Python server polls each source in the background (every 15 minutes and on page reload): RSS feeds, public Telegram pages (`t.me/s/...`), and Odnoklassniki groups through an optional headless browser.
2. Posts are normalised (title, text, pictures/video, original-article link), de-duplicated and stored in SQLite.
3. The page (`static/index.html`) reads them from a local JSON API and shows them newest first, with search, date and source filters, and bookmarks.
4. Older history is loaded lazily in the background: the last 30 days right away, the rest as you scroll.

## Installation

```
git clone <your-repo-url>
cd <repo>
python server.py
```

Python 3.9+ is the only requirement. For the Kinopoisk source (Odnoklassniki) and sites with bot protection, run `python server.py --install-browser` once to add Playwright + Chromium.

## Quick start

```
python server.py
```

Open http://127.0.0.1:8000. Reloading the page also polls the sources (at most once a minute); after that the feed refreshes itself every 15 minutes. The feed is served from the local SQLite database immediately (the first page is 20 items, then 30 at a time while scrolling, strictly by time). A refresh counts as finished once the fast sources (Telegram, RSS) have answered; slow ones that need a browser (Kinopoisk) finish in the background. The archive is loaded lazily: the last 30 days right away, older news as you scroll down.

Command-line options:

- `python server.py --refresh-once` – fetch the news once and print the state of every source.
- `python server.py --diagnose` – detailed diagnostics of what the sites return (attach the output when reporting a problem).
- `python server.py --install-browser` – install browser mode (Playwright + Chromium).

Requirements: Python 3.9+. No third-party packages are needed. Optional: `pip install playwright` (or just run `--install-browser`).

## Default sources

| Source | How it is read |
|---|---|
| Kinopoisk | Odnoklassniki group `ok.ru/kinopoisk`; the "Article" link points only to kinopoisk.ru, other links from the post are shown under the text |
| Hi-News.ru | Telegram channel `t.me/hinews_ru`; "Article" is the hi-news.ru link from the post, otherwise only the post link |
| Rozetked | two Telegram channels, `rozetked` and `rozetkedplus`, shown as one source in the sidebar and labelled "Rozetked" / "Rozetked Plus" in the feed; "Article" points to rozetked.me |
| Towards Data Science | RSS |
| KDnuggets | RSS (fallback feed / browser if the site blocks plain requests) |
| Machine Learning Mastery | RSS (fallback feed / browser if the site blocks plain requests) |

Posts tagged `#реклама` (Russian for "ad") are skipped for Hi-News and Rozetked, and the "Hi-News в MAX | Подписаться" footer is stripped. The Kinopoisk, Hi-News and Rozetked parsers match Russian-language content, so a few patterns in `server.py` are intentionally in Russian.

### Adding, editing and removing sources

Everything is done in the app. "＋ Add source" opens a dialog with a name and one link, or several links (several links are supported for Telegram channels, as with Rozetked: one source in the sidebar, a separate label per channel in the feed). Supported link types: RSS/Atom feeds and ordinary news sites, Telegram channels (`t.me/name`), public Odnoklassniki groups (`ok.ru/name`) and VK communities (`vk.ru/name`). "Edit" in the Sources dialog reopens the same dialog with the current links; "Remove" hides a source but keeps its data and bookmarks.

Telegram source options (stored in the source config): `channels`, `channel_names`, `skip` (regexes for posts to hide), `ext_hosts` (hosts of the original article), `noise` (lines to strip).

## Usage

The sidebar has the "All / Bookmarks" tabs, search (`"exact phrase"`, `-exclude`), date filters and the source list. Today's items show the time, older ones show the date. The star bookmarks an item. Clicking a picture in a collage opens a full-size viewer (← → Esc); GIFs and videos do not autoplay.

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `PORT` / `HOST` | `8000` / `127.0.0.1` | server address |
| `REFRESH_MINUTES` | `15` | refresh period |
| `NEWS_DB` | Windows: `%LOCALAPPDATA%\my-feed\news.db`, otherwise next to the script | database path |
| `VK_TOKEN` | empty | VK service token (only for VK sources) |
| `NEWS_INSECURE_SSL` | empty | `1` – skip SSL certificate verification |
| `NEWS_BROWSER_HEADED` | empty | `1` – show the browser-mode window |
| `NEWS_BROWSERS` | `2` | number of simultaneous browsers |
| `SOURCE_BUDGET` / `REFRESH_DEADLINE` | `150` / `300` | seconds per source / per whole refresh |
| `IMG_CACHE_MB` | `300` | image cache size |
| `BACKFILL_DAYS` | `30` | how many days back the archive is loaded right away; deeper on scroll-down or date pick (`0` = load the full archive) |

## Performance notes

- On start the console prints how long the database took to prepare; requests slower than 1.5 s are printed as `[slow] …`; every source shows its refresh time in the Sources dialog.
- The server starts immediately; TEMP and image-cache cleanup runs in the background afterwards.
- The "Refresh" button waits only for the fast sources (at most 40 s). Sources that need a browser or answer slower than 20 s are refreshed in the background, at most once per 20 minutes unless forced with the button.
- Archive loading and image completion start 30–45 s after launch so they do not compete with the first page load.

## Gaps in Kinopoisk (Odnoklassniki)

If a group publishes more posts between refreshes than fit on its first page, some posts could be missed. The app closes such gaps itself: after start and then every 3 hours (and right after a failed catch-up) it scrolls the group feed in a light browser back to posts older than `BACKFILL_DAYS` days and stores whatever is missing. The console shows `[kinopoisk] missed posts recovered: N`.

## Troubleshooting

- **`readonly database` / `database is locked`.** Another copy of the app, an antivirus or OneDrive is holding the database. Close extra `python.exe` processes. On Windows the database lives outside the Desktop by default. Writes are retried for up to ~30 s, and if needed the app switches to `~/.my-feed/news.db`. You can set your own path with `NEWS_DB=...`.
- **Port is busy** – another copy is running; close it or set `PORT=8001`.
- **A site blocks plain requests (403, Cloudflare).** Browser mode kicks in; if it is not installed, run `python server.py --install-browser`. Each source's status is shown in the Sources dialog.
- **SSL certificate error** – install the root certificates (`pip install certifi` helps) or run with `NEWS_INSECURE_SSL=1`.
- **Archive does not load** – check the source's archive status and attach the output of `--diagnose`.
- **Out of memory / browser crashes.** Only a limited number of browsers run at once and scrolling of long feeds is capped; a crashed browser is restarted.
- **Disk space.** The image cache is capped (`IMG_CACHE_MB`), temporary browser profiles are cleaned on start; you can also delete the `cache` folder next to the script.

## Project layout

```
server.py          HTTP server, parsers, archive loader, SQLite storage
static/index.html  the whole front end (vanilla JS, no build step)
tests/test_app.py  unit tests (no network access needed)
```

## Tests

```
python -m unittest discover -s tests
```

## License

MIT – see [LICENSE](LICENSE).
