import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import listing_html, product_html, tile  # noqa: E402
from uo_monitor.config import Settings, get_sites  # noqa: E402
from uo_monitor.fetch import FetchError  # noqa: E402
from uo_monitor.monitor import run  # noqa: E402
from uo_monitor.parse import parse_listing, parse_product  # noqa: E402
from uo_monitor.telegram import build_caption, fmt_price  # noqa: E402

SITES = get_sites()
DE, UK = SITES


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages          # url (без query для товаров) -> html | Exception
        self.used = Counter()
        self.requested = []

    def get(self, url, site, marker):
        self.requested.append(url)
        key = url if url in self.pages else url.split("?")[0]
        page = self.pages.get(key)
        if page is None or isinstance(page, Exception):
            raise FetchError(f"{url}: blocked")
        self.used["fake"] += 1
        return page

    @staticmethod
    def pause():
        pass


class FakeTelegram:
    def __init__(self):
        self.items, self.texts = [], []
        self.enabled = True

    def send_item(self, caption, photo, buttons):
        self.items.append((caption, photo, buttons))

    def send_text(self, text):
        self.texts.append(text)


JORTS = ("0283382100016", "jaded-london-pinstripe-double-knee-carpenter-jorts2")
TEE = ("0281382100016", "jaded-london-washed-black-haze-henley-tee")
NEWJ = ("0283382100099", "jaded-london-new-sale-jeans")


def pdp_url(site, slug):
    return site.product_url(slug)


def world(jorts_de=(75, 45), jorts_uk=(65, 39), jorts_sizes=(("28", 0), ("30", 9), ("32", 0)),
          tee_de=(55, 55), tee_uk=(48, 48), extra=None):
    de_tiles = [tile(*JORTS, "Jaded London Nadelstreifen Jorts", *jorts_de, colors=(("048", "Light Blue"),)),
                tile(*TEE, "Jaded London Henley T-Shirt", *tee_de)]
    uk_tiles = [tile(*JORTS, "Jaded London Pinstripe Double Knee Carpenter Jorts", *jorts_uk,
                     colors=(("048", "Light Blue"),)),
                tile(*TEE, "Jaded London Washed Black Haze Henley Tee", *tee_uk)]
    pages = {
        DE.url: listing_html(de_tiles, "de-de", "EUR"),
        UK.url: listing_html(uk_tiles, "en-gb", "GBP"),
        pdp_url(DE, JORTS[1]): product_html(JORTS[1], [("048", s, n, *jorts_de) for s, n in jorts_sizes]),
        pdp_url(UK, JORTS[1]): product_html(JORTS[1], [("048", s, n, *jorts_uk) for s, n in jorts_sizes]),
    }
    if tee_de[1] < tee_de[0]:
        pages[pdp_url(DE, TEE[1])] = product_html(TEE[1], [("001", "M", 3, *tee_de)])
        pages[pdp_url(UK, TEE[1])] = product_html(TEE[1], [("001", "M", 3, *tee_uk)])
    if extra:
        de_tiles.append(tile(*NEWJ, "Neue Jeans", 109, 65, colors=(("040", "Blue"),)))
        uk_tiles.append(tile(*NEWJ, "Jaded London New Sale Jeans", 95, 57, colors=(("040", "Blue"),)))
        pages[DE.url] = listing_html(de_tiles, "de-de", "EUR")
        pages[UK.url] = listing_html(uk_tiles, "en-gb", "GBP")
        pages[pdp_url(DE, NEWJ[1])] = product_html(NEWJ[1], [("040", "30", 2, 109, 65), ("040", "32", 0, 109, 65)])
        pages[pdp_url(UK, NEWJ[1])] = product_html(NEWJ[1], [("040", "30", 2, 95, 57), ("040", "32", 0, 95, 57)])
    return pages


@pytest.fixture
def settings(tmp_path):
    return Settings(state_path=str(tmp_path / "state.json"))


def do_run(settings, pages):
    tg = FakeTelegram()
    fetcher = FakeFetcher(pages)
    run(settings, SITES, fetcher, tg, now=datetime(2026, 10, 11, 12, tzinfo=timezone.utc))
    return tg, fetcher


# ---------------------------------------------------------------- parsing
def test_parse_listing_and_product():
    page = parse_listing(world()[DE.url])
    assert {t.style for t in page.tiles} == {JORTS[0], TEE[0]}
    jorts = next(t for t in page.tiles if t.style == JORTS[0])
    assert jorts.discounted and jorts.price == 45 and jorts.full_price == 75
    assert jorts.colors == {"048": "Light Blue"}
    tee = next(t for t in page.tiles if t.style == TEE[0])
    assert not tee.discounted

    info = parse_product(world()[pdp_url(DE, JORTS[1])], JORTS[1])
    assert info["048"].sizes == ["30"]
    assert info["048"].price == 45 and info["048"].full_price == 75


def test_listing_ignores_tiles_not_on_page():
    html = world()[DE.url].replace('"category"', '"recs": {"tiles": []}, "category"')
    assert len(parse_listing(html).tiles) == 2


def test_price_format():
    assert fmt_price(1045.5, "EUR") == "1.045,50 €"
    assert fmt_price(39, "GBP") == "£39.00"


