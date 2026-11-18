"""The seven stock-futures agents.

Each agent is deterministic code. Agents 1-5 react to events; 6 and 7 also
poll open state on every tick (monitoring and exits run first, and keep
running when entries are paused). Every handler runs inside ONE transaction
together with the consumed-mark for its input event, so a crash or a retry
cannot half-apply a step or apply it twice.

    ScanRequested ─▶ 1 MarketRegime ─▶ RegimeClassified
    RegimeClassified ─▶ 2 SectorRanking ─▶ SectorsRanked
    SectorsRanked ─▶ 3 StockScreening ─▶ CandidatesRanked
    CandidatesRanked / CandidateRejected ─▶ 4 Verification ─▶ CandidateVerified
    CandidateVerified / ApprovalDecided ─▶ 5 FuturesEntry ─▶ EntryOrderSubmitted
    OrderFilled(OPEN) ─▶ 5 ─▶ PositionOpened, ExitPlanActivated, OverrideRequested
    (poll) 6 PositionMonitor ─▶ ExitIntent
    ExitIntent ─▶ 7 PaperExit ─▶ ExitClaimed, ExitOrderSubmitted ─▶ PositionClosed / ExitUnresolved
Any stage can end a scan with ScanStopped and a specific reason.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from sf_core import (LONG, SHORT, BUY, SELL, OPEN, CLOSE, open_side, close_side, exit_quote_side,
                     gross_pnl, evaluate, describe, ist, iso, parse_ts)
from sf_broker import quote_check, position_qty, bar_exit_check

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sf-calc")


# ====================================================================== metrics

def window_metrics(bars, basis, lookback, now, cal, start_at=None):
    """Return over an explicit window, with the evidence needed to audit it.

    current_session: from the last bar at or before (now - minutes) to the last
      visible bar, both inside today's session. If the session is younger than
      the window, the session open is used and effective_minutes says so.
    completed_daily: close of `sessions` completed sessions ago to the last
      completed session's close. Today's partial bar is never included.
    """
    if not bars:
        return None
    if basis == "current_session":
        today = now.date()
        sess = [b for b in bars if parse_ts(b["ts"]).date() == today]
        if not sess:
            return None
        target = start_at or (now - timedelta(minutes=lookback.get("minutes", 30)))
        before = [b for b in sess if parse_ts(b["ts"]) <= target]
        if before:
            start_px, start_ts = before[-1]["close"], before[-1]["ts"]
            after = [b for b in sess if parse_ts(b["ts"]) > parse_ts(start_ts)]
        else:
            start_px, start_ts = sess[0]["open"], sess[0]["ts"]
            after = sess
        end = sess[-1]
        eff = round((parse_ts(end["available_at"]) - parse_ts(start_ts)).total_seconds() / 60)
        stale_ref = parse_ts(end["available_at"])
        closes = [b["close"] for b in sess]
        out = {"effective": f"{eff} min", "turnover_rs": sum(b.get("turnover", 0) for b in after)}
    else:
        n = lookback.get("sessions", 5)
        done = [b for b in bars if parse_ts(b["available_at"]) <= now]
        if len(done) < 2:
            return None
        win = done[-(n + 1):]
        start_px, start_ts = win[0]["close"], win[0]["ts"]
        end = win[-1]
        expected = last_completed_session(now, cal)
        stale_ref = None if parse_ts(end["ts"]).date() == expected else "stale"
        closes = [b["close"] for b in done]
        out = {"effective": f"{len(win) - 1} sessions", "turnover_rs": sum(b.get("turnover", 0) for b in win[1:]),
               "expected_last_session": str(expected)}
    if not start_px:
        return None
    out.update({"return_pct": (end["close"] / start_px - 1) * 100, "start_price": start_px,
                "end_price": end["close"], "start_ts": start_ts, "end_ts": end["ts"],
                "available_at": end["available_at"], "basis": basis, "closes": closes[-60:]})
    if basis == "current_session":
        out["age_s"] = (now - stale_ref).total_seconds()
    else:
        out["stale"] = stale_ref == "stale"
    return out


def last_completed_session(now, cal):
    d = now.date()
    if cal.is_session(d) and now >= cal.bounds(d)[1] + timedelta(minutes=1):
        return d
    return cal.prev_session(d)


def fresh(m, prof):
    if m is None:
        return False
    if m["basis"] == "current_session":
        return m["age_s"] <= prof["max_data_age_s"]
    return not m["stale"]


def sma_gap(m, n):
    c = (m or {}).get("closes") or []
    if len(c) < n:
        return None
    sma = sum(c[-n:]) / n
    return (c[-1] / sma - 1) * 100


def _r(x, d=4):
    return None if x is None else round(x, d)


# ====================================================================== base

class Agent:
    name = "agent"
    consumes = ()

    def __init__(self, engine):
        self.e = engine

    @property
    def st(self):
        return self.e.store

    def emit(self, c, type_, now, payload, src=None, **kw):
        """Publish, inheriting scan/trade/mode/config/direction from the input."""
        if src is not None:
            for k in ("scan_id", "trade_id", "mode", "config_version", "direction", "instrument_id"):
                kw.setdefault(k, src.get(k))
        return self.st.emit(c, type_, self.name, now, payload, **kw)

    def stop_scan(self, c, src, now, reason, stage, evidence=None):
        c.execute("UPDATE scans SET status='STOPPED', updated=? WHERE scan_id=? AND status NOT IN ('ENTERED')",
                  (iso(now), src["scan_id"]))
        self.emit(c, "ScanStopped", now, {"reason": reason, "stage": stage, "evidence": evidence or {}},
                  src=src, event_id=f"{src['scan_id']}:stopped")

    def handle(self, c, ev, now):
        raise NotImplementedError

    def poll(self, now):
        pass


# ====================================================================== 1

class MarketRegime(Agent):
    name = "1-market-regime"
    consumes = ("ScanRequested",)

    def handle(self, c, ev, now):
        cfg = self.e.cfg(ev["config_version"])
        prof = cfg["profiles"][ev["mode"]]
        rc = prof["regime"]
        bench = prof["benchmark"]
        basis = rc["lookback"]["basis"]
        interval = "1m" if basis == "current_session" else "1d"
        bm = window_metrics(self.e.data.bars(bench, now, interval), basis, rc["lookback"], now, self.e.cal)
        metrics, reasons = {}, []
        if bm is None:
            reasons.append(f"no {bench} bars for the window")
        elif not fresh(bm, prof):
            reasons.append(f"{bench} data stale (last bar {bm['end_ts']})")
        else:
            metrics["benchmark_return_pct"] = bm["return_pct"]
            metrics["close_vs_sma_pct"] = sma_gap(bm, rc.get("sma_bars", 20))
        # Breadth across the stock universe, over the same window.
        br = self.breadth(prof, basis, interval, now, bm)
        if br["counted"]:
            metrics["breadth_ratio"] = br["ratio"]
        missing = [m for m in rc.get("required_metrics", []) if metrics.get(m) is None]
        rule_log = []
        if reasons or missing:
            regime = "UNAVAILABLE"
            reasons += [f"required metric unavailable: {m}" for m in missing]
        else:
            regime = "SIDEWAYS"
            for rule in rc["rules"]:
                status, checks = evaluate(rule, metrics)
                rule_log.append({"regime": rule["regime"], "status": status, "checks": checks})
                if status == "PASS":
                    regime = rule["regime"]
                    break
            reasons.append(f"{regime}: " + ("; ".join(f"{r['regime']} {r['status']}" for r in rule_log)))
        permitted = list(prof["direction_map"].get(regime, []))
        prev = self.st.control(f"regime:{ev['mode']}")
        evidence = {"benchmark": bench, "source": self.e.data.name, "window": _strip(bm),
                    "metrics": {k: _r(v) for k, v in metrics.items()}, "breadth": br,
                    "rules": rule_log, "lookback": rc["lookback"]}
        c.execute("INSERT OR REPLACE INTO control(key,value,updated,actor) VALUES(?,?,?,?)",
                  (f"regime:{ev['mode']}", regime, iso(now), self.name))
        if prev and prev != regime:
            self.emit(c, "RegimeChanged", now, {"from": prev, "to": regime, "permitted": permitted}, src=ev,
                      event_id=f"{ev['scan_id']}:regime-changed")
        self.emit(c, "RegimeClassified", now, {"regime": regime, "permitted": permitted,
                  "reasons": reasons, "evidence": evidence}, src=ev, event_id=f"{ev['scan_id']}:regime")
        if not permitted:
            self.stop_scan(c, ev, now, f"market {regime}: no direction permitted for new entries "
                           f"({'; '.join(reasons)})", "market_regime", evidence)

    def breadth(self, prof, basis, interval, now, bm):
        uni = self.e.data.universe(now)
        start = parse_ts(bm["start_ts"]) if bm and basis == "current_session" else None
        def one(sym):
            return window_metrics(self.e.data.bars(sym, now, interval), basis, prof["regime"]["lookback"],
                                  now, self.e.cal, start_at=start)
        res = list(POOL.map(one, uni))
        rets = [m["return_pct"] for m in res if m and fresh(m, prof)]
        adv, dec = sum(r > 0 for r in rets), sum(r < 0 for r in rets)
        return {"universe": len(uni), "counted": len(rets), "advancers": adv, "decliners": dec,
                "ratio": (adv - dec) / (adv + dec) if adv + dec else 0.0}


def _strip(m):
    if not m:
        return m
    return {k: (_r(v) if isinstance(v, float) else v) for k, v in m.items() if k != "closes"}


# ====================================================================== 2

class SectorRanking(Agent):
    name = "2-sector-ranking"
    consumes = ("RegimeClassified",)

    def handle(self, c, ev, now):
        if not ev["payload"]["permitted"]:
            return
        cfg = self.e.cfg(ev["config_version"])
        prof = cfg["profiles"][ev["mode"]]
        sc = prof["sector"]
        basis = sc["lookback"]["basis"]
        interval = "1m" if basis == "current_session" else "1d"
        bwin = ev["payload"]["evidence"]["window"] or {}
        start = parse_ts(bwin.get("start_ts")) if basis == "current_session" and bwin else None
        bench_ret = ev["payload"]["evidence"]["metrics"].get("benchmark_return_pct")
        smap = self.e.data.sector_map(now)
        uni = self.e.data.universe(now)
        members, unmapped = {}, []
        for s in uni:
            rec = smap.get(s)
            if not rec or rec.get("ambiguous") or not rec.get("sector"):
                unmapped.append({"symbol": s, "reason": "ambiguous sector mapping" if rec else "no sector mapping"})
                continue
            members.setdefault(rec["sector"], []).append(s)

        def stock(sym):
            return sym, window_metrics(self.e.data.bars(sym, now, interval), basis, sc["lookback"], now,
                                       self.e.cal, start_at=start)
        stock_m = dict(POOL.map(stock, uni))

        def sector(name):
            syms = sorted(members[name])
            idx = self.e.data.sector_index_bars(name, now, interval)
            got = {s: stock_m.get(s) for s in syms if fresh(stock_m.get(s), prof)}
            row = {"sector": name, "constituents": len(syms), "fresh": len(got),
                   "coverage": round(len(got) / len(syms), 3) if syms else 0.0}
            if idx:
                m = window_metrics(idx, basis, sc["lookback"], now, self.e.cal, start_at=start)
                row.update(source="sector_index", return_pct=_r(m and m["return_pct"]))
            else:
                w = [max(m["turnover_rs"], 0) for m in got.values()]
                r = [m["return_pct"] for m in got.values()]
                if r:
                    ret = (sum(a * b for a, b in zip(r, w)) / sum(w)) if sum(w) > 0 else sum(r) / len(r)
                    row["return_pct"] = _r(ret)
                row["source"] = "constituent_aggregation (turnover-weighted)"
            row["turnover_rs"] = round(sum(m["turnover_rs"] for m in got.values()), 0)
            adv = sum(m["return_pct"] > 0 for m in got.values())
            dec = sum(m["return_pct"] < 0 for m in got.values())
            row["breadth"] = {"advancers": adv, "decliners": dec}
            return row
        rows = list(POOL.map(sector, sorted(members)))
        out = {"benchmark_return_pct": bench_ret, "unmapped": unmapped, "by_direction": {}, "sectors": rows}
        any_q = False
        for d in ev["payload"]["permitted"]:
            assessed = []
            for r in rows:
                a = dict(r)
                if r["constituents"] < sc["min_constituents"] or r["coverage"] < sc["min_coverage"] \
                        or r.get("return_pct") is None or bench_ret is None:
                    a.update(result="UNAVAILABLE", reason=f"insufficient fresh coverage "
                             f"({r['fresh']}/{r['constituents']}, need {sc['min_coverage']:.0%} and "
                             f"{sc['min_constituents']} constituents)")
                    assessed.append(a)
                    continue
                rel = r["return_pct"] - bench_ret
                a["relative_pct"] = _r(rel)
                if d == LONG:
                    abs_ok, rel_ok = r["return_pct"] > 0, rel > 0
                    want = "positive absolute return and benchmark outperformance"
                else:
                    abs_ok, rel_ok = r["return_pct"] < 0, rel < 0
                    want = "negative absolute return and benchmark underperformance"
                ok = (abs_ok or not sc["require_absolute"]) and (rel_ok or not sc["require_relative"])
                a.update(absolute_test="PASS" if abs_ok else "FAIL", relative_test="PASS" if rel_ok else "FAIL",
                         result="PASS" if ok else "FAIL",
                         reason=f"{d} needs {want}: absolute {r['return_pct']:+.2f}% "
                                f"{'ok' if abs_ok else 'fails'}, relative {rel:+.2f}% {'ok' if rel_ok else 'fails'}")
                assessed.append(a)
            q = [a for a in assessed if a["result"] == "PASS"]
            sign = -1 if d == LONG else 1
            q.sort(key=lambda a: (sign * a["relative_pct"], sign * a["return_pct"], a["sector"]))
            q = q[:sc["top_n"]]
            for i, a in enumerate(q, 1):
                a["rank"] = i
            out["by_direction"][d] = {"qualifying": q, "assessed": assessed}
            any_q |= bool(q)
        self.emit(c, "SectorsRanked", now, out, src=ev, event_id=f"{ev['scan_id']}:sectors")
        if not any_q:
            self.stop_scan(c, ev, now, "no sector qualified for the permitted direction", "sector_ranking",
                           {"assessed": {d: [(a["sector"], a["result"], a["reason"]) for a in v["assessed"]]
                                         for d, v in out["by_direction"].items()}})


# ====================================================================== 3

class StockScreening(Agent):
    name = "3-stock-screening"
    consumes = ("SectorsRanked",)

    def handle(self, c, ev, now):
        cfg = self.e.cfg(ev["config_version"])
        prof = cfg["profiles"][ev["mode"]]
        sk = prof["stock"]
        basis = sk["lookback"]["basis"]
        interval = "1m" if basis == "current_session" else "1d"
        bench_ret = ev["payload"]["benchmark_return_pct"]
        smap = self.e.data.sector_map(now)
        uni = self.e.data.universe(now)
        cands, rejected = [], []
        for d, block in ev["payload"]["by_direction"].items():
            rank_of = {a["sector"]: a["rank"] for a in block["qualifying"]}
            syms = sorted(s for s in uni if (smap.get(s) or {}).get("sector") in rank_of
                          and not smap[s].get("ambiguous"))

            def one(sym, d=d):
                m = window_metrics(self.e.data.bars(sym, now, interval), basis, sk["lookback"], now, self.e.cal)
                return sym, m
            for sym, m in POOL.map(one, syms):
                sec = smap[sym]["sector"]
                if not fresh(m, prof):
                    rejected.append({"symbol": sym, "direction": d, "reason": "stale or missing bars"})
                    continue
                metrics = {"return_pct": m["return_pct"], "rel_return_pct": m["return_pct"] - bench_ret,
                           "turnover_rs": m["turnover_rs"]}
                status, checks = evaluate(sk["long" if d == LONG else "short"], metrics)
                if metrics["turnover_rs"] < sk["min_turnover_rs"]:
                    status = "FAIL"
                    checks.append({"metric": "turnover_rs", "op": ">=", "threshold": sk["min_turnover_rs"],
                                   "value": metrics["turnover_rs"], "result": "FAIL"})
                if status != "PASS":
                    rejected.append({"symbol": sym, "direction": d, "sector": sec,
                                     "reason": describe(checks)})
                    continue
                score = metrics["rel_return_pct"] if d == LONG else -metrics["rel_return_pct"]
                cands.append({"symbol": sym, "sector": sec, "sector_rank": rank_of[sec], "direction": d,
                              "score": round(score, 4), "metrics": {k: _r(v) for k, v in metrics.items()},
                              "checks": checks, "last_price": m["end_price"], "bar_ts": m["end_ts"],
                              "available_at": m["available_at"], "window": m["effective"]})
        dir_order = {d: i for i, d in enumerate(ev["payload"]["by_direction"])}
        cands.sort(key=lambda x: (dir_order[x["direction"]], x["sector_rank"], -x["score"], x["symbol"]))
        cands = cands[:sk["top_n"]]
        for i, x in enumerate(cands):
            x["rank"] = i
        expires = now + timedelta(seconds=prof["candidate_ttl_s"])
        c.execute("UPDATE scans SET body=json_set(COALESCE(body,'{}'),'$.expires_at',?) WHERE scan_id=?",
                  (iso(expires), ev["scan_id"]))
        self.emit(c, "CandidatesRanked", now, {"candidates": cands, "rejected": rejected,
                  "expires_at": iso(expires), "tie_break": "sector rank, then score desc, then symbol"},
                  src=ev, event_id=f"{ev['scan_id']}:candidates")
        if not cands:
            self.stop_scan(c, ev, now, "no stock in the qualifying sectors passed screening", "stock_screening",
                           {"rejected": rejected[:20]})


# ====================================================================== 4

class Verification(Agent):
    name = "4-desk-verification"
    consumes = ("CandidatesRanked", "CandidateRejected")

    def handle(self, c, ev, now):
        scan = self.st.q1("SELECT * FROM scans WHERE scan_id=?", ev["scan_id"])
        if scan and scan["status"] in ("ENTERED", "STOPPED"):
            return
        if ev["type"] == "CandidatesRanked":
            cands, start = ev["payload"]["candidates"], 0
            expires = ev["payload"]["expires_at"]
        else:
            ranked = self.st.events("scan_id=? AND type='CandidatesRanked'", ev["scan_id"])[0]["payload"]
            cands, start, expires = ranked["candidates"], ev["payload"]["rank"] + 1, ranked["expires_at"]
        if not cands:
            return
        cfg = self.e.cfg(ev["config_version"])
        prof = cfg["profiles"][ev["mode"]]
        vc = prof["verification"]
        tried = []
        for cand in cands[start:]:
            if now > parse_ts(expires):
                return self.stop_scan(c, ev, now, f"scan expired at {expires} before a candidate was accepted",
                                      "verification", {"tried": tried})
            res = self.verify(cand, vc, now)
            tried.append({"symbol": cand["symbol"], "status": res["status"]})
            self.emit(c, "VerificationResult", now, res, src=ev, direction=cand["direction"],
                      event_id=f"{ev['scan_id']}:verify:{cand['rank']}")
            if res["status"] == "PASS":
                self.emit(c, "CandidateVerified", now, {"candidate": cand, "verification": res,
                          "rank": cand["rank"], "expires_at": expires}, src=ev, direction=cand["direction"],
                          event_id=f"{ev['scan_id']}:verified:{cand['rank']}")
                return
        reasons = ", ".join(f"{t['symbol']} {t['status']}" for t in tried) or "every ranked candidate was tried"
        self.stop_scan(c, ev, now, f"no candidate could be entered ({reasons}"
                       f"{'; last rejection: ' + ev['payload']['reason'] if ev['type'] == 'CandidateRejected' else ''})",
                       "verification", {"tried": tried})

    def verify(self, cand, vc, now):
        snap = self.e.data.desk_snapshot(cand["symbol"], now)
        base = {"symbol": cand["symbol"], "direction": cand["direction"], "rank": cand["rank"]}
        if not snap:
            return dict(base, status="UNAVAILABLE", reasons=["no Nifty Desk snapshot for the symbol"])
        age = (now - parse_ts(snap["ts"])).total_seconds()
        ev = {"source": snap.get("source"), "snapshot_ts": snap["ts"], "available_at": snap["available_at"],
              "age_s": int(age), "metrics": snap["metrics"], "kinds": snap.get("kinds", {}),
              "revision": snap.get("revision")}
        shared = "stocks_1m" in (snap.get("source") or "") and self.e.data.name == "repo"
        ev["shared_source_with_screening"] = shared
        if age > vc["max_age_s"]:
            return dict(base, status="UNAVAILABLE", evidence=ev,
                        reasons=[f"snapshot stale: {int(age)}s old > {vc['max_age_s']}s"])
        status, checks = evaluate(vc["long" if cand["direction"] == LONG else "short"], snap["metrics"])
        return dict(base, status=status, evidence=ev, checks=checks, reasons=[describe(checks)])


# ====================================================================== 5

class FuturesEntry(Agent):
    name = "5-futures-entry"
    consumes = ("CandidateVerified", "ApprovalDecided", "OrderFilled")

    def handle(self, c, ev, now):
        if ev["type"] == "CandidateVerified":
            return self.on_verified(c, ev, now)
        if ev["type"] == "ApprovalDecided":
            return self.on_approval(c, ev, now)
        if ev["type"] == "OrderFilled" and ev["payload"]["purpose"] == OPEN:
            return self.on_entry_fill(c, ev, now)

    # ------------------------------------------------------------ contract
    def resolve(self, cand, prof, now, mode):
        """(contract, quote, None) or (None, None, reason)."""
        sym = cand["symbol"]
        cc = prof["contract"]
        rows = self.e.data.contracts(sym, now)
        rows = [r for r in rows if r.get("instrument_type") == "FUTSTK" and r.get("underlying") == sym]
        if not rows:
            return None, None, f"no FUTSTK contract listed for {sym}"
        today = now.date()
        reasons, eligible = [], []
        by_exp = {}
        for r in rows:
            by_exp.setdefault(r.get("expiry"), []).append(r)
        for exp in sorted(k for k in by_exp if k):
            group = by_exp[exp]
            if len(group) > 1:
                reasons.append(f"{exp}: ambiguous ({len(group)} contracts for one expiry)")
                continue
            r = group[0]
            miss = [k for k in ("instrument_id", "expiry", "lot_size", "tick_size") if not r.get(k)]
            if miss:
                reasons.append(f"{r.get('tradingsymbol')}: missing {', '.join(miss)}")
                continue
            if r.get("status") != "listed":
                reasons.append(f"{r['tradingsymbol']}: status {r.get('status')}")
                continue
            e = date.fromisoformat(r["expiry"])
            if e < today:
                reasons.append(f"{r['tradingsymbol']}: expired")
                continue
            if mode == "intraday":
                if (e - today).days < cc.get("min_days_to_expiry", 0):
                    reasons.append(f"{r['tradingsymbol']}: {(e - today).days}d to expiry < {cc['min_days_to_expiry']}")
                    continue
            else:
                dl = pre_expiry_deadline(e, cc["pre_expiry_buffer_sessions"], prof, self.e.cal)
                need = prof["exit_plan"].get("max_holding_sessions") or 0
                life = self.e.cal.sessions_between(today, dl.date())
                if dl <= now or life < need:
                    reasons.append(f"{r['tradingsymbol']}: {life} sessions to pre-expiry deadline "
                                   f"{iso(dl)} < planned {need}")
                    continue
                r = dict(r, pre_expiry_deadline=iso(dl), sessions_to_deadline=life)
            eligible.append(r)
        if cc.get("expiry_preference") == "next" and eligible:
            eligible = eligible[1:]
        if not eligible:
            return None, None, "no eligible contract: " + "; ".join(reasons)
        k = eligible[0]
        q = self.e.data.quote(k["instrument_id"], now)
        ok, why = quote_check(q, k["instrument_id"], now, self.e.cfg_now()["shared"]["fill_policy"]["max_quote_age_s"])
        if not ok:
            return None, None, f"{k['tradingsymbol']}: {why}"
        spread_ticks = (q["ask"] - q["bid"]) / k["tick_size"]
        if spread_ticks > cc["max_spread_ticks"]:
            return None, None, f"{k['tradingsymbol']}: spread {spread_ticks:.0f} ticks > {cc['max_spread_ticks']}"
        side_qty = q.get("ask_qty") if open_side(cand["direction"]) == BUY else q.get("bid_qty")
        if side_qty is not None and side_qty < cc["min_top_qty_lots"] * k["lot_size"]:
            return None, None, f"{k['tradingsymbol']}: top-of-book size {side_qty} < {cc['min_top_qty_lots']} lot"
        return k, q, None

    # ------------------------------------------------------------ limits
    def limits(self, c, mode, cfg, notional, now):
        sh, prof = cfg["shared"], cfg["profiles"][mode]
        res = [dict(r) for r in c.execute("SELECT * FROM reservations").fetchall()]
        mine = [r for r in res if r["mode"] == mode]
        if len(res) + 1 > sh["max_open_positions"]:
            return f"shared open-position limit {sh['max_open_positions']} reached"
        if len(mine) + 1 > prof["max_open_positions"]:
            return f"{mode} open-position limit {prof['max_open_positions']} reached"
        tot = sum(r["notional"] for r in res) + notional
        if tot > sh["max_gross_notional_rs"]:
            return f"one lot (Rs {notional:,.0f}) takes shared notional to Rs {tot:,.0f} > Rs {sh['max_gross_notional_rs']:,.0f}"
        mt = sum(r["notional"] for r in mine) + notional
        if mt > prof["max_gross_notional_rs"]:
            return f"one lot (Rs {notional:,.0f}) takes {mode} notional to Rs {mt:,.0f} > Rs {prof['max_gross_notional_rs']:,.0f}"
        if sh.get("margin_budget_rs"):
            m = sum(r["margin"] for r in res) + notional * sh["margin_pct_estimate"]
            if m > sh["margin_budget_rs"]:
                return f"estimated paper margin Rs {m:,.0f} > budget Rs {sh['margin_budget_rs']:,.0f}"
        loss = self.e.day_pnl(now)
        if loss <= -sh["daily_loss_limit_rs"]:
            return f"daily loss Rs {loss:,.0f} at or beyond limit Rs {sh['daily_loss_limit_rs']:,.0f}"
        return None

    def gates(self, mode, prof, now):
        if self.st.paused(mode):
            return "entries paused (" + ("global" if self.st.control("pause:global") == "1" else mode) + ")"
        g = self.e.live_gate()
        if g:
            return g
        t = now.strftime("%H:%M")
        a, b = prof["entry_window"]
        if not (a <= t <= b) or not self.e.cal.in_session(now):
            return f"outside the {mode} entry window {a}-{b} IST"
        return None

    # ------------------------------------------------------------ handlers
    def on_verified(self, c, ev, now):
        scan = dict(c.execute("SELECT * FROM scans WHERE scan_id=?", (ev["scan_id"],)).fetchone())
        if scan["status"] in ("ENTERED", "STOPPED"):
            return
        cfg = self.e.cfg(ev["config_version"])
        mode, cand = ev["mode"], ev["payload"]["candidate"]
        prof = cfg["profiles"][mode]
        if now > parse_ts(ev["payload"]["expires_at"]):
            return self.stop_scan(c, ev, now, "scan expired before entry", "entry")
        gate = self.gates(mode, prof, now)
        if gate:
            return self.stop_scan(c, ev, now, gate, "entry")
        k, q, why = self.resolve(cand, prof, now, mode)
        if why:
            return self.reject(c, ev, now, cand, f"contract: {why}")
        side = open_side(cand["direction"])
        ref = q["ask"] if side == BUY else q["bid"]
        notional = ref * k["lot_size"]
        lim = self.limits(c, mode, cfg, notional, now)
        if lim:
            return self.reject(c, ev, now, cand, f"limit: {lim} -- one lot is the only size, so the entry is skipped")
        trade_id = f"{ev['scan_id']}-{cand['symbol']}"
        held = c.execute("SELECT * FROM reservations WHERE underlying=?", (cand["symbol"],)).fetchone()
        if held:
            return self.reject(c, ev, now, cand, f"{cand['symbol']} already has an active {held['mode']} "
                               f"{held['direction']} entry/position ({held['trade_id']}); no second position, no reversal")
        c.execute("INSERT INTO reservations(underlying,trade_id,mode,direction,notional,margin,created) "
                  "VALUES(?,?,?,?,?,?,?)", (cand["symbol"], trade_id, mode, cand["direction"], notional,
                                            notional * cfg["shared"]["margin_pct_estimate"], iso(now)))
        t = {"trade_id": trade_id, "scan_id": ev["scan_id"], "mode": mode, "direction": cand["direction"],
             "underlying": cand["symbol"], "sector": cand["sector"], "instrument_id": k["instrument_id"],
             "tradingsymbol": k["tradingsymbol"], "expiry": k["expiry"], "lot_size": k["lot_size"],
             "tick_size": k["tick_size"], "lots": 1, "requested_qty": k["lot_size"],
             "config_version": ev["config_version"], "created_at": iso(now), "candidate": cand,
             "verification": ev["payload"]["verification"], "contract": k, "entry_quote": q,
             "notional_reserved": notional, "flags": [], "label": self.e.label}
        if k.get("pre_expiry_deadline"):
            t["pre_expiry_deadline"] = k["pre_expiry_deadline"]
        ap = prof.get("approval", {})
        if ap.get("entry_required"):
            t["status"] = "PENDING_APPROVAL"
            deadline = now + timedelta(seconds=ap["entry_window_s"])
            aid = f"{trade_id}-entry"
            c.execute("INSERT OR IGNORE INTO approvals(approval_id,kind,trade_id,scan_id,status,deadline,created,body) "
                      "VALUES(?,?,?,?,?,?,?,?)", (aid, "entry", trade_id, ev["scan_id"], "PENDING", iso(deadline),
                                                  iso(now), json.dumps({"rank": cand["rank"]})))
            self.st.put_trade(c, t, now)
            c.execute("UPDATE scans SET status='AWAITING_APPROVAL', updated=? WHERE scan_id=?", (iso(now), ev["scan_id"]))
            self.emit(c, "EntryApprovalRequested", now, {"approval_id": aid, "deadline": iso(deadline),
                      "contract": k, "side": side, "lots": 1, "units": k["lot_size"], "quote": q},
                      src=ev, trade_id=trade_id, instrument_id=k["instrument_id"])
            return
        self.submit(c, t, ev, now)

    def submit(self, c, t, src, now):
        side = open_side(t["direction"])
        coid = f"{t['trade_id']}-OPEN-1"
        t["status"] = "PENDING_ENTRY"
        self.st.put_trade(c, t, now)
        o = {"client_order_id": coid, "trade_id": t["trade_id"], "instrument_id": t["instrument_id"],
             "side": side, "purpose": OPEN, "direction": t["direction"], "status": "PENDING",
             "requested_qty": t["requested_qty"], "filled_qty": 0, "created": iso(now),
             "lot_size": t["lot_size"], "tick_size": t["tick_size"], "mode": t["mode"],
             "config_version": t["config_version"], "lots": 1}
        c.execute("INSERT OR IGNORE INTO orders(client_order_id,trade_id,instrument_id,side,purpose,direction,status,"
                  "requested_qty,filled_qty,created,updated,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (coid, t["trade_id"], t["instrument_id"], side, OPEN, t["direction"], "PENDING",
                   t["requested_qty"], 0, iso(now), iso(now), json.dumps(o)))
        c.execute("UPDATE scans SET status='ENTERED', updated=? WHERE scan_id=?", (iso(now), t["scan_id"]))
        self.emit(c, "EntryOrderSubmitted", now, {"client_order_id": coid, "side": side, "purpose": OPEN,
                  "direction": t["direction"], "lots": 1, "units": t["requested_qty"],
                  "tradingsymbol": t["tradingsymbol"], "expiry": t["expiry"]},
                  src=src, trade_id=t["trade_id"], instrument_id=t["instrument_id"], direction=t["direction"],
                  mode=t["mode"], config_version=t["config_version"], event_id=f"submit:{coid}")

    def reject(self, c, ev, now, cand, reason):
        self.emit(c, "CandidateRejected", now, {"rank": cand["rank"], "symbol": cand["symbol"], "reason": reason},
                  src=ev, direction=cand["direction"], event_id=f"{ev['scan_id']}:rejected:{cand['rank']}")

    def release(self, c, trade_id):
        c.execute("DELETE FROM reservations WHERE trade_id=?", (trade_id,))

    def on_approval(self, c, ev, now):
        a = c.execute("SELECT * FROM approvals WHERE approval_id=?", (ev["payload"]["approval_id"],)).fetchone()
        if not a or a["kind"] != "entry" or a["status"] != "PENDING":
            return
        t = self.st.trade(a["trade_id"])["body"]
        cfg = self.e.cfg(t["config_version"])
        prof = cfg["profiles"][t["mode"]]
        late = now > parse_ts(a["deadline"])
        decision = "LATE" if late else ev["payload"]["decision"]
        c.execute("UPDATE approvals SET status=?, decided=? WHERE approval_id=?",
                  ({"APPROVE": "APPROVED", "REJECT": "REJECTED"}.get(decision, "EXPIRED"), iso(now), a["approval_id"]))
        src = dict(scan_id=t["scan_id"], mode=t["mode"], config_version=t["config_version"], direction=t["direction"])
        cand = t["candidate"]
        if decision != "APPROVE":
            return self._drop(c, t, src, now, f"entry approval {decision.lower()}")
        # Recheck after the delay: freshness, gates, contract and quote.
        exp = self.st.events("scan_id=? AND type='CandidatesRanked'", t["scan_id"])[0]["payload"]["expires_at"]
        why = None
        if now > parse_ts(exp):
            why = "scan expired during approval"
        why = why or self.gates(t["mode"], prof, now)
        if not why:
            k, q, why = self.resolve(cand, prof, now, t["mode"])
            if not why and k["instrument_id"] != t["instrument_id"]:
                why = "resolved contract changed during approval"
        if why:
            return self._drop(c, t, src, now, f"after approval: {why}")
        self.submit(c, t, src, now)

    def _drop(self, c, t, src, now, reason):
        self.release(c, t["trade_id"])
        t.update(status="CANCELED", closed_at=iso(now), cancel_reason=reason)
        self.st.put_trade(c, t, now)
        c.execute("UPDATE scans SET status='RUNNING' WHERE scan_id=?", (t["scan_id"],))
        self.emit(c, "CandidateRejected", now, {"rank": t["candidate"]["rank"], "symbol": t["underlying"],
                  "reason": reason}, src=src, trade_id=t["trade_id"],
                  event_id=f"{t['scan_id']}:rejected:{t['candidate']['rank']}")

    def poll(self, now):
        # An unanswered override window just closes: the default plan was active all along.
        with self.st.tx() as c:
            for a in c.execute("SELECT approval_id, deadline FROM approvals WHERE kind='override' AND status='PENDING'").fetchall():
                if now > parse_ts(a["deadline"]):
                    c.execute("UPDATE approvals SET status='EXPIRED', decided=? WHERE approval_id=?",
                              (iso(now), a["approval_id"]))
        for a in self.st.q("SELECT * FROM approvals WHERE kind='entry' AND status='PENDING'"):
            if now > parse_ts(a["deadline"]):
                with self.st.tx() as c:
                    cur = c.execute("SELECT status FROM approvals WHERE approval_id=?", (a["approval_id"],)).fetchone()
                    if cur["status"] != "PENDING":
                        continue
                    c.execute("UPDATE approvals SET status='EXPIRED', decided=? WHERE approval_id=?",
                              (iso(now), a["approval_id"]))
                    t = self.st.trade(a["trade_id"])["body"]
                    src = dict(scan_id=t["scan_id"], mode=t["mode"], config_version=t["config_version"],
                               direction=t["direction"])
                    self._drop(c, t, src, now, "entry approval expired without a reply")

    def on_entry_fill(self, c, ev, now):
        tr = self.st.trade(ev["trade_id"])
        if not tr:
            return
        t = tr["body"]
        opened, closed = position_qty(self.st, t["trade_id"])
        fills = self.st.fills("trade_id=? AND purpose='OPEN'", t["trade_id"])
        avg = sum(f["qty"] * f["price"] for f in fills) / opened if opened else None
        t["entry_filled_qty"], t["entry_avg"] = opened, avg
        if t.get("status") in ("PENDING_ENTRY",) or not t.get("opened_at"):
            cfg = self.e.cfg(t["config_version"])
            prof = cfg["profiles"][t["mode"]]
            t["status"] = "OPEN" if t["status"] in ("PENDING_ENTRY", "OPEN") else t["status"]
            t["opened_at"] = iso(now)
            t["entry_session"] = str(now.date())
            plan = activate_plan(prof, t, avg, now, self.e.cal)
            t["exit_plan"] = plan
            t["plan_history"] = [{"version": 1, "at": iso(now), "by": "default", "plan": plan}]
            self.st.put_trade(c, t, now)
            src = dict(scan_id=t["scan_id"], mode=t["mode"], config_version=t["config_version"],
                       direction=t["direction"], trade_id=t["trade_id"], instrument_id=t["instrument_id"])
            self.emit(c, "PositionOpened", now, {"first_fill": ev["payload"], "units": opened,
                      "entry_side": open_side(t["direction"]), "lots": 1}, src=src,
                      event_id=f"opened:{t['trade_id']}")
            self.emit(c, "ExitPlanActivated", now, {"plan": plan, "version": 1}, src=src,
                      event_id=f"plan:{t['trade_id']}:1")
            ov = prof.get("override", {})
            if ov.get("enabled"):
                dl = now + timedelta(seconds=ov["response_window_s"])
                hard = plan.get("intraday_deadline") or plan.get("pre_expiry_deadline")
                if hard and parse_ts(hard) < dl:
                    dl = parse_ts(hard)
                aid = f"{t['trade_id']}-override"
                c.execute("INSERT OR IGNORE INTO approvals(approval_id,kind,trade_id,scan_id,status,deadline,created,body) "
                          "VALUES(?,?,?,?,?,?,?,?)", (aid, "override", t["trade_id"], t["scan_id"], "PENDING",
                                                      iso(dl), iso(now), json.dumps({"allowed": ov["allowed"]})))
                self.emit(c, "OverrideRequested", now, {"approval_id": aid, "deadline": iso(dl),
                          "default_plan": plan, "allowed": ov["allowed"],
                          "note": "the default plan stays active while waiting"}, src=src,
                          event_id=f"override-req:{t['trade_id']}")
        else:
            self.st.put_trade(c, t, now)


def pre_expiry_deadline(expiry, buffer_sessions, prof, cal):
    """Last moment to be flat: `buffer_sessions` sessions before the expiry
    session, at the profile's session exit time. If expiry itself is not a
    session (holiday), the exchange's previous session is the expiry session."""
    e = expiry if cal.is_session(expiry) else cal.prev_session(expiry)
    d = cal.prev_session(e, buffer_sessions) if buffer_sessions else e
    return ist(d, prof["exit_plan"].get("session_exit_time", "15:15"))


