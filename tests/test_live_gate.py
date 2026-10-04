"""The gate state machine, tested in isolation.

The thing this guards against is the failure seen on the first live day:
credit/sigma 0.934 at 11:52, 0.944 at 11:54, the gate flipping on a move of
one hundredth. Every test below is a sequence of credit/sigma readings and
the sequence of gate states it should produce.

step_gate is called with dry=True throughout so nothing touches disk.
"""

import importlib
import sys
import types
import unittest

# live_signal imports two server-side modules that a DEV box may not have:
# `market` (the NSE calendar) and, lazily inside main(), `session` (the Kite
# client). Neither is reachable from the gate machine, so they are stubbed
# rather than mocked -- if a future edit makes step_gate depend on either,
# the stub will fail loudly instead of quietly passing.
#
# The guard was `if _name not in sys.modules`, which stubbed whenever the
# module had not been imported YET -- not only when it was missing. On the
# server, where session.py is perfectly importable, that put a stub into
# sys.modules for the WHOLE pytest run, and every later test importing the
# real thing died with
#     ImportError: cannot import name 'get_kite' from 'session'
#                  (unknown location)
# ("unknown location" is a types.ModuleType with no __file__, not a namespace
# package.) test_reconcile.py passed alone and failed in the suite, which is
# the signature of exactly this. Try the real module first; stub only what is
# genuinely absent, and never replace something already imported.
for _name in ("market", "session"):
    if _name in sys.modules:
        continue
    try:
        importlib.import_module(_name)
    except Exception:
        _m = types.ModuleType(_name)
        _m.is_trading_day = lambda d: True
        sys.modules[_name] = _m

import live_signal as L
import models as M


def run(readings, start="closed"):
    """Feed readings through the machine; return the state after each."""
    st = {"day": "2026-09-15", "expiry": "2026-09-16",
          "state": start, "pending": None, "count": 0}
    out = []
    for cs in readings:
        _, st = L.step_gate(st, cs, dry=True)
        out.append(st["state"])
    return out


class TestThresholds(unittest.TestCase):

    def test_open_threshold_matches_the_pop_it_claims(self):
        # GATE_OPEN is documented as POP 0.650 on the ATM straddle and must
        # stay identical to the gate the backtest was run with.
        from statistics import NormalDist
        self.assertAlmostEqual(L.GATE_OPEN,
                               NormalDist().inv_cdf((1 + 0.650) / 2), places=4)
        import intraday as I
        self.assertAlmostEqual(L.GATE_OPEN, I.CS_GATE, places=4)

    def test_close_threshold_matches_the_pop_it_claims(self):
        from statistics import NormalDist
        self.assertAlmostEqual(L.GATE_CLOSE,
                               NormalDist().inv_cdf((1 + 0.620) / 2), places=4)

    def test_band_is_a_band(self):
        self.assertLess(L.GATE_CLOSE, L.GATE_OPEN)


class TestOpening(unittest.TestCase):

    def test_one_strong_reading_is_not_enough(self):
        self.assertEqual(run([0.99]), ["closed"])

    def test_confirm_consecutive_readings_open_it(self):
        self.assertEqual(run([0.99] * L.CONFIRM)[-1], "open")

    def test_the_chatter_that_prompted_this_does_not_open_the_gate(self):
        # The real sequence: below, above, below, above. Under a bare
        # threshold that is two openings and two closings in eight minutes.
        self.assertEqual(run([0.934, 0.944, 0.930, 0.941]),
                         ["closed"] * 4)

    def test_a_broken_run_restarts_the_count(self):
        # Two strong readings would open it -- but the dip between them
        # clears the pending count, so the third is only the second in a row.
        seq = [0.99] * (L.CONFIRM - 1) + [0.50] + [0.99] * (L.CONFIRM - 1)
        self.assertEqual(run(seq)[-1], "closed")

    def test_a_sustained_move_still_gets_through(self):
        # Hysteresis must not mean "never". CONFIRM cycles is the price.
        states = run([0.99] * (L.CONFIRM + 3))
        self.assertEqual(states[L.CONFIRM - 1], "open")
        self.assertTrue(all(s == "open" for s in states[L.CONFIRM - 1:]))


