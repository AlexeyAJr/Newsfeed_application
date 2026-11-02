#!/usr/bin/env python3
"""News feed: collects news from several sources + web server.

Python 3.9+ standard library only. Run:  python server.py
Then open http://127.0.0.1:8000
"""
import functools
import gzip
import hashlib
import html
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE, "static")
DEFAULT_DB = os.path.join(BASE, "news.db")


def _default_db():
    """On Windows the Desktop/Documents are often synced by OneDrive and scanned by antivirus - SQLite fails there with "readonly".
    By default the database lives in %LOCALAPPDATA%\\my-feed (outside sync)."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        d = os.path.join(os.environ["LOCALAPPDATA"], "my-feed")
        try:
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, "news.db")
        except OSError:
            pass
    return DEFAULT_DB


DB_PATH = os.environ.get("NEWS_DB") or _default_db()
IMG_DIR = os.path.join(BASE, "cache")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "15"))
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_API_HOST = os.environ.get("VK_API_HOST", "api.vk.com")
VK_TZ_HOURS = float(os.environ.get("VK_TZ_HOURS", "3"))  # timezone of dates on m.vk.ru (Moscow by default)
SOURCE_BUDGET = int(os.environ.get("SOURCE_BUDGET", "150"))  # seconds allowed for refreshing one source
BACKFILL_DELAY = float(os.environ.get("BACKFILL_DELAY", "1.0"))
CATCHUP_PAGES = 60      # max pages to walk back so no gaps remain after downtime
TEXT_MAX = 4000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"
FIREFOX_MOBILE_UA = "Mozilla/5.0 (Android 13; Mobile; rv:125.0) Gecko/125.0 Firefox/125.0"
OPERA_MINI_UA = "Opera/9.80 (Android; Opera Mini/36.2.2254/119.132; U; ru) Presto/2.12.423 Version/12.16"
VK_UAS = [None, FIREFOX_UA, FIREFOX_MOBILE_UA, OPERA_MINI_UA]  # None -> MOBILE_UA; VK serves an "update your browser" page to some UAs
VK_UA_OK = {}  # which User-Agent worked for a group
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")

PALETTE = ["#2a9d8f", "#e76f51", "#f4a261", "#3a86ff", "#06a77d", "#8d99ae", "#d62828", "#9b5de5",
           "#ef476f", "#118ab2", "#8ac926", "#ff9f1c"]

HINEWS_TG = dict(channel="hinews_ru", ext_hosts=["hi-news.ru"], skip=[r"#\s*реклама"],
                 noise=[r"^.*Hi-News\s+в\s+MAX.*$", r"^\s*Подписаться\s*$"])
ROZETKED_TG = dict(channel="rozetked", channels=["rozetked", "rozetkedplus"], ext_hosts=["rozetked.me"], skip=[r"#\s*реклама"],
                   channel_names={"rozetked": "Rozetked", "rozetkedplus": "Rozetked Plus"})

# Default sources (can be added and removed in the database - see the UI).
DEFAULT_SOURCES = [
    dict(id="kinopoisk", name="Kinopoisk", type="ok", url="https://ok.ru/kinopoisk", config=dict(domain="kinopoisk")),
    dict(id="hinews", name="Hi-News.ru", type="telegram", url="https://t.me/hinews_ru", config=HINEWS_TG),
    dict(id="rozetked", name="Rozetked", type="telegram", url="https://rozetked.me", config=ROZETKED_TG),
    dict(id="tds", name="Towards Data Science", type="rss", url="https://towardsdatascience.com/latest/",
         config=dict(feed="https://towardsdatascience.com/feed/")),
    dict(id="kdnuggets", name="KDnuggets", type="rss", url="https://www.kdnuggets.com/news/index.html",
         config=dict(feed="https://www.kdnuggets.com/feed",
                     alt_feeds=["https://feeds.feedburner.com/kdnuggets-data-mining-analytics"])),
    dict(id="mlm", name="Machine Learning Mastery", type="rss", url="https://machinelearningmastery.com/blog/",
         config=dict(feed="https://machinelearningmastery.com/feed/",
                     alt_feeds=["https://machinelearningmastery.com/blog/feed/", "https://feeds.feedburner.com/MachineLearningMastery"])),
]

# ---------------------------------------------------------------- utilities


INSECURE_SSL = os.environ.get("NEWS_INSECURE_SSL") == "1"


def _make_ssl_context():
    """Certificates: system ones + certifi if installed. NEWS_INSECURE_SSL=1 - skip verification (only for reading public news)."""
    ctx = ssl.create_default_context()
    if INSECURE_SSL:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


SSL_CTX = _make_ssl_context()


def is_ssl_error(e):
    return "CERTIFICATE_VERIFY_FAILED" in str(e) or isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError)


def http_get(url, timeout=25, headers=None, max_bytes=None, ua=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua or UA,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.8",
        "Accept-Encoding": "gzip",
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        data = r.read(max_bytes) if max_bytes else r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            try:
                data = gzip.decompress(data)
            except (OSError, EOFError):
                pass  # gzip truncated by max_bytes - use as is
        charset = r.headers.get_content_charset() or "utf-8"
    return data, charset


def http_text(url, **kw):
    data, charset = http_get(url, **kw)
    return data.decode(charset, errors="replace")


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "li":
            self.out.append("\n• ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK or tag == "li":
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


_STRIP_ALWAYS = [
    r"<script\b.*?</script>", r"<style\b.*?</style>", r"<figcaption\b.*?</figcaption>",
    r'<p[^>]*wp-caption-text[^>]*>.*?</p>',
    r"<svg\b.*?</svg>", r"<noscript\b.*?</noscript>", r"<template\b.*?</template>", r"<!--.*?-->",
]


def html_to_text(s, strip_code=False):
    s = s or ""
    for pat in _STRIP_ALWAYS + ([r"<code\b.*?</code>"] if strip_code else []):
        s = re.sub(pat, " ", s, flags=re.S | re.I)
    s = re.sub(r"<svg\b.*$", " ", s, flags=re.S | re.I)  # unclosed svg (page chunk was cut off)
    s = re.sub(r"<[a-zA-Z/!][^<>]*$", " ", s)  # dangling tag at the end - otherwise HTMLParser emits it as text
    p = _TextExtractor()
    p.feed(s)
    p.close()
    t = "".join(p.out)
    t = re.sub(r"<path\b[^>]*>?|\bfill-rule=\"[^\"]*\"|\bclip-rule=\"[^\"]*\"|\bd=\"[MmLlCcZz][^\"]*\"?", " ", t).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def parse_date(s):
    """Date string (RFC 822 or ISO 8601) -> unix timestamp (UTC) or None."""
    if not s:
        return None
    s = s.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


_RU_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7,
              "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}


def parse_ru_date(text, now=None, tz_hours=None):
    """Russian relative/absolute dates ("сегодня в 12:30", "вчера в 9:05", "6 окт в 12:30", "6 окт 2025", "5 минут назад") -> unix timestamp."""
    tz = timezone(timedelta(hours=VK_TZ_HOURS if tz_hours is None else tz_hours))
    now_dt = datetime.fromtimestamp(now if now is not None else time.time(), tz)
    t = (text or "").strip().lower().replace("\xa0", " ")
    if not t:
        return None
    if "только что" in t or "сейчас" in t:
        return int(now_dt.timestamp())
    m = re.search(r"(?:(\d+)\s*)?(секунд|сек|минут|мин|час|ч|д|дн|день|дня|дней)\w*\.?\s+назад", t)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        unit = {"с": 1, "м": 60, "ч": 3600, "д": 86400}[m.group(2)[0]]
        return int(now_dt.timestamp()) - n * unit
    hm = re.search(r"(\d{1,2}):(\d{2})", t)
    hh, mm = (int(hm.group(1)), int(hm.group(2))) if hm else (12, 0)
    day = None
    if "позавчера" in t:
        day = now_dt - timedelta(days=2)
    elif "вчера" in t:
        day = now_dt - timedelta(days=1)
    elif "сегодня" in t:
        day = now_dt
    else:
        m = re.search(r"(\d{1,2})\s+([а-я]{3,})\.?(?:\s+(\d{4}))?", t)
        if m and m.group(2)[:3] in _RU_MONTHS:
            year = int(m.group(3)) if m.group(3) else now_dt.year
            try:
                day = datetime(year, _RU_MONTHS[m.group(2)[:3]], int(m.group(1)), tzinfo=tz)
            except ValueError:
                return None
            if not m.group(3) and day > now_dt + timedelta(days=1):
                day = day.replace(year=year - 1)
        else:
            m = re.search(r"(\d{1,2})\.(\d{2})\.(\d{4})", t)
            if m:
                try:
                    day = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=tz)
                except ValueError:
                    return None
    if day is None:
        return None
    return int(day.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())


def split_title_text(text, limit=160):
    """First line -> title, the rest -> text."""
    text = (text or "").strip()
    if not text:
        return "", ""
    first, _, rest = text.partition("\n")
    first = first.strip()
    if 0 < len(first) <= limit:
        return first, rest.strip()
    cut = first[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return cut, text


TEASER_MAX = 500
_JUNK_IMG = re.compile(r"emoji|avatar|gravatar|1x1|pixel|spacer|blank\.|tracking|counter|/icons?/|smil(?:e|ies)|badge|feedburner", re.I)


def _teaser(text, limit=TEASER_MAX):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-—") + "…"


def _media_key(u):
    """The same image in different sizes (...-300x200.jpg) and with parameters - one key."""
    u = re.sub(r"[?#].*$", "", u)
    return re.sub(r"-\d{2,4}x\d{2,4}(?=\.\w{3,4}$)", "", u)


def collect_media(snippets, base="", limit=8):
    """All images, gifs (.gif) and videos (<video>, <source>) from HTML chunks -> [{"u","t","p"}], without duplicates."""
    out, seen = [], set()

    def add(u, t, poster=None):
        u = urllib.parse.urljoin(base, html.unescape(u or "").strip())
        if not u.startswith(("http://", "https://")) or _media_key(u) in seen or len(out) >= limit:
            return
        seen.add(_media_key(u))
        e = {"u": u, "t": t}
        if poster:
            e["p"] = urllib.parse.urljoin(base, html.unescape(poster))
        out.append(e)
    for sn in snippets:
        sn = sn or ""
        for m in re.finditer(r"<img\b[^>]*>", sn, re.I):
            tag = m.group(0)
            mu = re.search(r'\b(?:data-src|data-lazy-src|data-original|src)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if not mu or mu.group(1).startswith("data:") or _JUNK_IMG.search(mu.group(1)):
                continue
            small = False
            for attr in ("width", "height"):
                v = re.search(r'\b' + attr + r'\s*=\s*["\']?(\d+)', tag, re.I)
                if v and int(v.group(1)) <= 100:
                    small = True
            if not small:
                add(mu.group(1), "image")
        for m in re.finditer(r"<video\b([^>]*)>(.*?)</video>|<video\b([^>]*)/?>", sn, re.I | re.S):
            attrs, inner = (m.group(1) or m.group(3) or ""), (m.group(2) or "")
            mu = re.search(r'\bsrc\s*=\s*["\']([^"\']+)', attrs, re.I) or re.search(r'<source\b[^>]*\bsrc\s*=\s*["\']([^"\']+)', inner, re.I)
            mp = re.search(r'\bposter\s*=\s*["\']([^"\']+)', attrs, re.I)
            if mu:
                add(mu.group(1), "video", mp.group(1) if mp else None)
    return out


def make_id(source_id, key):
    return hashlib.sha1(f"{source_id}|{key}".encode("utf-8")).hexdigest()[:16]


def norm_search(s):
    return (s or "").casefold().replace("ё", "е")


# ---------------------------------------------------------------- parsers


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _parse_xml(xml_bytes):
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError:
        # feed cut off mid-way - keep everything up to the last complete <item>/<entry>
        text = xml_bytes.decode("utf-8", errors="replace") if isinstance(xml_bytes, bytes) else xml_bytes
        text = re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip())
        end = max(text.rfind("</item>"), text.rfind("</entry>"))
        if end < 0:
            raise
        tail = "</channel></rss>" if "<rss" in text[:500] else "</feed>"
        return ET.fromstring(text[:end + (7 if text.rfind("</item>") >= text.rfind("</entry>") else 8)] + tail)


def parse_feed(xml_bytes, base_url="", strip_code=False, teaser=True):
    """RSS 2.0 / Atom -> list of news dicts. teaser: the text is a teaser (as on the site home page), not the whole article."""
    root = _parse_xml(xml_bytes)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        d = {"media": None, "extra": []}
        for ch in el:
            n = _local(ch.tag)
            txt = ch.text or ""
            if n == "title":
                d["title"] = txt
            elif n == "link":
                href = ch.get("href")
                if href:
                    if ch.get("rel") in (None, "alternate"):
                        d["link"] = href
                else:
                    d["link"] = txt.strip()
            elif n in ("pubDate", "published"):
                d["date"] = txt
            elif n in ("updated", "date"):
                d.setdefault("date_alt", txt)
            elif n in ("description", "summary"):
                d["desc"] = txt
            elif n == "encoded":
                d["content"] = txt
            elif n == "content":
                if ch.get("url"):
                    kind = ch.get("medium") or ("video" if (ch.get("type") or "").startswith("video") else "image")
                    if kind == "image" and not d["media"]:
                        d["media"] = ch.get("url")
                    elif kind in ("image", "video"):
                        d["extra"].append((ch.get("url"), kind))
                else:
                    d["content"] = txt
            elif n == "thumbnail" and ch.get("url") and not d["media"]:
                d["media"] = ch.get("url")
            elif n == "enclosure" and (ch.get("type") or "").startswith(("image", "video")):
                if (ch.get("type") or "").startswith("image") and not d["media"]:
                    d["media"] = ch.get("url")
                elif ch.get("url"):
                    d["extra"].append((ch.get("url"), "video" if ch.get("type").startswith("video") else "image"))
            elif n in ("guid", "id"):
                d["guid"] = txt.strip()
        link = urllib.parse.urljoin(base_url, d.get("link", "")) if d.get("link") else None
        has_desc = bool((d.get("desc") or "").strip())
        body_html = (d.get("desc") if teaser and has_desc else d.get("content") or d.get("desc")) or ""
        text = html_to_text(body_html, strip_code=strip_code)
        text = re.sub(r"\s*The post .{0,300}? appeared first on .{0,100}$", "", text, flags=re.S).strip()
        if teaser:  # teaser: either the site's description or the start of the article
            text = _teaser(text, TEASER_MAX if has_desc else 350)
        if len(text) > TEXT_MAX:
            text = text[:TEXT_MAX].rsplit(" ", 1)[0] + "…"
        title = html.unescape(d.get("title", "")).strip()
        body_only = False
        media = collect_media([f'<img src="{d["media"]}">'] if d["media"] else [], base_url)
        for u, kind in d.get("extra", []):  # remaining attachments of the feed entry itself (several media:content / enclosure)
            media += [{"u": urllib.parse.urljoin(base_url, u), "t": kind}]
        if teaser:  # only what the entry itself has (teaser); the full article with other images stays on the site
            snippets = [d.get("desc", "")]
            body_only = not media and not collect_media(snippets, base_url)
            if body_only:
                snippets.append(d.get("content", ""))
        else:
            snippets = [d.get("content", ""), d.get("desc", "")]
        have = {_media_key(x["u"]) for x in media}
        for m in collect_media(snippets, base_url):
            if _media_key(m["u"]) not in have:
                have.add(_media_key(m["u"])); media.append(m)
        media = media[:8]
        if teaser and body_only:
            media = media[:1]  # the entry has no images of its own: take only the main one from the article body
        image = next((m["u"] for m in media if m["t"] == "image"), None) or next((m.get("p") for m in media if m.get("p")), None)
        if not title and not text:
            continue
        out.append(dict(
            guid=d.get("guid") or link or title,
            title=title or split_title_text(text)[0],
            text=text if text != title else "",
            url=link, ext_url=None, image=image, media=media,
            published=parse_date(d.get("date") or d.get("date_alt")),
        ))
    return out


def feed_title(xml_bytes):
    try:
        root = _parse_xml(xml_bytes)
    except ET.ParseError:
        return ""
    for el in root.iter():
        if _local(el.tag) == "title" and el.text:
            return html.unescape(el.text).strip()
    return ""


_TG_IMG = [
    r'tgme_widget_message_photo_wrap[^>]*?background-image:\s*url\([\'"]?([^\'")]+)',
    r'tgme_widget_message_video_thumb[^>]*?background-image:\s*url\([\'"]?([^\'")]+)',
    r'tgme_widget_message_roundvideo_thumb[^>]*?background-image:\s*url\([\'"]?([^\'")]+)',
    r'link_preview_(?:right_)?image[^>]*?background-image:\s*url\([\'"]?([^\'")]+)',
]


_TG_PHOTO = r'tgme_widget_message_photo_wrap[^>]*?background-image:\s*url\([\'"]?([^\'")]+)'
_TG_THUMB = r'tgme_widget_message_(?:round)?video_thumb[^>]*?background-image:\s*url\([\'"]?([^\'")]+)'


def _tg_media(chunk):
    """All photos and videos/gifs of a message (album = several photo_wrap; gifs in Telegram are silent mp4)."""
    media = [{"u": html.unescape(u), "t": "image"} for u in re.findall(_TG_PHOTO, chunk, re.S)]
    thumbs = [html.unescape(u) for u in re.findall(_TG_THUMB, chunk, re.S)]
    vids = [html.unescape(u) for u in re.findall(r'<video\b[^>]*\bsrc="([^"]+)"', chunk, re.S)]
    if vids:
        media += [{"u": u, "t": "video", **({"p": thumbs[i]} if i < len(thumbs) else {})} for i, u in enumerate(vids)]
    else:
        media += [{"u": u, "t": "image"} for u in thumbs]
    if not media:
        for pat in _TG_IMG[3:]:  # link preview - only if there is no media of its own
            mi = re.search(pat, chunk, re.S)
            if mi:
                media.append({"u": html.unescape(mi.group(1)), "t": "image"})
                break
    seen, out = set(), []
    for m in media:
        if m["u"] not in seen:
            seen.add(m["u"]); out.append(m)
    return out[:10]


_PIN_WHAT = r"(?:message|photo|video|file|audio|voice|document|sticker|poll|gif|animation|poster|location|contact|сообщение|фото|видео|файл|опрос|документ|голосовое|стикер|[\"“«])"
_TG_PINNED = re.compile(r"\b(?:un)?pinned\s+(?:a\s+|an\s+|the\s+)?" + _PIN_WHAT + r"|\b(?:от)?закрепил[аи]?\s+" + _PIN_WHAT, re.I)


def is_pin_notice(text):
    """Service record "... pinned a photo" (also in Russian): not needed in the feed."""
    t = (text or "").strip()
    return len(t) < 200 and bool(_TG_PINNED.search(t))


def parse_telegram(page, channel, noise=(), skip=(), ext_hosts=()):
    """HTML of t.me/s/<channel> -> list of news and the minimum id on the page."""
    marks = [(m.start(), m.group(1)) for m in re.finditer(r'data-post="([^"]+)"', page)]
    items, seen, min_id = [], set(), None
    for i, (pos, post) in enumerate(marks):
        chan, _, mid = post.rpartition("/")
        if chan.lower() != channel.lower() or not mid.isdigit() or post in seen:
            continue
        seen.add(post)
        msg_id = int(mid)
        min_id = msg_id if min_id is None else min(min_id, msg_id)
        chunk = page[pos: marks[i + 1][0] if i + 1 < len(marks) else len(page)]

        m = re.search(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', chunk, re.S)
        text = html_to_text(m.group(1)) if m else ""
        if any(re.search(pat, text, re.I) for pat in skip):  # ad posts (#реклама) are not taken into the feed
            continue
        if "tgme_widget_message_service" in chunk or (is_pin_notice(text) and len(_tg_media(chunk)) == 0):
            continue  # pinned message - a Telegram service record
        for pat in noise:
            text = re.sub(pat, "", text, flags=re.M)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        media = _tg_media(chunk)
        image = next((m["u"] for m in media if m["t"] == "image"), None) or next((m.get("p") for m in media if m.get("p")), None)

        ext = None
        ma = re.search(r'<a[^>]*tgme_widget_message_link_preview[^>]*>', chunk)
        if ma:
            mh = re.search(r'href="([^"]+)"', ma.group(0))
            if mh:
                ext = html.unescape(mh.group(1))
        if ext_hosts:  # original source - the article on the publisher's site: from a link in the text or preview; otherwise the link to the post itself
            cands = ([ext] if ext else []) + [html.unescape(h) for h in re.findall(r'href="(https?://[^"]+)"', m.group(1) if m else "")]
            ext = next((c for c in cands if any((urllib.parse.urlparse(c).netloc or "").lower().removeprefix("www.").endswith(h) for h in ext_hosts)), None)

        md = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        ts = parse_date(md.group(1)) if md else None
        if not text and not media:
            continue
        title, body = split_title_text(text)
        items.append(dict(
            guid=post, title=title or "Media post", text=body, url=f"https://t.me/{chan}/{msg_id}",
            ext_url=ext, image=image, media=media, published=ts))
    return items, min_id


def _vk_best_image(sizes):
    best = None
    for s in sizes or []:
        if s.get("url") and (best is None or s.get("width", 0) * s.get("height", 0) >= best.get("width", 0) * best.get("height", 0)):
            best = s
    return best["url"] if best else None


def parse_vk(resp_json, domain):
    """wall.get API response -> news."""
    if "error" in resp_json:
        err = resp_json["error"]
        raise RuntimeError(f"VK API: {err.get('error_msg', err)}")
    items = []
    for p in resp_json.get("response", {}).get("items", []):
        if p.get("marked_as_ads"):
            continue
        text = p.get("text", "") or ""
        atts = list(p.get("attachments", []))
        for r in p.get("copy_history", [])[:1]:
            text = (text + "\n\n" + (r.get("text") or "")).strip()
            atts += r.get("attachments", [])
        text = re.sub(r"\[(?:club|id|public)\d+\|([^\]]+)\]", r"\1", text).strip()
        image = ext = None
        media = []
        for a in atts:
            t = a.get("type")
            if t == "photo":
                ph = a["photo"]
                u = _vk_best_image(ph.get("sizes")) or (ph.get("orig_photo") or {}).get("url")
                if u:
                    media.append({"u": u, "t": "image"})
            elif t == "doc" and (a["doc"].get("ext") or "").lower() == "gif" and a["doc"].get("url"):
                media.append({"u": a["doc"]["url"], "t": "image"})  # gif: URL ends in .gif or is flagged as ext
            elif t in ("video", "clip"):
                u = _vk_best_image(a[t].get("image")) or _vk_best_image(a[t].get("first_frame"))
                if u:
                    media.append({"u": u, "t": "image"})
            elif t == "link":
                ext = ext or a["link"].get("url")
                if not media:
                    u = _vk_best_image((a["link"].get("photo") or {}).get("sizes"))
                    if u:
                        media.append({"u": u, "t": "image"})
        media = media[:10]
        image = media[0]["u"] if media else None
        if not text and not image:
            continue
        title, body = split_title_text(text)
        items.append(dict(
            guid=f"{p['owner_id']}_{p['id']}", title=title or "Post", text=body,
            url=f"https://vk.ru/wall{p['owner_id']}_{p['id']}", ext_url=ext, image=image, media=media,
            published=p.get("date")))
    return items


def parse_vk_mobile(page, domain, now=None):
    """HTML of the mobile VK wall (m.vk.ru/<group>) -> (news, number of posts found)."""
    starts = [m.start() for m in re.finditer(r'<div[^>]*class="[^"]*\bwall_item\b', page)]
    chunks = [page[a: starts[i + 1] if i + 1 < len(starts) else len(page)] for i, a in enumerate(starts)]
    if not chunks:  # different markup: split by post links
        pos = []
        for m in re.finditer(r'href="/wall(-?\d+_\d+)', page):
            if not pos or pos[-1][1] != m.group(1):
                pos.append((m.start(), m.group(1)))
        chunks = [page[a: pos[i + 1][0] if i + 1 < len(pos) else len(page)] for i, (a, _) in enumerate(pos)]
    items, raw = [], 0
    for chunk in chunks:
        mw = re.search(r'/wall(-?\d+)_(\d+)', chunk)
        if not mw:
            continue
        raw += 1
        owner, pid = mw.group(1), mw.group(2)
        if re.search(r'class="[^"]*\b(?:ads_mark|wall_marked_as_ads|post_ad)\b', chunk):
            continue
        texts = re.findall(r'<div[^>]*class="[^"]*\bpi_text\b[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        text = "\n\n".join(html_to_text(t) for t in texts)
        text = re.sub(r"\s*Показать полностью…?\s*$", "", text).strip()
        if not text and not texts:
            text = ""
        md = re.search(r'class="[^"]*\bwi_date\b[^"]*"[^>]*>(.*?)</a>', chunk, re.S) or \
            re.search(r'<time[^>]*>(.*?)</time>', chunk, re.S)
        ts = parse_ru_date(html_to_text(md.group(1)), now) if md else None
        image = None
        for pat in (r'data-src_big="([^"|]+)', r'class="[^"]*\b(?:thumb_map_img|page_post_thumb\w*)\b[^"]*"[^>]*?background-image:\s*url\([\'"]?([^\'")]+)',
                    r'<img[^>]*class="[^"]*\bpi_img\b[^"]*"[^>]*?src="([^"]+)"'):
            mi = re.search(pat, chunk, re.S)
            if mi:
                image = html.unescape(mi.group(1))
                break
        ext = None
        ma = re.search(r'href="(?:https?://[^/"]*)?/away\.php\?to=([^"&]+)', chunk)
        if ma:
            ext = urllib.parse.unquote(html.unescape(ma.group(1)))
        if not text and not image:
            continue
        title, body = split_title_text(text)
        items.append(dict(
            guid=f"{owner}_{pid}", title=title or "Post", text=body, url=f"https://vk.ru/wall{owner}_{pid}",
            ext_url=ext, image=image, published=ts))
    return items, raw


def _meta_tags(page):
    """Dict of og:/article:/name meta tags of the page."""
    meta = {}
    for tag in re.findall(r"<meta\b[^>]*>", page[:300000], re.I):
        attrs = {k.lower(): (v1 or v2) for k, _, v1, v2 in
                 re.findall(r'([\w:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')', tag)}
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            meta.setdefault(key.lower(), html.unescape(attrs["content"]))
    return meta


def _first_paragraphs(page, limit=700):
    out = []
    for m in re.finditer(r"<p\b[^>]*>(.*?)</p>", page[:400000], re.S | re.I):
        t = html_to_text(m.group(1))
        if len(t) >= 60:
            out.append(t)
            if sum(map(len, out)) >= limit:
                break
    text = "\n\n".join(out)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _strip_site_suffix(t, site=""):
    """Removes a trailing "| Site name" suffix from a title or description."""
    t = (t or "").strip()
    if site and len(site) > 3:
        t = re.sub(r"\s*[|\-–—·]\s*" + re.escape(site) + r"\s*$", "", t, flags=re.I).strip()
    return t


def parse_article_page(page, url):
    meta = _meta_tags(page)
    date = meta.get("article:published_time") or meta.get("og:published_time") or meta.get("pubdate")
    if not date:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page) or re.search(r'<time[^>]*datetime="([^"]+)"', page)
        date = m.group(1) if m else None
    title = meta.get("og:title") or (re.search(r"<title>(.*?)</title>", page, re.S | re.I) or [None, ""])[1]
    desc = (meta.get("og:description") or meta.get("description") or "").strip() or _first_paragraphs(page)
    site = meta.get("og:site_name") or ""
    desc, title = _strip_site_suffix(desc, site), _strip_site_suffix(html.unescape(title), site)
    img = meta.get("og:image") or meta.get("twitter:image")
    return dict(
        guid=url, title=title.strip(), text=desc, url=url, ext_url=None,
        image=urllib.parse.urljoin(url, img) if img else None, published=parse_date(date))


def find_feed_url(page, base):
    m = re.search(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', page, re.I)
    if m:
        h = re.search(r'href=["\']([^"\']+)', m.group(0))
        if h:
            return urllib.parse.urljoin(base, html.unescape(h.group(1)))
    return None


def _host(u):
    return (urllib.parse.urlparse(u).netloc or "").lower().removeprefix("www.")


def extract_links(page, base, link_re):
    """Article links by regex (in the page text, including embedded JSON)."""
    page = page.replace("\\/", "/")
    seen, out = set(), []
    for m in re.finditer(link_re, page):
        u = urllib.parse.urljoin(base, html.unescape(m.group(1)))
        if u.rstrip("/") != base.rstrip("/") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


_NON_ARTICLE = re.compile(
    r"/(tags?|topics?|category|categories|rubrics?|author|authors|page|search|login|signin|about|contacts?|"
    r"rss|feed|users?|profile|subscribe|privacy|terms|cookies?|ads|advert|sitemap)(/|$)", re.I)


def auto_extract_links(page, base):
    """For an arbitrary site: same-domain links that look like article URLs."""
    host = _host(base)
    seen, out = set(), []
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\'#]+)', page, re.I):
        u = urllib.parse.urljoin(base, html.unescape(m.group(1)))
        p = urllib.parse.urlparse(u)
        if p.scheme not in ("http", "https") or _host(u) != host or p.query:
            continue
        path = p.path
        if path in ("", "/") or u.rstrip("/") == base.rstrip("/") or u in seen:
            continue
        if re.search(r"\.(jpe?g|png|gif|webp|svg|css|js|pdf|zip|xml|json)$", path, re.I):
            continue
        seg = path.rstrip("/").rsplit("/", 1)[-1]
        article_like = seg.count("-") >= 2 or re.search(r"\d{4,}", path) or re.search(r"/\d{4}/\d{1,2}/", path)
        if not article_like or (_NON_ARTICLE.search(path) and not re.search(r"\d{4,}", seg)):
            continue
        seen.add(u)
        out.append(u)
    return out


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
  url TEXT, ext_url TEXT, image TEXT, published INTEGER NOT NULL, fetched INTEGER NOT NULL,
  search TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_items_pub ON items(published DESC);
CREATE INDEX IF NOT EXISTS idx_items_src ON items(source, published DESC);
CREATE TABLE IF NOT EXISTS bookmarks(item_id TEXT PRIMARY KEY, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, url TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
  color TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS backfill(
  source TEXT PRIMARY KEY, cursor TEXT, done INTEGER NOT NULL DEFAULT 0, loaded INTEGER NOT NULL DEFAULT 0,
  message TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS source_status(
  source TEXT PRIMARY KEY, ok INTEGER, message TEXT, checked INTEGER, new_items INTEGER);
"""


