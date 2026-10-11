"""Один проход проверки: загрузить обе страницы, сравнить с прошлым состоянием, отправить уведомления."""
import copy
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Settings, Site
from .fetch import FetchError, Fetcher
from .parse import (LISTING_MARKER, PRODUCT_MARKER, ColorInfo, ParseError, Tile,
                    parse_listing, parse_product)
from .telegram import Telegram, build_buttons, build_caption, image_url

log = logging.getLogger(__name__)

MAX_PAGES = 5
PAGE_SIZE_GUESS = 50      # если на странице столько плиток — пробуем следующую
PRUNE_AFTER_DAYS = 120
EPS = 0.005


# ------------------------------------------------------------------ state
def load_state(path: str) -> Dict[str, Any]:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("version", 1)
    state.setdefault("initialized", False)
    state.setdefault("items", {})
    return state


def save_state(path: str, state: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------ fetching
def _with_page(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "page"] + [("page", str(page))]
    return urlunsplit(parts._replace(query=urlencode(query)))


def fetch_listing(fetcher: Fetcher, site: Site) -> List[Tile]:
    first = parse_listing(fetcher.get(site.url, site, LISTING_MARKER))
    tiles: Dict[str, Tile] = {t.style: t for t in first.tiles}
    if first.total_pages:
        last = min(first.total_pages, MAX_PAGES)
    else:
        last = MAX_PAGES if len(first.tiles) >= PAGE_SIZE_GUESS else 1
    for page in range(2, last + 1):
        fetcher.pause()
        try:
            res = parse_listing(fetcher.get(_with_page(site.url, page), site, LISTING_MARKER))
        except (FetchError, ParseError) as exc:
            log.warning("%s: страница %d не загрузилась: %s", site.code, page, exc)
            break
        new = [t for t in res.tiles if t.style not in tiles]
        if not new:
            break
        tiles.update({t.style: t for t in new})
    log.info("%s: найдено товаров: %d", site.code, len(tiles))
    return list(tiles.values())


def fetch_product(fetcher: Fetcher, site: Site, tile: Tile) -> Dict[str, ColorInfo]:
    fetcher.pause()
    html = fetcher.get(site.product_url(tile.slug, tile.default_color), site, PRODUCT_MARKER)
    data = parse_product(html, tile.slug)
    for color in tile.colors:
        if color in data:
            continue
        fetcher.pause()
        try:
            extra = parse_product(fetcher.get(site.product_url(tile.slug, color), site, PRODUCT_MARKER),
                                  tile.slug)
            data.setdefault(color, extra.get(color) or ColorInfo(None, None, []))
        except (FetchError, ParseError) as exc:
            log.warning("%s: цвет %s товара %s не загрузился: %s", site.code, color, tile.slug, exc)
    return data


# ------------------------------------------------------------------ snapshot
def build_snapshot(tiles_by_site: Dict[str, List[Tile]],
                   pdp_by_site: Dict[Tuple[str, str], Optional[Dict[str, ColorInfo]]],
                   site_order: List[str]) -> Dict[str, Dict[str, Any]]:
    """Ключ вещи: "<styleNumber>_<colorCode>" — одинаковый на DE и UK."""
    by_style: Dict[str, Dict[str, Tile]] = {}
    for code in site_order:
        for t in tiles_by_site.get(code, []):
            by_style.setdefault(t.style, {})[code] = t

    snapshot: Dict[str, Dict[str, Any]] = {}
    for style, per_site in by_style.items():
        colors: List[str] = []
        for t in per_site.values():
            colors += [c for c in t.colors if c not in colors]
        # английское название (UK) приоритетнее
        name_tile = per_site.get("uk") or next(iter(per_site.values()))
        for color in colors:
            item = {
                "style": style,
                "color": color,
                "name": name_tile.name,
                "color_name": next((t.colors.get(color) for t in (per_site.get("uk"), per_site.get("de"))
                                    if t and t.colors.get(color)), ""),
                "sites": {},
            }
            for code, t in per_site.items():
                if color not in t.colors:
                    continue
                attempted = (code, style) in pdp_by_site
                pdp = pdp_by_site.get((code, style))
                info = pdp.get(color) if pdp else None
                if info and info.price is not None:
                    entry = {"price": info.price, "full_price": info.full_price or t.full_price,
                             "sizes": info.sizes}
                else:
                    entry = {"price": t.price, "full_price": t.full_price, "sizes": None}
                    if attempted and pdp is None:
                        entry["_pdp_failed"] = True
                entry["slug"] = t.slug
                item["sites"][code] = entry
            snapshot[f"{style}_{color}"] = item
    return snapshot


# ------------------------------------------------------------------ diff
def site_discounted(s: Dict[str, Any]) -> bool:
    p, f = s.get("price"), s.get("full_price")
    return p is not None and f is not None and p < f - EPS


def item_discounted(item: Dict[str, Any]) -> bool:
    return any(site_discounted(s) for s in item["sites"].values())


def item_sold_out(item: Dict[str, Any]) -> bool:
    """True только если размеры известны для всех сайтов и их нигде нет."""
    sizes = [s.get("sizes") for s in item["sites"].values()]
    return bool(sizes) and all(sz is not None for sz in sizes) and not any(sizes)


def detect_events(prev: Optional[Dict[str, Any]], cur: Dict[str, Any],
                  initialized: bool) -> Tuple[List[str], Dict[str, Any]]:
    if prev is None:
        if initialized and item_discounted(cur) and not item_sold_out(cur):
            return ["new"], {}
        return [], {}

    events: List[str] = []
    prev_sites = prev.get("sites") or {}
    drops: Dict[str, float] = {}
    for code, s in cur["sites"].items():
        old = (prev_sites.get(code) or {}).get("price")
        if old is not None and s.get("price") is not None and s["price"] < old - EPS:
            drops[code] = old
    new_sizes: Dict[str, List[str]] = {}
    if item_discounted(cur):
        for code, s in cur["sites"].items():
            old_sizes = (prev_sites.get(code) or {}).get("sizes")
            if s.get("sizes") is None or old_sizes is None:
                continue
            added = [x for x in s["sizes"] if x not in old_sizes]
            if added:
                new_sizes[code] = added
    if drops and not item_sold_out(cur):
        events.append("drop")
    if new_sizes:
        events.append("sizes")
    return events, {"drops": drops, "new_sizes": new_sizes}


def merge_item(prev: Optional[Dict[str, Any]], cur: Dict[str, Any], today: str) -> Dict[str, Any]:
    merged = copy.deepcopy(prev) if prev else {"first_seen": today}
    sites = merged.get("sites") or {}
    for code, s in cur["sites"].items():
        entry = {k: v for k, v in s.items() if not k.startswith("_")}
        sites[code] = entry
    merged.update({k: cur[k] for k in ("style", "color", "name", "color_name")})
    merged["sites"] = sites
    merged["last_seen"] = today
    return merged


# ------------------------------------------------------------------ run
def _maybe_alert(state: Dict[str, Any], telegram: Telegram, errors: Dict[str, str],
                 settings: Settings, sites: List[Site], now: datetime) -> None:
    last = state.get("last_alert")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=settings.alert_every_hours):
        return
    flags = {s.code: s.flag for s in sites}
    lines = ["⚠️ <b>Не удалось загрузить Urban Outfitters</b>"]
    for code, err in errors.items():
        lines.append(f"{flags.get(code, code)} {escape(err[:300])}")
    if not settings.firecrawl_key and not settings.proxy_url:
        lines.append("\nСкорее всего, сайт блокирует серверы GitHub. Добавьте секрет "
                     "<code>FIRECRAWL_API_KEY</code> или <code>PROXY_URL</code> (см. README).")
    telegram.send_text("\n".join(lines))
    state["last_alert"] = now.isoformat()


