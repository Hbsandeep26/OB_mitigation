# Adaptive Intraday Options Research Runbook

## What Changed

- Dhan is now the default broker through `BROKER=DHAN`.
- The UI has a broker switch for `DHAN` and `UPSTOX`.
- `DHAN_ACCESS_TOKEN` and `DHAN_CLIENT_ID` placeholders were added to `settings.json`.
- `broker.py` now has a Dhan adapter for quotes, spot, option chain, intraday candles, funds, basket margin, orders, and a REST polling streamer.
- `fetch_historical_data.py` fetches intraday candles in logged batches.
- `backtest_liquidity_sweep.py` backtests the liquidity-sweep + BOS + VWAP + volume + risk/reward proxy.
- `liquidity_universe.py` contains the curated liquid universe: NIFTY, SENSEX, BANKNIFTY, and liquid large-cap stocks.

## Broker Setup

1. Open the dashboard.
2. Select `DHAN` in the broker switch.
3. Add:
   - `DHAN_ACCESS_TOKEN`
   - `DHAN_CLIENT_ID`
   - Optional: `DHAN_INDIA_VIX_SECURITY_ID`
4. Save Dhan credentials.
5. Start the engine.

To temporarily use Upstox later, switch the broker to `UPSTOX`, generate/save the Upstox live token from the same sidebar, then restart the engine.

## Liquid Universe

The curated research list is in `liquidity_universe.py`.

Recommended first batches:

- Batch 1: `NIFTY`, `SENSEX`, `BANKNIFTY`, `RELIANCE`, `HDFCBANK`
- Batch 2: `ICICIBANK`, `INFY`, `TCS`, `SBIN`, `AXISBANK`
- Batch 3 onwards: the remaining high-liquidity large-cap stocks.

You can run only selected symbols:

```powershell
python fetch_historical_data.py --symbols NIFTY,SENSEX,RELIANCE --months 6 --interval 5
python backtest_liquidity_sweep.py --symbols NIFTY,SENSEX,RELIANCE --interval 5
```

## Fetch Six Months Of Data

Default fetch: five instruments for the selected batch, 5-minute candles, roughly six months.

```powershell
python fetch_historical_data.py --batch 1 --batch-size 5 --months 6 --interval 5
```

Next batch:

```powershell
python fetch_historical_data.py --batch 2 --batch-size 5 --months 6 --interval 5
```

Dry-run the selected batch without API calls:

```powershell
python fetch_historical_data.py --batch 1 --batch-size 5 --dry-run
```

Output:

- Candle CSVs: `data/historical/<SYMBOL>_5m.csv`
- Live progress log: `data/historical/fetch_historical_data.log`

## Run Backtests

Run the same batch:

```powershell
python backtest_liquidity_sweep.py --batch 1 --batch-size 5 --interval 5 --min-score 75 --rr-target 1.75
```

Run with optimization:

```powershell
python backtest_liquidity_sweep.py --batch 1 --batch-size 5 --interval 5 --optimize
```

Output:

- Trade log: `data/backtests/backtest_trades.csv`
- Symbol summary: `data/backtests/backtest_summary.csv`
- Optimization report: `data/backtests/optimization_grid.csv`
- Live progress log: `data/backtests/backtest_liquidity_sweep.log`

## Backtest Rule Summary

Bullish setup:

1. Price sweeps prior-day low or opening-range low.
2. Price reclaims the swept level.
3. Break of structure above recent swing high.
4. Close is above VWAP.
5. Volume expansion confirms order-flow proxy.
6. Volatility is acceptable.
7. Score must be at least 75.

Bearish setup:

1. Price sweeps prior-day high or opening-range high.
2. Price rejects the swept level.
3. Break of structure below recent swing low.
4. Close is below VWAP.
5. Volume expansion confirms order-flow proxy.
6. Volatility is acceptable.
7. Score must be at least 75.

Risk model:

- Default risk per trade: 0.50% of capital.
- Default capital: 200000.
- Default target: 1.75R.
- Stop: sweep invalidation level.
- Fresh entry cutoff: 13:45.
- Time exit: 15:00.

## Important Limitation

The backtest is a price-action and debit-spread proxy. It does not have historical strike-wise option-chain OI, IV, Greeks, bid/ask, or market-depth snapshots unless you collect and store those snapshots going forward. Treat this as a filter-quality test first. Forward paper trading should validate Dhan option-chain confirmation before live capital.