class TestClosing(unittest.TestCase):

    def test_a_reading_inside_the_band_does_not_close_an_open_gate(self):
        # This is the whole point of the band: 0.90 is below the open
        # threshold but above the close one, so an open gate stays open.
        mid = (L.GATE_OPEN + L.GATE_CLOSE) / 2
        self.assertEqual(run([mid] * 10, start="open"), ["open"] * 10)

    def test_one_weak_reading_is_not_enough_to_close(self):
        self.assertEqual(run([0.10], start="open"), ["open"])

    def test_confirm_weak_readings_close_it(self):
        self.assertEqual(run([0.10] * L.CONFIRM, start="open")[-1], "closed")

    def test_a_recovery_inside_the_band_aborts_a_pending_close(self):
        seq = [0.10] * (L.CONFIRM - 1) + [0.90] + [0.10] * (L.CONFIRM - 1)
        self.assertEqual(run(seq, start="open")[-1], "open")


class TestStateFileScoping(unittest.TestCase):

    def test_a_new_day_starts_closed(self):
        # Yesterday's open gate must not be inherited: the state file is
        # keyed on the day, and a mismatch means a fresh closed state.
        import datetime as dt
        st = L.load_gate(dt.date(2026, 9, 15), dt.date(2026, 9, 16))
        self.assertIn(st["state"], ("closed", "open"))
        self.assertEqual(st["day"], "2026-09-15")

    def test_missing_file_is_not_an_error(self):
        import datetime as dt
        old, L.GATE_STATE = L.GATE_STATE, "/nonexistent/path/gate.json"
        try:
            st = L.load_gate(dt.date(2026, 9, 15), dt.date(2026, 9, 16))
        finally:
            L.GATE_STATE = old
        self.assertEqual(st["state"], "closed")
        self.assertEqual(st["count"], 0)