DB_NOTE = ""  # note for the UI if the database file had to be changed


def _heal_db_files(path):
    """Database file or -wal/-shm is not writable -> try to restore write permission."""
    for suf in ("", "-wal", "-shm"):
        f = path + suf
        try:
            if os.path.exists(f) and not os.access(f, os.W_OK):
                os.chmod(f, 0o644)
        except OSError:
            pass
    try:
        d = os.path.dirname(path) or "."
        if not os.access(d, os.W_OK):
            os.chmod(d, 0o755)
    except OSError:
        pass


def _connect(path):
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    try:  # fewer disk accesses (antivirus scans every write); data is not affected
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA cache_size=-20000")
    except sqlite3.Error:
        pass
    return con


@contextmanager
def db():
    con = _connect(DB_PATH)
    try:
        yield con
        con.commit()
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower():  # readable hint instead of a bare "attempt to write a readonly database"
            raise sqlite3.OperationalError(
                f"{e}: the process has no write permission for {DB_PATH} (or the neighbouring -wal/-shm). Close other copies "
                "of the app, check file ownership or set another path: NEWS_DB=...") from e
        raise
    finally:
        con.close()


_DB_SWITCH = threading.Lock()
DB_WRITE_LOCK = threading.RLock()  # writes go one at a time: less fighting over the file (antivirus/indexer interfere anyway)
RO_WAITS = (0.5, 1, 2, 3, 5, 8, 12)  # pauses between retries on "readonly", ~30 s in total


def _is_ro(e):
    return "readonly" in str(e).lower()


def _is_locked(e):
    return "database is locked" in str(e).lower() or "database table is locked" in str(e).lower()


def retry_readonly(fn):
    """Write on "attempt to write a readonly database" / "database is locked". On Windows the database file may be briefly held
    by another program (antivirus, indexer, a second app copy) or another thread. This passes by itself, so
    wait and retry (up to ~30 s in total); on "readonly" also repair permissions and finally move to a fallback database."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        for attempt in range(len(RO_WAITS) + 1):
            try:
                with DB_WRITE_LOCK:
                    return fn(*a, **k)
            except sqlite3.OperationalError as e:
                ro, locked = _is_ro(e), _is_locked(e)
                if not (ro or locked) or attempt == len(RO_WAITS):
                    raise
                if ro:
                    _heal_db_files(DB_PATH)
                    if attempt == len(RO_WAITS) - 1:
                        with _DB_SWITCH:
                            ensure_writable_db(force_copy=True)
                time.sleep(RO_WAITS[attempt] + (random.random() if locked else 0))
    return wrapper


def ensure_writable_db(force_copy=False):
    """Write check at startup. Repair permissions; if that fails - move to ~/.my-feed/news.db (data copy).
    force_copy: the file passes the probe but real writes fail - still move if the database can be copied."""
    global DB_PATH, DB_NOTE
    def probe(path):
        con = _connect(path)
        try:
            v = con.execute("PRAGMA user_version").fetchone()[0]
            con.execute(f"PRAGMA user_version={int(v)}")  # real write to the file (BEGIN IMMEDIATE does not fail on a read-only db)
            con.commit()
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            con.close()
    if not os.path.exists(DB_PATH) or (probe(DB_PATH) and not force_copy):
        return
    _heal_db_files(DB_PATH)
    if probe(DB_PATH) and not force_copy:
        return
    try:  # stale -wal/-shm from a dead process or another user: switch the journal
        con = _connect(DB_PATH)
        con.execute("PRAGMA journal_mode=DELETE")
        con.close()
    except sqlite3.Error:
        pass
    if probe(DB_PATH) and not force_copy:
        return
    alt_dir = os.path.join(os.path.expanduser("~"), ".my-feed")
    os.makedirs(alt_dir, exist_ok=True)
    alt = os.path.join(alt_dir, "news.db")
    if os.path.abspath(alt) == os.path.abspath(DB_PATH):
        return
    if not os.path.exists(alt):
        copied = False
        try:
            src, dst = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True), sqlite3.connect(alt)
            src.backup(dst)
            src.close(); dst.close()
            copied = True
        except sqlite3.Error:
            pass
        if not copied:  # SQLite failed - just copy the file(s): better than an empty database instead of data
            try:
                if os.path.exists(alt):
                    os.remove(alt)
                for suf in ("", "-journal", "-wal", "-shm"):
                    if os.path.exists(DB_PATH + suf):
                        shutil.copy2(DB_PATH + suf, alt + suf)
            except OSError:
                pass
    DB_NOTE = f"database {DB_PATH} is not writable - using {alt}"
    print("⚠ " + DB_NOTE)
    DB_PATH = alt
    _init_schema()


def init_db():
    ensure_writable_db()
    _init_schema()


def _init_schema():
    with db() as con:
        synced = bool(re.search(r"onedrive|desktop|рабочий стол|dropbox|google ?drive|yandex", DB_PATH, re.I))
        try:  # WAL: readers do not block writers (fewer "database is locked"); in synced folders - a plain journal without -wal/-shm
            con.execute("PRAGMA journal_mode=" + ("DELETE" if synced else "WAL"))
        except sqlite3.Error:
            pass
        con.executescript(SCHEMA)
        if not con.execute("SELECT 1 FROM sources LIMIT 1").fetchone():
            for i, s in enumerate(DEFAULT_SOURCES):
                con.execute("INSERT INTO sources(id,name,type,url,config,color,active,position,created) VALUES(?,?,?,?,?,?,1,?,?)",
                            (s["id"], s["name"], s["type"], s["url"], json.dumps(s["config"], ensure_ascii=False),
                             PALETTE[i % len(PALETTE)], i, int(time.time())))
        _migrate(con)


def _migrate(con):
    """Brings an existing database up to the current schema (adds missing columns)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    for name, ddl in (("media", "TEXT"), ("links", "TEXT"), ("exact", "INTEGER NOT NULL DEFAULT 0")):
        if name not in cols:  # media/links are JSON; exact: 1 = time taken from the post page, 0 = estimated from the feed
            con.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")


def _row_to_src(r):
    d = dict(r)
    d.update(json.loads(d.pop("config") or "{}"))
    return d


def get_sources(active_only=True):
    with db() as con:
        rows = con.execute("SELECT * FROM sources" + (" WHERE active=1" if active_only else "") +
                           " ORDER BY position, created").fetchall()
    return [_row_to_src(r) for r in rows]


def get_source(sid):
    with db() as con:
        r = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    return _row_to_src(r) if r else None


def source_links(src):
    """Source links for the edit dialog: [{"u": URL, "label": label in the feed}]."""
    if src["type"] == "telegram":
        names = src.get("channel_names") or {}
        return [{"u": f"https://t.me/{c}", "label": names.get(c, src["name"] if len(_tg_channels(src)) == 1 else c)}
                for c in _tg_channels(src)]
    return [{"u": src.get("feed") or src["url"], "label": ""}]


