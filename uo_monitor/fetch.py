"""Загрузка HTML страниц.

Сайт закрыт антиботом DataDome, с серверов GitHub напрямую он не пускает,
поэтому основной путь — Firecrawl API. Ключей может быть два: ключ №1 используется
с 1 по 15 число, ключ №2 — с 16-го до конца месяца. Если у активного ключа закончились
кредиты (HTTP 402) или он недействителен (401), бот сам переходит на другой.

Прямая загрузка (curl_cffi с отпечатком Chrome) включается, если задан PROXY_URL
или TRY_DIRECT=1, либо если ключей Firecrawl нет совсем.
"""
import logging
import random
import time
from collections import Counter
from typing import List, Optional, Set, Tuple

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
    def __init__(self, firecrawl_keys: Optional[List[Optional[str]]] = None,
                 preferred_key: int = 0, proxy_url: Optional[str] = None,
                 try_direct: bool = False):
        keys = [(f"№{i + 1}", k) for i, k in enumerate(firecrawl_keys or []) if k]
        # активный ключ (по половине месяца) — первым, остальные — запасные
        keys.sort(key=lambda lk: lk[0] != f"№{preferred_key + 1}")
        self.keys: List[Tuple[str, str]] = keys
        self.exhausted: Set[str] = set()          # ключи, у которых кончились кредиты в этом запуске
        self.credits: Counter = Counter()         # списано кредитов по ключам
        self.used: Counter = Counter()            # успешных загрузок по способам
        self.use_direct = bool(proxy_url) or try_direct or not self.keys
        self._direct_blocked = False
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._session = cr.Session(impersonate="chrome", proxies=proxies)

    @property
    def active_key(self) -> Optional[str]:
        for label, _ in self.keys:
            if label not in self.exhausted:
                return label
        return None

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

    def _firecrawl(self, label: str, key: str, url: str, site: Site) -> Tuple[int, str]:
        r = cr.post(
            FIRECRAWL_URL,
            headers={"Authorization": f"Bearer {key}"},
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
        if r.status_code in (401, 402):
            self.exhausted.add(label)
            reason = "закончились кредиты" if r.status_code == 402 else "ключ недействителен"
            raise FetchError(f"Firecrawl {label}: {reason} (HTTP {r.status_code})")
        if r.status_code == 429:
            time.sleep(15)
            raise FetchError(f"Firecrawl {label}: слишком много запросов (HTTP 429)")
        if r.status_code != 200:
            raise FetchError(f"Firecrawl {label}: HTTP {r.status_code}: {r.text[:200]}")
        self.credits[label] += 1
        data = (r.json() or {}).get("data") or {}
        status = int((data.get("metadata") or {}).get("statusCode") or 200)
        return status, data.get("rawHtml") or data.get("html") or ""

    # --- public ---------------------------------------------------------
    def get(self, url: str, site: Site, required_marker: str) -> str:
        errors: List[str] = []

        if self.use_direct and not self._direct_blocked:
            for _ in (1, 2):
                try:
                    status, text = self._direct(url, site)
                except Exception as exc:
                    errors.append(f"напрямую: {exc}")
                    time.sleep(3)
                    continue
                if status == 200 and required_marker in text:
                    self.used["напрямую"] += 1
                    return text
                errors.append(f"напрямую: {describe(status, text)}")
                if is_blocked(status, text):
                    if self.keys:
                        self._direct_blocked = True   # до конца запуска — только Firecrawl
                        break
                    time.sleep(random.uniform(6, 12))
                elif status == 404:
                    break

        for label, key in self.keys:
            if label in self.exhausted:
                continue
            for _ in (1, 2):
                try:
                    status, text = self._firecrawl(label, key, url, site)
                except FetchError as exc:
                    errors.append(str(exc))
                    if label in self.exhausted:
                        break
                    time.sleep(3)
                    continue
                except Exception as exc:
                    errors.append(f"Firecrawl {label}: {exc}")
                    time.sleep(3)
                    continue
                if status == 200 and required_marker in text:
                    self.used[f"Firecrawl {label}"] += 1
                    return text
                errors.append(f"Firecrawl {label}: {describe(status, text)}")
                if status == 404:
                    break
                time.sleep(3)

        if not errors:
            errors.append("нет ни одного способа загрузки (добавьте FIRECRAWL_API_KEY)")
        raise FetchError(f"{url} → " + " | ".join(errors))

    @staticmethod
    def pause() -> None:
        time.sleep(random.uniform(0.5, 1.5))
