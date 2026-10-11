import logging
import sys
from datetime import datetime, timezone

from .config import MSK, Settings, get_sites
from .fetch import Fetcher
from .monitor import half_period, run
from .telegram import Telegram


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if not (settings.telegram_token and settings.chat_ids):
        logging.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — сообщения только в лог")
    now = datetime.now(timezone.utc)
    _, _, _, key_index = half_period(now.astimezone(MSK))
    fetcher = Fetcher(firecrawl_keys=settings.firecrawl_keys, preferred_key=key_index,
                      proxy_url=settings.proxy_url, try_direct=settings.try_direct)
    telegram = Telegram(settings.telegram_token, settings.chat_ids)
    run(settings, get_sites(), fetcher, telegram, now=now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