def build_spec(name, links):
    """Name + one or more links -> source spec. Several links - Telegram channels only (like Rozetked)."""
    links = [l for l in (links or []) if (l.get("u") or "").strip()]
    if not links:
        raise ValueError("Specify at least one link")
    specs = [detect_source(l["u"]) for l in links]
    first, name = dict(specs[0]), (name or "").strip()
    if len(specs) == 1:
        if name:
            first["name"] = name
        if first["type"] == "telegram" and (links[0].get("label") or "").strip():
            first["config"] = {**first["config"], "channel_names": {first["config"]["channel"]: links[0]["label"].strip()}}
        return first
    if any(sp["type"] != "telegram" for sp in specs):
        raise ValueError("Several links in one source are supported only for Telegram channels (t.me/...)")
    chans, names = [], {}
    for sp, l in zip(specs, links):
        ch = sp["config"]["channel"]
        if ch.lower() in (c.lower() for c in chans):
            continue
        chans.append(ch)
        names[ch] = (l.get("label") or "").strip() or (name if len(chans) == 1 and name else sp["name"])
    first["name"] = name or specs[0]["name"]
    first["config"] = dict(channel=chans[0], channels=chans, channel_names=names)
    return first


def edit_source(sid, name, links):
    """Edit dialog: if the links did not change, only the name and channel labels change; otherwise the source is rebuilt."""
    cur = get_source(sid)
    if not cur:
        return None
    norm = lambda u: (u or "").strip().rstrip("/").lower()
    now = source_links(cur)
    if [norm(l.get("u")) for l in links or []] == [norm(l["u"]) for l in now]:
        cfg = {k: v for k, v in cur.items() if k not in ("id", "name", "type", "url", "color", "active", "position", "created")}
        if cur["type"] == "telegram":
            cfg["channel_names"] = {c: (l.get("label") or "").strip() or (name if len(now) == 1 else c)
                                    for c, l in zip(_tg_channels(cur), links)}
        spec = dict(name=(name or cur["name"]).strip(), type=cur["type"], url=cur["url"], config=cfg)
        return update_source(sid, spec)
    return update_source(sid, build_spec(name, links))


@retry_readonly
def update_source(sid, spec):
    """Edit a source from the dialog: name, links (type and settings are rebuilt, service noise/skip/ext_hosts are kept)."""
    with db() as con:
        old = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        if not old:
            return None
        cfg_old = json.loads(old["config"] or "{}")
        cfg = dict(spec["config"])
        if spec["type"] == old["type"]:
            keep = {k: v for k, v in cfg_old.items() if k in ("noise", "skip", "ext_hosts", "alt_feeds", "strip_code", "try_feeds", "pages", "link_re")}
            cfg = {**keep, **cfg}
        changed = (spec["type"], json.dumps(cfg, sort_keys=True)) != (old["type"], json.dumps(cfg_old, sort_keys=True))
        con.execute("UPDATE sources SET name=?, type=?, url=?, config=?, active=1 WHERE id=?",
                    (spec["name"], spec["type"], spec["url"], json.dumps(cfg, ensure_ascii=False), sid))
        if changed:  # links changed: walk the archive again (known entries are skipped)
            con.execute("UPDATE backfill SET done=0, cursor='{}', message=NULL WHERE source=?", (sid,))
    return get_source(sid)


@retry_readonly
def add_source(spec):
    """spec: id,name,type,url,config. Returns (source, is_new)."""
    with db() as con:
        old = con.execute("SELECT * FROM sources WHERE id=?", (spec["id"],)).fetchone()
        if old:
            was_active = bool(old["active"])
            con.execute("UPDATE sources SET active=1 WHERE id=?", (spec["id"],))
            return get_source(spec["id"]), not was_active
        n = con.execute("SELECT COUNT(*), COALESCE(MAX(position),-1) FROM sources").fetchone()
        con.execute("INSERT INTO sources(id,name,type,url,config,color,active,position,created) VALUES(?,?,?,?,?,?,1,?,?)",
                    (spec["id"], spec["name"], spec["type"], spec["url"], json.dumps(spec["config"], ensure_ascii=False),
                     PALETTE[n[0] % len(PALETTE)], n[1] + 1, int(time.time())))
    return get_source(spec["id"]), True


@retry_readonly
def remove_source(sid):
    """The source is removed from the feed (data and bookmarks are kept: everything returns if it is added again)."""
    with db() as con:
        return con.execute("UPDATE sources SET active=0 WHERE id=? AND active=1", (sid,)).rowcount > 0


def known_ids(source_id):
    with db() as con:
        return {r[0] for r in con.execute("SELECT id FROM items WHERE source=?", (source_id,))}


@retry_readonly
def _w_update_ok_item(iid, new):
    with db() as con:
        con.execute("UPDATE items SET title=?, text=?, image=COALESCE(?,image), ext_url=COALESCE(?,ext_url), links=COALESCE(?,links), search=? WHERE id=?",
                    (new["title"], new["text"], new["image"], new.get("ext_url"),
                     json.dumps(new["links"], ensure_ascii=False) if new.get("links") is not None else None,
                     norm_search(new["title"] + "\n" + new["text"]), iid))


@retry_readonly
def _w_delete_items(ids):
    with db() as con:
        con.execute(f"DELETE FROM items WHERE id IN ({','.join('?' * len(ids))}) "
                    "AND id NOT IN (SELECT item_id FROM bookmarks)", ids)


@retry_readonly
def _w_set_images(res):
    with db() as con:
        for iid, img in res:
            con.execute("UPDATE items SET image=?, media=CASE WHEN media IS NULL AND ?<>'' THEN ? ELSE media END WHERE id=?",
                        (img, img, json.dumps([{"u": img, "t": "image"}]) if img else None, iid))


@retry_readonly
def _w_set_media(pairs):
    with db() as con:
        for iid, it in pairs:
            con.execute("UPDATE items SET media=?, image=COALESCE(image, ?) WHERE id=?", (json.dumps(it["media"]), it["image"], iid))


@retry_readonly
def save_items(source_id, items):
    """Saves news, returns the number of new ones."""
    now = int(time.time())
    new = 0
    with db() as con:
        for it in items:
            iid = make_id(source_id, it["guid"])
            title, text = it["title"].strip(), (it.get("text") or "").strip()
            if it.get("ext_url") and con.execute("SELECT 1 FROM items WHERE source=? AND url=? AND id<>?",
                                                 (source_id, it["ext_url"], iid)).fetchone():
                continue  # this item already exists (e.g. an earlier site entry): the Telegram post does not duplicate it
            search = norm_search(title + "\n" + text)
            media_json = json.dumps(it["media"], ensure_ascii=False) if it.get("media") else None
            links_json = json.dumps(it["links"], ensure_ascii=False) if it.get("links") is not None else None
            cur = con.execute(
                "INSERT OR IGNORE INTO items(id,source,title,text,url,ext_url,image,published,fetched,search,exact,media,links)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, source_id, title, text, it.get("url"), it.get("ext_url"), it.get("image"),
                 it.get("published") or now, now, search, int(bool(it.get("precise"))), media_json, links_json))
            if cur.rowcount:
                new += 1
            else:  # update edits (title/text) without touching the date and an already found image
                con.execute(
                    "UPDATE items SET title=?, text=?, search=?, url=COALESCE(?,url), ext_url=COALESCE(?,ext_url),"
                    " image=COALESCE(?,image), media=COALESCE(?,media), links=COALESCE(?,links) WHERE id=?",
                    (title, text, search, it.get("url"), it.get("ext_url"), it.get("image"), media_json, links_json, iid))
                if it.get("precise") and it.get("published"):  # time was read from the post page - it is final
                    con.execute("UPDATE items SET published=?, exact=1 WHERE id=?", (it["published"], iid))
                elif it.get("exact") and it.get("published"):  # the source sets the order itself (OK) - fix the estimate
                    con.execute("UPDATE items SET published=? WHERE id=? AND exact=0", (it["published"], iid))
    return new


@retry_readonly
def set_status(source_id, ok, message, new_items=0):
    with db() as con:
        con.execute(
            "INSERT INTO source_status(source,ok,message,checked,new_items) VALUES(?,?,?,?,?)"
            " ON CONFLICT(source) DO UPDATE SET ok=excluded.ok, message=excluded.message,"
            " checked=excluded.checked, new_items=excluded.new_items",
            (source_id, int(ok), message, int(time.time()), new_items))


# ---------------------------------------------------------------- browser (for sites with bot protection)


_PW_TLS = threading.local()
BROWSER_MAX = max(1, int(os.environ.get("NEWS_BROWSERS", "2")))  # how many Chromium instances at once (each takes hundreds of MB)
class BrowserBusy(RuntimeError):
    """No free browser slot - not a source error, the attempt should be retried later."""


_BR_COND = threading.Condition()
_BR_HELD = {}  # slot -> when taken


def _br_acquire(timeout=120):
    """A browser slot. A hung browser (held longer than 10 minutes) does not keep the slot."""
    end = time.time() + timeout
    with _BR_COND:
        while True:
            now = time.time()
            for t, since in list(_BR_HELD.items()):
                if now - since > 600:
                    _BR_HELD.pop(t, None)
            if len(_BR_HELD) < BROWSER_MAX:
                tok = object()
                _BR_HELD[tok] = now
                return tok
            if end - now <= 0:
                raise BrowserBusy("all browser slots are taken by other sources, try again later")
            _BR_COND.wait(min(end - now, 5))


def _br_release(tok):
    if tok is None:
        return
    with _BR_COND:
        _BR_HELD.pop(tok, None)
        _BR_COND.notify_all()


def _acquire_pw():
    """Playwright (sync API) can be started only once per thread: a second sync_playwright().start() in the same thread
    fails with "Sync API inside the asyncio loop". So all browsers of a thread share one instance, with a counter."""
    from playwright.sync_api import sync_playwright
    st = getattr(_PW_TLS, "st", None)
    if st is None or st["pw"] is None:
        st = {"pw": sync_playwright().start(), "n": 0}
        _PW_TLS.st = st
    st["n"] += 1
    return st


def _release_pw(st):
    if not st:
        return
    st["n"] -= 1
    if st["n"] <= 0 and st["pw"] is not None:
        try:
            st["pw"].stop()
        except Exception:
            pass
        st["pw"] = None
        if getattr(_PW_TLS, "st", None) is st:
            _PW_TLS.st = None


