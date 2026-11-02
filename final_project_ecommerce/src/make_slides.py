"""Генерация slides.pdf (6 слайдов для защиты) из реальных результатов прогона.

    python -m src.make_slides          # после run_pipeline и analysis.ipynb

Числа берутся из reports/, mart/ и БД — слайды всегда соответствуют последнему прогону.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
import yaml  # noqa: E402

W, H = 13.33, 7.5
ACCENT = "#1f4e79"


def _slide(pdf: PdfPages, title: str, draw) -> None:
    fig = plt.figure(figsize=(W, H))
    fig.patch.set_facecolor("white")
    fig.text(0.04, 0.92, title, fontsize=24, weight="bold", color=ACCENT)
    fig.add_artist(plt.Line2D([0.04, 0.96], [0.9, 0.9], color=ACCENT, lw=2))
    draw(fig)
    pdf.savefig(fig)
    plt.close(fig)


def _bullets(fig, items, x=0.05, y=0.82, size=15, width=95, gap=0.012):
    for it in items:
        lines = textwrap.wrap(it, width)
        fig.text(x, y, "• " + "\n  ".join(lines), fontsize=size, va="top")
        y -= 0.045 * len(lines) + gap
    return y


def _table(fig, rows, cols, box, size=11, widths=None):
    ax = fig.add_axes(box)
    ax.axis("off")
    t = ax.table(cellText=rows, colLabels=cols, loc="upper left", cellLoc="left", colWidths=widths)
    t.auto_set_font_size(False)
    t.set_fontsize(size)
    t.scale(1, 1.45)
    for (r, _), cell in t.get_celld().items():
        if r == 0:
            cell.set_facecolor("#dbe5f1")
            cell.set_text_props(weight="bold")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="slides.pdf")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    st = cfg["storage"]
    reports, mart = Path(st["reports_dir"]), Path(st["mart_dir"])
    run = json.loads((reports / "run_report_latest.json").read_text(encoding="utf-8"))
    quality = json.loads(sorted(reports.glob("quality_*.json"))[-1].read_text(encoding="utf-8"))
    demo_p = reports / "idempotency_demo.json"
    demo = json.loads(demo_p.read_text(encoding="utf-8")) if demo_p.exists() else []
    con = sqlite3.connect(st["db_path"])
    overview = con.execute("SELECT source, n_products, n_groups, avg_price, in_stock_pct FROM v_source_overview").fetchall()
    n_rev = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    con.close()
    conclusions = (mart / "conclusions.txt").read_text(encoding="utf-8").split("\n\n") \
        if (mart / "conclusions.txt").exists() else ["Запустите analysis.ipynb, чтобы сформировать выводы."]

    with PdfPages(args.out) as pdf:
        # 1. Вопрос
        def s1(fig):
            fig.text(0.05, 0.78, "Исследовательский вопрос", fontsize=18, weight="bold")
            fig.text(0.05, 0.72, textwrap.fill(
                "Как соотносятся уровень цен и структура ассортимента двух учебных интернет-магазинов "
                "(scrapingcourse.com — HTML, DummyJSON — REST API) в сопоставимых товарных группах, и связаны "
                "ли цена и скидка с рейтингом товара и оценками в отзывах?", 100), fontsize=16, va="top")
            _bullets(fig, [
                f"Чистый слой: {sum(r[1] for r in overview)} товаров (2 источника) + {n_rev} отзывов — две связанные сущности",
                "Связь источников: единые товарные группы (bags, watches, tops, fitness …)",
                "Конфигурация B (гибридная): Python собирает и проверяет, n8n — расписание и оповещения",
            ], y=0.5, size=15)
        _slide(pdf, "Гибридный пайплайн сбора данных: два магазина", s1)

        # 2. Источники и инструменты
        def s2(fig):
            _table(fig, [
                ["scrapingcourse.com", "HTML, WooCommerce", "requests + BS4", "a.next → /page/N/", "slug товара", "ETag/304, sitemap lastmod"],
                ["dummyjson.com", "REST API, JSON", "requests", "limit / skip, total", "id товара", "ETag/304, updatedAt+хэш"],
            ], ["Источник", "Тип", "Инструмент", "Пагинация", "Бизнес-ключ", "Инкрементальность"], [0.03, 0.6, 0.94, 0.25], 12,
               widths=[0.22, 0.15, 0.12, 0.15, 0.11, 0.25])
            for t in fig.axes[-1].tables:
                t.scale(1, 1.6)
            _bullets(fig, [
                "Браузер не нужен: карточки, цены и ссылка «next» есть в исходном HTML (src/diagnose.py)",
                "Scrapy не нужен: ~200 запросов на один домен; вежливость, повторы и предохранитель — в PoliteClient",
                "ПДн авторов отзывов (имя, e-mail) отбрасываются ещё при сборе",
            ], y=0.42, size=14)
        _slide(pdf, "Источники и выбор инструментов", s2)

        # 3. Архитектура
        def s3(fig):
            steps = ["Источники\nHTML + API", "Сбор\nPolite-\nClient", "raw/\nJSONL\n(append)", "clean/\nParquet +\nrejects",
                     "Качество\n11 метрик", "SQLite\nUPSERT", "mart/\nпредстав-\nления", "n8n\nрасписание,\nTelegram"]
            for i, s in enumerate(steps):
                x = 0.035 + i * 0.118
                fig.patches.append(matplotlib.patches.FancyBboxPatch((x, 0.55), 0.094, 0.2, boxstyle="round,pad=0.005",
                                   transform=fig.transFigure, facecolor="#dbe5f1", edgecolor=ACCENT))
                fig.text(x + 0.047, 0.65, s, ha="center", va="center", fontsize=10.5)
                if i < len(steps) - 1:
                    fig.text(x + 0.099, 0.65, "→", fontsize=16, va="center", color=ACCENT)
            _bullets(fig, [
                "Вежливость: лимит на домен + джиттер, Crawl-delay, robots.txt, User-Agent с контактом",
                "Устойчивость: повторы только для 429/5xx/таймаутов, приоритет Retry-After, circuit breaker на домен",
                "Возобновляемость: state/checkpoint.json + водяные знаки — прерванный прогон продолжается с места остановки",
                "Наблюдаемость: logs/run_*.jsonl (каждый запрос), reports/run_report_*.json, alerts, incidents",
            ], y=0.45, size=14)
        _slide(pdf, "Архитектура пайплайна", s3)

        # 4. Качество и идемпотентность
        def s4(fig):
            rows = [[m["name"], f"{m['value']:g}", f"{m['warn']:g} / {m['critical']:g}", m["level"]] for m in quality["metrics"]]
            _table(fig, rows, ["Метрика", "Значение", "WARN / CRIT", "Итог"], [0.04, 0.08, 0.45, 0.78], 10,
                   widths=[0.48, 0.17, 0.2, 0.15])
            fig.text(0.53, 0.84, f"Вердикт качества: {quality['status']}", fontsize=16, weight="bold")
            if demo:
                drows = [[d["run"], d["requests"], d["http_304"], d["inserted"], d["updated"], d["db_products"], d["duplicate_keys"]] for d in demo]
                _table(fig, drows, ["Прогон", "Запросов", "304", "Insert", "Update", "COUNT(*)", "Дубли"], [0.53, 0.45, 0.43, 0.35], 11)
            fig.text(0.53, 0.38, textwrap.fill(
                f"Последний прогон: {run['requests_total']} запросов, коды {run['status_codes']}, ошибок {run['error_rate']:.1%}, "
                f"p50 {run['latency_median_ms']} мс, p95 {run['latency_p95_ms']} мс, {run['duration_sec']} c", 55), fontsize=12, va="top")
        _slide(pdf, "Качество данных и идемпотентность", s4)

        # 5. Результаты
        def s5(fig):
            img = mart / "fig_1_price_by_group.png"
            if img.exists():
                ax = fig.add_axes([0.03, 0.08, 0.5, 0.78])
                ax.imshow(plt.imread(img))
                ax.axis("off")
            y = 0.84
            for c in conclusions[:3]:
                lines = textwrap.wrap(c, 62)
                fig.text(0.55, y, "\n".join(lines), fontsize=11, va="top")
                y -= 0.032 * len(lines) + 0.04
        _slide(pdf, "Выводы", s5)

        # 6. Правомерность и ограничения
        def s6(fig):
            _bullets(fig, [
                "Учебные площадки, созданные для отработки сбора данных; без авторизации и обхода защиты",
                "robots.txt (RFC 9309): у scrapingcourse Disallow: /ecommerce/* — собираем по явному "
                "задокументированному исключению для учебной площадки (логируется в каждом прогоне)",
                "robots.txt и Crawl-delay соблюдаются; ≤ 1 запрос/с к HTML, ≤ 2 запроса/с к API, ~20 запросов в повторных прогонах",
                "Персональные данные не хранятся (reviewerName/reviewerEmail удаляются при сборе)",
                "Ограничения: синтетические цены и отзывы, малые выборки в общих группах, сопоставление по правилам, "
                "рейтинг есть только в API, каталоги статичны",
                "Демонстрация: (1) повторный прогон → 304, 0 вставок; (2) --simulate-failure api → инцидент, exit ≠ 0",
            ], size=15)
        _slide(pdf, "Правомерность, ограничения, демонстрация", s6)
    print(f"Сохранено: {args.out}")


if __name__ == "__main__":
    main()
