"""Контракт данных чистого слоя (pydantic v2).

Две связанные сущности:
* CleanProduct — товар из любого источника (бизнес-ключ «источник:идентификатор»);
* CleanReview  — отзыв о товаре DummyJSON (внешний ключ product_key -> CleanProduct).

Собственные валидаторы (ТЗ: ≥ 2):
 1. normalize_title           — канонизация пробелов, запрет «пустых» названий;
 2. price_finite_2dp          — цена конечна и округлена до центов;
 3. check_price_consistency   — регулярная цена ≥ текущей, скидка согласована с ценами;
 4. check_stock_consistency   — stock_qty = 0 несовместим с in_stock = True;
 5. collected_not_in_future   — дата сбора не в будущем (защита от сбоя часов);
 6. group_known               — товарная группа из закрытого списка;
 7. comment_without_pii       — в тексте отзыва нет e-mail/телефона (ПДн не храним);
 8. review_date_iso           — дата отзыва в ISO-8601 и не в будущем.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KNOWN_GROUPS = {"tops", "bottoms", "outerwear", "bags", "watches", "fitness", "shoes", "dresses", "other"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+?\d[\d\s()-]{8,}\d")
KEY_RE = r"^(scrapingcourse|dummyjson):[A-Za-z0-9._%-]+$"


def parse_iso(v: str) -> datetime:
    """ISO-8601, в т.ч. с суффиксом Z (Python 3.10 его не понимает сам)."""
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class CleanProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_key: str = Field(..., pattern=KEY_RE)
    source: Literal["scrapingcourse.com", "dummyjson.com"]
    source_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=2, max_length=300)
    brand: Optional[str] = None
    category_raw: Optional[str] = None
    product_group: str
    price_usd: float = Field(..., gt=0, le=100_000)
    regular_price_usd: Optional[float] = Field(None, gt=0, le=100_000)
    discount_pct: Optional[float] = Field(None, ge=0, le=100)
    rating: Optional[float] = Field(None, ge=0, le=5)
    review_count: int = Field(0, ge=0)
    stock_qty: Optional[int] = Field(None, ge=0)
    in_stock: bool
    sku: Optional[str] = None
    url: Optional[str] = None
    collected_at: str
    parser_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    content_hash: str = Field(..., min_length=8)
    extra: dict[str, Any] = Field(default_factory=dict)   # -> колонка extra_json (JSON)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, v: str) -> str:
        v = " ".join(v.split())
        if not re.search(r"[A-Za-zА-Яа-я]", v):
            raise ValueError("title has no letters")
        return v

    @field_validator("price_usd", "regular_price_usd")
    @classmethod
    def price_finite_2dp(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError("price must be finite")
        return round(float(v), 2)

    @field_validator("collected_at")
    @classmethod
    def collected_not_in_future(cls, v: str) -> str:
        if parse_iso(v) > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("collected_at is in the future")
        return v

    @field_validator("product_group")
    @classmethod
    def group_known(cls, v: str) -> str:
        if v not in KNOWN_GROUPS:
            raise ValueError(f"unknown product_group: {v}")
        return v

    @model_validator(mode="after")
    def check_price_consistency(self) -> "CleanProduct":
        if self.regular_price_usd is not None and self.regular_price_usd + 0.005 < self.price_usd:
            raise ValueError("regular_price_usd < price_usd")
        if (self.source == "scrapingcourse.com" and self.regular_price_usd
                and self.regular_price_usd > self.price_usd):
            expected = round((1 - self.price_usd / self.regular_price_usd) * 100, 2)
            if self.discount_pct is not None and abs(self.discount_pct - expected) > 0.5:
                raise ValueError("discount_pct inconsistent with prices")
        return self

    @model_validator(mode="after")
    def check_stock_consistency(self) -> "CleanProduct":
        if self.stock_qty == 0 and self.in_stock:
            raise ValueError("stock_qty == 0 but in_stock is True")
        return self


class CleanReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_key: str = Field(..., min_length=10)
    product_key: str = Field(..., pattern=KEY_RE)
    source: Literal["dummyjson.com"] = "dummyjson.com"
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field(..., min_length=1, max_length=2000)
    review_date: str
    collected_at: str
    parser_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    content_hash: str = Field(..., min_length=8)

    @field_validator("comment")
    @classmethod
    def comment_without_pii(cls, v: str) -> str:
        v = " ".join(v.split())
        if EMAIL_RE.search(v) or PHONE_RE.search(v):
            raise ValueError("comment contains personal data (email/phone)")
        return v

    @field_validator("review_date")
    @classmethod
    def review_date_iso(cls, v: str) -> str:
        dt = parse_iso(v)
        if dt > datetime.now(timezone.utc) + timedelta(days=1):
            raise ValueError("review_date is in the future")
        return dt.isoformat(timespec="seconds")


PRODUCT_COLUMNS = list(CleanProduct.model_fields.keys())
REVIEW_COLUMNS = list(CleanReview.model_fields.keys())
