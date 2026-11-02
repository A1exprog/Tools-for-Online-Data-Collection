# Протокол диагностики HTML-источника: нужен ли браузер?

Повторяет ручную процедуру занятия 4 и автоматизирован в `src/diagnose.py`
(результат сохраняется в `docs/diagnostics/diagnostic_<дата>.json`).

| Шаг | Что делаем | Критерий «браузер не нужен» |
|---|---|---|
| 1. View Source | `requests.get("https://www.scrapingcourse.com/ecommerce/")` без JS; ищем `li.product` | карточки есть в исходном HTML (ожидаем 16) |
| 2. Поиск текста | ищем в «сыром» HTML названия (`.product-name`) и цены (`.price`) | названия и цены найдены у всех карточек |
| 3. Network / XHR | ищем маркеры клиентского рендеринга (`__NEXT_DATA__`, `data-reactroot`, `__NUXT__`, infinite-scroll, load-more); проверяем ссылку «next» | маркеров нет, пагинация — обычные ссылки `/page/N/` |
| 4. Заголовки и robots.txt | `ETag`, `Last-Modified`, `Content-Encoding`, `Cache-Control`; robots.txt с вердиктом по RFC 9309 | — (для инкрементальности, вежливости и правовой части) |

**Фактический результат (27.10.2026):** 16 карточек, 16 названий и цен в исходном HTML, JS-маркеров нет,
`browser_not_needed`. Заголовки: `ETag` и `Last-Modified` отсутствуют (сайт за Cloudflare), gzip есть.
robots.txt: `User-agent: * / Disallow: /ecommerce/*` → точка входа **запрещена** правилом
`/ecommerce/*` (см. `docs/legal_checklist.md`, п. 4). DummyJSON: ETag есть, повтор с `If-None-Match` → 304,
`X-RateLimit-Limit: 100`.

**Вывод:** каталог `/ecommerce/` отдаёт полный HTML на сервере (WooCommerce); динамические стенды
сайта (`/infinite-scrolling`, `/button-click`, `/javascript-rendering`) мы **не** используем. Поэтому
достаточно `requests + BeautifulSoup`, а `collect_dynamic.py` (Playwright) не нужен — это и есть
«доказательство, что он не нужен» (ТЗ, занятие 4).

**Найденный XHR/JSON (плюс на защите):** WordPress публикует REST-API (`<link rel="https://api.w.org/">`,
WooCommerce Store API `/wp-json/wc/store/products`). Мы осознанно собираем именно HTML — задание требует
HTML-источник, а парсинг вёрстки с запасными селекторами демонстрирует навыки занятия 2.

## Как запустить

```bash
python -m src.diagnose
```

Пример вывода:

```
HTML https://www.scrapingcourse.com/ecommerce/: status 200, карточек в исходном HTML: 16, названий: 16, JS-маркеры: []
Вердикт: browser_not_needed
API https://dummyjson.com/products?limit=1&select=id%2Ctitle%2Cprice: status 200, total=194, ETag='W/"..."', повтор с If-None-Match -> 304
```

> Цифры выше — ожидаемые. Сохраните свой реальный `diagnostic_<дата>.json` и скриншоты DevTools
> (View Source, вкладка Network с фильтром Fetch/XHR) в `docs/diagnostics/` — это доказательство для защиты.