class PageGetter:
    """Fetches pages with a plain request; if the site returns an empty shell or refuses - switches
    to a real browser (Playwright, if installed: python server.py --install-browser)."""

    def __init__(self, ua=None, budget=None):
        self.ua = ua
        self.browser_mode = False
        self._pw = self._br = self._page = self._pwst = self._slot = None
        self.t0, self.budget = time.time(), budget or SOURCE_BUDGET
        self.auto_browser = True  # on a certificate error in a plain request - try the browser

    def _content(self, tries=8):
        """page.content() fails while the page is redirecting/reloading (Cloudflare, JS check):
        wait for navigation to finish and retry."""
        err = None
        for _ in range(tries):
            try:
                return self._page.content()
            except Exception as e:
                msg = str(e)
                if not re.search(r"navigat|context was destroyed|Target (?:page|closed)|changing the content", msg, re.I):
                    raise
                err = e
                try:
                    self._page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                try:
                    self._page.wait_for_timeout(700 + 400 * _)
                except Exception:
                    time.sleep(1.2)
        raise err

    def _goto(self, url):
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            if not re.search(r"navigat|interrupted|ERR_ABORTED", str(e), re.I):
                raise  # a redirect interrupted loading — the page opens anyway, carry on

    def text(self, url, nav_idle=True, until=None, **kw):
        """until(html) -> bool: True if the expected page has loaded (otherwise wait — it may be a JS browser check)."""
        if time.time() - self.t0 > self.budget:  # one source must not hang forever
            raise TimeoutError(f"time limit exceeded for the source ({self.budget} s)")
        if not self.browser_mode:
            try:
                return http_text(url, ua=self.ua, **kw)
            except urllib.error.URLError as e:
                if not is_ssl_error(e) or not self.auto_browser:
                    raise
                try:  # the plain request does not trust the site certificate — try the browser (it has its own store)
                    self.enable_browser()
                except Exception:
                    raise e
        self._goto(url)
        if nav_idle:
            try:
                self._page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
        html = self._content()
        waited = 0
        while waited < 20000 and (not until(html) if until else len(html) < 3000):  # a JS browser check usually passes within seconds
            self._page.wait_for_timeout(2500)
            waited += 2500
            html = self._content()
        return html

    def raw(self, url):
        """Raw response (RSS/XML) -> bytes. If the site blocks plain requests (403), fetch it through the browser with its cookies."""
        if time.time() - self.t0 > self.budget:
            raise TimeoutError(f"time limit exceeded for the source ({self.budget} s)")
        if not self.browser_mode:
            return http_get(url, ua=self.ua)[0]
        self._goto(url)
        t_ch = time.time()
        for _ in range(8):  # the browser check (Cloudflare) usually passes within a few seconds
            try:
                head = self._content()[:20000]
            except Exception:
                head = ""
            if not re.search(r"just a moment|cf-challenge|checking your browser|проверка браузера", head, re.I):
                break
            if time.time() - t_ch > 40:
                break
            self._page.wait_for_timeout(2500)
        js = "async u => { const r = await fetch(u, {credentials: 'include'}); return await r.text(); }"
        last = None
        for _ in range(4):  # the page may have reloaded in the middle of the request
            try:
                return self._page.evaluate(js, url).encode("utf-8")
            except Exception as e:
                last = e
                if not re.search(r"navigat|context was destroyed", str(e), re.I):
                    break
                try:
                    self._page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                self._page.wait_for_timeout(1500)
        try:  # fallback: the navigation response itself (for RSS that is the XML)
            resp = self._page.goto(url, wait_until="commit", timeout=30000)
            if resp is not None:
                return resp.body()
        except Exception:
            pass
        raise last

    def enable_browser(self, light=False):
        """light=True: no images, video or fonts (for scrolling long feeds — otherwise the browser runs out of memory)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "the site returns an empty page to plain requests (bot protection). Browser mode is required: "
                "run  python server.py --install-browser  and restart the app")
        try:
            # background tasks (catch-up) do not wait for a slot and yield to refresh; refresh waits up to 4 minutes
            self._slot = _br_acquire(0 if getattr(_PW_TLS, "nowait", False) else 240)
            self.t0 = time.time()  # waiting for a slot does not count toward the source budget
            self._pwst = _acquire_pw()
            self._pw = self._pwst["pw"]
            headless = os.environ.get("NEWS_BROWSER_HEADED") != "1"  # =1: show the window (gets past more protections)
            opts = dict(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            try:  # Chromium's "new" headless mode looks less like a bot
                self._br = self._pw.chromium.launch(channel="chromium", **opts) if headless else self._pw.chromium.launch(**opts)
            except Exception:
                self._br = self._pw.chromium.launch(**opts)
        except BrowserBusy:
            self.close()
            raise
        except Exception as e:
            self.close()
            tip = "run  python server.py --install-browser" if re.search(r"executable|install", str(e), re.I) else "see the console"
            raise RuntimeError(f"could not start the browser ({str(e)[:200]}); {tip}")
        ctx = self._br.new_context(user_agent=self.ua or UA, locale="ru-RU", ignore_https_errors=INSECURE_SSL)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        if light:
            try:
                ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font")
                          else route.continue_())
            except Exception:
                pass
        self._page = ctx.new_page()
        self.browser_mode = True
        self.t0 = time.time()  # starting the browser on a busy machine can take a minute — not counted toward the source budget

    def close(self):
        try:
            if self._br:
                self._br.close()
        except Exception:
            pass
        st, self._pwst = self._pwst, None
        _release_pw(st)  # Playwright itself stops when the thread's last browser is closed
        slot, self._slot = self._slot, None
        _br_release(slot)
        self._pw = self._br = self._page = None
        self.browser_mode = False


def install_browser():
    for cmd in ([sys.executable, "-m", "pip", "install", "playwright"],
                [sys.executable, "-m", "playwright", "install", "chromium"]):
        print("→", " ".join(cmd))
        subprocess.check_call(cmd)
    print("Done: browser mode enabled. Restart the app.")


def ok_debug(page, group, n=3):
    """What the parser sees in the first posts of an OK group (diagnostics: shows the markup and the detected time)."""
    prefix = "/" + group.lower() + "/topic/"
    starts, seen = [], set()
    for m in _OK_TOPIC.finditer(page):
        path = urllib.parse.urlparse(html.unescape(m.group(1))).path
        if (path.lower().startswith(prefix) or path.startswith("/group/")) and m.group(2) not in seen:
            seen.add(m.group(2))
            starts.append((max(page.rfind("<a ", 0, m.start()), 0), m.group(2)))
    for i, (a, tid) in enumerate(starts[:n]):
        b = starts[i + 1][0] if i + 1 < len(starts) else a + 4000
        chunk = re.sub(r'="([^"]{70})[^"]*"', r'="\1…"', re.sub(r"\s+", " ", page[a:b]))
        print(f"    --- post {tid}: time={_ok_label(page[a:b])!r}\n    {chunk[:1100]}")


def describe_page(page):
    """Short page description for error messages and diagnostics."""
    t = re.search(r"<title[^>]*>(.*?)</title>", page or "", re.S | re.I)
    walls = len(set(re.findall(r"/wall-?\d+_\d+", page or "")))
    txt = re.sub(r"\s+", " ", html_to_text(page or ""))[:140]
    return f"{len(page or '')} bytes, title={(t.group(1).strip() if t else '—')[:60]!r}, post links: {walls}, text: {txt!r}"


def diagnose():
    """python server.py --diagnose - what the problem sites actually return (plain request and browser)."""
    out_dir = os.path.join(BASE, "diagnose")
    os.makedirs(out_dir, exist_ok=True)
    targets = [("ok-kinopoisk", "https://ok.ru/kinopoisk"), ("tg-hinews", "https://t.me/s/hinews_ru"),
               ("tg-rozetked", "https://t.me/s/rozetked"), ("tg-rozetkedplus", "https://t.me/s/rozetkedplus")]
    for name, url in targets:
        print(f"\n=== {name}: {url}")
        for mode in ("plain request", "browser"):
            g, t = PageGetter(budget=90), time.time()
            g.auto_browser = False
            try:
                if mode == "browser":
                    g.enable_browser()
                page = g.text(url)
            except Exception as e:
                print(f"  {mode:<15} ERROR {type(e).__name__}: {str(e)[:300]}")
                continue
            finally:
                g.close()
            fn = os.path.join(out_dir, f"{name}-{'browser' if mode == 'browser' else 'plain'}.html")
            with open(fn, "w", encoding="utf-8") as f:
                f.write(page)
            if name.startswith("ok-"):
                n = parse_ok(page, "kinopoisk")[1]
            else:
                n = page.count("tgme_widget_message_wrap")
            title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
            print(f"  {mode:<15} {len(page):>7} bytes in {time.time() - t:.0f} s; items found: {n}; "
                  f"title: {(title.group(1).strip() if title else '-')[:80]!r}")
            if not n:
                print(f"                  {describe_page(page)}")
            if name.startswith("ok-"):
                ok_debug(page, "kinopoisk")
            print(f"                  saved: {fn}")
        print("\nIf \"items found: 0\" and the text shows a captcha / \"browser check\", the site blocks automated access.")


# ---------------------------------------------------------------- fetching news


def _paged(url, n, param):
    return url if n == 1 else f"{url}{'&' if '?' in url else '?'}{param}={n}"


def _blocked(e):
    """The site blocks non-browser requests (Cloudflare etc.) or does not trust the certificate -> worth trying the browser."""
    return (isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 429, 503)) or is_ssl_error(e)


def fetch_rss(src):
    known, feed, out, seen = known_ids(src["id"]), src["feed"], [], set()
    g = PageGetter()
    try:
        return _fetch_rss(src, known, feed, out, seen, g)
    finally:
        g.close()


def _rss_get(g, url, allow_empty=False, browser=True):
    """Raw feed response. If the site blocks plain requests (403, SSL) or returns nothing — fetch it through the browser.
    allow_empty: an empty page (no more archive) is not a block."""
    err = None
    try:
        data = g.raw(url)
        if data.strip() or g.browser_mode or allow_empty:
            return data
        err = "empty response"
    except (urllib.error.URLError, OSError) as e:
        if g.browser_mode or not _blocked(e):
            raise
        err = e
    if not browser:
        raise urllib.error.URLError(str(err))
    try:
        g.enable_browser()
        return g.raw(url)
    except BrowserBusy:
        raise
    except Exception as e2:
        raise RuntimeError(f"the site blocks plain requests ({err}); browser mode did not help: {str(e2)[:150]}. "
                           "Run python server.py --install-browser (and NEWS_BROWSER_HEADED=1 if needed)")


def _feed_items(src, data, feed):
    """Feed items, or None if the response is empty or not a feed."""
    if not data or not data.strip():
        return None
    try:
        return parse_feed(data, feed, strip_code=src.get("strip_code", False))
    except ET.ParseError:
        return None


def _fetch_rss(src, known, feed, out, seen, g):
    for n in range(1, CATCHUP_PAGES + 1):
        url = _paged(feed, n, "paged")
        try:
            data = None
            if n == 1 and src.get("alt_feeds") and not g.browser_mode:
                # the site blocks plain requests (Cloudflare) -> fallback feed (FeedBurner etc.) instead of the heavy browser
                try:
                    data = _rss_get(g, url, browser=False)
                except (urllib.error.URLError, OSError):
                    for alt in src["alt_feeds"]:
                        try:
                            cand = http_get(alt, timeout=20)[0]
                            if _feed_items(src, cand, alt):
                                data, feed = cand, alt
                                STATUS_NOTE[src["id"]] = "the main feed is blocked by site protection, reading the fallback"
                                break
                        except Exception:
                            continue
            if data is None:
                data = _rss_get(g, url, allow_empty=n > 1)
        except urllib.error.HTTPError as e:
            if n > 1 and e.code in (400, 404):
                break
            raise
        items = _feed_items(src, data, feed)
        if items is None and n == 1:  # an empty response can be random or due to protection: retry with different headers and URL variants
            for cand, ua in ((url, None), (url.rstrip("/"), "Mozilla/5.0 (compatible; Feedly/1.0; +http://www.feedly.com/fetcher.html)"),
                             (url + ("&" if "?" in url else "?") + "nocache=%d" % int(time.time()), None)):
                time.sleep(1.5)
                try:
                    cand_data = http_get(cand, timeout=20, ua=ua, headers={"Accept": "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.5"})[0]
                except Exception:
                    continue
                got = _feed_items(src, cand_data, feed)
                if got:
                    data, items = cand_data, got
                    break
        if items is None and n == 1:  # the feed at the given URL is empty: look for a feed link on the section page and typical addresses
            try:
                items = _try_feeds(http_text(src["url"], timeout=20, max_bytes=400000), src["url"])
                if items:
                    STATUS_NOTE[src["id"]] = "the feed at the main URL is empty, reading the one found on the site"
            except Exception:
                items = None
        if items is None:
            if n > 1:
                break  # no more pages (or the site returned nothing)
            raise RuntimeError("the site returned an empty response or something that is not an RSS feed" + (f": {describe_page(data.decode('utf-8', 'replace'))}" if data else ""))
        if n == 1 and not items:
            raise RuntimeError("the feed was read but contains no items")
        ids = [make_id(src["id"], it["guid"]) for it in items]
        fresh = [it for it, i in zip(items, ids) if i not in seen]
        if not fresh:
            break  # repeated page: pagination is not supported
        out += fresh
        seen.update(ids)
        if not known or any(i in known for i in ids):
            break  # reached already known entries — no gaps
    return out


def _tg_channels(src):
    return list(src.get("channels") or [src["channel"]])


def _tg_parse(src, chan, page):
    return parse_telegram(page, chan, src.get("noise", ()), src.get("skip", ()), src.get("ext_hosts", ()))


def fetch_telegram(src):
    known, items, errs = known_ids(src["id"]), [], []
    for chan in _tg_channels(src):  # several channels = one source in the list
        try:
            items += _fetch_tg_channel(src, chan, known)
        except Exception as e:
            errs.append(f"{chan}: {type(e).__name__}: {str(e)[:100]}")
    if errs and not items:
        raise RuntimeError("; ".join(errs))
    if errs:
        STATUS_NOTE[src["id"]] = "some channels could not be read: " + "; ".join(errs)
    if not items and not known:
        raise RuntimeError("the channel's public page has no messages (the channel is private or does not exist)")
    return items


def _fetch_tg_channel(src, chan, known):
    items, before, seen = [], None, set()
    for _ in range(CATCHUP_PAGES if known else 3):
        url = f"https://t.me/s/{chan}" + (f"?before={before}" if before else "")
        got, min_id = _tg_parse(src, chan, http_text(url))
        if min_id is None:
            break
        got = [it for it in got if it["guid"] not in seen]
        seen.update(it["guid"] for it in got)
        items += got
        if any(make_id(src["id"], it["guid"]) in known for it in got) or min_id <= 1 or (before and min_id >= before):
            break
        before = min_id
    return items


def fetch_vk_api(src):
    known, out, off = known_ids(src["id"]), [], 0
    for _ in range(CATCHUP_PAGES if known else 1):
        q = urllib.parse.urlencode(dict(domain=src["domain"], count=100, offset=off, access_token=VK_TOKEN, v="5.199"))
        data, _ = http_get(f"https://{VK_API_HOST}/method/wall.get?{q}")
        resp = json.loads(data.decode("utf-8"))
        items = parse_vk(resp, src["domain"])
        raw = len(resp.get("response", {}).get("items", []))
        out += items
        off += raw
        if not raw or off >= resp.get("response", {}).get("count", 0) or any(make_id(src["id"], it["guid"]) in known for it in items):
            break
    return out


def _vk_mobile_url(domain, offset):
    return f"https://m.vk.ru/{domain}" + (f"?offset={offset}" if offset else "")


def fetch_vk_mobile(src):
    """Mobile wall version (m.vk.ru/<group>) — no token. VK serves a "your browser is outdated" stub
    to some User-Agents, so try several, then the real browser."""
    dom = src["domain"]
    known, out, off = known_ids(src["id"]), [], 0
    ready = lambda h: bool(parse_vk_mobile(h, dom)[1])
    first, g, last_err, last_page, ssl_err = _vk_mobile_url(dom, 0), None, None, "", None
    try:
        for ua in ([VK_UA_OK[dom]] if dom in VK_UA_OK else VK_UAS):
            cand = PageGetter(ua=ua or MOBILE_UA)
            try:
                last_page = cand.text(first)
                items, raw = parse_vk_mobile(last_page, dom)
            except Exception as e:
                last_err = e
                ssl_err = ssl_err or (e if is_ssl_error(e) else None)
                cand.close()
                continue
            if raw:
                g, VK_UA_OK[dom] = cand, ua
                break
            cand.close()
        if g is None:  # plain requests failed — use the real browser (wait for the page to render)
            g = PageGetter(ua=MOBILE_UA)
            try:
                g.enable_browser()
                last_page = g.text(first, until=ready)
                items, raw = parse_vk_mobile(last_page, dom)
            except Exception as e:
                last_err = e
                ssl_err = ssl_err or (e if is_ssl_error(e) else None)
                raw = 0
            if not raw:
                if ssl_err is not None:
                    raise ssl_err  # the clear certificate hint is built in refresh_source
                why = f"{type(last_err).__name__}: {last_err}" if last_err else describe_page(last_page)
                raise RuntimeError(f"m.vk.ru returned no posts ({why}). Run  python server.py --diagnose  and share the output; "
                                   f"alternatives: a VK token (README) or the Telegram channel t.me/kinopoisk via the Sources dialog")
        for _ in range(CATCHUP_PAGES if known else 2):
            if off:
                items, raw = parse_vk_mobile(g.text(_vk_mobile_url(dom, off), until=ready), dom)
            if not raw:
                break
            out += items
            off += raw
            if any(make_id(src["id"], it["guid"]) in known for it in items):
                break
    finally:
        if g:
            g.close()
    return out


_OK_TOPIC = re.compile(r'href="([^"]*?/topic/(\d+)[^"]*)"')
_OK_NOISE = re.compile(r"^(?:комментировать|класс|поделиться|ещё|еще|подробнее|показать полностью|показать ещё|показать еще|читать далее|в группу|вступить в группу|"
                       r"войти|подписаться|пожаловаться|нравится|ответить|поделились|оценить)\b[\s\d.,:kк]*$", re.I)


_OK_JUNK = re.compile(r"^(?:[\d\s.,]+\s*(?:тыс\.?|млн\.?|[kкm])?\s*просмотр\w*|ещ[её]\s+\d+\s+(?:фото\w*|видео\w*|ролик\w*|изображен\w*)|"
                      r"\+\s*\d+\s*(?:фото\w*|видео\w*)?)\s*$", re.I)


def _ok_time_line(line):
    t = line.strip().lower().replace("\xa0", " ")
    if not t or len(t) > 60:
        return None
    if t in ("сегодня", "вчера", "позавчера"):
        return t
    m = re.fullmatch(r"(?:[^\d:]{0,40}?)((?:(?:сегодня|вчера|позавчера)[,\s]*(?:в\s*)?)?\d{1,2}:\d{2})\s*[·•|]\s*\d+\s*\w*\.?\s+назад", t)
    if m:  # "Кинопоиск17:34 · 4 ч назад" -> exact time, "N h ago" is only approximate
        return m.group(1)
    if re.fullmatch(r"\d+\s*(?:сек|мин|ч|час|д|дн)\w*\.?\s+назад|только что", t):
        return t
    if re.fullmatch(r"(?:(?:сегодня|вчера|позавчера)[,\s]*(?:в\s*)?)?\d{1,2}:\d{2}", t):
        return t
    if re.fullmatch(r"\d{1,2}\s+[а-я]{3,}\.?(?:\s+\d{4})?(?:[,\s]+(?:в\s*)?\d{1,2}:\d{2})?", t):
        return t
    return None


def _ok_images(chunk, limit=8):
    out = []
    for m in re.finditer(r"<img\b[^>]*>", chunk, re.I):
        u = _ok_image(m.group(0))
        if u and u not in out:
            out.append(u)
            if len(out) >= limit:
                break
    return out


def _ok_image(chunk):
    for m in re.finditer(r"<img\b[^>]*>", chunk, re.I):
        tag = m.group(0)
        if re.search(r"avatar|\bava\b|emoji|icon|logo", tag, re.I):
            continue
        w = re.search(r'\bwidth\s*=\s*"?(\d+)', tag)
        if w and int(w.group(1)) <= 120:
            continue
        u = re.search(r'(?:data-src|src)\s*=\s*"([^"]+)"', tag)
        if not u:
            continue
        url = html.unescape(u.group(1))
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http") and "okcdn.ru" in url and "//st.okcdn.ru" not in url and "//st-" not in url:
            return url
    return None


_OK_MONTHS = "янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек"
_OK_INLINE = re.compile(
    r"(?:(?:сегодня|вчера|позавчера)[,\s]*(?:в\s*)?\d{1,2}:\d{2})"
    r"|(?:\d{1,2}\s+(?:" + _OK_MONTHS + r")[а-я]*\.?(?:\s+\d{4})?(?:[,\s]+(?:в\s*)?\d{1,2}:\d{2})?)"
    r"|(?<![\d:.])\d{1,2}:\d{2}(?![\d:])", re.I)


def _ok_label(chunk):
    """Post time when it is not on a line of its own: <time datetime>, an element with class date/time, then the first
    mention in the text (video durations are discarded). -> ("ts", unix) | ("label", text) | None"""
    m = re.search(r'<time\b[^>]*\bdatetime\s*=\s*"([^"]+)"', chunk, re.I)
    if m:
        ts = parse_date(html.unescape(m.group(1)))
        if ts:
            return "ts", ts
    c = re.sub(r'<(\w+)\b[^>]*class="[^"]*(?:duration|video-length|vid_len)[^"]*"[^>]*>.*?</\1>', " ", chunk, flags=re.S | re.I)
    for m in re.finditer(r'<(\w+)\b[^>]*class="[^"]*(?:date|time|ago)[^"]*"[^>]*>(.*?)</\1>', c, re.S | re.I):
        mm = _OK_INLINE.search(re.sub(r"\s+", " ", html_to_text(m.group(2))))
        if mm:
            return "label", mm.group(0)
    mm = _OK_INLINE.search(html_to_text(re.sub(r"(</(?:a|span|button|time)>)\s*(?=<(?:a|span|button|time|div|li)\b)", r"\1\n", c)))
    return ("label", mm.group(0)) if mm else None


def parse_ok_post_time(page, now=None):
    """Single post page ok.ru/<group>/topic/<id> -> publication time (unix) or None.
    Order of reliability: <time datetime>, meta/JSON-LD, the date caption in the text above the title."""
    now = now or int(time.time())
    for pat in (r'<time\b[^>]*\bdatetime\s*=\s*"([^"]+)"',
                r'<meta[^>]+(?:article:published_time|datePublished|og:updated_time)[^>]+content\s*=\s*"([^"]+)"',
                r'"(?:datePublished|dateCreated)"\s*:\s*"([^"]+)"'):
        m = re.search(pat, page, re.I)
        if m:
            ts = parse_date(html.unescape(m.group(1)))
            if ts and 1262304000 < ts < now + 86400:
                return ts
    m = re.search(r'"(?:publishedMs|createdMs|date|created|publishTime|time)"\s*:\s*(1[0-9]{9}(?:[0-9]{3})?)\b', page)
    if m:
        v = int(m.group(1))
        v = v // 1000 if v > 10**11 else v
        if 1262304000 < v < now + 86400:
            return v
    c = re.sub(r'<(\w+)\b[^>]*class="[^"]*(?:duration|video-length|vid_len)[^"]*"[^>]*>.*?</\1>', " ", page, flags=re.S | re.I)
    c = re.sub(r"<head\b.*?</head>", " ", c, flags=re.S | re.I)
    spaced = re.sub(r"(</(?:a|span|button|time)>)\s*(?=<(?:a|span|button|time|div|li)\b)", r"\1\n", c)
    parts = []
    for line in html_to_text(spaced).split("\n"):
        tl = _ok_time_line(line.strip())
        if tl:
            parts.append(tl)
            if len(parts) >= 3:
                break
    when = _ok_when(parts)
    if not when:
        return None
    ts = parse_ru_date(when if re.search(r"[а-я]", when) else "сегодня в " + when, now)
    if ts and not re.search(r"[а-я]", when) and ts > now + 600:
        ts -= 86400
    return ts


_BAD_TITLE = re.compile(r"^(?:показать\s+(?:ещё|еще|полностью)|читать далее|подробнее|ещё|еще|без заголовка|untitled)\W*$", re.I)
_OK_SUFFIX = re.compile(r"\s*[|｜]\s*Вступай(?:те)? в группу.*$", re.I | re.S)


def _clean_ok(t):
    """Strips the OK suffix "| Вступай в группу …" that the site appends to a post description."""
    return _OK_SUFFIX.sub("", t or "").strip()


def _bad_title(t):
    t = (t or "").strip()
    return not t or bool(_BAD_TITLE.match(t)) or not re.search(r"\w", t)


def _ok_split(txt):
    """Post text -> (title, text): the first line; if the text is one block, the first sentence."""
    txt = (txt or "").strip()
    if "\n" not in txt and len(txt) > 120:
        m = re.search(r"^(.{20,200}?[.!?…])\s+(?=\S)", txt, re.S)
        if m:
            return m.group(1).strip(), txt[m.end():].strip()
    return split_title_text(txt)


def ok_normalize(title, text):
    """Unified OK post format: title = first sentence/line, the rest below; without the group suffix, view counter,
    "ещё N фото" and placeholder badges."""
    t, x = _clean_ok(title), _clean_ok(text)
    t = "" if (not re.search(r"\w", t or "") or _OK_JUNK.match(t.strip())) else t
    if _BAD_TITLE.match(t or ""):
        t = ""
    x = "\n".join(ln for ln in (x or "").split("\n") if (re.search(r"\w", ln) and not _OK_JUNK.match(ln.strip())) or not ln.strip()).strip()
    if not t:
        return _ok_split(x) if x else ("Untitled", "")
    if t.endswith("…") and x.replace("\n", " ").startswith(t[:-1].rstrip()):  # the title is a truncated start of the text: split at a sentence
        return _ok_split(x)
    if not x and len(t) > 120:
        return _ok_split(t)
    return t, x


def _ok_needs_repair(r):
    return (not (r["text"] or "").strip() or _bad_title(r["title"]) or "Вступай" in (r["title"] or "") + (r["text"] or "")
            or (r["text"] or "").rstrip().endswith(("...", "…")) and len(r["text"]) < 700)


def _ok_links(html_part, limit=6):
    """Hyperlinks to other sites from the post text -> [{"t": label, "u": url}] (links inside OK and service links are skipped)."""
    out, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>', html_part or "", re.S | re.I):
        h = html.unescape(m.group(1))
        if "logExternal" in h or "/dk?" in h:
            h = urllib.parse.parse_qs(urllib.parse.urlparse(h).query).get("st.link", [None])[0] or ""
        if not h.startswith("http"):
            continue
        host = (urllib.parse.urlparse(h).netloc or "").lower()
        if not host or re.search(r"(?:^|\.)(?:ok\.ru|okcdn\.ru|mycdn\.me|odnoklassniki\.ru|mail\.ru)$", host) or h in seen:
            continue
        seen.add(h)
        label = html_to_text(m.group(2)).strip().replace("\n", " ")
        if not re.search(r"\w", label) or label.startswith("http"):
            label = host.removeprefix("www.")
        out.append({"t": label[:80], "u": h})
        if len(out) >= limit:
            break
    return out


def _ok_ext_link(page, group):
    for h in re.findall(r'href="([^"]+)"', page or ""):
        h = html.unescape(h)
        link = urllib.parse.parse_qs(urllib.parse.urlparse(h).query).get("st.link", [None])[0] if ("logExternal" in h or "/dk?" in h) else h
        if link and link.startswith("http") and "ok.ru" not in link and group.lower() in (urllib.parse.urlparse(link).netloc or "").lower():
            return link
    return None


def parse_ok_post(page, now=None, group=None):
    """ok.ru post page -> dict(time, text, image): publication time, full text (og:description) and image."""
    meta = _meta_tags(page or "")
    text = (meta.get("og:description") or meta.get("description") or "").strip()
    if not text:
        m = re.search(r'"(?:articleBody|description)"\s*:\s*"((?:[^"\\]|\\.){10,})"', page or "")
        if m:
            try:
                text = json.loads('"' + m.group(1) + '"').strip()
            except ValueError:
                text = ""
    m = re.search(r'<div\b[^>]*class="[^"]*media-text_cnt_tx[^"]*"[^>]*>(.*?)</div>', page or "", re.S | re.I)
    full = html_to_text(m.group(1)).strip() if m else ""
    plinks = _ok_links(m.group(1)) if m else None
    text = _clean_ok(text)
    if full and len(full) >= len(text) - 3:
        text = full
    img = meta.get("og:image") or ""
    if img.startswith("//"):
        img = "https:" + img
    if img and ("okcdn.ru" not in img or "avatar" in img.lower()):
        img = img if "okcdn.ru" in img else ""
    return dict(time=parse_ok_post_time(page or "", now), text=html_to_text(text), image=img or None,
                ext=_ok_ext_link(page, group) if group else None, links=plinks)


def _ok_fill(it, d):
    """Fills a post in from its page: empty text, a placeholder instead of a title, image."""
    txt = (d.get("text") or "").strip()
    if txt and _bad_title(it["title"]):
        it["title"], it["text"] = _ok_split(txt)
    elif txt and not (it.get("text") or "").strip():
        t0 = it["title"].strip().rstrip("…")
        body = txt[len(t0):].lstrip(" \n:—-") if t0 and txt.startswith(t0) else txt
        it["text"] = "" if body.strip() == it["title"].strip() else body
    if d.get("image") and not it.get("image"):
        it["image"] = d["image"]
    if d.get("ext") and not it.get("ext_url"):
        it["ext_url"] = d["ext"]
    if d.get("links") is not None and not it.get("links"):
        it["links"] = d["links"]


OK_TEXT_TRIED = set()


def ok_repair_texts(g, src, limit=40):
    """Already stored OK posts have no text (or "Показать ещё" instead of a title) — read the rest from the post page."""
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id,title,text,url,image,ext_url,links FROM items WHERE source=? AND url LIKE 'http%' "
            "ORDER BY published DESC LIMIT 1500", (src["id"],))]
    rows = [r for r in rows if r["id"] not in OK_TEXT_TRIED and (_ok_needs_repair(r) or r["links"] is None)][:limit]
    for r in rows:
        OK_TEXT_TRIED.add(r["id"])
        try:
            d = parse_ok_post(g.text(r["url"], nav_idle=False, until=lambda h: True), group=src.get("domain"))
        except Exception:
            if time.time() - g.t0 > g.budget:
                break
            continue
        new = dict(title=r["title"], text=r["text"], image=r["image"], ext_url=r["ext_url"], links=json.loads(r["links"]) if r["links"] else None)
        if (d.get("text") or "").strip() and (_bad_title(r["title"]) or "Вступай" in r["title"] + r["text"]
                                              or len(d["text"]) > len(r["text"]) + len(r["title"])):
            new["title"], new["text"] = _ok_split(d["text"])  # unified format: title = first line, text below
            if d.get("image") and not new["image"]:
                new["image"] = d["image"]
        else:
            _ok_fill(new, d)
        new["title"], new["text"] = ok_normalize(new["title"], new["text"])
        if new.get("links") is None:
            new["links"] = d.get("links") if d.get("links") is not None else []
        if not new.get("ext_url") and d.get("ext"):
            new["ext_url"] = d["ext"]
        _w_update_ok_item(r["id"], new)


def ok_post_times(g, src, items, limit=40):
    """Fetches the exact time for posts where it is not yet known: opens the post page.
    Errors on individual posts are not fatal — the estimate from the feed remains."""
    ids = [make_id(src["id"], it["guid"]) for it in items]
    with db() as con:
        have = {r[0]: r[1] for r in con.execute(
            f"SELECT id, published FROM items WHERE exact=1 AND id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    done = fails = 0
    for it, iid in zip(items, ids):
        if iid in have:
            it["published"], it["precise"] = have[iid], True
            continue
        if done >= limit or fails >= 3:
            continue
        try:
            d = parse_ok_post(g.text(it["url"], nav_idle=False, until=lambda h: True), group=src.get("domain"))
            ts = d["time"]
            _ok_fill(it, d)
        except Exception:
            ts = None
            if time.time() - g.t0 > g.budget:
                break
        done += 1
        if ts:
            it["published"], it["precise"] = ts, True
        else:
            fails += 1
    return done


_OK_PIN_STRONG = re.compile(r"закреп|pinned", re.I)
_OK_PIN_LOOSE = re.compile(r"[-_]pin\b|\bpin[-_]|sticky", re.I)


def _ok_when(parts):
    """Date/time lines of one post -> a single string for parse_ru_date."""
    clock = [p for p in parts if re.fullmatch(r"\d{1,2}:\d{2}", p)]
    full = [p for p in parts if re.search(r"[а-я]", p) and re.search(r"\d{1,2}:\d{2}", p)]
    day = [p for p in parts if re.search(r"[а-я]", p) and not re.search(r"\d{1,2}:\d{2}", p)]
    if full:
        return full[0]
    if day and clock:
        return day[0] + " " + clock[0]
    return day[0] if day else (clock[0] if clock else None)


def parse_ok(page, group, now=None, pinned_out=None, stats=None):
    """ok.ru group page -> (items, number of posts found). Split on links /<group>/topic/<id>.
    The pinned post is skipped (its guid goes to pinned_out). Item order = order on the page."""
    now = now or int(time.time())
    prefix = "/" + group.lower() + "/topic/"
    first, order = {}, []
    for m in _OK_TOPIC.finditer(page):
        path = urllib.parse.urlparse(html.unescape(m.group(1))).path
        if not (path.lower().startswith(prefix) or path.startswith("/group/")):
            continue  # posts of other groups from side blocks
        if m.group(2) not in first:
            first[m.group(2)] = (max(page.rfind("<a ", 0, m.start()), 0), path)
            order.append(m.group(2))
    items = []
    for idx, tid in enumerate(order):
        a = first[tid][0]
        b = first[order[idx + 1]][0] if idx + 1 < len(order) else min(len(page), a + 25000)
        chunk = re.sub(r'<(\w+)\b[^>]*class="[^"]*(?:duration|video-length|vid_len)[^"]*"[^>]*>.*?</\1>', " ", page[a:b], flags=re.S | re.I)
        lines, parts, seen, more = [], [], set(), False
        spaced = re.sub(r"(</(?:a|span|button|time)>)\s*(?=<(?:a|span|button|time|div|li)\b)", r"\1\n", chunk)
        for line in html_to_text(spaced).split("\n"):
            line = line.strip()
            if not line:
                continue
            tl = _ok_time_line(line)
            if tl:
                if len(parts) < 4:
                    parts.append(tl)
                continue
            if lines and re.match(r"(?:поделились|поделиться|комментировать|класс)\b", line, re.I):
                break  # post footer: everything below is the next post (the page slice caught its beginning)
            if re.match(r"(?:показать|читать)\s+(?:ещ[её]|полностью|далее)", line, re.I):
                more = True  # the post is collapsed: the text is on the post page — read it from there
            if (_OK_NOISE.match(line) or _OK_JUNK.match(line) or re.fullmatch(r"[\d\s.,kк]+", line) or line in seen
                    or re.fullmatch(r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", line.lower())):
                continue
            seen.add(line)
            lines.append(line)
        ext = None
        for h in re.findall(r'href="([^"]+)"', chunk):
            h = html.unescape(h)
            link = None
            if "logExternal" in h or "/dk?" in h:
                link = urllib.parse.parse_qs(urllib.parse.urlparse(h).query).get("st.link", [None])[0]
            elif h.startswith("http") and "ok.ru" not in h and "okcdn.ru" not in h and "mail.ru" not in h:
                link = h
            # the original source is the article on the publication's own site (kinopoisk.ru for the kinopoisk group), not just any external link
            if link and link.startswith("http") and group.lower() in (urllib.parse.urlparse(link).netloc or "").lower():
                ext = link
                break
        links = _ok_links(chunk)
        imgs = _ok_images(chunk)
        image = imgs[0] if imgs else None
        if not lines and not image and not more:
            continue
        when, ts = _ok_when(parts), None
        lab = _ok_label(chunk)
        if lab and lab[0] == "ts":  # the exact time from <time datetime> is more reliable than any caption
            ts, when = lab[1], None
        elif not when:
            if lab:
                when = lab[1]
                pat = re.compile(r"\s+".join(map(re.escape, when.split())), re.I)  # remove the time label from the post text
                lines = [x for x in (pat.sub("", ln, count=1).strip(" ·•|,-—") for ln in lines) if x]
        if when:
            t = when if re.search(r"[а-я]", when) else "сегодня в " + when
            ts = parse_ru_date(t, now)
            if ts and not re.search(r"[а-я]", when) and ts > now + 600:
                ts -= 86400  # time without a date but "from the future" -> it was yesterday
        marked = bool(_OK_PIN_STRONG.search(chunk) or (idx == 0 and _OK_PIN_LOOSE.search(chunk)))
        title, body = _ok_split(lines[0]) if len(lines) == 1 else split_title_text("\n".join(lines))
        title, body = ok_normalize(title, body)
        items.append(dict(guid=tid, title=title or "Untitled", text=body, url="https://ok.ru" + first[tid][1],
                          ext_url=ext, links=links, image=image, media=[{"u": u, "t": "image"} for u in imgs], published=ts, exact=True, _pin=marked,
                          _tonly=bool(when and not re.search(r"[а-я]", when))))
    # pinned post: marked "pinned", or it comes first but is older than the next post
    if items and not items[0]["_pin"] and not items[0]["_tonly"] and len(items) > 1 and items[0]["published"] \
            and items[1]["published"] and items[0]["published"] < items[1]["published"] - 60:  # an explicit date ("12 фев"), not "09:12"
        items[0]["_pin"] = True
    # topic ids grow over time: in a normal feed they decrease from top to bottom.
    # A first post whose id is lower than the next one is pinned (it was pinned later than it was published)
    try:
        while len(items) > 1 and not items[0]["_pin"] and int(items[0]["guid"]) < int(items[1]["guid"]):
            items[0]["_pin"] = True
            break
    except ValueError:
        pass
    kept, prev = [], None
    for it in items:
        tonly = it.pop("_tonly")
        if it.pop("_pin"):
            if pinned_out is not None:
                pinned_out.append(it["guid"])
            continue
        ts = it["published"]
        it["undated"] = ts is None
        if ts is None and stats is not None:
            stats["undated"] = stats.get("undated", 0) + 1
        if tonly and ts is not None and prev is not None:  # "21:05" without a date after a "09:12" post is already yesterday
            for _ in range(2):
                if ts >= prev:
                    ts -= 86400
        if prev is not None and (ts is None or ts >= prev):
            # newer posts are above older ones on the page: source order beats time parsing; without a time — a step down from the previous one
            ts = prev - (1800 if ts is None else 1)
        elif ts is None:
            ts = now
        it["published"] = prev = ts
        kept.append(it)
    return kept, len(order)


def fetch_ok(src):
    """Public page of an Odnoklassniki group (ok.ru/<group>): latest posts, no token."""
    dom, url = src["domain"], src["url"]
    ready = lambda h: bool(parse_ok(h, dom)[1])
    g = PageGetter()
    try:
        try:
            page = g.text(url, until=ready)
        except (urllib.error.URLError, OSError) as e:
            if is_ssl_error(e):
                raise
            g.enable_browser()
            page = g.text(url, until=ready)
        pinned, stats = [], {}
        items, raw = parse_ok(page, dom, pinned_out=pinned, stats=stats)
        if not raw and not g.browser_mode:
            g.enable_browser()
            page = g.text(url, until=ready)
            pinned, stats = [], {}
            items, raw = parse_ok(page, dom, pinned_out=pinned, stats=stats)
        if not raw:
            raise RuntimeError(f"the OK group page has no posts ({describe_page(page)}). Run python server.py --diagnose")
        if stats.get("undated"):
            STATUS_NOTE[src["id"]] = (f"time not determined for {stats['undated']} of {raw} posts, order taken from the page; "
                                      "share the output of python server.py --diagnose")
        if pinned:  # a pinned post is not wanted in the feed — remove the previously stored one too (except bookmarked)
            ids = [make_id(src["id"], gid) for gid in pinned]
            _w_delete_items(ids)
        known = known_ids(src["id"])
        if known and items and not any(make_id(src["id"], it["guid"]) in known for it in items):
            try:  # gap after downtime: scroll the feed until an already stored post is met
                items = _ok_catchup(src, items, known)
            except BrowserBusy:
                HEAL_PENDING.add(src["id"])
                STATUS_NOTE[src["id"]] = "missed posts will be loaded when the browser is free"
            except Exception as e:
                STATUS_NOTE[src["id"]] = f"could not load the missed posts ({type(e).__name__}: {str(e)[:80]})"
        try:
            ok_post_times(g, src, items, limit=max(40, len(items)) if len(items) <= 120 else 120)
            ok_repair_texts(g, src, limit=30)
        except Exception as e:
            STATUS_NOTE[src["id"]] = f"exact time could not be read from post pages ({type(e).__name__}); time estimated from the feed"
    finally:
        g.close()
    with db() as con:  # for posts without a time keep the stored one: otherwise it would drift forward on every refresh
        prev = None
        for it in items:
            row = con.execute("SELECT published FROM items WHERE id=?", (make_id(src["id"], it["guid"]),)).fetchone()
            if it.get("precise"):  # do not move the exact time from the post page
                prev = it["published"]
                continue
            if it.get("undated") and row:
                it["published"] = row[0]
            if prev is not None and it["published"] >= prev:
                it["published"] = prev - 1
            prev = it["published"]
    return items


STATUS_NOTE = {}


HEAL_PENDING = set()   # OK groups that may have a gap of posts between refreshes: scan the feed deeper
HEAL_LAST = {}


def _ok_scroll_until(g, dom, stop, max_rounds=40):
    """Scrolls an OK group feed (page already open) until stop(posts) returns True. Patient with slow loading:
    stops only when the post count does not grow 4 times in a row. -> (html, reached_condition)."""
    last, stall = 0, 0
    h = g._content()
    for _ in range(max_rounds):
        posts = parse_ok(h, dom)[0]
        if stop(posts):
            return h, True
        if len(h) > SCROLL_MAX_HTML or len(posts) >= SCROLL_MAX_ITEMS:
            break
        stall = stall + 1 if len(posts) <= last else 0
        if stall >= 4:
            break
        last = len(posts)
        try:
            g._page.evaluate(_MORE_JS)
            g._page.keyboard.press("End")
        except Exception:
            pass
        g._page.wait_for_timeout(2000)
        h = g._content()
    return h, False


def _ok_open(src):
    g = PageGetter(budget=300)
    try:
        g.enable_browser(light=True)
        g._goto(src["url"])
        g._page.wait_for_timeout(2500)
    except BaseException:
        g.close()
        raise
    return g


def _ok_catchup(src, items, known):
    """Gap after downtime: scroll an OK group in the light browser until an already stored post is met. If not reached, the group goes to HEAL_PENDING."""
    sid, dom = src["id"], src["domain"]
    g = _ok_open(src)
    try:
        h, reached = _ok_scroll_until(g, dom, lambda posts: any(make_id(sid, it["guid"]) in known for it in posts))
    finally:
        g.close()
    if not reached:
        HEAL_PENDING.add(sid)
    pinned = []
    got, _raw = parse_ok(h, dom, pinned_out=pinned)
    seen = {it["guid"] for it in items}
    return items + [it for it in got if it["guid"] not in seen and it["guid"] not in pinned
                    and make_id(sid, it["guid"]) not in known]


def ok_heal(src, days):
    """Closes gaps within the last `days` days: scroll the group feed to posts older than that boundary and save everything
    that is not in the DB (previously a gap that appeared between refreshes stayed forever: new checks saw a "familiar" post at the top)."""
    sid, dom = src["id"], src["domain"]
    cutoff = int(time.time()) - days * 86400
    g = _ok_open(src)
    try:
        h, _ = _ok_scroll_until(g, dom, lambda posts: bool(posts) and min(p["published"] for p in posts) <= cutoff, max_rounds=60)
    finally:
        g.close()
    pinned = []
    got, _raw = parse_ok(h, dom, pinned_out=pinned)
    known = known_ids(sid)
    new = [it for it in got if it["guid"] not in pinned and make_id(sid, it["guid"]) not in known]
    if new:
        g2 = PageGetter()
        try:
            ok_post_times(g2, src, new, limit=60)
        except Exception:
            pass
        finally:
            g2.close()
        save_items(sid, new)
    HEAL_PENDING.discard(sid)
    HEAL_LAST[sid] = time.time()
    return len(new)


def heal_pass():
    """Every few hours (and right after a failed catch-up) scan the OK group feeds. True means the scan was done."""
    did = False
    days = BF_DAYS or 30
    for src in get_sources():
        if src["type"] != "ok" or not BF["enabled"] or STATE["refreshing"]:
            continue
        if src["id"] in HEAL_PENDING or time.time() - HEAL_LAST.get(src["id"], 0) > 3 * 3600:
            try:
                n = ok_heal(src, days)
                did = True
                if n:
                    print(f"[{src['id']}] missed posts recovered: {n}")
            except BrowserBusy:
                pass  # browser busy — retry later
            except Exception as e:
                HEAL_LAST[src["id"]] = time.time() - 3 * 3600 + 600  # again in 10 minutes
                print(f"[{src['id']}] gap search: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
    return did


def fetch_vk(src):
    return fetch_vk_api(src) if VK_TOKEN else fetch_vk_mobile(src)


def _site_listing(g, url, first, src):
    """Section page; falls back to the browser on refusal/empty page. -> (page, links found)."""
    def extract(page):
        return auto_extract_links(page, src["url"]) if src.get("auto_links") else extract_links(page, src["url"], src["link_re"])
    ready = lambda h: bool(extract(h))
    try:
        page = g.text(url, until=ready)
    except (urllib.error.URLError, OSError):
        if not first or g.browser_mode:
            raise
        g.enable_browser()
        page = g.text(url, until=ready)
    found = extract(page)
    if not found and first and not g.browser_mode:
        if src.get("try_feeds"):
            items = _try_feeds(page, src["url"])
            if items:
                return page, items
        g.enable_browser()
        page = g.text(url, until=ready)
        found = extract(page)
    return page, found


def _try_feeds(page, base, accept=None):
    root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(base))
    for cand in filter(None, [find_feed_url(page, base)] + [root + p for p in ("/rss", "/feed", "/rss.xml", "/news/rss", "/rss/news")]):
        try:
            data, _ = http_get(cand, timeout=15)
            if data.lstrip()[:200].lower().startswith((b"<?xml", b"<rss", b"<feed")):
                items = parse_feed(data, cand)
                if items:
                    return items
        except (urllib.error.URLError, ET.ParseError, OSError):
            continue
    return None


def _read_articles(g, urls, source_id, base_now=None):
    def one(u):
        try:
            return parse_article_page(g.text(u, nav_idle=False, timeout=15, max_bytes=400000), u)
        except (BrowserBusy, MemoryError):
            raise
        except Exception as e:  # one unavailable article must not break the source
            g.last_err = f"{type(e).__name__}: {str(e)[:120]}"
            return None
    if g.browser_mode:  # Playwright cannot be used from several threads
        res = [one(u) for u in urls]
    else:
        with ThreadPoolExecutor(max_workers=5) as ex:
            res = list(ex.map(one, urls))
        if urls and not any(res) and time.time() - g.t0 < g.budget:  # articles were not returned to a plain request (bot protection) — read them with the real browser, one by one
            g.enable_browser(light=True)
            res = [one(u) for u in urls[:40]]
    now = base_now or int(time.time())
    out = []
    for idx, it in enumerate(res):
        if not it or not it["title"]:
            continue
        if not it["published"]:
            m = re.search(r"/(\d{4}-\d{2}-\d{2})/", it["url"])
            # date from the article URL; otherwise the time of first discovery (same order as on the site)
            it["published"] = parse_date(m.group(1) + "T12:00:00+03:00") if m else now - idx * 10
        out.append(it)
    return out


def fetch_site(src, limit=60):
    """Site/section without RSS: article links from the section pages, then OpenGraph of each article.
    If the site does not answer plain requests (bot protection) — the real browser (Playwright)."""
    g = PageGetter()
    try:
        known = known_ids(src["id"])
        links, max_pages = [], (10 if known else src.get("pages", 2))
        for n in range(1, max_pages + 1):
            try:
                _, found = _site_listing(g, _paged(src["url"], n, "page"), n == 1, src)
            except Exception:
                if n > 1:
                    break  # no more pages
                raise
            if n == 1 and found and isinstance(found[0], dict):
                return found  # an RSS feed was found
            links += [u for u in found if u not in links]
            if not found:
                if n == 1:
                    raise RuntimeError("the section page has no news links: the site returned an empty page or blocked the request")
                break
            if any(make_id(src["id"], u) in known for u in found):
                break
        gap = bool(known) and not any(make_id(src["id"], u) in known for u in links)
        if gap:  # after downtime, between the last stored item and the one visible on the first pages there is a gap: close it via the sitemap / day pages
            try:
                extra_items = _site_catchup(src, g, known, links)
            except Exception as e:
                extra_items = []
                STATUS_NOTE[src["id"]] = f"could not close the gap in news ({type(e).__name__})"
            if extra_items:
                got = {it["url"] for it in extra_items}
                rest = [u for u in links if u not in got and make_id(src["id"], u) not in known][:limit]
                return extra_items + (_read_articles(g, rest, src["id"]) if rest else [])
        if not links:
            raise RuntimeError("the section pages have no news links: the site returned an empty page or blocked the request")
        fresh = [u for u in links if make_id(src["id"], u) not in known][:limit]
        if not fresh:
            return []
        out = _read_articles(g, fresh, src["id"])
        if not out:
            raise RuntimeError("article pages returned no data (possible bot protection or captcha)"
                               + (f"; last error: {g.last_err}" if getattr(g, "last_err", None) else ""))
        return out
    finally:
        g.close()


CATCHUP_SITEMAP = 150


def _newest_known(sid):
    with db() as con:
        r = con.execute("SELECT MAX(published) FROM items WHERE source=?", (sid,)).fetchone()
    return r[0] if r and r[0] else 0


def _site_catchup(src, g, known, shown):
    """Closes the forward gap: fresh articles from the sitemap (newest first), or, if there is none, from day pages back to the date of the last stored one."""
    sid = src["id"]
    SITEMAP_CACHE.pop(sid, None)  # a fresh sitemap is needed
    urls = [u for u in _sitemap_urls(src) if make_id(sid, u) not in known][:CATCHUP_SITEMAP]
    if urls:
        return _read_articles(g, urls, sid)
    newest = _newest_known(sid)
    if not newest or not src.get("link_re") or r"\d{4}-\d{2}-\d{2}" not in src["link_re"]:
        return []
    stop = datetime.fromtimestamp(newest, timezone(timedelta(hours=VK_TZ_HOURS))).date()
    out, cur = [], {}
    for _ in range(40):
        r = _bf_site_days(src, cur)
        if not r:
            break
        items, cur, done, _msg = r
        out += items
        if done or datetime.strptime(cur["day"], "%Y-%m-%d").date() < stop:
            break
    return out


FETCHERS = {"rss": fetch_rss, "telegram": fetch_telegram, "vk": fetch_vk, "site": fetch_site, "ok": fetch_ok}
REFRESH_DEADLINE = int(os.environ.get("REFRESH_DEADLINE", "300"))  # seconds for the whole refresh
STATE = {"refreshing": False, "last_refresh": None, "busy": set(), "slow": 0}
STATE_LOCK = threading.Lock()


SLOW_SOURCES = set()  # sources that need the browser or respond slowly
SLOW_INTERVAL = 20 * 60  # such sources refresh at most once per 20 minutes unless explicitly asked


def refresh_source(src):
    if src["id"] in STATE["busy"]:
        return  # already refreshing (the slow source is still running from last time)
    STATE["busy"].add(src["id"])
    try:
        STATUS_NOTE.pop(src["id"], None)
        t0 = time.time()
        items = FETCHERS[src["type"]](src)
        t_fetch = time.time() - t0
        new = save_items(src["id"], items)
        note = STATUS_NOTE.pop(src["id"], "")
        took = time.time() - t0
        if took > 20:
            SLOW_SOURCES.add(src["id"])  # longer than 20 s (browser, site protection) — next time go straight to the "slow" lane
        else:
            SLOW_SOURCES.discard(src["id"])
        set_status(src["id"], True, f"fetched {len(items)}, new {new}, {took:.1f} s (download {t_fetch:.1f} s)" + (f" ({note})" if note else ""), new)
    except Exception as e:  # one source must not break the others
        msg = f"{type(e).__name__}: {e}"[:300]
        if is_ssl_error(e) or "CERTIFICATE_VERIFY_FAILED" in str(e):
            msg = ("the site's SSL certificate check fails (this computer lacks the required root certificate). "
                   "Fix: run with NEWS_INSECURE_SSL=1 or install the certificates — see README (Certificate error section)")
        set_status(src["id"], False, msg)
        print(f"[{src['id']}] error: {msg}", file=sys.stderr)
    finally:
        STATE["busy"].discard(src["id"])


ENRICH_FAILS = {}


def enrich_images(limit=25, source_id=None):
    """For entries without an image take og:image from the article page (once; an empty string = page read, no image).
    If the site refuses plain requests (403) — open the articles in the browser. Network errors are not marked "no image": retry later."""
    with db() as con:
        rows = con.execute(
            "SELECT i.id,i.url FROM items i JOIN sources s ON s.id=i.source WHERE i.image IS NULL AND i.url LIKE 'http%'"
            " AND s.active=1 AND s.type IN ('rss','site')" + (" AND i.source=?" if source_id else "") +
            " ORDER BY i.published DESC LIMIT ?", ((source_id,) if source_id else ()) + (3000,)).fetchall()
    # skip hopeless ones (2+ failures) but do not let them block older articles
    rows = [r for r in rows if ENRICH_FAILS.get(r["id"], 0) < 2][:limit]
    blocked = []

    def one(row):
        try:
            page = http_text(row["url"], timeout=12, max_bytes=300000)
        except Exception as e:
            if _blocked(e):
                blocked.append(row)
            else:
                ENRICH_FAILS[row["id"]] = ENRICH_FAILS.get(row["id"], 0) + 1
            return None
        meta = _meta_tags(page)
        img = meta.get("og:image") or meta.get("twitter:image")
        return row["id"], urllib.parse.urljoin(row["url"], img) if img else ""
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = [r for r in ex.map(one, rows) if r]
    if blocked:  # the browser cannot be used from several threads — one by one, with a time limit
        g = PageGetter(budget=240)
        try:
            g.enable_browser(light=True)
            for row in blocked[:40]:
                try:
                    page = g.text(row["url"], nav_idle=False)
                    meta = _meta_tags(page)
                    img = meta.get("og:image") or meta.get("twitter:image")
                    res.append((row["id"], urllib.parse.urljoin(row["url"], img) if img else ""))
                except Exception:
                    ENRICH_FAILS[row["id"]] = ENRICH_FAILS.get(row["id"], 0) + 1
        except BrowserBusy:
            pass  # browser is busy with another source — not the article's fault, retry next time
        except Exception:
            for row in blocked:
                ENRICH_FAILS[row["id"]] = ENRICH_FAILS.get(row["id"], 0) + 1
        finally:
            g.close()
    _w_set_images(res)


def _finish_slow(futs):
    """Slow sources (browser-based: Kinopoisk) finish in the background without holding up the overall refresh."""
    pending, deadline = set(futs), time.time() + REFRESH_DEADLINE
    while pending and time.time() < deadline:
        _, pending = wait(pending, timeout=1)
    for f in pending:  # browser hung or crashed
        s = futs[f]
        STATE["busy"].discard(s["id"])
        set_status(s["id"], False, f"did not finish within {REFRESH_DEADLINE} s, skipped (will update on the next refresh)")
        print(f"[{s['id']}] refresh aborted on timeout", file=sys.stderr)
    STATE["slow"] = 0
    STATE["last_refresh"] = int(time.time())  # the UI will show “new posts”


def _is_slow(src):
    return src["type"] not in ("telegram", "rss", "vk") or src["id"] in SLOW_SOURCES


def refresh_all(force=False):
    """Fast sources (Telegram, RSS without a browser) are polled in parallel, and the refresh counts as finished as soon as they
    are done (but no later than 40 s). Slow ones (sites and OK via a browser, protected RSS) finish in the background; without an explicit
    request (force — the “Refresh” button) they are polled no more than once per SLOW_INTERVAL."""
    with STATE_LOCK:
        if STATE["refreshing"]:
            return False
        STATE["refreshing"] = True
    ex_fast, ex_slow = ThreadPoolExecutor(max_workers=8), ThreadPoolExecutor(max_workers=2)
    try:
        with db() as con:
            checked = {r[0]: r[1] or 0 for r in con.execute("SELECT source, checked FROM source_status")}
        srcs = [s for s in get_sources() if s["id"] not in STATE["busy"]]
        fast, slow = {}, {}
        for s_ in srcs:
            if _is_slow(s_):
                if force or time.time() - checked.get(s_["id"], 0) > SLOW_INTERVAL:
                    slow[ex_slow.submit(refresh_source, s_)] = s_
            else:
                fast[ex_fast.submit(refresh_source, s_)] = s_
        pending, deadline = set(fast), time.time() + 40
        while pending and time.time() < deadline:
            _, pending = wait(pending, timeout=0.5)
        for f in pending:  # did not finish in 40 s — completes in the background like a slow one
            slow[f] = fast[f]
            SLOW_SOURCES.add(fast[f]["id"])
        STATE["last_refresh"] = int(time.time())
        if slow:
            STATE["slow"] = len(slow)
            threading.Thread(target=_finish_slow, args=(slow,), daemon=True).start()
    finally:
        ex_fast.shutdown(wait=False)
        ex_slow.shutdown(wait=False)
        STATE["refreshing"] = False
    return True


def tg_repair_media(pages=6):
    """Previously saved Telegram posts (from before the collage existed) lack the full media set: re-read the channel pages."""
    for src in get_sources():
        if src["type"] != "telegram":
            continue
        with db() as con:
            rows = con.execute("SELECT id,url,media FROM items WHERE source=? ORDER BY published DESC LIMIT 200", (src["id"],)).fetchall()
        for chan in _tg_channels(src):
            need = {}
            for r in rows:
                try:
                    n = len(json.loads(r["media"] or "[]"))
                except ValueError:
                    n = 0
                m = re.search(r"t\.me/([^/]+)/(\d+)$", r["url"] or "")
                if m and m.group(1).lower() == chan.lower() and n <= 1:
                    need[r["id"]] = (int(m.group(2)), n)
            if not need:
                continue
            before = max(v[0] for v in need.values()) + 1
            for _ in range(pages):
                got, min_id = _tg_parse(src, chan, http_text(f"https://t.me/s/{chan}?before={before}"))
                if min_id is None:
                    break
                _w_set_media([(make_id(src["id"], it["guid"]), it) for it in got
                              if make_id(src["id"], it["guid"]) in need and len(it["media"]) > need[make_id(src["id"], it["guid"])][1]])
                if min_id <= min(v[0] for v in need.values()):
                    break
                before = min_id


def enrich_loop():
    """Background image top-up for all (including old) articles: does not interfere with refresh, takes a browser only if one is free."""
    _PW_TLS.nowait = True
    n = 0
    while True:
        time.sleep(30 if n == 0 else 40)
        n += 1
        if n % 80 == 0:
            ENRICH_FAILS.clear()  # once an hour give the “hopeless” ones another chance
        try:
            enrich_images(limit=60)
        except Exception as e:
            print(f"[images] error: {e}", file=sys.stderr)
        if n == 1 or n % 80 == 0:
            try:
                tg_repair_media()
            except Exception as e:
                print(f"[media] error: {e}", file=sys.stderr)


# ---------------------------------------------------------------- adding a source by link


def _slug(s, n=22):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "src"


def detect_source(raw):
    """Link -> source description {id,name,type,url,config}. Raises ValueError with a clear message."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Enter a link")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    u = urllib.parse.urlparse(raw)
    host = (u.netloc or "").lower().split(":")[0]
    if "." not in host:
        raise ValueError("This does not look like a website link")
    parts = [p for p in u.path.split("/") if p]
    h = host.removeprefix("www.").removeprefix("m.")

    if h in ("t.me", "telegram.me", "telegram.dog"):
        if parts and parts[0] == "s":
            parts = parts[1:]
        if not parts or not re.fullmatch(r"[A-Za-z0-9_]{4,}", parts[0]) or parts[0] in ("joinchat", "addstickers"):
            raise ValueError("Could not determine the Telegram channel: a link like t.me/channel_name is required")
        chan = parts[0]
        name = chan
        try:
            m = re.search(r'<meta property="og:title" content="([^"]+)"', http_text(f"https://t.me/s/{chan}", timeout=15))
            if m:
                name = html.unescape(m.group(1)).strip()
        except (urllib.error.URLError, OSError):
            pass
        return dict(id=f"tg-{_slug(chan)}", name=name, type="telegram", url=f"https://t.me/{chan}",
                    config=dict(channel=chan))

    if h in ("facebook.com", "fb.com", "fb.me", "instagram.com"):
        raise ValueError("Facebook and Instagram do not serve public posts without logging in and forbid automated reading, "
                         "so they cannot be added. Look for the same community on VK or Telegram, or the site's RSS feed.")

    if h in ("ok.ru", "odnoklassniki.ru"):
        if parts[:1] == ["group"] and len(parts) > 1:
            dom = f"group/{parts[1]}"
        elif parts and parts[0] not in ("feed", "profile", "video", "music", "games", "dk", "apps", "search", "settings", "messages"):
            dom = parts[0]
        else:
            raise ValueError("A link to a public Odnoklassniki group is required, e.g. ok.ru/kinopoisk")
        name = dom
        try:
            m = re.search(r'<meta property="og:title" content="([^"]+)"', http_text(f"https://ok.ru/{dom}", timeout=15))
            if m:
                name = html.unescape(m.group(1)).split("|")[0].strip() or dom
        except (urllib.error.URLError, OSError):
            pass
        return dict(id=f"ok-{_slug(dom)}", name=name, type="ok", url=f"https://ok.ru/{dom}", config=dict(domain=dom))

    if h in ("vk.com", "vk.ru", "vkontakte.ru"):
        if not parts or parts[0] in ("feed", "im", "away.php", "video", "music", "al_feed.php"):
            raise ValueError("A link to a community or user page is required, e.g. vk.ru/name")
        dom = parts[0]
        return dict(id=f"vk-{_slug(dom)}", name=dom, type="vk", url=f"https://m.vk.ru/{dom}", config=dict(domain=dom))

    # regular site / RSS
    try:
        data, charset = http_get(raw, timeout=20)
    except urllib.error.HTTPError as e:
        raise ValueError(f"The site returned error {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise ValueError(f"Could not open the link: {getattr(e, 'reason', e)}")
    sid = f"web-{_slug(h, 16)}-{hashlib.sha1(raw.encode()).hexdigest()[:4]}"
    head = data.lstrip()[:300].lower()
    if head.startswith((b"<?xml", b"<rss", b"<feed")):
        items = parse_feed(data, raw)
        if not items:
            raise ValueError("This is an RSS feed, but it has no news")
        return dict(id=sid, name=feed_title(data) or h, type="rss", url=raw, config=dict(feed=raw))
    page = data.decode(charset, errors="replace")
    meta = _meta_tags(page)
    site = meta.get("og:site_name") or h
    last = urllib.parse.unquote(parts[-1]).replace("-", " ").replace("_", " ").title() if parts else ""
    name = f"{site} · {last}" if last and len(last) < 40 else site
    base = raw.split("#")[0].rstrip("/")
    cands = [base + "/feed/", base + "/rss", base + "/feed", find_feed_url(page, raw), f"{u.scheme}://{u.netloc}/feed/", f"{u.scheme}://{u.netloc}/rss"]
    for cand in filter(None, cands):
        try:
            d2, _ = http_get(cand, timeout=15)
            if d2.lstrip()[:300].lower().startswith((b"<?xml", b"<rss", b"<feed")) and parse_feed(d2, cand):
                return dict(id=sid, name=name, type="rss", url=raw, config=dict(feed=cand))
        except (urllib.error.URLError, ET.ParseError, OSError):
            continue
    if len(auto_extract_links(page, raw)) >= 3:
        return dict(id=sid, name=name, type="site", url=raw, config=dict(auto_links=True, pages=2))
    raise ValueError("Found neither an RSS feed nor a list of articles on this page. Try a link to the news section "
                     "or to the RSS feed (if the site only opens in a browser, browser mode helps: python server.py --install-browser)")


# ---------------------------------------------------------------- archive: backfilling older news

BF_DAYS = int(os.environ.get("BACKFILL_DAYS", "30"))  # how many days back to backfill the archive right away; deeper — on scroll / date pick (0 — the full archive)
BF = {"enabled": os.environ.get("BACKFILL", "1") != "0", "errors": {}, "err_at": {}, "msg": {}, "until": None}


def bf_get(sid):
    with db() as con:
        r = con.execute("SELECT * FROM backfill WHERE source=?", (sid,)).fetchone()
    return dict(r) if r else {"cursor": "{}", "done": 0, "loaded": 0, "message": None}


@retry_readonly
def bf_put(sid, cursor, done, add, message):
    with db() as con:
        con.execute(
            "INSERT INTO backfill(source,cursor,done,loaded,message,updated) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor, done=excluded.done,"
            " loaded=loaded+?, message=excluded.message, updated=excluded.updated",
            (sid, json.dumps(cursor), int(done), add, message, int(time.time()), add))


def bf_min_tg_id(sid, chan=None):
    ids = []
    with db() as con:
        for (u,) in con.execute("SELECT url FROM items WHERE source=?", (sid,)):
            m = re.search(r"t\.me/([^/]+)/(\d+)$", u or "")
            if m and (chan is None or m.group(1).lower() == chan.lower()):
                ids.append(int(m.group(2)))
    return min(ids) if ids else None


def oldest(sid):
    with db() as con:
        return con.execute("SELECT MIN(published) FROM items WHERE source=?", (sid,)).fetchone()[0]


BF_GETTERS = {}  # open backfill browsers (for sites closed to plain requests)


def bf_step(src, cur):
    """One archive batch. -> (items, new cursor, done, message)"""
    t = src["type"]
    if t == "telegram":
        chans = _tg_channels(src)
        done_ch = set(cur.get("done", []))
        todo = [c for c in chans if c not in done_ch]
        if not todo:
            return [], cur, True, None
        chan = todo[0]
        before_map = cur.get("before") if isinstance(cur.get("before"), dict) else ({chans[0]: cur["before"]} if cur.get("before") else {})
        before = before_map.get(chan) or bf_min_tg_id(src["id"], chan)
        if not before:
            return [], cur, False, "waiting for the first posts"
        items, min_id = _tg_parse(src, chan, http_text(f"https://t.me/s/{chan}?before={before}"))
        cur2 = {"before": dict(before_map), "done": sorted(done_ch)}
        if not min_id or min_id >= before or min_id <= 1:
            cur2["done"] = sorted(done_ch | {chan})
        else:
            cur2["before"][chan] = min_id
        return items, cur2, set(cur2["done"]) >= set(chans), None
    if t == "rss":
        n = cur.get("page", 2)
        g = BF_GETTERS.get(src["id"]) or BF_GETTERS.setdefault(src["id"], PageGetter())
        g.t0 = time.time()
        try:
            sig = cur.get("sig")
            if n <= 2 and not sig:  # sample of the first page: to notice that the site ignores ?paged=N
                try:
                    sig = _sig(_feed_items(src, _rss_get(g, src["feed"]), src["feed"]) or [])
                except Exception:
                    sig = None  # the sample is only a safeguard; without it just carry on
            try:
                data = _rss_get(g, _paged(src["feed"], n, "paged"), allow_empty=True)
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):  # pages ran out (or the feed does not support paging)
                    g.close(); BF_GETTERS.pop(src["id"], None)
                    return [], cur, True, None if n > 2 else "the feed does not support a paged archive"
                raise
            items = _feed_items(src, data, src["feed"])
            if items is None:  # empty page: the archive is finished
                g.close(); BF_GETTERS.pop(src["id"], None)
                return [], cur, True, None if n > 2 else "the feed does not support a paged archive"
            if items and _sig(items) == sig:  # the site returned the same page: paging unsupported / end
                g.close(); BF_GETTERS.pop(src["id"], None)
                return [], cur, True, None if n > 2 else "the feed does not support a paged archive"
        except BaseException:
            g.close(); BF_GETTERS.pop(src["id"], None)
            raise
        if not g.browser_mode:  # keep a browser only for sites that do not respond without one
            BF_GETTERS.pop(src["id"], None)
        return items, {"page": n + 1, "sig": _sig(items)}, not items, None
    if t == "vk" and VK_TOKEN:
        off = cur.get("offset", 0)
        q = urllib.parse.urlencode(dict(domain=src["domain"], count=100, offset=off, access_token=VK_TOKEN, v="5.199"))
        data, _ = http_get(f"https://{VK_API_HOST}/method/wall.get?{q}")
        resp = json.loads(data.decode("utf-8"))
        items = parse_vk(resp, src["domain"])
        raw = len(resp.get("response", {}).get("items", []))
        total = resp.get("response", {}).get("count", 0)
        return items, {"offset": off + raw}, raw == 0 or off + raw >= total, None
    if t == "vk":  # no token: mobile version of the wall, going deeper from what is already loaded
        off = cur.get("offset")
        if off is None:
            off = max(0, len(known_ids(src["id"])) - 10)
        g = PageGetter(ua=VK_UA_OK.get(src["domain"]) or MOBILE_UA)
        try:
            items, raw = parse_vk_mobile(g.text(_vk_mobile_url(src["domain"], off)), src["domain"])
            if not raw and not off:
                g.enable_browser()
                items, raw = parse_vk_mobile(g.text(_vk_mobile_url(src["domain"], off)), src["domain"])
        finally:
            g.close()
        return items, {"offset": off + raw}, raw == 0, None
    if t == "site":
        return _bf_site_step(src, cur)
    if t == "ok":
        return _bf_ok_step(src, cur)
    return [], cur, True, None


