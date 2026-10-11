import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Dict, List, Optional

# Москва живёт в UTC+3 круглый год (без перехода на летнее время)
MSK = timezone(timedelta(hours=3), "MSK")

DEFAULT_DE_URL = "https://www.urbanoutfitters.com/de-de/brands/jaded-london?department=Herren"
DEFAULT_UK_URL = (
    "https://www.urbanoutfitters.com/en-gb/brands/jaded-london"
    "?language=en-GB&ref=languageSelect&department=Men"
)


@dataclass(frozen=True)
class Site:
    code: str          # "de" / "uk"
    flag: str
    url: str           # страница бренда (листинг)
    locale_path: str   # "de-de" / "en-gb" — часть пути в URL товара
    accept_language: str
    country: str       # для Firecrawl location
    currency: str      # EUR / GBP

    def product_url(self, slug: str, color: Optional[str] = None) -> str:
        url = f"https://www.urbanoutfitters.com/{self.locale_path}/shop/{slug}"
        return f"{url}?color={color}" if color else url


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Пустая строка (не заданный secret в Actions) считается отсутствием значения."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def get_sites() -> Dict[str, Site]:
    return {
        "uk": Site("uk", "🇬🇧", _env("UK_URL", DEFAULT_UK_URL), "en-gb",
                   "en-GB,en;q=0.9", "GB", "GBP"),
        "de": Site("de", "🇩🇪", _env("DE_URL", DEFAULT_DE_URL), "de-de",
                   "de-DE,de;q=0.9,en;q=0.8", "DE", "EUR"),
    }


@dataclass
class Settings:
    telegram_token: Optional[str] = None
    chat_ids: List[str] = field(default_factory=list)
    # Ключ №1 работает с 1 по 15 число, ключ №2 — с 16 до конца месяца.
    firecrawl_keys: List[Optional[str]] = field(default_factory=list)
    credits_per_key: int = 1000       # месячный лимит одного аккаунта Firecrawl
    runs_per_day: int = 38            # запусков в сутки по расписанию (для расчёта бюджета)
    size_check_hours: List[int] = field(default_factory=lambda: [9, 21])   # МСК
    proxy_url: Optional[str] = None
    try_direct: bool = False          # пробовать сайт напрямую (с GitHub обычно блокируется)
    state_path: str = "state.json"
    mode: str = "run"                 # run | test
    alert_every_hours: float = 6.0
    test_limit: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        chats = _env("TELEGRAM_CHAT_ID", "") or ""
        hours = _env("SIZE_CHECK_HOURS", "9,21") or "9,21"
        return cls(
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            chat_ids=[c.strip() for c in chats.split(",") if c.strip()],
            firecrawl_keys=[_env("FIRECRAWL_API_KEY"), _env("FIRECRAWL_API_KEY_2")],
            credits_per_key=int(_env("FIRECRAWL_CREDITS_PER_KEY", "1000")),
            runs_per_day=int(_env("RUNS_PER_DAY", "38")),
            size_check_hours=sorted({int(h) % 24 for h in hours.split(",") if h.strip()}),
            proxy_url=_env("PROXY_URL"),
            try_direct=(_env("TRY_DIRECT", "0") or "0").lower() in ("1", "true", "yes"),
            state_path=_env("STATE_PATH", "state.json"),
            mode=(_env("MODE", "run") or "run").lower(),
            alert_every_hours=float(_env("ALERT_EVERY_HOURS", "6")),
            test_limit=int(_env("TEST_LIMIT", "3")),
        )
