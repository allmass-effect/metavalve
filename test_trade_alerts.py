import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from trade_alerts import (
    TradeParseError,
    TradeStore,
    TelegramNotifier,
    _arguments,
    build_market_url,
    check_for_new_trades,
    parse_market_html,
)


def listing_card(
    seller: str = "raider-one",
    online: str = "Online",
    stock: str = "4/4",
    offer: str = "Selling rare gear",
) -> str:
    return f"""
    <div class="flex flex-col gap-0.5 p-1 sm:p-1.5">
      <a href="/profile/{seller}-123?game=arc-raiders">{seller}</a>
      <span>100% (42)</span><span>{online}</span><span>today · 15:08</span>
      <span>Stock:</span><span>{stock}</span>
      <span>Selling</span><button title="{offer}">Description</button>
      <span>Wanting</span>
      <a href="/arc-raiders/database/item/propellant-valve-assembly">
        Propellant Valve Assembly
      </a>
    </div>
    """


def market_page(
    seller: str = "raider-one",
    *,
    created_at: str = "2026-10-28T09:08:00+00:00",
    stock: int = 4,
    description: str | None = "Selling rare gear",
    trade_type: str = "barter",
    price: int | None = None,
    preferences: list[dict[str, object]] | None = None,
) -> str:
    record = {
        "id": f"listing-{seller}",
        "created_at": created_at,
        "item_id": "propellant-valve-assembly",
        "quantity": stock,
        "available_quantity": stock,
        "description": description,
        "trade_type": trade_type,
        "price": price,
        "preferences": preferences
        if preferences is not None
        else [
            {
                "item": {"name": "ARC Data Module"},
                "quantity": 2,
            }
        ],
        "wants_item_ids": ["arc-data-module"],
        "item": {"name": "Propellant Valve Assembly"},
        "user_profile": {"username": f"{seller}#1234"},
        "user_reputation": {
            "positive_percentage": 98.5,
            "total_ratings": 42,
        },
    }
    response = {
        "status": 200,
        "statusText": "OK",
        "headers": {},
        "body": json.dumps({"data": [record]}, separators=(",", ":")),
    }
    return listing_card(seller=seller, stock=f"{stock}/{stock}") + json.dumps(
        response, separators=(",", ":")
    )


class ParseMarketHTMLTests(unittest.TestCase):
    def test_extracts_seller_and_listing_text(self) -> None:
        trades = parse_market_html(market_page())

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].seller, "raider-one#1234")
        self.assertEqual(trades[0].rating, "98.5% (42)")
        self.assertEqual(
            trades[0].profile_url,
            "https://metaforge.app/profile/raider-one-123?game=arc-raiders",
        )
        self.assertIn("Propellant Valve Assembly", trades[0].text)
        self.assertIn("Selling rare gear", trades[0].text)

    def test_ignores_online_status_rating_and_stock_for_deduplication(self) -> None:
        first = parse_market_html(market_page())[0]
        updated_html = market_page(stock=2).replace("Online", "Offline").replace(
            "today · 15:08", "yesterday · 15:08"
        )
        updated = parse_market_html(updated_html)[0]

        self.assertEqual(first.listing_id, updated.listing_id)

    def test_reports_markup_change_instead_of_returning_no_trades(self) -> None:
        with self.assertRaises(TradeParseError):
            parse_market_html("<html><body>Marketplace</body></html>")

    def test_accepts_an_explicit_empty_results_page(self) -> None:
        self.assertEqual(parse_market_html("<p>No listings found</p>"), [])

    def test_prefers_full_profile_label_to_avatar_initial(self) -> None:
        html = """
        <div class="flex flex-col gap-0.5 p-1 sm:p-1.5">
          <a href="/profile/user-123?game=arc-raiders">T</a>
          <a href="/profile/user-123?game=arc-raiders">T-martyrplayarc</a>
          <span>Selling</span>
          <a href="/arc-raiders/database/item/propellant-valve-assembly">
            Propellant Valve Assembly
          </a>
        </div>
        """

        wrapped = html + json.dumps(
            {
                "status": 200,
                "statusText": "OK",
                "headers": {},
                "body": json.dumps(
                    {
                        "data": [
                            {
                                "id": "listing-test",
                                "created_at": "2026-10-28T00:00:00+00:00",
                                "item_id": "propellant-valve-assembly",
                                "item": {"name": "Propellant Valve Assembly"},
                                "user_profile": {"username": "T-martyrplayarc#1234"},
                                "user_reputation": {
                                    "positive_percentage": 100,
                                    "total_ratings": 38,
                                },
                            }
                        ]
                    },
                    separators=(",", ":"),
                ),
            },
            separators=(",", ":"),
        )
        trade = parse_market_html(wrapped)[0]

        self.assertEqual(trade.seller, "T-martyrplayarc#1234")
        self.assertNotIn("T-martyrplayarc", trade.text)


