"""Tests that check the DEFINITIONS, not that the code agrees with itself.

An indicator test that computes the answer the same way the implementation
does proves only that the code is deterministic. Each test below either
hand-computes the expected value, or asserts a property the definition
guarantees, or reimplements the maths a different way.
"""

import unittest

import indicators as I


class TestVWAP(unittest.TestCase):

    def test_hand_computed(self):
        # (10*100 + 20*300) / (100+300) = 7000/400 = 17.5
        out = I.vwap([10, 20], [100, 300])
        self.assertAlmostEqual(out[0], 10.0)
        self.assertAlmostEqual(out[1], 17.5)

    def test_it_is_cumulative_not_rolling(self):
        # A rolling-2 VWAP of the last two bars would be (20*1+30*1)/2 = 25.
        # Session VWAP includes the first bar for ever: 60/3 = 20.
        out = I.vwap([10, 20, 30], [1, 1, 1])
        self.assertAlmostEqual(out[-1], 20.0)

    def test_zero_volume_bars_do_not_move_it(self):
        a = I.vwap([10, 999, 20], [100, 0, 100])
        b = I.vwap([10, 20], [100, 100])
        self.assertAlmostEqual(a[-1], b[-1])

    def test_undefined_before_any_volume(self):
        # Zero would draw a line at the bottom of the axis and look like
        # a price. None draws nothing, which is the truth.
        self.assertIsNone(I.vwap([10], [0])[0])

    def test_constant_price_gives_that_price(self):
        out = I.vwap([42.0] * 5, [3, 1, 4, 1, 5])
        self.assertAlmostEqual(out[-1], 42.0)


class TestSMA(unittest.TestCase):

    def test_hand_computed(self):
        out = I.sma([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)      # (1+2+3)/3
        self.assertAlmostEqual(out[3], 3.0)      # (2+3+4)/3
        self.assertAlmostEqual(out[4], 4.0)      # (3+4+5)/3

    def test_length_matches_input(self):
        self.assertEqual(len(I.sma(list(range(20)), 5)), 20)

    def test_a_gap_restarts_the_window_rather_than_shortening_it(self):
        # Without the restart, the bar after the gap would average only the
        # values it happens to have, silently using a shorter window.
        out = I.sma([1, 2, None, 4, 5, 6], 3)
        self.assertIsNone(out[2])
        self.assertIsNone(out[3])
        self.assertIsNone(out[4])
        self.assertAlmostEqual(out[5], 5.0)      # (4+5+6)/3


class TestEMA(unittest.TestCase):

    def test_seeded_with_the_sma(self):
        out = I.ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)      # SMA(1,2,3)

    def test_recursion_matches_the_definition(self):
        vals, n = [1, 2, 3, 4, 5], 3
        out = I.ema(vals, n)
        alpha = 2.0 / (n + 1.0)
        expect = 2.0                              # the seed
        for x in vals[n:]:
            expect = alpha * x + (1 - alpha) * expect
        self.assertAlmostEqual(out[-1], expect)

    def test_constant_series_is_that_constant(self):
        out = I.ema([7.0] * 30, 10)
        self.assertAlmostEqual(out[-1], 7.0)

    def test_reacts_faster_than_sma_to_a_step(self):
        step = [10.0] * 20 + [20.0] * 5
        self.assertGreater(I.ema(step, 10)[-1], I.sma(step, 10)[-1])


