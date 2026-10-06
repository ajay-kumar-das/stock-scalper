# Stock Scalper

A research-first, safety-first intraday trading system for NSE cash equities via Upstox.

**Status:** M0 (design) complete. M1 (foundation) is built and tested on synthetic data only. **No real-money capability exists yet**: there is no live adapter in this build, and the mode guard refuses LIVE.

Start with [docs/00-index.md](docs/00-index.md).

## What's here (M1)

| Area | Module | Notes |
|------|--------|-------|
| Domain types, clock, tick math | `scalper/core` | Prices as integer paise; injectable clock |
| Upstox cost model | `scalper/costs/upstox.py` | Brokerage per executed order; all statutory charges; versioned |
| Order lifecycle + OMS | `scalper/oms` | State machine, idempotent tags, throttle, no-duplicate and no-accidental-short invariants, exit-by-modify |
| Conservative bar fill model | `scalper/execution/sim_bar.py` | Next-bar fills, half-spread, impact, volume caps, stop-first intrabar, gap-through stop-limits, random rejects |
| Features, regime, selection | `scalper/features`, `scalper/regime`, `scalper/selection` | Look-ahead-safe incremental features |
| Strategy interface + H1 ORB | `scalper/strategies` | Strategies propose only; never touch orders |
| Decision gates G1–G7 | `scalper/decision/pipeline.py` | Edge-vs-cost gate; research mode measures rather than enforces evidence-dependent gates |
| Risk engine & sizing | `scalper/risk/engine.py` | Layered limits, daily-loss projection, anti-revenge multipliers, drawdown stop |
| Position manager | `scalper/portfolio` | Broker-resident stop, tighten-only stops, thesis/time/trail exits, escalation |
| Session & mode guard | `scalper/session` | Session state machine, LIVE guard |
| Journal & explain | `scalper/journal` | SQLite WAL; `explain_trade`, `why_not` |
| Metrics | `scalper/analytics/metrics.py` | Every §23 metric, before/after costs, day-block bootstrap CI |
| Backtester, partitions | `scalper/backtest` | Deterministic event-driven runner; holdout lock; experiment registry |

## Run

Commands work the same in Windows `cmd`, PowerShell, macOS and Linux. Run them from the repo folder.
Use `python -m pip` / `python -m pytest` so they work even when pip's Scripts folder is not on PATH.

```
python -m pip install -e ".[dev]"
python -m pytest
python scripts/demo_synthetic.py
python scripts/demo_synthetic.py --null
```

- The first line installs numpy, pandas, pyarrow and pytest and makes `scalper` importable.
- `pytest` runs the full suite (about 100 s).
- `demo_synthetic.py` runs a synthetic market with a planted edge (checks the machinery);
  `--null` runs a no-edge market, which must not show a significant net edge.

## Operator workflow (M2, on your machine)

```
python -m pip install -e ".[dev,live]"
```

Set your Upstox app credentials (the redirect URI must match the one registered in your Upstox app):

| Shell | Command |
|---|---|
| Windows cmd | `set UPSTOX_API_KEY=...` (repeat for `UPSTOX_API_SECRET` and `UPSTOX_REDIRECT_URI=http://127.0.0.1:8765/callback`) |
| PowerShell | `$env:UPSTOX_API_KEY="..."` (same for the other two) |
| macOS / Linux | `export UPSTOX_API_KEY=... UPSTOX_API_SECRET=... UPSTOX_REDIRECT_URI=http://127.0.0.1:8765/callback` |

Then:

```
python scripts/upstox_login.py
python scripts/download_history.py --symbols-file config/universe_seed.txt --start 2022-01-01 --end 2026-09-30
python scripts/record_feed.py --symbols-file config/universe_seed.txt
python scripts/run_research.py --partition development --bhavcopy data/bhavcopy --master data/instruments/NSE-<date>.json
```

- `upstox_login.py`: once each trading morning (the token expires daily).
- `download_history.py`: one-off, 1-minute history for the seed universe into `data/`.
- `record_feed.py`: during market hours, market data only (it cannot place orders). Needs ≥ 5 clean sessions for the M2 gate.
- `config/universe_seed.txt` is a starting list of liquid F&O stocks; symbols missing from today's
  instrument master are printed and skipped.

Also in M2: tick-level fill simulator (`scalper/execution/sim_tick.py`), REPLAY runner (`scalper/backtest/replay.py`), trading calendar, and parallel walk-forward windows (`run_research.py --workers N`).
