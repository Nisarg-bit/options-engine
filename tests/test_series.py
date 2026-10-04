"""Tests for the structure series, focused on the trap it exists to avoid.

The central claim of series.py is that indicators on a ROLLING at-the-money
series are meaningless, and that a FIXED strike is what indicators need. The
tests below construct a session where the index drifts far enough to roll the
strike several times, then check that the rolling series really is
discontinuous and that indicators are refused on it.
"""

import math
import unittest

import series as S


def leg(mid, volume=100, pv=None, oi=1000):
    return {"mid": mid, "volume": volume,
            "pv": mid * volume if pv is None else pv, "oi": oi}


def session(spot_path, strikes=(23000, 23050, 23100, 23150, 23200, 23250),
            base=90.0, decay=200.0, start="09:20"):
    """A synthetic session: intrinsic plus time value that DECAYS with
    distance from the money.

    The first version of this fixture gave every strike the same flat time
    value. That made a strangle cost exactly as much as a straddle, and it
    made the fixed-strike series jump as much as the rolling one -- so two
    tests failed for reasons that were entirely the fixture's fault. Time
    value has to fall away from the money or the fixture is not a chain.
    """
    h0, m0 = (int(x) for x in start.split(":"))
    out = {}
    for i, spot in enumerate(spot_path):
        mins = h0 * 60 + m0 + i
        ts = f"{mins // 60:02d}:{mins % 60:02d}"
        m = {}
        for k in strikes:
            k = float(k)
            tv = base * math.exp(-0.5 * ((k - spot) / decay) ** 2)
            m[k] = {"CE": leg(round(max(0.0, spot - k) + tv, 2)),
                    "PE": leg(round(max(0.0, k - spot) + tv, 2))}
        out[ts] = m
    return out


class TestATMByParity(unittest.TestCase):

    def test_finds_the_strike_where_call_and_put_agree(self):
        m = session([23100.0])["09:20"]
        self.assertEqual(S.atm_by_parity(m), 23100.0)

    def test_is_model_free(self):
        # Shifting every premium by a constant does not move the parity ATM,
        # because it cancels in |C - P|.
        m = session([23100.0], base=5.0)["09:20"]
        n = session([23100.0], base=500.0)["09:20"]
        self.assertEqual(S.atm_by_parity(m), S.atm_by_parity(n))

    def test_none_when_no_strike_is_two_sided(self):
        self.assertIsNone(S.atm_by_parity({23100.0: {"CE": leg(10.0)}}))


class TestStrikeStep(unittest.TestCase):

    def test_infers_fifty(self):
        self.assertEqual(S.infer_step([23000, 23050, 23100, 23150]), 50.0)

    def test_survives_a_missing_strike(self):
        # A gap of 100 appears once; 50 still repeats more often.
        self.assertEqual(S.infer_step([23000, 23050, 23150, 23200]), 50.0)


class TestFixedAnchor(unittest.TestCase):

    def setUp(self):
        # Drifts 23100 -> 23260, far enough to roll the ATM several times.
        self.sess = session([23100.0 + 4 * i for i in range(40)])

    def test_strike_never_changes(self):
        out = S.build(self.sess, "straddle", anchor="atm_open")
        strikes = {tuple(sorted(g["strike"] for g in r["legs"]))
                   for r in out["rows"]}
        self.assertEqual(len(strikes), 1)
        self.assertEqual(out["strike_rolls"], 0)

    def test_indicators_are_computed(self):
        out = S.build(self.sess, "straddle", anchor="atm_open",
                      ma=5, ema=5, rsi=5)
        self.assertTrue(any(r["rsi"] is not None for r in out["rows"]))
        self.assertTrue(any(r["ma"] is not None for r in out["rows"]))

    def test_an_explicit_strike_is_honoured(self):
        out = S.build(self.sess, "call", anchor=23200.0)
        self.assertTrue(all(r["legs"][0]["strike"] == 23200.0
                            for r in out["rows"]))

    def test_price_rises_as_the_index_rises_through_a_fixed_call(self):
        out = S.build(self.sess, "call", anchor=23100.0)
        self.assertGreater(out["rows"][-1]["price"], out["rows"][0]["price"])


