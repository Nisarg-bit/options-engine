"""Champion / challenger governance for the stock-futures paper system.

Pure functions (no I/O except load_policy) so every rule is unit-tested:

  in_search_space(champion, candidate)  only approved paths changed, within bounds;
                                        one lot / limits / costs untouchable
  drift(history, today)                 robust z-scores of session features
  deterioration(rows, mode)             expectancy interval below zero
  eligible_for_experiments(...)         whether a cycle may run today
  chrono_split(sessions, ...)           train / validation / test with a swing embargo
  assess_promotion(champ, chal, ...)    every promotion requirement, met or not
  assess_rollback(...)                  rollback triggers after a promotion

Every assessment returns {"decision", "requirements": [{"name", "met", "value",
"threshold", "detail"}], "reasons"}: the dashboard shows exactly this.
"""
import copy
import json
import math
import os
import random
import statistics

import sf_metrics as M

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY = os.environ.get("SF_GOVERNANCE") or next(
    (p for p in (os.path.join(HERE, "config", "governance.json"), os.path.join(HERE, "futures_governance.json"))
     if os.path.exists(p)), os.path.join(HERE, "config", "governance.json"))


def load_policy(path=None):
    with open(path or POLICY, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ search space

def _get(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _set(d, path, v):
    ks = path.split(".")
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    d[ks[-1]] = v


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, p))
        else:
            out[p] = v
    return out


IGNORED = {"label", "rules_validated"}       # descriptive; never affect behaviour


def in_search_space(champion, candidate, policy):
    """(ok, problems). The candidate may differ from the champion only on
    search-space paths, within bounds; everything else must be identical."""
    space = policy["search_space"]["params"]
    a, b = _flatten(champion), _flatten(candidate)
    problems = []
    for p in sorted(set(a) | set(b)):
        if p in IGNORED or p.endswith(".plan_version"):
            continue
        if a.get(p) == b.get(p):
            continue
        rule = space.get(p)
        if rule is None:
            problems.append(f"{p}: not in the approved search space ({a.get(p)!r} -> {b.get(p)!r})")
            continue
        v = b.get(p)
        if "choices" in rule:
            if v not in rule["choices"]:
                problems.append(f"{p}: {v!r} not in {rule['choices']}")
        elif not isinstance(v, (int, float)) or not (rule["min"] <= v <= rule["max"]):
            problems.append(f"{p}: {v!r} outside [{rule['min']}, {rule['max']}]")
    return not problems, problems


def neighbours(champion, policy, limit=None):
    """One-parameter moves from the champion, in a fixed order: the candidate
    generator. Each is (path, new_value, config)."""
    out = []
    for path, rule in sorted(policy["search_space"]["params"].items()):
        cur = _get(champion, path)
        if cur is None:
            continue
        if "choices" in rule:
            vals = [v for v in rule["choices"] if v != cur]
        else:
            vals = [round(cur - rule["step"], 10), round(cur + rule["step"], 10)]
            vals = [v for v in vals if rule["min"] <= v <= rule["max"]]
            if isinstance(cur, int) and all(isinstance(rule[k], int) for k in ("min", "max", "step")):
                vals = [int(v) for v in vals]
        for v in vals:
            c = copy.deepcopy(champion)
            _set(c, path, v)
            out.append((path, v, c))
    return out[:limit] if limit else out


# ------------------------------------------------------------------ drift

def robust_z(history, x):
    if x is None or len(history) == 0:
        return None
    med = statistics.median(history)
    mad = statistics.median([abs(h - med) for h in history]) * 1.4826
    if mad == 0:
        return 0.0 if x == med else math.copysign(float("inf"), x - med)
    return (x - med) / mad


def drift(history, today, policy):
    """history: [ {feature: value}, ... ] for prior sessions (oldest first)."""
    dc = policy["drift"]
    hist = history[-dc["trailing_sessions"]:]
    out = {"method": dc["method"], "prior_sessions": len(hist), "features": {}, "flagged": []}
    if len(hist) < dc["min_prior_sessions"]:
        out["status"] = "INSUFFICIENT"
        out["detail"] = f"{len(hist)} prior sessions < {dc['min_prior_sessions']} required"
        return out
    for f in dc["features"]:
        xs = [h[f] for h in hist if h.get(f) is not None]
        z = robust_z(xs, today.get(f)) if len(xs) >= dc["min_prior_sessions"] else None
        out["features"][f] = {"value": today.get(f), "median": statistics.median(xs) if xs else None,
                              "z": None if z is None else (round(z, 2) if math.isfinite(z) else str(z))}
        if z is not None and abs(z) > dc["z_max"]:
            out["flagged"].append(f)
    out["status"] = "DRIFT" if out["flagged"] else "OK"
    return out


