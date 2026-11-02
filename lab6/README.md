# ЛР6, вариант 4 — погода и наблюдения: raw → clean → mart

Источники:
- **HTML** — справочник городов России с координатами https://time-in.ru/coordinates/russia;
- **API** — суточный архив погоды Open-Meteo https://archive-api.open-meteo.com: 25 городов × 30 дней.

## Воспроизведение с нуля
```powershell
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

python src\collect.py             # 1. сбор в raw/ (только дозапись), ~1 мин
python run_all.py                 # 2. всё остальное + выполнение quality_report.ipynb
```
`run_all.py` по шагам (каждый можно запустить отдельно):

| Шаг | Команда | Результат |
|---|---|---|
| синтетическая порча 7 % | `python src/corrupt.py` | `raw/*_synthetic.jsonl` (исходные raw не меняются) |
| пересборка clean из raw | `python src/rebuild.py` | `clean/dataset.parquet`, `locations.parquet`, `rejects.jsonl`, `protocol.csv` |
| сравнение форматов | `python src/formats.py` | `clean/format_benchmark.csv` |
| загрузка в БД | `python db/load.py` | `db/warehouse.db` |
| три прогона + ограничения | `python db/load.py --demo` | `db/idempotency_demo.db`, `db/idempotency_demo.json` |
| метрики и светофор | `python src/quality.py` | `clean/quality_metrics.csv`, `quality_verdict.json` |
| mart | `python src/mart.py` | `mart/views/*.parquet`, `mart/snapshots/dt=…/`, графики |

## Структура
```
raw/        сырые JSON Lines с метаданными (_source, _source_url, _collected_at, _parser_version, _status, _run_id)
clean/      чистый слой (Parquet), rejects, протокол, метрики, сравнение форматов
mart/       views/*.parquet, snapshots/dt=YYYY-MM-DD/, графики
db/         schema.sql, load.py, warehouse.db
src/        collect, corrupt, clean, contract, rebuild, formats, quality, mart
nocode/     workflow.json (n8n), schema_postgres.sql, docker-compose.yml
quality_report.ipynb   протокол, метрики, графики
report.md              отчёт
```

## No-code (n8n + PostgreSQL)
```powershell
cd nocode
docker compose up -d              # n8n на http://localhost:5678, Postgres со схемой schema_postgres.sql
```
В n8n: Workflows → Import from File → `nocode/workflow.json`. В узлах Postgres создайте учётные
данные: Host `postgres`, Database `lab6`, User `lab6`, Password `lab6`. Затем Execute workflow.
Результат — таблицы в Postgres и `nocode/nocode_result.csv`.
