# Итоговый проект: гибридный пайплайн сбора данных о товарах

**Исследовательский вопрос.** Как соотносятся уровень цен и структура ассортимента двух учебных
интернет-магазинов — `scrapingcourse.com/ecommerce` (HTML) и `DummyJSON` (REST API) — в сопоставимых
товарных группах, и связаны ли цена и скидка с рейтингом товара и оценками в отзывах?

**Конфигурация B (гибридная):** Python собирает, очищает, проверяет и загружает данные; n8n отвечает за
расписание, запуск и оповещения. Подробности решений — в [`report.md`](report.md).

```
scrapingcourse.com (HTML) ─┐                                         ┌─> SQLite (UPSERT) ─> mart/ ─> analysis.ipynb
                           ├─> PoliteClient ─> raw/ ─> clean/ ─> quality ┤
dummyjson.com (API) ───────┘   (лимиты, 304,     JSONL   Parquet  11 метрик └─> RED: публикация блокируется
                                повторы, CB)                       
n8n: Schedule 06:00 → Execute Command → IF → Telegram
```

Схема: [`docs/pipeline_diagram.png`](docs/pipeline_diagram.png) (исходник — `docs/pipeline_diagram.mmd`).

---

## 1. Воспроизведение с нуля

### Требования
* Python **3.10+** (проверено на 3.13), ~200 МБ на зависимости, интернет-доступ к
  `www.scrapingcourse.com` и `dummyjson.com`.
* Для no-code части — n8n (self-hosted) и Telegram-бот (см. `nocode/notes.md`).

### Установка

Windows (PowerShell):
```powershell
cd final_project_ecommerce
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
Linux / macOS:
```bash
cd final_project_ecommerce
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Настройка
Все параметры — в `config.yaml` (источники, лимиты, пороги качества и оповещений, расписание).
**Обязательно** замените контакт в `http.user_agent` и `legal.contact_email` на свой e-mail.

> ⚠️ robots.txt scrapingcourse.com содержит `Disallow: /ecommerce/*`. Сбор идёт по явному исключению
> `http.robots_exceptions` (обоснование — `docs/legal_checklist.md`, п. 4). Удалите исключение — и
> пайплайн откажется собирать HTML-источник.
Переменные окружения нужны только n8n: `TELEGRAM_CHAT_ID` (и учётные данные бота внутри n8n).

### Команды

| Что | Команда | Ожидаемое время |
|---|---|---|
| Диагностика источников (нужен ли браузер, ETag, robots) | `python -m src.diagnose` | ~5 с |
| **Первый (полный) прогон** | `python -m src.run_pipeline` | ~4–6 мин (≈ 200 HTML-запросов по 1 с + 4 запроса к API) |
| Повторный (инкрементальный) прогон | `python -m src.run_pipeline` (по умолчанию) | ~30–60 с, в основном ответы 304 |
| Полный пересбор без 304/водяных знаков | `python -m src.run_pipeline --full` | ~4–6 мин |
| Быстрый прогон без карточек товаров | `python -m src.run_pipeline --no-details` | ~30 с |
| Демонстрация идемпотентности (3 прогона) | `python -m src.demo_idempotency` | ~1–2 мин после первого прогона |
| Демонстрация устойчивости (API → httpbin 503) | `python -m src.run_pipeline --simulate-failure api` | ~15 с, exit code ≠ 0 |
| Аналитика | `jupyter notebook analysis.ipynb` → *Run All* | ~10 с |
| Слайды для защиты (из реальных результатов) | `python -m src.make_slides` → `slides.pdf` | ~5 с |
| Тесты (парсеры, контракт, сквозные на mock) | `pytest -q` | ~10 с |
| Очистка raw по сроку хранения | `python -m src.retention --dry-run` | ~1 с |

Рекомендуемый порядок к защите:
```bash
python -m src.diagnose
python -m src.run_pipeline              # 1-й прогон: полный сбор
python -m src.demo_idempotency          # прогоны 2–4: 304, 0 вставок, COUNT(*) не меняется
python -m src.run_pipeline --simulate-failure api
jupyter nbconvert --to notebook --execute --inplace analysis.ipynb
python -m src.make_slides
pytest -q
```