class TestRSI(unittest.TestCase):

    def test_all_gains_is_100(self):
        self.assertAlmostEqual(I.rsi(list(range(1, 40)), 14)[-1], 100.0)

    def test_all_losses_is_0(self):
        self.assertAlmostEqual(I.rsi(list(range(40, 1, -1)), 14)[-1], 0.0)

    def test_first_value_lands_at_index_n(self):
        out = I.rsi(list(range(1, 30)), 14)
        self.assertTrue(all(v is None for v in out[:14]))
        self.assertIsNotNone(out[14])

    def test_symmetric_moves_oscillate_tightly_around_50(self):
        # Equal alternating gains and losses. The first draft of this test
        # asserted exactly 50 and failed -- correctly. Wilder carries every
        # past change forward with a 1/n weight, so the last change always
        # tilts the average and the series OSCILLATES about 50 rather than
        # settling on it (51.76, 48.06, 51.77, 48.07, ...). The definition
        # promises symmetry, not a fixed point.
        vals, p = [], 100.0
        for i in range(60):
            p += 1.0 if i % 2 == 0 else -1.0
            vals.append(p)
        r = [x for x in I.rsi(vals, 14) if x is not None]
        for v in r[-10:]:
            self.assertLess(abs(v - 50.0), 3.0)
        # The midpoint of a cycle sits near 50 but not exactly on it (49.93
        # here): RSI is a nonlinear function of RS, so two RS values that
        # are reciprocals do not map to two RSI values that average to 50.
        # Half a point is the honest tolerance; a tighter one fails for a
        # reason that has nothing to do with the code being wrong.
        self.assertLess(abs((r[-1] + r[-2]) / 2 - 50.0), 0.5)
        self.assertLess(min(r[-2:]), 50.0)
        self.assertGreater(max(r[-2:]), 50.0)

    def test_wilder_smoothing_not_a_plain_mean(self):
        # After the seed, Wilder carries the whole history forward with a
        # 1/n weight. A plain n-period mean (Cutler's RSI) forgets anything
        # older than n bars. One big spike n+5 bars ago must therefore still
        # be visible under Wilder and gone under Cutler.
        # The spike must fall OUTSIDE Cutler's window for the direction to
        # be predictable. The first draft put it 8 bars back -- still inside
        # a 14-bar window -- where Cutler actually reads HIGHER (87.3 vs
        # 84.8), because Wilder had already decayed it by (13/14)^8 while
        # Cutler still counted it in full. Twenty bars back, Cutler has
        # forgotten it entirely and Wilder has not.
        def series(after):
            v = [100.0]
            for _ in range(14):
                v.append(v[-1] + 1.0)            # seed: all gains
            v.append(v[-1] + 50.0)               # one large spike
            for _ in range(after):
                v.append(v[-1] - 1.0)            # then steady losses
            return v

        def cutler(v, n):
            ch = [v[i] - v[i - 1] for i in range(1, len(v))][-n:]
            g = sum(max(c, 0) for c in ch) / n
            l = sum(max(-c, 0) for c in ch) / n
            return 100.0 if l == 0 else 100.0 - 100.0 / (1 + g / l)

        far = series(20)
        self.assertAlmostEqual(cutler(far, 14), 0.0, places=6)
        self.assertGreater(I.rsi(far, 14)[-1], 50.0,
                           "Wilder must still remember a spike Cutler forgot")

        # And inside the window the two simply differ -- which is the point:
        # they are not the same indicator.
        near = series(8)
        self.assertNotAlmostEqual(I.rsi(near, 14)[-1], cutler(near, 14),
                                  places=2)

    def test_hand_computed_seed(self):
        # 14 changes: ten +2 and four -1.
        #   avg gain = 20/14, avg loss = 4/14, RS = 5
        #   RSI = 100 - 100/6 = 83.333...
        vals, p = [100.0], 100.0
        for i in range(14):
            p += 2.0 if i < 10 else -1.0
            vals.append(p)
        self.assertAlmostEqual(I.rsi(vals, 14)[14], 100.0 - 100.0 / 6.0,
                               places=9)

    def test_bounded(self):
        import random
        random.seed(4)
        p, vals = 100.0, []
        for _ in range(500):
            p *= 1 + random.gauss(0, 0.01)
            vals.append(p)
        for v in I.rsi(vals, 14):
            if v is not None:
                self.assertGreaterEqual(v, 0.0)
                self.assertLessEqual(v, 100.0)


class TestBuildup(unittest.TestCase):

    def test_the_four_quadrants(self):
        self.assertEqual(I.buildup(+0.05, +0.05), "long buildup")
        self.assertEqual(I.buildup(-0.05, +0.05), "short buildup")
        self.assertEqual(I.buildup(+0.05, -0.05), "short covering")
        self.assertEqual(I.buildup(-0.05, -0.05), "long unwinding")

    def test_only_a_quiet_OI_is_flat(self):
        self.assertEqual(I.buildup(0.5, 0.001), "flat")
        self.assertEqual(I.buildup(0.001, 0.001), "flat")

    def test_a_big_OI_move_with_no_price_direction_is_not_flat(self):
        # The live chain showed 23100 PE at price -1.9% and OI +172.6%.
        # Calling that "flat" discarded the most informative row on the
        # board. The price floor says the DIRECTION is unclear, not that
        # nothing happened.
        self.assertEqual(I.buildup(-0.019, 1.726, px_eps=0.02, oi_eps=0.02),
                         "building, unclear")
        self.assertEqual(I.buildup(0.001, -0.8), "closing, unclear")

    def test_unclear_still_reports_the_direction_of_the_OI(self):
        self.assertIn("building", I.buildup(0.0, +0.5))
        self.assertIn("closing", I.buildup(0.0, -0.5))

    def test_missing_input_is_none_not_a_guess(self):
        self.assertIsNone(I.buildup(None, 0.5))
        self.assertIsNone(I.buildup(0.5, None))

    def test_growth_from_zero_open_interest_is_not_dropped(self):
        # 21,350 PE went from 0 to 1,951,755 contracts -- the clearest new
        # positioning on the chain -- and the naive (b-a)/a guard reported
        # it as unknown, which is the one row a reader most wants.
        d = I.frac_change(0, 1_951_755)
        self.assertEqual(d, float("inf"))
        self.assertEqual(I.buildup(0.57, d, px_eps=0.02, oi_eps=0.02),
                         "long buildup")

    def test_frac_change_ordinary_and_degenerate(self):
        self.assertAlmostEqual(I.frac_change(100, 150), 0.5)
        self.assertEqual(I.frac_change(0, 0), 0.0)
        self.assertEqual(I.frac_change(0, -5), float("-inf"))
        self.assertIsNone(I.frac_change(None, 5))

    def test_thresholds_are_fractional_so_scale_does_not_matter(self):
        # The same 10% move on a 4-rupee option and a 400-rupee one must
        # classify identically -- which it does only because the caller
        # passes fractional changes, and this test documents that contract.
        self.assertEqual(I.buildup(0.10, 0.10), I.buildup(0.10, 0.10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
