# Stock Scalper — Session Handoff / Status

Last updated: 2026-10-05

## Where things stand
- **M0 Design: done.** docs/00–13 (PRD, FRs, lifecycle, risk model, research framework, selection, entry/exit, paper criteria, promotion ladder, failure scenarios, milestone plan, architecture, critical review with 34 findings).
- **M1 Foundation: done.** 69 tests passing. Review in docs/reviews/M1.md. An independent adversarial code review found 7 defects; all are fixed and have regression tests.
- **M2 Data & replay: code complete, gate pending real data.** Data layer, recorder, tick-level fill simulator, REPLAY runner, trading calendar and parallel walk-forward are built; a second adversarial review found 7 defects in the new code, all fixed with regression tests. 95 tests pass. The gate needs ≥ 5 recorded real sessions. See docs/reviews/M2-progress.md.

## How to restore the code in a new session
Project docs under `code/` mirror the repo layout: `code/scalper/...`, `code/tests/...`, `code/scripts/...`, `code/pyproject.toml`. Read each one with project_read and write it to the same relative path. Empty `__init__.py` files are not stored, so recreate one in every package directory (`scalper/` and each subfolder, plus `tests/`). Then run `pip install --break-system-packages pytest pyarrow` and `python3 -m pytest`.

## Key decisions (see docs/00-index.md)
1. Each trade must clear roughly 15–30 bps of all-in cost. Holding periods are minutes, not seconds.
2. One code path runs every mode: BACKTEST, REPLAY, PAPER and LIVE.
3. The broker is the source of truth; local state is only a cache.
4. Every position has a protective stop resting at the broker. Exits work by modifying that stop.
5. Research runs *measure* the edge and regime gates instead of enforcing them. Paper and live trading enforce them, using p from validated results.

## Environment notes
- The cloud sandbox **cannot reach upstox.com** (blocked by the egress proxy). Anything that needs Upstox (data download, feed recorder, auth) has to run on Ajay's machine or a VPS.
- No folder on Ajay's computer is linked to this project yet.

## Needed from Ajay before M2 can finish
- An Upstox API app: API key, secret and redirect URI. Also a daily login.
- A machine that can run the recorder during market hours (his PC, or a Mumbai VPS with a static IP, which will also be needed for live trading later).
- Optional: a Telegram bot token for alerts.

## Next steps
With credentials: run download_history.py (2022→), record ≥ 5 sessions with record_feed.py, check bar agreement, then M3 (market research: spreads, RVOL, volatility by time of day, selection-score validation) and M4 (strategy research with run_research.py). Without credentials, the next useful offline work is M5 (fault-injection suite F1–F32 in REPLAY) and the monitoring console. If M4 finds no strategy that clears the gates, the project stops at that point: no live trading.
