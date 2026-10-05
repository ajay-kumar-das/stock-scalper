# 06 — Stock-Selection Framework

Three tiers, each narrower and more frequently refreshed.

```
Eligible universe (~120–180 stocks, built pre-market, point-in-time)
   └─ Scan set (all eligible, `ltpc`/1-min data, scored every 60 s)
        └─ Watchlist (top 25, `full` mode depth, strategies evaluate here)
             └─ Active candidates (setups armed; capital allocated by rank)
```

## 1. Eligible universe — hard filters (pre-market)

Computed from prior-session data only (no look-ahead).

| Filter | Default | Why |
|--------|---------|-----|
| Segment | NSE EQ series, F&O-eligible stock | Dynamic price bands instead of fixed 2/5/10% circuits; deepest books |
| Median daily traded value (20d) | ≥ ₹150 crore | Our order must be a tiny fraction of flow |
| Price | ₹100 – ₹10,000 | Tick size (₹0.05 for most of this range at time of writing; tick table loaded from instrument master) should be ≤ ~5 bps of price; avoid low-price noise |
| Median 1-min spread (20d, from recorded data when available; else from bar-proxy) | ≤ 5 bps | Spread is a direct cost |
| Average true range % (14d daily) | 1.2% – 6% | Too quiet → moves don't cover costs; too wild → noise and gap risk |
| Exclusions | ASM/GSM lists, CAS-eligible, trade-for-trade (BE/BZ) series, stocks with split/bonus ex-date today, stocks under ban in F&O (MWPL > 95%) | Execution and regulatory risk |
| Operator blacklist | file `config/blacklist.yaml` | Manual control |

Survivorship: the universe for historical date *d* is computed from bhavcopy and F&O list as of *d* − 1. Delisted names remain in history.

## 2. Opportunity score (intraday, every 60 s)

Each component is a percentile rank within the eligible universe at that moment (robust to scale), then combined. Weights are fixed from rationale initially and **only changed via research** (does higher score → higher subsequent net strategy expectancy? tested in R1 as a ranking validation: top-quintile vs bottom-quintile forward returns/trade outcomes).

| Component | Measure | Weight | Hypothesis |
|-----------|---------|--------|-----------|
| Relative volume (RVOL) | cumulative volume today / average cumulative volume at the same minute over 20 sessions | 0.25 | Abnormal participation = information/flows present ("in play") |
| Relative strength | stock return since open − β × NIFTY return; also vs sector index | 0.20 | Leaders keep leading intraday when institutions are accumulating |
| Directional efficiency | |net move| / sum of |1-min moves| over last 30 min (Kaufman efficiency ratio) | 0.20 | Clean, trending price paths have less noise relative to move → stops less likely hit by noise |
| Tradable volatility | 15-min realised range / round-trip cost (bps) | 0.15 | Move must be large relative to cost |
| Liquidity quality | inverse of current spread (bps) × top-5 depth value | 0.10 | Execution reliability |
| Book pressure | (total buy qty − total sell qty)/(sum), smoothed | 0.05 | Weak evidence; small weight until validated with recorded data |
| Proximity to structure | distance to day high / OR high / VWAP in ATR units (near is better for breakout setups) | 0.05 | Setup likely to arm soon |

**Penalties / vetoes (score → 0):**
- Current spread > 2× its 20-min median, or > 8 bps.
- Stale data (> 5 s) or quarantined instrument.
- Within 3% of upper price band (for non-dynamic-band names, if any are admitted).
- Move from open > 3 × daily ATR (exhausted / news-driven halts risk).
- Directional efficiency < 0.15 (random walk-like noise).
- Already traded 2× today, or in cooldown.

## 3. Watchlist management

- Top 25 by score enter full-depth mode; hysteresis: a stock leaves only when its rank drops below 40 for ≥ 3 consecutive refreshes (avoids churn of subscriptions and features).
- Stocks with open positions are always in the watchlist regardless of score.
- Feature warm-up: a stock newly added to the watchlist is not tradable until its tick-derived features have ≥ 2 minutes of data (bar-derived features are pre-warmed from 1-min history).

## 4. Capital allocation across candidates

When several setups trigger at once, candidates are ordered by **expected net edge per unit risk** (expected R after costs × confidence), not by raw score. Ties broken by liquidity quality. Exposure/sector/correlation limits (doc 04) are applied in that order, so the best opportunity gets capital first.

## 5. Avoid-list rationale (from the brief)

| Brief: avoid when… | Implemented by |
|-------------------|----------------|
| liquidity is poor | ADV, depth, spread filters; liquidity-capped sizing |
| spreads are excessive | spread filter + dynamic spread veto |
| execution is unreliable | per-instrument reject/slippage stats; instruments with ≥ 2 rejects/day are blacklisted for the day |
| movement is random/noisy | efficiency ratio veto; noise-to-ATR stop check |
| circuit limits | F&O universe (dynamic bands) + band-proximity veto |
| costs consume expected profit | tradable-volatility component + edge-vs-cost gate + min notional |
| participation insufficient | RVOL component and min RVOL per strategy |

## 6. Validation of the selection layer

The selection layer is itself a hypothesis. Research test (R1-style): over development data, do top-quintile names by score at time *t* produce larger absolute forward moves (normalised by cost) and better strategy outcomes than bottom-quintile names? If not, weights are revisited in research — never live.