def run(settings: Settings, sites: List[Site], fetcher: Fetcher, telegram: Telegram,
        now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    state = load_state(settings.state_path)
    test_mode = settings.mode == "test"
    site_order = [s.code for s in sites]

    # 1. Листинги
    tiles_by_site: Dict[str, List[Tile]] = {}
    errors: Dict[str, str] = {}
    for site in sites:
        try:
            tiles_by_site[site.code] = fetch_listing(fetcher, site)
        except (FetchError, ParseError) as exc:
            errors[site.code] = str(exc)
            log.error("%s: листинг не загрузился: %s", site.code, exc)
        fetcher.pause()
    site_ok = {s.code: s.code in tiles_by_site for s in sites}

    if errors:
        print(f"::warning::Не загрузились: {', '.join(errors)}")
        _maybe_alert(state, telegram, errors, settings, sites, now)
    if not tiles_by_site:
        if not test_mode:
            save_state(settings.state_path, state)
        return {"events": 0, "errors": errors}
    if not errors:
        state.pop("last_alert", None)

    # 2. Карточки товаров — только для вещей со скидкой (нужны размеры и цены по цветам)
    discounted_styles = {t.style for tiles in tiles_by_site.values() for t in tiles if t.discounted}
    pdp_by_site: Dict[Tuple[str, str], Optional[Dict[str, ColorInfo]]] = {}
    site_by_code = {s.code: s for s in sites}
    for code, tiles in tiles_by_site.items():
        for t in tiles:
            if t.style not in discounted_styles:
                continue
            try:
                pdp_by_site[(code, t.style)] = fetch_product(fetcher, site_by_code[code], t)
            except (FetchError, ParseError) as exc:
                log.warning("%s: карточка %s не загрузилась: %s", code, t.slug, exc)
                pdp_by_site[(code, t.style)] = None

    snapshot = build_snapshot(tiles_by_site, pdp_by_site, site_order)
    items = state["items"]

    # если карточка не загрузилась — берём размеры с прошлой проверки, чтобы не было ложных событий
    for key, cur in snapshot.items():
        for code, s in cur["sites"].items():
            if s.get("_pdp_failed"):
                s["sizes"] = ((items.get(key) or {}).get("sites", {}).get(code) or {}).get("sizes")

    # 3. Тестовый режим: показать текущие скидки и выйти, ничего не сохраняя
    if test_mode:
        disc = [it for it in snapshot.values() if item_discounted(it)]
        summary = (f"🧪 <b>Тест бота</b>\nНайдено вещей: "
                   + ", ".join(f"{s.flag} {len(tiles_by_site.get(s.code, []))}" for s in sites)
                   + f"\nСо скидкой сейчас: {len(disc)}"
                   + f"\nЗагрузка: {dict(fetcher.used) or '—'}")
        telegram.send_text(summary)
        for it in disc[: settings.test_limit]:
            telegram.send_item(build_caption(it, ["test"], {}, sites, site_ok), image_url(it),
                               build_buttons(it, sites))
        return {"events": len(disc[: settings.test_limit]), "errors": errors}

    # 4. Сравнение и уведомления
    initialized = state["initialized"]
    sent = 0
    for key, cur in snapshot.items():
        prev = items.get(key)
        events, details = detect_events(prev, cur, initialized)
        if events:
            log.info("Событие %s: %s %s", events, key, cur["name"])
            telegram.send_item(build_caption(cur, events, details, sites, site_ok), image_url(cur),
                               build_buttons(cur, sites))
            sent += 1
        items[key] = merge_item(prev, cur, today)

    if not initialized:
        state["initialized"] = True
        disc = sum(1 for it in snapshot.values() if item_discounted(it))
        telegram.send_text(
            "✅ <b>Бот запущен</b>\nОтслеживаю Jaded London (мужское): "
            + ", ".join(f"{s.flag} {len(tiles_by_site.get(s.code, []))} шт." for s in sites)
            + f"\nСейчас со скидкой: {disc}.\n\nБуду присылать: новые вещи со скидкой, "
              "снижение цены и новые размеры у вещей со скидкой."
        )

    # 5. Чистка давно пропавших вещей
    cutoff = (now - timedelta(days=PRUNE_AFTER_DAYS)).date().isoformat()
    for key in [k for k, v in items.items() if v.get("last_seen", today) < cutoff]:
        del items[key]

    state["heartbeat"] = today
    save_state(settings.state_path, state)
    log.info("Готово: уведомлений %d, загрузка %s", sent, dict(fetcher.used))
    return {"events": sent, "errors": errors}
