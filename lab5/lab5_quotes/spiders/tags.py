import json
import os
import re
import time
from urllib.parse import urlparse

import scrapy
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.internet.error import DNSLookupError, TCPTimedOutError, TimeoutError

from lab5_quotes.items import AuthorItem, QuoteItem


class TagsSpider(scrapy.Spider):
    """Вариант 4: обход quotes.toscrape.com по тегам.

    Уровень 1 — главная: облако топ-тегов (div.tags-box) и пагинация главной
                (/page/1..10/), с которой собираются ссылки на ВСЕ теги цитат.
    Уровень 2 — страницы тега /tag/<tag>/page/N/ с пагинацией: цитаты тега.
    Карточка автора /author/<slug>/ запрашивается один раз на автора
    (повторы отсекает штатный dupefilter Scrapy).

    Состояние в памяти паука не хранится: цитата и автор — разные Item,
    связываются в БД по author_url. Поэтому прогон корректно возобновляется
    через JOBDIR: всё, что нужно для продолжения, лежит в очереди запросов.
    """

    name = "tags"
    base_url = "https://quotes.toscrape.com/"

    def __init__(self, base_url=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if base_url:  # например, -a base_url=http://127.0.0.1:8800/ для локальной копии
            self.base_url = base_url.rstrip("/") + "/"
        self.allowed_domains = [urlparse(self.base_url).hostname]
        self.start_urls = [self.base_url]
        self._t0 = time.time()

    # ---------- уровень 1: главная и её пагинация ----------
    def parse(self, response, list_page=1):
        self.crawler.stats.inc_value("pages/list")

        # облако топ-10 тегов есть на каждой странице главной — берём его
        for a in response.css("div.tags-box span.tag-item a.tag"):
            yield from self._follow_tag(response, a, origin="top10")

        # теги, встречающиеся в цитатах этой страницы списка
        for a in response.css("div.quote div.tags a.tag"):
            yield from self._follow_tag(response, a, origin="list")

        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield response.follow(
                next_page,
                callback=self.parse,
                errback=self.errback,
                cb_kwargs={"list_page": list_page + 1},
            )

    def _follow_tag(self, response, a, origin):
        tag = (a.css("::text").get() or "").strip().lower()
        href = a.attrib.get("href")
        if not tag or not href:
            self.crawler.stats.inc_value("parse/tag_link_incomplete")
            return
        # /tag/love/ и /tag/love/page/1/ — одна и та же страница; приводим
        # к одному виду, чтобы dupefilter Scrapy отсёк повтор.
        url = response.urljoin(href)
        if not re.search(r"/page/\d+/?$", url):
            url = url.rstrip("/") + "/page/1/"
        self.crawler.stats.inc_value(f"tags/found_via_{origin}")
        yield scrapy.Request(
            url,
            callback=self.parse_tag,
            errback=self.errback,
            cb_kwargs={"tag": tag, "page": 1},
        )

    # ---------- уровень 2: страницы тега ----------
    def parse_tag(self, response, tag, page):
        self.crawler.stats.inc_value("pages/tag")
        for q in response.css("div.quote"):
            quote = q.css("span.text::text").get()
            author = q.css("small.author::text").get()
            author_href = q.css("small.author + a::attr(href)").get()

            # отсутствие author_href не роняет разбор: запись уйдёт в пайплайн
            # и будет отброшена валидацией со счётчиком
            author_url = self._author_url(response, author_href) if author_href else None

            yield QuoteItem(
                quote=quote,
                author=author,
                tags=q.css("div.tags a.tag::text").getall(),
                source_tag=tag,
                tag_page=page,
                author_url=author_url,
            )

            if author_url:
                # одинаковые URL автора отсекает dupefilter → 1 запрос на автора
                yield scrapy.Request(
                    author_url,
                    callback=self.parse_author,
                    errback=self.errback,
                    cb_kwargs={"author_url": author_url},
                )

        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield response.follow(
                next_page,
                callback=self.parse_tag,
                errback=self.errback,
                cb_kwargs={"tag": tag, "page": page + 1},
            )

    @staticmethod
    def _author_url(response, href):
        # на сайте ссылка /author/Albert-Einstein без «/», а маршрут —
        # /author/<slug>/: без нормализации каждый запрос получает 308-редирект
        return response.urljoin(href).rstrip("/") + "/"

    # ---------- карточка автора ----------
    def parse_author(self, response, author_url):
        self.crawler.stats.inc_value("pages/author")
        yield AuthorItem(
            author=response.css("h3.author-title::text").get(),
            author_url=author_url,
            author_born_date=response.css("span.author-born-date::text").get(),
            author_born_place=response.css("span.author-born-location::text").get(),
            author_bio=" ".join(response.css("div.author-description::text").getall()),
        )

    # ---------- ошибки ----------
    def errback(self, failure):
        stats = self.crawler.stats
        request = failure.request
        stats.inc_value("errback/total")
        if failure.check(HttpError):
            status = failure.value.response.status
            stats.inc_value(f"errback/http_{status}")
            self.logger.warning("HTTP %s: %s", status, request.url)
        elif failure.check(DNSLookupError):
            stats.inc_value("errback/dns")
            self.logger.error("DNS error: %s", request.url)
        elif failure.check(TimeoutError, TCPTimedOutError):
            stats.inc_value("errback/timeout")
            self.logger.error("Timeout: %s", request.url)
        else:
            stats.inc_value(f"errback/{failure.type.__name__}")
            self.logger.error("%s: %s", failure.type.__name__, request.url)

    # ---------- статистика прогона ----------
    def closed(self, reason):
        os.makedirs("data", exist_ok=True)
        nums = [
            int(m.group(1))
            for f in os.listdir("data")
            if (m := re.fullmatch(r"stats_(\d+)\.json", f))
        ]
        run_no = max(nums, default=0) + 1

        stats = dict(self.crawler.stats.get_stats())
        stats["run_number"] = run_no
        stats["close_reason"] = reason
        stats["elapsed_s"] = round(time.time() - self._t0, 1)
        stats["base_url"] = self.base_url
        s = self.crawler.settings
        stats["settings_snapshot"] = {
            k: s.get(k)
            for k in (
                "CLOSESPIDER_ITEMCOUNT", "CLOSESPIDER_PAGECOUNT", "CLOSESPIDER_TIMEOUT",
                "CLOSESPIDER_ERRORCOUNT", "DEPTH_LIMIT", "DOWNLOAD_DELAY",
                "AUTOTHROTTLE_ENABLED", "CONCURRENT_REQUESTS_PER_DOMAIN",
                "HTTPCACHE_ENABLED", "JOBDIR",
            )
        }
        fname = f"data/stats_{run_no}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2, default=str)
        self.logger.info("Stats saved to %s", fname)
