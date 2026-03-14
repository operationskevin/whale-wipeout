"""Tests for WhaleWipeout."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import whale_wipeout as ww


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

def _market(
    question="Nuggets vs. Thunder",
    slug="nuggets-vs-thunder",
    outcomes=None,
    prices=None,
    token_ids=None,
    volume=100_000,
    closed_time="2026-03-10 04:00:00+00",
    condition_id="0xabc123",
    group_item_title="",
    description="",
):
    """Build a fake Gamma API market dict."""
    return {
        "question": question,
        "slug": slug,
        "outcomes": json.dumps(outcomes or ["Yes", "No"]),
        "outcomePrices": json.dumps(prices or ["1", "0"]),
        "clobTokenIds": json.dumps(token_ids or ["token_win", "token_lose"]),
        "volumeNum": volume,
        "closedTime": closed_time,
        "conditionId": condition_id,
        "groupItemTitle": group_item_title,
        "description": description,
    }


def _trade(wallet, side, asset, size, price, name="User1", pseudonym="Pseudo1"):
    return {
        "proxyWallet": wallet,
        "side": side,
        "asset": asset,
        "size": size,
        "price": price,
        "name": name,
        "pseudonym": pseudonym,
    }


# ---------------------------------------------------------------------------
# parse_json_field
# ---------------------------------------------------------------------------

class TestParseJsonField:
    def test_parses_json_string(self):
        assert ww.parse_json_field('["a", "b"]') == ["a", "b"]

    def test_returns_list_as_is(self):
        assert ww.parse_json_field(["a", "b"]) == ["a", "b"]

    def test_invalid_json_returns_empty_list(self):
        assert ww.parse_json_field("not json") == []

    def test_non_list_non_string_returns_empty_list(self):
        assert ww.parse_json_field(123) == []
        assert ww.parse_json_field(None) == []

    def test_empty_string(self):
        assert ww.parse_json_field("") == []

    def test_json_object_string(self):
        result = ww.parse_json_field('{"key": "val"}')
        # Returns the parsed dict; get_losing_outcome handles non-list via len checks
        assert result == {"key": "val"}


# ---------------------------------------------------------------------------
# is_real_world_event
# ---------------------------------------------------------------------------

class TestIsRealWorldEvent:
    def test_sports_market_passes(self):
        m = _market(question="Nuggets vs. Thunder", volume=500_000)
        assert ww.is_real_world_event(m) is True

    def test_politics_market_passes(self):
        m = _market(question="Will Biden win the 2028 election?", volume=1_000_000)
        assert ww.is_real_world_event(m) is True

    def test_bitcoin_excluded(self):
        m = _market(question="Bitcoin Up or Down - March 10", volume=500_000)
        assert ww.is_real_world_event(m) is False

    def test_ethereum_excluded(self):
        m = _market(question="Ethereum price above 5000?", volume=100_000)
        assert ww.is_real_world_event(m) is False

    def test_nikkei_excluded(self):
        m = _market(question="Will Nikkei 225 hit 44000?", volume=100_000)
        assert ww.is_real_world_event(m) is False

    def test_crypto_in_slug_excluded(self):
        m = _market(question="Some market", slug="crypto-prediction", volume=10_000)
        assert ww.is_real_world_event(m) is False

    def test_low_volume_excluded(self):
        m = _market(question="Will it rain tomorrow?", volume=100)
        assert ww.is_real_world_event(m) is False

    def test_zero_volume_excluded(self):
        m = _market(question="Some market", volume=0)
        assert ww.is_real_world_event(m) is False

    def test_none_volume_excluded(self):
        m = _market(question="Some market", volume=0)
        m["volumeNum"] = None
        assert ww.is_real_world_event(m) is False

    def test_volume_at_threshold(self):
        m = _market(question="Will it rain?", volume=500)
        assert ww.is_real_world_event(m) is True

    def test_volume_below_threshold(self):
        m = _market(question="Will it rain?", volume=499)
        assert ww.is_real_world_event(m) is False

    def test_description_not_checked_for_keywords(self):
        """Description may contain 'token' or 'index' generically — should not trigger exclusion."""
        m = _market(
            question="Will SS Lazio win?",
            description="This token resolves based on the index of outcomes.",
            volume=50_000,
        )
        assert ww.is_real_world_event(m) is True

    def test_group_item_title_checked(self):
        m = _market(question="Some market", group_item_title="btc price", volume=10_000)
        assert ww.is_real_world_event(m) is False

    def test_hang_seng_excluded(self):
        m = _market(question="Will Hang Seng hit 24000?", volume=50_000)
        assert ww.is_real_world_event(m) is False

    def test_up_or_down_excluded(self):
        m = _market(question="Solana Up or Down - March 10", volume=50_000)
        assert ww.is_real_world_event(m) is False

    def test_case_insensitive(self):
        m = _market(question="BITCOIN price prediction", volume=50_000)
        assert ww.is_real_world_event(m) is False


# ---------------------------------------------------------------------------
# get_losing_outcomes
# ---------------------------------------------------------------------------

class TestGetLosingOutcomes:
    def test_yes_wins_no_loses(self):
        m = _market(outcomes=["Yes", "No"], prices=["1", "0"], token_ids=["t1", "t2"])
        result = ww.get_losing_outcomes(m)
        assert result == [{"outcome": "No", "token_id": "t2", "index": 1}]

    def test_no_wins_yes_loses(self):
        m = _market(outcomes=["Yes", "No"], prices=["0", "1"], token_ids=["t1", "t2"])
        result = ww.get_losing_outcomes(m)
        assert result == [{"outcome": "Yes", "token_id": "t1", "index": 0}]

    def test_named_outcomes(self):
        m = _market(
            outcomes=["Nuggets", "Thunder"],
            prices=["0", "1"],
            token_ids=["t_nug", "t_thu"],
        )
        result = ww.get_losing_outcomes(m)
        assert len(result) == 1
        assert result[0]["outcome"] == "Nuggets"
        assert result[0]["token_id"] == "t_nug"

    def test_no_clear_loser_returns_empty(self):
        m = _market(outcomes=["Yes", "No"], prices=["0.5", "0.5"], token_ids=["t1", "t2"])
        assert ww.get_losing_outcomes(m) == []

    def test_empty_outcomes(self):
        m = {"outcomes": "[]", "outcomePrices": "[]", "clobTokenIds": "[]"}
        assert ww.get_losing_outcomes(m) == []

    def test_mismatched_lengths(self):
        m = _market(outcomes=["Yes", "No"], prices=["1"], token_ids=["t1", "t2"])
        assert ww.get_losing_outcomes(m) == []

    def test_json_string_fields(self):
        """Fields come as JSON strings from the API."""
        m = {
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0", "1"]',
            "clobTokenIds": '["tok_up", "tok_down"]',
        }
        result = ww.get_losing_outcomes(m)
        assert result == [{"outcome": "Up", "token_id": "tok_up", "index": 0}]

    def test_missing_fields(self):
        assert ww.get_losing_outcomes({}) == []

    def test_three_way_market_two_losers(self):
        """3-way market (Win/Draw/Lose): two outcomes go to 0."""
        m = _market(
            outcomes=["Frankfurt", "Draw", "Heidenheim"],
            prices=["0", "0", "1"],
            token_ids=["t_fra", "t_draw", "t_hei"],
        )
        result = ww.get_losing_outcomes(m)
        assert len(result) == 2
        assert result[0] == {"outcome": "Frankfurt", "token_id": "t_fra", "index": 0}
        assert result[1] == {"outcome": "Draw", "token_id": "t_draw", "index": 1}

    def test_three_way_returns_token_ids_as_set_for_find_functions(self):
        """Confirm token_ids from all losers can be passed to find functions."""
        m = _market(
            outcomes=["Frankfurt", "Draw", "Heidenheim"],
            prices=["0", "0", "1"],
            token_ids=["t_fra", "t_draw", "t_hei"],
        )
        losers = ww.get_losing_outcomes(m)
        losing_token_ids = {l["token_id"] for l in losers}
        assert losing_token_ids == {"t_fra", "t_draw"}

    def test_neg_risk_style_prices(self):
        """negRisk markets resolve to ~0.9995/0.0005 instead of 1/0 — loser still detected."""
        m = _market(
            outcomes=["Frankfurt not to win", "Frankfurt to win"],
            prices=["0.0005", "0.9995"],
            token_ids=["t_not_win", "t_win"],
        )
        result = ww.get_losing_outcomes(m)
        assert len(result) == 1
        assert result[0]["outcome"] == "Frankfurt not to win"
        assert result[0]["token_id"] == "t_not_win"

    def test_active_market_not_flagged(self):
        """An active 60/40 market should not return any losers."""
        m = _market(outcomes=["Yes", "No"], prices=["0.60", "0.40"], token_ids=["t1", "t2"])
        assert ww.get_losing_outcomes(m) == []


# ---------------------------------------------------------------------------
# find_heartbreak_losses
# ---------------------------------------------------------------------------

class TestFindHeartbreakLosses:
    def test_basic_heartbreak(self):
        """User buys losing token at 95% odds for $15k total."""
        trades = [
            _trade("wallet_a", "BUY", "lose_token", 15000, 0.95),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        assert len(results) == 1
        assert results[0]["wallet"] == "wallet_a"
        assert results[0]["net_loss"] == 14250.0  # 15000 * 0.95
        assert results[0]["max_odds"] == 95.0

    def test_partial_sell_reduces_loss(self):
        """User buys $20k worth, sells $8k → net loss $12k."""
        trades = [
            _trade("wallet_a", "BUY", "lose_token", 20000, 0.95),
            _trade("wallet_a", "SELL", "lose_token", 10000, 0.80),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        assert len(results) == 1
        # net_loss = 20000*0.95 - 10000*0.80 = 19000 - 8000 = 11000
        assert results[0]["net_loss"] == 11000.0

    def test_sell_enough_no_heartbreak(self):
        """User sells enough to reduce net loss below threshold."""
        trades = [
            _trade("wallet_a", "BUY", "lose_token", 12000, 0.95),
            _trade("wallet_a", "SELL", "lose_token", 5000, 0.90),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        # net_loss = 12000*0.95 - 5000*0.90 = 11400 - 4500 = 6900 < 10000
        assert len(results) == 0

    def test_below_odds_threshold(self):
        """User bought at 85% odds — not a heartbreak."""
        trades = [
            _trade("wallet_a", "BUY", "lose_token", 20000, 0.85),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        assert len(results) == 0

    def test_below_loss_threshold(self):
        """User bought at high odds but small amount."""
        trades = [
            _trade("wallet_a", "BUY", "lose_token", 5000, 0.95),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        # 5000 * 0.95 = 4750 < 10000
        assert len(results) == 0

    def test_wrong_token_ignored(self):
        """Trades on the winning token should be ignored."""
        trades = [
            _trade("wallet_a", "BUY", "win_token", 50000, 0.95),
            _trade("wallet_a", "BUY", "lose_token", 500, 0.95),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        assert len(results) == 0  # only $475 on losing token

    def test_multiple_users(self):
        """Multiple users, only some qualify."""
        trades = [
            _trade("whale", "BUY", "lose_token", 50000, 0.98, name="BigWhale"),
            _trade("small", "BUY", "lose_token", 100, 0.95, name="SmallFish"),
            _trade("lowodds", "BUY", "lose_token", 50000, 0.50, name="LowOdds"),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        assert len(results) == 1
        assert results[0]["name"] == "BigWhale"

    def test_empty_trades(self):
        assert ww.find_heartbreak_losses([], "lose_token") == []

    def test_name_fallback_to_pseudonym(self):
        trades = [
            _trade("wallet_a", "BUY", "lose_token", 20000, 0.95, name="", pseudonym="CoolPseudo"),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        assert results[0]["name"] == "CoolPseudo"

    def test_name_fallback_to_wallet(self):
        trades = [
            _trade("0xabcdef1234", "BUY", "lose_token", 20000, 0.95, name="", pseudonym=""),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        assert results[0]["name"] == "0xabcdef12"  # first 10 chars

    def test_multiple_buys_same_user(self):
        """Multiple buy trades aggregate correctly."""
        trades = [
            _trade("wallet_a", "BUY", "lose_token", 6000, 0.92),
            _trade("wallet_a", "BUY", "lose_token", 7000, 0.95),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        assert len(results) == 1
        # net_loss = 6000*0.92 + 7000*0.95 = 5520 + 6650 = 12170
        assert results[0]["net_loss"] == 12170.0
        assert results[0]["max_odds"] == 95.0

    def test_exactly_at_thresholds(self):
        """At exactly 90% odds and exactly $10k loss."""
        # Need size*price = 10000 with price = 0.90
        # size = 10000/0.90 ≈ 11111.11
        trades = [
            _trade("wallet_a", "BUY", "lose_token", 11111.12, 0.90),
        ]
        results = ww.find_heartbreak_losses(trades, {"lose_token"})
        assert len(results) == 1

    def test_three_way_market_catches_second_losing_token(self):
        """Whale who bet on the 2nd losing outcome of a 3-way market is caught."""
        trades = [
            _trade("wallet_draw", "BUY", "t_draw", 20000, 0.95, name="DrawBetter"),
            _trade("wallet_win", "BUY", "t_fra", 500, 0.95, name="SmallFra"),
        ]
        # Both t_fra and t_draw are losing tokens; t_hei won
        results = ww.find_heartbreak_losses(trades, {"t_fra", "t_draw"})
        assert len(results) == 1
        assert results[0]["name"] == "DrawBetter"


# ---------------------------------------------------------------------------
# generate_draft_post
# ---------------------------------------------------------------------------

class TestGenerateDraftPost:
    def test_contains_market_question(self):
        hb = {"wallet": "0x123", "name": "User", "net_loss": 50000, "max_odds": 95.0}
        m = _market(question="Will it rain in NYC?")
        post = ww.generate_draft_post(hb, m, "No")
        assert "Will it rain in NYC?" in post

    def test_contains_loss_amount(self):
        hb = {"wallet": "0x123", "name": "User", "net_loss": 50000, "max_odds": 95.0}
        m = _market(question="Test")
        post = ww.generate_draft_post(hb, m, "No")
        assert "$50.0K" in post

    def test_contains_odds(self):
        hb = {"wallet": "0x123", "name": "User", "net_loss": 50000, "max_odds": 95.0}
        m = _market(question="Test")
        post = ww.generate_draft_post(hb, m, "No")
        assert "95.0%" in post

    def test_contains_polymarket_url(self):
        hb = {"wallet": "0x123", "name": "User", "net_loss": 50000, "max_odds": 95.0}
        m = _market(question="Test")
        post = ww.generate_draft_post(hb, m, "No")
        assert "polymarket" in post.lower()

    def test_million_dollar_format(self):
        hb = {"wallet": "0x123", "name": "User", "net_loss": 1_500_000, "max_odds": 99.0}
        m = _market(question="Test")
        post = ww.generate_draft_post(hb, m, "No")
        assert "$1.5M" in post

    def test_sub_thousand_format(self):
        hb = {"wallet": "0x123", "name": "User", "net_loss": 750, "max_odds": 95.0}
        m = _market(question="Test")
        post = ww.generate_draft_post(hb, m, "No")
        assert "$750" in post

    def test_different_wallets_get_different_templates(self):
        """Template rotation should produce variety across wallets."""
        m = _market(question="Test")
        posts = set()
        for i in range(20):
            hb = {"wallet": f"0x{i:040x}", "name": "User", "net_loss": 50000, "max_odds": 95.0}
            posts.add(ww.generate_draft_post(hb, m, "No"))
        # With 3 templates and 20 different wallets, we should see multiple templates
        assert len(posts) > 1


# ---------------------------------------------------------------------------
# get_market_url
# ---------------------------------------------------------------------------

class TestGetMarketUrl:
    def test_uses_event_slug_when_present(self):
        market = {"slug": "market-slug", "events": [{"slug": "event-slug"}]}
        assert ww.get_market_url(market) == "https://polymarket.com/event/event-slug"

    def test_falls_back_to_market_slug(self):
        market = {"slug": "market-slug", "events": []}
        assert ww.get_market_url(market) == "https://polymarket.com/event/market-slug"

    def test_falls_back_to_bare_url_when_no_slug(self):
        market = {}
        assert ww.get_market_url(market) == "https://polymarket.com"

    def test_event_with_no_slug_falls_back_to_market_slug(self):
        market = {"slug": "market-slug", "events": [{"id": "123"}]}
        assert ww.get_market_url(market) == "https://polymarket.com/event/market-slug"

    def test_none_events_falls_back_to_market_slug(self):
        market = {"slug": "market-slug", "events": None}
        assert ww.get_market_url(market) == "https://polymarket.com/event/market-slug"


# ---------------------------------------------------------------------------
# load_seen / save_seen
# ---------------------------------------------------------------------------

class TestSeenPersistence:
    def test_load_nonexistent_returns_empty(self, tmp_path):
        with patch.object(ww, "SEEN_FILE", tmp_path / "nonexistent.json"):
            assert ww.load_seen() == set()

    def test_save_and_load_roundtrip(self, tmp_path):
        seen_file = tmp_path / "seen.json"
        with patch.object(ww, "SEEN_FILE", seen_file):
            original = {"loss_1", "loss_2", "loss_3"}
            ww.save_seen(original)
            loaded = ww.load_seen()
            assert loaded == original

    def test_save_overwrites(self, tmp_path):
        seen_file = tmp_path / "seen.json"
        with patch.object(ww, "SEEN_FILE", seen_file):
            ww.save_seen({"a", "b"})
            ww.save_seen({"c", "d"})
            loaded = ww.load_seen()
            assert loaded == {"c", "d"}


# ---------------------------------------------------------------------------
# get_resolved_markets_today
# ---------------------------------------------------------------------------

class TestGetResolvedMarketsToday:
    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_collects_todays_markets(self, mock_sleep, mock_get):
        today = "2026-03-10"
        with patch("whale_wipeout.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = today

            mock_resp = MagicMock()
            mock_resp.json.return_value = [
                {"closedTime": "2026-03-10 12:00:00+00", "question": "Q1"},
                {"closedTime": "2026-03-10 08:00:00+00", "question": "Q2"},
                {"closedTime": "2026-03-09 23:00:00+00", "question": "Old"},
            ]
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            result = ww.get_resolved_markets_today()
            assert len(result) == 2
            assert result[0]["question"] == "Q1"
            assert result[1]["question"] == "Q2"

    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_skips_markets_without_closed_time(self, mock_sleep, mock_get):
        today = "2026-03-10"
        with patch("whale_wipeout.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = today

            mock_resp = MagicMock()
            mock_resp.json.return_value = [
                {"closedTime": "", "question": "NoTime"},
                {"closedTime": "2026-03-10 12:00:00+00", "question": "Valid"},
                {"closedTime": "2026-03-09 12:00:00+00", "question": "Old"},
            ]
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            result = ww.get_resolved_markets_today()
            assert len(result) == 1
            assert result[0]["question"] == "Valid"

    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_empty_response_stops(self, mock_sleep, mock_get):
        today = "2026-03-10"
        with patch("whale_wipeout.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = today

            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            result = ww.get_resolved_markets_today()
            assert result == []


# ---------------------------------------------------------------------------
# get_neg_risk_markets_today
# ---------------------------------------------------------------------------

class TestGetNegRiskMarketsToday:
    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_collects_todays_neg_risk_markets(self, mock_sleep, mock_get):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xneg1",
                "question": "Frankfurt not to win?",
                "finishedTimestamp": f"{today}T16:45:46Z",
                "endDateIso": today,
                "negRisk": True,
            },
            {
                "conditionId": "0xneg2",
                "question": "Draw or not?",
                "finishedTimestamp": f"{today}T18:00:00Z",
                "endDateIso": today,
                "negRisk": True,
            },
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = ww.get_neg_risk_markets_today()
        condition_ids = [m["conditionId"] for m in result]
        assert "0xneg1" in condition_ids
        assert "0xneg2" in condition_ids

    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_old_market_not_included(self, mock_sleep, mock_get):
        """Markets with finishedTimestamp != today should not be included."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xold",
                "question": "Old match",
                "finishedTimestamp": "2020-01-01T18:00:00Z",
                "endDateIso": "2020-01-01",
                "negRisk": True,
            },
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = ww.get_neg_risk_markets_today()
        assert result == []

    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_empty_response_stops(self, mock_sleep, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = ww.get_neg_risk_markets_today()
        assert result == []

    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_request_error_returns_empty(self, mock_sleep, mock_get):
        from requests.exceptions import RequestException
        mock_get.side_effect = RequestException("network error")

        result = ww.get_neg_risk_markets_today()
        assert result == []


# ---------------------------------------------------------------------------
# get_trades_for_market
# ---------------------------------------------------------------------------

class TestGetTradesForMarket:
    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_single_page(self, mock_sleep, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"trade": 1}, {"trade": 2}]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = ww.get_trades_for_market("0xabc")
        assert len(result) == 2

    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_pagination(self, mock_sleep, mock_get):
        page1 = MagicMock()
        page1.json.return_value = [{"t": i} for i in range(500)]
        page1.raise_for_status = MagicMock()

        page2 = MagicMock()
        page2.json.return_value = [{"t": i} for i in range(100)]
        page2.raise_for_status = MagicMock()

        mock_get.side_effect = [page1, page2]

        result = ww.get_trades_for_market("0xabc")
        assert len(result) == 600

    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_request_error_returns_partial(self, mock_sleep, mock_get):
        page1 = MagicMock()
        page1.json.return_value = [{"t": 1}]
        page1.raise_for_status = MagicMock()

        mock_get.side_effect = [page1, Exception("timeout")]
        # First page has 1 item (< 500 limit) so it stops naturally
        result = ww.get_trades_for_market("0xabc")
        assert len(result) == 1

    @patch("whale_wipeout.requests.get")
    @patch("whale_wipeout.time.sleep")
    def test_max_offset_respected(self, mock_sleep, mock_get):
        """Should stop at max_offset even if full pages keep coming."""
        full_page = MagicMock()
        full_page.json.return_value = [{"t": i} for i in range(500)]
        full_page.raise_for_status = MagicMock()
        mock_get.return_value = full_page

        result = ww.get_trades_for_market("0xabc")
        # max_offset=5000, limit=500 → 10 pages max
        assert len(result) == 5000
        assert mock_get.call_count == 10