class TestPairedRescore(unittest.TestCase):
    """The paired re-score must not disturb the numbers the gate acted on.

    score_candidate mutates its argument in place and returns it. Re-pricing
    the acting pick under the alternate sigma therefore has to work on a
    copy -- passing the pick itself would silently overwrite the POP, EV and
    credit/sigma that the gate decision was just made from, and the recorded
    row would describe a decision that was never taken.
    """

    def setUp(self):
        import intraday as I
        self.I = I
        # A synthetic chain, symmetric about 23300, priced so that every
        # strike is two-sided and the straddle is the richest structure.
        self.spot = 23300.0
        self.prices = {}
        for k in range(22800, 23851, 50):
            d = abs(k - self.spot)
            self.prices[(float(k), "CE")] = max(4.0, 90.0 - 0.06 * d)
            self.prices[(float(k), "PE")] = max(4.0, 90.0 - 0.06 * d)

    def test_rescoring_a_copy_leaves_the_original_untouched(self):
        I = self.I
        cands = [I.score_candidate(c, self.spot, 85.0, 91.0, 75)
                 for c in I.candidates(self.prices, self.spot, 85.0,
                                       I.TWO_LEG)]
        pick = max(cands, key=lambda c: c["net_ev_rs"])
        before = {k: pick[k] for k in
                  ("cs", "pop_terminal", "pop_no_touch", "net_ev_rs")}
        paired = I.score_candidate(dict(pick), self.spot, 70.7, 75.4, 75)
        after = {k: pick[k] for k in before}
        self.assertEqual(before, after, "the acting pick was mutated")
        self.assertNotEqual(paired["cs"], pick["cs"],
                            "the re-score produced no change at all")

    def test_a_narrower_sigma_raises_pop_on_the_same_strikes(self):
        # Strikes fixed, sigma cut: the zone is unchanged but the move that
        # can escape it is smaller, so every probability must improve.
        I = self.I
        c = I.candidates(self.prices, self.spot, 85.0, ("straddle",))[0]
        wide = I.score_candidate(dict(c), self.spot, 85.0, 91.0, 75)
        tight = I.score_candidate(dict(c), self.spot, 70.7, 75.4, 75)
        self.assertEqual((wide["ce"], wide["pe"]), (tight["ce"], tight["pe"]))
        self.assertGreater(tight["pop_terminal"], wide["pop_terminal"])
        self.assertGreater(tight["pop_no_touch"], wide["pop_no_touch"])
        self.assertGreater(tight["cs"], wide["cs"])

    def test_straddle_strikes_ignore_sigma_but_strangle_strikes_do_not(self):
        # This is the confound the paired column exists to remove.
        I = self.I
        s_wide = I.candidates(self.prices, self.spot, 85.0, ("straddle",))[0]
        s_tight = I.candidates(self.prices, self.spot, 70.7, ("straddle",))[0]
        self.assertEqual((s_wide["ce"], s_wide["pe"]),
                         (s_tight["ce"], s_tight["pe"]))
        g_wide = I.candidates(self.prices, self.spot, 85.0, ("strangle_1.5",))
        g_tight = I.candidates(self.prices, self.spot, 70.7, ("strangle_1.5",))
        self.assertNotEqual((g_wide[0]["ce"], g_wide[0]["pe"]),
                            (g_tight[0]["ce"], g_tight[0]["pe"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSpotBelievability(unittest.TestCase):
    """The guard is tested against the minutes that exposed the problem.

    These are the recorded mids from 15 September, not invented numbers. The
    15:27 and 15:28 cycles must be refused and the 15:29 one must pass --
    that is the whole specification.
    """

    # (strike, ce_mid, pe_mid) as the collector recorded them
    M1527 = [(23050, 77.22, 0.17), (23100, 29.48, 1.62), (23150, 4.53, 27.38),
             (23200, 0.78, 74.03), (23250, 0.28, 124.70)]
    M1528 = [(23050, 74.10, 0.12), (23100, 18.80, 0.23), (23150, 0.23, 31.43),
             (23200, 0.12, 81.28), (23250, 0.12, 131.22)]
    M1529 = [(23000, 118.55, 0.05), (23050, 68.60, 0.05),
             (23100, 18.65, 0.05), (23150, 0.08, 31.43)]

    @staticmethod
    def rows(legs):
        return [{"strike": k, "ce": c, "pe": p} for k, c, p in legs]

    def verdict(self, legs, spot, dte=0, mins_to_close=3.0):
        import vol_time as V
        years = max(0.0, dte + mins_to_close / V.SESSION_MINUTES) / 365.0
        ok, fwd, disp, tol, _ = M.spot_is_believable(self.rows(legs), spot,
                                                     years)
        return fwd, disp, ok, tol

    def test_the_stale_1527_index_is_refused(self):
        fwd, disp, ok, tol = self.verdict(self.M1527, 23172.35)
        self.assertAlmostEqual(fwd, 23127.05, places=1)
        self.assertLess(disp, 3.0, "the chain agreed with itself")
        self.assertFalse(ok, f"gap {fwd - 23172.35:+.1f} vs tolerance {tol}")

    def test_the_stale_1528_index_is_refused(self):
        fwd, disp, ok, tol = self.verdict(self.M1528, 23172.35,
                                          mins_to_close=2.0)
        self.assertFalse(ok, f"gap {fwd - 23172.35:+.1f} vs tolerance {tol}")

    def test_the_good_1529_index_passes(self):
        # Once the feed caught up, index and chain agree to a hundredth.
        fwd, disp, ok, tol = self.verdict(self.M1529, 23118.60,
                                          mins_to_close=1.0)
        self.assertAlmostEqual(fwd, 23118.60, places=1)
        self.assertLess(disp, 0.5)
        self.assertTrue(ok, f"a clean minute must not be refused (tol {tol})")

    def test_the_old_flat_guard_would_have_missed_all_of_this(self):
        # BASIS_MAX was 150. Every gap here is far inside it, which is why
        # the bad cycles were recorded as if they were fine.
        for legs, spot in ((self.M1527, 23172.35), (self.M1528, 23172.35)):
            fwd, _ = M.forward_and_spread(self.rows(legs), spot)
            self.assertLess(abs(fwd - spot), 150.0)

    def test_carry_allowance_grows_with_time_to_expiry(self):
        # A 30-point gap is impossible at 0 DTE and unremarkable at 20.
        near = M.basis_tolerance(23000.0, 0.0, 0.0)
        far = M.basis_tolerance(23000.0, 20 / 365.0, 0.0)
        self.assertLess(near, far)
        self.assertAlmostEqual(near, M.BASIS_FLOOR, places=6)

    def test_a_chain_that_disagrees_with_itself_is_caught(self):
        # One leg mispriced by 60 points: dispersion explodes, and the guard
        # must key on that rather than on the median still looking sane.
        bad = list(self.M1529)
        bad[0] = (23000, 178.55, 0.05)
        _, disp = M.forward_and_spread(self.rows(bad), 23118.60)
        self.assertGreater(disp, M.CHAIN_INCOHERENT)

    def test_too_few_two_sided_strikes_returns_nothing(self):
        thin = [{"strike": 23100, "ce": 10.0, "pe": 0.0},
                {"strike": 23150, "ce": 0.0, "pe": 10.0}]
        self.assertEqual(M.forward_and_spread(thin, 23120.0), (None, None))
