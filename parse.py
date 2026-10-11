"""Разбор HTML страниц urbanoutfitters.com.

Сайт кладёт состояние приложения в HTML как дважды закодированный JSON:
  <script type="mime/invalid" id="urbnInitialPiniaState">"{\"category\":...}"</script>  — листинг
  <script type="mime/invalid" id="urbnInitialState">"{\"product--slug\":...}"</script>     — карточка товара
"""
import html as htmllib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Set

LISTING_MARKER = "urbnInitialPiniaState"
PRODUCT_MARKER = "urbnInitialState"


class ParseError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def extract_state(page_html: str, script_id: str) -> Optional[Any]:
    m = re.search(
        r'<script\b[^>]*\bid="' + re.escape(script_id) + r'"[^>]*>(.*?)</script>',
        page_html, re.S,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    for candidate in (raw, htmllib.unescape(raw)):
        try:
            value = json.loads(candidate)
            if isinstance(value, str):
                value = json.loads(value)
            return value
        except (json.JSONDecodeError, TypeError):
            continue
    raise ParseError(f"не удалось декодировать JSON из <script id={script_id}>")


def _walk(obj: Any) -> Iterator[Any]:
    stack = [obj]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _slug_from_url(url: str) -> Optional[str]:
    m = re.search(r"/shop/([^/?#]+)", url or "")
    return m.group(1).lower() if m else None


def _jsonld_blocks(page_html: str) -> Iterator[Any]:
    for m in re.finditer(r'<script\b[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page_html, re.S):
        try:
            yield json.loads(m.group(1))
        except json.JSONDecodeError:
            continue


# ---------------------------------------------------------------- listing
@dataclass
class Tile:
    style: str                       # styleNumber — общий для DE и UK
    slug: str
    name: str
    list_low: Optional[float]
    list_high: Optional[float]
    sale_low: Optional[float]
    sale_high: Optional[float]
    has_markdown: bool
    default_color: Optional[str]
    colors: Dict[str, str] = field(default_factory=dict)   # code -> название цвета
    sold_out: bool = False

    @property
    def price(self) -> Optional[float]:
        return self.sale_low if self.sale_low is not None else self.list_low

    @property
    def full_price(self) -> Optional[float]:
        return self.list_high if self.list_high is not None else self.list_low

    @property
    def discounted(self) -> bool:
        if self.has_markdown:
            return True
        p, f = self.price, self.full_price
        return p is not None and f is not None and p < f - 0.005


@dataclass
class ListingPage:
    tiles: List[Tile]
    total_pages: Optional[int] = None


def _tile_from_raw(raw: Dict[str, Any]) -> Optional[Tile]:
    product = raw.get("product") or {}
    sku = raw.get("skuInfo") or {}
    slug = product.get("productSlug")
    style = product.get("styleNumber") or product.get("productId")
    if not slug or not style:
        return None
    colors: Dict[str, str] = {}
    for item in ((sku.get("primarySlice") or {}).get("sliceItems") or []):
        code = item.get("code")
        if code:
            colors[str(code)] = item.get("displayName") or ""
    if not colors:
        for c in ((product.get("facets") or {}).get("colors") or []):
            if c.get("colorId"):
                colors[str(c["colorId"])] = ""
    default_color = product.get("defaultColorCode") or (next(iter(colors)) if colors else None)
    if not colors and default_color:
        colors[str(default_color)] = ""
    return Tile(
        style=str(style),
        slug=str(slug).lower(),
        name=product.get("displayName") or str(slug),
        list_low=_num(sku.get("listPriceLow")),
        list_high=_num(sku.get("listPriceHigh")),
        sale_low=_num(sku.get("salePriceLow")),
        sale_high=_num(sku.get("salePriceHigh")),
        has_markdown=bool(sku.get("hasMarkdown")),
        default_color=str(default_color) if default_color else None,
        colors=colors,
        sold_out=bool(product.get("displaySoldOut")) or sku.get("hasAvailableSku") is False,
    )


def _is_tile(obj: Any) -> bool:
    return (isinstance(obj, dict) and isinstance(obj.get("product"), dict)
            and "skuInfo" in obj and obj["product"].get("productSlug"))


def _listing_slugs_from_jsonld(page_html: str) -> Set[str]:
    slugs: Set[str] = set()
    for block in _jsonld_blocks(page_html):
        for node in _walk(block):
            if isinstance(node, dict) and node.get("@type") == "ItemList":
                for el in node.get("itemListElement") or []:
                    item = (el or {}).get("item") or {}
                    slug = _slug_from_url(item.get("url", ""))
                    if slug:
                        slugs.add(slug)
    return slugs


def _find_total_pages(category: Any) -> Optional[int]:
    keys = ("totalPages", "pageCount", "numberOfPages", "totalPageCount", "lastPage")
    for node in _walk(category):
        if isinstance(node, dict):
            for k in keys:
                v = node.get(k)
                if isinstance(v, (int, float)) and 0 < v < 1000:
                    return int(v)
    return None


def parse_listing(page_html: str) -> ListingPage:
    state = extract_state(page_html, LISTING_MARKER)
    if not isinstance(state, dict):
        raise ParseError("на странице нет urbnInitialPiniaState")

    category = state.get("category") or {}
    raw_tiles: List[Dict[str, Any]] = []
    pages = category.get("pages") if isinstance(category, dict) else None
    if isinstance(pages, dict):
        for page in pages.values():
            tiles = ((page or {}).get("wrapper") or {}).get("tiles") or []
            raw_tiles.extend(t for t in tiles if _is_tile(t))
    if not raw_tiles:  # запасной вариант: ищем плитки товаров по всему состоянию
        raw_tiles = [n for n in _walk(state) if _is_tile(n)]

    tiles: Dict[str, Tile] = {}
    for raw in raw_tiles:
        if raw.get("recordType") not in (None, "PRODUCT"):
            continue
        tile = _tile_from_raw(raw)
        if tile and tile.style not in tiles:
            tiles[tile.style] = tile

    # Отсекаем случайно попавшие посторонние плитки (рекомендации и т.п.),
    # сверяясь со списком товаров страницы из JSON-LD.
    ld_slugs = _listing_slugs_from_jsonld(page_html)
    result = list(tiles.values())
    if ld_slugs:
        filtered = [t for t in result if t.slug in ld_slugs]
        if filtered:
            result = filtered
    return ListingPage(tiles=result, total_pages=_find_total_pages(category))


# ---------------------------------------------------------------- product page
@dataclass
class ColorInfo:
    price: Optional[float]
    full_price: Optional[float]
    sizes: List[str]          # размеры в наличии, в порядке сайта


def parse_product(page_html: str, slug: Optional[str] = None) -> Dict[str, ColorInfo]:
    state = extract_state(page_html, PRODUCT_MARKER)
    if not isinstance(state, dict):
        raise ParseError("на странице товара нет urbnInitialState")

    modules = {k: v for k, v in state.items()
               if k.startswith("product--") and isinstance(v, dict)}
    order = sorted(modules, key=lambda k: k != f"product--{slug}")
    selector_map: Dict[str, Any] = {}
    for key in order:
        sm = ((modules[key].get("skuSelection") or {}).get("selectorMap")) or {}
        if sm:
            selector_map = sm
            break
    if not selector_map:
        raise ParseError("в карточке товара нет списка размеров (selectorMap)")

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for sku in selector_map.values():
        if isinstance(sku, dict) and sku.get("colorId") is not None:
            grouped.setdefault(str(sku["colorId"]), []).append(sku)

    def size_key(sku: Dict[str, Any]):
        sid = str(sku.get("sizeId") or "")
        return (0, int(sid)) if sid.isdigit() else (1, sid)

    result: Dict[str, ColorInfo] = {}
    for color, skus in grouped.items():
        skus.sort(key=size_key)
        in_stock = [s for s in skus if (s.get("stockLevel") or 0) > 0]
        sizes: List[str] = []
        for s in in_stock:
            size = str(s.get("size") or s.get("sizeId") or "?")
            if size not in sizes:
                sizes.append(size)
        price_pool = in_stock or skus
        sale = [p for p in (_num(s.get("salePrice")) for s in price_pool) if p is not None]
        lst = [p for p in (_num(s.get("listPrice")) for s in skus) if p is not None]
        result[color] = ColorInfo(
            price=min(sale) if sale else None,
            full_price=max(lst) if lst else None,
            sizes=sizes,
        )
    return result
