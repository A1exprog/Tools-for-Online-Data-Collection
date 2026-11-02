import os
import re
import sqlite3
from datetime import datetime

from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem

from lab5_quotes.items import AuthorItem, QuoteItem


def _kind(item):
    return "author" if isinstance(item, AuthorItem) else "quote"


class ValidatePipeline:
    """100. Валидация обязательных полей (свой список для каждого типа записи)."""

    required = {
        "quote": ["quote", "author", "source_tag", "author_url"],
        "author": ["author", "author_url", "author_born_date", "author_born_place", "author_bio"],
    }

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        kind = _kind(item)
        missing = [f for f in self.required[kind] if not str(adapter.get(f) or "").strip()]
        stats = spider.crawler.stats
        if missing:
            stats.inc_value("dropped/missing_fields")
            for f in missing:
                stats.inc_value(f"dropped/missing_fields/{kind}/{f}")
            raise DropItem(f"Missing fields {missing} in {kind}: {adapter.get('author_url')}")
        stats.inc_value(f"validate/passed/{kind}")
        return item


class CleanPipeline:
    """200. Очистка и типизация: пробелы, регистр, кавычки, дата → ISO, место без «in»."""

    QUOTE_MARKS = "“”\"'"

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        stats = spider.crawler.stats
        if _kind(item) == "quote":
            text = " ".join(adapter["quote"].split()).strip(self.QUOTE_MARKS + " ")
            adapter["quote"] = text
            adapter["author"] = " ".join(adapter["author"].split())
            adapter["source_tag"] = adapter["source_tag"].strip().lower()
            adapter["tags"] = sorted({t.strip().lower() for t in adapter.get("tags") or [] if t and t.strip()})
            adapter["tag_page"] = int(adapter.get("tag_page") or 1)
            adapter["business_key"] = (
                f"{adapter['author'].lower()}|{text.lower()}|{adapter['source_tag']}"
            )
            stats.inc_value("clean/quotes")
            if not adapter["tags"]:
                stats.inc_value("clean/quotes_without_tags")
            return item

        # author
        raw_date = adapter["author_born_date"].strip()
        try:
            adapter["author_born_date"] = datetime.strptime(raw_date, "%B %d, %Y").date().isoformat()
        except ValueError:
            stats.inc_value("dropped/invalid_date")
            raise DropItem(f"Invalid author_born_date: {raw_date!r}")
        place = " ".join(adapter["author_born_place"].split())
        adapter["author_born_place"] = re.sub(r"^in\s+", "", place, flags=re.IGNORECASE)
        adapter["author"] = " ".join(adapter["author"].split())
        adapter["author_bio"] = " ".join(adapter["author_bio"].split())
        adapter["business_key"] = adapter["author"].lower()
        stats.inc_value("clean/authors")
        stats.inc_value("clean/dates_to_iso")
        return item


class DuplicateFilterPipeline:
    """300. Дедупликация по бизнес-ключу (не по URL).

    Цитата: автор + цитата + тег. Автор: имя автора.
    """

    def __init__(self):
        self.seen = {"quote": set(), "author": set()}

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        kind = _kind(item)
        key = adapter["business_key"]
        if key in self.seen[kind]:
            spider.crawler.stats.inc_value("dropped/duplicates")
            spider.crawler.stats.inc_value(f"dropped/duplicates/{kind}")
            raise DropItem(f"Duplicate {kind}: {key}")
        self.seen[kind].add(key)
        return item


class SQLitePipeline:
    """400. Пакетная запись в SQLite: executemany + INSERT OR REPLACE."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS quotes (
        business_key TEXT PRIMARY KEY,          -- author|quote|source_tag
        quote        TEXT NOT NULL,
        author       TEXT NOT NULL,
        tags         TEXT,                      -- через запятую
        source_tag   TEXT NOT NULL,
        tag_page     INTEGER,
        author_url   TEXT NOT NULL,
        scraped_at   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS authors (
        author_url        TEXT PRIMARY KEY,
        business_key      TEXT UNIQUE NOT NULL, -- имя автора в нижнем регистре
        author            TEXT NOT NULL,
        author_born_date  TEXT,                 -- ISO 8601
        author_born_place TEXT,
        author_bio        TEXT,
        scraped_at        TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_quotes_author_url ON quotes(author_url);
    CREATE INDEX IF NOT EXISTS ix_quotes_source_tag ON quotes(source_tag);
    -- итоговая запись варианта 4: «цитата тега + данные автора»
    CREATE VIEW IF NOT EXISTS quotes_full AS
        SELECT q.business_key, q.quote, q.author, q.tags, q.source_tag, q.tag_page,
               a.author_born_date, a.author_born_place, a.author_bio, q.author_url
        FROM quotes q LEFT JOIN authors a ON a.author_url = q.author_url;
    """

    SQL = {
        "quote": """INSERT OR REPLACE INTO quotes
                    (business_key, quote, author, tags, source_tag, tag_page, author_url, scraped_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        "author": """INSERT OR REPLACE INTO authors
                     (author_url, business_key, author, author_born_date, author_born_place,
                      author_bio, scraped_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
    }

    def __init__(self, db_path="data/crawl.db", batch_size=50):
        self.db_path = db_path
        self.batch_size = batch_size
        self.conn = None
        self.batch = {"quote": [], "author": []}

    @classmethod
    def from_crawler(cls, crawler):
        return cls(
            db_path=crawler.settings.get("SQLITE_DB_PATH", "data/crawl.db"),
            batch_size=crawler.settings.getint("SQLITE_BATCH_SIZE", 50),
        )

    def open_spider(self, spider):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def close_spider(self, spider):
        for kind in self.batch:
            self._flush(kind, spider)
        if self.conn:
            self.conn.close()

    def process_item(self, item, spider):
        a = ItemAdapter(item)
        kind = _kind(item)
        now = datetime.now().isoformat(timespec="seconds")
        if kind == "quote":
            row = (a["business_key"], a["quote"], a["author"], ",".join(a["tags"]),
                   a["source_tag"], a["tag_page"], a["author_url"], now)
        else:
            row = (a["author_url"], a["business_key"], a["author"], a["author_born_date"],
                   a["author_born_place"], a["author_bio"], now)
        self.batch[kind].append(row)
        if len(self.batch[kind]) >= self.batch_size:
            self._flush(kind, spider)
        return item

    def _flush(self, kind, spider):
        rows = self.batch[kind]
        if not rows:
            return
        self.conn.executemany(self.SQL[kind], rows)
        self.conn.commit()
        stats = spider.crawler.stats
        stats.inc_value("pipeline/sqlite_written", len(rows))
        stats.inc_value(f"pipeline/sqlite_written/{kind}", len(rows))
        stats.inc_value("pipeline/sqlite_batches")
        rows.clear()
