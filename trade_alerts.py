#!/usr/bin/env python3
"""Poll a MetaForge marketplace page and alert a Telegram chat about new trades."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen


USER_AGENT = "MetaForgeTradeAlerts/1.0 (personal marketplace notifications)"
HTTP_TIMEOUT_SECONDS = 20
TELEGRAM_MAX_MESSAGE_LENGTH = 4000
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
CARD_CLASS = "flex flex-col gap-0.5 p-1 sm:p-1.5"
VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class TradeAlertsError(Exception):
    """An expected error while fetching trades or sending an alert."""


class MetaForgeFetchError(TradeAlertsError):
    pass


class TradeParseError(TradeAlertsError):
    pass


class TelegramError(TradeAlertsError):
    pass


@dataclass(frozen=True)
class Trade:
    listing_id: str
    seller: str
    profile_url: str
    text: str
    rating: str = "Unrated"


@dataclass
class _CardCapture:
    depth: int = 1
    text_parts: list[str] = field(default_factory=list)
    title_parts: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    open_links: list[tuple[str, list[str]]] = field(default_factory=list)


class _MarketHTMLParser(HTMLParser):
    """Extract the rendered listing cards without needing a browser engine."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.trades: list[Trade] = []
        self._card: _CardCapture | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._card is None:
            classes = (attributes.get("class") or "").split()
            if tag == "div" and CARD_CLASS in " ".join(classes):
                self._card = _CardCapture()
            return

        if tag not in VOID_ELEMENTS:
            self._card.depth += 1
        if tag == "button" and attributes.get("title"):
            self._card.title_parts.append(attributes["title"] or "")
        if tag == "a":
            self._card.open_links.append((attributes.get("href") or "", []))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._card is None:
            return
        self._card.text_parts.append(data)
        if self._card.open_links:
            self._card.open_links[-1][1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        if tag == "a" and self._card.open_links:
            href, text_parts = self._card.open_links.pop()
            self._card.links.append((href, " ".join(text_parts).strip()))
        if tag not in VOID_ELEMENTS:
            self._card.depth -= 1
        if self._card.depth == 0:
            self._finish_card()

    def _finish_card(self) -> None:
        assert self._card is not None
        text = " ".join(" ".join(self._card.text_parts).split())
        titles = " ".join(" ".join(self._card.title_parts).split())
        if titles:
            text = f"{text} {titles}"
        profile = max(
            (
                (href, label)
                for href, label in self._card.links
                if href.startswith("/profile/") and label
            ),
            key=lambda item: len(item[1]),
            default=None,
        )
        if profile and text:
            profile_path, seller = profile
            profile_url = f"https://metaforge.app{profile_path}"
            item_links = sorted(
                href
                for href, _ in self._card.links
                if href.startswith("/arc-raiders/database/item/")
            )
            fingerprint = json.dumps(
                [profile_path, item_links, text],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            listing_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
            self.trades.append(
                Trade(
                    listing_id=listing_id,
                    seller=seller,
                    profile_url=profile_url,
                    text=text,
                )
            )
        self._card = None


def parse_market_html(html: str, listing_type: str = "sell") -> list[Trade]:
    """Parse rendered cards and their embedded marketplace records."""
    if listing_type not in {"buy", "sell"}:
        raise ValueError("Listing type must be 'buy' or 'sell'.")
    parser = _MarketHTMLParser()
    parser.feed(html)
    parser.close()
    records = _extract_market_records(html)
    if records is None and not parser.trades:
        if re.search(r"\b(?:no trades|no listings|no results)\b", html, re.IGNORECASE):
            return []
        raise TradeParseError(
            "No marketplace listing cards or embedded listing records were found; "
            "the page markup may have changed or the response may be an access challenge."
        )
    if records is None:
        raise TradeParseError("No embedded marketplace listing records were found.")
    if len(parser.trades) != len(records):
        raise TradeParseError(
            "Marketplace cards do not match the embedded listing records "
            f"({len(parser.trades)} cards, {len(records)} records)."
        )
    return [
        _trade_from_record(card, record, listing_type)
        for card, record in zip(parser.trades, records)
    ]


def _extract_market_records(html: str) -> list[dict[str, object]] | None:
    decoder = json.JSONDecoder()
    position = 0
    while True:
        start = html.find('{"status":200', position)
        if start < 0:
            return None
        try:
            response, consumed = decoder.raw_decode(html[start:])
        except json.JSONDecodeError:
            position = start + 1
            continue
        position = start + max(consumed, 1)
        if not isinstance(response, dict) or not isinstance(response.get("body"), str):
            continue
        try:
            body = json.loads(response["body"])
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict):
            continue
        data = body.get("data")
        if (
            isinstance(data, list)
            and not data
            and re.search(r"\b(?:no trades|no listings|no results)\b", html, re.IGNORECASE)
        ):
            return []
        if isinstance(data, list) and data and all(
            isinstance(record, dict)
            and {"id", "created_at", "item", "user_profile", "user_reputation"}
            <= record.keys()
            for record in data
        ):
            return data


def _trade_from_record(
    card: Trade, record: dict[str, object], listing_type: str
) -> Trade:
    created_at_value = record.get("created_at")
    if not isinstance(created_at_value, str):
        raise TradeParseError("A marketplace record has no valid creation timestamp.")
    try:
        created_at = datetime.fromisoformat(
            created_at_value.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise TradeParseError(
            f"A marketplace record has an invalid creation timestamp: {exc}"
        ) from exc
    if created_at.tzinfo is None:
        raise TradeParseError("A marketplace creation timestamp has no timezone.")

    item = record.get("item")
    item_name = item.get("name") if isinstance(item, dict) else None
    if not isinstance(item_name, str) or not item_name:
        item_name = str(record.get("item_id", "Unknown item"))

    user_profile = record.get("user_profile")
    if not isinstance(user_profile, dict):
        raise TradeParseError("A marketplace record has no trader profile.")
    username = user_profile.get("username")
    seller = username if isinstance(username, str) and username else card.seller

    reputation = record.get("user_reputation")
    rating = "Unrated"
    if isinstance(reputation, dict):
        percentage = reputation.get("positive_percentage")
        total_ratings = reputation.get("total_ratings")
        if isinstance(percentage, (int, float)) and isinstance(total_ratings, int):
            rating = f"{percentage:g}% ({total_ratings})"

    available = record.get("available_quantity", record.get("quantity"))
    total = record.get("quantity", available)
    stock = f"{available}/{total}" if available is not None else "Unknown"

    description = record.get("description")
    if not isinstance(description, str) or not description.strip():
        description = "—"

    trade_type = record.get("trade_type")
    wants = _format_wants(record, trade_type)
    timestamp = created_at.astimezone(MOSCOW_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M MSK"
    )
    selling_label, wanting_label = (
        ("Buying", "Offering") if listing_type == "buy" else ("Selling", "Wanting")
    )
    text = "\n".join(
        (
            f"Time: {timestamp}",
            f"Stock: {stock}",
            f"Description: {description}",
            f"{selling_label}: {item_name}",
            f"{wanting_label}: {wants}",
        )
    )

    listing_id = record.get("id")
    if not isinstance(listing_id, str) or not listing_id:
        raise TradeParseError("A marketplace record has no listing ID.")
    return Trade(
        listing_id=listing_id,
        seller=seller,
        profile_url=card.profile_url,
        text=text,
        rating=rating,
    )


def _format_wants(record: dict[str, object], trade_type: object) -> str:
    if trade_type == "seed":
        price = record.get("price")
        if price is not None:
            return f"{price} Seeds each"
    if trade_type == "offer":
        return "Open to offers"

    preferences = record.get("preferences")
    if isinstance(preferences, list):
        wanted_items: list[str] = []
        for preference in preferences:
            if not isinstance(preference, dict):
                continue
            item = preference.get("item")
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name:
                continue
            quantity = preference.get("quantity")
            wanted_items.append(
                f"{name} x{quantity}"
                if isinstance(quantity, int) and quantity > 1
                else name
            )
        if wanted_items:
            return ", ".join(wanted_items)

    wants_item_ids = record.get("wants_item_ids")
    if isinstance(wants_item_ids, list) and wants_item_ids:
        return ", ".join(str(item_id).replace("-", " ").title() for item_id in wants_item_ids)
    return "Open to offers"


def validate_market_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "metaforge.app":
        raise ValueError("The market URL must use HTTPS on metaforge.app.")
    if not parsed.path.startswith("/arc-raiders/market"):
        raise ValueError("The URL must point to an ARC Raiders marketplace page.")


def fetch_market_html(url: str) -> str:
    validate_market_url(url)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type.lower():
                raise MetaForgeFetchError(
                    f"Expected an HTML marketplace response, got {content_type!r}."
                )
    except HTTPError as exc:
        if exc.code == 403:
            raise MetaForgeFetchError(
                "MetaForge returned HTTP 403. The current request is blocked by "
                "Cloudflare; this parser does not attempt to solve browser challenges."
            ) from None
        raise MetaForgeFetchError(
            f"MetaForge returned HTTP {exc.code} while fetching the marketplace."
        ) from None
    except URLError as exc:
        raise MetaForgeFetchError(f"Could not reach MetaForge: {exc.reason}") from None

    html = body.decode("utf-8", errors="replace")
    if "Just a moment..." in html or "cf-mitigated" in html:
        raise MetaForgeFetchError(
            "MetaForge returned a Cloudflare challenge instead of marketplace HTML."
        )
    return html


class TradeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS seen_trades "
            "(listing_id TEXT PRIMARY KEY, seller TEXT NOT NULL, "
            "profile_url TEXT NOT NULL, text TEXT NOT NULL, "
            "first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.connection.commit()

    def contains(self, listing_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM seen_trades WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row is not None

    def save(self, trade: Trade) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO seen_trades "
            "(listing_id, seller, profile_url, text) VALUES (?, ?, ?, ?)",
            (trade.listing_id, trade.seller, trade.profile_url, trade.text),
        )
        self.connection.commit()

    def is_initialized(self) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM state WHERE key = 'baseline_created'"
            ).fetchone()
            is not None
        )

    def mark_initialized(self) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES ('baseline_created', '1')"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class TelegramNotifier:
    def __init__(
        self, bot_token: str, chat_id: str, proxy_url: str | None = None
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.opener = None
        if proxy_url:
            proxy = urlsplit(proxy_url)
            if proxy.scheme not in {"http", "https"} or not proxy.hostname:
                raise ValueError(
                    "TELEGRAM_PROXY must be an HTTP(S) proxy URL with a hostname."
                )
            try:
                proxy.port
            except ValueError:
                raise ValueError("TELEGRAM_PROXY contains an invalid port.") from None
            self.opener = build_opener(
                ProxyHandler({"http": proxy_url, "https": proxy_url})
            )

    def send_trade(self, trade: Trade) -> None:
        message = (
            f"New MetaForge trade\n"
            f"Trader: {trade.seller} ({trade.rating})\n"
            f"Profile: {trade.profile_url}\n\n"
            f"{trade.text}"
        )
        if len(message) > TELEGRAM_MAX_MESSAGE_LENGTH:
            message = message[: TELEGRAM_MAX_MESSAGE_LENGTH - 20] + "\n…[truncated]"
        payload = json.dumps(
            {"chat_id": self.chat_id, "text": message}, ensure_ascii=False
        ).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response_context = (
                self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS)
                if self.opener is not None
                else urlopen(request, timeout=HTTP_TIMEOUT_SECONDS)
            )
            with response_context as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            raise TelegramError(
                f"Telegram returned HTTP {exc.code} while sending a message."
            ) from None
        except URLError as exc:
            raise TelegramError(f"Could not reach Telegram: {exc.reason}") from None
        except json.JSONDecodeError as exc:
            raise TelegramError("Telegram returned an invalid JSON response.") from exc
        if not result.get("ok"):
            description = result.get("description", "unknown Telegram API error")
            raise TelegramError(f"Telegram rejected the message: {description}")