def plan_levels(direction, entry, plan):
    def lvl(spec, sign):
        if not spec:
            return None
        if spec["basis"] == "pct":
            return round(entry * (1 + sign * spec["value"] / 100), 4)
        if spec["basis"] == "points":
            return round(entry + sign * spec["value"], 4)
        if spec["basis"] == "price":
            return spec["value"]
        raise ValueError(f"unknown basis {spec['basis']}")
    s = -1 if direction == LONG else 1
    return lvl(plan.get("stop"), s), lvl(plan.get("target"), -s)


def activate_plan(prof, t, entry, now, cal):
    p = json.loads(json.dumps(prof["exit_plan"]))
    p["stop_price"], p["target_price"] = plan_levels(t["direction"], entry, p)
    p["evaluated_on"] = f"{exit_quote_side(t['direction'])} (executable {close_side(t['direction'])} side)"
    if t["mode"] == "intraday":
        p["intraday_deadline"] = iso(ist(now.date(), prof["exit_deadline"]))
    else:
        p["pre_expiry_deadline"] = t.get("pre_expiry_deadline")
    p["regime_at_entry"] = None
    return p


# ====================================================================== 6

class PositionMonitor(Agent):
    name = "6-position-monitor"
    consumes = ()

    def poll(self, now):
        for tr in self.st.trades("status='OPEN'"):
            try:
                self.check(tr["body"], now)
            except Exception as e:  # noqa: BLE001 -- one bad position must not stop the rest
                with self.st.tx() as c:
                    self.st.emit(c, "MonitorError", self.name, now, {"error": f"{type(e).__name__}: {e}"},
                                 trade_id=tr["trade_id"], mode=tr["mode"])

    def check(self, t, now):
        cfg = self.e.cfg(t["config_version"])
        sh = cfg["shared"]
        plan = t["exit_plan"]
        d = t["direction"]
        side = exit_quote_side(d)
        trig, quote_ok, px, q, ambiguous = {}, False, None, None, False
        if self.e.fill_mode == "bars":
            bars = self.e.data.bars("FUT:" + t["instrument_id"], now, "1m")
            bars = [b for b in bars if parse_ts(b["ts"]) >= parse_ts(t["opened_at"])]
            if bars:
                b = bars[-1]
                which, ambiguous = bar_exit_check(b, d, plan.get("stop_price"), plan.get("target_price"),
                                                  sh["fill_policy"]["bar_ambiguity"])
                quote_ok, px = True, b["close"]
                if which:
                    lvl = plan["stop_price"] if which == "stop" else plan["target_price"]
                    trig[which] = {"trigger_price": lvl, "bar": b, "ambiguous_bar": ambiguous}
        elif not self.e.cal.in_session(now):
            pass                       # market closed: no quote expected, time rules still apply below
        else:
            q = self.e.data.quote(t["instrument_id"], now)
            quote_ok, why = quote_check(q, t["instrument_id"], now, sh["fill_policy"]["max_quote_age_s"])
            if quote_ok:
                px = q[side]
                sp, tp = plan.get("stop_price"), plan.get("target_price")
                hit_s = sp is not None and (px <= sp if d == LONG else px >= sp)
                hit_t = tp is not None and (px >= tp if d == LONG else px <= tp)
                if hit_s:
                    trig["stop"] = {"trigger_price": sp, "observed": px, "quote": q}
                if hit_t:
                    trig["target"] = {"trigger_price": tp, "observed": px, "quote": q}
                hard = plan.get("hard_max_stop_pct")
                if hard and t.get("entry_avg"):
                    adverse = (t["entry_avg"] - px if d == LONG else px - t["entry_avg"]) / t["entry_avg"] * 100
                    if adverse >= hard:
                        trig["hard_risk"] = {"adverse_pct": round(adverse, 3), "limit_pct": hard, "quote": q}
            flag = None if quote_ok else why
            if flag != t.get("data_flag"):
                with self.st.tx() as c:
                    t["data_flag"] = flag
                    t["data_flag_since"] = iso(now) if flag else None
                    self.st.put_trade(c, t, now)
                    self.st.emit(c, "QuoteStale" if flag else "QuoteRestored", self.name, now,
                                 {"reason": flag, "quote": q}, trade_id=t["trade_id"], mode=t["mode"],
                                 direction=d, instrument_id=t["instrument_id"], config_version=t["config_version"])
        # time-based exits need no quote
        if plan.get("intraday_deadline") and now >= parse_ts(plan["intraday_deadline"]):
            trig["intraday_deadline"] = {"deadline": plan["intraday_deadline"]}
        if plan.get("pre_expiry_deadline") and now >= parse_ts(plan["pre_expiry_deadline"]):
            trig["pre_expiry_deadline"] = {"deadline": plan["pre_expiry_deadline"]}
        mh = plan.get("max_holding_sessions")
        if t["mode"] == "swing" and mh:
            held = self.e.cal.sessions_between(date.fromisoformat(t["entry_session"]), now.date())
            if held >= mh and self.e.cal.is_session(now.date()) and \
                    now >= ist(now.date(), plan.get("session_exit_time", "15:15")):
                trig["max_holding"] = {"sessions_held": held, "max": mh}
        inv = plan.get("invalidation") or {}
        if inv.get("regime_change"):
            regime = self.st.control(f"regime:{t['mode']}")
            permitted = cfg["profiles"][t["mode"]]["direction_map"].get(regime or "UNAVAILABLE", [])
            if regime and d not in permitted:
                trig["invalidation"] = {"regime": regime}
        mtm = gross_pnl(d, t["entry_avg"], px, t.get("entry_filled_qty") or 0) if quote_ok and px else None
        if mtm is not None and (t.get("mtm") is None or abs(mtm - t["mtm"]) > 1e-9):
            with self.st.tx() as c:
                fresh_t = self.st.trade(t["trade_id"])["body"]
                fresh_t.update(mtm=round(mtm, 2), mark=px, mark_side=side, mark_at=iso(now))
                self.st.put_trade(c, fresh_t, now)
                t = fresh_t
        if not trig:
            return
        order = sh["exit_precedence"]
        primary = sorted(trig, key=order.index)[0]
        with self.st.tx() as c:
            self.st.emit(c, "ExitIntent", self.name, now,
                         {"primary": primary, "triggered": sorted(trig, key=order.index), "details": trig,
                          "trigger_price": trig[primary].get("trigger_price"),
                          "observed_price": trig[primary].get("observed"), "plan_version": len(t.get("plan_history", [])),
                          "quote_usable": quote_ok, "precedence": order},
                         trade_id=t["trade_id"], mode=t["mode"], direction=d, scan_id=t["scan_id"],
                         instrument_id=t["instrument_id"], config_version=t["config_version"],
                         event_id=f"exit-intent:{t['trade_id']}")


