import json
import logging
import time
from html import escape
from typing import Any, Dict, List, Optional

from curl_cffi import requests as cr

from .config import Site

log = logging.getLogger(__name__)

IMAGE_URL = ("https://imageseu.urbndata.com/is/image/UrbanOutfittersEU/"
             "{style}_{color}_a2?$xlarge$&fit=constrain&qlt=80&wid=800")

TITLES = {
    "new": "🆕 <b>Новая вещь со скидкой</b>",
    "drop": "📉 <b>Цена снизилась</b>",
    "sizes": "📏 <b>Появились новые размеры</b>",
    "back": "🔁 <b>Снова в наличии</b>",
    "test": "🧪 <b>Тест: сейчас со скидкой</b>",
}


# ------------------------------------------------------------- formatting
def fmt_price(value: Optional[float], currency: str) -> str:
    if value is None:
        return "?"
    if currency == "EUR":
        s = f"{value:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
        return f"{s} €"
    if currency == "GBP":
        return f"£{value:,.2f}"
    return f"{value:.2f} {currency}"


def discount_pct(price: Optional[float], full: Optional[float]) -> Optional[int]:
    if price is None or not full or price >= full - 0.005:
        return None
    return round((1 - price / full) * 100)


def _sizes_text(sizes: List[str], new: List[str]) -> str:
    if not sizes:
        return "нет в наличии"
    return ", ".join(f"<b>{escape(s)}</b>🆕" if s in new else escape(s) for s in sizes)


def build_caption(item: Dict[str, Any], events: List[str], details: Dict[str, Any],
                  sites: Dict[str, Site]) -> str:
    """item: {"style","color","name","color_name","uk":{...},"de":{...}|None}"""
    lines = [TITLES[e] for e in events]
    lines.append("")
    lines.append(f"<b>{escape(item.get('name') or item['style'])}</b>")
    if item.get("color_name"):
        lines.append(f"🎨 {escape(item['color_name'])}")
    lines.append("")

    drops = details.get("drops") or {}
    for code in ("de", "uk"):
        site = sites[code]
        data = item.get(code) or {}
        if data.get("price") is None:
            lines.append(f"{site.flag} цену загрузить не удалось")
            continue
        line = f"{site.flag} <b>{fmt_price(data['price'], site.currency)}</b>"
        pct = discount_pct(data.get("price"), data.get("full_price"))
        if pct:
            line += f"  <s>{fmt_price(data.get('full_price'), site.currency)}</s>  −{pct}%"
        if data.get("stale"):
            line += "  <i>(последняя известная)</i>"
        if code in drops:
            line += f"\n      было {fmt_price(drops[code], site.currency)}"
        lines.append(line)

    sizes = (item.get("uk") or {}).get("sizes")
    if sizes is not None:
        lines.append("")
        lines.append("📏 Размеры: " + _sizes_text(sizes, details.get("new_sizes") or []))

    caption = "\n".join(lines)
    return caption if len(caption) <= 1024 else caption[:1020] + "…"


def build_buttons(item: Dict[str, Any], sites: Dict[str, Site]) -> List[List[Dict[str, str]]]:
    slug = (item.get("uk") or {}).get("slug")
    if not slug:
        return []
    de_slug = (item.get("de") or {}).get("slug") or slug
    return [[
        {"text": "🇩🇪 Открыть DE", "url": sites["de"].product_url(de_slug, item["color"])},
        {"text": "🇬🇧 Открыть UK", "url": sites["uk"].product_url(slug, item["color"])},
    ]]


def image_url(item: Dict[str, Any]) -> str:
    return IMAGE_URL.format(style=item["style"], color=item["color"])


# ------------------------------------------------------------- client
class Telegram:
    def __init__(self, token: Optional[str], chat_ids: List[str]):
        self.token = token
        self.chat_ids = chat_ids

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_ids)

    def _call(self, method: str, payload: Dict[str, Any]) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for _ in range(4):
            try:
                r = cr.post(url, json=payload, timeout=30)
                data = r.json()
            except Exception as exc:
                log.warning("Telegram %s: ошибка сети: %s", method, exc)
                time.sleep(3)
                continue
            if data.get("ok"):
                return True
            if r.status_code == 429:
                time.sleep(int((data.get("parameters") or {}).get("retry_after", 5)) + 1)
                continue
            log.warning("Telegram %s: %s", method, data.get("description"))
            return False
        return False

    def send_item(self, caption: str, photo: str, buttons: List[List[Dict[str, str]]]) -> None:
        if not self.enabled:
            log.info("[telegram выключен] %s", caption.replace("\n", " | "))
            return
        markup = json.dumps({"inline_keyboard": buttons}) if buttons else None
        for chat in self.chat_ids:
            payload = {"chat_id": chat, "photo": photo, "caption": caption, "parse_mode": "HTML"}
            if markup:
                payload["reply_markup"] = markup
            if not self._call("sendPhoto", payload):
                text_payload = {"chat_id": chat, "text": caption, "parse_mode": "HTML",
                                "link_preview_options": {"is_disabled": True}}
                if markup:
                    text_payload["reply_markup"] = markup
                self._call("sendMessage", text_payload)
            time.sleep(1.1)

    def send_text(self, text: str) -> None:
        if not self.enabled:
            log.info("[telegram выключен] %s", text.replace("\n", " | "))
            return
        for chat in self.chat_ids:
            self._call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                       "link_preview_options": {"is_disabled": True}})
            time.sleep(1.1)
