# ЛР5, вариант 4 — quotes.toscrape.com, обход по тегам (Scrapy + n8n)

## Структура
```
lab5_quotes/           проект Scrapy (scrapy startproject)
  items.py             QuoteItem (цитата тега) и AuthorItem (карточка автора)
  pipelines.py         Validate(100) → Clean(200) → Dedup(300) → SQLite(400)
  settings.py          вежливость, предохранители, кэш (с комментариями)
  spiders/tags.py      паук tags
data/
  crawl.db             SQLite: quotes, authors, представление quotes_full
  items.jsonl          сырой экспорт всех Item основного прогона
  stats_<N>.json       статистика каждого прогона (1 — основной, 2–3 — JOBDIR, 4–5 — HTTPCACHE)
  quality_summary.json эталон для smoke-check
  jobdir_demo.db, cache_demo.db  БД демонстрационных прогонов
jobstate/tags_run1/    состояние JOBDIR после демонстрации
logs/                  логи всех пяти прогонов
quality/
  quality_report.ipynb отчёт о качестве (полнота, отбраковка, коды, трафик, агрегаты)
  smoke_check.py       проверка деградации
nocode/
  workflow.json        workflow n8n
  notes.md             как запустить, что замерить
report.md              отчёт
```

## Установка
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск
```bash
# основной прогон (https://quotes.toscrape.com/)
scrapy crawl tags

# тот же паук на локальной копии сайта (github.com/scrapinghub/spidyquotes)
scrapy crawl tags -a base_url=http://127.0.0.1:8800/

# JOBDIR: запустить, прервать Ctrl+C ОДИН раз (мягкая остановка), запустить ту же команду ещё раз
scrapy crawl tags -s JOBDIR=jobstate/tags_run1 -s SQLITE_DB_PATH=data/jobdir_demo.db -s FEEDS={}

# HTTP-кэш: первый запуск заполняет .scrapy/httpcache/, второй отдаёт всё из кэша
scrapy crawl tags -s HTTPCACHE_ENABLED=True -s SQLITE_DB_PATH=data/cache_demo.db -s FEEDS={}

# проверка деградации (код выхода 1 = FAIL)
python quality/smoke_check.py

# отчёт о качестве
jupyter notebook quality/quality_report.ipynb
```
В PowerShell `FEEDS={}` пишется в кавычках: `-s "FEEDS={}"`.

## n8n
```bash
docker run -it --rm --name n8n -p 5678:5678 ^
  -v n8n_data:/home/node/.n8n ^
  -v <путь к проекту>\nocode:/files ^
  -e N8N_RESTRICT_FILE_ACCESS_TO=/files ^
  -e TELEGRAM_CHAT_ID=<id чата> ^
  docker.n8n.io/n8nio/n8n
```
Дальше: Workflows → Import from File → `nocode/workflow.json`. Подробности в `nocode/notes.md`.