class TestRollingAnchorIsRefusedIndicators(unittest.TestCase):

    def setUp(self):
        self.sess = session([23100.0 + 4 * i for i in range(40)])

    def test_the_strike_actually_rolls(self):
        out = S.build(self.sess, "straddle", anchor="atm_rolling")
        self.assertGreater(out["strike_rolls"], 0,
                           "the fixture must roll or the test proves nothing")

    def test_indicators_are_none_not_wrong(self):
        out = S.build(self.sess, "straddle", anchor="atm_rolling",
                      ma=5, ema=5, rsi=5)
        self.assertTrue(all(r["rsi"] is None for r in out["rows"]))
        self.assertTrue(all(r["ma"] is None for r in out["rows"]))
        self.assertTrue(all(r["ema"] is None for r in out["rows"]))

    def test_the_refusal_is_explained_in_the_payload(self):
        # Assert the SUBSTANCE, not a keyword. The first version looked for
        # the word "discontinuous", which stopped appearing the moment the
        # note was corrected to say a rolling straddle is nearly continuous
        # -- so the test was pinned to a claim that turned out to be false.
        joined = " ".join(
            S.build(self.sess, "straddle",
                    anchor="atm_rolling")["notes"]).lower()
        self.assertIn("rolling", joined)
        self.assertIn("indicators are not computed", joined)
        self.assertIn("vwap", joined)

    def test_a_single_leg_refusal_names_the_price_jump(self):
        joined = " ".join(
            S.build(self.sess, "call",
                    anchor="atm_rolling")["notes"]).lower()
        self.assertIn("half a strike", joined)

    @staticmethod
    def _max_jump(rows):
        return max(abs(b["price"] - a["price"])
                   for a, b in zip(rows, rows[1:]))

    def test_a_single_leg_really_does_jump_at_a_roll(self):
        # This is where the discontinuity is real: at the midpoint the old
        # ATM call is half a strike in the money and the new one is half a
        # strike out, so the price steps by roughly step/2.
        roll = S.build(self.sess, "call", anchor="atm_rolling")["rows"]
        fix = S.build(self.sess, "call", anchor="atm_open")["rows"]
        self.assertGreater(self._max_jump(roll), 5 * self._max_jump(fix))

    def test_a_rolling_straddle_is_NEARLY_continuous(self):
        # The surprise that corrected this module's docstring. The ATM is
        # defined as the strike where call and put agree, which is also
        # where the two candidate straddles agree -- so the roll costs
        # almost nothing in price. Indicators are still refused, because
        # VWAP, volume and OI do not survive a change of contract.
        roll = S.build(self.sess, "straddle", anchor="atm_rolling")["rows"]
        fix = S.build(self.sess, "straddle", anchor="atm_open")["rows"]
        self.assertLess(self._max_jump(roll), 3 * self._max_jump(fix))


class TestVWAP(unittest.TestCase):

    def test_straddle_vwap_is_the_sum_of_leg_vwaps(self):
        sess = session([23100.0] * 5)
        out = S.build(sess, "straddle", anchor="atm_open")
        r = out["rows"][-1]
        # Flat prices all session, so each leg's VWAP equals its price and
        # the sum equals the straddle price.
        self.assertAlmostEqual(r["vwap"], r["price"], places=2)

    def test_the_definition_is_disclosed(self):
        out = S.build(session([23100.0] * 3), "straddle", anchor="atm_open")
        self.assertIn("summed", " ".join(out["notes"]))

    def test_vwap_lags_a_rising_price(self):
        sess = session([23100.0 + 4 * i for i in range(40)])
        out = S.build(sess, "call", anchor=23100.0)
        last = out["rows"][-1]
        self.assertLess(last["vwap"], last["price"])


