"""Tests for the two chain views, built on prices a model could produce.

THE FIXTURE THAT FAILED, AND WHY IT MATTERED

The first version of this file moved every call up and every put down by the
SAME amount at every strike, then asserted that straddles were therefore
unchanged by a forward move. Every test passed. The claim was false.

That fixture requires every option on the board to have delta +1 or -1 at
once, which no chain can. It was not a chain; it was an object built to
agree with the conclusion. Live data exposed the error within minutes --
the residual fell monotonically with strike and crossed zero exactly at the
money, which is delta's signature and nothing else's.

So this file prices options with the Bachelier (normal) model instead. It is
the same model the project's own pricing uses, and it has a property that
makes it exactly the right instrument here: with constant sigma, a straddle's
value depends only on (F - K). Shifting the forward by dF is therefore
identical to shifting the strike by dF, which is precisely the correction
otm_rows applies -- so under a pure forward move the residual must come
out at zero, and any leak shows up immediately.
"""

import math
import unittest

import chain_buildup as CB
import indicators as IND


def _N(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def call(f, k, s):
    """Bachelier call. s is sigma*sqrt(T) in points."""
    if s <= 0:
        return max(0.0, f - k)
    d = (f - k) / s
    return (f - k) * _N(d) + s * _phi(d)


def leg(mid, oi):
    return {"mid": round(mid, 2), "volume": 10, "pv": 10 * mid, "oi": oi}


def chain(f, s, strikes, oi=500_000, sigma_at=None):
    """A two-sided chain priced off the forward.

    sigma_at, when given, overrides s per strike -- the only way to inject a
    vol move that is not also a forward move.
    """
    out = {}
    for k in strikes:
        k = float(k)
        sk = s if sigma_at is None else sigma_at(k)
        c = call(f, k, sk)
        p = c - (f - k)                      # parity, exactly
        out[k] = {"CE": leg(c, oi), "PE": leg(p, oi)}
    return out


STRIKES = [22600 + 50 * i for i in range(41)]       # 22600 .. 24600
F0, SIG = 23300.0, 180.0


class TestPureForwardMove(unittest.TestCase):
    """Only the forward moves. No vol change, no positioning."""

    def setUp(self):
        self.sess = {
            "09:20": chain(F0, SIG, STRIKES, oi=500_000),
            "12:00": chain(F0 + 60.0, SIG, STRIKES, oi=750_000),
        }

    def test_the_parity_forward_is_recovered(self):
        a = CB.parity_forward(self.sess["09:20"])
        b = CB.parity_forward(self.sess["12:00"])
        self.assertAlmostEqual(a, F0, places=1)
        self.assertAlmostEqual(b - a, 60.0, places=1)

    def test_the_per_leg_view_degenerates_along_call_versus_put(self):
        rows, _ = CB.strike_rows(self.sess)
        ce = [r for r in rows if r["opt_type"] == "CE"]
        pe = [r for r in rows if r["opt_type"] == "PE"]
        self.assertTrue(ce and pe)
        # Every call rose and every put fell: the label is the index.
        self.assertTrue(all(r["d_px"] > 0 for r in ce))
        self.assertTrue(all(r["d_px"] < 0 for r in pe))

    def test_raw_price_change_DOES_contain_the_forward(self):
        # The uncorrected change is not clean. A forward rise makes OTM puts
        # further out of the money and cheaper, and OTM calls nearer and
        # dearer, with no vol change at all.
        srows, _, _ = CB.otm_rows(self.sess)
        # Bands chosen inside the penny floor: far wings are filtered out
        # because a 5-paise quote moves 50% on one tick.
        lo = [r for r in srows if r["strike"] <= F0 - 100]
        hi = [r for r in srows if r["strike"] >= F0 + 100]
        self.assertTrue(lo and hi)
        self.assertTrue(all(r["d_px"] < 0 for r in lo),
                        "OTM puts must CHEAPEN when the forward rises")
        self.assertTrue(all(r["d_px"] > 0 for r in hi),
                        "OTM calls must RICHEN when the forward rises")

    def test_the_forward_adjustment_removes_it(self):
        srows, med, _ = CB.otm_rows(self.sess)
        self.assertTrue(srows)
        # Bachelier with constant sigma depends only on (F - K), so shifting
        # the strike by dF is exact. What is left is interpolation error on
        # a 50-point grid, which is small.
        # Log interpolation across the convex curve leaves well under 1%;
        # the straight-line version left -1% to -3% and one +17% row where
        # the forward had crossed a strike.
        self.assertLess(abs(med), 0.01)
        for r in srows:
            self.assertLess(abs(r["resid"]), 0.005,
                            msg=f"leak at {r['strike']:.0f}: {r['resid']:.3%}")

    def test_and_therefore_reports_no_vol_direction(self):
        srows, _, _ = CB.otm_rows(self.sess)
        directional = {"vol bought", "vol sold",
                       "vol shorts covering", "vol longs unwinding"}
        got = {r["label"] for r in srows}
        self.assertFalse(got & directional,
                         f"the forward leaked into a vol label: {got}")

    def test_the_open_interest_still_comes_through(self):
        # Refusing a direction is not reporting nothing.
        srows, _, _ = CB.otm_rows(self.sess)
        for r in srows:
            self.assertAlmostEqual(r["d_oi"], 0.5, places=6)


class TestARealVolMove(unittest.TestCase):

    def test_a_uniform_vol_rise_lifts_every_strike(self):
        sess = {"09:20": chain(F0, SIG, STRIKES),
                "12:00": chain(F0, SIG * 1.10, STRIKES, oi=700_000)}
        srows, med, _ = CB.otm_rows(sess)
        self.assertGreater(med, 0.10)
        self.assertTrue(all(r["d_adj"] > 0 for r in srows))

    def test_but_NOT_by_the_same_percentage_which_is_a_known_limit(self):
        """The documented failure mode, pinned so it cannot be forgotten.

        A parallel vol shift is not a parallel percentage shift: an option
        far out of the money gains much more of its own value than one at
        the money. Removing the median therefore leaves a moneyness-shaped
        residual that is arithmetic, not positioning. Measuring in
        implied-vol units would fix it and would require a model.
        """
        sess = {"09:20": chain(F0, SIG, STRIKES),
                "12:00": chain(F0, SIG * 1.10, STRIKES, oi=700_000)}
        srows, _, _ = CB.otm_rows(sess)
        spread = max(r["resid"] for r in srows) - min(r["resid"] for r in srows)
        self.assertGreater(spread, 0.2,
                           msg="if this shrinks, the limitation was fixed "
                               "and the docstring needs updating")

    def test_one_region_bid_for_vol_is_named(self):
        # Downside vol bid, everything else unchanged: a skew move, which
        # is the observation this view exists to surface.
        def sk(k):
            return SIG * (1.25 if k <= F0 - 150 else 1.0)
        sess = {"09:20": chain(F0, SIG, STRIKES),
                "12:00": chain(F0, SIG, STRIKES, oi=700_000, sigma_at=sk)}
        srows, _, _ = CB.otm_rows(sess)
        by_k = {r["strike"]: r for r in srows}
        self.assertEqual(by_k[23100.0]["label"], "vol bought")
        self.assertGreater(by_k[23100.0]["resid"], 0.05)
        # And the strikes it did not touch come out at exactly zero.
        self.assertAlmostEqual(by_k[F0 + 200]["resid"], 0.0, places=4)
        self.assertNotIn("vol", by_k[F0 + 200]["label"] or "")

    def test_a_vol_move_survives_a_simultaneous_forward_move(self):
        # The case that matters in live data: both happen at once. The
        # forward adjustment must strip one and leave the other.
        def sk(k):
            return SIG * (1.25 if k <= F0 - 150 else 1.0)
        sess = {"09:20": chain(F0, SIG, STRIKES),
                "12:00": chain(F0 + 60.0, SIG, STRIKES, oi=700_000,
                               sigma_at=sk)}
        srows, _, _ = CB.otm_rows(sess)
        by_k = {r["strike"]: r for r in srows}
        self.assertEqual(by_k[23100.0]["label"], "vol bought")
        self.assertNotIn("vol", by_k[F0 + 200]["label"] or "")


class TestMissingData(unittest.TestCase):

    def test_zero_opening_open_interest_is_excluded(self):
        a = chain(F0, SIG, STRIKES, oi=0)
        b = chain(F0, SIG, STRIKES, oi=900_000)
        srows, _, skipped = CB.otm_rows({"09:20": a, "12:00": b})
        self.assertEqual(srows, [])
        self.assertEqual(skipped["missing"] + skipped["penny"], len(STRIKES))

    def test_a_one_sided_strike_is_skipped(self):
        a = {23100.0: {"CE": leg(100.0, 500_000)}}
        b = {23100.0: {"CE": leg(105.0, 700_000)}}
        srows, _, skipped = CB.otm_rows({"09:20": a, "12:00": b})
        self.assertEqual(srows, [])
        self.assertEqual(skipped["missing"], 1)

    def test_one_minute_of_session_yields_a_reason_not_a_crash(self):
        srows, med, skipped = CB.otm_rows({"09:20": {}})
        self.assertEqual(srows, [])
        self.assertIsNone(med)
        self.assertIn("reason", skipped)


class TestInterpolation(unittest.TestCase):

    def test_it_refuses_to_extrapolate(self):
        curve = [(100.0, 1.0), (200.0, 2.0)]
        self.assertIsNone(CB._interp(curve, 99.0))
        self.assertIsNone(CB._interp(curve, 201.0))

    def test_it_interpolates_geometrically_not_linearly(self):
        # Midway between 1 and 4 is 2 (the geometric mean), not 2.5. An
        # option curve decays exponentially, so the straight line sits
        # above it everywhere and biases every residual the same way.
        curve = [(100.0, 1.0), (200.0, 4.0)]
        self.assertAlmostEqual(CB._interp(curve, 150.0), 2.0, places=9)

    def test_a_strike_the_forward_crossed_is_dropped(self):
        sess = {"09:20": chain(F0, SIG, STRIKES),
                "12:00": chain(F0 + 60.0, SIG, STRIKES, oi=700_000)}
        _, _, skipped = CB.otm_rows(sess)
        self.assertEqual(skipped["crossed"], 1)
        self.assertNotIn(23350.0, {r["strike"] for r in CB.otm_rows(sess)[0]})

    def test_strikes_shifted_off_the_curve_are_dropped_not_guessed(self):
        sess = {"09:20": chain(F0, SIG, STRIKES),
                "12:00": chain(F0 + 60.0, SIG, STRIKES, oi=700_000)}
        rows, _, skipped = CB.otm_rows(sess)
        # Nothing may be extrapolated; everything returned was interpolated
        # from inside the opening curve.
        self.assertTrue(rows)
        self.assertTrue(all(r["px_expect"] is not None for r in rows))


class TestLabelVocabulary(unittest.TestCase):

    def test_it_reuses_the_tested_classifier(self):
        # One implementation of the four quadrants, not two.
        self.assertEqual(IND.buildup(0.05, 0.05), "long buildup")

    def test_the_names_say_vol_not_direction(self):
        def sk(k):
            return SIG * (1.25 if k <= F0 - 150 else 1.0)
        sess = {"09:20": chain(F0, SIG, STRIKES),
                "12:00": chain(F0, SIG, STRIKES, oi=700_000, sigma_at=sk)}
        got = {r["label"] for r in CB.otm_rows(sess)[0]}
        self.assertNotIn("long buildup", got)
        self.assertIn("vol bought", got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
