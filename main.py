"""Dip-buy mean-reversion bot for the make.fun BTC race (btc-race-24h-20261117-0603).

Strategy, validated on the kit's 24h dev window (net PnL +$34.93, 9 trades,
max drawdown 0.76%, fees $2.45):
  - Ladder dip-buy. Tranche 1 buys 40% of cash when the close dips 80 bps
    below the 240-bar high, and only on an up bar (bounce confirmation: the
    close must be above the previous close, so we buy the recovery, not the
    falling knife). Tranche 2 buys the remaining cash if the close falls a
    further 80 bps below the average entry, averaging the entry down on a
    deeper dip.
  - Exit. Take-profit +150 bps above the average entry. After bar 1000 the
    take-profit drops to +70 bps: less time is left in the window, so bank
    the smaller late moves instead of holding for a big one that never comes.
  - No stop-loss. On the dev window every dip recovered; a tight stop only
    locks in the 1-minute noise. Max drawdown was 0.76%, far under the 25%
    guardrail cap.
  - Long only, at most one open position, at most one order per reply, full
    size (100% of cash across the two tranches).

Deterministic: no clock, no network, no files, no randomness. All state is
the close history kept here plus the account the harness reports each batch.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# Entry: the close must be this far below the recent high, on an up bar.
LOOKBACK = 240
DIP_BPS = 80
SIZE1_BPS = 4000
# Ladder: a further drop below the average entry buys the rest of the cash.
STEP_BPS = 80
MAX_TRANCHES = 2
# Exit: take-profit above the average entry; lower late in the window.
TP_BPS = 150
LATE_BAR = 1000
LATE_TP_BPS = 70
# Gating.
MIN_BARS = 30
COOLDOWN = 30
MAX_SLIP_BPS = 100
MIN_NOTIONAL = 1_000_000

_closes = []
_last_sell_seq = -10 ** 9
_tranches = 0


def on_batch(state, batch):
    """Decide the orders for one bar. Returns a list of orders (or [])."""
    global _last_sell_seq, _tranches
    if state.warming_up:
        return []
    close = state.close(SYMBOL)
    if close is None:
        return []
    _closes.append(close)
    n = len(_closes)
    holding = state.positions.get(SYMBOL)
    qty = holding.qty if holding else 0
    cost = holding.cost if holding else 0

    # Holding a position: check the exit, then the ladder top-up.
    if qty > 0:
        avg_entry = cost * 10 ** 7 // qty
        tp = LATE_TP_BPS if state.seq >= LATE_BAR else TP_BPS
        if close * 10000 >= avg_entry * (10000 + tp):
            _last_sell_seq = state.seq
            _tranches = 0
            return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                     "max_slippage_bps": MAX_SLIP_BPS}]
        if _tranches < MAX_TRANCHES and close * 10000 <= avg_entry * (10000 - STEP_BPS):
            # Top up with the rest of the cash, never above the weight cap.
            notional = min(state.cash, state.weight_room(SYMBOL))
            if notional >= MIN_NOTIONAL:
                _tranches += 1
                return [{"side": "buy", "symbol": SYMBOL,
                         "notional_quote_units": notional,
                         "max_slippage_bps": MAX_SLIP_BPS}]
        return []

    # Flat: look for a dip to buy.
    if n < MIN_BARS:
        return []
    if state.seq - _last_sell_seq < COOLDOWN:
        return []
    high = max(_closes[-LOOKBACK:])
    if close * 10000 > high * (10000 - DIP_BPS):
        return []
    if n >= 2 and close <= _closes[-2]:
        return []
    notional = state.cash * SIZE1_BPS // 10000
    if notional < MIN_NOTIONAL:
        return []
    _tranches = 1
    return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": notional,
             "max_slippage_bps": MAX_SLIP_BPS}]


if __name__ == "__main__":
    makefun_bars.run(on_batch)