# ---------------------------------------------------------------------------
# main (integration-style with mocks)
# ---------------------------------------------------------------------------

class TestMain:
    @patch("whale_wipeout.save_seen")
    @patch("whale_wipeout.load_seen")
    @patch("whale_wipeout.get_trades_for_market")
    @patch("whale_wipeout.get_neg_risk_markets_today")
    @patch("whale_wipeout.get_resolved_markets_today")
    def test_full_flow_with_heartbreak(self, mock_markets, mock_neg_risk, mock_trades, mock_load, mock_save, tmp_path):
        drafts_file = tmp_path / "drafts.txt"
        mock_neg_risk.return_value = []
        with patch.object(ww, "DRAFTS_FILE", drafts_file):
            mock_markets.return_value = [
                _market(
                    question="Will SS Lazio win?",
                    outcomes=["Yes", "No"],
                    prices=["0", "1"],
                    token_ids=["t_yes", "t_no"],
                    volume=500_000,
                    condition_id="0xcond1",
                ),
            ]
            mock_trades.return_value = [
                _trade("whale_wallet", "BUY", "t_yes", 20000, 0.95, name="BigWhale"),
            ]
            mock_load.return_value = set()

            ww.main()

            assert drafts_file.exists()
            content = drafts_file.read_text()
            assert "Will SS Lazio win?" in content
            assert "$19.0K" in content
            assert "95.0%" in content
            mock_save.assert_called_once()

    @patch("whale_wipeout.save_seen")
    @patch("whale_wipeout.load_seen")
    @patch("whale_wipeout.get_trades_for_market")
    @patch("whale_wipeout.get_neg_risk_markets_today")
    @patch("whale_wipeout.get_resolved_markets_today")
    def test_deduplication_skips_seen(self, mock_markets, mock_neg_risk, mock_trades, mock_load, mock_save, tmp_path):
        drafts_file = tmp_path / "drafts.txt"
        mock_neg_risk.return_value = []
        with patch.object(ww, "DRAFTS_FILE", drafts_file):
            mock_markets.return_value = [
                _market(
                    outcomes=["Yes", "No"],
                    prices=["0", "1"],
                    token_ids=["t_yes", "t_no"],
                    volume=500_000,
                    condition_id="0xcond1",
                ),
            ]
            mock_trades.return_value = [
                _trade("whale_wallet", "BUY", "t_yes", 20000, 0.95),
            ]
            mock_load.return_value = {"0xcond1:whale_wallet"}

            ww.main()

            assert not drafts_file.exists()  # No new drafts written

    @patch("whale_wipeout.get_neg_risk_markets_today")
    @patch("whale_wipeout.get_resolved_markets_today")
    def test_no_markets_exits_gracefully(self, mock_markets, mock_neg_risk):
        mock_markets.return_value = []
        mock_neg_risk.return_value = []
        ww.main()  # should not raise

    @patch("whale_wipeout.load_seen")
    @patch("whale_wipeout.get_trades_for_market")
    @patch("whale_wipeout.get_neg_risk_markets_today")
    @patch("whale_wipeout.get_resolved_markets_today")
    def test_no_losing_outcome_skipped(self, mock_markets, mock_neg_risk, mock_trades, mock_load):
        mock_neg_risk.return_value = []
        mock_markets.return_value = [
            _market(
                outcomes=["Yes", "No"],
                prices=["0.5", "0.5"],  # No clear loser
                token_ids=["t1", "t2"],
                volume=500_000,
            ),
        ]
        mock_load.return_value = set()

        ww.main()

        mock_trades.assert_not_called()

    @patch("whale_wipeout.save_seen")
    @patch("whale_wipeout.load_seen")
    @patch("whale_wipeout.get_trades_for_market")
    @patch("whale_wipeout.get_neg_risk_markets_today")
    @patch("whale_wipeout.get_resolved_markets_today")
    def test_neg_risk_market_merged_into_scan(self, mock_markets, mock_neg_risk, mock_trades, mock_load, mock_save, tmp_path):
        """negRisk markets returned by get_neg_risk_markets_today are scanned."""
        drafts_file = tmp_path / "drafts.txt"
        with patch.object(ww, "DRAFTS_FILE", drafts_file):
            mock_markets.return_value = []  # No closed markets today
            mock_neg_risk.return_value = [
                _market(
                    question="Frankfurt to win vs Heidenheim?",
                    outcomes=["Frankfurt", "Draw", "Heidenheim"],
                    prices=["0.9995", "0.0005", "0.0005"],
                    token_ids=["t_fra", "t_draw", "t_hei"],
                    volume=800_000,
                    condition_id="0xneg1",
                ),
            ]
            mock_trades.return_value = [
                _trade("big_whale", "BUY", "t_draw", 30000, 0.95, name="DrawWhale"),
            ]
            mock_load.return_value = set()

            ww.main()

            assert drafts_file.exists()
            content = drafts_file.read_text()
            assert "Frankfurt" in content
            mock_save.assert_called_once()

    @patch("whale_wipeout.save_seen")
    @patch("whale_wipeout.load_seen")
    @patch("whale_wipeout.get_trades_for_market")
    @patch("whale_wipeout.get_neg_risk_markets_today")
    @patch("whale_wipeout.get_resolved_markets_today")
    def test_neg_risk_deduplication_no_double_scan(self, mock_markets, mock_neg_risk, mock_trades, mock_load, mock_save, tmp_path):
        """A market appearing in both closed and negRisk lists is only scanned once."""
        drafts_file = tmp_path / "drafts.txt"
        market = _market(
            question="Will it happen?",
            outcomes=["Yes", "No"],
            prices=["0", "1"],
            token_ids=["t_yes", "t_no"],
            volume=500_000,
            condition_id="0xdup",
        )
        mock_neg_risk.return_value = []
        with patch.object(ww, "DRAFTS_FILE", drafts_file):
            mock_markets.return_value = [market]
            mock_neg_risk.return_value = [market]  # Same market in both lists
            mock_trades.return_value = [
                _trade("whale_wallet", "BUY", "t_yes", 30000, 0.95, name="DupWhale"),
            ]
            mock_load.return_value = set()

            ww.main()

            # Trades should only be fetched once (dedup by conditionId)
            assert mock_trades.call_count == 1
