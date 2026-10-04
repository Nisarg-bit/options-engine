"""Chart indicators, defined precisely and tested against their definitions.

WHY THIS IS ITS OWN FILE

These are small functions that everyone assumes they know. That assumption is
where the errors live -- RSI in particular has two incompatible smoothings in
common use, and the wrong one produces a line that looks plausible, moves the
right way, and is quietly a different indicator from the one every other
chart is showing.

So each function here states which definition it implements, and the tests
check the definition rather than checking that the code agrees with itself.

WHAT EACH ONE IS

  vwap      Cumulative, session-anchored: sum(price*volume)/sum(volume) from
            the session open to each bar. NOT a rolling window. This is the
            VWAP a trader means, and it is why the collector stores `pv`
            (price times volume, accumulated per bar) rather than making the
            chart multiply a bar's average price by its volume afterwards.

  sma       Simple moving average over the last n values.

  ema       Exponential, alpha = 2/(n+1), seeded with the SMA of the first n
            values. Seeding with the first value instead is also common and
            gives a visibly different line for the first few dozen bars.

  rsi       WILDER's smoothing, which is what Wilder defined in 1978 and what
            TradingView, Zerodha and most platforms draw. The first average
            is the simple mean of the first n changes; every subsequent one
            is (prev*(n-1) + current)/n. Using a plain n-period mean instead
            -- "Cutler's RSI" -- is a different and more jagged indicator.

All of them return a list the same length as the input, padded at the front
with None where the indicator is not yet defined. A caller that wants to
plot alignment gets it for free; a caller that forgets to handle None finds
out immediately rather than silently plotting a shifted series.
"""


def _num(v):
    try:
        f = float(v)
        return f if f == f else None          # NaN check without math import
    except (TypeError, ValueError):
        return None


def vwap(prices, volumes):
    """Session-anchored cumulative VWAP.

    Bars with no volume do not move the average -- they contribute nothing to
    either sum -- but they still carry the running value forward, which is
    what a chart needs. Before any volume has traded the VWAP is undefined
    rather than zero, because zero would draw a line at the bottom of the
    axis and look like a price.
    """
    out, pv, vol = [], 0.0, 0.0
    for p, v in zip(prices, volumes):
        p, v = _num(p), _num(v)
        if p is not None and v is not None and v > 0:
            pv += p * v
            vol += v
        out.append(pv / vol if vol > 0 else None)
    return out


def sma(values, n):
    """Simple moving average of the last n values."""
    if n <= 0:
        raise ValueError("sma needs a positive window")
    out, run, buf = [], 0.0, []
    for v in values:
        x = _num(v)
        if x is None:
            # A gap must not silently shorten the window. Restart cleanly.
            out.append(None)
            run, buf = 0.0, []
            continue
        buf.append(x)
        run += x
        if len(buf) > n:
            run -= buf.pop(0)
        out.append(run / n if len(buf) == n else None)
    return out


def ema(values, n):
    """Exponential moving average, alpha = 2/(n+1), seeded with the SMA."""
    if n <= 0:
        raise ValueError("ema needs a positive window")
    alpha = 2.0 / (n + 1.0)
    out, seed, prev = [], [], None
    for v in values:
        x = _num(v)
        if x is None:
            out.append(prev)
            continue
        if prev is None:
            seed.append(x)
            if len(seed) < n:
                out.append(None)
                continue
            prev = sum(seed) / n
        else:
            prev = alpha * x + (1.0 - alpha) * prev
        out.append(prev)
    return out


def rsi(values, n=14):
    """Relative Strength Index, Wilder's smoothing.

        RS  = average gain / average loss
        RSI = 100 - 100/(1 + RS)

    The first averages are the simple means of the first n changes; after
    that each is (prev*(n-1) + current)/n. An all-gain stretch gives 100 and
    an all-loss stretch gives 0, which is why average loss is tested for zero
    before dividing rather than after.
    """
    if n <= 0:
        raise ValueError("rsi needs a positive window")
    nums = [_num(v) for v in values]
    out = [None] * len(nums)
    gains, losses = [], []
    avg_g = avg_l = None

    for i in range(1, len(nums)):
        a, b = nums[i - 1], nums[i]
        if a is None or b is None:
            continue
        ch = b - a
        g, l = max(ch, 0.0), max(-ch, 0.0)

        if avg_g is None:
            gains.append(g)
            losses.append(l)
            if len(gains) < n:
                continue
            avg_g = sum(gains) / n
            avg_l = sum(losses) / n
        else:
            avg_g = (avg_g * (n - 1) + g) / n
            avg_l = (avg_l * (n - 1) + l) / n

        if avg_l == 0:
            out[i] = 100.0 if avg_g > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def buildup(d_px, d_oi, px_eps=0.005, oi_eps=0.005):
    """What a change in price and open interest says about positioning.

        price up,   OI up      new longs paying up        long buildup
        price down, OI up      new shorts pressing        short buildup
        price up,   OI down    shorts covering            short covering
        price down, OI down    longs giving up            long unwinding

    THE FLOORS, AND WHY THEY ARE NOT A SINGLE "FLAT"

    The first version collapsed every sub-threshold case to "flat". Run on a
    live chain it labelled this row flat:

        23100 PE   price -1.9%   OI +172.6%

    OI nearly tripling is the most informative thing on the chain, and
    "flat" is the one word that says nothing happened. The price floor
    exists because the DIRECTION is ambiguous when price barely moves -- you
    genuinely cannot tell a long buildup from a short one -- but that is a
    statement about direction, not about whether anything occurred.

    So there are three outcomes below the floors, not one:

        both small                          flat
        OI up, price direction unclear      building, unclear
        OI down, price direction unclear    closing, unclear

    The thresholds are FRACTIONAL changes, not absolute, so the same rule
    works on a 4-rupee option and a 400-rupee one.
    """
    d_px, d_oi = _num(d_px), _num(d_oi)
    if d_px is None or d_oi is None:
        return None
    if abs(d_oi) < oi_eps:
        return "flat"
    if abs(d_px) < px_eps:
        # Something is being put on or taken off, but the price move is too
        # small to say which side is doing it. Reporting the ambiguity is
        # more useful than discarding the observation.
        return "building, unclear" if d_oi > 0 else "closing, unclear"
    if d_px > 0 and d_oi > 0:
        return "long buildup"
    if d_px < 0 and d_oi > 0:
        return "short buildup"
    if d_px > 0 and d_oi < 0:
        return "short covering"
    return "long unwinding"


def frac_change(a, b):
    """(b - a)/a, with the zero-denominator case handled rather than dropped.

    A strike whose open interest goes from 0 to 1,951,755 is the clearest
    new positioning a chain can show, and the naive guard -- `x if a else
    None` -- reports exactly that row as unknown. Growth from nothing is
    unbounded, so it is returned as a large positive number rather than a
    division error or a silent None.
    """
    a, b = _num(a), _num(b)
    if a is None or b is None:
        return None
    if a == 0:
        if b == 0:
            return 0.0
        return float("inf") if b > 0 else float("-inf")
    return (b - a) / a
