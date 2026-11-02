"""Локальный стенд-заглушка (127.0.0.1) для воспроизведения сбоев, которые нельзя
честно получить от учебных сайтов: капча, 403, серия 503, 429 с Retry-After, 401.
Все ответы синтетические и помечены заголовком X-Synthetic: 1.

Маршруты:
  /stub/captcha        200, страница «проверка: вы не робот» (маркер captcha)
  /stub/empty          200, «тихая заглушка»: нормальная вёрстка, но без цитат
  /stub/forbidden      403 «Access denied»
  /flaky               503 первые FLAKY_FAILS обращений, потом 200 с цитатой
  /ratelimit           429 + Retry-After: 2 (секунды) один раз, потом 200
  /ratelimit-date      429 + Retry-After: <HTTP-дата через 3 с> один раз, потом 200
  /ratelimit-noheader  429 без Retry-After один раз, потом 200
  /auth                401, пока не придёт заголовок X-Token: fresh-token
  /reset               сбросить счётчики
"""
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FLAKY_FAILS = 6
QUOTE_PAGE = ('<html><body><div class="container"><div class="quote"><span class="text">'
              '“A synthetic but healthy quote.”</span><small class="author">Stub Author</small>'
              '</div>' + '<p>' + 'padding ' * 300 + '</p></div></body></html>')
PAGES = {
    "/stub/captcha": (200, '<html><body><h1>Проверка: вы не робот?</h1><div class="g-recaptcha">captcha</div>'
                           '<p>Мы заметили подозрительную активность.</p></body></html>'),
    "/stub/empty": (200, '<html><body><div class="container"><div class="col-md-8">No quotes found!</div>'
                         + '<p>' + 'layout ' * 300 + '</p></div></body></html>'),
    "/stub/forbidden": (403, "<html><body><h1>403 Forbidden</h1><p>Access denied.</p></body></html>"),
}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.hits = {}

    def hit(self, path):
        with self.lock:
            self.hits[path] = self.hits.get(path, 0) + 1
            return self.hits[path]


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, status, body, headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Synthetic", "1")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        n = STATE.hit(path)
        if path == "/reset":
            STATE.reset()
            return self.send(200, "reset")
        if path in PAGES:
            return self.send(*PAGES[path])
        if path == "/flaky":
            if n <= FLAKY_FAILS:
                return self.send(503, "<h1>503 Service Unavailable</h1>")
            return self.send(200, QUOTE_PAGE)
        if path == "/ratelimit":
            return self.send(429, "slow down", {"Retry-After": "2"}) if n == 1 else self.send(200, QUOTE_PAGE)
        if path == "/ratelimit-date":
            if n == 1:
                when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=3), usegmt=True)
                return self.send(429, "slow down", {"Retry-After": when})
            return self.send(200, QUOTE_PAGE)
        if path == "/ratelimit-noheader":
            return self.send(429, "slow down") if n == 1 else self.send(200, QUOTE_PAGE)
        if path == "/auth":
            if self.headers.get("X-Token") == "fresh-token":
                return self.send(200, QUOTE_PAGE)
            return self.send(401, "token expired", {"WWW-Authenticate": 'Bearer error="invalid_token"'})
        self.send(404, "not found")


def start(port: int = 8765):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    s = start()
    print("stub server on http://127.0.0.1:8765 (Ctrl+C — стоп)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.shutdown()
