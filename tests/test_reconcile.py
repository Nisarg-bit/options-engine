from reconcile import compare_row


def test_exact_match_has_no_problems():
    ours = {"close": 100.0, "volume": 5000, "oi": 20000}
    assert compare_row(ours, dict(ours)) == []


def test_small_differences_are_tolerated():
    ours = {"close": 100.0, "volume": 5000, "oi": 20000}
    theirs = {"close": 100.2, "volume": 5100, "oi": 20100}
    assert compare_row(ours, theirs) == []


def test_close_mismatch_is_flagged():
    ours = {"close": 110.0, "volume": 5000, "oi": 20000}
    theirs = {"close": 100.0, "volume": 5000, "oi": 20000}
    probs = compare_row(ours, theirs)
    assert len(probs) == 1 and probs[0].startswith("close")


def test_volume_mismatch_is_flagged():
    ours = {"close": 100.0, "volume": 2000, "oi": 20000}
    theirs = {"close": 100.0, "volume": 5000, "oi": 20000}
    assert any(p.startswith("volume") for p in compare_row(ours, theirs))


def test_missing_in_kite_only_matters_if_we_traded():
    traded = {"close": 100.0, "volume": 5000, "oi": 20000}
    assert compare_row(traded, None) == ["missing_in_kite"]

    silent = {"close": 100.0, "volume": 0, "oi": 20000}
    assert compare_row(silent, None) == []