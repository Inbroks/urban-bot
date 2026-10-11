"""Генераторы HTML в том же формате, что отдаёт urbanoutfitters.com (структура снята с живых страниц)."""
import json


def _script(script_id, state):
    # на сайте JSON закодирован дважды: внутри <script> лежит JSON-строка
    return (f'<script nonce="" type="mime/invalid" id="{script_id}">\n'
            f'            {json.dumps(json.dumps(state))}\n        </script>')


def tile(style, slug, name, list_price, sale_price, colors=(("001", "Black"),), sold_out=False):
    return {
        "product": {
            "brand": "Jaded London", "defaultColorCode": colors[0][0], "displayName": name,
            "displaySoldOut": sold_out, "productId": f"UOEU-{style}-000",
            "productSlug": slug, "styleNumber": style,
            "facets": {"colors": [{"colorId": c} for c, _ in colors]},
        },
        "recordType": "PRODUCT",
        "skuInfo": {
            "hasAvailableSku": not sold_out, "hasMarkdown": sale_price < list_price,
            "listPriceHigh": list_price, "listPriceLow": list_price,
            "markdownState": "PERMANENT" if sale_price < list_price else "NONE",
            "salePriceHigh": sale_price, "salePriceLow": sale_price,
            "primarySlice": {"sliceItems": [{"code": c, "displayName": n, "id": f"{style}_{c}"}
                                            for c, n in colors]},
        },
    }


def listing_html(tiles, locale="de-de", currency="EUR"):
    state = {"core": {}, "category": {"pages": {"1": {"wrapper": {"tiles": tiles}}}}}
    ld = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "item": {
            "@type": "Product", "name": t["product"]["displayName"],
            "url": f"https://www.urbanoutfitters.com/{locale}/shop/{t['product']['productSlug']}?color=001",
            "offers": {"price": t["skuInfo"]["salePriceLow"], "priceCurrency": currency}}}
        for i, t in enumerate(tiles)]}
    return ("<html><head>"
            f'<script nonce="" type="application/ld+json">{json.dumps(ld)}</script>'
            "</head><body><div id=app></div>"
            + _script("urbnInitialState", {"quickshop": {}})
            + _script("urbnInitialPiniaState", state)
            + "</body></html>")


def product_html(slug, skus):
    """skus: [(color, size, stock, list, sale)]"""
    selector = {}
    for i, (color, size, stock, lst, sale) in enumerate(skus):
        size_id = str(4700 + i)
        selector[f"{color}|REGULAR|{size_id}"] = {
            "skuId": str(100 + i), "colorId": color, "sizeId": size_id, "size": size,
            "stockLevel": stock, "availableStatus": 1000 if stock else 1111,
            "listPrice": lst, "salePrice": sale, "markdownState": "PERMANENT",
        }
    state = {
        "quickshop": {"product": {"skuSelection": {"selectorMap": {}}}},
        f"product--{slug}": {"core": {"currentSlug": slug},
                             "skuSelection": {"selectorMap": selector, "selectedColor": skus[0][0]}},
    }
    return ("<html><body>" + _script("urbnInitialState", state)
            + _script("urbnInitialPiniaState", {"core": {}}) + "</body></html>")