# ====================================================================== 7

class PaperExit(Agent):
    name = "7-paper-exit"
    consumes = ("ExitIntent",)

    def handle(self, c, ev, now):
        tid = ev["trade_id"]
        cur = c.execute("INSERT OR IGNORE INTO exit_claims(trade_id,reason,claimed_by,at,body) VALUES(?,?,?,?,?)",
                        (tid, ev["payload"]["primary"], self.e.worker_id, iso(now), json.dumps(ev["payload"], default=str)))
        if cur.rowcount != 1:
            return                                               # already claimed
        t = self.st.trade(tid)["body"]
        t["status"] = "EXITING"
        t["exit"] = {"reason": ev["payload"]["primary"], "triggered": ev["payload"]["triggered"],
                     "trigger_price": ev["payload"].get("trigger_price"),
                     "observed_price": ev["payload"].get("observed_price"),
                     "details": ev["payload"]["details"], "claimed_at": iso(now), "claimed_by": self.e.worker_id}
        # cancel outstanding entry quantity
        for o in self.st.orders("trade_id=? AND purpose='OPEN' AND status IN ('PENDING','PARTIAL')", tid):
            body = dict(o["body"], status="CANCEL_PENDING")
            c.execute("UPDATE orders SET status='CANCEL_PENDING', updated=?, body=? WHERE client_order_id=?",
                      (iso(now), json.dumps(body), o["client_order_id"]))
            self.emit(c, "EntryCancelRequested", now, {"client_order_id": o["client_order_id"],
                      "remaining": o["requested_qty"] - o["filled_qty"]}, src=ev)
        self.st.put_trade(c, t, now)
        self.emit(c, "ExitClaimed", now, {"reason": t["exit"]["reason"], "triggered": t["exit"]["triggered"],
                  "worker": self.e.worker_id}, src=ev, event_id=f"claimed:{tid}")
        self.reconcile(c, t, now, src=ev)

    def reconcile(self, c, t, now, src=None):
        """Bring working CLOSE quantity in line with the open position, then
        finalise when flat. Never sends more than is open."""
        tid = t["trade_id"]
        opened, closed = position_qty(self.st, tid)
        open_units = opened - closed
        working = [o for o in self.st.orders("trade_id=? AND purpose='CLOSE' AND status IN ('PENDING','PARTIAL')", tid)]
        working_qty = sum(o["requested_qty"] - o["filled_qty"] for o in working)
        src = src or dict(scan_id=t["scan_id"], mode=t["mode"], config_version=t["config_version"],
                          direction=t["direction"], trade_id=tid, instrument_id=t["instrument_id"])
        if open_units > working_qty:
            n = len(self.st.orders("trade_id=? AND purpose='CLOSE'", tid)) + 1
            coid = f"{tid}-CLOSE-{n}"
            qty = open_units - working_qty
            side = close_side(t["direction"])
            o = {"client_order_id": coid, "trade_id": tid, "instrument_id": t["instrument_id"], "side": side,
                 "purpose": CLOSE, "direction": t["direction"], "status": "PENDING", "requested_qty": qty,
                 "filled_qty": 0, "created": iso(now), "lot_size": t["lot_size"], "tick_size": t["tick_size"],
                 "mode": t["mode"], "config_version": t["config_version"], "reason": t["exit"]["reason"]}
            c.execute("INSERT OR IGNORE INTO orders(client_order_id,trade_id,instrument_id,side,purpose,direction,status,"
                      "requested_qty,filled_qty,created,updated,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (coid, tid, t["instrument_id"], side, CLOSE, t["direction"], "PENDING", qty, 0,
                       iso(now), iso(now), json.dumps(o)))
            self.emit(c, "ExitOrderSubmitted", now, {"client_order_id": coid, "side": side, "purpose": CLOSE,
                      "units": qty, "open_units": open_units, "reason": t["exit"]["reason"]},
                      src=src, event_id=f"submit:{coid}")
        elif open_units < working_qty:
            excess = working_qty - open_units
            for o in reversed(working):                        # trim newest first
                if excess <= 0:
                    break
                rem = o["requested_qty"] - o["filled_qty"]
                cut = min(rem, excess)
                new_req = o["requested_qty"] - cut
                st = "CANCELED" if new_req == o["filled_qty"] else o["status"]
                c.execute("UPDATE orders SET requested_qty=?, status=?, updated=? WHERE client_order_id=?",
                          (new_req, st, iso(now), o["client_order_id"]))
                excess -= cut
        entry_live = self.st.orders("trade_id=? AND purpose='OPEN' AND status IN ('PENDING','PARTIAL','CANCEL_PENDING')", tid)
        if open_units == 0 and not entry_live:
            self.finalise(c, t, now, src)

    def finalise(self, c, t, now, src):
        tid = t["trade_id"]
        fills = self.st.fills("trade_id=?", tid)
        o_f = [f for f in fills if f["purpose"] == OPEN]
        c_f = [f for f in fills if f["purpose"] == CLOSE]
        units = sum(f["qty"] for f in c_f)
        c.execute("DELETE FROM reservations WHERE trade_id=?", (tid,))
        if units == 0:
            t.update(status="CANCELED", closed_at=iso(now), cancel_reason="exit before any entry fill")
            self.st.put_trade(c, t, now)
            self.emit(c, "PositionClosed", now, {"result": None, "status": "CANCELED",
                      "reason": t["exit"]["reason"]}, src=src, event_id=f"closed:{tid}")
            return
        e_avg = sum(f["qty"] * f["price"] for f in o_f) / sum(f["qty"] for f in o_f)
        x_avg = sum(f["qty"] * f["price"] for f in c_f) / units
        gross = gross_pnl(t["direction"], e_avg, x_avg, units)
        costs = sum(f["body"]["costs"]["total"] for f in fills)
        res = {"entry_side": open_side(t["direction"]), "exit_side": close_side(t["direction"]),
               "direction": t["direction"], "entry_avg": round(e_avg, 4), "exit_avg": round(x_avg, 4),
               "requested_units": t["requested_qty"], "entry_filled_units": sum(f["qty"] for f in o_f),
               "closed_units": units, "gross_pnl": round(gross, 2), "costs": round(costs, 2),
               "net_pnl": round(gross - costs, 2), "trigger_price": t["exit"].get("trigger_price"),
               "fills": [{"fill_id": f["fill_id"], "side": f["side"], "purpose": f["purpose"], "qty": f["qty"],
                          "price": f["price"], "ts": f["ts"], "model": f["body"]["evidence"].get("model")}
                         for f in fills]}
        t.update(status="CLOSED", closed_at=iso(now), result=res, mtm=None)
        self.st.put_trade(c, t, now)
        self.emit(c, "PositionClosed", now, {"result": res, "status": "CLOSED", "reason": t["exit"]["reason"],
                  "exception": t.get("unresolved")}, src=src, event_id=f"closed:{tid}")

    def poll(self, now):
        for tr in self.st.trades("status='EXITING'"):
            t = tr["body"]
            with self.st.tx() as c:
                t = self.st.trade(t["trade_id"])["body"]
                if t["status"] != "EXITING":
                    continue
                self.reconcile(c, t, now)
                t2 = self.st.trade(t["trade_id"])["body"]
                if t2["status"] == "EXITING":
                    self.unresolved_check(c, t2, now)

    def unresolved_check(self, c, t, now):
        cfg = self.e.cfg(t["config_version"])
        plan = t["exit_plan"]
        reason = None
        if t["mode"] == "intraday":
            dl = parse_ts(plan["intraday_deadline"]) + timedelta(seconds=plan.get("cutoff_grace_s", 600))
            if now >= dl:
                reason = ("intraday_cutoff_exception: exit could not be filled by the session deadline "
                          "(no usable quote); not relabelled as swing, no fill invented")
        else:
            exp = date.fromisoformat(t["expiry"])
            if not self.e.cal.is_session(exp):
                exp = self.e.cal.prev_session(exp)          # expiry on a holiday moves to the session before
            if now >= ist(exp, cfg["calendar"].get("session_close", "15:30")):
                reason = ("pre_expiry_exit_unfilled: closure could not be simulated before expiry; flagged for "
                          "reconciliation, no settlement manufactured, no roll")
            elif plan.get("pre_expiry_deadline") and now >= parse_ts(plan["pre_expiry_deadline"]) + timedelta(hours=1):
                reason = "pre_expiry_deadline passed with the exit still unfilled"
        if not reason or (t.get("unresolved") or {}).get("reason") == reason:
            return
        t["unresolved"] = {"reason": reason, "since": iso(now), "policy": plan.get("missing_quote_policy")}
        self.st.put_trade(c, t, now)
        src = dict(scan_id=t["scan_id"], mode=t["mode"], config_version=t["config_version"],
                   direction=t["direction"], trade_id=t["trade_id"], instrument_id=t["instrument_id"])
        self.emit(c, "ExitUnresolved", now, t["unresolved"], src=src)
        expired = reason.startswith("pre_expiry_exit_unfilled")
        if plan.get("missing_quote_policy") == "manual" or expired:
            for o in self.st.orders("trade_id=? AND purpose='CLOSE' AND status IN ('PENDING','PARTIAL')", t["trade_id"]):
                c.execute("UPDATE orders SET status='CANCELED', updated=? WHERE client_order_id=?",
                          (iso(now), o["client_order_id"]))
            t["status"] = "UNRESOLVED"
            self.st.put_trade(c, t, now)
