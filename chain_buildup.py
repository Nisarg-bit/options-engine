"""Per-strike positioning: who is building, who is unwinding, ranked.

WHAT A BUILDUP READ IS

Price and open interest move together in four combinations, and each says
something different about who is doing the trading:

    price up,   OI up      new money is paying up            long buildup
    price down, OI up      new money is pressing the other   short buildup
                           way
    price up,   OI down    positions closing into strength   short covering
    price down, OI down    positions closing into weakness   long unwinding

OI is the part that carries the information. Price alone cannot tell a new
position from an old one being closed; open interest can, because it counts
contracts outstanding rather than contracts traded.

WHAT THIS MEASURES IT AGAINST

The change from the session's opening minute to the latest one. That is the
day's own story and needs no yesterday.

The other common convention -- comparing to the PREVIOUS day's close -- is
what a trader usually means by "day buildup", and it is not used here
because it would silently change meaning on the days it matters most. After
a gap, the opening quote already reflects the new level, so the
close-to-now change mixes the gap into the intraday story. The two
definitions agree on a quiet day and diverge exactly when something has
happened, which is the wrong time for a chart to become ambiguous.

WHY THE NOISE FLOOR MATTERS MORE THAN THE CLASSIFIER

Four buckets over two signs will classify anything, including a strike whose
price moved 0.1% on nine contracts. Labelling that "long buildup" is how a
dashboard starts telling stories about nothing, and a reader who acts on
those stories is worse off than one with no indicator.

So a strike must clear an absolute OI floor to appear at all, and the
fractional floors decide how confidently it is labelled. The counts of what
was skipped are printed, because a filter nobody can see is a filter nobody
can argue with.

THE ROLL DAY, AND WHY A CHAIN-WIDE "BUILDING" READING IS NOT A BUG

On 2026-09-16 this reported 108 of 142 contracts building, and that looked
like an indicator saying the same thing about everything. It was not. NIFTY
weeklies expire on Tuesday, so the Wednesday after an expiry is the first
full session of a newly-front contract, and last week's positions roll into
it. Measured against the 11:00 open interest, the 09:15 reading was:

    6 DTE (the Wednesday after expiry)    70%, 76%
    5 and 4 DTE                           95%, 96%
    0 DTE (expiry day)                    92%, 94%

The roll is real positioning and the label is correct. But a reader seeing
three quarters of the board building will reach for an explanation, and
"everyone turned bullish" is the wrong one. So when the share is extreme
this prints the DTE and says what day it is, because an unexplained
chain-wide reading is how a correct indicator gets mistrusted.

WHAT IS ACTUALLY WRONG IS NARROWER: A MISSING OPENING READING

    21,350 PE   open interest   0 -> 1,961,570

Zero is not a credible open interest for a contract carrying two million
later the same morning; positions are carried overnight. That is a datum
that did not arrive, and reporting it as "new" -- the most emphatic label
available -- states a certainty the data does not support. It is rare (2 of
111 contracts above the OI floor that day), which is exactly why it must be
handled rather than absorbed: rare and loud is the combination a reader
trusts most.

Such a row is now reported separately as a missing opening reading and kept
out of the ranking, rather than being ranked top by a change that is mostly
an absent number.

BUT "FLAT" IS NOT THE SAME AS "NOTHING HAPPENED"

The first version of this collapsed everything below either floor to flat,
and the first live run showed what that costs:

    23100 PE   price -1.9%   OI +172.6%   -> "flat"
    21350 PE   price +57%    OI 0 -> 1,951,755   -> "-"

The first is a sub-2% price move flattening a row where open interest nearly
tripled. The second is growth from zero open interest -- the clearest new
positioning a chain can produce -- being dropped entirely because (b-a)/a
divides by zero. Both were the most informative rows on the board, and both
said nothing.

Now a large OI move with an unreadable price direction is "building,
unclear" or "closing, unclear", and growth from zero is handled rather than
discarded. Only a quiet OI is flat.

Usage:
    python chain_buildup.py
    python chain_buildup.py --session 2026-09-16 --expiry 2026-09-22
    python chain_buildup.py --top 15 --min-oi 25000
"""

import sys
from datetime import date

import indicators as IND
import series_probe as SP