class TestStrangle(unittest.TestCase):

    def test_legs_straddle_the_money_at_the_requested_width(self):
        out = S.build(session([23100.0] * 3), "strangle",
                      anchor="atm_open", width=2)
        legs = {(g["strike"], g["opt_type"]) for g in out["rows"][0]["legs"]}
        self.assertEqual(legs, {(23200.0, "CE"), (23000.0, "PE")})

    def test_a_strangle_is_cheaper_than_the_straddle(self):
        sess = session([23100.0] * 3)
        st = S.build(sess, "straddle", anchor="atm_open")["rows"][0]["price"]
        sg = S.build(sess, "strangle", anchor="atm_open",
                     width=2)["rows"][0]["price"]
        self.assertLess(sg, st)


class TestDegenerateInput(unittest.TestCase):

    def test_empty_session(self):
        out = S.build({}, "straddle")
        self.assertEqual(out["rows"], [])
        self.assertTrue(out["notes"])

    def test_unknown_structure_raises(self):
        with self.assertRaises(ValueError):
            S.build(session([23100.0]), "butterfly")

    def test_unpriceable_strike_yields_no_rows_rather_than_zeros(self):
        out = S.build(session([23100.0] * 3), "call", anchor=99999.0)
        self.assertEqual(out["rows"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAnchorSkipsTheOpeningAuction(unittest.TestCase):
    """The anchor must not be chosen from the opening-auction minutes.

    The session opens at 09:15 and the first minutes are the auction
    unwinding. This project has already paid for trusting that window once:
    the 09:00 signal read the pre-open print 853 points high. Anchoring a
    whole session's chart there picks the wrong contract for the entire day.
    """

    def build_sess(self):
        # 09:15-09:19 print a spot far from where the session actually
        # settles -- an auction artefact. From 09:20 the real level is 23100.
        # The default fixture starts at 09:20, so this one starts earlier on
        # purpose: without auction minutes the test proves nothing.
        path = [23400.0] * 5 + [23100.0] * 30
        return session(path, start="09:15")

    def test_the_auction_minutes_do_not_choose_the_strike(self):
        out = S.build(self.build_sess(), "straddle", anchor="atm_open")
        got = out["rows"][0]["legs"][0]["strike"]
        self.assertEqual(got, 23100.0,
                         "anchored on the auction print, not the session")

    def test_the_auction_minutes_are_still_plotted(self):
        # They are real quotes. A chart should show them; they simply do
        # not get a vote on the strike.
        out = S.build(self.build_sess(), "straddle", anchor="atm_open")
        self.assertEqual(out["rows"][0]["ts"], "09:15")

    def test_the_cutoff_is_disclosed(self):
        out = S.build(self.build_sess(), "straddle", anchor="atm_open")
        self.assertIn("09:20", " ".join(out["notes"]))

    def test_an_explicit_cutoff_is_honoured(self):
        # Asserting the exact strike would be asserting 23400, which the
        # fixture's chain does not carry -- it stops at 23250, so parity
        # picks the highest available. The claim worth testing is that an
        # earlier cutoff anchors on the AUCTION level rather than the
        # session level, whatever strike that lands on.
        default = S.build(self.build_sess(), "straddle", anchor="atm_open")
        early = S.build(self.build_sess(), "straddle", anchor="atm_open",
                        anchor_from="09:15")
        self.assertGreater(early["rows"][0]["legs"][0]["strike"],
                           default["rows"][0]["legs"][0]["strike"],
                           "an explicit early cutoff must still take effect")

    def test_a_session_ending_before_the_cutoff_says_so(self):
        short = session([23100.0] * 3)          # 09:20, 09:21, 09:22
        out = S.build(short, "straddle", anchor="atm_open",
                      anchor_from="14:00")
        self.assertEqual(out["rows"], [])
        self.assertIn("14:00", " ".join(out["notes"]))
