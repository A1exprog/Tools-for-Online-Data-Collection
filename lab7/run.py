"""Запуск прогонов ЛР7.

  python run.py robots              # robots.txt источников → state/robots.json
  python run.py A | B | C           # один прогон профиля (C можно с --run-id C_warm)
  python run.py abc                 # A, B, C_cold (с чистым state), C_warm + reports/comparison.md
  python run.py scenarios           # сценарии варианта 4 + повторы, предохранитель, детектор
  python run.py parallel            # 4 процесса: общий лимит домена против раздельных
  python run.py all                 # всё по порядку (≈ 10 минут)

Код возврата ≠ 0, если в прогоне была блокировка или итоговый статус «красный».
"""
import argparse
import json
import multiprocessing as mp
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

import robots as robots_mod  # noqa: E402
import stub_server  # noqa: E402
from collect import Browser, Pipeline, QuotesCollector, httpbin_set  # noqa: E402
from detector import compare_with_history  # noqa: E402
from limiter import SharedDomainLimiter, TokenBucket  # noqa: E402
from observability import StructLogger, build_report, evaluate_alerts  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
REPORTS, LOGS, STATE = ROOT / "reports", ROOT / "logs", ROOT / "state"
STUB_PORT = 8765
STUB = f"http://127.0.0.1:{STUB_PORT}"


def load_robots() -> dict:
    p = STATE / "robots.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else cmd_robots()


def cmd_robots() -> dict:
    q, h = CFG["sources"]["quotes"], CFG["sources"]["httpbin"]
    res = robots_mod.inspect_all({
        q: ["/js/page/1/", "/api/quotes", "/author/Albert-Einstein/", "/tag/love/", "/login"],
        h: ["/etag/lab7-a", "/cache", "/status/503", "/delay/3"],
    }, CFG["user_agent"], out=STATE / "robots.json")
    for host, info in res.items():
        print(f"{host}: HTTP {info['status']} — {info['interpretation']}")
        print(f"   can_fetch: {info['can_fetch']}")
        print(f"   crawl_delay={info['crawl_delay']} request_rate={info['request_rate']} sitemaps={info['sitemaps']}")
    return res


