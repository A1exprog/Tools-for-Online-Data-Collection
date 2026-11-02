# ЛР7, вариант 4 — устойчивый браузерный сбор (Playwright)

Исходный сборщик — цитаты quotes.toscrape.com из ЛР4. Страницы `/js/page/N/` рендерятся в Chromium
через Playwright, карточки авторов забираются HTTP-запросами. Пайплайн доработан до устойчивого,
вежливого и наблюдаемого.

## Установка
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## Запуск
```powershell
python run.py all        # всё по порядку, ~10 минут: robots → A/B/C → сценарии → 4 процесса → графики
```
По шагам:

| Команда | Что делает | Результат |
|---|---|---|
| `python run.py robots` | robots.txt источников: can_fetch, Crawl-delay, Request-rate, Sitemap | `state/robots.json` |
| `python run.py abc` | прогоны A, B, C_cold (с чистым state), C_warm на одинаковом объёме | `logs/run_*.jsonl`, `reports/run_report_*.json`, `reports/comparison.md` |
| `python run.py scenarios` | сценарии варианта 4 и демонстрации повторов, предохранителя, детектора | `logs/run_S*.jsonl`, `logs/incidents/` |
| `python run.py parallel` | 4 процесса: раздельные лимитеры против общего | `reports/parallel_limit.json` |
| `jupyter notebook analysis.ipynb` | таблицы и графики | — |

Код возврата 2 — в прогоне была блокировка или красный статус. Для сценария с капчей и 403 это ожидаемо.

## Сценарии
| id | Что воспроизводится | Где |
|---|---|---|
| S1_selector_timeout | таймаут ожидания селектора: `/js-delayed/` рисует цитаты через 12 с, ждём 3 с → повтор → деградация до `/api/quotes` | quotes.toscrape.com |
| S2_browser_down | браузер недоступен → весь сбор через HTTP JSON-эндпоинт | quotes.toscrape.com |
| S3_stub_pages | «тихая заглушка» 200 без цитат (реальная и синтетическая), капча (+ скриншот), 403 → остановка домена, остальные домены работают | сайт + стенд |
| S4_breaker_isolation | серия 503 на стенде → предохранитель размыкается только для него → пробный запрос | стенд + quotes.toscrape.com |
| S5_retry_policy | 429 с Retry-After (секунды / дата / без заголовка), 401 → обновление токена, 500, 404, таймаут | стенд + httpbin.org |

Стенд `src/stub_server.py` поднимается автоматически на 127.0.0.1:8765. Все его ответы синтетические
(заголовок `X-Synthetic: 1`).

## Структура
```
src/  limiter.py  robots.py  retry.py  breaker.py  detector.py  observability.py
      collect.py  stub_server.py  compare.py  analysis.py
state/   etags.json, watermark.json, http_cache/, robots.json, locks/
logs/    run_<id>.jsonl, incidents/
reports/ run_report_<id>.json, comparison.md, savings.json, parallel_limit.json, history.jsonl, *.png
nocode/  workflow.json, error_workflow.json, nocode_notes.md
legal_checklist.md   report.md   analysis.ipynb   config.yaml
```