PX_EPS = 0.02        # 2% price move
OI_EPS = 0.02        # 2% OI move
MIN_OI = 10_000      # a strike nobody holds has nothing to say
MIN_PX = 1.0         # below this a tick is a bigger move than the market
DEFAULT_TOP = 12
NEAR_STRIKES = 5     # half-width of the "near the money" band, in strikes


def pc(v, w):
    """A percentage cell that says which kind of nothing it is.

    None means not computable, `new` means growth from zero and `gone`
    means decay to it. A dash for all three would hide the two cases a
    reader most wants to see.
    """
    if v is None:
        return f"{'-':>{w}}"
    if v == float("inf"):
        return f"{'new':>{w}}"
    if v == float("-inf"):
        return f"{'gone':>{w}}"
    return f"{v:>{w}.1%}"


def strike_rows(by_minute, min_oi=MIN_OI, px_eps=PX_EPS, oi_eps=OI_EPS):
    """One row per (strike, opt_type): open vs latest, classified."""
    times = sorted(by_minute)
    if len(times) < 2:
        return [], {"reason": "need at least two minutes"}

    first, last = by_minute[times[0]], by_minute[times[-1]]
    out = []
    skipped = {"thin": 0, "missing": 0, "flat": 0, "no_open_oi": 0}
    no_open = []

    for k in sorted(set(first) & set(last)):
        for ot in ("CE", "PE"):
            a, b = first[k].get(ot), last[k].get(ot)
            if not a or not b:
                skipped["missing"] += 1
                continue
            px0, px1 = a.get("mid"), b.get("mid")
            oi0, oi1 = a.get("oi"), b.get("oi")
            if not px0 or not px1 or oi1 is None or oi0 is None:
                skipped["missing"] += 1
                continue
            if oi1 < min_oi:
                skipped["thin"] += 1
                continue

            # A zero opening reading on a contract that carries real open
            # interest later is a datum that did not arrive, not a position
            # that did not exist -- open interest is carried overnight. It
            # would otherwise rank first on a change that is mostly an
            # absent number, labelled "new", which is the loudest thing this
            # table can say. Report it separately and keep it out of the
            # ranking.
            if oi0 == 0:
                skipped["no_open_oi"] += 1
                no_open.append({"strike": k, "opt_type": ot, "oi_last": oi1})
                continue

            d_px = IND.frac_change(px0, px1)
            d_oi = IND.frac_change(oi0, oi1)
            label = IND.buildup(d_px, d_oi, px_eps=px_eps, oi_eps=oi_eps)
            if label == "flat":
                skipped["flat"] += 1
            out.append({"strike": k, "opt_type": ot,
                        "px_open": px0, "px_last": px1, "d_px": d_px,
                        "oi_open": oi0, "oi_last": oi1, "d_oi": d_oi,
                        "oi_chg": oi1 - oi0, "label": label})
    skipped["no_open_rows"] = no_open
    return out, skipped