SITE_SCROLL = {}     # source id -> browser with the section page open (“scroll” mode)
SITEMAP_CACHE = {}   # source id -> (time, [article URLs, newest first])
SITEMAP_BAD = {}     # source id -> URLs that could not be read


def _site_article_filter(src, urls):
    base = src["url"]
    if src.get("link_re"):
        return extract_links("\n".join(urls), base, src["link_re"])
    fake = "".join(f'<a href="{html.escape(u)}">x</a>' for u in urls)
    return auto_extract_links(fake, base)


def _sitemap_urls(src):
    """Article URLs from sitemap.xml (and nested maps), newest first. [] — no map or it has no articles."""
    hit = SITEMAP_CACHE.get(src["id"])
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    p = urllib.parse.urlparse(src["url"])
    root = f"{p.scheme}://{p.netloc}"
    queue = []
    try:
        queue += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", http_text(root + "/robots.txt", timeout=15, max_bytes=100000))
    except Exception:
        pass
    queue += [root + x for x in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-news.xml", "/news-sitemap.xml")]
    seen, entries, files = set(), {}, 0
    while queue and files < 40:
        u = queue.pop(0)
        if u in seen or u.endswith(".gz"):
            continue
        seen.add(u)
        try:
            txt = http_get(u, timeout=25)[0].decode("utf-8", "replace")
        except Exception:
            continue
        files += 1
        for blk in re.findall(r"<sitemap\b[^>]*>(.*?)</sitemap>", txt, re.S | re.I):
            m = re.search(r"<loc>\s*(.*?)\s*</loc>", blk, re.S | re.I)
            if m:
                queue.append(html.unescape(m.group(1)))
        for blk in re.findall(r"<url\b[^>]*>(.*?)</url>", txt, re.S | re.I):
            m = re.search(r"<loc>\s*(.*?)\s*</loc>", blk, re.S | re.I)
            if not m:
                continue
            lm = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", blk, re.S | re.I)
            entries[html.unescape(m.group(1))] = (lm.group(1) if lm else "")
    keep = _site_article_filter(src, list(entries))

    def key(u):
        d = re.search(r"\d{4}-\d{2}-\d{2}", entries.get(u, "")) or re.search(r"/(\d{4}-\d{2}-\d{2})/", u)
        return d.group(0) if d else ""
    urls = sorted(keep, key=key, reverse=True)
    if urls:  # do not cache an empty result: the map may have been temporarily unavailable
        SITEMAP_CACHE[src["id"]] = (time.time(), urls)
    return urls


_DAY_TPLS = ("{base}/{d}", "{base}/{d}/", "{base}?date={d}", "{base}/archive/{d}", "{base}/{y}/{m}/{d}", "{base}?day={d}")


def _day_url(tpl, base, d):
    return tpl.format(base=base, d=d.isoformat(), y=f"{d.year:04d}", m=f"{d.month:02d}")


def _bf_site_days(src, cur):
    """Archive by day: on sites where article URLs contain a date (/news/2026-10-17/…), the section may have day pages.
    Pick a template from the three previous days, then go back one day per step. None — the method does not apply."""
    if not src.get("link_re") or r"\d{4}-\d{2}-\d{2}" not in src["link_re"]:
        return None
    sid, base = src["id"], src["url"].rstrip("/")
    tpl, day, empty = (cur.get("tpl"), cur.get("day"), cur.get("empty", 0)) if cur.get("mode") == "day" else (None, None, 0)
    g = PageGetter()
    try:
        if not tpl:
            probe = [datetime.now(timezone(timedelta(hours=VK_TZ_HOURS))).date() - timedelta(days=k) for k in (1, 2, 3)]
            for t in _DAY_TPLS:
                for d in probe:
                    try:
                        _, found = _site_listing(g, _day_url(t, base, d), False, src)
                    except Exception:
                        continue
                    if any(d.isoformat() in u for u in found):  # the day page really shows articles of that day
                        tpl, day = t, d.isoformat()
                        break
                if tpl:
                    break
            if not tpl:
                return None
        d = datetime.strptime(day, "%Y-%m-%d").date()
        try:
            _, found = _site_listing(g, _day_url(tpl, base, d), False, src)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404):
                raise
            found = []
        known = known_ids(sid)
        fresh = [u for u in found if d.isoformat() in u and make_id(sid, u) not in known]
        items = _read_articles(g, fresh, sid) if fresh else []
        empty = 0 if found else empty + 1
        done = empty >= 60  # two months in a row without news — the archive is over
        return items, {"mode": "day", "tpl": tpl, "day": (d - timedelta(days=1)).isoformat(), "empty": empty}, done, \
            ("day-by-day archive finished" if done else f"reading day-by-day archive ({d.isoformat()})")
    finally:
        g.close()


