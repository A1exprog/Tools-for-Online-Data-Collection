"""Контракт данных чистого слоя (pydantic v2).

Location    — справочник локаций (HTML).
Observation — суточное наблюдение (API), связано с Location по location_id.
"""
import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _require_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("наивная дата без таймзоны; ожидается UTC")
    if v.utcoffset().total_seconds() != 0:
        raise ValueError(f"смещение {v.utcoffset()} вместо UTC")
    return v.astimezone(timezone.utc)


class Location(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    location_id: str = Field(min_length=2, max_length=64)
    city: str = Field(min_length=2, max_length=80)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    source: str = Field(min_length=1)
    source_url: str = Field(pattern=r"^https?://")
    parser_version: str = Field(min_length=1)
    collected_at: datetime

    @field_validator("location_id")
    @classmethod
    def slug_format(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError("location_id должен быть ASCII-slug: строчные латинские буквы, цифры, дефис")
        return v

    @field_validator("collected_at")
    @classmethod
    def utc_only(cls, v: datetime) -> datetime:
        return _require_utc(v)


class Observation(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    observation_id: str = Field(min_length=12, max_length=80)
    location_id: str = Field(min_length=2, max_length=64)
    city: str = Field(min_length=2)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    observation_time_utc: datetime
    day_code: str = Field(pattern=r"^(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")
    temperature_2m_max: float = Field(gt=-90, le=60)
    temperature_2m_min: float = Field(gt=-90, le=60)
    precipitation_sum: Optional[float] = Field(default=None, ge=0, le=500)
    source: str = Field(min_length=1)
    source_url: str = Field(pattern=r"^https?://")
    parser_version: str = Field(min_length=1)
    collected_at: datetime

    @field_validator("observation_time_utc", "collected_at")
    @classmethod
    def utc_only(cls, v: datetime) -> datetime:
        return _require_utc(v)

    @field_validator("location_id")
    @classmethod
    def slug_format(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError("location_id должен быть ASCII-slug")
        return v

    @field_validator("source_url")
    @classmethod
    def no_tracking(cls, v: str) -> str:
        if "utm_" in v:
            raise ValueError("URL содержит метки трекинга utm_*")
        return v

    @model_validator(mode="after")
    def consistency(self):
        if self.temperature_2m_max < self.temperature_2m_min:
            raise ValueError("temperature_2m_max < temperature_2m_min")
        day = self.observation_time_utc.date().isoformat()
        if self.observation_id != f"{self.location_id}_{day}":
            raise ValueError(f"observation_id ≠ location_id + '_' + дата ({self.location_id}_{day})")
        if self.day_code != self.observation_time_utc.strftime("%m%d"):
            raise ValueError("day_code не соответствует дате наблюдения")
        if self.collected_at < self.observation_time_utc:
            raise ValueError("данные собраны раньше даты наблюдения")
        return self


def demo_broken() -> list[dict]:
    """Заведомо битые записи: каждая нарушает ровно одно правило контракта."""
    from datetime import timedelta
    t = datetime(2026, 9, 30, tzinfo=timezone.utc)
    base = dict(observation_id="moskva_2026-09-30", location_id="moskva", city="Москва",
                latitude=55.75, longitude=37.62, observation_time_utc=t, day_code="0930",
                temperature_2m_max=12.4, temperature_2m_min=4.1, precipitation_sum=0.8,
                source="open-meteo", source_url="https://archive-api.open-meteo.com/v1/archive?latitude=55.75",
                parser_version="1.1", collected_at=t + timedelta(days=31))
    cases = [
        ("эталонная запись", {}),
        ("широта вне диапазона (Field le=90)", {"latitude": 155.75}),
        ("наивная дата без таймзоны (field_validator utc_only)",
         {"observation_time_utc": datetime(2026, 9, 30)}),
        ("кириллический ключ (field_validator slug_format)", {"location_id": "москва"}),
        ("utm-метки в URL (field_validator no_tracking)",
         {"source_url": base["source_url"] + "&utm_source=tg"}),
        ("осадки < 0 (Field ge=0)", {"precipitation_sum": -2.0}),
        ("пустой город (min_length=2)", {"city": "  "}),
        ("day_code без ведущего нуля (pattern)", {"day_code": "930"}),
        ("tmax < tmin (model_validator)", {"temperature_2m_max": 1.0, "temperature_2m_min": 6.0}),
        ("ключ не совпадает с датой (model_validator)", {"observation_id": "moskva_2026-10-01"}),
        ("сбор раньше наблюдения (model_validator)", {"collected_at": t - timedelta(days=1)}),
    ]
    out = []
    for name, patch in cases:
        try:
            Observation(**{**base, **patch})
            out.append({"проверка": name, "результат": "принята", "поле": "", "сообщение": ""})
        except ValidationError as e:
            err = e.errors()[0]
            out.append({"проверка": name, "результат": "отбракована",
                        "поле": ".".join(map(str, err["loc"])) or "model", "сообщение": err["msg"]})
    return out


def validate_rows(rows: list[dict], model) -> tuple[list[dict], list[dict]]:
    """Возвращает (валидные model_dump, rejects с полем и причиной)."""
    ok, bad = [], []
    for r in rows:
        try:
            ok.append(model(**r).model_dump())
        except ValidationError as e:
            err = e.errors()[0]
            bad.append({
                "source": r.get("source"),
                "step": "7. Контракт pydantic",
                "field": ".".join(map(str, err["loc"])) or "model",
                "reason": err["msg"],
                "synthetic": r.get("_synthetic_corruption"),
                "payload": r,
            })
    return ok, bad
