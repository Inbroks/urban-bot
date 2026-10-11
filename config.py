import os
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_DE_URL = "https://www.urbanoutfitters.com/de-de/brands/jaded-london?department=Herren"
DEFAULT_UK_URL = (
    "https://www.urbanoutfitters.com/en-gb/brands/jaded-london"
    "?language=en-GB&ref=languageSelect&department=Men"
)


@dataclass(frozen=True)
class Site:
    code: str          # "de" / "uk" — ключ в state.json
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


def get_sites() -> List[Site]:
    return [
        Site("de", "🇩🇪", _env("DE_URL", DEFAULT_DE_URL), "de-de",
             "de-DE,de;q=0.9,en;q=0.8", "DE", "EUR"),
        Site("uk", "🇬🇧", _env("UK_URL", DEFAULT_UK_URL), "en-gb",
             "en-GB,en;q=0.9", "GB", "GBP"),
    ]


@dataclass
class Settings:
    telegram_token: Optional[str] = None
    chat_ids: List[str] = field(default_factory=list)
    proxy_url: Optional[str] = None
    firecrawl_key: Optional[str] = None
    state_path: str = "state.json"
    mode: str = "run"                 # run | test
    alert_every_hours: float = 6.0    # как часто напоминать, что сайт не грузится
    test_limit: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        chats = _env("TELEGRAM_CHAT_ID", "") or ""
        return cls(
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            chat_ids=[c.strip() for c in chats.split(",") if c.strip()],
            proxy_url=_env("PROXY_URL"),
            firecrawl_key=_env("FIRECRAWL_API_KEY"),
            state_path=_env("STATE_PATH", "state.json"),
            mode=(_env("MODE", "run") or "run").lower(),
            alert_every_hours=float(_env("ALERT_EVERY_HOURS", "6")),
            test_limit=int(_env("TEST_LIMIT", "3")),
        )
