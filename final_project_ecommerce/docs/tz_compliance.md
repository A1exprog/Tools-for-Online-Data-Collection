# Сверка с техническим заданием (Занятие 8)

Легенда: ✅ реализовано в коде/документах · 📸 нужно сделать после реального прогона (скриншоты, цифры) ·
➖ не требуется / необязательно.

## 2.1. Источники и объём

| Требование | Статус | Где |
|---|---|---|
| ≥ 2 источника: ≥ 1 HTML и ≥ 1 REST API | ✅ | scrapingcourse.com/ecommerce (HTML) + dummyjson.com/products (API) |
| Объём чистого слоя ≥ 500 (или ≥ 300 при двух связанных сущностях) | ✅ | ≈ 382 товара + ≈ 582 отзыва (две сущности, FK); контроль — метрики `products_count`, `reviews_count` |
| Связь между источниками | ✅ | сопоставление по единым товарным группам (`product_groups`, `config.yaml → linkage`), метрики `group_overlap_count`, `html_unmapped_group_pct`; представление `v_overlap_groups` |
| Допустимые площадки | ✅ | обе — из списка «Источники данных для итогового проекта» |
| Запрещено: обход капчи, маскировка, чужая авторизация | ✅ | честный UA, без прокси/авторизации; `docs/legal_checklist.md` п. 5–6 |

## 2.2. Слой сбора

| Требование | Статус | Где |
|---|---|---|
| 1. Паспорт каждого источника (тип, вход, пагинация, лимиты, формат, ключ, ПДн) | ✅ | `docs/source_passports.md` |
| 2. Протокол диагностики HTML (View Source → текст → Network/XHR) | ✅ 📸 | `src/diagnose.py`, `docs/diagnostics.md`; приложить свой `diagnostic_*.json` и скриншоты DevTools |
| 3. Вежливость: лимит на домен, джиттер, Crawl-delay, User-Agent с контактом | ✅ | `src/http_client.py` (`_throttle`, `load_robots`), `src/robots.py` (RFC 9309), `config.yaml → http, sources.*.rate_limit_sec/jitter_sec` |
| robots.txt scrapingcourse запрещает `/ecommerce/*` | ⚠️ | явное исключение с обоснованием и журналированием; **подтвердить у преподавателя** (`docs/legal_checklist.md`, п. 4) |
| 4. Устойчивость: классификация ошибок, повторы только временных, приоритет Retry-After, предохранитель | ✅ | `http_client.py`: `TRANSIENT_STATUSES`, `PermanentHTTPError`, `_retry_after`, `CircuitBreaker`; тесты `tests/test_pipeline.py` |
| 5. ≥ 2 механизма снижения нагрузки | ✅ (все 5) | ETag/Last-Modified → 304; водяные знаки; sitemap; `--debug-cache`; gzip |

## 2.3. Хранение и качество

| Требование | Статус | Где |
|---|---|---|
| 1. `raw/` JSON Lines, только дозапись, метаданные происхождения | ✅ | `src/state.py → RawWriter` (режим `"a"`, конверт: run_id, source, url, fetched_at, http_status, page_sha256, parser_version, record_hash) |
| `clean/` Parquet | ✅ | `clean/products.parquet`, `clean/reviews.parquet` |
| `mart/` представления | ✅ | представления `v_*` в `db/schema.sql` → `mart/*.csv`, `*.parquet` |
| 2. Контракт (pydantic): типы, обязательность, диапазоны, ≥ 2 собственных валидатора | ✅ | `src/contract.py` — 8 валидаторов; тесты `tests/test_contract.py` |
| Отбракованные записи с причиной | ✅ | `clean/rejects.jsonl`, `clean/cleaning_protocol.json` |
| 3. БД: PK по бизнес-ключу, NOT NULL, CHECK, индексы, `source`, `collected_at`, `updated_at`, `parser_version` | ✅ | `db/schema.sql` |
| 4. Идемпотентный UPSERT, демонстрация тремя прогонами | ✅ 📸 | `storage.py → upsert_all`; `python -m src.demo_idempotency` → `reports/idempotency_demo.json` |
| 5. ≥ 6 метрик качества с порогами и вердиктом; при RED mart не строится | ✅ | `src/quality.py` — 11 метрик, GREEN/YELLOW/RED; `run_pipeline.py` блокирует БД и mart, exit 3 |

