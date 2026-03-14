#!/usr/bin/env python3
"""
WhaleWipeout - Track massive Polymarket betting losses.
Finds "Heartbreak" scenarios: users who held >90% winning odds
that went to $0, with losses >= $10,000.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

MIN_LOSS_USD = 10_000
MIN_ODDS = 0.90  # >90% implied probability
AUTO_POST_MIN_LOSS = 25_000  # Only auto-post losses >= this amount
MAX_POSTS_PER_RUN = 5  # Cap posts per scan run

DRAFTS_FILE = Path(__file__).parent / "drafts.txt"
SEEN_FILE = Path(__file__).parent / ".seen_losses.json"

# Keywords to EXCLUDE from question/slug/group_title
EXCLUDE_KEYWORDS = [
    # Crypto / financial
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
    "crypto", "token", "defi", "nft",
    "s&p", "nasdaq", "dow jones", "nikkei", "ftse", "dax",
    "stock", "index", "fed funds", "interest rate",
    "fdv", "market cap", "price target",
    "gas price", "tvl", "apy",
    "up or down",  # BTC up/down micro markets
    "hang seng", "hsi",
    # Weather / climate
    "temperature", "°c", "°f",
    # Esports to exclude (Dota, R6, Overwatch — not CS/Valorant/CoD/LoL)
    "dota 2", "dota2", "rainbow six", "overwatch",
    # Minor regional leagues
    "del:",        # German DEL hockey
    "saudi club",  # Saudi Pro League
]

# Substrings in the lowercased question that disqualify a market
EXCLUDE_SUBSTRINGS = [
    ": o/u ",               # prop O/U ("Team A vs Team B: O/U 2.5")
    "both teams to score",
    "odd/even total",
    "any player rampage",
    "both teams beat",
    "set 1 games",
    "set 2 games",
    "match o/u",
    ": map 1 winner",
    ": map 2 winner",
    ": game 1 winner",
    ": game 2 winner",
    "total kills",
]

# Prefixes of the lowercased question that disqualify a market
EXCLUDE_PREFIXES = [
    "spread:",
    "map 1:", "map 2:", "map 3:",
    "game 1:", "game 2:", "game 3:",
    "game handicap:", "map handicap:", "games total:",
    "total kills",
]


def is_real_world_event(market: dict) -> bool:
    """Filter to popular sports, politics, culture, esports (CS/Valorant/CoD/LoL), and social content."""
    question = market.get("question", "").lower()
    slug = market.get("slug", "").lower()
    group_title = market.get("groupItemTitle", "").lower()

    # Only check question, slug, and group title — not description
    # (description often contains generic words like "token", "index")
    text = f"{question} {slug} {group_title}"

    for keyword in EXCLUDE_KEYWORDS:
        if keyword in text:
            return False

    for substring in EXCLUDE_SUBSTRINGS:
        if substring in question:
            return False

    for prefix in EXCLUDE_PREFIXES:
        if question.startswith(prefix):
            return False

    # Exclude very low volume markets (likely niche)
    volume = market.get("volumeNum", 0) or 0
    if volume < 500:
        return False

    return True


def get_resolved_markets_today() -> list[dict]:
    """Fetch markets that resolved today (UTC)."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    markets = []
    offset = 0
    limit = 100

    while True:
        params = {
            "closed": "true",
            "limit": limit,
            "offset": offset,
            "order": "closedTime",
            "ascending": "false",
        }
        resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=30)
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        for m in batch:
            closed_time = m.get("closedTime", "")
            if not closed_time:
                continue
            # closedTime format: "2026-03-10 04:46:45+00"
            closed_date = closed_time[:10]
            if closed_date == today_str:
                markets.append(m)
            elif closed_date < today_str:
                # Past today, stop paginating
                return markets

        offset += limit
        time.sleep(0.5)  # Rate limiting

    return markets


