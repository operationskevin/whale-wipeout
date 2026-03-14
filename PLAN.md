# WhaleWipeout — Build Plan

## Goal
An automated social media bot (@WhaleWipeout) that tracks massive betting losses on Polymarket and generates draft posts for manual review and posting on X.

## Core Hook: "Heartbreak" Scenarios
Target users who:
- Held a position with **>90% winning odds** at some point
- Ended up with that position going to **$0**
- Lost **≥ $10,000** in a single position

## Decisions Made

| Question | Answer |
|---|---|
| Polymarket data access | Public API endpoints only (CLOB + gamma) |
| Position tracking | Polymarket API (no on-chain/subgraph) |
| X/Twitter posting | Manual — bot generates drafts for human review |
| Whale threshold | $10,000 minimum loss |
| Posting frequency | Every qualifying loss |
| Tone | Deadpan, darkly humorous — like a sports commentator calling a catastrophic loss |
| Tech stack | Python |

## Architecture

### Flow
1. Fetch markets resolved **today (UTC)** from Polymarket public API
2. Filter to **real-world categories only** (politics, sports, culture, entertainment, current events, world news) — exclude crypto prices, financial markets, niche prediction markets
3. For each qualifying resolved market, scan user positions
4. Flag "Heartbreak" cases: user held >90% odds position → resolved against them, loss ≥ $10k
5. Generate draft post in deadpan/darkly humorous tone
6. Save drafts to a plain text file for manual review (deduplication to avoid repeat posts)

### Post Format (example)
```
94% odds. $47,200 on the line. One bad outcome later — gone.
[Market name] resolved NO.
Moment of silence. 🕯️
```

## Deployment
- Local CLI script — run manually on your computer
- Not a web app

## Output
- Drafts saved to a plain text file for manual review before posting
- Deduplication: same loss won't appear twice across runs

## Key Polymarket API Endpoints
- Gamma API: `https://gamma-api.polymarket.com` — market metadata, resolution
- CLOB API: `https://clob.polymarket.com` — order book, trade history, positions
