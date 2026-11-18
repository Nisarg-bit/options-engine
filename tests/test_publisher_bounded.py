"""publisher.bounded: a slow node is skipped, never freezes the loop."""
import threading
import time

import publisher as PUB


def test_bounded_returns_result_or_skips(monkeypatch):
    assert PUB.bounded("t1", lambda: {"ok": 1}, secs=2) == {"ok": 1}
    gate = threading.Event()
    t0 = time.time()
    assert PUB.bounded("t2", lambda: gate.wait(5) or {"late": 1}, secs=0.2) is None
    assert time.time() - t0 < 2
    assert PUB.bounded("t2", lambda: {"x": 1}, secs=0.2) is None      # previous still running
    gate.set()
    PUB._bounded["t2"].join(2)
    assert PUB.bounded("t2", lambda: {"x": 1}, secs=2) == {"x": 1}
    assert "error" in PUB.bounded("t3", lambda: 1 / 0, secs=2)


def test_every_node_builds_without_a_name_error(monkeypatch):
    """The 11 Nov deploy shipped `import project_paths` inside the docstring: the
    postmortem node raised NameError and cut every node after it out of the cycle."""
    import project_paths
    assert PUB.project_paths is project_paths
    monkeypatch.setattr(project_paths, "ROOT", "/nonexistent-root")
    assert PUB.build("postmortem") is None                     # no file: skipped, not crashed


def test_prune_cache_drops_only_old_entries():
    c = {"old": (100.0, "frame"), "new": (950.0, "frame"), "odd": "not a tuple"}
    assert PUB.prune_cache(c, older_than=600, now=1000.0) == 1
    assert set(c) == {"new", "odd"}


def test_release_memory_never_raises(monkeypatch):
    PUB._last_trim["at"] = 0
    PUB.release_memory()                    # real libc on Linux, no-op elsewhere
    PUB.limit_arenas()


def test_cycle_logs_node_timings(monkeypatch, capsys):
    import publisher as P
    monkeypatch.setattr(P, "CADENCE", {"a": 1, "b": 1})
    monkeypatch.setattr(P, "build", lambda node: {"node": node})
    monkeypatch.setattr(P, "changed", lambda node, blob: True)
    monkeypatch.setattr(P, "archive_pass", lambda dry=False: "")
    monkeypatch.setattr(P, "market_open", lambda: True)
    state = {}
    P.cycle(state, dry=True)
    out = capsys.readouterr().out
    assert "first pass: a took" in out and "first pass: b took" in out and "first pass: total" in out
    state.update(a=0, b=0, _archive=0)
    P.cycle(state, dry=True)                       # quiet after the first pass
    assert "first pass" not in capsys.readouterr().out
    monkeypatch.setattr(P, "SLOW_NODE_S", -1.0)    # everything counts as slow
    state.update(a=0, b=0)
    P.cycle(state, dry=True)
    assert "slow node: a took" in capsys.readouterr().out