def parse_json_field(value):
    """Parse a field that may be a JSON string or already a list."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    return value if isinstance(value, list) else []


def get_losing_outcome(market: dict) -> dict | None:
    """Determine which outcome lost (price went to 0)."""
    outcomes = parse_json_field(market.get("outcomes", []))
    prices = parse_json_field(market.get("outcomePrices", []))
    token_ids = parse_json_field(market.get("clobTokenIds", []))

    if not outcomes or not prices or not token_ids:
        return None
    if len(outcomes) != len(prices) or len(outcomes) != len(token_ids):
        return None

    for i, price in enumerate(prices):
        if float(price) == 0:
            return {
                "outcome": outcomes[i],
                "token_id": token_ids[i],
                "index": i,
            }

    return None


def get_trades_for_market(condition_id: str) -> list[dict]:
    """Fetch all trades for a given market from data API."""
    all_trades = []
    offset = 0
    limit = 500
    max_offset = 5000  # API has a pagination limit

    while offset < max_offset:
        params = {
            "market": condition_id,
            "limit": limit,
            "offset": offset,
        }
        try:
            resp = requests.get(f"{DATA_API}/trades", params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            break

        batch = resp.json()

        if not batch:
            break

        all_trades.extend(batch)

        if len(batch) < limit:
            break

        offset += limit
        time.sleep(0.3)

    return all_trades


def find_heartbreak_losses(trades: list[dict], losing_token_id: str) -> list[dict]:
    """
    Find users who bought the losing outcome at >90% odds and lost >= $10k.

    A "heartbreak" is when someone bought shares at a price > 0.90
    (meaning the market implied >90% chance of winning) and it went to $0.

    Loss = sum of (size * price) for BUY trades on losing side
           minus sum of (size * price) for SELL trades on losing side
    """
    # Aggregate by user
    user_positions: dict[str, dict] = {}

    for trade in trades:
        asset = trade.get("asset", "")
        if asset != losing_token_id:
            continue

        wallet = trade.get("proxyWallet", "")
        side = trade.get("side", "").upper()
        size = float(trade.get("size", 0))
        price = float(trade.get("price", 0))
        name = trade.get("name", "") or trade.get("pseudonym", "") or wallet[:10]

        if wallet not in user_positions:
            user_positions[wallet] = {
                "wallet": wallet,
                "name": name,
                "total_spent": 0.0,
                "total_sold": 0.0,
                "max_buy_price": 0.0,
                "buy_trades": [],
            }

        if side == "BUY":
            cost = size * price
            user_positions[wallet]["total_spent"] += cost
            user_positions[wallet]["buy_trades"].append({"size": size, "price": price})
            if price > user_positions[wallet]["max_buy_price"]:
                user_positions[wallet]["max_buy_price"] = price
        elif side == "SELL":
            proceeds = size * price
            user_positions[wallet]["total_sold"] += proceeds

    # Filter for heartbreak scenarios
    heartbreaks = []
    for wallet, pos in user_positions.items():
        net_loss = pos["total_spent"] - pos["total_sold"]
        if net_loss >= MIN_LOSS_USD and pos["max_buy_price"] >= MIN_ODDS:
            # Calculate weighted avg price of buys at >90%
            high_odds_buys = [t for t in pos["buy_trades"] if t["price"] >= MIN_ODDS]
            if high_odds_buys:
                high_odds_cost = sum(t["size"] * t["price"] for t in high_odds_buys)
                heartbreaks.append({
                    "wallet": wallet,
                    "name": pos["name"],
                    "net_loss": round(net_loss, 2),
                    "max_odds": round(pos["max_buy_price"] * 100, 1),
                    "high_odds_cost": round(high_odds_cost, 2),
                })

    for hb in heartbreaks:
        hb["scenario"] = "heartbreak"

    return heartbreaks


def find_big_losses(trades: list[dict], losing_token_id: str, exclude_wallets: set | None = None) -> list[dict]:
    """
    Find users who lost >= $10k on the losing outcome regardless of odds.
    Excludes wallets already flagged as heartbreaks.
    """
    if exclude_wallets is None:
        exclude_wallets = set()

    user_positions: dict[str, dict] = {}

    for trade in trades:
        asset = trade.get("asset", "")
        if asset != losing_token_id:
            continue

        wallet = trade.get("proxyWallet", "")
        if wallet in exclude_wallets:
            continue

        side = trade.get("side", "").upper()
        size = float(trade.get("size", 0))
        price = float(trade.get("price", 0))
        name = trade.get("name", "") or trade.get("pseudonym", "") or wallet[:10]

        if wallet not in user_positions:
            user_positions[wallet] = {
                "wallet": wallet,
                "name": name,
                "total_spent": 0.0,
                "total_sold": 0.0,
                "max_buy_price": 0.0,
            }

        if side == "BUY":
            user_positions[wallet]["total_spent"] += size * price
            if price > user_positions[wallet]["max_buy_price"]:
                user_positions[wallet]["max_buy_price"] = price
        elif side == "SELL":
            user_positions[wallet]["total_sold"] += size * price

    big_losses = []
    for wallet, pos in user_positions.items():
        net_loss = pos["total_spent"] - pos["total_sold"]
        if net_loss >= MIN_LOSS_USD:
            big_losses.append({
                "wallet": wallet,
                "name": pos["name"],
                "net_loss": round(net_loss, 2),
                "max_odds": round(pos["max_buy_price"] * 100, 1),
                "scenario": "big_loss",
            })

    return big_losses


def generate_draft_post(heartbreak: dict, market: dict, losing_outcome: str) -> str:
    """Generate a deadpan, darkly humorous draft post."""
    question = market.get("question", "Unknown market")
    loss = heartbreak["net_loss"]
    odds = heartbreak["max_odds"]
    name = heartbreak["name"]
    scenario = heartbreak.get("scenario", "heartbreak")

    # Format loss nicely
    if loss >= 1_000_000:
        loss_str = f"${loss / 1_000_000:.1f}M"
    elif loss >= 1_000:
        loss_str = f"${loss / 1_000:.1f}K"
    else:
        loss_str = f"${loss:,.0f}"

    if scenario == "heartbreak":
        templates = [
            (
                f"{name} held {odds}% odds. {loss_str} on the line.\n\n"
                f'"{question}" — resolved against them.\n\n'
                f"That\'s not a bad beat. That\'s a funeral.\n\n"
                f"@WhaleWipeout"
            ),
            (
                f"{name} went in at {odds}% confidence "
                f"and watched {loss_str} evaporate.\n\n"
                f'Market: "{question}"\n\n'
                f"The house doesn\'t always win. But the market does.\n\n"
                f"@WhaleWipeout"
            ),
            (
                f"Polymarket heartbreak alert:\n\n"
                f"{name}: {loss_str} gone. {odds}% sure it was a lock.\n\n"
                f'"{question}"\n\n'
                f"Turns out {odds}% isn\'t 100%. Moment of silence. \U0001F56F\uFE0F\n\n"
                f"@WhaleWipeout"
            ),
        ]
    else:  # big_loss
        templates = [
            (
                f"Whale loss alert:\n\n"
                f"{name} dropped {loss_str} on Polymarket.\n\n"
                f'"{question}" didn\'t go their way.\n\n'
                f"The market always collects.\n\n"
                f"@WhaleWipeout"
            ),
            (
                f"{name}: {loss_str}. Gone.\n\n"
                f'"{question}"\n\n'
                f"No high-odds story. Just a heavy position on the wrong side.\n\n"
                f"@WhaleWipeout"
            ),
            (
                f"It happens to the best of them.\n\n"
                f"{name} lost {loss_str} on \"{question}\"\n\n"
                f"Markets are humbling.\n\n"
                f"@WhaleWipeout"
            ),
        ]

    # Rotate templates based on hash of wallet for variety
    idx = hash(heartbreak["wallet"]) % len(templates)
    return templates[idx]


def post_to_x(text: str) -> bool:
    """Post to X (Twitter) via tweepy OAuth 1.0a. Returns True on success."""
    try:
        import tweepy  # type: ignore
    except ImportError:
        print("  [X] tweepy not installed — skipping")
        return False

    api_key = os.environ.get("X_API_KEY", "")
    api_secret = os.environ.get("X_API_SECRET", "")
    access_token = os.environ.get("X_ACCESS_TOKEN", "")
    access_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

    if not all([api_key, api_secret, access_token, access_secret]):
        return False

    if len(text) > 280:
        text = text[:277] + "..."

    try:
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        client.create_tweet(text=text)
        return True
    except Exception as e:
        print(f"  [X] Post failed: {e}")
        return False


def post_to_threads(text: str) -> bool:
    """Post to Threads via the Threads API. Returns True on success."""
    user_id = os.environ.get("THREADS_USER_ID", "")
    access_token = os.environ.get("THREADS_ACCESS_TOKEN", "")

    if not user_id or not access_token:
        return False

    base = "https://graph.threads.net/v1.0"

    try:
        # Step 1: Create text container
        resp = requests.post(
            f"{base}/{user_id}/threads",
            params={
                "media_type": "TEXT",
                "text": text,
                "access_token": access_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        creation_id = resp.json().get("id")
        if not creation_id:
            return False

        time.sleep(2)  # Threads recommends waiting before publishing

        # Step 2: Publish
        resp = requests.post(
            f"{base}/{user_id}/threads_publish",
            params={"creation_id": creation_id, "access_token": access_token},
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  [Threads] Post failed: {e}")
        return False


def load_seen() -> set:
    """Load previously seen loss identifiers to avoid duplicates."""
    if SEEN_FILE.exists():
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    """Save seen loss identifiers."""
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def main():
    print("=" * 60)
    print("WhaleWipeout - Polymarket Loss Scanner")
    print("=" * 60)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"\nScanning markets resolved on {today} (UTC)...")

    # Step 1: Get today's resolved markets
    markets = get_resolved_markets_today()
    print(f"Found {len(markets)} markets resolved today.")

    # Step 2: Filter to qualifying events
    real_world = [m for m in markets if is_real_world_event(m)]
    print(f"After filtering: {len(real_world)} qualifying markets.\n")

    if not real_world:
        print("No qualifying markets found today. Try again later.")
        return

    seen = load_seen()
    new_drafts = []
    total = len(real_world)

    # Step 3: Scan each market
    for i, market in enumerate(real_world):
        question = market.get("question", "?")
        condition_id = market.get("conditionId", "")

        # Single updating progress line
        label = question[:55].ljust(55)
        print(f"\r[{i + 1:4d}/{total}] {label}  ({len(new_drafts)} found)", end="", flush=True)

        losing = get_losing_outcome(market)
        if not losing:
            continue

        trades = get_trades_for_market(condition_id)
        if not trades:
            continue

        heartbreaks = find_heartbreak_losses(trades, losing["token_id"])
        heartbreak_wallets = {hb["wallet"] for hb in heartbreaks}
        big_losses = find_big_losses(trades, losing["token_id"], exclude_wallets=heartbreak_wallets)

        for hb in heartbreaks + big_losses:
            loss_id = f"{condition_id}:{hb['wallet']}"
            if loss_id in seen:
                continue

            scenario = hb["scenario"]
            tag = "★ HEARTBREAK" if scenario == "heartbreak" else "◆ BIG LOSS"
            odds_str = f" at {hb['max_odds']}% odds" if scenario == "heartbreak" else ""

            # Break out of progress line before printing the finding
            print(f"\n  {tag}: {hb['name']} lost ${hb['net_loss']:,.0f}{odds_str}")

            draft = generate_draft_post(hb, market, losing["outcome"])
            new_drafts.append({
                "draft": draft,
                "loss_id": loss_id,
                "market": question,
                "user": hb["name"],
                "loss": hb["net_loss"],
                "odds": hb["max_odds"],
                "scenario": scenario,
            })
            seen.add(loss_id)

    print()  # end progress line

    # Step 4: Save drafts
    if new_drafts:
        with open(DRAFTS_FILE, "a") as f:
            f.write(f"\n{'=' * 60}\n")
            f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"{'=' * 60}\n\n")

            for d in new_drafts:
                label = "HEARTBREAK" if d["scenario"] == "heartbreak" else "BIG LOSS"
                f.write(f"--- [{label}] Market: {d['market']}\n")
                f.write(f"--- User: {d['user']} | Loss: ${d['loss']:,.0f} | Odds: {d['odds']}%\n\n")
                f.write(d["draft"])
                f.write(f"\n\n{'─' * 40}\n\n")

        save_seen(seen)

        heartbreak_count = sum(1 for d in new_drafts if d["scenario"] == "heartbreak")
        big_loss_count = sum(1 for d in new_drafts if d["scenario"] == "big_loss")

        # Auto-post top qualifying losses if credentials are present
        has_x = all(os.environ.get(k) for k in ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"])
        has_threads = all(os.environ.get(k) for k in ["THREADS_USER_ID", "THREADS_ACCESS_TOKEN"])
        posted_count = 0

        if (has_x or has_threads) and new_drafts:
            eligible = [d for d in new_drafts if d["loss"] >= AUTO_POST_MIN_LOSS]
            eligible.sort(key=lambda d: d["loss"], reverse=True)
            to_post = eligible[:MAX_POSTS_PER_RUN]

            if to_post:
                print(f"\nAuto-posting {len(to_post)} loss(es) >= ${AUTO_POST_MIN_LOSS:,}...")
                for d in to_post:
                    x_ok = post_to_x(d["draft"]) if has_x else False
                    threads_ok = post_to_threads(d["draft"]) if has_threads else False
                    platforms = [p for p, ok in [("X", x_ok), ("Threads", threads_ok)] if ok]
                    status = f"✓ {', '.join(platforms)}" if platforms else "✗ all failed"
                    print(f"  {status}: {d['user']} ${d['loss']:,.0f}")
                    if platforms:
                        posted_count += 1

        print(f"\n{'=' * 60}")
        print(f"  Heartbreaks : {heartbreak_count}")
        print(f"  Big losses  : {big_loss_count}")
        print(f"  Total drafts: {len(new_drafts)}")
        if has_x or has_threads:
            print(f"  Auto-posted : {posted_count}")
        print(f"  Saved to    : {DRAFTS_FILE}")
        print("=" * 60)
    else:
        print(f"\n{'=' * 60}")
        print("  No new losses found today.")
        print("=" * 60)


if __name__ == "__main__":
    main()
