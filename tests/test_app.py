import http.server
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402


SITE_SRC = dict(id="examplesite", name="Example News", type="site", url="https://news.example.org/news",
                config=dict(try_feeds=True, pages=2,
                            link_re=r'((?:https?://(?:www\.)?news\.example\.org)?/news/\d{4}-\d{2}-\d{2}/[^"\'<>\s#?\\]+)'))


def fresh_db():
    server.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
    server.init_db()
    # Hi-News is read from Telegram in the app; most RSS/archive tests use it as a convenient RSS source
    with server.db() as con:
        con.execute("UPDATE sources SET type='rss', url='https://hi-news.ru', config=? WHERE id='hinews'",
                    (json.dumps({"feed": "https://hi-news.ru/feed", "strip_code": True}),))
    # generic sources for tests that need a plain Telegram channel / a plain site (not part of the defaults)
    server.add_source(dict(id="tgtest", name="TG Test", type="telegram", url="https://t.me/newschan", config=dict(channel="newschan")))
    server.add_source(SITE_SRC)


HINEWS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Hi-News</title>
<item>
<title>Почему перед сном вдруг вспоминается всё, за что нам стыдно</title>
<link>https://hi-news.ru/eto-interesno/pochemu.html</link>
<pubDate>Thu, 15 Oct 2026 18:00:07 +0000</pubDate>
<guid isPermaLink="false">https://hi-news.ru/?p=490912</guid>
<description><![CDATA[Можно забыть важные события, но перед сном вдруг вспомнить случай, за который стыдно.]]></description>
<content:encoded><![CDATA[<div class="wp-caption"><img src="https://hi-news.ru/wp-content/uploads/2026/10/a.jpg" alt="x" /><p class="wp-caption-text">Подпись под фото</p></div>
<p>Вы уже почти уснули — и вдруг вспомнили <a href="https://x.ru">случай</a> из школы.</p>
<h2>Стыд оставляет след</h2><p>Текст раздела.</p>
<p><code>А ты уже подписан на <a href="https://max.ru/hinews_ru">наш канал в MAX</a>?</code></p>
<ul><li>Первый пункт</li><li>Второй пункт</li></ul>]]></content:encoded>
</item></channel></rss>"""

KDN = """<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>KDnuggets</title><item>
<title>7 Open-Source AI Tools That Can Replace Paid Subscriptions</title>
<description><![CDATA[Find open-source alternatives to OpenAI API and more.]]></description>
<link>https://www.kdnuggets.com/7-open-source-ai-tools</link>
<guid isPermaLink="false">https://www.kdnuggets.com/?p=210437</guid>
<pubDate>Thu, 15 Oct 2026 12:00:34 +0000</pubDate>
<media:content url="https://www.kdnuggets.com/wp-content/uploads/awan.png" medium="image" />
</item></channel></rss>"""

TDS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>TDS</title><item>
<title><![CDATA[Does Stacking Beat Your Best Model?]]></title>
<description><![CDATA[<p>It only helps when your models make different mistakes.</p>
<p>The post <a href="https://towardsdatascience.com/does-stacking-beat-your-best-model/">Does Stacking Beat Your Best Model?</a> appeared first on <a href="https://towardsdatascience.com">Towards Data Science</a>.</p>]]></description>
<link>https://towardsdatascience.com/does-stacking-beat-your-best-model/</link>
<pubDate>Wed, 14 Oct 2026 15:30:00 GMT</pubDate></item></channel></rss>"""

# a feed cut off in the middle (as some sites return it)
TRUNCATED = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Example News</title>
<item><title>Worst smartphones of 2026 named</title><link>https://news.example.org/worst-smartphones-2026/</link>
<pubDate>Fri, 16 Oct 2026 10:00:00 +0300</pubDate><description>The site named 5 smartphones you should not buy.</description></item>
<item><title>Zero announced</title><link>https://news.example.org/ctrl-energy-zero-announced/</link><pubDate>Fri, 16 Oct 2026 09:00:00 +0300</pubDate><description>An eternal powerba"""

TG = """<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="newschan/94578" data-view="x">
<div class="tgme_widget_message_text js-message_text" dir="auto"><b>Hungary demanded that the EU compensate losses</b><br/><br/>The prime minister said the budgets are not ready.<br/><br/><a href="https://t.me/newschan_bot">Donate via the bot</a> | <a href="https://www.patreon.com/newschan">Patreon</a></div>
<a class="tgme_widget_message_photo_wrap 5 js-message_photo" href="https://t.me/newschan/94578" style="width:800px;background-image:url('https://cdn1.telesco.pe/file/photo1.jpg')"></a>
<a class="tgme_widget_message_link_preview" href="https://news.example.org/news/2026-09-10/x" target="_blank"><i class="link_preview_image" style="background-image:url('https://cdn1.telesco.pe/file/prev.jpg')"></i></a>
<div class="tgme_widget_message_info"><a class="tgme_widget_message_date" href="https://t.me/newschan/94578"><time datetime="2026-09-10T13:24:00+00:00" class="time">16:24</time></a></div></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="newschan/94577">
<div class="tgme_widget_message_text js-message_text" dir="auto">Text only, no picture</div>
<time datetime="2026-09-10T12:00:00+00:00" class="time">15:00</time></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="newschan/94576"><time datetime="2026-09-10T11:00:00+00:00"></time></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="other/5"><div class="tgme_widget_message_text js-message_text">another channel</div></div></div>"""

VK = {"response": {"count": 3, "items": [
    {"id": 101, "owner_id": -1234, "date": 1760000000, "text": "Новый трейлер [club99|Кинопоиска]\nПодробности внутри",
     "attachments": [{"type": "photo", "photo": {"sizes": [
         {"type": "s", "url": "https://vk/s.jpg", "width": 75, "height": 50},
         {"type": "x", "url": "https://vk/x.jpg", "width": 604, "height": 400}]}},
         {"type": "link", "link": {"url": "https://kinopoisk.ru/film/1"}}]},
    {"id": 102, "owner_id": -1234, "date": 1760000100, "text": "Реклама", "marked_as_ads": 1},
    {"id": 103, "owner_id": -1234, "date": 1760000200, "text": "", "attachments": []}]}}

# "now" for the VK date tests: 17.10.2026 12:00 Moscow time
NOW = int(time.mktime(time.strptime("2026-10-17 09:00:00", "%Y-%m-%d %H:%M:%S"))) - time.timezone
VK_MOBILE = """<div class="wall_module"><div class="wall_item"><div class="wi_head"><img class="wi_img" src="https://sun/avatar.jpg">
<a class="wi_date" href="/wall-1234_555">вчера в 21:05</a></div>
<div class="pi_text">Вышел трейлер сериала<br>Смотрим вместе <span class="pi_text_more">Показать полностью…</span></div>
<a class="thumb_map_img" data-src_big="https://sun/big.jpg|1000|562" style="background-image:url(https://sun/small.jpg)"></a></div>
<div class="wall_item"><a class="wi_date" href="/wall-1234_554">6 окт в 12:30</a><div class="pi_text">Подборка: <a href="/away.php?to=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1">ссылка</a></div></div>
<div class="wall_item"><div class="ads_mark">Реклама</div><a class="wi_date" href="/wall-1234_553">5 окт</a><div class="pi_text">Купите слона</div></div>
<div class="wall_item"><a class="wi_date" href="/wall-1234_552">5 минут назад</a></div></div>"""


OK_PAGE = """<html><body><div class="sidebar"><a href="/othergroup/topic/999">Another post</a></div>
<div class="feed"><img class="avatar" src="https://i.okcdn.ru/i?r=AVATAR" width="40">
<div class="post"><a href="/kinopoisk/topic/157216895081823?st.layer.lg.ftid=1&amp;st.layer.lg.flt=GROUP"><img src="https://i.okcdn.ru/i?r=POST1&amp;fn=b" width="604"></a>
<a class="h" href="/kinopoisk/topic/157216895081823">Пока в российском прокате лидирует «Битва моторов»</a><div class="d">09:12</div>
<div class="t">Вспоминаем лучшие фильмы про гонки.</div>
<a href="/dk?cmd=logExternal&amp;st.cmd=logExternal&amp;st.link=https%3A%2F%2Fwww.kinopoisk.ru%2Fmedia%2Fnews%2F4013589%2F">kinopoisk.ru</a>
<span>Комментировать</span><span>Класс 35</span></div>
<div class="post"><a href="/kinopoisk/topic/157216895081822"><img src="https://iv.okcdn.ru/videoPreview?id=1&amp;type=0" width="500"></a>
<a href="/kinopoisk/topic/157216895081822">«Престижу» — 20 лет.</a><div>вчера, 21:05</div><a href="/kinopoisk/topic/157216895081822">Комментировать</a><span>Класс 12</span></div>
<div class="post"><a href="/kinopoisk/topic/157216895081821">Зумеры тут?</a><div>12 фев</div><span>Класс 3</span></div>
<div class="post"><a href="/kinopoisk/topic/157216895081820"></a><div>10:00</div></div></div></body></html>"""


