# No-code компонент: n8n

В проекте два workflow:

| Файл | Что делает | Требование ТЗ 2.5 |
|---|---|---|
| **`workflow_scraping.json`** | **Сбор обоих источников нодами n8n**: пагинация HTML-каталога, карточки и страницы товаров, API с limit/skip, нормализация, проверка качества, выгрузка в CSV (и опционально в Google Sheets), оповещение в Telegram | «сбор одного из источников» + «выгрузка в таблицу» + оповещение |
| `workflow.json` | Оркестрация Python-пайплайна: расписание → `python -m src.n8n_entry` → IF → Telegram | «оркестрация (расписание + запуск + оповещение)» |

## 1. `workflow_scraping.json` — сбор нодами

```
[Расписание 06:00] / [Ручной запуск]
   ├─> HTML: страницы каталога ──> HTML: карточки ──> Разбить на карточки ──> HTML: поля карточки ─┬─────────────────────┐
   │   (HTTP Request, пагинация        (HTML extract     (Split Out)              (HTML extract:     │                     ▼
   │    по ссылке «next», 1 запрос/с)   li.product)                                имя, цена, url)    └─> HTML: страница ─> HTML: поля ─> Карточка + страница
   │                                                                                                      товара (1/с)      страницы     (Merge по позиции)
   │                                                                                                                                         │
   │                                                                                                                         Нормализация HTML (Code) ─┐
   └─> API: товары (HTTP, пагинация skip) ──> Разбить на товары API ─┬─> Нормализация API (без ПДн) (Code) ───────────────────────────────┤
                                                                     └─> Отзывы API (без ПДн) ─> Отзывы → CSV ─> Сохранить reviews_n8n.csv  ▼
                                                                                                                                 Все товары (Merge: append)
                                                                                                    ┌──────────────────────────────────┼─────────────────────────┐
                                                                                         Проверка качества (Code)          Товары → CSV → Сохранить        Google Sheets (опц.)
                                                                                                    │                     products_n8n.csv
                                                                                             Качество RED? ─ да ─> Telegram: инцидент
                                                                                                    └──── нет ─> Telegram: сводка
```

Что повторяет из кода:

| Шаг Python | Узел n8n |
|---|---|
| `collect_html.py`: пагинация по `a.next.page-numbers`, ≤ 30 страниц | **HTTP Request** с Pagination → «Response contains next URL», пауза 1000 мс, максимум 30 запросов |
| `parse_listing`: `li.product`, название, цена, ссылка, id, SKU | **HTML** (Extract HTML Content) → **Split Out** → **HTML** |
| карточка товара: категории, SKU, наличие, вариации | **HTTP Request** (батч 1 запрос/с) → **HTML** |
| `collect_api.py`: `limit=50`, `skip` до `total`, `select` | **HTTP Request** с Pagination → «Update a parameter in each request» (`skip = $pageCount × 50`) |
| `strip_pii`: без `reviewerName` / `reviewerEmail` | **Code** «Отзывы API (без ПДн)» копирует только оценку, текст и дату |
| `clean.py`: цена → число, бизнес-ключ, товарная группа, наличие по вариациям, дубли | **Code** «Нормализация HTML / API» (те же правила групп, что в `config.yaml`) |
| `quality.py`: метрики и светофор | **Code** «Проверка качества» — 7 метрик WARN/CRITICAL → **IF** |
| выгрузка | **Convert to File** (CSV) → **Read/Write Files from Disk** → `nocode/output/*.csv`; **Google Sheets** (append or update по `business_key`, выключен) |
| оповещения | **Telegram** (инцидент со звуком / сводка без звука) |
| повторы при сбоях | у HTTP-узлов **Retry On Fail**: 3 попытки, 2 с |

## 2. Как импортировать и запустить

1. n8n self-hosted (`npx n8n` или Docker) → *Workflows → Import from File* → `nocode/workflow_scraping.json`.
2. Узлы «Сохранить …csv»: проверьте путь к папке `nocode/output/` (создайте её). В новых версиях n8n запись на диск
   разрешена только в разрешённые папки: переменная `N8N_RESTRICT_FILE_ACCESS_TO` должна включать этот путь.
3. Telegram: создать бота у @BotFather, добавить учётные данные *Telegram API* в оба Telegram-узла, задать
   `TELEGRAM_CHAT_ID` (или вписать chat id в узлы). Без Telegram workflow всё равно соберёт данные и запишет CSV.