def check_for_new_trades(
    url: str,
    store: TradeStore,
    send_trade: Callable[[Trade], None],
    *,
    notify_existing: bool = False,
) -> int:
    listing_type = _listing_type_from_url(url)
    trades = parse_market_html(fetch_market_html(url), listing_type)
    initialized = store.is_initialized()

    if not initialized and not notify_existing:
        for trade in trades:
            store.save(trade)
        store.mark_initialized()
        logging.info("Saved an initial baseline of %d existing trades.", len(trades))
        return 0

    new_count = 0
    for trade in trades:
        if store.contains(trade.listing_id):
            continue
        send_trade(trade)
        store.save(trade)
        new_count += 1

    if not initialized:
        store.mark_initialized()
    return new_count


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def build_market_url(base_url: str, item_id: str, listing_type: str) -> str:
    if not item_id.strip():
        raise ValueError("The item ID cannot be empty.")
    if listing_type not in {"buy", "sell"}:
        raise ValueError("Listing type must be 'buy' or 'sell'.")
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'item_id': item_id, 'listing_type': listing_type})}"


def _listing_type_from_url(url: str) -> str:
    listing_types = parse_qs(urlsplit(url).query).get("listing_type", [])
    if not listing_types or listing_types[-1] not in {"buy", "sell"}:
        raise ValueError("The market URL must include listing_type=buy or listing_type=sell.")
    return listing_types[-1]


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        help="complete MetaForge market URL (overrides --item-id and --listing-type)",
    )
    parser.add_argument(
        "--item-id",
        default=os.getenv("MARKET_ITEM_ID", "propellant-valve-assembly"),
        help="MetaForge item slug (default: MARKET_ITEM_ID)",
    )
    parser.add_argument(
        "--listing-type",
        choices=("buy", "sell"),
        default=os.getenv("MARKET_LISTING_TYPE", "sell"),
        help="listing direction to monitor: buy or sell (default: MARKET_LISTING_TYPE)",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("TRADE_ALERTS_DB", "trade-alerts.sqlite3"),
        help="SQLite state path (default: trade-alerts.sqlite3)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep checking at the configured interval instead of checking once",
    )
    parser.add_argument(
        "--notify-existing",
        action="store_true",
        help="send alerts for listings already on the page during the first run",
    )
    parser.add_argument(
        "--interval",
        type=_positive_int,
        default=_positive_int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        help="poll interval in seconds when --watch is enabled",
    )
    return parser.parse_args(argv)


