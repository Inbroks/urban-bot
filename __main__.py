import logging
import sys

from .config import Settings, get_sites
from .fetch import Fetcher
from .monitor import run
from .telegram import Telegram


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if not (settings.telegram_token and settings.chat_ids):
        logging.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — сообщения только в лог")
    fetcher = Fetcher(proxy_url=settings.proxy_url, firecrawl_key=settings.firecrawl_key)
    telegram = Telegram(settings.telegram_token, settings.chat_ids)
    run(settings, get_sites(), fetcher, telegram)
    return 0


if __name__ == "__main__":
    sys.exit(main())