class MarketURLTests(unittest.TestCase):
    def test_builds_market_url_from_dynamic_item_and_listing_type(self) -> None:
        self.assertEqual(
            build_market_url(
                "https://metaforge.app/arc-raiders/market",
                "herbal-bandage",
                "buy",
            ),
            "https://metaforge.app/arc-raiders/market"
            "?item_id=herbal-bandage&listing_type=buy",
        )

    def test_encodes_item_id(self) -> None:
        self.assertEqual(
            build_market_url(
                "https://metaforge.app/arc-raiders/market",
                "item name",
                "sell",
            ),
            "https://metaforge.app/arc-raiders/market"
            "?item_id=item+name&listing_type=sell",
        )

    def test_cli_options_accept_dynamic_market_parameters(self) -> None:
        args = _arguments(
            ["--item-id", "herbal-bandage", "--listing-type", "buy", "--watch", "--interval", "90"]
        )

        self.assertEqual(args.item_id, "herbal-bandage")
        self.assertEqual(args.listing_type, "buy")
        self.assertTrue(args.watch)
        self.assertEqual(args.interval, 90)

    def test_rejects_empty_item_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            build_market_url(
                "https://metaforge.app/arc-raiders/market",
                " ",
                "sell",
            )


class TradeMessageTests(unittest.TestCase):
    def test_formats_requested_fields_with_moscow_time_and_newlines(self) -> None:
        trade = parse_market_html(market_page())[0]

        self.assertEqual(
            trade.text,
            "Time: 2026-10-28 12:08 MSK\n"
            "Stock: 4/4\n"
            "Description: Selling rare gear\n"
            "Selling: Propellant Valve Assembly\n"
            "Wanting: ARC Data Module x2",
        )
        self.assertEqual(trade.seller, "raider-one#1234")
        self.assertEqual(trade.rating, "98.5% (42)")

    def test_buy_listing_uses_buying_and_offering_labels(self) -> None:
        trade = parse_market_html(market_page(), "buy")[0]

        self.assertEqual(
            trade.text.splitlines()[-2:],
            [
                "Buying: Propellant Valve Assembly",
                "Offering: ARC Data Module x2",
            ],
        )

    def test_listing_type_is_read_from_url(self) -> None:
        from trade_alerts import _listing_type_from_url

        self.assertEqual(
            _listing_type_from_url(
                "https://metaforge.app/arc-raiders/market"
                "?item_id=herbal-bandage&listing_type=buy"
            ),
            "buy",
        )
        self.assertEqual(
            _listing_type_from_url(
                "https://metaforge.app/arc-raiders/market"
                "?item_id=propellant-valve-assembly&listing_type=sell"
            ),
            "sell",
        )

    def test_missing_listing_type_is_rejected(self) -> None:
        from trade_alerts import _listing_type_from_url

        with self.assertRaisesRegex(ValueError, "must include listing_type"):
            _listing_type_from_url("https://metaforge.app/arc-raiders/market")


class NewTradeAlertTests(unittest.TestCase):
    def test_first_run_baselines_and_later_run_alerts_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TradeStore(Path(directory) / "trades.sqlite3")
            messages = []
            html = market_page()

            def fetch(_url: str) -> str:
                return html

            import trade_alerts

            original_fetch = trade_alerts.fetch_market_html
            trade_alerts.fetch_market_html = fetch
            try:
                self.assertEqual(
                    check_for_new_trades("https://metaforge.app/arc-raiders/market?listing_type=sell",
                                         store, messages.append),
                    0,
                )
                self.assertEqual(messages, [])

                html = market_page(seller="raider-two", description="New offer")
                self.assertEqual(
                    check_for_new_trades("https://metaforge.app/arc-raiders/market?listing_type=sell",
                                         store, messages.append),
                    1,
                )
                self.assertEqual(len(messages), 1)

                self.assertEqual(
                    check_for_new_trades("https://metaforge.app/arc-raiders/market?listing_type=sell",
                                         store, messages.append),
                    0,
                )
                self.assertEqual(len(messages), 1)
            finally:
                trade_alerts.fetch_market_html = original_fetch
                store.close()

    def test_first_run_can_alert_for_existing_listings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TradeStore(Path(directory) / "trades.sqlite3")
            messages = []
            import trade_alerts

            original_fetch = trade_alerts.fetch_market_html
            trade_alerts.fetch_market_html = lambda _url: market_page()
            try:
                self.assertEqual(
                    check_for_new_trades(
                        "https://metaforge.app/arc-raiders/market?listing_type=sell",
                        store,
                        messages.append,
                        notify_existing=True,
                    ),
                    1,
                )
                self.assertEqual(len(messages), 1)
            finally:
                trade_alerts.fetch_market_html = original_fetch
                store.close()


class TelegramProxyTests(unittest.TestCase):
    def test_sends_through_configured_proxy(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        opener = MagicMock()
        opener.open.return_value = response
        trade = parse_market_html(market_page())[0]

        with patch("trade_alerts.build_opener", return_value=opener) as build_opener_mock:
            notifier = TelegramNotifier(
                "test-token", "12345", "http://proxy.example:8080"
            )
            notifier.send_trade(trade)

        handler = build_opener_mock.call_args.args[0]
        self.assertEqual(
            handler.proxies,
            {
                "http": "http://proxy.example:8080",
                "https": "http://proxy.example:8080",
            },
        )
        opener.open.assert_called_once()
        sent_message = json.loads(opener.open.call_args.args[0].data)["text"]
        self.assertIn("Trader: raider-one#1234 (98.5% (42))", sent_message)
        self.assertIn("Time: 2026-10-28 12:08 MSK\nStock: 4/4", sent_message)

    def test_rejects_non_http_proxy_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTP\\(S\\) proxy URL"):
            TelegramNotifier("test-token", "12345", "socks5://proxy.example:1080")


if __name__ == "__main__":
    unittest.main()