# ---------------------------------------------------------------- scenarios
def test_first_run_is_silent_baseline(settings):
    tg, _ = do_run(settings, world())
    assert tg.items == []
    assert len(tg.texts) == 1 and "Бот запущен" in tg.texts[0] and "со скидкой: 1" in tg.texts[0]
    state = json.load(open(settings.state_path))
    item = state["items"][f"{JORTS[0]}_048"]
    assert item["sites"]["de"]["sizes"] == ["30"] and item["sites"]["uk"]["price"] == 39


def test_no_changes_no_messages(settings):
    do_run(settings, world())
    tg, _ = do_run(settings, world())
    assert tg.items == [] and tg.texts == []


def test_new_discounted_item_one_message_both_prices(settings):
    do_run(settings, world())
    tg, _ = do_run(settings, world(extra=True))
    assert len(tg.items) == 1
    caption, photo, buttons = tg.items[0]
    assert "Новая вещь со скидкой" in caption
    assert "Jaded London New Sale Jeans" in caption        # английское название
    assert "65,00 €" in caption and "109,00 €" in caption and "−40%" in caption
    assert "£57.00" in caption and "£95.00" in caption
    assert "Размеры: 30" in caption                         # 32 нет в наличии
    assert photo.endswith("0283382100099_040_a2?$xlarge$&fit=constrain&qlt=80&wid=800")
    urls = [b["url"] for b in buttons[0]]
    assert urls == ["https://www.urbanoutfitters.com/de-de/shop/jaded-london-new-sale-jeans?color=040",
                    "https://www.urbanoutfitters.com/en-gb/shop/jaded-london-new-sale-jeans?color=040"]


def test_price_drop_on_sale_item(settings):
    do_run(settings, world())
    tg, _ = do_run(settings, world(jorts_de=(75, 35), jorts_uk=(65, 30)))
    assert len(tg.items) == 1
    caption = tg.items[0][0]
    assert "Цена снизилась" in caption and "было 45,00 €" in caption and "было £39.00" in caption
    assert "35,00 €" in caption and "−53%" in caption


def test_full_price_item_goes_on_sale(settings):
    do_run(settings, world())
    tg, _ = do_run(settings, world(tee_de=(55, 39), tee_uk=(48, 34)))
    assert len(tg.items) == 1
    caption = tg.items[0][0]
    assert "Цена снизилась" in caption and "было 55,00 €" in caption and "Henley" in caption


def test_new_size_on_sale_item(settings):
    do_run(settings, world())
    tg, _ = do_run(settings, world(jorts_sizes=(("28", 0), ("30", 9), ("32", 4))))
    assert len(tg.items) == 1
    caption = tg.items[0][0]
    assert "Появились новые размеры" in caption
    assert "30, <b>32</b>🆕" in caption


def test_size_sold_out_is_not_an_event(settings):
    do_run(settings, world())
    tg, _ = do_run(settings, world(jorts_sizes=(("28", 0), ("30", 0), ("32", 0))))
    assert tg.items == []
    # а когда размер вернётся — это новый размер
    tg, _ = do_run(settings, world())
    assert len(tg.items) == 1 and "новые размеры" in tg.items[0][0]


def test_one_site_down_keeps_state_and_alerts(settings):
    do_run(settings, world())
    pages = world(jorts_de=(75, 35), jorts_uk=(65, 30))
    pages[UK.url] = Exception("blocked")
    tg, _ = do_run(settings, pages)
    assert len(tg.items) == 1
    assert "🇬🇧 не удалось загрузить" in tg.items[0][0]
    assert any("Не удалось загрузить" in t for t in tg.texts)
    state = json.load(open(settings.state_path))
    assert state["items"][f"{JORTS[0]}_048"]["sites"]["uk"]["price"] == 39   # UK не затёрт
    # UK вернулся с той же сниженной ценой — для UK это тоже снижение относительно 39
    tg, _ = do_run(settings, world(jorts_de=(75, 35), jorts_uk=(65, 30)))
    assert len(tg.items) == 1 and "было £39.00" in tg.items[0][0]


def test_pdp_failure_does_not_fake_size_events(settings):
    do_run(settings, world())
    pages = world()
    pages[pdp_url(DE, JORTS[1])] = Exception("blocked")
    tg, _ = do_run(settings, pages)
    assert tg.items == []
    tg, _ = do_run(settings, world())
    assert tg.items == []


def test_both_sites_down_first_run_stays_uninitialized(settings):
    tg, _ = do_run(settings, {})
    assert tg.items == [] and any("Не удалось" in t for t in tg.texts)
    assert json.load(open(settings.state_path))["initialized"] is False
    tg, _ = do_run(settings, world())
    assert tg.items == [] and any("Бот запущен" in t for t in tg.texts)


def test_test_mode_sends_current_sales_without_saving(settings):
    settings.mode = "test"
    tg, _ = do_run(settings, world())
    assert len(tg.items) == 1 and "Тест" in tg.items[0][0]
    assert not os.path.exists(settings.state_path)


def test_caption_handles_missing_site():
    item = {"style": "1", "color": "001", "name": "X & <Y>", "color_name": "Black",
            "sites": {"de": {"price": 20.0, "full_price": 40.0, "sizes": ["S"], "slug": "x"}}}
    cap = build_caption(item, ["new"], {}, SITES, {"de": True, "uk": True})
    assert "X &amp; &lt;Y&gt;" in cap and "🇬🇧 нет на сайте" in cap and "📏 🇩🇪 S" in cap