def _bf_site_step(src, cur):
    """Site archive. Methods in turn: section pages (?page=N etc.) -> sitemap -> scrolling in a browser."""
    sid, mode = src["id"], cur.get("mode", "tpl")
    noscroll = cur.get("noscroll", False)  # paging worked and ended — browser scrolling is not needed
    if mode == "tpl":
        g = PageGetter()
        try:
            tpls, i, n, sig = cur.get("tpls"), cur.get("i", 0), cur.get("page", 2), cur.get("sig")
            if not tpls:  # first pass: find out how paging works on this site
                page, found = _site_listing(g, src["url"], True, src)
                tpls, sig, n, i = _page_templates(src, page), sorted(found), 2, 0
            for _ in range(len(tpls) + 1):
                url = tpls[i].format(n=n)
                try:
                    _, found = _site_listing(g, url, False, src)
                except urllib.error.HTTPError as e:
                    if e.code not in (400, 404):
                        raise
                    found = []
                stuck = not found or set(found) <= set(sig or [])  # empty or same page -> template does not work / end
                if stuck and n == 2 and i + 1 < len(tpls):
                    i += 1  # on the second page try another paging method
                    continue
                if stuck:
                    noscroll = n > 2
                    mode = "sitemap"  # pages ran out or cannot be paged — try the sitemap, days, scrolling
                    break
                known = known_ids(sid)
                fresh = [u for u in found if make_id(sid, u) not in known]
                items = _read_articles(g, fresh, sid) if fresh else []
                return items, {"page": n + 1, "tpls": tpls, "i": i, "sig": sorted(found)}, False, None
        finally:
            g.close()
    if mode == "sitemap":
        urls = _sitemap_urls(src)
        if not urls:
            mode = "day"
        else:
            known, bad = known_ids(sid), SITEMAP_BAD.setdefault(sid, set())
            fresh = [u for u in urls if make_id(sid, u) not in known and u not in bad][:15]
            if not fresh:
                mode = "day"
            if fresh:
                g = PageGetter()
                try:
                    items = _read_articles(g, fresh, sid)
                finally:
                    g.close()
                got = {it["guid"] for it in items}
                bad.update(u for u in fresh if u not in got)
                return items, {"mode": "sitemap", "noscroll": noscroll}, False, "reading archive from the sitemap"
    if mode == "sitemap":
        mode = "day"
    if mode == "day":
        res = _bf_site_days(src, cur)
        if res is not None:
            res[1]["noscroll"] = noscroll
            return res
    if noscroll:
        return [], {"mode": "day", "noscroll": True}, True, None
    # scrolling: the section page opens in a browser, scrolls down and clicks “Показать ещё”
    extract = (lambda h: auto_extract_links(h, src["url"])) if src.get("auto_links") else (lambda h: extract_links(h, src["url"], src["link_re"]))
    g, seen = SITE_SCROLL.get(sid), cur.get("seen", 0)
    try:
        if g is None:
            g = PageGetter(budget=300)
            g.enable_browser(light=True)
            SITE_SCROLL[sid] = g
            g._goto(src["url"])
            g._page.wait_for_timeout(2500)
        g.t0 = time.time()
        n = _scroll_load(g, lambda h: len(extract(h)), seen)
        found = extract(g._content())
    except BaseException:
        if g:
            g.close()
        SITE_SCROLL.pop(sid, None)
        raise
    known, bad = known_ids(sid), SITEMAP_BAD.setdefault(sid, set())
    fresh = [u for u in found if make_id(sid, u) not in known and u not in bad]
    items = []
    if fresh:
        g2 = PageGetter()
        try:
            items = _read_articles(g2, fresh[:15], sid)
        finally:
            g2.close()
        got = {it["guid"] for it in items}
        bad.update(u for u in fresh[:15] if u not in got)
    limit = n >= SCROLL_MAX_ITEMS or getattr(g, "dom_size", 0) > SCROLL_MAX_HTML
    done = (n <= seen or limit) and len(fresh) <= 15
    if done or limit:
        g.close(); SITE_SCROLL.pop(sid, None)
    return items, {"mode": "scroll", "seen": max(n, seen)}, done, \
        (f"scroll limit reached ({n} links)" if limit and done else "section feed finished" if done else "scrolling the section in a browser")


