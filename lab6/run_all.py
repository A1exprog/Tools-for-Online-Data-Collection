"""Полный прогон пайплайна после сбора (python src/collect.py):

  1. синтетическая порча (отдельные raw/*_synthetic.jsonl, исходные raw не трогаются)
  2. пересборка clean из raw — два раза подряд, сравнение контрольных сумм
  3. сравнение форматов CSV / JSONL / Parquet
  4. загрузка в db/warehouse.db (UPSERT) + демонстрация трёх прогонов и ограничений
  5. метрики качества и светофор
  6. mart: представления, партиции, графики (только если светофор не красный)
  7. выполнение quality_report.ipynb (если установлен jupyter)

Запуск из корня проекта: python run_all.py
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "db"))


def banner(t):
    print("\n" + "=" * 80 + f"\n{t}\n" + "=" * 80)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # кириллица и символы в консоли Windows


def main():
    import corrupt, rebuild, formats, quality, mart  # noqa: E401
    import load

    banner("1. Синтетическая порча")
    corrupt.main()

    banner("2. Пересборка clean из raw (без обращения к сайтам)")
    first = rebuild.rebuild(verbose=False)
    second = rebuild.rebuild(verbose=True)
    same = (first["fingerprint_observations"] == second["fingerprint_observations"]
            and first["fingerprint_locations"] == second["fingerprint_locations"])
    check = {"first": first, "second": second, "identical": same}
    (ROOT / "clean" / "rebuild_check.json").write_text(json.dumps(check, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nДве пересборки подряд дали одинаковый результат: {same}")

    banner("3. Сравнение форматов")
    formats.main()

    banner("4. Загрузка в БД")
    load.main_load()
    banner("4a. Идемпотентность: три прогона + ограничения (db/idempotency_demo.db)")
    load.demo()

    banner("5. Метрики качества")
    df, meta = quality.compute()
    quality.save(df, meta)
    print(df.to_string(index=False))
    print(f"\nИтоговый вердикт: {meta['verdict'].upper()}")

    banner("6. Mart")
    mart.build()

    banner("7. Ноутбук")
    if shutil.which("jupyter"):
        subprocess.run(["jupyter", "nbconvert", "--to", "notebook", "--execute", "--inplace",
                        str(ROOT / "quality_report.ipynb")], check=False)
        print("quality_report.ipynb выполнен")
    else:
        print("jupyter не найден — откройте quality_report.ipynb и выполните все ячейки (Run All)")


if __name__ == "__main__":
    main()
