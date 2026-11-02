import scrapy


class QuoteItem(scrapy.Item):
    """Цитата в выдаче конкретного тега (уровень 2: /tag/<tag>/page/N/).

    Одна и та же цитата встречается на страницах нескольких тегов, поэтому
    запись = «цитата + тег, через который её нашли». Бизнес-ключ:
    автор + цитата + source_tag.
    """

    quote = scrapy.Field()
    author = scrapy.Field()
    tags = scrapy.Field()          # все теги цитаты, list[str]
    source_tag = scrapy.Field()    # тег уровня обхода (из cb_kwargs)
    tag_page = scrapy.Field()      # номер страницы внутри тега (из cb_kwargs)
    author_url = scrapy.Field()    # ссылка на карточку автора (связь с AuthorItem)
    business_key = scrapy.Field()


class AuthorItem(scrapy.Item):
    """Карточка автора (/author/<slug>/). Связывается с QuoteItem по author_url."""

    author = scrapy.Field()
    author_url = scrapy.Field()
    author_born_date = scrapy.Field()
    author_born_place = scrapy.Field()
    author_bio = scrapy.Field()
    business_key = scrapy.Field()
