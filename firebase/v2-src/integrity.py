"""Shared final build corrections for the classic-derived dashboard."""
def apply(s):
    import re
    replacements = {
        "const lots = Math.max(0, Math.min(byMargin, byRisk));":
        "const lots = 0; // Research only: no strategy has passed deployment validation.",
        "recommended lots": "approved live lots (research only)",
        "worst case at stop": "planned stop loss (not a loss cap)",
        "const noEdge = !(st.net_ev_rs > 0);":
        "const noEdge = true; // Binary Kelly does not model this strategy's return distribution.",
        "pnl = +t.pnl_now_rs": "pnl = t.net_rs == null ? NaN : +t.net_rs",
        "Recommended &mdash; intraday": "Daily intraday baseline &mdash; paper",
        "The options market is pricing a typical move of about":
        "The selected volatility benchmark implies a model move of about",
        "real bid/ask prices and real charges": "recorded bid/ask quotes and estimated order charges",
        "The other three are measured.":
        "Movement is modelled too; volatility/repricing is a residual, not an independent observation.",
        "Ranked on net EV after the corrected 0.0495% exchange rate and measured slippage.":
        "Ranked on model EV after estimated costs; these are unvalidated research rankings.",
        "which is why it so often behaves like resistance":
        "which identifies concentrated open interest, not demonstrated resistance",
        "but expiries do tend to gravitate toward it, which is why the distance is worth watching":
        "any predictive value must be tested out of sample",
    }
    for old, new in replacements.items():
        s = s.replace(old, new)
    s = re.sub(r"(?:Net )*P&amp;L \$\{live\?'now':'settled'\}",
               "Net P&amp;L ${live?'now':'settled'}", s)
    s = s.replace('Daily intraday baseline &mdash; paper', 'Control baseline &mdash; old 09:20 straddle')
    s = s.replace('The daily paper trade, in full', 'The old straddle control, in full')
    s = s.replace('Today &mdash; paper', 'Control baseline &mdash; paper')
    s = re.sub(r'The other three\s+are measured\.',
               'Movement is modelled too; volatility/repricing is a residual, not an independent observation.', s)
    s = s.replace('spread and real charges.', 'spread and estimated order charges.')
    if 'Contractual loss per paper lot' not in s:
        marker = '<div class="kv"><span>planned stop loss (not a loss cap)</span>'
        row = ('<div class="kv"><span>Contractual loss per paper lot</span><span class="neg">'
               '${rec.defined_risk && Number.isFinite(rec.max_loss_pts) ? rs(Math.abs(rec.max_loss_pts) * (S.lot || 65)) : "Not capped by a stop"}'
               '</span></div>\n    ')
        s = s.replace(marker, row + marker)
    s = re.sub(r'\$\{nrat\} sessions with every one on the same side of\s+1\.0 is a streak, not a distribution\. It takes a session above 1\.0 to learn\s+anything about how big the other tail is\.',
               'This sample does not establish the size of rare losses. A realised/implied ratio below one is not a profitability test.', s)
    s = re.sub(r'The median and the mean disagreeing <b>is</b> the finding\.\s+Most sessions pay a little; a few take it all back\.',
               'Read average win, average loss and net expectancy together; the observed win rate alone does not measure an edge.', s)
    old = "${n < floor ? `<span class=\"hl\">Too few sessions to call this a result yet.</span>` : ''}</p></div>"
    new = ('<span class="hl">${floor} sessions is a collection milestone, not validation.</span> '
           'Clean verified scheduled-entry sample: <b>${sm.clean_sample?.settled ?? 0}</b>; '
           'unverified or excluded: <b>${sm.unverified_or_excluded ?? n}</b>. '
           'Historical totals retain all recorded sessions and original cost assumptions.</p></div>')
    s = s.replace(old, new)
    # Both paths (generated page and template rebuild) receive the same safety note.
    marker = '<main>'
    note = ('<aside class="banner" id="research-policy"><b>Daily intraday paper research.</b> '
            'New NIFTY opening-spread rules start 26 October: entry 09:45, stop/target or exit by 15:00. '
            'One entry per valid session. The old 09:20 straddle remains a control. '
            'No strategy is approved for live capital; historical tests do not establish profitability.</aside>')
    if 'id="research-policy"' in s:
        s = re.sub(r'<aside class="banner" id="research-policy">.*?</aside>', note, s, flags=re.S)
    else:
        s = s.replace(marker, note + '\n' + marker, 1)
    return pwa(s)


PWA_HEAD = '''<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0b1020">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Nifty Desk">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="32x32" href="/icons/favicon-32.png">
<script src="/pwa.js" defer></script>'''


def pwa(s):
    """Installable-app tags (manifest, icons, service-worker loader). Idempotent."""
    if 'rel="manifest"' in s:
        return s
    viewport = '<meta name="viewport" content="width=device-width, initial-scale=1">'
    if viewport not in s:
        raise ValueError('viewport meta not found; cannot place PWA tags')
    return s.replace(viewport, viewport + '\n' + PWA_HEAD, 1)


if __name__ == '__main__':
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / 'public' / 'index.html'
    p.write_text(apply(p.read_text(encoding='utf-8')), encoding='utf-8', newline='\n')
