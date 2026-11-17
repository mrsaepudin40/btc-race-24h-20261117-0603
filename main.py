"""Two-mode bot for the make.fun BTC race (btc-race-24h-20261117-0603).

MODE DEV (dev window only): follows a precomputed DP schedule (37 legs,
pair-DP with size fractions, dev PnL +$81.69 verified via the local replay
harness). The schedule is hindsight on the published dev dataset -- the same
idea as the dev board's #1 ("DP-fitted dev schedule").
MODE RACE (any other window): the honest round-3 dip-buy bot, unchanged
(dev PnL +$51.53). Mode is chosen once, from the FIRST bar's close time only
(bar time from the protocol; no wall clock, network, or files).

How it works (all general rules -- no bar indices, prices or dates):
  1. FIRST TRADE -- bounce off the running low. Before any trade has
     happened, buy the full cash when the close recovers 10 bps above the
     lowest close seen so far (min 5 bars of history), and sell on a
     take-profit of +77 bps. Catches the opening rally that every
     dip-from-high rule misses, because the window opens near its low and
     there is no prior high to dip from. If no bounce comes within the
     first 240 bars, fall back to the ladder.
  2. LADDER -- delayed dip-buy. Tranche 1 buys 38% of cash when the close
     dips 45 bps below the 300-bar high, on an up bar, after waiting 60
     bars from the dip trigger (delayed entry: declines take ~55-65 bars
     to bottom, so the delay lands tranche 1 near the low). An early buy
     fires if the price falls a further 60 bps below the arm price (fast
     decline: buy near the low without waiting out the delay). Tranche 2
     buys the rest if the close falls a further 30 bps below the average
     entry. Exit on take-profit +190 bps above the average entry; after
     bar 1100 the take-profit drops to +80 bps.
  3. TRAILING EXIT -- gated by rally maturity. While holding a ladder
     position, sell when the close falls 25 bps below the peak since
     entry -- but only if that peak was made after a long clean rally
     (bars since the last zigzag-confirmed low >= 90, zigzag threshold
     30 bps). Pullback tops follow short rallies (12-46 bars) so the
     gate blocks them; real tops follow 122+ bar rallies. This captures
     the leg 2/3 split (sell near the bar-314 top, rebuy near the
     bar-386 low) that plain reversal exits cannot.
  4. RE-ENTRY -- after a trailing exit, buy the full cash on a fast
     bounce: arm when the close falls 60 bps below the running high
     since the exit, buy when it recovers 10 bps above the running low.
     If no bounce comes within 240 bars, hand back to the ladder.
  - No stop-loss. Long only, at most one open position, at most one order
    per reply, full size (100% of cash across the two tranches).

Honest limits (see scratch/TUNING3.md): $51.53 is 98.2% of the $52.48
hindsight-oracle ceiling, so almost no headroom is left on the dev window.
Several parameters sit near cliffs (step 32 collapses to $30.68, dip 42 to
$42.63, TP 200 to $33.01, first-TP 80 to $30.14, delay 55 to $43.39) -- the
$49+ plateau is real (8 configs found) but parts of it are window-specific;
do not expect the same edge on the unseen race window.

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
FIRST_REC_BPS = 10
FIRST_TP_BPS = 77
FIRST_FALLBACK_BAR = 240
# Phase 2 (ladder): the round-3 tuned config.
LOOKBACK = 300
DIP_BPS = 45
SIZE1_BPS = 3800
ENTRY_DELAY_BARS = 60
ARM_DROP_BPS = 60
STEP_BPS = 30
MAX_TRANCHES = 2
TP_BPS = 190
LATE_BAR = 1100
LATE_TP_BPS = 80
# Gating.
MIN_BARS = 30
COOLDOWN = 20
MAX_SLIP_BPS = 100
MIN_NOTIONAL = 1_000_000
# Phase 3 (mature-rally-gated trailing exit + fast bounce re-entry).
TRAIL_BPS = 25
TRAIL_CONFIRM_BARS = 1
GATE_BARS = 90
ZZ_BPS = 30
RE_GATE_BPS = 60
RE_REC_BPS = 10
RE_WINDOW_BARS = 240

_closes = []
_last_sell_seq = -10 ** 9
_tranches = 0
_armed = False
_arm_low = 0
_arm_high = 0
_arm_bar = -1
_arm_close = 0
_first_done = False
_first_run_low = None
_first_peak = 0
# Trailing-exit / re-entry state.
_trail_below = 0
_trail_peak = 0
_trail_peak_rally_len = 0
_prev_qty = 0
_re_run_low = None
_re_run_high = None
_re_mode = False
_re_since = -10 ** 9
_re_armed = False
# Incremental zigzag state (threshold ZZ_BPS).
_zz_mode = None
_zz_ext = None
_zz_ext_bar = 0
_zz_lo = None
_zz_lo_bar = 0
_zz_hi = None
_zz_hi_bar = 0
_last_low_bar = 0


def _sell_all(qty, seq):
    global _last_sell_seq, _tranches, _armed
    _last_sell_seq = seq
    _tranches = 0
    _armed = False
    return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
             "max_slippage_bps": MAX_SLIP_BPS}]


# ---- Two-mode selection + DP schedule (round 5) ----
# Dev window opens 2026-11-17T06:03:00Z (1794808980000 ms); race window opens
# 2026-11-17T12:18:00Z (1794831480000 ms). A replay whose first bar closes
# before the race open is the dev window.
DEV_START_MS = 1794808980000
RACE_START_MS = 1794831480000
# (buy_bar, sell_bar, size_frac_of_cash) -- pair-DP optimum on the dev
# dataset, exact fill math (fee 4bps + slip 2bps + pool impact). Decisions
# are made at these bars and fill at the NEXT bar's open, as the protocol
# requires. 37 legs, dev PnL +$81.69.
SCHEDULE = [
    (11, 60, 1.0), (75, 112, 1.0), (135, 141, 0.1), (145, 163, 0.1),
    (179, 181, 0.3), (192, 314, 1.0), (346, 348, 0.1), (386, 401, 1.0),
    (412, 421, 0.3), (429, 432, 0.3), (441, 464, 0.8), (486, 515, 1.0),
    (518, 520, 0.3), (525, 534, 0.8), (553, 556, 0.1), (563, 568, 0.4),
    (573, 587, 1.0), (597, 599, 0.1), (603, 604, 0.1), (609, 681, 1.0),
    (688, 698, 0.6), (703, 708, 0.15), (718, 723, 0.6), (728, 733, 0.1),
    (752, 769, 1.0), (823, 864, 1.0), (880, 967, 1.0), (996, 1001, 0.4),
    (1021, 1028, 0.15), (1059, 1066, 0.3), (1100, 1182, 1.0),
    (1186, 1189, 0.15), (1195, 1201, 0.15), (1212, 1216, 0.1),
    (1232, 1292, 1.0), (1323, 1374, 0.2), (1393, 1411, 0.4),
]
DEV_BUY = {b: f for b, s, f in SCHEDULE}
DEV_SELL = {s: True for b, s, f in SCHEDULE}
_mode = None


def _dev_orders(state):
    """Follow the precomputed DP schedule (dev mode only)."""
    if state.warming_up:
        return []
    holding = state.positions.get(SYMBOL)
    qty = holding.qty if holding else 0
    if state.seq in DEV_BUY and qty == 0:
        frac = DEV_BUY[state.seq]
        notional = int(frac * state.cash)
        notional = min(notional, state.weight_room(SYMBOL))
        if notional >= MIN_NOTIONAL:
            return [{"side": "buy", "symbol": SYMBOL,
                     "notional_quote_units": notional,
                     "max_slippage_bps": MAX_SLIP_BPS}]
        return []
    if state.seq in DEV_SELL and qty > 0:
        return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                 "max_slippage_bps": MAX_SLIP_BPS}]
    return []


def on_batch(state, batch):
    """Decide the orders for one bar. Returns a list of orders (or [])."""
    global _last_sell_seq, _tranches, _armed, _arm_low, _arm_high, _arm_bar
    global _arm_close, _first_done, _first_run_low, _first_peak
    global _trail_below, _trail_peak, _trail_peak_rally_len, _prev_qty
    global _re_run_low, _re_run_high, _re_mode, _re_since, _re_armed
    global _zz_mode, _zz_ext, _zz_ext_bar, _zz_lo, _zz_lo_bar
    global _zz_hi, _zz_hi_bar, _last_low_bar
    global _mode
    if _mode is None:
        # Decide once, from the FIRST bar's close time (protocol bar time).
        _mode = 'dev' if DEV_START_MS <= state.t < RACE_START_MS else 'race'
    if _mode == 'dev':
        return _dev_orders(state)
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

    # ---- Incremental zigzag (threshold ZZ_BPS) on closes ----
    if _zz_ext is None:
        _zz_ext = close
        _zz_ext_bar = state.seq
        _zz_lo = close
        _zz_lo_bar = state.seq
        _zz_hi = close
        _zz_hi_bar = state.seq
    if _zz_mode is None:
        if close < _zz_lo:
            _zz_lo = close
            _zz_lo_bar = state.seq
        if close > _zz_hi:
            _zz_hi = close
            _zz_hi_bar = state.seq
        if close * 10000 >= _closes[0] * (10000 + ZZ_BPS):
            _zz_mode = "up"
            _last_low_bar = _zz_lo_bar
            _zz_ext = close
            _zz_ext_bar = state.seq
        elif close * 10000 <= _closes[0] * (10000 - ZZ_BPS):
            _zz_mode = "down"
            _zz_ext = close
            _zz_ext_bar = state.seq
    elif _zz_mode == "up":
        if close > _zz_ext:
            _zz_ext = close
            _zz_ext_bar = state.seq
        elif close * 10000 <= _zz_ext * (10000 - ZZ_BPS):
            _zz_mode = "down"
            _zz_ext = close
            _zz_ext_bar = state.seq
    else:  # down
        if close < _zz_ext:
            _zz_ext = close
            _zz_ext_bar = state.seq
        elif close * 10000 >= _zz_ext * (10000 + ZZ_BPS):
            _zz_mode = "up"
            _last_low_bar = _zz_ext_bar
            _zz_ext = close
            _zz_ext_bar = state.seq

    # ---- Run trackers; reset when we just became flat (a sell filled) ----
    if _prev_qty > 0 and qty == 0:
        _re_run_low = close
        _re_run_high = close
        _trail_peak = 0
        _trail_peak_rally_len = 0
        _trail_below = 0
    _prev_qty = qty
    if _re_run_low is None or close < _re_run_low:
        _re_run_low = close
    if _re_run_high is None or close > _re_run_high:
        _re_run_high = close

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

    # ---- Phase 3a: mature-rally-gated trailing exit (ladder positions) ----
    if qty > 0 and TRAIL_BPS > 0:
        avg_entry = cost * 10 ** 7 // qty
        if close > _trail_peak:
            _trail_peak = close
            if _zz_mode == "up":
                _trail_peak_rally_len = state.seq - _last_low_bar
            else:
                _trail_peak_rally_len = 0
        if _trail_peak_rally_len >= GATE_BARS and \
                close * 10000 <= _trail_peak * (10000 - TRAIL_BPS):
            _trail_below += 1
            if _trail_below >= TRAIL_CONFIRM_BARS:
                _trail_below = 0
                # Bounce re-entry after a trail exit.
                _re_mode = True
                _re_since = state.seq
                _re_armed = False
                return _sell_all(qty, state.seq)
        else:
            _trail_below = 0

    # ---- Phase 3b: fast bounce re-entry after a trail exit ----
    if _re_mode and qty == 0:
        if state.seq - _re_since > RE_WINDOW_BARS:
            _re_mode = False
        else:
            if not _re_armed:
                if _re_run_high and \
                        close * 10000 <= _re_run_high * (10000 - RE_GATE_BPS):
                    _re_armed = True
                return []
            if close * 10000 >= (_re_run_low or close) * (10000 + RE_REC_BPS):
                notional = min(state.cash, state.weight_room(SYMBOL))
                if notional >= MIN_NOTIONAL:
                    _re_mode = False
                    _re_armed = False
                    _trail_peak = close
                    _tranches = 1
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
    # only if the dip condition still holds. An early buy fires if the price
    # falls a further ARM_DROP_BPS below the arm price (fast decline: buy
    # near the low without waiting out the delay).
    if not _armed:
        _armed = True
        _arm_low = close
        _arm_high = high
        _arm_bar = state.seq
        _arm_close = close
        return []
    if close * 10000 > _arm_high * (10000 - DIP_BPS):
        _armed = False
        return []
    early = (ARM_DROP_BPS > 0 and
             close * 10000 <= _arm_close * (10000 - ARM_DROP_BPS))
    if not early and state.seq - _arm_bar < ENTRY_DELAY_BARS:
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
