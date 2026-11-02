"""Детектор блокировок и деградации: код ответа + содержимое + размер + ожидаемые элементы,
сохранение артефактов инцидента и сравнение прогона с историей."""
import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

MARKERS = [
    "captcha", "recaptcha", "hcaptcha", "access denied", "are you a robot", "unusual traffic",
    "доступ ограничен", "доступ запрещён", "подозрительная активность", "too many requests",
]
OK, SUSPECT, BLOCK = "ok", "подозрение", "блокировка"


def check(status: int | None, body: str, expected: str | None = None, min_size: int = 1500,
          kind: str = "html") -> tuple[str, list[str]]:
    """expected — CSS-класс/ключ, который обязан быть в нормальном ответе ('quote', 'author-details')."""
    reasons = []
    text = (body or "").lower()
    if status == 403:
        reasons.append("HTTP 403")
    if status == 429:
        reasons.append("HTTP 429")
    found = [m for m in MARKERS if m in text]
    if found:
        reasons.append("маркеры: " + ", ".join(found))
    if status and status < 400:
        if len(body or "") < min_size:
            reasons.append(f"аномально малый ответ: {len(body or '')} Б < {min_size} Б")
        if expected:
            if kind == "json":
                try:
                    data = json.loads(body)
                    if not data.get(expected):
                        reasons.append(f"в JSON нет непустого поля '{expected}'")
                except (ValueError, AttributeError):
                    reasons.append("ответ не является JSON")
            elif not re.search(rf'class=["\'][^"\']*\b{re.escape(expected)}\b', body or ""):
                reasons.append(f"код {status}, но нет ожидаемого элемента .{expected}")
    if status == 403 or found:
        return BLOCK, reasons
    if reasons:
        return SUSPECT, reasons
    return OK, reasons


def save_incident(run_id: str, url: str, status, headers: dict, body: str, verdict: str, reasons: list,
                  screenshot: bytes | None = None, root="logs/incidents") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    d = Path(root) / f"{run_id}_{ts}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "body.html").write_text(body or "", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({
        "run_id": run_id, "url": url, "status": status, "verdict": verdict, "reasons": reasons,
        "headers": dict(headers or {}), "saved_at": ts}, ensure_ascii=False, indent=2), encoding="utf-8")
    if screenshot:
        (d / "screenshot.png").write_bytes(screenshot)
    return str(d)


def compare_with_history(current: dict, history: list[dict], warn=0.20, crit=0.30) -> dict:
    """Сравнивает объём записей и средний размер ответа с медианой прошлых прогонов того же профиля."""
    out = {"status": "green", "checks": []}
    if not history:
        out["note"] = "истории нет — сравнивать не с чем"
        return out
    for key in ("collected", "avg_response_bytes"):
        past = [h[key] for h in history if h.get(key)]
        if not past or current.get(key) is None:
            continue
        med = statistics.median(past)
        dev = abs(current[key] - med) / med if med else 0.0
        st = "red" if dev > crit else "yellow" if dev > warn else "green"
        out["checks"].append({"metric": key, "current": current[key], "median": med,
                              "deviation": round(dev, 3), "status": st})
        if st == "red" or (st == "yellow" and out["status"] == "green"):
            out["status"] = st
    return out