OK_SCROLL = {}  # source id -> (PageGetter with the group feed open)


_MORE_JS = """() => {
  window.scrollTo(0, document.body.scrollHeight);
  const e = document.scrollingElement; if (e) e.scrollTop = e.scrollHeight;
  const re = /^\\s*(показать\\s+ещ[её]|загрузить\\s+ещ[её]|ещ[её]\\s+новости|больше\\s+новостей|показать\\s+больше|load\\s+more|show\\s+more|more)\\s*$/i;
  for (const b of document.querySelectorAll('button, a, div[role=button], span[role=button]')) {
    const t = (b.innerText || '').trim();
    if (t.length < 40 && re.test(t) && b.offsetParent !== null && !(b.tagName === 'A' && /\\/topic\\//.test(b.getAttribute('href') || ''))) {
      try { b.click(); return true; } catch (x) {}
    }
  }
  return false;
}"""


SCROLL_MAX_HTML = 6_000_000   # characters: a longer page exhausts browser and Python memory
SCROLL_MAX_ITEMS = 600        # posts/links per scroll session


def _scroll_load(g, count, want, max_rounds=None):
    """Scrolls the page down (and clicks “Показать ещё”) until count(html) exceeds want or growth stops.
    The page must not grow without bound: scrolling stops when the limit is exceeded."""
    page = g._page
    last, stall = 0, 0
    if max_rounds is None:  # after a browser restart, scroll again down to what was already loaded
        max_rounds = min(80, 12 + want // 8)
    for _ in range(max_rounds):
        html_ = g._content()
        n = count(html_)
        g.dom_size = len(html_)
        del html_
        if n > want or g.dom_size > SCROLL_MAX_HTML or n >= SCROLL_MAX_ITEMS:
            return n
        stall = stall + 1 if n <= last else 0
        if stall >= 3:
            return n
        last = n
        try:
            page.evaluate(_MORE_JS)
            page.keyboard.press("End")
        except Exception:
            pass
        page.wait_for_timeout(1600)
    return last


def _ok_scroll(g, want, max_rounds=None):
    return _scroll_load(g, lambda h: len(set(m.group(2) for m in _OK_TOPIC.finditer(h))), want, max_rounds)


def _bf_ok_step(src, cur):
    """OK group archive: open the feed in a browser and scroll down — the page loads older posts."""
    sid, dom = src["id"], src["domain"]
    g = OK_SCROLL.get(sid)
    seen = cur.get("seen", 0)
    try:
        if g is None:
            g = PageGetter(budget=300)
            g.enable_browser(light=True)
            OK_SCROLL[sid] = g
            g._goto(src["url"])
            g._page.wait_for_timeout(2500)
        g.t0 = time.time()
        n = _ok_scroll(g, seen)
        page = g._content()
    except BaseException:
        g.close() if g else None
        OK_SCROLL.pop(sid, None)
        raise
    pinned, stats = [], {}
    items, _ = parse_ok(page, dom, pinned_out=pinned, stats=stats)
    known = known_ids(sid)
    items = [it for it in items if make_id(sid, it["guid"]) not in known]
    if items:
        g2 = PageGetter()  # post pages via a separate plain request so the browser does not leave the feed
        try:
            ok_post_times(g2, src, items, limit=30)
        except Exception:
            pass
        finally:
            g2.close()
    try:  # for previously saved posts without text — read the rest from the post page
        g3 = PageGetter()
        try:
            ok_repair_texts(g3, src, limit=10)
        finally:
            g3.close()
    except Exception:
        pass
    limit = n >= SCROLL_MAX_ITEMS or getattr(g, "dom_size", 0) > SCROLL_MAX_HTML
    done = n <= seen or limit
    if done or limit:
        g.close(); OK_SCROLL.pop(sid, None)
    return items, {"seen": max(n, seen), "stall": 0}, done, \
        (f"scroll limit reached ({n} posts): the browser lacks memory for a longer feed" if limit
         else "group feed finished" if done else None)


def _sig(items):
    return sorted(str(it.get("guid")) for it in items)


def _page_templates(src, page=None):
    """Ways to page a site section: first what is visible in the page's own pagination links, then the typical ones."""
    base = src["url"].split("#")[0]
    host, path = _host(base), urllib.parse.urlparse(base).path.rstrip("/")
    cands = []
    for m in re.finditer(r'href=["\']([^"\'#]+)', page or "", re.I):
        h = urllib.parse.urljoin(base, html.unescape(m.group(1)))
        p = urllib.parse.urlparse(h)
        if _host(h) != host or not p.path.startswith(path or "/"):
            continue
        q = re.sub(r"([?&](?:page|PAGEN_\d+|PAGE|p|pg|paged|pagenumber)=)\d+", r"\1{n}", h)
        if q == h:
            q = re.sub(r"(/page[/-]?)\d+", r"\1{n}", h)
        if q != h and "{n}" in q:
            cands.append(q.replace("{n}", "\0").replace("{", "{{").replace("}", "}}").replace("\0", "{n}"))
    root = base.rstrip("/")
    sep = "&" if "?" in root else "?"
    for d in (f"{root}{sep}page={{n}}", f"{root}{sep}PAGEN_1={{n}}", f"{root}/page/{{n}}/", f"{root}{sep}p={{n}}"):
        cands.append(d)
    out = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


def bf_available(src):
    if src["type"] == "ok":
        try:
            import importlib.util
            if importlib.util.find_spec("playwright"):
                return True, None
        except Exception:
            pass
        return False, "the OK group archive needs browser mode: python server.py --install-browser"
    if src["type"] not in ("telegram", "rss", "vk", "site"):
        return False, "archive is not available for this source"
    return True, None


def _is_net_down(e):
    """Temporary network failure (DNS getaddrinfo, drop, timeout), but not a site refusal (HTTP error) and not SSL."""
    if isinstance(e, urllib.error.HTTPError) or is_ssl_error(e):
        return False
    r = getattr(e, "reason", e)
    if isinstance(e, (socket.gaierror, socket.timeout, TimeoutError, ConnectionError)) or isinstance(r, (socket.gaierror, socket.timeout, ConnectionError)):
        return True
    return bool(re.search(r"getaddrinfo|11001|Name or service|Temporary failure|timed out|Network is unreachable|10060|10061", str(e), re.I))


def backfill_pass():
    """One pass over the sources (one batch per source). True if anything was done."""
    worked = False
    until = BF["until"]
    if until is None and BF_DAYS > 0:  # lazy mode: preload only the last BF_DAYS days, the rest when needed
        until = int(time.time()) - BF_DAYS * 86400
    for src in get_sources():
        if not BF["enabled"] or STATE["refreshing"]:
            break
        ok, _ = bf_available(src)
        row = bf_get(src["id"])
        if BF["errors"].get(src["id"], 0) >= 5 and time.time() - BF["err_at"].get(src["id"], 0) > 600:
            BF["errors"][src["id"]] = 2  # retry in 10 minutes (the site may have stopped blocking)
        if not ok or row["done"] or BF["errors"].get(src["id"], 0) >= 5:
            continue
        if until is not None:  # urgent mode: only sources that have not yet reached the requested date
            o = oldest(src["id"])
            if o is not None and o <= until:
                continue
        worked = True
        _close_persistent(keep=src["id"])  # the other source's browser is no longer needed — do not hold space and memory
        try:
            cur = json.loads(row["cursor"] or "{}")
            items, cur2, done, msg = bf_step(src, cur)
            new = save_items(src["id"], items) if items else 0
            if new and src["type"] in ("rss", "site"):  # old articles need images too (e.g. Towards Data Science)
                try:
                    enrich_images(limit=20, source_id=src["id"])
                except Exception as e:
                    print(f"[{src['id']}] archive images: {type(e).__name__}: {e}", file=sys.stderr)
            bf_put(src["id"], cur2, done, new, msg or ("fully loaded" if done else None))
            BF["errors"][src["id"]] = 0
        except BrowserBusy as e:  # not a source error: a browser slot will free up
            BF["msg"][src["id"]] = f"waiting for a free browser: {e}"[:200]
            time.sleep(5)
        except Exception as e:
            if _is_net_down(e):  # no connection/DNS — temporary, not the source's fault: quietly retry later, do not pile up errors
                BF["msg"][src["id"]] = "no connection to the site, will retry later"
                time.sleep(20)
                continue
            BF["errors"][src["id"]] = BF["errors"].get(src["id"], 0) + 1
            BF["err_at"][src["id"]] = time.time()
            text = str(e) or ("out of memory" if isinstance(e, MemoryError) else "")
            if re.search(r"crash|Target.*closed|Connection closed|MemoryError|out of memory", f"{type(e).__name__} {text}", re.I):
                text = f"browser crashed (out of memory?): {text[:100]}"
                _close_persistent()  # next time — a clean browser
                cleanup_browser_temp(max_age=300)
            BF["msg"][src["id"]] = f"{type(e).__name__}: {text}"[:200]
            print(f"[{src['id']}] archive, error: {BF['msg'][src['id']]}", file=sys.stderr)
            time.sleep(5)
        time.sleep(0.4 if BF["until"] is not None else BACKFILL_DELAY)
    return worked


def _close_persistent(keep=None):
    """Close the browsers that backfill keeps open between steps (frees memory and browser slots).
    keep — id of the source whose browser to keep: only one is held open at a time."""
    for pool in (BF_GETTERS, OK_SCROLL, SITE_SCROLL):
        for k, g in list(pool.items()):
            if keep is not None and k == keep:
                continue
            try:
                g.close()
            except Exception:
                pass
            pool.pop(k, None)


def backfill_loop():
    _PW_TLS.nowait = True  # background backfill yields the browser to refresh
    time.sleep(45)  # in the first seconds the page and refresh are working — backfill does not get in their way
    while True:
        if STATE["refreshing"] or not BF["enabled"]:
            _close_persistent()  # refresh needs the browser (and memory), backfill can wait
        if BF["enabled"] and not STATE["refreshing"]:
            if backfill_pass():
                continue
            if BF["until"] is not None:  # requested date reached (or nothing more to load)
                BF["until"] = None
                continue
            if heal_pass():
                continue
        _close_persistent()
        time.sleep(5 if not BF["enabled"] else 30)


def auto_refresh_loop():
    while True:
        time.sleep(max(1, REFRESH_MINUTES) * 60)
        refresh_all()
        try:
            cleanup_browser_temp()
            trim_image_cache()
        except Exception:
            pass


# ---------------------------------------------------------------- feed queries


def search_clauses(q):
    clauses, params = [], []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', q or ""):
        t = norm_search(phrase or word).strip()
        if t:
            clauses.append("i.search LIKE ? ESCAPE '\\'")
            params.append("%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return clauses, params


def day_bounds(date_str, tz_offset_min):
    """'YYYY-MM-DD' in the client's time zone -> [start, start of next day) in unix time."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = int(d.timestamp()) + tz_offset_min * 60
    return start, start + 86400


def query_items(q="", date_from="", date_to="", sources=(), bookmarked=False, limit=30, offset=0, tz=0):
    where, params = search_clauses(q)
    try:
        if date_from:
            where.append("i.published >= ?")
            params.append(day_bounds(date_from, tz)[0])
        if date_to:
            where.append("i.published < ?")
            params.append(day_bounds(date_to, tz)[1])
    except ValueError:
        raise ValueError("date must be in YYYY-MM-DD format")
    sources = [s for s in sources if s]
    if sources:
        where.append(f"i.source IN ({','.join('?' * len(sources))})")
        params += sources
    if bookmarked:
        where.append("b.item_id IS NOT NULL")
    else:  # removed sources are not shown in the combined feed
        where.append("s.active=1")
    w = " WHERE " + " AND ".join(where)
    frm = "FROM items i LEFT JOIN bookmarks b ON b.item_id=i.id LEFT JOIN sources s ON s.id=i.source" + w
    with db() as con:
        total = con.execute("SELECT COUNT(*) " + frm, params).fetchone()[0]
        rows = con.execute(
            "SELECT i.id,i.source,i.title,i.text,i.url,i.ext_url,i.image,i.media,i.links,i.published,"
            "COALESCE(s.name,i.source) AS source_name, COALESCE(s.color,'#888888') AS source_color, s.type AS stype,"
            "(b.item_id IS NOT NULL) AS bookmarked " + frm +
            " ORDER BY i.published DESC, i.id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    chan_names = {}
    for sv in get_sources(active_only=False):
        if sv["type"] == "telegram" and sv.get("channel_names"):
            chan_names[sv["id"]] = {c.lower(): n for c, n in sv["channel_names"].items()}
    items = []
    for r in rows:
        d = dict(r)
        if d["source"] in chan_names:  # several channels in one source: label each one separately in the feed
            m = re.search(r"t\.me/([^/]+)/\d+$", d["url"] or "")
            if m and m.group(1).lower() in chan_names[d["source"]]:
                d["source_name"] = chan_names[d["source"]][m.group(1).lower()]
        d["bookmarked"] = bool(d["bookmarked"])
        d["image"] = d["image"] or None
        try:
            media = json.loads(d.pop("media") or "null")
        except ValueError:
            media = None
        d["media"] = media or ([{"u": d["image"], "t": "image"}] if d["image"] else [])
        d.pop("stype", None)
        try:
            d["links"] = json.loads(d.get("links") or "[]")
        except ValueError:
            d["links"] = []
        items.append(d)
    return {"total": total, "items": items}


def sources_info():
    with db() as con:
        agg = {r[0]: r[1:] for r in con.execute(
            "SELECT source, COUNT(*), SUM(image IS NOT NULL AND image<>''), MIN(published), MAX(published) FROM items GROUP BY source")}
        counts = {k: v[0] for k, v in agg.items()}
        pics = {k: v[1] or 0 for k, v in agg.items()}
        olds = {k: v[2] for k, v in agg.items()}
        news = {k: v[3] for k, v in agg.items()}
        bfs = {r["source"]: dict(r) for r in con.execute("SELECT * FROM backfill")}
        status = {r["source"]: dict(r) for r in con.execute("SELECT * FROM source_status")}
        marks = con.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0]
    out = []
    for s in get_sources():
        st = status.get(s["id"], {})
        b = bfs.get(s["id"], {})
        err = BF["errors"].get(s["id"], 0) >= 5
        can, why = bf_available(s)
        arch = dict(supported=can, done=bool(b.get("done")), loaded=b.get("loaded", 0), error=err,
                    message=(why if not can else BF["msg"].get(s["id"]) if err else b.get("message")))
        o = olds.get(s["id"])
        if can and not err and not arch["done"] and BF_DAYS > 0 and BF["until"] is None and o is not None and o <= int(time.time()) - BF_DAYS * 86400:
            arch.update(lazy=True, message=f"loaded for the last {BF_DAYS} days; deeper — as you scroll down")
        out.append(dict(id=s["id"], name=s["name"], url=s["url"], type=s["type"], color=s["color"], links=source_links(s),
                        count=counts.get(s["id"], 0), with_image=pics.get(s["id"], 0), oldest=olds.get(s["id"]), newest=news.get(s["id"]),
                        archive=arch, ok=None if not st else bool(st["ok"]), message=st.get("message"),
                        checked=st.get("checked"), busy=s["id"] in STATE["busy"]))
    return dict(sources=out, bookmarks=marks, refreshing=STATE["refreshing"], last_refresh=STATE["last_refresh"],
                backfill=BF["enabled"])


@retry_readonly
def set_bookmark(item_id, on):
    with db() as con:
        if on:
            if not con.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
                return False
            con.execute("INSERT OR IGNORE INTO bookmarks(item_id,created) VALUES(?,?)", (item_id, int(time.time())))
        else:
            con.execute("DELETE FROM bookmarks WHERE item_id=?", (item_id,))
    return True


# ---------------------------------------------------------------- image proxy
# The browser requests images from our server, and the server downloads them with the “right” Referer and caches them on disk:
# so hotlink protection and CDN referrer blocking do not interfere.


def is_public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlparse(newurl)
        if u.scheme not in ("http", "https") or not is_public_host(u.hostname or ""):
            raise urllib.error.URLError("redirect to a disallowed address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


IMG_CACHE_MB = int(os.environ.get("IMG_CACHE_MB", "300"))
_IMG_WRITES = [0]


def trim_image_cache(max_mb=None):
    """The image cache does not grow forever: over the limit the oldest files are deleted (down to 80% of the limit)."""
    limit = (max_mb if max_mb is not None else IMG_CACHE_MB) * 1024 * 1024
    try:
        files = []
        for n in os.listdir(IMG_DIR):
            p = os.path.join(IMG_DIR, n)
            if os.path.isfile(p):
                st = os.stat(p)
                files.append((st.st_mtime, st.st_size, p))
    except OSError:
        return 0
    total = sum(f[1] for f in files)
    if total <= limit:
        return 0
    removed = 0
    for _, size, p in sorted(files):
        if total <= limit * 0.8:
            break
        try:
            os.remove(p)
            total -= size
            removed += size
        except OSError:
            pass
    return removed


def cleanup_browser_temp(max_age=3600):
    """On a crash Chromium does not delete its temporary profiles (hundreds of MB each) in the TEMP folder.
    We delete old (older than max_age seconds) Playwright directories; live browsers' profiles are fresh and are left alone."""
    if _BR_HELD:
        return 0
    n, now = 0, time.time()
    tmp = tempfile.gettempdir()
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0
    for name in names:
        if not re.match(r"(?:playwright_chromiumdev_profile-|playwright-artifacts-|playwright_chromium_profile-)", name):
            continue
        p = os.path.join(tmp, name)
        try:
            if now - os.path.getmtime(p) > max_age:
                shutil.rmtree(p, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


def fetch_image(url):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or not is_public_host(u.hostname):
        raise ValueError("invalid image address")
    key = hashlib.sha1(url.encode()).hexdigest()
    path = os.path.join(IMG_DIR, key)
    if os.path.isfile(path) and os.path.isfile(path + ".type"):
        with open(path, "rb") as f, open(path + ".type") as t:
            return f.read(), t.read().strip()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        "Referer": f"{u.scheme}://{u.netloc}/"})
    with urllib.request.build_opener(_SafeRedirect, urllib.request.HTTPSHandler(context=SSL_CTX)).open(req, timeout=20) as r:
        data = r.read(8 * 1024 * 1024 + 1)
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("image is too large")
    if not ctype.startswith("image/"):
        raise ValueError(f"this address is not an image ({ctype or 'no type given'})")
    os.makedirs(IMG_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    with open(path + ".type", "w") as f:
        f.write(ctype)
    _IMG_WRITES[0] += 1
    if _IMG_WRITES[0] % 40 == 0:
        trim_image_cache()
    return data, ctype


# ---------------------------------------------------------------- HTTP server

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "NewsFeed/2.0"

    def log_message(self, fmt, *args):
        if os.environ.get("NEWS_DEBUG"):
            super().log_message(fmt, *args)

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(min(n, 100000)) or b"{}")
        except ValueError:
            return {}

    def _static(self, rel):
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
            return self._send(404, b"not found", "text/plain")
        with open(path, "rb") as f:
            self._send(200, f.read(), MIME.get(os.path.splitext(path)[1], "application/octet-stream"))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        t0 = time.time()
        try:
            self._do_get()
        finally:
            if time.time() - t0 > 1.5:  # slow requests are visible in the console
                print(f"[slow] {self.path[:80]} — {time.time() - t0:.1f} s", file=sys.stderr)

    def _do_get(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        one = lambda k, d="": qs.get(k, [d])[0]
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path == "/favicon.ico":
                return self._send(204)
            if u.path == "/api/items":
                res = query_items(
                    q=one("q"), date_from=one("from"), date_to=one("to"),
                    sources=[s for s in one("sources").split(",") if s],
                    bookmarked=one("bookmarked") == "1",
                    limit=max(1, min(100, int(one("limit", "30")))), offset=max(0, int(one("offset", "0"))),
                    tz=int(one("tz", "0")))
                return self._json(res)
            if u.path == "/api/sources":
                return self._json(sources_info())
            if u.path == "/img":
                try:
                    data, ctype = fetch_image(one("u"))
                except (ValueError, urllib.error.URLError, OSError) as e:
                    return self._send(502, f"{type(e).__name__}: {e}".encode("utf-8"), "text/plain; charset=utf-8")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
                self.end_headers()
                return self.wfile.write(data)
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            self._send(404, b"not found", "text/plain")
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/refresh":
                if not STATE["refreshing"]:
                    threading.Thread(target=refresh_all, kwargs={"force": qs.get("force", ["0"])[0] == "1"}, daemon=True).start()
                    time.sleep(0.05)
                return self._json({"started": True})
            if u.path == "/api/sources":
                body = self._body()
                try:
                    spec = build_spec(body.get("name"), body.get("links") or [{"u": body.get("url", "")}])
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                src, is_new = add_source(spec)
                refresh_source(src)  # fetch fresh news right away to show the result
                info = next((s for s in sources_info()["sources"] if s["id"] == src["id"]), None)
                return self._json({"source": info, "added": is_new})
            if u.path == "/api/backfill":
                if "on" in qs:
                    BF["enabled"] = qs["on"][0] == "1"
                    if BF["enabled"]:
                        BF["errors"].clear()
                if qs.get("until", [""])[0]:  # urgently load the archive up to this date (inclusive)
                    new_until = day_bounds(qs["until"][0], int(qs.get("tz", ["0"])[0]))[0]
                    if new_until != BF["until"]:
                        BF["errors"].clear()
                    BF["until"] = new_until
                    BF["enabled"] = True
                return self._json({"enabled": BF["enabled"], "until": BF["until"]})
            if u.path.startswith("/api/bookmarks/"):
                ok = set_bookmark(urllib.parse.unquote(u.path.rsplit("/", 1)[-1]), True)
                return self._json({"ok": ok}, 200 if ok else 404)
            self._send(404, b"not found", "text/plain")
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def do_PUT(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path.startswith("/api/sources/"):
                sid = urllib.parse.unquote(u.path.rsplit("/", 1)[-1])
                body = self._body()
                try:
                    src = edit_source(sid, body.get("name"), body.get("links"))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                if not src:
                    return self._json({"error": "source not found"}, 404)
                refresh_source(src)
                info = next((s for s in sources_info()["sources"] if s["id"] == sid), None)
                return self._json({"source": info})
            self._send(404, b"not found", "text/plain")
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        tail = urllib.parse.unquote(u.path.rsplit("/", 1)[-1])
        if u.path.startswith("/api/bookmarks/"):
            set_bookmark(tail, False)
            return self._json({"ok": True})
        if u.path.startswith("/api/sources/"):
            ok = remove_source(tail)
            return self._json({"ok": ok}, 200 if ok else 404)
        self._send(404, b"not found", "text/plain")


def make_server(host=HOST, port=PORT):
    return ThreadingHTTPServer((host, port), Handler)


def _housekeeping():
    try:
        t0 = time.time()
        n = cleanup_browser_temp(max_age=600)  # leftovers of earlier crashed browser runs
        trim_image_cache()
        if n:
            print(f"Temporary browser profiles removed: {n} ({time.time() - t0:.0f} s)")
    except Exception:
        pass


def main(argv):
    if "--install-browser" in argv:
        return install_browser()
    if "--diagnose" in argv:
        return diagnose()
    t0 = time.time()
    init_db()
    print(f"Database ready in {time.time() - t0:.1f} s: {DB_PATH}")
    if "--refresh-once" in argv or "--backfill" in argv:
        _housekeeping()
    if "--refresh-once" in argv:
        refresh_all(force=True)
        for s in sources_info()["sources"]:
            print(f"{'OK ' if s['ok'] else 'ERR'} {s['name']:<28} {s['message']}")
        return
    if "--backfill" in argv:  # load the archive and exit
        refresh_all(force=True)
        BF["enabled"] = True
        while backfill_pass():
            pass
        for s in sources_info()["sources"]:
            a = s["archive"]
            print(f"{s['name']:<28} {s['count']:>6} items  archive: {a['message'] or ('done' if a['done'] else 'in progress')}")
        return
    try:  # port first: if it is taken, another copy is already running — two copies fight over the database (“readonly”)
        srv = make_server()
    except OSError as e:
        print(f"Could not bind port {PORT} ({e}). The app is probably already running (another window, PyCharm, a background process). "
              "Close the other copy (Task Manager → python.exe) or set PORT=8001.", file=sys.stderr)
        return
    threading.Thread(target=_housekeeping, daemon=True).start()  # TEMP/cache cleanup — after the server starts, not before (it used to take minutes)
    threading.Thread(target=refresh_all, daemon=True).start()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    threading.Thread(target=backfill_loop, daemon=True).start()
    threading.Thread(target=enrich_loop, daemon=True).start()
    print(f"News feed: http://{HOST}:{PORT}  (refreshes every {REFRESH_MINUTES} min, Ctrl+C to quit)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main(sys.argv[1:])
