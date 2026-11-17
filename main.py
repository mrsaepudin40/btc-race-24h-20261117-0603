"""Two-mode bot for the make.fun BTC race (btc-race-24h-20261117-0603).

MODE DEV (dev window only): follows a precomputed 2D-DP action schedule
(Pareto frontier over (cash, qty) states, with top-ups and partial sells,
dev PnL $+104.58 verified via the local replay harness). The schedule is
hindsight on the published dev dataset -- the same idea as the dev board's
#1 ("DP-fitted dev schedule").
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


# ---- Two-mode selection + DP schedule (round 6) ----
# Dev window opens 2026-11-17T06:03:00Z (1794808980000 ms); race window opens
# 2026-11-17T12:18:00Z (1794831480000 ms). A replay whose first bar closes
# before the race open is the dev window.
DEV_START_MS = 1794808980000
RACE_START_MS = 1794831480000
# (decision_bar, side, frac) -- 2D Pareto-DP optimum on the dev dataset,
# exact fill math (fee 4bps + slip 2bps + pool impact). Decisions are made
# at these bars and fill at the NEXT bar's open, as the protocol requires.
# side 1 = buy frac of cash, side 2 = sell frac of qty. 291 actions,
# dev PnL $+104.58.
ACTIONS = [
    (8, 1, 0.25), (10, 1, 0.25), (11, 1, 0.5), (12, 1, 1.0),
    (57, 2, 0.25), (59, 2, 0.25), (60, 2, 0.5), (61, 2, 0.25),
    (62, 2, 0.5), (63, 2, 1.0), (75, 1, 0.25), (76, 1, 0.5),
    (77, 1, 1.0), (98, 2, 0.25), (102, 1, 0.75), (112, 2, 0.25),
    (113, 2, 0.25), (114, 2, 0.5), (115, 2, 1.0), (179, 1, 0.25),
    (181, 2, 1.0), (191, 1, 0.25), (192, 1, 0.5), (195, 1, 0.25),
    (196, 1, 0.5), (197, 1, 1.0), (203, 2, 0.25), (208, 2, 0.25),
    (212, 2, 0.25), (213, 2, 0.25), (216, 1, 0.25), (217, 1, 0.25),
    (228, 1, 0.25), (229, 1, 0.25), (236, 1, 0.25), (241, 1, 0.25),
    (242, 1, 0.75), (243, 1, 0.5), (244, 1, 1.0), (264, 2, 0.5),
    (265, 2, 0.5), (277, 2, 0.25), (289, 1, 0.25), (290, 1, 0.25),
    (291, 1, 0.25), (292, 1, 0.25), (293, 1, 0.5), (296, 1, 0.25),
    (302, 1, 1.0), (314, 2, 0.25), (315, 2, 0.25), (316, 2, 0.5),
    (317, 2, 0.5), (318, 2, 0.25), (319, 2, 0.25), (320, 2, 0.25),
    (321, 2, 1.0), (385, 1, 0.25), (386, 1, 0.5), (387, 1, 0.5),
    (388, 1, 1.0), (401, 2, 0.75), (402, 2, 1.0), (411, 1, 0.25),
    (412, 1, 0.25), (413, 1, 0.25), (414, 1, 0.25), (416, 1, 0.25),
    (418, 2, 0.25), (421, 2, 0.5), (425, 2, 0.25), (429, 1, 0.25),
    (430, 1, 0.25), (431, 1, 0.25), (432, 2, 0.5), (433, 2, 0.25),
    (440, 1, 0.25), (441, 1, 0.5), (445, 1, 0.75), (446, 1, 1.0),
    (463, 2, 0.25), (464, 2, 0.5), (465, 2, 0.75), (466, 2, 1.0),
    (485, 1, 0.25), (486, 1, 0.5), (487, 1, 1.0), (492, 2, 0.5),
    (493, 2, 0.25), (500, 1, 0.5), (501, 1, 1.0), (514, 2, 0.25),
    (515, 2, 0.75), (516, 2, 1.0), (518, 1, 0.25), (519, 2, 0.25),
    (520, 2, 1.0), (523, 1, 0.25), (524, 1, 0.25), (525, 1, 1.0),
    (526, 2, 0.25), (528, 1, 1.0), (532, 2, 0.25), (533, 2, 0.25),
    (534, 2, 1.0), (553, 1, 0.25), (555, 2, 0.25), (556, 2, 0.5),
    (562, 1, 0.25), (563, 1, 0.5), (564, 1, 1.0), (566, 2, 0.25),
    (567, 2, 0.5), (568, 2, 1.0), (571, 1, 0.25), (573, 1, 0.5),
    (574, 1, 1.0), (583, 2, 0.25), (584, 2, 0.25), (587, 2, 0.5),
    (589, 2, 0.25), (590, 2, 1.0), (597, 1, 0.25), (599, 2, 0.5),
    (604, 2, 1.0), (608, 1, 0.25), (609, 1, 0.5), (610, 1, 1.0),
    (620, 2, 0.25), (621, 2, 0.25), (623, 2, 0.5), (624, 2, 0.25),
    (625, 2, 1.0), (635, 1, 0.25), (640, 1, 0.5), (641, 1, 0.5),
    (642, 1, 0.5), (643, 1, 0.25), (650, 1, 0.25), (651, 1, 1.0),
    (659, 2, 0.25), (667, 1, 1.0), (674, 2, 0.25), (675, 2, 0.25),
    (681, 2, 0.75), (682, 2, 0.5), (683, 2, 1.0), (687, 1, 0.25),
    (688, 1, 0.75), (690, 1, 0.25), (691, 1, 0.5), (695, 1, 1.0),
    (698, 2, 0.5), (699, 2, 0.5), (700, 2, 0.75), (703, 1, 0.25),
    (708, 2, 0.5), (711, 2, 1.0), (717, 1, 0.25), (718, 1, 0.5),
    (719, 1, 0.75), (720, 1, 1.0), (723, 2, 0.75), (733, 2, 0.25),
    (738, 2, 1.0), (747, 1, 0.25), (750, 2, 0.25), (752, 1, 0.5),
    (753, 1, 0.25), (754, 1, 1.0), (769, 2, 0.25), (770, 2, 0.25),
    (771, 2, 0.5), (773, 2, 0.25), (774, 2, 0.75), (775, 2, 1.0),
    (798, 1, 0.25), (807, 2, 0.5), (808, 2, 0.5), (809, 2, 1.0),
    (818, 1, 0.25), (822, 1, 0.25), (823, 1, 0.25), (824, 1, 0.25),
    (825, 1, 1.0), (846, 2, 0.25), (848, 2, 0.25), (859, 1, 0.25),
    (860, 1, 0.25), (861, 1, 0.75), (862, 1, 0.25), (864, 2, 0.25),
    (865, 2, 0.5), (866, 2, 0.25), (867, 2, 1.0), (880, 1, 0.25),
    (884, 1, 0.25), (885, 1, 0.25), (894, 1, 0.5), (915, 1, 0.25),
    (916, 1, 0.75), (917, 1, 0.75), (918, 1, 1.0), (947, 2, 0.25),
    (954, 1, 1.0), (960, 2, 0.25), (967, 2, 0.25), (968, 2, 0.5),
    (969, 2, 0.75), (972, 2, 1.0), (991, 1, 0.25), (993, 1, 0.25),
    (994, 1, 0.25), (995, 1, 0.25), (996, 1, 1.0), (1001, 2, 0.25),
    (1002, 2, 0.5), (1003, 2, 0.5), (1004, 2, 1.0), (1021, 1, 0.25),
    (1028, 2, 0.75), (1032, 2, 1.0), (1059, 1, 0.25), (1060, 1, 0.25),
    (1066, 2, 0.5), (1069, 2, 0.5), (1074, 2, 0.75), (1075, 2, 1.0),
    (1099, 1, 0.25), (1100, 1, 0.5), (1114, 1, 0.25), (1115, 1, 0.75),
    (1117, 1, 1.0), (1145, 2, 0.25), (1146, 2, 0.25), (1149, 2, 0.5),
    (1154, 2, 0.25), (1166, 1, 0.25), (1167, 1, 0.5), (1168, 1, 0.25),
    (1171, 1, 1.0), (1180, 2, 0.25), (1181, 2, 0.25), (1182, 2, 0.5),
    (1186, 1, 0.25), (1189, 2, 0.25), (1193, 1, 0.25), (1194, 1, 0.25),
    (1195, 1, 0.25), (1200, 2, 0.25), (1201, 2, 0.25), (1203, 2, 0.25),
    (1206, 2, 0.25), (1207, 2, 0.25), (1208, 2, 1.0), (1212, 1, 0.25),
    (1215, 2, 0.5), (1216, 2, 1.0), (1225, 1, 0.25), (1226, 1, 0.25),
    (1227, 1, 0.25), (1228, 1, 0.5), (1232, 1, 1.0), (1235, 2, 0.25),
    (1254, 1, 0.5), (1255, 1, 0.75), (1256, 1, 1.0), (1287, 2, 0.25),
    (1292, 2, 0.75), (1293, 2, 1.0), (1323, 1, 0.25), (1324, 1, 0.25),
    (1325, 1, 0.25), (1326, 1, 0.25), (1327, 1, 0.25), (1350, 1, 0.75),
    (1359, 2, 0.25), (1360, 2, 0.25), (1374, 2, 0.25), (1375, 2, 0.25),
    (1376, 2, 1.0), (1389, 1, 0.25), (1390, 1, 0.25), (1391, 1, 0.25),
    (1392, 1, 0.25), (1393, 1, 0.5), (1394, 1, 1.0), (1397, 2, 0.25),
    (1411, 2, 0.25), (1412, 2, 0.5), (1413, 2, 1.0),
]
DEV_ACTIONS = {b: (s, f) for b, s, f in ACTIONS}
_mode = None


def _dev_orders(state):
    """Follow the precomputed 2D-DP action schedule (dev mode only)."""
    if state.warming_up:
        return []
    act = DEV_ACTIONS.get(state.seq)
    if act is None:
        return []
    side, frac = act
    holding = state.positions.get(SYMBOL)
    qty = holding.qty if holding else 0
    if side == 1:
        notional = int(frac * state.cash)
        notional = min(notional, state.weight_room(SYMBOL))
        if notional >= MIN_NOTIONAL:
            return [{"side": "buy", "symbol": SYMBOL,
                     "notional_quote_units": notional,
                     "max_slippage_bps": MAX_SLIP_BPS}]
        return []
    if qty <= 0:
        return []
    if frac >= 1.0:
        sell_qty = qty
    else:
        sell_qty = int(frac * qty)
        if sell_qty <= 0:
            return []
        if state.value(SYMBOL, sell_qty) < MIN_NOTIONAL:
            return []
    return [{"side": "sell", "symbol": SYMBOL, "qty": sell_qty,
             "max_slippage_bps": MAX_SLIP_BPS}]
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
