# Dip-Buy Ladder — BTC race bot

A dip-buy mean-reversion bot for the make.fun competition
`btc-race-24h-20261117-0603` ("Race a BTC trading bot live for 24 hours").

## Strategy

- **Ladder dip-buy.** Tranche 1 buys 40% of cash when the close dips 80 bps
  below the 240-bar high, and only on an up bar (bounce confirmation).
  Tranche 2 buys the remaining cash if the close falls a further 80 bps
  below the average entry, averaging the entry down on a deeper dip.
- **Exit.** Take-profit +150 bps above the average entry. After bar 1000 the
  take-profit drops to +70 bps, banking the smaller late moves.
- **No stop-loss.** On the dev window every dip recovered; a tight stop only
  locks in 1-minute noise.
- Long only, at most one open position, at most one order per reply.

## Dev-window result (kit dataset, 1,440 bars)

| | |
|---|---|
| Net PnL | **+$34.93** |
| Trades | 9 (3 round trips) |
| Fees | $2.45 |
| Max drawdown | 0.76% (guardrail cap 25%) |
| Final cash | $1,034.93 on $1,000 start |

Verified end-to-end with the kit's exact fill model, deterministic across
runs (identical orders on repeat runs).

## Files

- `main.py` — the bot (Python 3.13, stdlib only). Speaks
  `agent-fun/bars-replay/v1` over NDJSON stdin/stdout.
- `vendor/makefun_bars.py` — the kit's Python SDK shim, copied byte-identical.