def parity_forward(chain, n=5):
    """Model-free forward: median of K + C - P over the n most ATM strikes.

    Parity makes C - P = F - K exactly, so every two-sided strike implies a
    forward. The median over the strikes nearest the money is taken because
    the wings are wide and a single bad quote there would move a mean.
    """
    imp = []
    for k, legs in chain.items():
        try:
            imp.append((abs(legs["CE"]["mid"] - legs["PE"]["mid"]),
                        k + legs["CE"]["mid"] - legs["PE"]["mid"]))
        except (KeyError, TypeError):
            continue
    if not imp:
        return None
    imp.sort()
    near = sorted(f for _, f in imp[:n])
    m = len(near)
    return near[m // 2] if m % 2 else 0.5 * (near[m // 2 - 1] + near[m // 2])


def _interp(curve, x):
    """Interpolate a sorted [(strike, price)] curve, in LOG price.

    Returns None outside the range rather than extrapolating: an option
    curve is steep in the wings and extrapolating it would invent prices
    exactly where the chain has stopped knowing any.

    The log matters. An out-of-the-money option price falls away roughly
    exponentially with distance from the money, so the curve is strongly
    convex, and straight-line interpolation across a convex function always
    overshoots. On a 50-point grid that showed up as a systematic -1% to -3%
    bias in every residual -- absorbed by the median, but leaving a 2%
    curvature artefact the same size as the classification floor it feeds.
    Interpolating the logarithm turns the dominant exponential decay into
    something nearly straight and takes the error to a fraction of that.
    """
    if len(curve) < 2 or x < curve[0][0] or x > curve[-1][0]:
        return None
    import math
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            if y0 > 0 and y1 > 0:
                return math.exp(math.log(y0) + t * (math.log(y1) - math.log(y0)))
            return y0 + t * (y1 - y0)
    return None


def otm_rows(by_minute, min_oi=MIN_OI, px_eps=PX_EPS, oi_eps=OI_EPS):
    """Per-strike positioning with the forward move taken out of the price.

    WHY NOT THE PER-LEG PRICE

    Parity says C - P = F - K exactly, so the difference between a call's
    move and a put's move at one strike is the forward's move and nothing
    else. Near the money that swamps everything, which is why the per-leg
    quadrants degenerate into a restatement of the index.

    WHY NOT THE STRADDLE EITHER

    The version before this one used C + P, on the reasoning that the sum
    escapes what the difference is made of. It does not. Rearranged,

        Straddle(K) = 2*P(K) + F - K

    so a strike straddle has delta 1 + 2*delta_P(K). That is zero at the
    money and only there: for a low strike delta_P -> 0 and the straddle's
    delta -> +1, for a high strike delta_P -> -1 and it -> -1. Live data
    showed it within minutes, as a residual sloping monotonically with
    strike and crossing zero exactly at the money -- delta's signature.

    A second flaw came with it. Most strikes on a wide chain are deep in the
    wings, where a straddle is nearly all intrinsic value, so a real vol move
    is a negligible PERCENTAGE of it. A median across such a chain is set by
    the strikes least able to move.

    WHAT IS USED

    The out-of-the-money leg: the put below the forward, the call above. It
    is the straddle's time value with the intrinsic removed by construction,
    it is the quantity VIX is built from, and it keeps the call-versus-put
    distinction that reads as support and resistance. The leg is chosen once,
    from the OPENING forward, and held at both ends so the same contract is
    compared with itself.

    HOW THE FORWARD IS REMOVED

    The chain's own shape supplies it. If only the forward moved and the
    smile travelled with it, a strike should now be worth what the strike
    dF lower was worth at the open. So the expected price is the opening
    curve read at (K - dF), with dF from parity at both ends.

    That needs no model, no implied-vol fit and no greeks. It does assume
    the smile is sticky in MONEYNESS rather than in strike -- an assumption,
    explicitly, not an identity, and the honest cost of the correction.
    Strikes whose shifted point falls outside the opening range are dropped
    rather than extrapolated. A uniform bleed survives it, so the median
    residual is removed as well and reported as its own number.
    """
    times = sorted(by_minute)
    if len(times) < 2:
        return [], None, {"reason": "need at least two minutes"}

    first, last = by_minute[times[0]], by_minute[times[-1]]
    f0, f1 = parity_forward(first), parity_forward(last)
    raw, skipped = [], {"missing": 0, "thin": 0, "off_curve": 0,
                        "crossed": 0, "penny": 0}

    def side(k):
        """Which leg is out of the money, decided once at the open."""
        if f0 is None:
            return "PE"
        return "PE" if k <= f0 else "CE"

    def crosses(k):
        """True when the forward moved through this strike during the day.

        The leg is fixed at the open so the same contract is compared with
        itself. But a strike the forward has since crossed has changed
        character underneath that choice -- it was out of the money at 09:15
        and is in the money now -- so the comparison is an OTM price against
        an ITM one. It showed up in test as a single +17% row in an
        otherwise clean chain. There is no honest fix, so such a strike is
        dropped and counted.
        """
        if f0 is None or f1 is None:
            return False
        return (k - f0) * (k - f1) < 0

    def px(chain, k):
        try:
            return chain[k][side(k)]["mid"]
        except (KeyError, TypeError):
            return None

    # The OTM curve is continuous: at K = F the put and the call are equal,
    # so the two halves meet and interpolating across the peak is sound.
    open_curve = sorted((k, v) for k in first if (v := px(first, k)))

    for k in sorted(set(first) & set(last)):
        if crosses(k):
            skipped["crossed"] += 1
            continue
        ot = side(k)
        p0, p1 = px(first, k), px(last, k)
        try:
            oi0 = first[k][ot]["oi"]
            oi1 = last[k][ot]["oi"]
        except (KeyError, TypeError):
            skipped["missing"] += 1
            continue
        if not p0 or not p1 or oi0 is None or oi1 is None:
            skipped["missing"] += 1
            continue
        # A option quoted at a few paise moves 50% on one tick. Percentages
        # there are quantisation, not information.
        if p0 < MIN_PX or p1 < MIN_PX:
            skipped["penny"] += 1
            continue
        if oi1 < min_oi:
            skipped["thin"] += 1
            continue
        if oi0 == 0:
            skipped["missing"] += 1
            continue

        expect = p0
        if f0 is not None and f1 is not None:
            expect = _interp(open_curve, k - (f1 - f0))
            if not expect:
                skipped["off_curve"] += 1
                continue

        raw.append({"strike": k, "opt_type": ot,
                    "px_open": p0, "px_last": p1, "px_expect": expect,
                    "d_px": IND.frac_change(p0, p1),
                    "d_adj": IND.frac_change(expect, p1),
                    "oi_open": oi0, "oi_last": oi1,
                    "d_oi": IND.frac_change(oi0, oi1),
                    "oi_chg": oi1 - oi0})

    ds = sorted(r["d_adj"] for r in raw
                if r["d_adj"] is not None and abs(r["d_adj"]) != float("inf"))
    if not ds:
        return [], None, skipped
    n = len(ds)
    med = ds[n // 2] if n % 2 else 0.5 * (ds[n // 2 - 1] + ds[n // 2])

    # One tested classifier, not a second copy of the same four branches.
    # The names change because the axis changed: this is volatility being
    # put on or taken off at a strike, not a directional position.
    RENAME = {"long buildup": "vol bought",
              "short buildup": "vol sold",
              "short covering": "vol shorts covering",
              "long unwinding": "vol longs unwinding"}
    for r in raw:
        r["resid"] = None if r["d_adj"] is None else r["d_adj"] - med
        lab = IND.buildup(r["resid"], r["d_oi"], px_eps=px_eps, oi_eps=oi_eps)
        r["label"] = RENAME.get(lab, lab)
    raw.sort(key=lambda r: r["strike"])
    return raw, med, {**skipped, "fwd_open": f0, "fwd_last": f1}


def main():
    args = {"--session": None, "--expiry": None,
            "--top": str(DEFAULT_TOP), "--min-oi": str(MIN_OI)}
    for i, a in enumerate(sys.argv):
        if a in args and i + 1 < len(sys.argv):
            args[a] = sys.argv[i + 1]

    days = SP.sessions()
    if not days:
        print(f"no sessions under {SP.BARS}")
        return 1
    day = date.fromisoformat(args["--session"]) if args["--session"] else days[-1]

    exps = [e for e in SP.expiries_on(day, SP.UNDERLYING) if e >= day]
    if not exps:
        print(f"no expiries on {day}")
        return 1
    expiry = date.fromisoformat(args["--expiry"]) if args["--expiry"] else exps[0]

    bm, err = SP.by_minute(day, SP.UNDERLYING, expiry)
    if err:
        print(f"adapter failed: {err}")
        return 1
    times = sorted(bm)

    print(f"CHAIN BUILDUP   {day}   expiry {expiry}   "
          f"({(expiry - day).days} DTE)")
    print(f"open {times[0]} -> latest {times[-1]}   "
          f"({len(times)} minutes)\n")

    rows, skipped = strike_rows(bm, min_oi=float(args["--min-oi"]))
    if not rows:
        print(f"nothing to classify: {skipped}")
        return 1

    atm = None
    a = bm[times[-1]]
    from series import atm_by_parity
    atm = atm_by_parity(a)

    top = int(args["--top"])
    ranked = sorted(rows, key=lambda r: -abs(r["oi_chg"]))[:top]

    # The percentage columns are the point of the table, not decoration.
    # Without them a reader cannot tell a row called "unclear" because the
    # price drifted 0.1% from one that drifted 1.9% and only just missed the
    # floor -- and those are very different observations. A label you cannot
    # audit is a label you have to take on trust.
    hdr = (f"{'strike':>8} {'':2}  {'price':>16}{'d%':>8}  "
           f"{'OI change':>22}{'d%':>9}  {'reading':<18}")
    print(f"RANKED BY OI CHANGE  (top {top} of {len(rows)} contracts)\n")
    print(hdr)
    print("-" * len(hdr))
    for r in ranked:
        mark = " *" if atm is not None and r["strike"] == atm else "  "
        px = f"{r['px_open']:>7.2f}->{r['px_last']:<7.2f}"
        oi = f"{r['oi_open']:>10,.0f}->{r['oi_last']:<10,.0f}"

        print(f"{r['strike']:>8,.0f} {r['opt_type']}{mark} {px}"
              f"{pc(r['d_px'], 8)}  {oi}{pc(r['d_oi'], 9)}  "
              f"{r['label'] or '-':<18}")

    print()
    counts = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    print("ACROSS THE WHOLE CHAIN")
    for label in ("long buildup", "short buildup", "short covering",
                  "long unwinding", "building, unclear", "closing, unclear",
                  "flat"):
        n = counts.get(label, 0)
        if n:
            print(f"  {label:<16}{n:>5}")
    print()
    # A chain-wide reading is usually a calendar effect, not a change of
    # mind. Naming the DTE next to the share is what turns "everything is
    # building" from an alarming number into a legible one. The 60% line is
    # a disclosure threshold, not a finding -- it decides when to explain,
    # never what to label.
    building = sum(n for lab, n in counts.items()
                   if lab and lab.startswith(("long buildup", "short buildup",
                                              "building")))
    dte = (expiry - day).days
    if rows and building / len(rows) >= 0.60:
        print(f"  {building} of {len(rows)} contracts are building "
              f"({building / len(rows):.0%}) at {dte} DTE.")
        if dte >= 6:
            print(f"  Before reading that as sentiment: NIFTY weeklies expire")
            print(f"  Tuesday, so at {dte} DTE this contract became the front")
            print(f"  weekly yesterday and last week's positions are rolling")
            print(f"  into it. Measured against 11:00 open interest, the 09:15")
            print(f"  reading is ~70-76% on a roll day and ~95% two days later.")
            print(f"  A chain-wide buildup here is the roll, not a change of")
            print(f"  opinion.")
        else:
            print(f"  This is NOT a roll day ({dte} DTE), where a chain-wide")
            print(f"  buildup has a calendar explanation. At this DTE the")
            print(f"  opening open interest is normally ~95% of its 11:00")
            print(f"  value, so a board-wide build is unusual and worth")
            print(f"  looking at rather than assuming.")
        print()

    # THE DEGENERACY CHECK.
    #
    # An option's price moves mostly with delta, so on a day the index
    # trends, every call gains and every put loses whatever anyone is
    # doing. If open interest is also rising board-wide -- a roll day, say
    # -- then price-up-OI-up labels every call "long buildup" and
    # price-down-OI-up labels every put "short buildup", and the four
    # quadrants collapse into a restatement of the spot move.
    #
    # This measures exactly that and nothing more: what share of calls rose
    # and what share of puts fell. Near 100% on both means the price axis
    # carried the index, not positioning, and the labels should not be read
    # as disagreement between two camps.
    #
    # This is a limitation of the standard buildup indicator, not of this
    # implementation -- the same chart on any retail platform degenerates
    # the same way on a trending day. Naming it is the only honest fix
    # available without modelling delta out, which this does not do.
    # IT MUST BE MEASURED NEAR THE MONEY, AND THAT IS NOT A DETAIL.
    #
    # The first version of this check ran over every contract and reported
    # 34% of calls rising and 54% of puts falling -- no lockstep at all --
    # on a session whose top twelve rows were 3 of 3 calls up and 8 of 9
    # puts down. Both numbers were right. Near the money delta is large and
    # the index move sets the price; out in the wings delta is small, decay
    # and vol drift dominate, and calls and puts fall together. A whole-board
    # average is swamped by the hundred-odd far strikes nobody reads, and it
    # reported "no degeneracy" about a table that was almost entirely
    # degenerate at the top.
    #
    # So both are printed. The near band is the one that governs how the
    # ranked table should be read, because ranking by OI change sorts
    # near-money strikes to the top by construction.
    def agree(rs):
        c = [r for r in rs if r["opt_type"] == "CE" and r["d_px"] is not None]
        p = [r for r in rs if r["opt_type"] == "PE" and r["d_px"] is not None]
        if not c or not p:
            return None
        return (sum(1 for r in c if r["d_px"] > 0) / len(c),
                sum(1 for r in p if r["d_px"] < 0) / len(p), len(c) + len(p))

    band = None
    if atm is not None:
        from series import infer_step
        step = infer_step(sorted({r["strike"] for r in rows})) or 50.0
        band = [r for r in rows if abs(r["strike"] - atm) <= NEAR_STRIKES * step]

    whole, near = agree(rows), agree(band) if band else None
    if whole:
        print("  DIRECTIONAL AGREEMENT  (share of calls that rose, "
              "puts that fell)")
        if near:
            print(f"    within {NEAR_STRIKES} strikes of the money"
                  f"   {near[0]:>5.0%} / {near[1]:>5.0%}   "
                  f"({near[2]} contracts)")
        print(f"    whole chain"
              f"{'':<21}{whole[0]:>5.0%} / {whole[1]:>5.0%}   "
              f"({whole[2]} contracts)")
        judge = near or whole
        where = "near the money" if near else "across the chain"
        if min(judge[0], judge[1]) >= 0.85:
            print(f"  Near unanimous {where}, which is what a delta-driven")
            print(f"  index move looks like: every call gains and every put")
            print(f"  loses whoever is trading. The long/short split above is")
            print(f"  then mostly CE versus PE -- the spot direction wearing")
            print(f"  four labels, not two camps positioning against each")
            print(f"  other. Read the OI magnitudes, not the quadrants.")
        else:
            print(f"  Calls and puts are not moving in lockstep {where}, so")
            print(f"  the price axis carries something beyond the index move")
            print(f"  and the quadrants are worth reading as positioning.")
        if near and whole and abs(near[0] - whole[0]) >= 0.25:
            print(f"  The two bands disagree, which is expected: delta rules")
            print(f"  the price near the money and decay rules it in the")
            print(f"  wings. The near figure is the one that governs the")
            print(f"  ranked table, because ranking by OI change puts")
            print(f"  near-money strikes at the top.")
        print()

    if skipped.get("no_open_oi"):
        print(f"  {skipped['no_open_oi']} contract(s) had NO opening open-interest "
              f"reading and are excluded:")
        for r in skipped.get("no_open_rows", [])[:6]:
            print(f"      {r['strike']:>8,.0f} {r['opt_type']}   "
                  f"0 at {times[0]} -> {r['oi_last']:,.0f} now")
        print(f"  Zero is not a credible open interest for a contract "
              f"carrying that many")
        print(f"  contracts later the same session -- open interest is "
              f"carried overnight.")
        print(f"  This is a missing datum, so it is named rather than "
              f"ranked as 'new'.")
        print()

    print(f"  skipped: {skipped['thin']} below the {float(args['--min-oi']):,.0f} "
          f"OI floor, {skipped['missing']} not two-sided at both ends")
    print(f"  floors: {PX_EPS:.0%} on price, {OI_EPS:.0%} on OI. A big OI "
          f"move with too small a price move to")
    print(f"  read a direction from is 'unclear', not 'flat' -- something "
          f"happened, the side is ambiguous.")
    if atm is not None:
        print(f"  * marks the current parity ATM, {atm:,.0f}")
    print()
    print("  A buildup label is a description of what happened, not a")
    print("  forecast. 'Short buildup' at a strike means sellers were")
    print("  active there today -- it does not mean the level holds.")

    srows, med, sskip = otm_rows(bm, min_oi=float(args["--min-oi"]))
    if not srows:
        print()
        print(f"  (no OTM view: {sskip})")
        return 0

    print()
    print("=" * 78)
    print("OTM VIEW -- THE SAME CHAIN WITH THE FORWARD MOVE REMOVED")
    f0, f1 = sskip.get("fwd_open"), sskip.get("fwd_last")
    print()
    if f0 is not None and f1 is not None:
        print(f"  Parity forward: {f0:,.1f} -> {f1:,.1f}  ({f1 - f0:+.1f})")
        print(f"  The instrument is the out-of-the-money leg -- put below the")
        print(f"  forward, call above -- which is time value with no intrinsic")
        print(f"  in it. Each strike is compared with what the OPENING curve")
        print(f"  was worth {f1 - f0:+.1f} points lower, which takes out delta")
        print(f"  and skew together. That assumes the smile travels with the")
        print(f"  forward: an assumption, not an identity.")
    else:
        print(f"  No parity forward available; residuals are raw straddle")
        print(f"  changes and DO still contain the index move.")
    print(f"  Common residual after that, removed from every row: {med:+.1%}")
    print(f"  (time decay plus any board-wide shift in volatility)")
    print()

    sranked = sorted(srows, key=lambda r: -abs(r["oi_chg"]))[:top]
    hdr2 = (f"{'strike':>8} {'':2}  {'OTM price':>16}{'raw':>8}{'vs fwd':>9}"
            f"{'resid':>9}  {'OI change':>22}{'d%':>9}  {'reading':<20}")
    print(f"RANKED BY TOTAL OI CHANGE  (top {top} of {len(srows)} strikes)\n")
    print(hdr2)
    print("-" * len(hdr2))
    for r in sranked:
        mark = " *" if atm is not None and r["strike"] == atm else "  "
        st = f"{r['px_open']:>7.2f}->{r['px_last']:<7.2f}"
        oi = f"{r['oi_open']:>10,.0f}->{r['oi_last']:<10,.0f}"
        print(f"{r['strike']:>8,.0f} {r['opt_type']}{mark} {st}"
              f"{pc(r['d_px'], 8)}{pc(r['d_adj'], 9)}{pc(r['resid'], 9)}  {oi}"
              f"{pc(r['d_oi'], 9)}  {r['label'] or '-':<20}")

    # THE TEST THAT CAUGHT THE LAST MISTAKE, NOW RUN AUTOMATICALLY.
    #
    # Leftover delta has a signature no vol story shares: the residual falls
    # monotonically with strike and crosses zero at the money. Comparing the
    # low third of strikes with the high third detects that in one number,
    # so the next time this view is contaminated it says so itself instead
    # of waiting for someone to notice the column is sorted.
    ordered = sorted((r for r in srows if r["resid"] is not None),
                     key=lambda r: r["strike"])
    if len(ordered) >= 9:
        third = len(ordered) // 3
        def medr(rs):
            v = sorted(r["resid"] for r in rs)
            m = len(v)
            return v[m // 2] if m % 2 else 0.5 * (v[m // 2 - 1] + v[m // 2])
        lo, hi = medr(ordered[:third]), medr(ordered[-third:])
        print()
        print(f"  TILT ACROSS STRIKES: low third {lo:+.1%}, "
              f"high third {hi:+.1%}")
        if abs(lo - hi) >= 0.02:
            print(f"  These differ by {abs(lo - hi):.1%}, which is the shape")
            print(f"  leftover delta makes -- residual sloping with strike.")
            print(f"  Read it as a skew move only after satisfying yourself")
            print(f"  it is not the forward adjustment falling short.")
        else:
            print(f"  Flat across strikes, so the forward adjustment has done")
            print(f"  its job and what remains is strike-specific.")
        print()
        print(f"  Residuals are percentages of price, and a board-wide vol")
        print(f"  move is not a board-wide percentage move -- the wings are")
        print(f"  far more vol-sensitive than the money. So a hump or dip")
        print(f"  shaped by moneyness may be arithmetic rather than")
        print(f"  positioning. A single strike standing out from its")
        print(f"  neighbours is the reading this view supports best.")

    print()
    scounts = {}
    for r in srows:
        scounts[r["label"]] = scounts.get(r["label"], 0) + 1
    print("ACROSS THE WHOLE CHAIN")
    for label in ("vol bought", "vol sold", "vol shorts covering",
                  "vol longs unwinding", "building, unclear",
                  "closing, unclear", "flat"):
        if scounts.get(label):
            print(f"  {label:<22}{scounts[label]:>5}")

    print()
    print("  The per-leg table's price axis is the index move near the money,")
    print("  by parity. This one subtracts that move explicitly, using the")
    print("  chain's own opening curve, and prices the out-of-the-money leg")
    print("  so intrinsic value cannot swamp the percentage. What it cannot")
    print("  do is prove the smile travelled with the forward -- that is the")
    print("  assumption, and the tilt line above is what checks it. Neither")
    print("  table supersedes the other: where they disagree is where the")
    print("  per-leg one was reading the index.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