def finish(run_id: str, log: StructLogger, history_key: str, extra=None, collected_min=None) -> dict:
    log.close()
    rep = build_report(log.path, extra)
    hist_file = REPORTS / "history.jsonl"
    history = []
    if hist_file.exists():
        history = [json.loads(l) for l in hist_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        history = [h for h in history if h.get("history_key") == history_key]
    rep["history_key"] = history_key
    rep["history"] = compare_with_history(rep, history)
    alert_cfg = dict(CFG["alerts"], **({"collected_min": collected_min} if collected_min is not None else {}))
    rep["collected_min"] = alert_cfg["collected_min"]
    rep["alerts"], rep["status"] = evaluate_alerts(rep, alert_cfg)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"run_report_{run_id}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(hist_file, "a", encoding="utf-8") as f:
        f.write(json.dumps({k: rep[k] for k in ("run_id", "history_key", "started_at", "collected", "known",
                                                 "avg_response_bytes", "requests", "status")}, ensure_ascii=False) + "\n")
    print(f"  {run_id}: {rep['requests']} запросов, коды {rep['status_codes']}, повторов {rep['retries']}, "
          f"304: {rep['not_modified']}, трафик {rep['bytes_wire'] / 1024:.1f} КБ, {rep['duration_s']} с, "
          f"собрано {rep['collected']} (+{rep['known']} известных), статус {rep['status'].upper()}")
    for a in rep["alerts"]:
        print(f"    [{a['level']}] {a['metric']}: {a['reason']}")
    return rep


def clear_state():
    for p in (STATE / "etags.json", STATE / "watermark.json"):
        p.unlink(missing_ok=True)
    shutil.rmtree(STATE / "http_cache", ignore_errors=True)


def run_profile(name: str, run_id: str | None = None, clear=False) -> dict:
    run_id = run_id or name
    if clear:
        clear_state()
    prof = CFG["profiles"][name]
    print(f"\n=== {prof['label']} ({run_id}) ===")
    log = StructLogger(run_id, LOGS / f"run_{run_id}.jsonl")
    log.log("run_start", profile=name, label=prof["label"], flags=prof)
    pipe = Pipeline(run_id, prof, CFG, log, load_robots() if prof["limiter"] else None)
    browser = Browser(pipe, CFG["playwright"], prof["block_assets"])
    try:
        QuotesCollector(pipe, browser, CFG, prof).run()
        httpbin_set(pipe, CFG)
    finally:
        browser.close()
        pipe.save_state()
    log.log("run_end")
    # инкрементальный повтор: достаточно подтвердить первую страницу (10 цитат) — дальше старые данные
    return finish(run_id, log, run_id, collected_min=10 if run_id.endswith("warm") else None)


# ------------------------------------------------------------------ сценарии
def scenario(run_id, fn, expected_min, profile="B"):
    print(f"\n=== Сценарий {run_id} ===")
    prof = dict(CFG["profiles"][profile])
    log = StructLogger(run_id, LOGS / f"run_{run_id}.jsonl")
    log.log("run_start", profile=profile, scenario=run_id)
    pipe = Pipeline(run_id, prof, CFG, log, load_robots())
    try:
        extra = fn(pipe, log, prof) or {}
    finally:
        pipe.save_state()
    log.log("run_end")
    return finish(run_id, log, run_id, extra, collected_min=expected_min)


def s_selector_timeout(pipe, log, prof):
    """Таймаут ожидания селектора: /js-delayed/ рисует цитаты через 12 с, ждём 3 с → повтор → деградация."""
    cfg = json.loads(json.dumps(CFG))
    cfg["workload"].update(pages=1, page_path="js-delayed/page/{n}/?delay=12000", selector_timeout_ms=3000)
    browser = Browser(pipe, cfg["playwright"], block_assets=True)
    try:
        QuotesCollector(pipe, browser, cfg, dict(prof, watermark=False)).list_page(1)
    finally:
        browser.close()


def s_browser_down(pipe, log, prof):
    """Браузер недоступен целиком → весь сбор деградирует до JSON-эндпоинта /api/quotes."""
    cfg = json.loads(json.dumps(CFG))
    browser = Browser(pipe, cfg["playwright"], block_assets=True, fail_launch=True)
    c = QuotesCollector(pipe, browser, cfg, dict(prof, watermark=False))
    for n in range(1, cfg["workload"]["pages"] + 1):
        c.list_page(n)


def s_stub_pages(pipe, log, prof):
    """Детектор: реальная «пустая» страница сайта, синтетические капча / тихая заглушка / 403."""
    q = CFG["sources"]["quotes"]
    r = pipe.http(q + "tag/no-such-tag-lab7/", expected="quote")                 # реальный ответ 200 без цитат
    log.log("records", url=r.url, collected=0, discarded=1)
    for path in ("/stub/empty", "/stub/forbidden"):                              # 403 → домен 127.0.0.1 стоп
        r = pipe.http(STUB + path, expected="quote")
        log.log("records", url=r.url, collected=0, discarded=1)
    alt = f"http://localhost:{STUB_PORT}"                                         # тот же стенд под другим именем
    browser = Browser(pipe, CFG["playwright"], block_assets=True)                # капча в браузере → скриншот
    try:
        browser.load(alt + "/stub/captcha", ".quote", 2000)
        r = browser.load(alt + "/flaky", ".quote")                               # домен уже остановлен
        log.log("records", url=alt + "/flaky", collected=0, discarded=1, skipped=r.skipped)
    finally:
        browser.close()
    r = pipe.http(q + "author/Albert-Einstein/", expected="author-details")      # другой домен работает
    log.log("records", url=r.url, collected=int(r.ok), discarded=int(not r.ok))


def s_breaker(pipe, log, prof):
    """Изоляция отказа: стенд отвечает 503 серией, предохранитель размыкается только для него,
    сбор карточек авторов quotes.toscrape.com продолжается; после паузы — пробный запрос."""
    stub_server.FLAKY_FAILS = 4
    stub_server.STATE.reset()
    q = CFG["sources"]["quotes"]
    authors = ["Albert-Einstein", "J-K-Rowling", "Jane-Austen", "Marilyn-Monroe", "Andre-Gide", "Thomas-A-Edison",
               "Eleanor-Roosevelt", "Steve-Martin", "Bob-Marley", "Dr-Seuss", "Douglas-Adams", "Elie-Wiesel",
               "Friedrich-Nietzsche", "Mark-Twain", "Allen-Saunders", "Pablo-Neruda", "Ralph-Waldo-Emerson",
               "Mother-Teresa", "Garrison-Keillor", "Jim-Henson"]
    for a in authors:
        r = pipe.http(STUB + "/flaky", expected="quote")
        log.log("records", url=STUB + "/flaky", collected=int(r.ok), discarded=int(not r.ok), skipped=r.skipped)
        r = pipe.http(f"{q}author/{a}/", expected="author-details")
        log.log("records", url=r.url, collected=int(r.ok), discarded=int(not r.ok))
    return {"breaker_by_host": pipe.breaker.activations}


def s_retry(pipe, log, prof):
    """Политика повторов: 429 с Retry-After (секунды / дата / без заголовка), 401 с обновлением
    токена, 5xx и таймаут на httpbin.org, 404 без повторов."""
    pipe.breaker.threshold = 100   # здесь смотрим на повторы; предохранитель — в S4
    pipe.tokens["127.0.0.1:%d" % STUB_PORT] = "stale-token"
    pipe.token_refresh["127.0.0.1:%d" % STUB_PORT] = lambda: "fresh-token"
    h = CFG["sources"]["httpbin"]
    for url in (STUB + "/ratelimit", STUB + "/ratelimit-date", STUB + "/ratelimit-noheader", STUB + "/auth",
                h + "status/500", h + "status/404", h + "delay/12"):
        r = pipe.http(url, expected=None, min_size=0)
        log.log("records", url=url, collected=int(r.ok), discarded=int(not r.ok))


def cmd_scenarios():
    srv = stub_server.start(STUB_PORT)
    reps = {}
    try:
        # ожидаемый минимум записей: S1 — 1 страница (10), S2 — 3 страницы (30), S3 — 1 автор,
        # S4 — 20 авторов, S5 — 4 ресурса стенда после повторов
        reps["S1_selector_timeout"] = scenario("S1_selector_timeout", s_selector_timeout, 10)
        reps["S2_browser_down"] = scenario("S2_browser_down", s_browser_down, 30)
        reps["S3_stub_pages"] = scenario("S3_stub_pages", s_stub_pages, 1)
        reps["S4_breaker_isolation"] = scenario("S4_breaker_isolation", s_breaker, 20)
        reps["S5_retry_policy"] = scenario("S5_retry_policy", s_retry, 4)
    finally:
        srv.shutdown()
    return reps


# ------------------------------------------------------------------ 4 процесса
def _worker_shared(i, n, rate, out):
    lim = SharedDomainLimiter(STATE / "locks", rate=rate, jitter=(0.0, 0.0))
    for _ in range(n):
        lim.wait("quotes.toscrape.com")
        out.append(time.time())


def _worker_local(i, n, rate, out):
    b = TokenBucket(rate, 1)
    for _ in range(n):
        w = b.reserve()
        if w > 0:
            time.sleep(w)
        out.append(time.time())


def cmd_parallel(rate=2.0, n=10):
    """Без сети: только моменты разрешения запросов. Раздельные лимитеры в 4 процессах дают ×4."""
    res = {}
    shutil.rmtree(STATE / "locks", ignore_errors=True)
    for name, fn in (("separate_limiters", _worker_local), ("shared_limiter", _worker_shared)):
        with mp.Manager() as m:
            out = m.list()
            procs = [mp.Process(target=fn, args=(i, n, rate, out)) for i in range(4)]
            t0 = time.time()
            [p.start() for p in procs]
            [p.join() for p in procs]
            ts = sorted(out)
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        span = ts[-1] - ts[0]
        res[name] = {"processes": 4, "requests": len(ts), "target_rate": rate,
                     "actual_rate": round((len(ts) - 1) / span, 2) if span else None,
                     "min_gap_s": round(min(gaps), 3), "span_s": round(span, 2),
                     "start_delay_s": round(ts[0] - t0, 2)}
        print(f"  {name}: {res[name]}")
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "parallel_limit.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


# ------------------------------------------------------------------ сравнение
def cmd_abc():
    reps = {"A": run_profile("A"), "B": run_profile("B"),
            "C_cold": run_profile("C", "C_cold", clear=True), "C_warm": run_profile("C", "C_warm")}
    import compare
    compare.write(reps, REPORTS / "comparison.md")
    return reps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="all",
                    choices=["robots", "A", "B", "C", "abc", "scenarios", "parallel", "all"])
    ap.add_argument("--run-id")
    ap.add_argument("--clear-state", action="store_true")
    a = ap.parse_args()
    reps = {}
    if a.target in ("robots", "all"):
        cmd_robots()
    if a.target in ("A", "B", "C"):
        reps[a.target] = run_profile(a.target, a.run_id, a.clear_state)
    if a.target in ("abc", "all"):
        reps.update(cmd_abc())
    if a.target in ("scenarios", "all"):
        reps.update(cmd_scenarios())
    if a.target in ("parallel", "all"):
        cmd_parallel()
    if a.target == "all":
        import analysis
        analysis.main()
    bad = [k for k, r in reps.items() if isinstance(r, dict) and (r.get("status") == "red" or
                                                                   r.get("detector", {}).get("блокировка"))]
    if bad:
        print(f"\nКод возврата 2: блокировка или красный статус в прогонах {bad}"
              + (" — для сценариев сбоев это ожидаемо" if a.target in ("scenarios", "all") else ""))
        sys.exit(2)


if __name__ == "__main__":
    main()