def main() -> int:
    log_path = os.getenv("TRADE_ALERTS_LOG_FILE")
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            filename=log_path,
            filemode="a",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    args = _arguments()
    try:
        market_url = args.url or build_market_url(
            os.getenv("MARKET_BASE_URL", "https://metaforge.app/arc-raiders/market"),
            args.item_id,
            args.listing_type,
        )
    except ValueError as exc:
        logging.error("%s", exc)
        return 2
    global HTTP_TIMEOUT_SECONDS
    try:
        HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
        if HTTP_TIMEOUT_SECONDS <= 0:
            raise ValueError("must be greater than zero")
    except ValueError as exc:
        logging.error("HTTP_TIMEOUT_SECONDS must be a positive integer: %s", exc)
        return 2

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    proxy_url = os.getenv("TELEGRAM_PROXY")
    if not token or not chat_id:
        logging.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before running.")
        return 2
    try:
        validate_market_url(market_url)
    except ValueError as exc:
        logging.error("%s", exc)
        return 2

    try:
        notifier = TelegramNotifier(token, chat_id, proxy_url)
    except ValueError as exc:
        logging.error("%s", exc)
        return 2

    store = TradeStore(args.db)
    try:
        while True:
            try:
                count = check_for_new_trades(
                    market_url,
                    store,
                    notifier.send_trade,
                    notify_existing=args.notify_existing,
                )
                logging.info("Check complete: %d new trade(s).", count)
            except TradeAlertsError as exc:
                logging.error("Trade check failed: %s", exc)
                if not args.watch:
                    return 1
            if not args.watch:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logging.info("Stopped.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