4. *Execute workflow* → ~4 минуты (188 страниц товаров по 1 в секунду) → в `nocode/output/` появятся
   `products_n8n.csv` (≈ 382 строки) и `reviews_n8n.csv` (≈ 582 строки).
5. Для расписания — переключатель *Active*.

## 3. Скриншоты для сдачи (`nocode/screenshots/`)
1. `01_workflow_scraping.png` — весь workflow сбора на холсте;
2. `02_pagination.png` — настройки пагинации узла «HTML: страницы каталога»;
3. `03_execution.png` — успешное выполнение (число items на узлах: 12 страниц → 188 карточек → 382 товара);
4. `04_output_csv.png` — открытый `products_n8n.csv`;
5. `05_telegram.png` — сообщение со сводкой качества;
6. `06_workflow_orchestration.png` — второй workflow (запуск Python-пайплайна).

## 4. Этап → код / no-code → что не удалось перенести

| Этап | Код (Python) | No-code (n8n) | Что не удалось перенести / ограничения n8n |
|---|---|---|---|
| Расписание | cron / Планировщик Windows | ✅ Schedule Trigger | — |
| Пагинация HTML | `collect_html.py` | ✅ HTTP Request: next URL | нет запасных селекторов: при смене вёрстки узел молча вернёт пустые поля |
| Разбор карточек | BeautifulSoup + `SEL` с fallback | ✅ HTML Extract + Split Out | нет журнала срабатывания запасных селекторов (`selector_fallback`) |
| Сбор API | `collect_api.py` | ✅ HTTP Request: пагинация skip | нет условных запросов ETag → 304 — каждый прогон качает всё заново |
| Вежливость | лимит на домен + джиттер, Crawl-delay, robots.txt (RFC 9309) | ⚠️ фиксированная пауза 1 с | нет джиттера, Crawl-delay и чтения robots.txt |
| Устойчивость | временные/постоянные ошибки, Retry-After, circuit breaker | ⚠️ Retry On Fail | повтор одинаков для 404 и 503, Retry-After игнорируется, предохранителя нет |
| Инкрементальность | водяные знаки, sha256, sitemap lastmod, checkpoint | ❌ | n8n не хранит состояние между запусками без внешней БД — всегда полный сбор (~200 запросов) |
| Очистка и связь источников | `clean.py` | ⚠️ узел Code (JavaScript) | по сути это код внутри n8n, а не no-code |
| Контракт данных | pydantic, 8 валидаторов, `rejects.jsonl` с причиной | ❌ | типы и диапазоны не проверяются, отбракованное не сохраняется с причиной (вывод занятия 6) |
| Метрики качества | 11 метрик, `quality.py` | ⚠️ 7 метрик в узле Code | снова код; нет порога «объём против прошлого прогона» (нет истории) |
| Хранилище | SQLite: UPSERT, CHECK, история цен, журнал прогонов | ⚠️ CSV / Google Sheets (append or update) | нет ограничений CHECK, истории изменений и транзакций |
| Сырой слой | `raw/*.jsonl`, только дозапись | ❌ | данные живут только внутри выполнения n8n |
| Наблюдаемость | JSON-логи по каждому запросу, отчёт: p50/p95, трафик | ⚠️ история выполнений n8n | нет латентности p95, трафика, распределения кодов |
| Оповещения | `evaluate_alerts` → reports | ✅ IF + Telegram | — |
| Аналитика | `analysis.ipynb` | ❌ | статистика и графики в n8n не делаются |

**Вывод:** n8n хорошо закрывает расписание, простой сбор с пагинацией, выгрузку в таблицу и оповещения.
Качество данных, инкрементальность и устойчивость к сбоям без кода не переносятся — поэтому в итоговой
архитектуре (конфигурация B) данные собирает и проверяет Python, а n8n отвечает за расписание и оповещения
(`workflow.json`); `workflow_scraping.json` — доказательство, что сбор можно повторить нодами, и замер того,
что при этом теряется.

> robots.txt scrapingcourse.com запрещает `/ecommerce/*`; workflow n8n robots.txt не читает и использует
> то же задокументированное исключение для учебной площадки, что и Python-код (`docs/legal_checklist.md`, п. 4).