def deterioration(rows, mode, policy):
    dc = policy["deterioration"]
    s = [r for r in rows if r.get("mode") == mode and r.get("class") == "settled"]
    s = sorted(s, key=lambda r: r.get("closed_at") or "")[-dc["window_trades"]:]
    out = {"method": dc["method"], "mode": mode, "trades": len(s)}
    if len(s) < dc["min_trades"]:
        return dict(out, status="INSUFFICIENT", detail=f"{len(s)} settled trades < {dc['min_trades']}")
    ci = M.bootstrap_ci([float(r["net_pnl"]) for r in s])
    return dict(out, ci80=ci, status="DETERIORATED" if ci[1] < 0 else "OK")


# ------------------------------------------------------------------ experiments

def eligible_for_experiments(sessions_since_last, settled_by_mode, flags, policy):
    e = policy["experiments"]["eligibility"]
    reasons = []
    if sessions_since_last < e["min_new_sessions_since_last_cycle"]:
        reasons.append(f"{sessions_since_last} new sessions since the last cycle < {e['min_new_sessions_since_last_cycle']}")
    enough = {m: n for m, n in settled_by_mode.items() if n >= e["min_settled_trades_in_mode"]}
    flagged = [f for f in flags if f in e["or_if_flagged"]]
    if not enough and not flagged:
        reasons.append(f"no mode has {e['min_settled_trades_in_mode']}+ settled trades and nothing is flagged "
                       f"({', '.join(f'{m} {n}' for m, n in settled_by_mode.items())})")
    return (not reasons), reasons


def chrono_split(sessions, policy, embargo=0, burned=()):
    """Oldest-first sessions -> (train, validation, test) or (None, reason).
    `embargo` sessions are dropped between windows (Swing: max holding + 1) so
    no trade can be open across a boundary. A session already used as a test
    window by an earlier cycle (`burned`) can never be a test session again."""
    sp = policy["experiments"]["split"]
    s = sorted(sessions)
    if len(s) < sp["min_sessions"]:
        return None, f"{len(s)} sessions < {sp['min_sessions']} required for a chronological split"
    n = len(s) - 2 * embargo                       # the embargo sessions are set aside first
    if n < sp["min_sessions"]:
        return None, f"{len(s)} sessions leave {n} after a {embargo}-session embargo on each boundary " \
                     f"(< {sp['min_sessions']})"
    n_tr, n_va = int(n * sp["train"]), int(n * sp["validation"])
    tr = s[:n_tr]
    va = s[n_tr + embargo:n_tr + embargo + n_va]
    te = s[n_tr + 2 * embargo + n_va:]
    te = [d for d in te if d not in set(burned)]
    if not tr or not va or not te:
        return None, "a window is empty after the embargo and burned test sessions"
    return (tr, va, te), None


def required_level(policy, k):
    """Confidence needed when k candidates were compared in a cycle."""
    return 1 - (1 - policy["promotion"]["ci_level"]) / max(1, k)


# ------------------------------------------------------------------ promotion

def daily_pnl(rows):
    out = {}
    for r in rows:
        if r.get("class") == "settled" and r.get("net_pnl") is not None:
            d = (r.get("closed_at") or "")[:10]
            out[d] = out.get(d, 0.0) + float(r["net_pnl"])
    return out


def paired_diff_ci(champ_rows, chal_rows, sessions, level=0.80, seed=7):
    """Bootstrap interval of the mean daily (challenger - champion) net P&L over
    the SAME sessions (a session with no trade counts as 0)."""
    a, b = daily_pnl(champ_rows), daily_pnl(chal_rows)
    diffs = [b.get(str(d), 0.0) - a.get(str(d), 0.0) for d in sessions]
    if len(diffs) < 2:
        return None, diffs
    rnd, k, n = random.Random(seed), len(diffs), 2000
    means = sorted(sum(rnd.choice(diffs) for _ in range(k)) / k for _ in range(n))
    return [round(means[int((1 - level) / 2 * n)], 2), round(means[int((1 + level) / 2 * n) - 1], 2)], diffs


def _req(name, met, value=None, threshold=None, detail=""):
    return {"name": name, "met": bool(met), "value": value, "threshold": threshold, "detail": detail}


