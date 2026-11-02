"""Доработанный сборщик (вариант 4 — браузерный сбор, Playwright) на базе сборщика цитат
quotes.toscrape.com из ЛР4: страницы /js/page/N/ рендерятся в браузере, карточки авторов
забираются HTTP-запросами.

Каждый сетевой запрос проходит один путь:
robots.txt → стоп-лист доменов (детектор) → предохранитель → ограничитель скорости →
запрос (условные заголовки, gzip) → классификация → повтор/отказ → детектор содержимого → журнал.
"""
import gzip
import hashlib
import json
import re
import time
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import detector
from breaker import DomainBreaker
from limiter import DomainLimiter
from retry import CLASSES, RetryPolicy, classify, should_retry

STATE = Path("state")


def site_slug(name: str) -> str:
    """Как на quotes.toscrape.com: «J.K. Rowling» → J-K-Rowling, «García» → Garcia."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class Resp:
    url: str
    status: int | None = None
    text: str = ""
    headers: dict = field(default_factory=dict)
    error_class: str | None = None
    verdict: str = "ok"
    from_cache: bool = False
    skipped: str | None = None          # breaker / stopped / robots / budget

    @property
    def ok(self):
        return self.text != "" and self.verdict != detector.BLOCK and self.error_class in ("ok", "not_modified")


class Pipeline:
    def __init__(self, run_id: str, profile: dict, cfg: dict, log, robots: dict | None = None):
        self.run_id, self.p, self.cfg, self.log = run_id, profile, cfg, log
        self.robots = robots or {}
        lim = cfg["limiter"]
        self.limiter = DomainLimiter(lim["rate"], lim["burst"], tuple(lim["jitter"]), cfg["adaptive"],
                                     {k: dict(v) for k, v in (lim.get("per_domain") or {}).items()})
        for host, info in self.robots.items():
            if info.get("crawl_delay"):
                self.limiter.set_crawl_delay(host, info["crawl_delay"])
        self.breaker = DomainBreaker(cfg["breaker"]["threshold"], cfg["breaker"]["cooldown"],
                                     on_change=self._breaker_changed)
        self.policy = RetryPolicy(**cfg["retry"])
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg["user_agent"]
        self.timeout = cfg["http_timeout"]
        self.etags = _load(STATE / "etags.json", {}) if self.p["conditional"] else {}
        self.stopped: set[str] = set()
        self.tokens = {}                     # host → токен (сценарий 401)
        self.token_refresh = {}              # host → функция получения нового токена

    # ---------- служебное ----------
    def _breaker_changed(self, host, old, new, info):
        self.log.log("breaker_state", host=host, old=old, new=new, **info)

    def _gate(self, url) -> str | None:
        """Можно ли сейчас отправить запрос; иначе причина пропуска."""
        host = urlparse(url).netloc
        rb = self.robots.get(host)
        if rb and not rb["can_fetch"].get(urlparse(url).path, True):
            self.log.log("robots_skip", host=host, url=url)
            return "robots"
        if host in self.stopped:
            self.log.log("stopped_skip", host=host, url=url, error="домен остановлен детектором")
            return "stopped"
        if self.p["breaker"] and not self.breaker.allow(host):
            self.log.log("breaker_skip", host=host, url=url,
                         remaining_s=round(self.breaker.remaining_cooldown(host), 1))
            return "breaker"
        return None

    def _throttle(self, host) -> dict:
        return self.limiter.wait(host) if self.p["limiter"] else {"wait_s": 0.0}

    def _cache_path(self, url):
        return STATE / "http_cache" / hashlib.sha256(url.encode()).hexdigest()[:20]

    def _block(self, url, status, headers, body, reasons, screenshot=None):
        host = urlparse(url).netloc
        path = detector.save_incident(self.run_id, url, status, headers, body, detector.BLOCK, reasons, screenshot)
        self.stopped.add(host)
        self.log.log("incident", host=host, url=url, status=status, verdict=detector.BLOCK,
                     reasons=reasons, artifacts=path, error="сбор домена остановлен")

    def _after_failure(self, url, attempt, cls, retry_after, t_start, max_attempts) -> float | None:
        """Решение о повторе: задержка или None (сдаёмся)."""
        host = urlparse(url).netloc
        if cls in ("server_error", "timeout", "connection", "rate_limited") and self.p["breaker"]:
            self.breaker.record_failure(host)
        if not self.p["retry"] or not should_retry(cls) or attempt >= max_attempts:
            reason = ("повторы выключены" if not self.p["retry"] else
                      "класс без повторов: " + CLASSES[cls][1] if not should_retry(cls) else "исчерпаны попытки")
            self.log.log("give_up", host=host, url=url, attempt=attempt, error_class=cls, error=reason)
            return None
        delay, source = self.policy.next_delay(attempt, retry_after)
        if time.monotonic() - t_start + delay > self.policy.url_budget:
            self.log.log("give_up", host=host, url=url, attempt=attempt, error_class=cls,
                         error=f"бюджет времени на URL {self.policy.url_budget} с исчерпан")
            return None
        self.log.log("retry", host=host, url=url, attempt=attempt + 1, delay_s=round(delay, 3),
                     delay_source=source, error_class=cls, retry_after=retry_after)
        return delay

    # ---------- HTTP ----------
    def http(self, url: str, expected: str | None = None, kind: str = "html", min_size: int | None = None) -> Resp:
        host = urlparse(url).netloc
        t_start = time.monotonic()
        attempt, refreshed = 0, False
        while True:
            attempt += 1
            skip = self._gate(url)
            if skip:
                return Resp(url, skipped=skip)
            wait = self._throttle(host)
            headers = {"Accept-Encoding": "gzip" if self.p["gzip"] else "identity"}
            saved = self.etags.get(url) if self.p["conditional"] else None
            if saved:
                if saved.get("etag"):
                    headers["If-None-Match"] = saved["etag"]
                if saved.get("last_modified"):
                    headers["If-Modified-Since"] = saved["last_modified"]
            if host in self.tokens:
                headers["X-Token"] = self.tokens[host]

            t0 = time.monotonic()
            status, raw, exc, rh = None, b"", None, {}
            try:
                r = self.session.get(url, headers=headers, timeout=self.timeout, stream=True)
                raw = r.raw.read(decode_content=False)          # байты «по проводу»
                # r.headers — словарь без учёта регистра: httpbin.org отдаёт «etag», а не «ETag»
                status, rh = r.status_code, r.headers
            except requests.RequestException as e:
                exc = e
            ms = round((time.monotonic() - t0) * 1000)
            enc = rh.get("Content-Encoding", "")
            try:
                body_bytes = gzip.decompress(raw) if enc == "gzip" else zlib.decompress(raw) if enc == "deflate" else raw
            except OSError:
                body_bytes = raw
            body = body_bytes.decode("utf-8", errors="replace")
            cls = classify(status, exc)
            self.log.log("request", host=host, url=url, status=status, ms=ms, attempt=attempt, bytes=len(raw),
                         bytes_decoded=len(body_bytes), content_encoding=enc or None, error_class=cls,
                         error=str(exc) if exc else None, via="http", conditional=bool(saved), **wait)
            if self.p["limiter"]:
                self.limiter.observe(host, ms / 1000, status)

            if cls == "not_modified":
                cached = self._cache_path(url)
                text = cached.read_text(encoding="utf-8") if cached.exists() else ""
                if self.p["breaker"]:
                    self.breaker.record_success(host)
                return Resp(url, 304, text, rh, cls, from_cache=True)

            if cls == "ok":
                verdict = "ok"
                if self.p["detector"]:
                    verdict, reasons = detector.check(status, body, expected,
                                                      self.cfg["detector"]["min_size"] if min_size is None else min_size, kind)
                    self.log.log("detector", host=host, url=url, status=status, verdict=verdict, reasons=reasons)
                    if verdict == detector.BLOCK:
                        self._block(url, status, rh, body, reasons)
                        return Resp(url, status, body, rh, "content_block", verdict)
                if self.p["breaker"]:
                    self.breaker.record_success(host)
                if self.p["conditional"] and (rh.get("ETag") or rh.get("Last-Modified")):
                    self.etags[url] = {"etag": rh.get("ETag"), "last_modified": rh.get("Last-Modified")}
                    self._cache_path(url).parent.mkdir(parents=True, exist_ok=True)
                    self._cache_path(url).write_text(body, encoding="utf-8")
                return Resp(url, status, body, rh, cls, verdict)

            # ---- отказ ----
            if cls == "possible_block":
                if self.p["detector"]:
                    _, reasons = detector.check(status, body, expected)
                    self._block(url, status, rh, body, reasons)
                else:
                    self.log.log("give_up", host=host, url=url, attempt=attempt, error_class=cls, error="403")
                return Resp(url, status, body, rh, cls, detector.BLOCK)
            if cls == "auth_expired" and not refreshed and host in self.token_refresh:
                self.tokens[host] = self.token_refresh[host]()
                refreshed = True
                self.log.log("auth_refresh", host=host, url=url, status=401, attempt=attempt,
                             error="токен истёк — получен новый, один повтор без задержки")
                continue
            delay = self._after_failure(url, attempt, cls, rh.get("Retry-After"), t_start,
                                        self.policy.max_attempts)
            if delay is None:
                return Resp(url, status, body, rh, cls)
            time.sleep(delay)

    def save_state(self):
        if self.p["conditional"]:
            _save(STATE / "etags.json", self.etags)


class Browser:
    """Playwright: загрузка страницы и ожидание селектора с теми же лимитами, повторами и предохранителем."""

    def __init__(self, pipe: Pipeline, cfg: dict, block_assets: bool, fail_launch: bool = False):
        self.pipe, self.cfg = pipe, cfg
        self.block_assets = block_assets
        self.available = False
        self.launch_error = None
        self._pw = self._browser = self.page = None
        self._bytes = 0
        try:
            if fail_launch:
                raise RuntimeError("браузер недоступен (симуляция: Chromium не установлен на сервере)")
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = None
            # сначала установленный Google Chrome (channel="chrome"), затем Chromium от Playwright
            for channel in [c for c in (cfg.get("channel"), None) if c is not False]:
                try:
                    kw = {"headless": cfg["headless"]}
                    if channel:
                        kw["channel"] = channel
                    self._browser = self._pw.chromium.launch(**kw)
                    pipe.log.log("browser_launch", channel=channel or "playwright-chromium",
                                 version=self._browser.version)
                    break
                except Exception as e:  # noqa: BLE001
                    pipe.log.log("browser_launch_failed", channel=channel or "playwright-chromium",
                                 error=str(e).splitlines()[0])
            if self._browser is None:
                raise RuntimeError("не удалось запустить ни Google Chrome, ни Chromium от Playwright")
            ctx = self._browser.new_context(user_agent=pipe.cfg["user_agent"])
            self.page = ctx.new_page()
            if block_assets:
                # не грузим картинки, шрифты и медиа. Через CDP, а не page.route: route отключает
                # HTTP-кэш браузера, и трафик вырос бы за счёт повторной загрузки скриптов
                cdp = ctx.new_cdp_session(self.page)
                cdp.send("Network.enable")
                cdp.send("Network.setBlockedURLs", {"urls": [
                    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico",
                    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.mp4", "*.webm"]})
            self.page.on("requestfinished", self._count)
            self.available = True
        except Exception as e:  # noqa: BLE001
            self.launch_error = str(e)
            pipe.log.log("browser_unavailable", error=self.launch_error)

    def _count(self, req):
        try:
            s = req.sizes()
            self._bytes += s.get("responseBodySize", 0) + s.get("responseHeadersSize", 0)
        except Exception:  # noqa: BLE001
            pass

    def load(self, url: str, selector: str, selector_timeout_ms: int | None = None) -> Resp:
        from playwright.sync_api import TimeoutError as PWTimeout
        pipe = self.pipe
        host = urlparse(url).netloc
        if not self.available:
            pipe.log.log("browser_skip", host=host, url=url, error=f"браузер недоступен: {self.launch_error}")
            return Resp(url, skipped="browser_unavailable")
        t_start = time.monotonic()
        attempt = 0
        sel_timeout = selector_timeout_ms or self.cfg["selector_timeout_ms"]
        while True:
            attempt += 1
            skip = pipe._gate(url)
            if skip:
                return Resp(url, skipped=skip)
            wait = pipe._throttle(host)
            self._bytes = 0
            t0 = time.monotonic()
            status, html, exc = None, "", None
            try:
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=self.cfg["nav_timeout_ms"])
                status = resp.status if resp else None
                if status and status < 400:
                    self.page.wait_for_selector(selector, timeout=sel_timeout)
                html = self.page.content()
            except PWTimeout as e:
                exc = TimeoutError(str(e).splitlines()[0])
                # селектор не дождались — сначала проверяем, не капча ли это / заглушка
                try:
                    html = self.page.content()
                except Exception:  # noqa: BLE001
                    html = ""
                if pipe.p["detector"] and status and status < 400:
                    verdict, reasons = detector.check(status, html, selector.lstrip("."),
                                                      pipe.cfg["detector"]["min_size"])
                    if verdict == detector.BLOCK:
                        ms = round((time.monotonic() - t0) * 1000)
                        pipe.log.log("request", host=host, url=url, status=status, ms=ms, attempt=attempt,
                                     bytes=self._bytes, error_class="content_block", error=str(exc),
                                     via="playwright", selector=selector, **wait)
                        pipe.log.log("detector", host=host, url=url, status=status, verdict=verdict, reasons=reasons)
                        pipe._block(url, status, {}, html, reasons, self.page.screenshot(full_page=True))
                        return Resp(url, status, html, {}, "content_block", verdict)
            except Exception as e:  # noqa: BLE001
                exc = ConnectionError(str(e).splitlines()[0])
            ms = round((time.monotonic() - t0) * 1000)
            cls = classify(status, exc) if exc else classify(status)
            pipe.log.log("request", host=host, url=url, status=status, ms=ms, attempt=attempt, bytes=self._bytes,
                         error_class=cls, error=str(exc) if exc else None, via="playwright",
                         selector=selector, **wait)
            if pipe.p["limiter"]:
                pipe.limiter.observe(host, ms / 1000, status)
            if cls == "ok":
                verdict = "ok"
                if pipe.p["detector"]:
                    verdict, reasons = detector.check(status, html, selector.lstrip("."),
                                                      pipe.cfg["detector"]["min_size"])
                    pipe.log.log("detector", host=host, url=url, status=status, verdict=verdict, reasons=reasons)
                    if verdict == detector.BLOCK:
                        pipe._block(url, status, {}, html, reasons, self.page.screenshot(full_page=True))
                        return Resp(url, status, html, {}, "content_block", verdict)
                if pipe.p["breaker"]:
                    pipe.breaker.record_success(host)
                return Resp(url, status, html, {}, cls, verdict)
            delay = pipe._after_failure(url, attempt, cls, None, t_start, self.cfg["max_attempts"])
            if delay is None:
                if exc and pipe.p["detector"]:     # артефакт для разбора таймаута
                    shot = self.page.screenshot(full_page=True)
                    path = detector.save_incident(pipe.run_id, url, status, {}, self.page.content(),
                                                  detector.SUSPECT, [str(exc)], shot)
                    pipe.log.log("incident", host=host, url=url, verdict=detector.SUSPECT,
                                 reasons=[str(exc)], artifacts=path, error="таймаут селектора")
                return Resp(url, status, html, {}, cls)
            time.sleep(delay)

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


# ======================================================================
# Сборщик цитат
# ======================================================================
class QuotesCollector:
    def __init__(self, pipe: Pipeline, browser: Browser | None, cfg: dict, profile: dict):
        self.pipe, self.browser, self.cfg, self.p = pipe, browser, cfg, profile
        self.base = cfg["sources"]["quotes"]
        self.wm = _load(STATE / "watermark.json", {"quotes": {}, "authors": {}}) if profile["watermark"] else None
        self.quotes, self.authors = [], {}

    @staticmethod
    def quote_id(text, author):
        return hashlib.sha1(f"{author}|{text}".encode()).hexdigest()[:12]

    def parse_html(self, html):
        soup = BeautifulSoup(html, "html.parser")
        out, bad = [], 0
        for q in soup.select("div.quote"):
            text, author = q.select_one(".text"), q.select_one(".author")
            if not text or not author:
                bad += 1
                continue
            name = author.get_text(strip=True)
            out.append({"text": text.get_text(strip=True), "author": name,
                        "tags": [t.get_text(strip=True) for t in q.select(".tags .tag")],
                        "author_url": urljoin(self.base, f"author/{site_slug(name)}/")})
        return out, bad

    def parse_api(self, body):
        data = json.loads(body)
        out = [{"text": q["text"], "author": q["author"]["name"], "tags": q.get("tags", []),
                "author_url": urljoin(self.base, f"author/{q['author'].get('slug') or site_slug(q['author']['name'])}/")}
               for q in data.get("quotes", [])]
        return out, 0

    def list_page(self, n: int) -> list[dict]:
        url = urljoin(self.base, self.cfg["workload"]["page_path"].format(n=n))
        resp = None
        if self.browser and self.browser.available:
            resp = self.browser.load(url, ".quote", self.cfg["workload"].get("selector_timeout_ms"))
            if resp.ok:
                quotes, bad = self.parse_html(resp.text)
                if quotes:
                    return self._accept(quotes, bad, url)
        # деградация: браузер недоступен или не дождался селектора → JSON-эндпоинт сайта
        api = urljoin(self.base, f"api/quotes?page={n}")
        reason = (self.browser.launch_error if self.browser and not self.browser.available
                  else (resp.error_class or resp.skipped or resp.verdict) if resp else "браузер выключен")
        self.pipe.log.log("fallback", url=url, fallback_url=api, error=reason)
        r = self.pipe.http(api, expected="quotes", kind="json")
        if r.ok:
            quotes, bad = self.parse_api(r.text)
            return self._accept(quotes, bad, api)
        return []

    def _accept(self, quotes, bad, url):
        seen = {self.quote_id(q["text"], q["author"]) for q in self.quotes}
        fresh = [q for q in quotes if self.quote_id(q["text"], q["author"]) not in seen]
        dup = len(quotes) - len(fresh)
        known = [q for q in fresh if self.wm is not None and self.quote_id(q["text"], q["author"]) in self.wm["quotes"]]
        self.quotes.extend(fresh)
        self.pipe.log.log("records", url=url, collected=len(fresh) - len(known), known=len(known),
                          discarded=bad + dup)
        return fresh

    def run(self):
        w = self.cfg["workload"]
        for n in range(1, w["pages"] + 1):
            quotes = self.list_page(n)
            if self.wm is not None and quotes:
                new = [q for q in quotes if self.quote_id(q["text"], q["author"]) not in self.wm["quotes"]]
                if not new:   # водяной знак: страница целиком уже собрана — дальше старые данные
                    rest = list(range(n + 1, w["pages"] + 1))
                    for m in rest:
                        self.pipe.log.log("watermark_skip", url=urljoin(self.base, w["page_path"].format(n=m)),
                                          error="страница старше водяного знака")
                    self.pipe.log.log("watermark_stop", page=n, skipped_pages=len(rest))
                    break
        # карточки авторов (HTTP)
        for q in self.quotes:
            au = q["author_url"]
            if au in self.authors:
                continue
            if self.wm is not None and au in self.wm["authors"]:
                self.authors[au] = "watermark"
                self.pipe.log.log("watermark_skip", url=au, error="автор уже собран ранее")
                self.pipe.log.log("records", url=au, collected=0, known=1, discarded=0)
                continue
            r = self.pipe.http(au, expected="author-details")
            if r.ok:
                soup = BeautifulSoup(r.text, "html.parser")
                born = soup.select_one(".author-born-date")
                self.authors[au] = born.get_text(strip=True) if born else None
                self.pipe.log.log("records", url=au, collected=1, discarded=0)
            else:
                self.pipe.log.log("records", url=au, collected=0, discarded=1)
        if self.wm is not None:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for q in self.quotes:
                self.wm["quotes"].setdefault(self.quote_id(q["text"], q["author"]), now)
            for au, v in self.authors.items():
                if v != "watermark":
                    self.wm["authors"].setdefault(au, now)
            _save(STATE / "watermark.json", self.wm)


def httpbin_set(pipe: Pipeline, cfg: dict):
    """Ресурсы с валидаторами (ETag / Last-Modified) и сжатием: на quotes.toscrape.com их нет."""
    base = cfg["sources"]["httpbin"]
    for path in cfg["workload"]["httpbin_paths"]:
        r = pipe.http(urljoin(base, path), expected=None, kind="json", min_size=20)
        pipe.log.log("records", url=urljoin(base, path), collected=int(r.ok and not r.from_cache),
                     known=int(r.from_cache and r.ok), discarded=int(not r.ok))
