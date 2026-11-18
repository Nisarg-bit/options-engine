"""collector.py: the universe is split over several websockets (Kite caps one)."""
import collector as C


def test_plan_shards_sizes_and_cap():
    s, d = C.plan_shards(range(6144))
    assert [len(x) for x in s] == [3500, 2644] and d == 0
    s, d = C.plan_shards(range(10))
    assert s == [list(range(10))] and d == 0
    s, d = C.plan_shards(range(9000))
    assert len(s) == 3 and d == 0
    s, d = C.plan_shards(range(11000))
    assert len(s) == 3 and d == 11000 - 3 * 3500
    assert C.plan_shards([]) == ([], 0)


class FakeWS:
    MODE_FULL, MODE_QUOTE = "full", "quote"
    made = []

    def __init__(self, key, token):
        self.calls = []
        FakeWS.made.append(self)

    def connect(self, threaded=False):
        self.calls.append(("connect", threaded))

    def subscribe(self, toks):
        self.calls.append(("subscribe", list(toks)))

    def set_mode(self, mode, toks):
        self.calls.append((mode, list(toks)))


class FakeReactor:
    running = True

    def __init__(self):
        self.later = []

    def callFromThread(self, fn, **kw):
        self.later.append(fn.__self__)
        fn(**kw)


def test_open_connections_one_ticker_per_shard_with_modes():
    FakeWS.made = []
    r = FakeReactor()
    logs, ticks, recon = [], [], []
    shards = [[1, 2, 3], [4, 5, 900]]
    conns = C.open_connections("k", "t", shards, {900}, lambda ws, t: ticks.append(t),
                               log=logs.append, ticker_cls=FakeWS, reactor=r,
                               on_reconnect=lambda: recon.append(1))
    assert len(conns) == 2
    assert conns[0].calls[0] == ("connect", True)
    assert r.later == [conns[1]]                    # the second goes through the reactor thread
    conns[1].on_connect(conns[1], {})
    assert ("subscribe", [4, 5, 900]) in conns[1].calls
    assert ("full", [4, 5]) in conns[1].calls and ("quote", [900]) in conns[1].calls
    conns[0].on_connect(conns[0], {})
    assert not any(c[0] == "quote" for c in conns[0].calls)
    conns[1].on_reconnect(conns[1], 2)
    assert recon == [1] and any("connection 2: reconnecting" in l for l in logs)
    conns[0].on_ticks(conns[0], [{"x": 1}])
    assert ticks == [[{"x": 1}]]


def test_stock_futures_go_in_full_mode_for_open_interest():
    FakeWS.made = []
    conns = C.open_connections("k", "t", [[1, 800, 900]], {800, 900}, lambda ws, t: None,
                               log=lambda *_: None, ticker_cls=FakeWS, reactor=FakeReactor(),
                               full_set={900})
    conns[0].on_connect(conns[0], {})
    assert ("full", [1, 900]) in conns[0].calls and ("quote", [800]) in conns[0].calls