class FakeNet:
    """Network stub: dict url -> bytes | int (HTTP error code). Records the requests."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append(url)
        v = self.pages.get(url)
        if v is None or isinstance(v, int):
            raise urllib.error.HTTPError(url, v or 404, "x", {}, None)
        return (v.encode() if isinstance(v, str) else v), "utf-8"

    def text(self, url, **kw):
        data, _ = self.get(url)
        return data.decode()

    def __enter__(self):
        self._old = (server.http_get, server.http_text)
        server.http_get, server.http_text = self.get, self.text
        return self

    def __exit__(self, *a):
        server.http_get, server.http_text = self._old


def tg_page(ids, base=94600):
    """A Telegram page with messages from the id list (ascending, like t.me)."""
    return "".join(
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="newschan/{i}">'
        f'<div class="tgme_widget_message_text js-message_text">Post {i}</div>'
        f'<time datetime="2026-09-10T{(i % 24):02d}:00:00+00:00"></time></div></div>' for i in sorted(ids))


class Parsers(unittest.TestCase):
    def test_rss_hinews(self):
        [it] = server.parse_feed(HINEWS.encode(), "https://hi-news.ru/feed", strip_code=True)
        self.assertEqual(it["title"], "Почему перед сном вдруг вспоминается всё, за что нам стыдно")
        self.assertEqual(it["image"], "https://hi-news.ru/wp-content/uploads/2026/10/a.jpg")
        self.assertEqual(it["published"], 1792087207)
        # the site front page shows the teaser (description), not the whole article
        self.assertEqual(it["text"], "Можно забыть важные события, но перед сном вдруг вспомнить случай, за который стыдно.")
        [full] = server.parse_feed(HINEWS.encode(), "https://hi-news.ru/feed", strip_code=True, teaser=False)
        self.assertIn("• Первый пункт", full["text"])
        self.assertNotIn("Подпись под фото", full["text"])
        self.assertNotIn("подписан на", full["text"])

    def test_rss_without_description_uses_short_start_of_article(self):
        long_text = "Word " * 400
        feed = (f'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>T</title><link>https://s/1</link>'
                f'<guid>1</guid><content:encoded><![CDATA[<p>{long_text}</p>]]></content:encoded></item></channel></rss>')
        [it] = server.parse_feed(feed.encode(), "https://s/feed")
        self.assertLessEqual(len(it["text"]), 360)
        self.assertTrue(it["text"].endswith("…"))

    def test_rss_collects_all_images_gifs_and_videos(self):
        feed = ('<rss xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:media="http://search.yahoo.com/mrss/"><channel><item>'
                '<title>T</title><link>https://s/1</link><guid>1</guid><media:content url="https://s/img/a-1024x768.jpg" medium="image"/>'
                '<description>Teaser</description>'
                '<content:encoded><![CDATA[<img src="https://s/img/a.jpg"><img src="https://s/img/b.png">'
                '<img src="https://s/img/anim.gif"><img src="https://s/i/emoji/smile.png"><img src="https://s/px.gif" width="1" height="1">'
                '<video poster="https://s/img/p.jpg"><source src="https://s/v/clip.mp4"></video>]]></content:encoded></item></channel></rss>')
        [one] = server.parse_feed(feed.encode(), "https://s/feed")
        self.assertEqual(len(one["media"]), 1)  # the record has no images of its own besides the main one - take one from the article body
        [it] = server.parse_feed(feed.encode(), "https://s/feed", teaser=False)
        kinds = [(m["t"], m["u"].rsplit("/", 1)[-1]) for m in it["media"]]
        self.assertEqual(kinds, [("image", "a-1024x768.jpg"), ("image", "b.png"), ("image", "anim.gif"), ("video", "clip.mp4")])
        self.assertEqual(it["media"][3]["p"], "https://s/img/p.jpg")  # a.jpg and a-1024x768.jpg are the same image

    def test_telegram_album_and_video_are_all_collected(self):
        page = ('<div class="tgme_widget_message" data-post="newschan/7"><div class="tgme_widget_message_text js-message_text">Roundup</div>'
                '<a class="tgme_widget_message_photo_wrap" style="background-image:url(\'https://cdn/p1.jpg\')"></a>'
                '<a class="tgme_widget_message_photo_wrap" style="background-image:url(\'https://cdn/p2.jpg\')"></a>'
                '<a class="tgme_widget_message_video_player"><i class="tgme_widget_message_video_thumb" style="background-image:url(\'https://cdn/t.jpg\')"></i>'
                '<video src="https://cdn/v.mp4" class="tgme_widget_message_video js-message_video"></video></a>'
                '<time datetime="2026-10-15T10:00:00+00:00"></time></div>')
        [it], _ = server.parse_telegram(page, "newschan")
        self.assertEqual([(m["t"], m["u"]) for m in it["media"]],
                         [("image", "https://cdn/p1.jpg"), ("image", "https://cdn/p2.jpg"), ("video", "https://cdn/v.mp4")])
        self.assertEqual(it["media"][2]["p"], "https://cdn/t.jpg")
        self.assertEqual(it["image"], "https://cdn/p1.jpg")

    def test_media_saved_and_returned_by_api(self):
        fresh_db()
        server.save_items("tgtest", [dict(guid="m1", title="Many images", text="", url="https://h/m1", ext_url=None, image="https://x/1.jpg",
                                         media=[{"u": "https://x/1.jpg", "t": "image"}, {"u": "https://x/2.gif", "t": "image"}], published=100),
                                    dict(guid="m2", title="One", text="", url="https://h/m2", ext_url=None, image="https://x/3.jpg", published=99)])
        got = {i["title"]: i for i in server.query_items(sources=["tgtest"])["items"]}
        self.assertEqual(len(got["Many images"]["media"]), 2)
        self.assertEqual(got["One"]["media"], [{"u": "https://x/3.jpg", "t": "image"}])  # old records have a single image

    def test_enrich_reaches_older_items_even_if_newest_ones_fail(self):
        fresh_db()
        items = [dict(guid=f"t{i}", title=f"T{i}", text="", url=f"https://tds/{i}", ext_url=None, image=None, published=1000 - i) for i in range(120)]
        server.save_items("tds", items)
        ids = [server.make_id("tds", f"t{i}") for i in range(120)]
        for iid in ids[:100]:
            server.ENRICH_FAILS[iid] = 2      # the newest ones are hopeless
        pages = {f"https://tds/{i}": f'<meta property="og:image" content="https://i/{i}.jpg">' for i in range(100, 120)}
        with FakeNet(pages):
            server.enrich_images(limit=25, source_id="tds")
        with server.db() as con:
            n = con.execute("SELECT COUNT(*) FROM items WHERE source='tds' AND image LIKE 'https://i/%'").fetchone()[0]
        server.ENRICH_FAILS.clear()
        self.assertEqual(n, 20)

    def test_readonly_write_is_retried_then_succeeds(self):
        calls = []
        @server.retry_readonly
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return "ok"
        real_sleep, server.time.sleep = server.time.sleep, lambda s: None
        try:
            self.assertEqual(flaky(), "ok")
        finally:
            server.time.sleep = real_sleep
        self.assertEqual(len(calls), 3)

    def test_rss_media_content(self):
        [it] = server.parse_feed(KDN.encode(), "https://www.kdnuggets.com/feed")
        self.assertEqual(it["image"], "https://www.kdnuggets.com/wp-content/uploads/awan.png")

    def test_rss_boilerplate_removed(self):
        [it] = server.parse_feed(TDS.encode(), "https://towardsdatascience.com/feed/")
        self.assertEqual(it["text"], "It only helps when your models make different mistakes.")
        self.assertIsNone(it["image"])

    def test_rss_truncated_feed_still_parsed(self):
        items = server.parse_feed(TRUNCATED.encode(), "https://news.example.org/topics/technology/feed/")
        self.assertEqual([i["title"] for i in items], ["Worst smartphones of 2026 named"])  # the truncated post is dropped
        self.assertEqual(server.feed_title(TRUNCATED.encode()), "Example News")

    def test_telegram(self):
        items, min_id = server.parse_telegram(TG, "newschan", noise=[r"^.*Donate via the bot.*$"])
        self.assertEqual(min_id, 94576)
        self.assertEqual([i["guid"] for i in items], ["newschan/94578", "newschan/94577"])
        a, b = items
        self.assertEqual(a["title"], "Hungary demanded that the EU compensate losses")
        self.assertEqual(a["text"], "The prime minister said the budgets are not ready.")
        self.assertEqual(a["image"], "https://cdn1.telesco.pe/file/photo1.jpg")
        self.assertEqual(a["ext_url"], "https://news.example.org/news/2026-09-10/x")
        self.assertEqual(a["published"], 1789046640)
        self.assertIsNone(b["image"])

    def test_vk_api(self):
        items = server.parse_vk(VK, "kinopoisk")
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it["title"], it["text"]), ("Новый трейлер Кинопоиска", "Подробности внутри"))
        self.assertEqual(it["image"], "https://vk/x.jpg")
        self.assertEqual(it["url"], "https://vk.ru/wall-1234_101")
        with self.assertRaises(RuntimeError):
            server.parse_vk({"error": {"error_msg": "bad token"}}, "x")

    def test_ru_dates(self):
        tz = 3
        f = lambda s: server.parse_ru_date(s, NOW, tz)
        # NOW = 17.10.2026 12:00 MSK
        self.assertEqual(f("сегодня в 9:05"), NOW - 175 * 60)
        self.assertEqual(f("вчера в 21:05"), NOW - 15 * 3600 + 5 * 60)
        self.assertEqual(f("5 минут назад"), NOW - 300)
        self.assertEqual(f("час назад"), NOW - 3600)
        self.assertEqual(f("только что"), NOW)
        oct6 = server.parse_ru_date("6 окт в 12:30", NOW, tz)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.gmtime(oct6 + 3 * 3600)), "2026-10-06 12:30")
        y = server.parse_ru_date("6 окт 2025", NOW, tz)
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(y + 3 * 3600)), "2025-10-06")
        # a date "in the future" without a year -> previous year (e.g. "20 дек" in October)
        d = server.parse_ru_date("20 дек", NOW, tz)
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(d + 3 * 3600)), "2025-12-20")
        self.assertIsNone(f("абракадабра"))

    def test_vk_mobile(self):
        items, raw = server.parse_vk_mobile(VK_MOBILE, "kinopoisk", NOW)
        self.assertEqual(raw, 4)                    # total posts on the page
        self.assertEqual(len(items), 2)             # the ad and the empty post are dropped
        a, b = items
        self.assertEqual(a["guid"], "-1234_555")
        self.assertEqual(a["title"], "Вышел трейлер сериала")
        self.assertEqual(a["text"], "Смотрим вместе")
        self.assertEqual(a["image"], "https://sun/big.jpg")
        self.assertEqual(a["url"], "https://vk.ru/wall-1234_555")
        self.assertEqual(a["published"], NOW - 15 * 3600 + 5 * 60)
        self.assertEqual(b["ext_url"], "https://example.com/a?b=1")

    def test_ok_group_page(self):
        items, raw = server.parse_ok(OK_PAGE, "kinopoisk", NOW)
        self.assertEqual(raw, 4)                       # posts of another group are not counted
        self.assertEqual(len(items), 3)                # the empty post is dropped
        a, b, c = items
        self.assertEqual(a["guid"], "157216895081823")
        self.assertEqual(a["title"], "Пока в российском прокате лидирует «Битва моторов»")
        self.assertEqual(a["text"], "Вспоминаем лучшие фильмы про гонки.")
        self.assertEqual(a["url"], "https://ok.ru/kinopoisk/topic/157216895081823")
        self.assertEqual(a["image"], "https://i.okcdn.ru/i?r=POST1&fn=b")      # the avatar is skipped
        self.assertEqual(a["ext_url"], "https://www.kinopoisk.ru/media/news/4013589/")
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.gmtime(a["published"] + 3 * 3600)), "2026-10-17 09:12")
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.gmtime(b["published"] + 3 * 3600)), "2026-10-16 21:05")
        self.assertEqual(b["image"], "https://iv.okcdn.ru/videoPreview?id=1&type=0")
        self.assertEqual(time.strftime("%m-%d", time.gmtime(c["published"] + 3 * 3600)), "02-12")
        self.assertNotIn("Класс", a["text"] + b["text"] + c["text"])
        self.assertNotIn("Комментировать", a["text"] + b["text"])

    def test_ok_time_without_date_rolls_back_to_yesterday_in_the_evening(self):
        late = NOW + 11 * 3600  # 23:00: "21:05" is not "from the future" yet, but after "09:12" it is yesterday's post
        page = "".join(f'<div><a href="/g/topic/{i}">Post {i}</a><div>{lab}</div></div>' for i, lab in [(2, "09:12"), (1, "21:05")])
        items, _ = server.parse_ok(page, "g", late)
        self.assertEqual([time.strftime("%d %H:%M", time.gmtime(i["published"] + 3 * 3600)) for i in items], ["17 09:12", "16 21:05"])

    def test_ok_time_found_in_various_markups(self):
        parse = lambda page, now=NOW: server.parse_ok(page, "g", now)[0]
        fmt = lambda ts: time.strftime("%d %H:%M", time.gmtime(ts + 3 * 3600))
        # <time datetime> - exact time with a date
        [a] = parse('<a href="/g/topic/1">Headline</a><time datetime="2026-10-15T09:12:00+03:00">вчера</time>')
        self.assertEqual(fmt(a["published"]), "15 09:12")
        # time inside a link with a date class, next to the group name on the same line
        [b] = parse('<div><a href="/g/topic/2">Новость</a><a class="feed_date" href="/g/topic/2">вчера, 21:05</a></div>')
        self.assertEqual(fmt(b["published"]), "16 21:05")
        # time merged with words on one line: take it and remove it from the text
        [c] = parse('<a href="/g/topic/3">Headline</a> <span>Кинопоиск 18:12</span>')
        self.assertEqual(fmt(c["published"]), "16 18:12")  # it is 12:00 now, 18:12 is "from the future" -> yesterday
        self.assertNotIn("18:12", c["title"] + c["text"])
        # a video duration (01:05) is not taken as the post time
        [d] = parse('<a href="/g/topic/4"><span class="video-duration">01:05</span></a><a href="/g/topic/4">Видео</a><div class="feed_date">09:30</div>')
        self.assertEqual(fmt(d["published"]), "17 09:30")

    def test_ok_without_any_dates_does_not_pile_up_at_top_of_feed(self):
        page = "".join(f'<div class="post"><a href="/g/topic/{i}">Post {i}</a></div>' for i in range(5, 0, -1))
        stats = {}
        items, _ = server.parse_ok(page, "g", NOW, stats=stats)
        self.assertEqual(stats["undated"], 5)
        ts = [i["published"] for i in items]
        self.assertEqual(ts, sorted(ts, reverse=True))
        self.assertGreaterEqual(ts[0] - ts[-1], 4 * 1800)  # spread out, not merged into the single second "now"
        server.STATUS_NOTE.pop("kinopoisk", None)
        with FakeNet({"https://ok.ru/kinopoisk": page.replace("/g/topic", "/kinopoisk/topic")}):
            server.fetch_ok(server.get_source("kinopoisk"))
        self.assertIn("time not determined", server.STATUS_NOTE.pop("kinopoisk"))

    def test_ok_undated_posts_keep_stored_time_between_refreshes(self):
        page = "".join(f'<div class="post"><a href="/kinopoisk/topic/{i}">Post {i}</a></div>' for i in range(1, 4)).replace("/g/", "/kinopoisk/")
        src = server.get_source("kinopoisk")
        with FakeNet({"https://ok.ru/kinopoisk": page}):
            first = server.fetch_ok(src)
        server.save_items("kinopoisk", first)
        stored = {i["guid"]: i["published"] for i in first}
        real = server.time.time
        server.time.time = lambda: real() + 3600  # an hour later the posts still have no time
        try:
            with FakeNet({"https://ok.ru/kinopoisk": page}):
                second = server.fetch_ok(src)
        finally:
            server.time.time = real
        self.assertEqual({i["guid"]: i["published"] for i in second}, stored)

    def test_ok_pinned_post_is_skipped_by_marker(self):
        page = ('<a href="/kinopoisk/topic/1">Pinned</a><div>Зумеры тут?</div><div>12 фев</div>'
                '<a href="/kinopoisk/topic/2">Fresh</a><div>09:00</div><a href="/kinopoisk/topic/3">Yesterday</a><div>вчера, 20:00</div>')
        pinned = []
        items, raw = server.parse_ok(page, "kinopoisk", NOW, pinned_out=pinned)
        self.assertEqual((raw, pinned, [i["guid"] for i in items]), (3, ["1"], ["2", "3"]))

    def test_ok_pinned_post_is_skipped_by_date_order(self):
        # markup without a marker: the first post is older than the second -> it is pinned
        page = ('<a href="/kinopoisk/topic/1">Old pinned</a><div>12 фев</div>'
                '<a href="/kinopoisk/topic/2">Fresh</a><div>09:00</div><a href="/kinopoisk/topic/3">Yesterday</a><div>вчера, 20:00</div>')
        pinned = []
        items, _ = server.parse_ok(page, "kinopoisk", NOW, pinned_out=pinned)
        self.assertEqual((pinned, [i["guid"] for i in items]), (["1"], ["2", "3"]))
        # a normal feed (first newer than second) has no pinned posts
        pinned = []
        server.parse_ok(OK_PAGE, "kinopoisk", NOW, pinned_out=pinned)
        self.assertEqual(pinned, [])

    def test_ok_order_follows_page_order(self):
        # three posts share the same time and the fourth is "from the future" relative to the third - keep the page order
        page = "".join(f'<a href="/g/topic/{i}">Post {i}</a><div>{t}</div>' for i, t in
                        ((4, "10:00"), (3, "10:00"), (2, "09:59"), (1, "11:30")))
        items, _ = server.parse_ok(page, "g", NOW)
        ts = [i["published"] for i in items]
        self.assertEqual([i["guid"] for i in items], ["4", "3", "2", "1"])
        self.assertTrue(all(a > b for a, b in zip(ts, ts[1:])), ts)  # strictly descending

    def test_ok_svg_junk_and_next_post_do_not_leak_into_text(self):
        page = ('<div><a href="/kinopoisk/topic/200">Зумеры тут?</a><div>17:34 · 4 ч назад</div><span>Поделились: 2</span>'
                '<a href="/kinopoisk/topic/199">Съемки сериала по Life Is Strange завершились.</a><div>17:30</div>'
                '<svg width="1"><path fill-rule="evenodd" clip-rule="evenodd" d="M6.6 4.6C2.8 5.7"/></svg></div>'
                '<div><a href="/kinopoisk/topic/198">Третий</a><div>вчера, 10:00</div>')
        items, _ = server.parse_ok(page, "kinopoisk", NOW)
        text = " ".join(i["title"] + " " + i["text"] for i in items)
        self.assertNotIn("path", text)
        self.assertNotIn("fill-rule", text)
        self.assertNotIn("Поделились", text)
        self.assertNotIn("Life Is Strange", items[0]["title"] + items[0]["text"])
        # a dangling tag at the end of a chunk does not turn into text
        self.assertEqual(server.html_to_text('Привет <path fill-rule="evenodd" d="M6.63587 4.68'), "Привет")

    def test_ok_pinned_by_id_order_without_dates(self):
        page = ('<a href="/kinopoisk/topic/100">Pinned one</a><div>12 фев</div>'
                '<a href="/kinopoisk/topic/300">Fresh one</a><div>09:00</div><a href="/kinopoisk/topic/290">Yesterday one</a><div>вчера, 20:00</div>')
        pinned = []
        items, _ = server.parse_ok(page, "kinopoisk", NOW, pinned_out=pinned)
        self.assertEqual((pinned, [i["guid"] for i in items]), (["100"], ["300", "290"]))

    def test_ok_relative_time(self):
        page = '<a href="/g/topic/2">Пост</a><div>Кинопоиск17:34 · 4 ч назад</div><a href="/g/topic/1">Старый</a><div>5 мин назад</div>'
        items, _ = server.parse_ok(page, "g", NOW)
        self.assertEqual(time.strftime("%d %H:%M", time.gmtime(items[0]["published"] + 3 * 3600)), "16 17:34")  # "from the future" -> yesterday
        self.assertEqual(items[0]["title"], "Пост")

    def test_ok_post_page_time(self):
        fmt = lambda ts: time.strftime("%d %H:%M", time.gmtime(ts + 3 * 3600))
        self.assertEqual(fmt(server.parse_ok_post_time('<time datetime="2026-10-15T09:12:00+03:00">x</time>', NOW)), "15 09:12")
        self.assertEqual(fmt(server.parse_ok_post_time('<body><div>Кинопоиск</div><div>вчера в 21:05</div><span class="video-duration">01:05</span></body>', NOW)), "16 21:05")
        self.assertIsNone(server.parse_ok_post_time("<body>нет времени</body>", NOW))

    def test_ok_exact_time_is_fetched_from_post_page_and_kept(self):
        page = ('<a href="/kinopoisk/topic/20">News A</a><div>09:00</div>'
                '<a href="/kinopoisk/topic/10">News B</a><div>вчера, 20:00</div>')
        pages = {"https://ok.ru/kinopoisk": page,
                 "https://ok.ru/kinopoisk/topic/20": '<time datetime="2026-10-17T08:41:00+03:00"></time>',
                 "https://ok.ru/kinopoisk/topic/10": '<time datetime="2026-10-16T19:13:00+03:00"></time>'}
        src = server.get_source("kinopoisk")
        with FakeNet(pages) as net:
            items = server.fetch_ok(src)
            self.assertEqual(len([c for c in net.calls if "/topic/" in c]), 2)
        self.assertEqual([time.strftime("%d %H:%M", time.gmtime(i["published"] + 3 * 3600)) for i in items], ["17 08:41", "16 19:13"])
        server.save_items("kinopoisk", items)
        server.OK_TEXT_TRIED.clear()
        with FakeNet(pages) as net:  # second pass: the time is already exact and not re-read (pages are opened only for an empty text)
            again = server.fetch_ok(src)
            self.assertLessEqual(len([c for c in net.calls if "/topic/" in c]), 2)
        self.assertEqual([i["published"] for i in again], [i["published"] for i in items])

    def test_browser_content_retries_while_page_is_navigating(self):
        class P:
            n = 0
            def content(self):
                P.n += 1
                if P.n < 3:
                    raise RuntimeError("Page.content: Unable to retrieve content because the page is navigating and changing the content.")
                return "<html>ok</html>"
            def wait_for_load_state(self, *a, **k): pass
            def wait_for_timeout(self, ms): pass
        g = server.PageGetter()
        g._page = P()
        self.assertEqual(g._content(), "<html>ok</html>")
        self.assertEqual(P.n, 3)

    def test_readonly_db_falls_back_to_home_copy(self):
        import tempfile, sqlite3 as sq
        tmp = tempfile.mkdtemp()
        old_path, old_home, old_probe = server.DB_PATH, os.environ.get("HOME"), server._heal_db_files
        server.DB_PATH = os.path.join(tmp, "news.db")
        sq.connect(server.DB_PATH).execute("CREATE TABLE t(x)").connection.close()
        os.environ["HOME"] = tmp
        server._heal_db_files = lambda p: None
        real_connect = server._connect
        def fake_connect(path):  # the original file is "read-only"
            if path == os.path.join(tmp, "news.db"):
                con = sq.connect(f"file:{path}?mode=ro", uri=True)
                con.row_factory = sq.Row
                return con
            return real_connect(path)
        server._connect = fake_connect
        try:
            server.ensure_writable_db()
            self.assertTrue(server.DB_PATH.endswith(os.path.join(".my-feed", "news.db")))
            self.assertIn("not writable", server.DB_NOTE)
        finally:
            server._connect, server._heal_db_files, server.DB_PATH, server.DB_NOTE = real_connect, old_probe, old_path, ""
            if old_home: os.environ["HOME"] = old_home

    def test_ok_empty_text_is_filled_from_post_page(self):
        page = '<a href="/kinopoisk/topic/50">Показать ещё</a><div>вчера, 10:00</div><a href="/kinopoisk/topic/40">Дублёр</a><div>12 фев</div>'
        post = ('<html><head><meta property="og:description" content="Съёмки сериала завершились.\nПремьера осенью.">'
                '<meta property="og:image" content="https://i.okcdn.ru/i?r=IMG"></head><body></body></html>')
        pages = {"https://ok.ru/kinopoisk": page, "https://ok.ru/kinopoisk/topic/50": post,
                 "https://ok.ru/kinopoisk/topic/40": "<html></html>"}
        src = server.get_source("kinopoisk")
        with FakeNet(pages):
            items = server.fetch_ok(src)
        it = [i for i in items if i["guid"] == "50"][0]
        self.assertEqual(it["title"], "Съёмки сериала завершились.")
        self.assertEqual(it["text"], "Премьера осенью.")
        self.assertEqual(it["image"], "https://i.okcdn.ru/i?r=IMG")
        # an already stored post without text is completed by a separate pass
        server.save_items("kinopoisk", [dict(guid="77", title="Headline", text="", url="https://ok.ru/kinopoisk/topic/77", ext_url=None, image=None, published=100)])
        pages["https://ok.ru/kinopoisk/topic/77"] = '<meta property="og:description" content="Headline\nПолный текст поста.">'
        g = server.PageGetter()
        with FakeNet(pages):
            server.ok_repair_texts(g, src)
        with server.db() as con:
            row = con.execute("SELECT title,text FROM items WHERE id=?", (server.make_id("kinopoisk", "77"),)).fetchone()
        self.assertEqual((row[0], row[1]), ("Headline", "Полный текст поста."))

    def test_ok_date_and_clock_in_separate_lines(self):
        page = '<a href="/g/topic/1">Пост</a><div>вчера</div><div>21:05</div>'
        [it], _ = server.parse_ok(page, "g", NOW)
        self.assertEqual(time.strftime("%m-%d %H:%M", time.gmtime(it["published"] + 3 * 3600)), "10-16 21:05")

    def test_ok_time_without_date_in_future_means_yesterday(self):
        page = '<a href="/g/topic/1">Новость</a><div>23:59</div>'
        [it], _ = server.parse_ok(page, "g", NOW)    # it is 12:00 now, so 23:59 was yesterday
        self.assertEqual(time.strftime("%m-%d %H:%M", time.gmtime(it["published"] + 3 * 3600)), "10-16 23:59")

    def test_article_page(self):
        page = ('<html><head><title>T</title><meta property="og:title" content="Headline &amp; co">'
                '<meta content="Description" property="og:description">'
                '<meta property="og:image" content="/img/1.jpg">'
                '<meta property="article:published_time" content="2026-10-15T10:00:00+03:00"></head></html>')
        it = server.parse_article_page(page, "https://news.example.org/news/1")
        self.assertEqual((it["title"], it["text"]), ("Headline & co", "Description"))
        self.assertEqual(it["image"], "https://news.example.org/img/1.jpg")
        self.assertEqual(it["published"], 1792047600)

    def test_article_without_description_uses_paragraphs(self):
        page = '<meta property="og:title" content="T"><p>short</p><p>' + "A long article paragraph. " * 5 + "</p>"
        it = server.parse_article_page(page, "https://x.ru/a")
        self.assertTrue(it["text"].startswith("A long article paragraph"))

    def test_site_links_from_json_and_html(self):
        link_re = SITE_SRC["config"]["link_re"]
        page = ('<a href="/news/2026-10-16/rain-started-1428786">x</a>'
                '<script>{"url":"https:\\/\\/news.example.org\\/news\\/2026-10-15\\/another-story-1428700","t":1}</script>'
                '<a href="/news/society">section</a><a href="/news/2026-10-16/rain-started-1428786">dup</a>')
        links = server.extract_links(page, "https://news.example.org/news", link_re)
        self.assertEqual(links, ["https://news.example.org/news/2026-10-16/rain-started-1428786",
                                 "https://news.example.org/news/2026-10-15/another-story-1428700"])

    def test_auto_links(self):
        page = ('<nav><a href="/">Home</a><a href="/about">About</a><a href="/topics/technology/">Technology</a>'
                '<a href="https://other.com/x-y-z">foreign</a></nav>'
                '<a href="/worst-smartphones-2026/">1</a><a href="/ctrl-energy-zero-announced/">2</a>'
                '<a href="https://news.example.org/magicos-11-released/">3</a><a href="/tag/phones-and-more/">tag</a>'
                '<a href="/pic.jpg">img</a>')
        links = server.auto_extract_links(page, "https://news.example.org/topics/technology/")
        self.assertEqual(links, ["https://news.example.org/worst-smartphones-2026/",
                                 "https://news.example.org/ctrl-energy-zero-announced/",
                                 "https://news.example.org/magicos-11-released/"])

    def test_split_title(self):
        self.assertEqual(server.split_title_text("A\n\nB"), ("A", "B"))
        t, body = server.split_title_text("слово " * 60)
        self.assertTrue(t.endswith("…") and len(t) <= 161 and body.startswith("слово"))


class Detect(unittest.TestCase):
    def test_telegram_and_vk(self):
        with FakeNet({"https://t.me/s/durov_news": '<meta property="og:title" content="Durov News">'}):
            s = server.detect_source("https://t.me/s/durov_news")
            self.assertEqual((s["type"], s["config"]["channel"], s["name"], s["id"]), ("telegram", "durov_news", "Durov News", "tg-durov_news".replace("_", "-")))
            self.assertEqual(server.detect_source("t.me/durov_news")["url"], "https://t.me/durov_news")
        with self.assertRaises(ValueError):
            server.detect_source("https://t.me/+AbCdEf")
        v = server.detect_source("https://m.vk.ru/kinopoisk")
        self.assertEqual((v["type"], v["config"]["domain"], v["url"]), ("vk", "kinopoisk", "https://m.vk.ru/kinopoisk"))
        self.assertEqual(server.detect_source("vk.com/rozetked")["config"]["domain"], "rozetked")
        with self.assertRaises(ValueError):
            server.detect_source("https://vk.com/feed")

    def test_ok_group_link(self):
        with FakeNet({"https://ok.ru/kinopoisk": '<meta property="og:title" content="Кинопоиск | Фильмы и сериалы | OK">'}):
            s = server.detect_source("https://ok.ru/kinopoisk")
        self.assertEqual((s["type"], s["config"]["domain"], s["url"], s["name"]), ("ok", "kinopoisk", "https://ok.ru/kinopoisk", "Кинопоиск"))
        self.assertEqual(server.detect_source("m.ok.ru/group/52728120934492")["config"]["domain"], "group/52728120934492")
        with self.assertRaises(ValueError):
            server.detect_source("https://ok.ru/feed")

    def test_direct_feed(self):
        with FakeNet({"https://hi-news.ru/feed": HINEWS}):
            s = server.detect_source("https://hi-news.ru/feed")
        self.assertEqual((s["type"], s["config"]["feed"], s["name"]), ("rss", "https://hi-news.ru/feed", "Hi-News"))

    def test_site_with_topic_feed_prefers_topic_feed(self):
        net = FakeNet({"https://news.example.org/topics/technology/": '<html><meta property="og:site_name" content="Example News"></html>',
                       "https://news.example.org/topics/technology/feed/": TRUNCATED})
        with net:
            s = server.detect_source("news.example.org/topics/technology/")
        self.assertEqual((s["type"], s["config"]["feed"]), ("rss", "https://news.example.org/topics/technology/feed/"))
        self.assertEqual(s["name"], "Example News · Technology")

    def test_site_without_feed_uses_listing(self):
        page = ('<html><title>News</title>' + "".join(f'<a href="/news/some-long-title-{i}/">x</a>' for i in range(5)) + "</html>")
        with FakeNet({"https://news.example.org/": page}):
            s = server.detect_source("https://news.example.org/")
        self.assertEqual((s["type"], s["config"]["auto_links"]), ("site", True))

    def test_errors_are_readable(self):
        with FakeNet({}):
            with self.assertRaises(ValueError) as c:
                server.detect_source("https://nothing.example.org/x")
        self.assertIn("404", str(c.exception))
        with self.assertRaises(ValueError):
            server.detect_source("")
        with self.assertRaises(ValueError):
            server.detect_source("just words")
        with FakeNet({"https://empty.example.org/": "<html><p>no news</p></html>"}):
            with self.assertRaises(ValueError) as c:
                server.detect_source("https://empty.example.org/")
        self.assertIn("RSS", str(c.exception))


class Fetch(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_telegram_catchup_closes_gap(self):
        src = server.get_source("tgtest")
        server.save_items("tgtest", [dict(guid="newschan/94561", title="old", text="", url="https://t.me/newschan/94561", ext_url=None, image=None, published=5)])
        net = FakeNet({
            "https://t.me/s/newschan": tg_page(range(94581, 94601)),
            "https://t.me/s/newschan?before=94581": tg_page(range(94561, 94581)),
            "https://t.me/s/newschan?before=94561": tg_page(range(94541, 94561)),  # we must not get here
        })
        with net:
            items = server.fetch_telegram(src)
        ids = sorted(int(i["guid"].split("/")[1]) for i in items)
        self.assertEqual(ids[0], 94561)  # 94561 is already known: overlap found, no gap
        self.assertEqual(ids[-1], 94600)
        self.assertEqual(len(net.calls), 2)

    def test_telegram_first_run_is_limited(self):
        pages = {"https://t.me/s/newschan": tg_page(range(94581, 94601))}
        for k in range(1, 6):
            hi = 94581 - 20 * (k - 1)
            pages[f"https://t.me/s/newschan?before={hi}"] = tg_page(range(hi - 20, hi))
        net = FakeNet(pages)
        with net:
            items = server.fetch_telegram(server.get_source("tgtest"))
        self.assertEqual(len(net.calls), 3)  # while the database is empty - 3 pages, the archive takes over after that
        self.assertEqual(len(items), 60)

    def test_rss_catchup_and_repeat_page_guard(self):
        src = server.get_source("hinews")
        mk = lambda n: ('<rss><channel>' + "".join(
            f'<item><title>T{i}</title><link>https://h/{i}</link><guid>g{i}</guid><pubDate>Thu, 15 Oct 2026 {i % 24:02d}:00:00 +0000</pubDate></item>'
            for i in n) + '</channel></rss>')
        server.save_items("hinews", [dict(guid="g5", title="T5", text="", url="https://h/5", ext_url=None, image=None, published=5)])
        net = FakeNet({"https://hi-news.ru/feed": mk(range(10, 20)), "https://hi-news.ru/feed?paged=2": mk(range(1, 10))})
        with net:
            items = server.fetch_rss(src)
        self.assertEqual(len(items), 19)  # up to and including the known g5
        # the site ignores ?paged= and returns the same page -> do not loop
        server.save_items("tds", [dict(guid="gx", title="x", text="", url=None, ext_url=None, image=None, published=5)])
        same = mk(range(10, 20))
        net = FakeNet({"https://towardsdatascience.com/feed/": same, "https://towardsdatascience.com/feed/?paged=2": same})
        with net:
            self.assertEqual(len(server.fetch_rss(server.get_source("tds"))), 10)

    def test_site_fetch_dates_from_url_and_known_skipped(self):
        src = server.get_source("examplesite")
        listing = ('<a href="/news/2026-10-16/a-novost-1">1</a><a href="/news/2026-10-15/b-novost-2">2</a>')
        art = lambda t: f'<meta property="og:title" content="{t}"><meta property="og:description" content="d">'
        server.save_items("examplesite", [dict(guid="https://news.example.org/news/2026-10-15/b-novost-2", title="b", text="", url="https://news.example.org/news/2026-10-15/b-novost-2", ext_url=None, image=None, published=1)])
        net = FakeNet({"https://news.example.org/news": listing, "https://news.example.org/news?page=2": "<html></html>",
                       "https://news.example.org/news/2026-10-16/a-novost-1": art("A")})
        with net:
            items = server.fetch_site(src)
        self.assertEqual([i["title"] for i in items], ["A"])
        self.assertEqual(items[0]["published"], server.parse_date("2026-10-16T12:00:00+03:00"))

    def test_site_gap_after_downtime_filled_from_sitemap(self):
        src = server.get_source("examplesite")
        old = "https://news.example.org/news/2026-10-10/staraya"
        server.save_items("examplesite", [dict(guid=old, title="s", text="", url=old, ext_url=None, image=None, published=1)])
        art = lambda t: f'<meta property="og:title" content="{t}"><meta property="og:description" content="d">'
        sm = ("<urlset>" + "".join(f"<url><loc>https://news.example.org/news/2026-10-{d}/n{d}</loc></url>" for d in (17, 16, 12)) +
              f"<url><loc>{old}</loc></url></urlset>")
        pages = {"https://news.example.org/news": '<a href="/news/2026-10-17/n17">1</a>', "https://news.example.org/news?page=2": "<html></html>",
                 "https://news.example.org/sitemap.xml": sm}
        for d in (17, 16, 12):
            pages[f"https://news.example.org/news/2026-10-{d}/n{d}"] = art(f"N{d}")
        server.SITEMAP_CACHE.clear()
        with FakeNet(pages):
            items = server.fetch_site(src)
        self.assertEqual(sorted(i["title"] for i in items), ["N12", "N16", "N17"])

    def test_enrich_busy_browser_is_not_a_failure(self):
        server.save_items("tds", [dict(guid="g1", title="t", text="", url="https://towardsdatascience.com/x", ext_url=None, image=None, published=5)])
        iid = server.make_id("tds", "g1")
        real_get, real_enable = server.http_text, server.PageGetter.enable_browser
        def deny(url, **kw):
            raise urllib.error.HTTPError(url, 403, "x", {}, None)
        def busy(self, light=False):
            raise server.BrowserBusy("busy")
        server.http_text, server.PageGetter.enable_browser = deny, busy
        self.addCleanup(setattr, server, "http_text", real_get)
        self.addCleanup(setattr, server.PageGetter, "enable_browser", real_enable)
        server.ENRICH_FAILS.clear()
        server.enrich_images(limit=5)
        self.assertEqual(server.ENRICH_FAILS.get(iid, 0), 0)

    def test_ok_normalize_unified_format(self):
        t, x = server.ok_normalize("•", "Стивен Содерберг поможет Мэтту Ривзу завершить съемки сиквела «Бэтмена» с Робертом Паттинсоном. "
                                         "По данным Deadline, Ривз временно отошел от работы.... | Вступай в группу Кинопоиск в Одноклассниках")
        self.assertTrue(t.startswith("Стивен Содерберг") and t.endswith("Паттинсоном."))
        self.assertTrue(x.startswith("По данным Deadline") and "Вступай" not in x)

    def test_net_down_detection(self):
        self.assertTrue(server._is_net_down(urllib.error.URLError(OSError(11001, "getaddrinfo failed"))))
        self.assertFalse(server._is_net_down(urllib.error.HTTPError("u", 403, "x", {}, None)))

    def test_site_name_suffix_stripped(self):
        page = ('<meta property="og:site_name" content="Example News">'
                '<meta property="og:title" content="Headline | Example News">'
                '<meta property="og:description" content="Body of the story - Example News">')
        it = server.parse_article_page(page, "https://news.example.org/news/2026-10-18/x")
        self.assertEqual((it["title"], it["text"]), ("Headline", "Body of the story"))

    def test_rss_media_from_post_not_article_body(self):
        feed = ('<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>T</title><link>https://s/1</link><guid>1</guid>'
                '<description><![CDATA[<img src="https://s/a.jpg"><img src="https://s/b.jpg"> Teaser]]></description>'
                '<content:encoded><![CDATA[<img src="https://s/c.jpg"><img src="https://s/d.jpg">]]></content:encoded></item></channel></rss>')
        [it] = server.parse_feed(feed.encode(), "https://s/feed")
        self.assertEqual([m["u"].rsplit("/", 1)[-1] for m in it["media"]], ["a.jpg", "b.jpg"])

    def test_ok_junk_lines_and_split(self):
        t, x = server.ok_normalize("ещё 3 фотографии", "Эль и Дакота Фаннинг, Александр и Билл Скарсгарды, Шарлиз Терон и Ана де Армас побывали на Academy Museum Gala. Это благотворительный вечер, который ежегодно проводит Музей киноакадемии.\n653 просмотра")
        self.assertTrue(t.endswith("Academy Museum Gala.") and t.startswith("Эль"))
        self.assertTrue(x.startswith("Это благотворительный вечер") and "просмотр" not in x)
        t, x = server.ok_normalize("«Проклятье белых глаз» — тизер", "653 просмотра")
        self.assertEqual((t, x), ("«Проклятье белых глаз» — тизер", ""))

    def test_ok_ext_link_only_own_site(self):
        chunk = ('<a href="/kinopoisk/topic/1">пост</a><a href="https://example.org/x">чужая</a>'
                 '<a href="https://www.kinopoisk.ru/media/news/4093399/">статья</a>')
        page = chunk + '<div>Пост</div>'
        self.assertEqual(server._ok_ext_link(page, "kinopoisk"), "https://www.kinopoisk.ru/media/news/4093399/")
        self.assertIsNone(server._ok_ext_link('<a href="https://example.org/x">x</a>', "kinopoisk"))

    def test_ok_links_kept_as_hyperlinks(self):
        part = ('<a href="https://ok.ru/kinopoisk/topic/1">пост</a><a href="/dk?st.cmd=outLinkWarning&amp;st.link=https%3A%2F%2Fwww.kinopoisk.ru%2Fmedia%2Fnews%2F1%2F">Читать на Кинопоиске</a>'
                '<a href="https://example.org/a">https://example.org/a</a>')
        got = server._ok_links(part)
        self.assertEqual(got, [{"t": "Читать на Кинопоиске", "u": "https://www.kinopoisk.ru/media/news/1/"}, {"t": "example.org", "u": "https://example.org/a"}])
        server.save_items("kinopoisk", [dict(guid="L1", title="t", text="x", url="https://ok.ru/kinopoisk/topic/9", ext_url=None, image=None, links=got, published=5)])
        it = [i for i in server.query_items(sources=["kinopoisk"])["items"] if i["url"].endswith("/9")][0]
        self.assertEqual(it["links"], got)

    def test_retry_on_database_locked(self):
        calls = []
        old = server.RO_WAITS
        server.RO_WAITS = (0, 0, 0)
        self.addCleanup(setattr, server, "RO_WAITS", old)

        @server.retry_readonly
        def w():
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"
        self.assertEqual(w(), "ok")
        self.assertEqual(len(calls), 3)

    def test_mlm_has_alt_feeds(self):
        self.assertIn("feedburner", " ".join(server.get_source("mlm").get("alt_feeds", [])))

    def test_hinews_telegram_rules(self):
        server.init_db()
        with server.db() as con:
            con.execute("UPDATE sources SET type='telegram', url='https://t.me/hinews_ru', config=? WHERE id='hinews'",
                        (json.dumps(server.HINEWS_TG),))
        src = server.get_source("hinews")
        post = lambda i, body, extra="": (f'<div class="tgme_widget_message" data-post="hinews_ru/{i}"><div class="tgme_widget_message_text js-message_text">{body}</div>{extra}'
                                          f'<time datetime="2026-10-15T10:00:00+00:00"></time></div>')
        page = (post(1, 'Новость про <a href="https://hi-news.ru/tech/x.html">сайт</a><br/>Hi-News в MAX | Подписаться')
                + post(2, 'Реклама <a href="?q=%23реклама">#реклама</a>')
                + post(3, 'Просто пост без ссылки<br/>Hi-News в MAX | Подписаться'))
        items, _ = server._tg_parse(src, "hinews_ru", page)
        self.assertEqual([i["guid"] for i in items], ["hinews_ru/3", "hinews_ru/1"][::-1] if items[0]["guid"] == "hinews_ru/1" else ["hinews_ru/3", "hinews_ru/1"])
        by = {i["guid"]: i for i in items}
        self.assertEqual(by["hinews_ru/1"]["ext_url"], "https://hi-news.ru/tech/x.html")
        self.assertIsNone(by["hinews_ru/3"]["ext_url"])
        self.assertTrue(all("MAX" not in i["title"] + i["text"] for i in items))

    def test_rozetked_two_channels_one_source(self):
        src = server.get_source("rozetked")
        self.assertEqual((src["type"], src["channels"]), ("telegram", ["rozetked", "rozetkedplus"]))
        self.assertTrue(src["skip"])  # posts with #реклама are not shown
        page = lambda chan: (f'<div class="tgme_widget_message" data-post="{chan}/7"><div class="tgme_widget_message_text js-message_text">Post {chan}</div>'
                             '<time datetime="2026-10-15T10:00:00+00:00"></time></div>')
        with FakeNet({"https://t.me/s/rozetked": page("rozetked"), "https://t.me/s/rozetkedplus": page("rozetkedplus"),
                      "https://t.me/s/rozetked?before=7": "<html></html>", "https://t.me/s/rozetkedplus?before=7": "<html></html>"}):
            items = server.fetch_telegram(src)
        self.assertEqual(sorted(i["guid"] for i in items), ["rozetked/7", "rozetkedplus/7"])

    def test_telegram_pin_notices_skipped(self):
        post = lambda i, body, cls="": (f'<div class="tgme_widget_message{cls}" data-post="newschan/{i}"><div class="tgme_widget_message_text js-message_text">{body}</div>'
                                        '<time datetime="2026-10-15T10:00:00+00:00"></time></div>')
        page = (post(1, "News Channel pinned a photo") + post(2, "Депутат закрепил за собой кабинет") + post(3, "Обычная новость"))
        items, _ = server.parse_telegram(page, "newschan")
        self.assertEqual(sorted(i["guid"] for i in items), ["newschan/2", "newschan/3"])
        self.assertTrue(server.is_pin_notice("Канал pinned «Headline»"))
        self.assertFalse(server.is_pin_notice("Суд закрепил права на землю"))

    def test_ok_heal_fills_gap_left_between_refreshes(self):
        src = server.get_source("kinopoisk")
        posts, _ = server.parse_ok(OK_PAGE, "kinopoisk")
        self.assertGreater(len(posts), 2)
        server.save_items("kinopoisk", posts[:1])  # only the newest post is in the database, the rest are "missed"
        class P:
            def evaluate(self, *a): return None
            def wait_for_timeout(self, ms): pass
            keyboard = type("K", (), {"press": lambda self, k: None})()
        class G:
            _page = P()
            def _content(self): return OK_PAGE
            def close(self): pass
        real_open, real_times = server._ok_open, server.ok_post_times
        server._ok_open, server.ok_post_times = lambda s: G(), lambda *a, **k: None
        self.addCleanup(setattr, server, "_ok_open", real_open)
        self.addCleanup(setattr, server, "ok_post_times", real_times)
        n = server.ok_heal(src, 30)
        self.assertEqual(n, len(posts) - 1)
        self.assertEqual(server.ok_heal(src, 30), 0)  # a repeated pass does not duplicate anything

    def test_kinopoisk_default_is_ok_group(self):
        src = server.get_source("kinopoisk")
        self.assertEqual((src["type"], src["url"], src["domain"]), ("ok", "https://ok.ru/kinopoisk", "kinopoisk"))
        with FakeNet({"https://ok.ru/kinopoisk": OK_PAGE}):
            items = server.fetch_ok(src)
        self.assertEqual(len(items), 3)
        new = server.save_items("kinopoisk", items)
        self.assertEqual(new, 3)
        import importlib.util
        real_spec = importlib.util.find_spec
        importlib.util.find_spec = lambda name, *a: None if name == "playwright" else real_spec(name, *a)
        try:
            ok, why = server.bf_available(src)
            self.assertFalse(ok)  # without browser mode the OK archive is unavailable - and the message says so honestly
            self.assertIn("--install-browser", why)
            info = {x["id"]: x for x in server.sources_info()["sources"]}
            self.assertFalse(info["kinopoisk"]["archive"]["supported"])
        finally:
            importlib.util.find_spec = real_spec
        # a page without posts -> a clear error and no crashes
        real = server.PageGetter.enable_browser
        server.PageGetter.enable_browser = lambda self: (_ for _ in ()).throw(RuntimeError("no browser"))
        try:
            with FakeNet({"https://ok.ru/kinopoisk": "<html>Войдите</html>"}):
                with self.assertRaises(RuntimeError):
                    server.fetch_ok(src)
        finally:
            server.PageGetter.enable_browser = real

    def test_ok_refresh_removes_stored_pinned_and_fixes_times(self):
        src = server.get_source("kinopoisk")
        page = ('<a href="/kinopoisk/topic/1">Pinned</a><div>Зумеры тут?</div><div>12 фев</div>'
                '<a href="/kinopoisk/topic/2">Fresh</a><div>09:00</div>')
        server.save_items("kinopoisk", [
            dict(guid="1", title="Зумеры тут?", text="", url="https://ok.ru/kinopoisk/topic/1", ext_url=None, image=None, published=server.NOW if hasattr(server, "NOW") else 5),
            dict(guid="2", title="Fresh", text="", url="https://ok.ru/kinopoisk/topic/2", ext_url=None, image=None, published=12345)])
        with FakeNet({"https://ok.ru/kinopoisk": page}):
            items = server.fetch_ok(src)
        server.save_items("kinopoisk", items)
        titles = {i["title"]: i["published"] for i in server.query_items(sources=["kinopoisk"])["items"]}
        self.assertEqual(list(titles), ["Fresh"])          # the pinned one is removed
        self.assertGreater(titles["Fresh"], 1700000000)    # time fixed from the page (was 12345)

    def test_site_reads_only_news_section(self):
        src = server.get_source("examplesite")
        self.assertNotIn("extra", src)  # the main page is not used
        art = lambda t: f'<meta property="og:title" content="{t}"><meta property="og:description" content="d">'
        net = FakeNet({"https://news.example.org/news": '<a href="/news/2026-10-16/s-razdela-1">1</a>',
                       "https://news.example.org/news?page=2": "<html></html>",
                       "https://news.example.org/news/2026-10-16/s-razdela-1": art("Section")})
        with net:
            self.assertEqual([i["title"] for i in server.fetch_site(src)], ["Section"])
        self.assertTrue(all(u.startswith("https://news.example.org/news") for u in net.calls))

    def test_rss_403_uses_alt_feed_before_browser(self):
        src = server.get_source("kdnuggets")
        self.assertTrue(src["alt_feeds"])
        real_raw, real_en = server.PageGetter.raw, server.PageGetter.enable_browser
        def fake_raw(self, url):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        server.PageGetter.raw = fake_raw
        server.PageGetter.enable_browser = lambda self: (_ for _ in ()).throw(AssertionError("the browser is not needed"))
        try:
            with FakeNet({src["alt_feeds"][0]: KDN}):
                items = server.fetch_rss(src)
        finally:
            server.PageGetter.raw, server.PageGetter.enable_browser = real_raw, real_en
        self.assertEqual(len(items), 1)

    def test_rss_403_falls_back_to_browser(self):
        src = dict(server.get_source("kdnuggets"))
        src.pop("alt_feeds", None)
        calls = []
        real_raw, real_en = server.PageGetter.raw, server.PageGetter.enable_browser

        def fake_raw(self, url):
            calls.append((self.browser_mode, url))
            if not self.browser_mode:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return KDN.encode()
        server.PageGetter.raw = fake_raw
        server.PageGetter.enable_browser = lambda self: setattr(self, "browser_mode", True)
        try:
            items = server.fetch_rss(src)
        finally:
            server.PageGetter.raw, server.PageGetter.enable_browser = real_raw, real_en
        self.assertEqual([i["title"] for i in items], ["7 Open-Source AI Tools That Can Replace Paid Subscriptions"])
        self.assertEqual(calls[0][0], False)
        self.assertEqual(calls[1][0], True)
        # without a browser - a clear error with a hint, not a bare "403"
        server.PageGetter.raw = fake_raw
        server.PageGetter.enable_browser = lambda self: (_ for _ in ()).throw(RuntimeError("no browser"))
        try:
            with self.assertRaises(RuntimeError) as c:
                server.fetch_rss(src)
        finally:
            server.PageGetter.raw, server.PageGetter.enable_browser = real_raw, real_en
        self.assertIn("--install-browser", str(c.exception))

    def test_rss_empty_second_page_is_end_not_error(self):
        src = dict(id="genericrss", name="LH", type="rss", url="https://news.example.org/topics/technology/",
                   feed="https://news.example.org/topics/technology/feed/")
        server.save_items("genericrss", [dict(guid="old", title="o", text="", url="https://l/o", ext_url=None, image=None, published=1)])
        feed = TRUNCATED.replace('<description>An eternal powerba', '<description>An eternal powerbank</description></item>')
        with FakeNet({"https://news.example.org/topics/technology/feed/": feed,
                      "https://news.example.org/topics/technology/feed/?paged=2": ""}):
            items = server.fetch_rss(src)
        self.assertGreaterEqual(len(items), 1)  # the second page is empty -> that is the end, not "ParseError: no element found"
        # an empty first page - a clear error, not ParseError
        real = server.PageGetter.enable_browser
        server.PageGetter.enable_browser = lambda self: (_ for _ in ()).throw(RuntimeError("no browser"))
        try:
            with FakeNet({"https://news.example.org/topics/technology/feed/": ""}):
                with self.assertRaises(RuntimeError) as c:
                    server.fetch_rss(src)
        finally:
            server.PageGetter.enable_browser = real
        self.assertNotIn("no element found", str(c.exception))

    def test_rss_archive_403_goes_through_browser(self):
        src = server.get_source("kdnuggets")
        real_raw, real_en = server.PageGetter.raw, server.PageGetter.enable_browser

        def fake_raw(self, url):
            if not self.browser_mode:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return KDN.encode() if "paged=2" in url else b""
        server.PageGetter.raw = fake_raw
        server.PageGetter.enable_browser = lambda self: setattr(self, "browser_mode", True)
        try:
            items, cur, done, msg = server.bf_step(src, {"page": 2})
            self.assertEqual(len(items), 1)
            self.assertEqual(cur["page"], 3)
            items, cur, done, msg = server.bf_step(src, cur)  # an empty page - the archive is over, no error
            self.assertEqual((items, done), ([], True))
        finally:
            server.PageGetter.raw, server.PageGetter.enable_browser = real_raw, real_en
            for g in list(server.BF_GETTERS.values()):
                g.close()
            server.BF_GETTERS.clear()

    def test_vk_user_agent_rotation(self):
        src = server.get_source("kinopoisk")
        old, old_ok = server.VK_TOKEN, dict(server.VK_UA_OK)
        server.VK_TOKEN = ""
        server.VK_UA_OK.clear()
        seen = []
        real = server.http_text

        def fake(url, ua=None, **k):
            seen.append(ua)
            if ua != server.FIREFOX_UA:
                return "<html>Your browser is out of date</html>"
            return VK_MOBILE if "offset" not in url else "<html></html>"
        server.http_text = fake
        try:
            items = server.fetch_vk(src)
        finally:
            server.http_text, server.VK_TOKEN = real, old
            server.VK_UA_OK.clear(); server.VK_UA_OK.update(old_ok)
        self.assertEqual([i["guid"] for i in items], ["-1234_555", "-1234_554"])
        self.assertEqual(seen[:2], [server.MOBILE_UA, server.FIREFOX_UA])  # the stub was skipped, a suitable UA was found

    def test_enrich_images_retries_after_network_error(self):
        server.save_items("tds", [dict(guid="z1", title="t", text="", url="https://towardsdatascience.com/z1/", ext_url=None, image=None, published=9)])
        real = server.http_text
        server.http_text = lambda url, **k: (_ for _ in ()).throw(urllib.error.URLError("network unreachable"))
        try:
            server.enrich_images()
        finally:
            server.http_text = real
        with server.db() as con:
            self.assertIsNone(con.execute("SELECT image FROM items WHERE title='t'").fetchone()[0])  # not marked "no image"
        server.http_text = lambda url, **k: '<meta property="og:image" content="/img/z1.jpg">'
        try:
            server.enrich_images()
        finally:
            server.http_text = real
        with server.db() as con:
            self.assertEqual(con.execute("SELECT image FROM items WHERE title='t'").fetchone()[0], "https://towardsdatascience.com/img/z1.jpg")

    def test_source_time_budget_stops_slow_source(self):
        g = server.PageGetter(budget=1)
        g.t0 -= 5  # the budget is already used up
        with self.assertRaises(TimeoutError):
            g.text("https://example.org/")
        # articles that did not load in time are skipped and do not break the source
        self.assertEqual(server._read_articles(g, ["https://example.org/a"], "x"), [])

    def test_vk_without_token_reads_only_mobile_vk(self):
        old = server.VK_TOKEN
        server.VK_TOKEN = ""
        try:
            src = server.get_source("kinopoisk")
            self.assertNotIn("fallback", src)  # the Kinopoisk site is no longer a source
            with FakeNet({"https://m.vk.ru/kinopoisk": VK_MOBILE, "https://m.vk.ru/kinopoisk?offset=4": "<html></html>"}):
                items = server.fetch_vk(src)
            self.assertEqual([i["guid"] for i in items], ["-1234_555", "-1234_554"])
            # m.vk.ru returned nothing -> an error, and no requests to kinopoisk.ru
            net = FakeNet({"https://m.vk.ru/kinopoisk": "<html>Your browser is out of date</html>"})
            real = server.PageGetter.enable_browser
            server.PageGetter.enable_browser = lambda self: (_ for _ in ()).throw(RuntimeError("no browser"))
            try:
                with net:
                    with self.assertRaises(Exception):
                        server.fetch_vk(src)
            finally:
                server.PageGetter.enable_browser = real
            self.assertTrue(all("kinopoisk.ru" not in u for u in net.calls))
        finally:
            server.VK_TOKEN = old

    def test_ssl_error_gives_clear_status_and_tries_browser(self):
        import ssl
        err = urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate"))
        tried = []
        real_text, real_en = server.http_text, server.PageGetter.enable_browser
        server.http_text = lambda url, **k: (_ for _ in ()).throw(err)
        server.PageGetter.enable_browser = lambda self: (tried.append(1), (_ for _ in ()).throw(RuntimeError("no browser")))[1]
        old = server.VK_TOKEN
        server.VK_TOKEN = ""
        try:
            server.refresh_source(server.get_source("kinopoisk"))
        finally:
            server.http_text, server.PageGetter.enable_browser, server.VK_TOKEN = real_text, real_en, old
        self.assertTrue(tried)  # on a certificate error the browser is tried
        with server.db() as con:
            msg = con.execute("SELECT message FROM source_status WHERE source='kinopoisk'").fetchone()[0]
        self.assertIn("NEWS_INSECURE_SSL=1", msg)
        self.assertNotIn("kinopoisk", [s for s in server.STATE["busy"]])  # the "refreshing" indicator is cleared

    def test_facebook_is_refused_with_explanation(self):
        with self.assertRaises(ValueError) as c:
            server.detect_source("https://www.facebook.com/kinopoisk")
        self.assertIn("Facebook", str(c.exception))


class Backfill(unittest.TestCase):
    def setUp(self):
        fresh_db()
        server.BF.update(enabled=True, until=None)
        server.BF["errors"].clear()
        server.SITEMAP_CACHE.clear(); server.SITEMAP_BAD.clear(); server.OK_TEXT_TRIED.clear()
        self._delay, self._days = server.BACKFILL_DELAY, server.BF_DAYS
        server.BACKFILL_DELAY, server.BF_DAYS = 0, 0   # here the full walk is tested; the lazy mode has its own test
        # keep two sources so that the test is fast
        for s in server.get_sources():
            if s["id"] not in ("tgtest", "hinews"):
                server.remove_source(s["id"])

    def tearDown(self):
        server.BACKFILL_DELAY, server.BF_DAYS = self._delay, self._days

    def test_lazy_backfill_stops_beyond_window_and_resumes_on_request(self):
        server.BF_DAYS = 30
        now = int(time.time())
        server.save_items("tgtest", [dict(guid="newschan/50", title="old", text="", url="https://t.me/newschan/50", ext_url=None, image=None, published=now - 40 * 86400)])
        calls = []
        real = server.bf_step
        server.bf_step = lambda src, cur: (calls.append(src["id"]) or ([], cur, True, None))
        self.addCleanup(setattr, server, "bf_step", real)
        server.backfill_pass()
        self.assertNotIn("tgtest", calls)  # the 30-day window is already covered - do not go deeper
        server.BF["until"] = now - 60 * 86400  # the user scrolled deeper
        server.backfill_pass()
        self.assertIn("tgtest", calls)

    def test_telegram_archive_walks_back_until_start(self):
        server.save_items("tgtest", [dict(guid="newschan/25", title="p25", text="", url="https://t.me/newschan/25", ext_url=None, image=None, published=100)])
        pages = {"https://t.me/s/newschan?before=25": tg_page(range(5, 25)), "https://t.me/s/newschan?before=5": tg_page(range(1, 5))}
        with FakeNet(pages):
            server.remove_source("hinews")
            n = 0
            while server.backfill_pass() and n < 10:
                n += 1
        row = server.bf_get("tgtest")
        self.assertEqual(row["done"], 1)
        self.assertEqual(row["loaded"], 24)

    def test_rss_archive_stops_when_site_ignores_paged_param(self):
        feed = '<rss><channel><item><title>a</title><link>https://h/1</link><guid>g1</guid><pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate></item></channel></rss>'
        with FakeNet({"https://hi-news.ru/feed": feed, "https://hi-news.ru/feed?paged=2": feed}):
            server.remove_source("tgtest")
            server.backfill_pass()
        row = server.bf_get("hinews")
        self.assertEqual(row["done"], 1)
        self.assertIn("does not support", row["message"])

    def test_rss_archive_keeps_going_through_pages_of_known_items(self):
        mk = lambda g, d: (f'<rss><channel><item><title>{g}</title><link>https://h/{g}</link><guid>{g}</guid>'
                           f'<pubDate>Mon, {d:02d} Jan 2024 10:00:00 +0000</pubDate></item></channel></rss>')
        server.remove_source("tgtest")
        known = [dict(guid=f"g{i}", title=f"t{i}", text="", url=f"https://h/g{i}", ext_url=None, image=None, published=1700000000 + i) for i in range(2, 6)]
        server.save_items("hinews", known)
        pages = {"https://hi-news.ru/feed": mk("g1", 1)}
        for n in range(2, 6):
            pages[f"https://hi-news.ru/feed?paged={n}"] = mk(f"g{n}", n)
        pages["https://hi-news.ru/feed?paged=6"] = mk("g9", 9)
        with FakeNet(pages):
            for _ in range(8):  # four pages in a row without new items do not mean the end of the archive
                server.backfill_pass()
        row = server.bf_get("hinews")
        self.assertEqual(row["loaded"], 1)  # g9 from page 6 was found although pages 2-5 gave nothing new
        self.assertEqual(row["done"], 1)    # it ended only on the empty/nonexistent page 7

    def test_site_archive_discovers_pagination_pattern(self):
        server.remove_source("tgtest"); server.remove_source("hinews")
        server.add_source(SITE_SRC)
        sid = "examplesite"
        src = server.get_source(sid)
        link = lambda d, n: f'<a href="/news/2026-10-{d:02d}/stat-{n}">Article {n}</a>'
        art = lambda n, d: (f'<html><head><meta property="og:title" content="Article {n}">'
                            f'<meta property="article:published_time" content="2026-10-{d:02d}T10:00:00+03:00"></head></html>')
        base = src["url"]
        pages = {base: link(17, 1) + link(17, 2) + '<a href="/news/?PAGEN_1=2">2</a>',
                 base + "?page=2": link(17, 1) + link(17, 2),  # ?page= is ignored by the site -> the same page
                 base + "?PAGEN_1=2": link(10, 3) + link(10, 4),
                 base + "?PAGEN_1=3": link(5, 5)}
        for n, d in ((3, 10), (4, 10), (5, 5)):
            pages[f"https://news.example.org/news/2026-10-{d:02d}/stat-{n}"] = art(n, d)
        with FakeNet(pages):
            for _ in range(6):
                server.backfill_pass()
        with server.db() as con:
            n = con.execute("SELECT COUNT(*) FROM items WHERE source=?", (sid,)).fetchone()[0]
        self.assertEqual(n, 3)
        self.assertEqual(server.bf_get(sid)["done"], 1)

    def test_site_archive_falls_back_to_sitemap(self):
        server.remove_source("tgtest"); server.remove_source("hinews")
        server.add_source(SITE_SRC)
        base = "https://news.example.org/news"
        art = lambda n, d: (f'<html><head><meta property="og:title" content="Article {n}">'
                            f'<meta property="article:published_time" content="2026-09-{d:02d}T10:00:00+03:00"></head></html>')
        lst = '<a href="/news/2026-10-17/s-1">a</a><a href="/news/2026-10-17/s-2">b</a>'
        sm = ('<urlset><url><loc>https://news.example.org/news/2026-10-17/s-1</loc></url>'
              '<url><loc>https://news.example.org/news/2026-09-02/s-3</loc><lastmod>2026-09-02</lastmod></url>'
              '<url><loc>https://news.example.org/news/2026-09-01/s-4</loc><lastmod>2026-09-01</lastmod></url>'
              '<url><loc>https://news.example.org/about</loc></url></urlset>')
        pages = {base: lst, "https://news.example.org/sitemap.xml": sm,   # ?page=, PAGEN, /page/, ?p= - all return 404 (not in the dict)
                 "https://news.example.org/news/2026-10-17/s-1": art(1, 17), "https://news.example.org/news/2026-09-02/s-3": art(3, 2),
                 "https://news.example.org/news/2026-09-01/s-4": art(4, 1)}
        class FakePage:
            def evaluate(self, *a, **k): pass
            class keyboard:
                @staticmethod
                def press(k): pass
            def wait_for_timeout(self, ms): pass
        class NoBrowser(server.PageGetter):  # we do not launch a real Chromium in tests
            def enable_browser(self, light=False): self._page = FakePage()
            def _goto(self, url): pass
            def _content(self): return lst
        real = server.PageGetter
        server.PageGetter = NoBrowser
        try:
            with FakeNet(pages):
                for _ in range(8):
                    server.backfill_pass()
        finally:
            server.PageGetter = real
            server.SITE_SCROLL.clear()
        with server.db() as con:
            titles = sorted(r[0] for r in con.execute("SELECT title FROM items WHERE source='examplesite'"))
        self.assertEqual(titles, ["Article 1", "Article 3", "Article 4"])
        self.assertEqual(server.bf_get("examplesite")["done"], 1)

    def test_site_archive_by_day_pages(self):
        server.remove_source("tgtest"); server.remove_source("hinews")
        server.add_source(SITE_SRC)
        today = datetime.now(timezone(timedelta(hours=server.VK_TZ_HOURS))).date()
        day = lambda k: (today - timedelta(days=k)).isoformat()
        art = lambda n, d: (f'<html><head><meta property="og:title" content="Day {n}">'
                            f'<meta property="article:published_time" content="{d}T10:00:00+03:00"></head></html>')
        pages = {"https://news.example.org/news": f'<a href="/news/{day(0)}/a-0">x</a>'}
        for k in (1, 2, 3, 4):
            pages[f"https://news.example.org/news/{day(k)}"] = f'<a href="/news/{day(k)}/a-{k}">x</a>'
            pages[f"https://news.example.org/news/{day(k)}/a-{k}"] = art(k, day(k))
        pages[f"https://news.example.org/news/{day(0)}/a-0"] = art(0, day(0))
        with FakeNet(pages):
            for _ in range(12):
                server.backfill_pass()
        with server.db() as con:
            titles = sorted(r[0] for r in con.execute("SELECT title FROM items WHERE source='examplesite'"))
        self.assertEqual(titles, ["Day 1", "Day 2", "Day 3", "Day 4"])  # today is read by the regular refresh

    def test_two_browsers_in_one_thread_share_playwright(self):
        import types
        started = []
        class PW:
            def __init__(self): self.stopped = False
            class chromium:
                @staticmethod
                def launch(**k):
                    return types.SimpleNamespace(close=lambda: None, new_context=lambda **kw: types.SimpleNamespace(
                        add_init_script=lambda s: None, new_page=lambda: object()))
            def stop(self): self.stopped = True
        class Starter:
            def start(self):
                if any(not p.stopped for p in started):
                    raise RuntimeError("It looks like you are using Playwright Sync API inside the asyncio loop.")
                started.append(PW()); return started[-1]
        mod = types.ModuleType("playwright.sync_api"); mod.sync_playwright = lambda: Starter()
        pkg = types.ModuleType("playwright"); pkg.sync_api = mod
        saved = {k: sys.modules.get(k) for k in ("playwright", "playwright.sync_api")}
        sys.modules["playwright"], sys.modules["playwright.sync_api"] = pkg, mod
        server._PW_TLS.st = None
        old_max, server.BROWSER_MAX = server.BROWSER_MAX, 2
        try:
            a, b = server.PageGetter(), server.PageGetter()
            a.enable_browser(); b.enable_browser()   # the second launch used to fail
            self.assertEqual(len(started), 1)
            a.close(); self.assertFalse(started[0].stopped)
            b.close(); self.assertTrue(started[0].stopped)  # the last one is closed -> Playwright is stopped
        finally:
            server.BROWSER_MAX = old_max
            for k, v in saved.items():
                if v is None: sys.modules.pop(k, None)
                else: sys.modules[k] = v
            server._PW_TLS.st = None

    def test_browser_slots_limit_and_release(self):
        old = server.BROWSER_MAX
        server.BROWSER_MAX = 1
        try:
            t1 = server._br_acquire()
            with self.assertRaises(RuntimeError):
                server._br_acquire(timeout=0.2)   # the second slot is taken
            server._br_release(t1)
            t2 = server._br_acquire(timeout=0.2)  # after release - allowed
            server._br_release(t2)
            t3 = server._br_acquire()
            server._BR_HELD[t3] -= 700           # a hung browser does not hold a slot
            t4 = server._br_acquire(timeout=0.2)
            server._br_release(t4); server._br_release(t3)
        finally:
            server.BROWSER_MAX = old
            server._BR_HELD.clear()

    def test_scroll_stops_when_page_grows_too_large(self):
        class P:
            n = 0
            def evaluate(self, *a, **k): P.n += 1
            class keyboard:
                @staticmethod
                def press(k): pass
            def wait_for_timeout(self, ms): pass
        class G:
            _page = P()
            def _content(self): return "x" * (1000 * (P.n + 1))   # grows with every scroll
        old = server.SCROLL_MAX_HTML
        server.SCROLL_MAX_HTML = 3500
        try:
            n = server._scroll_load(G(), lambda h: len(h) // 100, 10**9, max_rounds=50)
        finally:
            server.SCROLL_MAX_HTML = old
        self.assertLessEqual(P.n, 4)  # stopped by size instead of scrolling all 50 times

    def test_refresh_all_does_not_wait_for_hung_source(self):
        server.remove_source("tgtest")
        real, real_dl = server.refresh_source, server.REFRESH_DEADLINE
        stop = threading.Event()
        def slow(src):
            if src["id"] == "hinews":
                return real(src)
            stop.wait(30)                   # a "hung" source
        server.refresh_source, server.REFRESH_DEADLINE = slow, 1
        try:
            t0 = time.time()
            with FakeNet({"https://hi-news.ru/feed": '<rss><channel><item><title>Fresh</title><link>https://h/1</link><guid>g1</guid><pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate></item></channel></rss>'}):
                server.refresh_all()
            self.assertLess(time.time() - t0, 10)
            self.assertFalse(server.STATE["refreshing"])
        finally:
            stop.set()
            server.refresh_source, server.REFRESH_DEADLINE = real, real_dl
        with server.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items WHERE title='Fresh'").fetchone()[0], 1)

    def test_refresh_all_finishes_when_fast_sources_done_slow_continue(self):
        server.remove_source("tgtest")
        server.add_source(dict(id="slowsite", name="S", type="site", url="https://slow.example/news", config={}))
        real = server.refresh_source
        stop = threading.Event()
        def fake(src):
            if src["type"] == "site":
                stop.wait(20)
            else:
                real(src)
        server.refresh_source = fake
        try:
            t0 = time.time()
            with FakeNet({"https://hi-news.ru/feed": '<rss><channel><item><title>Quick</title><link>https://h/9</link><guid>g9</guid><pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate></item></channel></rss>'}):
                server.refresh_all()
            self.assertLess(time.time() - t0, 8)          # we do not wait for the slow source
            self.assertEqual(server.STATE["slow"], 1)
        finally:
            stop.set()
            server.refresh_source = real

    def test_slow_sources_not_refreshed_too_often_unless_forced(self):
        server.remove_source("tgtest")
        server.add_source(dict(id="slowsite", name="S", type="site", url="https://slow.example/news", config={}))
        server.set_status("slowsite", True, "ok")  # was just checked
        ran = []
        real = server.refresh_source
        server.refresh_source = lambda src: ran.append(src["id"])
        try:
            server.refresh_all()
            self.assertNotIn("slowsite", ran)
            time.sleep(0.3)
            server.refresh_all(force=True)
            time.sleep(0.5)
            self.assertIn("slowsite", ran)
        finally:
            server.refresh_source = real

    def test_image_cache_is_trimmed_and_browser_temp_cleaned(self):
        import tempfile as tf
        d = tf.mkdtemp()
        old_dir = server.IMG_DIR
        server.IMG_DIR = d
        try:
            for i in range(10):
                p = os.path.join(d, f"f{i}")
                with open(p, "wb") as f:
                    f.write(b"x" * 100_000)
                os.utime(p, (1000 + i, 1000 + i))
            removed = server.trim_image_cache(max_mb=0.5)   # limit ~512 KB with 1 MB in the cache
            left = sorted(os.listdir(d))
            self.assertGreater(removed, 0)
            self.assertIn("f9", left)       # the fresh ones stayed
            self.assertNotIn("f0", left)    # the oldest were removed
        finally:
            server.IMG_DIR = old_dir
        tmp = tf.mkdtemp()
        real_tmp = tf.gettempdir
        tf.gettempdir = lambda: tmp
        try:
            stale, fresh = os.path.join(tmp, "playwright_chromiumdev_profile-abc"), os.path.join(tmp, "playwright_chromiumdev_profile-new")
            other = os.path.join(tmp, "my_project")
            for p in (stale, fresh, other):
                os.makedirs(p)
            os.utime(stale, (1000, 1000))
            server._BR_HELD.clear()
            self.assertEqual(server.cleanup_browser_temp(max_age=3600), 1)
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(fresh) and os.path.exists(other))  # foreign and fresh ones are left alone
        finally:
            tf.gettempdir = real_tmp

    def test_backfill_errors_cool_down_and_retry(self):
        server.remove_source("tgtest")
        server.BF["errors"]["hinews"] = 5
        server.BF["err_at"]["hinews"] = time.time()
        with FakeNet({}):
            self.assertFalse(server.backfill_pass())  # pause
            server.BF["err_at"]["hinews"] = time.time() - 700
            self.assertTrue(server.backfill_pass())  # the pause is over -> a new attempt

    def test_ok_archive_scrolls_group_page_in_browser(self):
        server.remove_source("tgtest"); server.remove_source("hinews")
        server.add_source([d for d in server.DEFAULT_SOURCES if d["id"] == "kinopoisk"][0])
        topics = lambda ids: "".join(f'<div><a href="/kinopoisk/topic/{i}">Post {i}</a><div>{i % 20 + 1} фев</div></div>' for i in ids)
        class FakePage:
            def __init__(self): self.scrolls = 0
            def evaluate(self, *a, **k): self.scrolls += 1
            class keyboard:
                @staticmethod
                def press(k): pass
            def wait_for_timeout(self, ms): pass
        class FakeGetter:
            t0 = 0; budget = 300; browser_mode = True
            def __init__(self, *a, **k): self._page = FakePage()
            def enable_browser(self, light=False): pass
            def _goto(self, url): pass
            def _content(self):  # every scroll loads 5 more posts, 12 in total
                return topics(range(100, max(100 - 5 - 5 * self._page.scrolls, 88), -1))
            def text(self, url, **k): raise urllib.error.HTTPError(url, 404, "x", {}, None)
            def close(self): pass
        real = server.PageGetter
        server.PageGetter = FakeGetter
        real_spec = __import__("importlib.util").util.find_spec
        try:
            for _ in range(6):
                if not server.backfill_pass():
                    break
        finally:
            server.PageGetter = real
            server.OK_SCROLL.clear()
        with server.db() as con:
            n = con.execute("SELECT COUNT(*) FROM items WHERE source='kinopoisk'").fetchone()[0]
        self.assertGreaterEqual(n, 10)  # old posts were loaded by scrolling
        self.assertEqual(server.bf_get("kinopoisk")["done"], 1)

    def test_until_prioritises_sources_that_do_not_reach_date(self):
        old = int(time.time()) - 400 * 86400
        # tgtest already has old items (back to the needed date), hinews only fresh ones
        server.save_items("tgtest", [dict(guid="newschan/10", title="a", text="", url="https://t.me/newschan/10", ext_url=None, image=None, published=old)])
        server.save_items("hinews", [dict(guid="g1", title="b", text="", url="https://h/1", ext_url=None, image=None, published=int(time.time()))])
        server.BF["until"] = int(time.time()) - 30 * 86400
        pages = {"https://hi-news.ru/feed?paged=2": '<rss><channel><item><title>old</title><link>https://h/2</link><guid>g2</guid><pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate></item></channel></rss>'}
        net = FakeNet(pages)
        with net:
            self.assertTrue(server.backfill_pass())
        self.assertEqual([c for c in net.calls if "hi-news.ru" in c], ["https://hi-news.ru/feed", "https://hi-news.ru/feed?paged=2"])  # tgtest was not touched: it already covers the date
        self.assertEqual(server.oldest("hinews"), server.parse_date("Mon, 01 Jan 2024 10:00:00 +0000"))
        with FakeNet(pages):
            self.assertFalse(server.backfill_pass())  # hinews reached the date -> nothing urgent


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_db()
        day, base = 86400, 1792087207
        server.save_items("hinews", [
            dict(guid="a", title="Кошки и ЁЛКИ", text="Про новогоднюю ёлку", url="https://h/a", ext_url=None, image="https://i/a.jpg", published=base),
            dict(guid="b", title="Old story", text="Something else entirely", url="https://h/b", ext_url=None, image=None, published=base - 3 * day)])
        server.save_items("tds", [
            dict(guid="c", title="Machine Learning basics", text="100% accuracy_test", url="https://t/c", ext_url=None, image=None, published=base - day)])
        cls.srv = server.make_server("127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, method="GET", body=None):
        path = urllib.parse.quote(path, safe="/?&=%+:,-_.~")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=data)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode()

    def titles(self, qs):
        return [i["title"] for i in self.call("/api/items?" + qs)[1]["items"]]

    def test_order_newest_first(self):
        self.assertEqual(self.titles(""), ["Кошки и ЁЛКИ", "Machine Learning basics", "Old story"])

    def test_search_cyrillic_case_and_yo(self):
        self.assertEqual(self.titles("q=кошки"), ["Кошки и ЁЛКИ"])
        self.assertEqual(self.titles("q=елки"), ["Кошки и ЁЛКИ"])

    def test_search_phrase_and_words(self):
        self.assertEqual(self.titles("q=%22machine+learning%22"), ["Machine Learning basics"])
        self.assertEqual(self.titles("q=machine+basics"), ["Machine Learning basics"])
        self.assertEqual(self.titles("q=machine+кошки"), [])

    def test_search_wildcards_escaped(self):
        self.assertEqual(self.titles("q=100%25"), ["Machine Learning basics"])
        self.assertEqual(self.titles("q=accuracy_tesx"), [])

    def test_date_filter_every_day_no_gaps(self):
        self.assertEqual(self.titles("from=2026-10-15&to=2026-10-15"), ["Кошки и ЁЛКИ"])
        self.assertEqual(self.titles("from=2026-10-14&to=2026-10-14"), ["Machine Learning basics"])
        self.assertEqual(self.titles("from=2026-10-13&to=2026-10-13"), [])
        self.assertEqual(self.titles("from=2026-10-12&to=2026-10-12"), ["Old story"])
        # the union of one-day selections = the whole feed, nothing lost or duplicated
        days = [t for d in range(10, 17) for t in self.titles(f"from=2026-10-{d}&to=2026-10-{d}")]
        self.assertEqual(sorted(days), sorted(self.titles("")))
        self.assertEqual(self.call("/api/items?from=15.10.2026")[0], 400)

    def test_only_one_source_and_item_has_source_name(self):
        r = self.call("/api/items?sources=tds")[1]["items"]
        self.assertEqual([(i["title"], i["source_name"]) for i in r], [("Machine Learning basics", "Towards Data Science")])

    def test_bookmarks_roundtrip(self):
        iid = self.call("/api/items?q=кошки")[1]["items"][0]["id"]
        self.assertEqual(self.call(f"/api/bookmarks/{iid}", "POST")[0], 200)
        marked = self.call("/api/items?bookmarked=1")[1]
        self.assertEqual([i["title"] for i in marked["items"]], ["Кошки и ЁЛКИ"])
        self.assertEqual(self.call(f"/api/bookmarks/{iid}", "DELETE")[0], 200)
        self.assertEqual(self.call("/api/items?bookmarked=1")[1]["total"], 0)
        self.assertEqual(self.call("/api/bookmarks/nope", "POST")[0], 404)

    def test_sources_info_has_coverage_and_images(self):
        info = {s["id"]: s for s in self.call("/api/sources")[1]["sources"]}
        self.assertEqual(info["hinews"]["count"], 2)
        self.assertEqual(info["hinews"]["with_image"], 1)
        self.assertEqual(info["hinews"]["oldest"], 1792087207 - 3 * 86400)
        self.assertNotIn("genericrss", info)
        self.assertEqual(info["rozetked"]["type"], "telegram")

    def test_multi_link_source_labels_and_edit(self):
        post = lambda ch, i: (f'<div class="tgme_widget_message" data-post="{ch}/{i}"><div class="tgme_widget_message_text js-message_text">Post {ch}</div>'
                              '<time datetime="2026-10-15T10:00:00+00:00"></time></div>')
        pages = {}
        for ch, i in (("chanaaa", 5), ("chanbbb", 6)):
            pages[f"https://t.me/s/{ch}"] = post(ch, i)
            pages[f"https://t.me/s/{ch}?before={i}"] = "<html></html>"
        with FakeNet(pages):
            st, body = self.call("/api/sources", "POST", {"name": "My mix", "links": [
                {"u": "https://t.me/chanaaa", "label": "Main"}, {"u": "t.me/chanbbb", "label": "Second"}]})
            self.assertEqual(st, 200)
            sid = body["source"]["id"]
            self.assertEqual([l["u"] for l in body["source"]["links"]], ["https://t.me/chanaaa", "https://t.me/chanbbb"])
            by = {i["title"]: i["source_name"] for i in self.call("/api/items?sources=" + sid)[1]["items"]}
            self.assertEqual(sorted(by.values()), ["Main", "Second"])
            self.assertEqual(len([s for s in self.call("/api/sources")[1]["sources"] if s["id"] == sid]), 1)  # one source in the sidebar
            # edit: only the name and labels (same links) - no rebuild
            st, body = self.call("/api/sources/" + sid, "PUT", {"name": "New name", "links": [
                {"u": "https://t.me/chanaaa", "label": "A"}, {"u": "https://t.me/chanbbb", "label": "B"}]})
            self.assertEqual((st, body["source"]["name"]), (200, "New name"))
            self.assertEqual(sorted(i["source_name"] for i in self.call("/api/items?sources=" + sid)[1]["items"]), ["A", "B"])
        self.call("/api/sources/" + sid, "DELETE")  # the class-wide database: leave no traces
        st, body = self.call("/api/sources", "POST", {"links": [{"u": "https://t.me/chanaaa"}, {"u": "https://example.org/feed"}]})
        self.assertEqual(st, 400)

    def test_add_and_remove_source_by_link(self):
        net = FakeNet({"https://example-news.org/feed": KDN})
        with net:
            st, body = self.call("/api/sources", "POST", {"url": "https://example-news.org/feed"})
        self.assertEqual(st, 200)
        sid = body["source"]["id"]
        self.assertTrue(body["added"])
        self.assertEqual(body["source"]["count"], 1)
        self.assertEqual(self.titles("sources=" + sid), ["7 Open-Source AI Tools That Can Replace Paid Subscriptions"])
        # removed: gone from the feed and from the source list
        self.assertEqual(self.call("/api/sources/" + sid, "DELETE")[0], 200)
        self.assertNotIn("7 Open-Source", " ".join(self.titles("")))
        self.assertNotIn(sid, [s["id"] for s in self.call("/api/sources")[1]["sources"]])
        # but a bookmark on its item stays available
        # (the bookmark case is covered by another test - here we check re-adding)
        with FakeNet({"https://example-news.org/feed": KDN}):
            st, body = self.call("/api/sources", "POST", {"url": "https://example-news.org/feed"})
        self.assertTrue(body["added"])
        self.assertEqual(self.titles("sources=" + sid)[0][:6], "7 Open")  # the data came back
        self.call("/api/sources/" + sid, "DELETE")
        self.assertEqual(self.call("/api/sources/" + sid, "DELETE")[0], 404)

    def test_bookmark_survives_source_removal(self):
        with FakeNet({"https://keep-news.org/feed": TDS}):
            sid = self.call("/api/sources", "POST", {"url": "https://keep-news.org/feed"})[1]["source"]["id"]
        iid = self.call("/api/items?sources=" + sid)[1]["items"][0]["id"]
        self.call(f"/api/bookmarks/{iid}", "POST")
        self.call("/api/sources/" + sid, "DELETE")
        self.assertEqual(self.call("/api/items?sources=" + sid)[1]["total"], 0)
        marked = self.call("/api/items?bookmarked=1")[1]["items"]
        self.assertEqual([i["title"] for i in marked], ["Does Stacking Beat Your Best Model?"])
        self.call(f"/api/bookmarks/{iid}", "DELETE")

    def test_add_bad_link_gives_error(self):
        st, body = self.call("/api/sources", "POST", {"url": "https://vk.com/feed"})
        self.assertEqual(st, 400)
        self.assertIn("error", body)

    def test_backfill_until_endpoint(self):
        st, body = self.call("/api/backfill?until=2026-10-01&tz=-180", "POST")
        self.assertEqual(st, 200)
        self.assertEqual(body["until"], server.day_bounds("2026-10-01", -180)[0])
        server.BF["until"] = None

    def test_index_served_and_static_safe(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            self.assertIn("My feed", r.read().decode())
        self.assertEqual(self.call("/static/../server.py")[0], 404)


class ImageProxy(unittest.TestCase):
    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")

    @classmethod
    def setUpClass(cls):
        fresh_db()
        png, seen = cls.PNG, []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                if self.path == "/pic.png":
                    self.send_response(200); self.send_header("Content-Type", "image/png"); self.end_headers(); self.wfile.write(png)
                else:
                    self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(b"<html>")

            def log_message(self, *a):
                pass
        cls.img_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.seen = seen
        threading.Thread(target=cls.img_srv.serve_forever, daemon=True).start()
        server.IMG_DIR = tempfile.mkdtemp()
        cls.srv = server.make_server("127.0.0.1", 0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.img_srv.shutdown(); cls.srv.shutdown()

    def get(self, url):
        q = urllib.parse.quote(url, safe="")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.srv.server_address[1]}/img?u={q}") as r:
                return r.status, r.headers["Content-Type"], r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers["Content-Type"], e.read()

    def test_private_addresses_are_refused(self):
        st, _, body = self.get(f"http://127.0.0.1:{self.img_srv.server_address[1]}/pic.png")
        self.assertEqual(st, 502)
        self.assertIn("invalid image address", body.decode())
        self.assertEqual(self.get("file:///etc/passwd")[0], 502)

    def test_proxy_sends_referer_and_caches(self):
        real = server.is_public_host
        server.is_public_host = lambda h: True
        try:
            url = f"http://127.0.0.1:{self.img_srv.server_address[1]}/pic.png"
            st, ctype, body = self.get(url)
            self.assertEqual((st, ctype, body), (200, "image/png", self.PNG))
            self.assertEqual(self.seen[-1]["Referer"], f"http://127.0.0.1:{self.img_srv.server_address[1]}/")
            n = len(self.seen)
            self.assertEqual(self.get(url)[0], 200)
            self.assertEqual(len(self.seen), n)  # the second response comes from the cache
            st, _, body = self.get(f"http://127.0.0.1:{self.img_srv.server_address[1]}/page.html")
            self.assertEqual(st, 502)
            self.assertIn("not an image", body.decode())
        finally:
            server.is_public_host = real


if __name__ == "__main__":
    unittest.main()