### Коды завершения `run_pipeline`
`0` — успех; `1` — данные опубликованы, но есть критические оповещения (например, отказ источника);
`2` — нечего очищать; `3` — качество RED, публикация в БД и mart заблокирована; `130` — прерван (Ctrl+C),
следующий запуск продолжит с чекпоинта (`state/checkpoint.json`).

### Резервная демонстрация без интернета
```bash
python tests/mock_server.py --port 8765                      # окно 1
python -m src.run_pipeline --config tests/config.mock.yaml   # окно 2 (результаты в mock_run/)
```
Mock имитирует вёрстку WooCommerce и формат DummyJSON (ETag/304, gzip, robots, sitemap, 503) — данные
синтетические, только для показа механики. Также сохраните логи и скриншоты реальных прогонов.

---

## 2. Структура проекта

```
final_project_ecommerce/
├── README.md                  # этот файл
├── report.md                  # отчёт: решения по шести осям защиты, результаты
├── config.yaml                # источники, лимиты, пороги качества и оповещений, расписание
├── requirements.txt
├── src/
│   ├── collect_html.py        # сбор scrapingcourse.com: листинг + карточки, sitemap, 304, checkpoint
│   ├── collect_api.py         # сбор DummyJSON: limit/skip, select, ETag, водяной знак, удаление ПДн
│   ├── http_client.py         # вежливость и устойчивость: лимиты, robots, Retry-After, circuit breaker
│   ├── state.py               # raw (append-only JSONL), водяные знаки, checkpoint
│   ├── contract.py            # контракт данных (pydantic): 2 сущности, 8 собственных валидаторов
│   ├── clean.py               # очистка с протоколом, связь источников (товарные группы), rejects
│   ├── storage.py             # SQLite: UPSERT, журнал прогонов, mart
│   ├── quality.py             # 11 метрик, вердикт GREEN/YELLOW/RED, оповещения
│   ├── observability.py       # JSON-логи, отчёт о прогоне
│   ├── run_pipeline.py        # единая точка входа
│   ├── diagnose.py            # протокол диагностики «нужен ли браузер»
│   ├── demo_idempotency.py    # демонстрация трёх прогонов
│   ├── n8n_entry.py           # обёртка для n8n (одна JSON-строка в stdout)
│   ├── make_slides.py         # slides.pdf из результатов
│   └── retention.py           # срок хранения raw с компакцией
├── db/schema.sql              # таблицы, CHECK, индексы, триггеры истории, представления mart
├── raw/  clean/  mart/  logs/  reports/  state/   # создаются при первом запуске
├── nocode/{workflow.json, notes.md, screenshots/}
├── docs/{pipeline_diagram.*, source_passports.md, diagnostics.md, data_card.md, legal_checklist.md, tz_compliance.md}
├── tests/                     # pytest: парсеры на фикстурах и raw-снимках, контракт, сквозные на mock
├── analysis.ipynb             # выводы, графики, ограничения
└── slides.pdf                 # генерируется src/make_slides.py
```

## 3. Расписание
Ежедневно в 06:00 — n8n Schedule Trigger (`nocode/workflow.json`). Обоснование: учебные каталоги меняются
редко, суточного шага достаточно, чтобы заметить изменения цен/наличия и деградацию источника; повторные
прогоны дешёвые (≈ 20 запросов благодаря 304 и водяным знакам).

Альтернативы без n8n:
```bash
# Linux cron
0 6 * * * cd /path/to/final_project_ecommerce && .venv/bin/python -m src.run_pipeline --quiet >> logs/cron.log 2>&1
```
```powershell
# Планировщик Windows
schtasks /Create /TN "ecommerce_pipeline" /SC DAILY /ST 06:00 /TR "cmd /c cd /d C:\path\to\final_project_ecommerce && .venv\Scripts\python -m src.run_pipeline --quiet"
```
