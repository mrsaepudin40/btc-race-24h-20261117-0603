"""Dip-buy mean-reversion bot for the make.fun BTC race (btc-race-24h-20261117-0603).

Round-2 tuned variant (first-trade bounce + tuned ladder).
Dev PnL +$44.38 on the kit's 24h dev window (verified end-to-end via the
local replay harness, 3 identical runs), 11 trades, max drawdown 0.66%,
fees $2.46. Vs the repo bot (+$34.93) and the round-1 tuned copy (+$38.36).

Two phases (both general rules -- no bar indices, prices or dates):
  1. FIRST TRADE -- bounce off the running low. Before any trade has
     happened, buy the full cash when the close recovers 15 bps above the
     lowest close seen so far (min 5 bars of history), and sell on a
     take-profit of +70 bps. On the dev window this catches the opening
     rally (buy ~bar 17, sell ~bar 99) that every dip-from-high rule misses,
     because the window opens near its low and there is no prior high to
     dip from. If no bounce comes within the first 240 bars, fall back to
     the ladder (never verified to trigger on the dev window -- it does not
     fire there, so the dev result is unaffected).
  2. LADDER -- the round-1 tuned config. Tranche 1 buys 38% of cash when
     the close dips 55 bps below the 240-bar high, after waiting 60 bars
     from the dip trigger (delayed entry: declines in the dev window take
     ~55-65 bars to bottom, so the delay lands tranche 1 near the low).
     Tranche 2 buys the rest if the close falls a further 40 bps below the
     average entry. Exit on take-profit +180 bps above the average entry;
     after bar 1000 the take-profit drops to +70 bps.
  - No stop-loss. Long only, at most one open position, at most one order
    per reply, full size (100% of cash across the two tranches).

Honest limits (see scratch/TUNING2.md): $44.38 is still $4.62 below the
$49 target. The remaining gap is the leg 2/3 split (sell near the bar-314
top, rebuy near the bar-386 low), which no causal exit rule captures:
reversal exits either fire on mid-rally pullbacks (which last 29-53 bars
here, as long as the real decline) or never fire. The first-leg gain sits
on a narrow plateau (rec 10-15 bps, TP 70 bps; rec 20+ or TP 100 collapses
it), so it is partly window-specific -- do not expect the same edge on the
unseen race window.

Deterministic: no clock, no network, no files, no randomness. All state is
the close history kept here plus the account the harness reports each batch.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# Phase 1 (first trade only): bounce off the running low.
FIRST_MIN_BARS = 5
FIRST_REC_BPS = 15
FIRST_TP_BPS = 70
FIRST_FALLBACK_BAR = 240
# Phase 2 (ladder): the round-1 tuned config.
LOOKBACK = 240
DIP_BPS = 55
SIZE1_BPS = 3800
ENTRY_DELAY_BARS = 60
STEP_BPS = 40
MAX_TRANCHES = 2
TP_BPS = 180
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
_armed = False
_arm_low = 0
_arm_high = 0
_arm_bar = -1
_first_done = False
_first_run_low = None
_first_peak = 0


def on_batch(state, batch):
    """Decide the orders for one bar. Returns a list of orders (or [])."""
    global _last_sell_seq, _tranches, _armed, _arm_low, _arm_high, _arm_bar
    global _first_done, _first_run_low, _first_peak
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

    # ---- Phase 1: first trade (bounce off the running low) ----
    if not _first_done:
        if _first_run_low is None or close < _first_run_low:
            _first_run_low = close
        if state.seq >= FIRST_FALLBACK_BAR:
            # No bounce came in time; hand over to the ladder. (Does not
            # fire on the dev window, so the verified dev result is exact.)
            _first_done = True
        elif qty > 0:
            avg_entry = cost * 10 ** 7 // qty
            if close > _first_peak:
                _first_peak = close
            if close * 10000 >= avg_entry * (10000 + FIRST_TP_BPS):
                _first_done = True
                return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                         "max_slippage_bps": MAX_SLIP_BPS}]
            return []
        else:
            if n >= FIRST_MIN_BARS and \
                    close * 10000 >= _first_run_low * (10000 + FIRST_REC_BPS):
                notional = min(state.cash, state.weight_room(SYMBOL))
                if notional >= MIN_NOTIONAL:
                    _first_peak = close
                    return [{"side": "buy", "symbol": SYMBOL,
                             "notional_quote_units": notional,
                             "max_slippage_bps": MAX_SLIP_BPS}]
            return []

    # ---- Phase 2: tuned ladder ----
    if qty > 0:
        avg_entry = cost * 10 ** 7 // qty
        tp = LATE_TP_BPS if state.seq >= LATE_BAR else TP_BPS
        if close * 10000 >= avg_entry * (10000 + tp):
            _last_sell_seq = state.seq
            _tranches = 0
            _armed = False
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
    # Delayed entry: arm on the dip trigger, wait ENTRY_DELAY_BARS, then buy
    # only if the dip condition still holds.
    if not _armed:
        _armed = True
        _arm_low = close
        _arm_high = high
        _arm_bar = state.seq
        return []
    if close * 10000 > _arm_high * (10000 - DIP_BPS):
        _armed = False
        return []
    if state.seq - _arm_bar < ENTRY_DELAY_BARS:
        return []
    notional = state.cash * SIZE1_BPS // 10000
    if notional < MIN_NOTIONAL:
        return []
    _tranches = 1
    _armed = False
    return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": notional,
             "max_slippage_bps": MAX_SLIP_BPS}]


if __name__ == "__main__":
    makefun_bars.run(on_batch)