## 2.4. Наблюдаемость и режим работы

| Требование | Статус | Где |
|---|---|---|
| 1. Структурные JSON-логи по каждому запросу и событию | ✅ | `logs/run_<id>.jsonl` (`http_request`, `http_retry`, `circuit_opened`, `quality_metric`, …) |
| 2. Отчёт о прогоне: запросы, коды, доля ошибок, повторы, медиана и p95, трафик, записи собранные/отброшенные, длительность | ✅ | `reports/run_report_<id>.json`, сводка в консоли |
| 3. ≥ 4 порога оповещения, включая сравнение объёма с прошлым прогоном | ✅ | 7 порогов, `quality.evaluate_alerts`, таблица `runs` |
| 4. Расписание с частотой и обоснованием | ✅ | n8n Schedule Trigger (`0 6 * * *`), `config.yaml → schedule`, cron/schtasks в README |
| 5. Возобновляемость после прерывания | ✅ | `state/checkpoint.json` + `watermark.json` + `etags.json`; проверено прерыванием Ctrl+C |

## 2.5. No-code компонент

| Требование | Статус | Где |
|---|---|---|
| Работающий элемент: оркестрация (расписание + запуск + оповещение) | ✅ 📸 | `nocode/workflow.json` (импорт в n8n, подключить Telegram) |
| Экспорт workflow | ✅ | `nocode/workflow.json` |
| Скриншоты | 📸 | `nocode/screenshots/` — список нужных кадров в `nocode/notes.md` |
| Таблица «этап → код / no-code → что не удалось перенести» | ✅ | `nocode/notes.md` |

## 2.6. Аналитический результат

| Требование | Статус | Где |
|---|---|---|
| 2–3 вывода с подтверждением (таблицы, агрегаты) | ✅ 📸 | `analysis.ipynb` разделы 1–4, 6 (выводы формируются из данных), `mart/conclusions.txt` |
| ≥ 2 графика | ✅ | 5 рисунков → `mart/fig_*.png` |
| Явные ограничения | ✅ | `analysis.ipynb` раздел 7, `docs/data_card.md` |

## 2.7. Документация и правовая часть

| Требование | Статус | Где |
|---|---|---|
| README: зависимости, переменные окружения, команды, время прогона | ✅ | `README.md` |
| Схема пайплайна | ✅ | `docs/pipeline_diagram.png` / `.mmd`, раздел 3 `report.md` |
| Правовой чек-лист (12 пунктов) | ✅ | `docs/legal_checklist.md` |
| Паспорт данных | ✅ | `docs/data_card.md` |

## 3. Состав сдаваемых материалов

| Файл/папка | Статус |
|---|---|
| `README.md`, `config.yaml`, `requirements.txt` | ✅ |
| `src/collect_html.py`, `collect_api.py`, `clean.py`, `contract.py`, `storage.py`, `quality.py`, `observability.py`, `run_pipeline.py` | ✅ |
| `src/collect_dynamic.py` | ➖ опционально; браузер не нужен (доказательство — `diagnose.py`) |
| `raw/`, `clean/`, `mart/`, `db/warehouse.db`, `logs/run_*.jsonl`, `reports/run_report_*.json`, `state/{watermark,etags}.json` | 📸 создаются первым прогоном |
| `db/schema.sql` | ✅ |
| `nocode/{workflow.json, screenshots, notes.md}` | ✅ + 📸 скриншоты |
| `analysis.ipynb` | ✅ (выполнить после прогона) |
| `docs/{pipeline_diagram, data_card.md, legal_checklist.md}` | ✅ |
| `slides.pdf` (5–7 слайдов) | ✅ генерируется `python -m src.make_slides` (6 слайдов) после прогона |

## 5. Живая демонстрация

| Сценарий | Команда | Что показать |
|---|---|---|
| Быстрый инкрементальный прогон (30–60 с) | `python -m src.run_pipeline` | ответы 304, `raw_appended: {}`, `inserted 0 / updated 0`, сводка отчёта |
| Устойчивость | `python -m src.run_pipeline --simulate-failure api` | повторы с Retry-After, инцидент, оповещения, HTML-источник OK, `exit code 1` (`echo $LASTEXITCODE` в PowerShell) |
| Резерв без интернета | `python tests/mock_server.py` + `--config tests/config.mock.yaml` | та же механика на mock; плюс сохранённые логи/скриншоты |
