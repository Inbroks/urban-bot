"""Один проход проверки.

Экономная схема (1 кредит Firecrawl на обычную проверку):
  1. Каждый запуск — только страница бренда UK: цены, скидки, новые вещи.
  2. Карточка товара UK (размеры) — только для вещей, где что-то случилось
     (новая вещь со скидкой, снижение цены, возврат в наличие), и плановая проверка
     размеров у всех вещей со скидкой в часы SIZE_CHECK_HOURS (по умолчанию 9:00 и 21:00 МСК).
  3. Страница DE (цены в €) — только если есть что отправить, одна на запуск.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import MSK, Settings, Site
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
    state: Dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    if state.get("version") != 2:          # формат прошлой версии бота — начинаем заново
        state = {"version": 2}
    state.setdefault("initialized", False)
    state.setdefault("items", {})
    return state


def save_state(path: str, state: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------ calendar & budget
def half_period(msk: datetime) -> Tuple[str, datetime, datetime, int]:
    """Половина месяца: (метка, начало, конец, индекс ключа Firecrawl)."""
    first = msk.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mid = first.replace(day=16)
    nxt = (first + timedelta(days=32)).replace(day=1)
    if msk < mid:
        return f"{msk:%Y-%m}-1", first, mid, 0
    return f"{msk:%Y-%m}-2", mid, nxt, 1


def last_size_slot(msk: datetime, hours: List[int]) -> datetime:
    slots = [msk.replace(hour=h, minute=0, second=0, microsecond=0) for h in hours]
    past = [s for s in slots if s <= msk]
    return max(past) if past else max(slots) - timedelta(days=1)


def size_check_due(state: Dict[str, Any], msk: datetime, hours: List[int]) -> bool:
    last = state.get("last_size_check")
    return not last or datetime.fromisoformat(last) < last_size_slot(msk, hours)


def half_budget(settings: Settings) -> float:
    keys = sum(1 for k in settings.firecrawl_keys if k)
    # два ключа — у каждого своя половина месяца; один ключ — делим его лимит пополам
    return settings.credits_per_key if keys >= 2 else settings.credits_per_key / 2


def budget_allows(used: int, cost: int, settings: Settings, msk: datetime, end: datetime) -> bool:
    """Хватит ли кредитов до конца половины месяца, если сейчас потратить `cost`.
    Резерв — на обычные проверки (по 1 кредиту) и редкие события."""
    remaining_days = max(0.0, (end - msk).total_seconds() / 86400)
    reserve = remaining_days * settings.runs_per_day * 1.1 + 30
    return used + cost + reserve <= half_budget(settings)


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
            if first.total_pages:
                # страниц точно больше одной — неполный список дал бы ложные «пропажи» и «возвраты»
                raise FetchError(f"{site.code}: страница {page} из {last} не загрузилась: {exc}")
            log.info("%s: страницы %d нет (%s)", site.code, page, exc)
            break
        new = [t for t in res.tiles if t.style not in tiles]
        if not new:
            break
        tiles.update({t.style: t for t in new})
    if not tiles:
        raise ParseError(f"{site.code}: на странице бренда не найдено ни одной вещи (изменилась вёрстка?)")
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
            data[color] = extra.get(color) or ColorInfo(None, None, [])
        except (FetchError, ParseError) as exc:
            log.warning("%s: цвет %s товара %s не загрузился: %s", site.code, color, tile.slug, exc)
    return data


# ------------------------------------------------------------------ helpers
def items_from_tiles(tiles: List[Tile]) -> Dict[str, Dict[str, Any]]:
    """Ключ вещи: "<styleNumber>_<colorCode>" — одинаковый на DE и UK."""
    out: Dict[str, Dict[str, Any]] = {}
    for t in tiles:
        for color, color_name in t.colors.items():
            out[f"{t.style}_{color}"] = {
                "style": t.style, "color": color, "name": t.name, "color_name": color_name,
                "slug": t.slug, "price": t.price, "full_price": t.full_price,
                "sold_out": t.sold_out, "multi": len(t.colors) > 1,
                "sizes": None, "fresh": False,
            }
    return out


def is_discounted(d: Dict[str, Any]) -> bool:
    p, f = d.get("price"), d.get("full_price")
    return p is not None and f is not None and p < f - EPS


def message_item(it: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "style": it["style"], "color": it["color"], "name": it["name"],
        "color_name": it["color_name"],
        "uk": {"price": it["price"], "full_price": it["full_price"],
               "sizes": it["sizes"], "slug": it["slug"]},
        "de": it.get("de"),
    }


def _maybe_alert(state: Dict[str, Any], telegram: Telegram, error: str,
                 settings: Settings, now: datetime) -> None:
    last = state.get("last_alert")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=settings.alert_every_hours):
        return
    lines = ["⚠️ <b>Не удалось загрузить Urban Outfitters (UK)</b>", escape(error[:600])]
    if not any(settings.firecrawl_keys) and not settings.proxy_url:
        lines.append("\nСайт блокирует серверы GitHub. Добавьте секрет "
                     "<code>FIRECRAWL_API_KEY</code> (см. README).")
    telegram.send_text("\n".join(lines))
    state["last_alert"] = now.isoformat()


# ------------------------------------------------------------------ run
def run(settings: Settings, sites: Dict[str, Site], fetcher: Fetcher, telegram: Telegram,
        now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    msk = now.astimezone(MSK)
    today = msk.date().isoformat()
    state = load_state(settings.state_path)
    items: Dict[str, Dict[str, Any]] = state["items"]
    initialized = state["initialized"]
    test = settings.mode == "test"
    uk, de = sites["uk"], sites["de"]

    period, _, period_end, _ = half_period(msk)
    credits = state.get("credits") or {}
    if credits.get("period") != period:
        credits = {"period": period, "used": {}, "warned": []}
    state["credits"] = credits

    def warn_once(tag: str, text: str) -> None:
        if tag not in credits["warned"]:
            telegram.send_text(text)
            credits["warned"].append(tag)

    def finish(result: Dict[str, Any]) -> Dict[str, Any]:
        for label, n in fetcher.credits.items():
            credits["used"][label] = credits["used"].get(label, 0) + n
        for label in sorted(fetcher.exhausted):
            warn_once(f"exhausted{label}",
                      f"⚠️ Ключ Firecrawl {label}: закончились кредиты или ключ недействителен. "
                      + ("Работаю через другой ключ." if fetcher.active_key
                         else "Других рабочих ключей нет — проверки не идут, пока не пополнится."))
        state["heartbeat"] = today
        save_state(settings.state_path, state)
        log.info("Итог: %s; списано кредитов: %s; загрузки: %s",
                 result, dict(fetcher.credits), dict(fetcher.used))
        return result

    # 1. Страница бренда UK — каждый запуск
    try:
        uk_tiles = fetch_listing(fetcher, uk)
    except (FetchError, ParseError) as exc:
        log.error("UK: листинг не загрузился: %s", exc)
        print(f"::warning::Страница UK не загрузилась: {exc}")
        _maybe_alert(state, telegram, str(exc), settings, now)
        return finish({"events": 0, "error": str(exc)})
    state.pop("last_alert", None)
    tiles = {t.style: t for t in uk_tiles}
    order = [t.style for t in uk_tiles]
    cur = items_from_tiles(uk_tiles)
    discounted = [s for s in order if tiles[s].discounted and not tiles[s].sold_out]

    # 2. Для каких вещей открыть карточку UK (размеры)
    need: Set[str] = set()
    # минимальная известная цена по каждой вещи (у многоцветных в листинге цена общая — самая низкая)
    prev_min: Dict[str, float] = {}
    for key, it in cur.items():
        p = ((items.get(key) or {}).get("uk") or {}).get("price")
        if p is not None:
            prev_min[it["style"]] = min(p, prev_min.get(it["style"], p))
    for key, it in cur.items():
        prev_uk = (items.get(key) or {}).get("uk") or {}
        style, t = it["style"], tiles[it["style"]]
        if key not in items:
            if initialized and not t.sold_out:
                need.add(style)                                        # новая вещь (любая)
        elif style in prev_min and t.price is not None and t.price < prev_min[style] - EPS:
            need.add(style)                                            # цена снизилась
        elif items[key].get("missing") and not t.sold_out:
            need.add(style)                                            # пропадала с сайта и вернулась
        elif prev_uk.get("listing_sold_out") and not t.sold_out and t.discounted:
            need.add(style)                                            # вернулась в наличие

    if test:
        need = set(discounted[: settings.test_limit])
    elif not initialized or size_check_due(state, msk, settings.size_check_hours):
        extra = [s for s in discounted if s not in need]
        used = sum(credits["used"].values())
        if not initialized or budget_allows(used, len(extra), settings, msk, period_end):
            need.update(extra)
            log.info("Плановая проверка размеров: %d вещей", len(extra))
        else:
            log.warning("Проверка размеров пропущена: бережём кредиты до конца половины месяца")
            warn_once("budget", "ℹ️ Кредиты Firecrawl расходуются быстрее плана — до конца половины "
                                "месяца проверяю размеры реже. Цены и новые вещи проверяются как обычно.")
        state["last_size_check"] = now.isoformat()

    pdp: Dict[str, Optional[Dict[str, ColorInfo]]] = {}
    for style in [s for s in order if s in need]:
        try:
            pdp[style] = fetch_product(fetcher, uk, tiles[style])
        except (FetchError, ParseError) as exc:
            log.warning("UK: карточка %s не загрузилась: %s", tiles[style].slug, exc)
            pdp[style] = None

    # 3. Уточняем цены и размеры по карточкам
    for key, it in cur.items():
        prev_uk = (items.get(key) or {}).get("uk") or {}
        style = it["style"]
        it["sizes"] = prev_uk.get("sizes")
        if style in pdp:
            data = pdp[style]
            if data is None:
                it["unreliable"] = True          # карточка не загрузилась — событий по вещи не делаем
                continue
            info = data.get(it["color"])
            if info:
                # Цену с карточки берём только для многоцветных вещей (в листинге у них одна цена
                # на все цвета). У одноцветных источник цены всегда листинг — иначе цены из двух
                # источников могут расходиться и давать ложные «снижения».
                if it["multi"]:
                    if info.price is not None:
                        it["price"] = info.price
                    if info.full_price is not None:
                        it["full_price"] = info.full_price
                it["sizes"], it["sold_out"], it["fresh"] = info.sizes, not info.sizes, True
                continue
        if it["multi"] and prev_uk.get("price") is not None:
            # у многоцветных вещей цена в листинге общая на все цвета — держим точную цену цвета
            it["price"] = prev_uk["price"]
            it["full_price"] = prev_uk.get("full_price", it["full_price"])

    # 4. События
    events: Dict[str, Tuple[List[str], Dict[str, Any]]] = {}
    if not test:
        for key, it in cur.items():
            if it.get("unreliable"):
                continue
            prev = items.get(key)
            prev_uk = (prev or {}).get("uk") or {}
            disc = is_discounted(it)
            unavailable = it["sold_out"] or (it["fresh"] and not it["sizes"])
            types: List[str] = []
            det: Dict[str, Any] = {"drops": {}, "new_sizes": []}
            if prev is None:
                if initialized and not unavailable:
                    types.append("new" if disc else "new_full")
            elif prev.get("missing"):
                if not unavailable and it["fresh"]:
                    types.append("back")                               # вещь вернулась на сайт
                    old = prev_uk.get("price")
                    if old is not None and it["price"] is not None and it["price"] < old - EPS:
                        types.insert(0, "drop")
                        det["drops"]["uk"] = old
            else:
                old = prev_uk.get("price")
                if old is not None and it["price"] is not None and it["price"] < old - EPS \
                        and not unavailable:
                    types.append("drop")
                    det["drops"]["uk"] = old
                if disc and it["fresh"] and prev_uk.get("sizes") is not None and is_discounted(prev_uk):
                    added = [s for s in it["sizes"] if s not in prev_uk["sizes"]]
                    if added:
                        types.append("back" if not prev_uk["sizes"] else "sizes")
                        det["new_sizes"] = added
                elif disc and it["fresh"] and it["sizes"] and prev_uk.get("listing_sold_out") \
                        and "drop" not in types:
                    types.append("back")
            if types:
                events[key] = (types, det)

    # 5. Цены DE — только если есть что отправить
    msg_keys = list(events) if not test else [k for k, it in cur.items()
                                              if it["style"] in need and is_discounted(it)]
    if msg_keys:
        try:
            de_tiles: Optional[Dict[str, Tile]] = {t.style: t for t in fetch_listing(fetcher, de)}
        except (FetchError, ParseError) as exc:
            log.warning("DE: листинг не загрузился: %s", exc)
            de_tiles = None
        de_pdp: Dict[str, Optional[Dict[str, ColorInfo]]] = {}
        for key in msg_keys:
            it = cur[key]
            prev_de = (items.get(key) or {}).get("de") or {}
            fresh_de: Optional[Dict[str, Any]] = None
            if de_tiles is not None and it["style"] in de_tiles:
                t = de_tiles[it["style"]]
                fresh_de = {"price": t.price, "full_price": t.full_price, "slug": t.slug}
            elif de_tiles is not None:
                # в листинге DE не нашли — открываем карточку DE (тот же адрес, что на UK)
                if it["style"] not in de_pdp:
                    try:
                        de_pdp[it["style"]] = fetch_product(fetcher, de, tiles[it["style"]])
                    except (FetchError, ParseError) as exc:
                        log.warning("DE: карточка %s не загрузилась: %s", it["slug"], exc)
                        de_pdp[it["style"]] = None
                info = (de_pdp[it["style"]] or {}).get(it["color"])
                if info and info.price is not None:
                    fresh_de = {"price": info.price, "full_price": info.full_price, "slug": it["slug"]}
            if fresh_de:
                fresh_de["checked"] = today
                it["de"] = fresh_de
                if key in events and prev_de.get("price") is not None \
                        and fresh_de["price"] is not None and fresh_de["price"] < prev_de["price"] - EPS:
                    events[key][1]["drops"]["de"] = prev_de["price"]
            elif prev_de.get("price") is not None:
                it["de"] = dict(prev_de, stale=True)

    # 6. Уведомления
    if test:
        telegram.send_text(
            f"🧪 <b>Тест бота</b>\nВещей на странице UK: {len(uk_tiles)}"
            f"\nСо скидкой сейчас: {len(discounted)}"
            f"\nКлюч Firecrawl: {fetcher.active_key or '—'}"
            f"\nСписано за этот тест: {sum(fetcher.credits.values())}"
            f"\nСписано за половину месяца: {sum(credits['used'].values()) + sum(fetcher.credits.values())}")
        for key in msg_keys:
            mi = message_item(cur[key])
            telegram.send_item(build_caption(mi, ["test"], {}, sites), image_url(mi),
                               build_buttons(mi, sites))
        return finish({"events": len(msg_keys)})

    failed: Set[str] = set()
    for key, (types, det) in events.items():
        mi = message_item(cur[key])
        log.info("Событие %s: %s %s", types, key, mi["name"])
        if not telegram.send_item(build_caption(mi, types, det, sites), image_url(mi),
                                  build_buttons(mi, sites)):
            failed.add(key)          # не отправилось — состояние не обновляем, повторим в следующий раз

    # 7. Сохраняем состояние
    for key, it in cur.items():
        if key not in items and (it.get("unreliable") or key in failed):
            continue                 # новая вещь, о которой не удалось сообщить, — останется «новой»
        prev = items.get(key) or {"first_seen": today}
        if not it.get("unreliable") and key not in failed:
            prev.update({k: it[k] for k in ("style", "color", "name", "color_name")})
            prev["uk"] = {k: it[k] for k in ("slug", "price", "full_price", "sold_out", "sizes")}
            prev["uk"]["listing_sold_out"] = tiles[it["style"]].sold_out
            if it.get("de") and not it["de"].get("stale"):
                prev["de"] = it["de"]
            prev.pop("missing", None)
        prev["last_seen"] = today
        items[key] = prev

    # Вещи, которых больше нет на странице, помечаем — если вернутся, придёт «Снова в наличии».
    # Если пропало больше половины известных вещей разом, это скорее сбой сайта: не помечаем.
    gone = [k for k in items if k not in cur and not items[k].get("missing")]
    present = sum(1 for k in items if k in cur)
    if gone and len(gone) <= max(3, present):
        for k in gone:
            items[k]["missing"] = True
    elif gone:
        log.warning("Пропало слишком много вещей сразу (%d) — не помечаю их как пропавшие", len(gone))

    if not initialized:
        state["initialized"] = True
        telegram.send_text(
            "✅ <b>Бот запущен</b>\n"
            f"Jaded London, мужское: на странице UK {len(uk_tiles)} вещей, со скидкой {len(discounted)}.\n\n"
            "Проверка цен и новых вещей — каждые 30 минут (пауза 2:00–7:00 МСК), "
            f"размеров — в {', '.join(f'{h}:00' for h in settings.size_check_hours)} МСК.\n"
            "Буду присылать: все новые вещи (со скидкой и без), снижение цены, "
            "новые размеры и возврат в наличие."
        )

    cutoff = (msk - timedelta(days=PRUNE_AFTER_DAYS)).date().isoformat()
    for key in [k for k, v in items.items() if v.get("last_seen", today) < cutoff]:
        del items[key]

    return finish({"events": len(events)})
