BOT_NAME = "lab5_quotes"

SPIDER_MODULES = ["lab5_quotes.spiders"]
NEWSPIDER_MODULE = "lab5_quotes.spiders"

# ---------------- Вежливость ----------------
# Соблюдаем robots.txt (у quotes.toscrape.com он отдаёт 404 = ограничений нет,
# но проверка всё равно выполняется: robotstxt/request_count в статистике).
ROBOTSTXT_OBEY = True
# Честно представляемся: учебный краулер + контакт для владельца сайта.
USER_AGENT = (
    "EducationalCrawler/1.0 "
    "(+https://example.edu; contact: student@example.edu)"
)

# Глобальный потолок параллельных запросов Scrapy.
CONCURRENT_REQUESTS = 16
# Не больше 4 одновременных запросов к одному домену (требование ≤ 4).
CONCURRENT_REQUESTS_PER_DOMAIN = 4
# Базовая пауза между запросами к домену, с; Scrapy добавляет случайный
# разброс 0.5–1.5× (RANDOMIZE_DOWNLOAD_DELAY), чтобы не долбить ровным ритмом.
DOWNLOAD_DELAY = 1.0

# AutoThrottle подстраивает паузу под задержку ответа сервера:
# медленнее отвечает — реже спрашиваем.
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0     # стартовая пауза, с
AUTOTHROTTLE_MAX_DELAY = 10.0      # максимум паузы при медленном сервере, с
# В среднем ~2 запроса «в полёте» к сайту — щадящий режим для чужого учебного сервера.
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0

# ---------------- Устойчивость ----------------
DOWNLOAD_TIMEOUT = 30              # дольше 30 с не ждём — считаем ошибкой
RETRY_ENABLED = True
RETRY_TIMES = 2                    # 2 повтора = до 3 попыток на запрос
# Повторяем только временные ошибки: 5xx, таймаут и 429 «слишком часто».
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]

# ---------------- Предохранители ----------------
# Глубина: главная (0) → страницы главной 2..10 (1–9) → страницы тегов и их
# пагинация → авторы. Измеренный максимум в прогоне — request_depth_max = 11
# (data/stats_*.json); лимит 15 даёт запас, но не пустит в бесконечную пагинацию.
DEPTH_LIMIT = 15
DEPTH_STATS_VERBOSE = True         # распределение запросов по глубине в статистике
# Ожидаемо 232 цитаты-в-теге + 49 авторов = 281 запись. Порог 400 не обрежет
# нормальный прогон, но остановит паука, если вёрстка «зациклит» пагинацию.
CLOSESPIDER_ITEMCOUNT = 400
CLOSESPIDER_PAGECOUNT = 300        # в прогоне 202 ответа
CLOSESPIDER_TIMEOUT = 1200         # 20 минут — с запасом к ~4 мин нормального прогона
CLOSESPIDER_ERRORCOUNT = 20        # 20 исключений в колбэках паука — останов

# ---------------- HTTP-кэш (только для отладки) ----------------
# По умолчанию выключен, чтобы рабочий прогон видел свежие данные.
# Для отладки селекторов: scrapy crawl tags -s HTTPCACHE_ENABLED=True
HTTPCACHE_ENABLED = False
HTTPCACHE_EXPIRATION_SECS = 0      # 0 = кэш не устаревает
HTTPCACHE_DIR = "httpcache"
HTTPCACHE_IGNORE_HTTP_CODES = [500, 502, 503, 504, 408, 429]

LOG_LEVEL = "INFO"

ITEM_PIPELINES = {
    "lab5_quotes.pipelines.ValidatePipeline": 100,
    "lab5_quotes.pipelines.CleanPipeline": 200,
    "lab5_quotes.pipelines.DuplicateFilterPipeline": 300,
    "lab5_quotes.pipelines.SQLitePipeline": 400,
}

FEEDS = {
    "data/items.jsonl": {
        "format": "jsonlines",
        "encoding": "utf8",
        "overwrite": True,
    },
}

SQLITE_DB_PATH = "data/crawl.db"
SQLITE_BATCH_SIZE = 50

FEED_EXPORT_ENCODING = "utf-8"
