# Паспорта источников

> Значения, помеченные «⚑ проверить», зависят от текущего состояния сайта: подтвердите их
> командой `python -m src.diagnose` (результат — `docs/diagnostics/diagnostic_<дата>.json`)
> и при необходимости поправьте таблицу.

## 1. HTML: scrapingcourse.com/ecommerce

| Параметр | Значение |
|---|---|
| Тип | Статический HTML-каталог (WordPress + WooCommerce), учебная площадка для отработки скрейпинга |
| Точка входа | `https://www.scrapingcourse.com/ecommerce/` |
| Пагинация | ссылка `a.next.page-numbers` → `/ecommerce/page/N/`; конец — ссылки нет; предохранитель `max_pages: 30` |
| Объём | 188 товаров («Showing 1–16 of 188 results»), 16 на странице → 12 страниц листинга + 188 карточек |
| Что берём из листинга | `li.product`: ссылка, название (`.product-name`), цена (`span.price`, со скидкой — `del`/`ins`), `data-product_id`, `data-product_sku`, классы `instock`/`outofstock`, `product_cat-*`, `product-type-*` |
| Что берём из карточки | `h1.product_title`, `p.price`, `.sku`, категории `.posted_in a`, теги, `p.stock`, число вариаций (`form.variations_form[data-product_variations]`), атрибуты (размеры/цвета) |
| Формат ответа | HTML, UTF-8, gzip |
| robots.txt | `User-agent: *` / `Disallow: /ecommerce/*` — **раздел формально закрыт для роботов**; Crawl-delay и Sitemap не указаны. Собираем по явному задокументированному исключению (`config.yaml → http.robots_exceptions`, `docs/legal_checklist.md`, п. 4) |
| Лимиты | явно не объявлены → собственный лимит 1 запрос/с + джиттер 0–0,5 с; Crawl-delay из robots.txt имеет приоритет |
| Условные запросы | сервер (за Cloudflare) **не отдаёт** ETag/Last-Modified → 304 не бывает; работает запасной механизм — sha256 страницы в `state/etags.json` (страница не изменилась → в raw ничего не дописывается) |
| Sitemap | `/ecommerce/wp-sitemap.xml` → `wp-sitemap-posts-product-1.xml`: 188 товаров, у всех есть `lastmod` — водяной знак карточек |
| Наличие | у вариативных товаров (размер × цвет) WooCommerce ставит на товар класс `outofstock`, реальное наличие — у вариаций (`data-product_variations[].is_in_stock`, «100 in stock»); парсер 1.1.0 считает наличие по вариациям |
| Ключ уникальности | slug товара из URL → бизнес-ключ `scrapingcourse:<slug>` |
| Персональные данные | нет (каталог товаров) |
| Нужен ли браузер | нет — см. `docs/diagnostics.md` |

## 2. REST API: DummyJSON `/products`

| Параметр | Значение |
|---|---|
| Тип | Открытый REST API без ключа, учебный набор данных магазина |
| Точка входа | `https://dummyjson.com/products?limit=50&skip=N&select=...` |
| Пагинация | `limit`/`skip`, конец — `skip >= total` (`total` = 194 по документации) |
| Объём | 194 товара × 3 отзыва ≈ 582 отзыва |
| Поля (`select`) | `id, title, description, category, brand, price, discountPercentage, rating, stock, availabilityStatus, sku, tags, minimumOrderQuantity, reviews, meta` (картинки и габариты не запрашиваем — меньше трафика) |
| Формат ответа | JSON `{products: [...], total, skip, limit}`; отзывы вложены в товар |
| Лимиты | явно не документированы → собственный лимит 2 запроса/с + джиттер; реакция на 429 по `Retry-After` |
| Условные запросы | ETag → `If-None-Match` → 304 (⚑ проверить в diagnostic); водяной знак `meta.updatedAt` + хэш товара |
| Ключ уникальности | `id` → `dummyjson:<id>`; отзыв — `dummyjson:<id>:<hash(rating, comment, date)>` |
| Персональные данные | **есть в отзывах**: `reviewerName`, `reviewerEmail` (вымышленные, но по форме — ПДн). **Отбрасываются при сборе**, в raw не попадают |

## 3. Как источники дополняют друг друга

| Признак | HTML (scrapingcourse) | API (DummyJSON) |
|---|---|---|
| Цена, валюта USD | есть | есть |
| Цена до скидки / скидка | есть только у товаров на распродаже (`del`/`ins`) | `discountPercentage` у всех |
| Наличие | класс `instock`/`outofstock`, текст `p.stock` | `stock`, `availabilityStatus` |
| Категории | иерархия WooCommerce (Men › Tops › Hoodies …) | 24 плоских slug (`mens-shirts`, `womens-bags`, …) |
| Рейтинг, отзывы | нет / почти нет | рейтинг + 3 отзыва на товар |
| Бренд | нет | есть |

**Связь:** единые товарные группы (`product_groups`): `tops`, `bottoms`, `outerwear`, `bags`, `watches`,
`fitness`, `shoes`, `dresses`, `other`. Правила — в `config.yaml → linkage`. Сопоставление
проверяется метриками `group_overlap_count` и `html_unmapped_group_pct`.

## 4. Если источник недоступен

| Сценарий | Поведение |
|---|---|
| API недоступен (есть данные прошлых прогонов) | инцидент, `source_status = FAILED`, оповещение `source_failed` (critical); чистый слой строится из последней успешной версии raw → публикация продолжается, `exit 1` |
| API недоступен при первом запуске | `min_products_per_source` = 0 → качество RED → БД и mart не обновляются, `exit 3` |
| HTML недоступен | симметрично; при сбое отдельных карточек — инцидент на карточку, листинг всё равно сохраняется |
| Оба недоступны | чистый слой из raw прошлых прогонов или пуст → RED / `exit 2` |
