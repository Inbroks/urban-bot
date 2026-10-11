"""Загрузка HTML страниц.

Сайт защищён DataDome. Порядок попыток:
  1. напрямую через curl_cffi (TLS-отпечаток настоящего Chrome), при желании через PROXY_URL;
  2. через Firecrawl API, если задан FIRECRAWL_API_KEY (их браузер и прокси проходят защиту).
Если прямой запрос заблокирован и есть Firecrawl, до конца запуска сразу идём через Firecrawl.
"""
import logging
import random
import time
from collections import Counter
from typing import Callable, List, Optional, Tuple

from curl_cffi import requests as cr

from .config import Site

log = logging.getLogger(__name__)

FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"


class FetchError(Exception):
    pass


def is_blocked(status: int, text: str) -> bool:
    return status in (403, 429) or "captcha-delivery.com" in text


def describe(status: int, text: str) -> str:
    if "captcha-delivery.com" in text:
        return f"HTTP {status}, капча DataDome (антибот)"
    if status in (403, 429):
        return f"HTTP {status}, доступ запрещён (антибот)"
    if status != 200:
        return f"HTTP {status}"
    return "в ответе нет данных каталога (изменилась вёрстка?)"


class Fetcher:
    def __init__(self, proxy_url: Optional[str] = None, firecrawl_key: Optional[str] = None,
                 impersonate: str = "chrome"):
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._session = cr.Session(impersonate=impersonate, proxies=proxies)
        self.firecrawl_key = firecrawl_key
        self._direct_blocked = False
        self.used: Counter = Counter()

    # --- backends -------------------------------------------------------
    def _direct(self, url: str, site: Site) -> Tuple[int, str]:
        r = self._session.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": site.accept_language,
            },
            timeout=40,
            allow_redirects=True,
        )
        return r.status_code, r.text

    def _firecrawl(self, url: str, site: Site) -> Tuple[int, str]:
        r = cr.post(
            FIRECRAWL_URL,
            headers={"Authorization": f"Bearer {self.firecrawl_key}"},
            json={
                "url": url,
                "formats": ["rawHtml"],
                "onlyMainContent": False,
                "maxAge": 0,
                "proxy": "auto",
                "location": {"country": site.country},
                "timeout": 90000,
            },
            timeout=150,
        )
        if r.status_code != 200:
            raise FetchError(f"Firecrawl HTTP {r.status_code}: {r.text[:200]}")
        data = (r.json() or {}).get("data") or {}
        status = int((data.get("metadata") or {}).get("statusCode") or 200)
        return status, data.get("rawHtml") or data.get("html") or ""

    # --- public ---------------------------------------------------------
    def get(self, url: str, site: Site, required_marker: str) -> str:
        backends: List[Tuple[str, Callable[[str, Site], Tuple[int, str]]]] = []
        if not self._direct_blocked or not self.firecrawl_key:
            backends.append(("direct", self._direct))
        if self.firecrawl_key:
            backends.append(("firecrawl", self._firecrawl))

        errors: List[str] = []
        for name, fn in backends:
            for attempt in (1, 2):
                try:
                    status, text = fn(url, site)
                except Exception as exc:  # сеть, таймаут, ошибка API
                    errors.append(f"{name}: {exc}")
                    time.sleep(3)
                    continue
                if status == 200 and required_marker in text:
                    self.used[name] += 1
                    return text
                errors.append(f"{name}: {describe(status, text)}")
                if status == 404:
                    break
                if name == "direct" and is_blocked(status, text):
                    if self.firecrawl_key:
                        self._direct_blocked = True
                        break
                    time.sleep(random.uniform(6, 12))
                else:
                    time.sleep(3)
        raise FetchError(f"{url} → " + " | ".join(errors))

    @staticmethod
    def pause() -> None:
        time.sleep(random.uniform(1.0, 2.5))