def assess_promotion(champ_rows, chal_rows, shadow_sessions, policy, flags=(), k_compared=1):
    """Decision PROMOTE only if every requirement is met. flags: ledger_fail /
    spec_fail / data_quarantine_majority found for the challenger."""
    pc = policy["promotion"]
    cm, xm = M.metrics(champ_rows), M.metrics(chal_rows)
    reqs = [
        _req("forward shadow sessions", len(shadow_sessions) >= pc["min_shadow_sessions"],
             len(shadow_sessions), pc["min_shadow_sessions"]),
        _req("settled shadow trades", xm["settled"] >= pc["min_shadow_settled_trades"],
             xm["settled"], pc["min_shadow_settled_trades"]),
    ]
    level = required_level(policy, k_compared)
    ci, diffs = paired_diff_ci(champ_rows, chal_rows, shadow_sessions, level)
    reqs.append(_req(f"paired daily P&L difference, {level:.0%} lower bound > 0",
                     ci is not None and ci[0] > pc["paired_daily_diff_lower_bound_gt"], ci, "> 0",
                     f"{len(diffs)} paired sessions"))
    sm = M.metrics(M.stressed(chal_rows, pc["stress_extra_ticks_per_side"]))
    reqs.append(_req(f"net P&L positive with +{pc['stress_extra_ticks_per_side']} tick slippage per side",
                     sm["settled"] > 0 and sm["net_pnl"] > 0, sm["net_pnl"], "> 0"))
    dd_lim = min(pc["max_drawdown_abs_rs"], pc["max_drawdown_vs_champion"] * max(abs(cm["max_drawdown"]), 1.0)) \
        if cm["settled"] else pc["max_drawdown_abs_rs"]
    reqs.append(_req("max drawdown within ceiling", abs(xm["max_drawdown"]) <= dd_lim,
                     xm["max_drawdown"], -round(dd_lim, 2)))
    n_sub = pc["subperiods"]
    chunks = [diffs[i * len(diffs) // n_sub:(i + 1) * len(diffs) // n_sub] for i in range(n_sub)] if diffs else []
    pos = sum(1 for ch in chunks if ch and sum(ch) > 0)
    reqs.append(_req("stable across time", pos >= pc["min_positive_subperiods"], f"{pos}/{n_sub} sub-periods ahead",
                     pc["min_positive_subperiods"]))
    worse = []
    for d in ("LONG", "SHORT"):
        cx = [r for r in chal_rows if r.get("direction") == d]
        cc = [r for r in champ_rows if r.get("direction") == d]
        mx, mc = M.metrics(cx), M.metrics(cc)
        if mx["settled"] >= 5 and mc["settled"] and mx["expectancy"] is not None and mc["expectancy"] is not None:
            floor = mc["expectancy"] - abs(mc["avg_loss"] or 0)
            if mx["expectancy"] < floor:
                worse.append(f"{d} expectancy {mx['expectancy']:.0f} < {floor:.0f}")
    reqs.append(_req("no direction materially worse", not worse, worse or "ok"))
    for f in pc["reject_if"]:
        reqs.append(_req(f"no {f.replace('_', ' ')}", f not in flags, f in flags))
    unmet = [r["name"] for r in reqs if not r["met"]]
    evidence_short = any(r["name"] in ("forward shadow sessions", "settled shadow trades") and not r["met"] for r in reqs)
    hard_fail = any(r["name"].startswith("no ") and not r["met"] for r in reqs)
    if not unmet:
        decision = "PROMOTE"
    elif hard_fail:
        decision = "REJECT"
    elif evidence_short:
        decision = "CONTINUE_SHADOW"
    elif len(shadow_sessions) >= 2 * pc["min_shadow_sessions"]:
        decision = "REJECT"                       # had twice the evidence and still failed
    else:
        decision = "CONTINUE_SHADOW"
    return {"decision": decision, "requirements": reqs, "unmet": unmet, "champion": cm, "challenger": xm,
            "k_compared": k_compared, "level": level, "provisional": pc.get("provisional", False)}


def assess_rollback(champ_rows_since, lkg_rows_since, sessions_since, shadow_max_dd, policy, flags=()):
    rc = policy["rollback"]
    tr = rc["triggers"]
    cm = M.metrics(champ_rows_since)
    fired = []
    dd = abs(cm["max_drawdown"])
    if shadow_max_dd and dd > tr["drawdown_vs_shadow_max"] * abs(shadow_max_dd):
        fired.append(f"drawdown {dd:.0f} > {tr['drawdown_vs_shadow_max']}x shadow max {abs(shadow_max_dd):.0f}")
    if dd > tr["drawdown_abs_rs"]:
        fired.append(f"drawdown {dd:.0f} > Rs {tr['drawdown_abs_rs']}")
    if len(sessions_since) >= tr["min_sessions_for_comparison"]:
        ci, _ = paired_diff_ci(champ_rows_since, lkg_rows_since, sessions_since)
        if ci and ci[0] > tr["lkg_beats_champion_lower_bound_gt"]:
            fired.append(f"last-known-good beat the champion: 80% interval {ci}")
    if tr.get("ledger_or_spec_fail") and set(flags) & {"ledger_fail", "spec_fail"}:
        fired.append("ledger or specification check failed for the champion")
    return {"decision": "ROLLBACK" if fired else "KEEP", "triggers": fired,
            "watching": len(sessions_since) <= rc["watch_sessions"]}
