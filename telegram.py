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


def _discount_pct(price: Optional[float], full: Optional[float]) -> Optional[int]:
    if price is None or not full or price >= full - 0.005:
        return None
    return round((1 - price / full) * 100)


def _sizes_text(sizes: List[str], new: List[str]) -> str:
    if not sizes:
        return "нет в наличии"
    return ", ".join(f"<b>{escape(s)}</b>🆕" if s in new else escape(s) for s in sizes)


def build_caption(item: Dict[str, Any], events: List[str], details: Dict[str, Any],
                  sites: List[Site], site_ok: Dict[str, bool]) -> str:
    lines = [TITLES[e] for e in events]
    lines.append("")
    lines.append(f"<b>{escape(item.get('name') or item['style'])}</b>")
    if item.get("color_name"):
        lines.append(f"🎨 {escape(item['color_name'])}")
    lines.append("")

    drops = details.get("drops") or {}
    new_sizes = details.get("new_sizes") or {}
    for site in sites:
        s = item["sites"].get(site.code)
        if not s:
            lines.append(f"{site.flag} " + ("нет на сайте" if site_ok.get(site.code)
                                             else "не удалось загрузить"))
            continue
        line = f"{site.flag} <b>{fmt_price(s.get('price'), site.currency)}</b>"
        pct = _discount_pct(s.get("price"), s.get("full_price"))
        if pct:
            line += f"  <s>{fmt_price(s.get('full_price'), site.currency)}</s>  −{pct}%"
        if site.code in drops:
            line += f"\n      было {fmt_price(drops[site.code], site.currency)}"
        lines.append(line)

    # Размеры: одной строкой, если на обоих сайтах одинаково
    size_rows = [(site, item["sites"][site.code]["sizes"]) for site in sites
                 if site.code in item["sites"] and item["sites"][site.code].get("sizes") is not None]
    if size_rows:
        lines.append("")
        first = size_rows[0][1]
        same = all(r[1] == first for r in size_rows) and \
            all(new_sizes.get(r[0].code, []) == new_sizes.get(size_rows[0][0].code, []) for r in size_rows)
        if same and len(size_rows) > 1:
            lines.append("📏 Размеры: " + _sizes_text(first, new_sizes.get(size_rows[0][0].code, [])))
        else:
            for site, sizes in size_rows:
                lines.append(f"📏 {site.flag} " + _sizes_text(sizes, new_sizes.get(site.code, [])))

    caption = "\n".join(lines)
    return caption if len(caption) <= 1024 else caption[:1020] + "…"


def build_buttons(item: Dict[str, Any], sites: List[Site]) -> List[List[Dict[str, str]]]:
    row = []
    for site in sites:
        s = item["sites"].get(site.code)
        if s and s.get("slug"):
            label = "Открыть DE" if site.code == "de" else "Открыть UK"
            row.append({"text": f"{site.flag} {label}", "url": site.product_url(s["slug"], item["color"])})
    return [row] if row else []


def image_url(item: Dict[str, Any]) -> str:
    return IMAGE_URL.format(style=item["style"], color=item["color"])


# ------------------------------------------------------------- client
class Telegram:
    def __init__(self, token: Optional[str], chat_ids: List[str]):
        self.token = token
        self.chat_ids = chat_ids
        self.sent = 0

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
            self.sent += 1
            time.sleep(1.1)

    def send_text(self, text: str) -> None:
        if not self.enabled:
            log.info("[telegram выключен] %s", text.replace("\n", " | "))
            return
        for chat in self.chat_ids:
            self._call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                       "link_preview_options": {"is_disabled": True}})
            self.sent += 1
            time.sleep(1.1)
