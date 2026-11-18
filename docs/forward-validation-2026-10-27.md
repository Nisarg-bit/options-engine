# Frozen candidate forward paper validation

Installed and verified the isolated `options-confirmation-forward.timer` and service. First eligible Indian-market session: 27 October 2026. Runs weekdays at 15:50 and 16:10 IST (10:20 and 10:40 UTC). The second run is idempotent; it cannot replace a recorded outcome. Catch-up evaluates available unrecorded sessions after the start date.

This is automatic post-session replay of rules frozen before the session. It is not live order submission, live signal observation, or fill validation. No Zerodha orders are submitted. The active paper policy, API and publisher were not replaced or restarted. Collector PID remained 2865637. Initial service result: success; timer active; zero forward sessions at installation.

## Frozen rules and integrity

- Continuation confirmed by two consecutive completed minute observations outside the 09:20–09:44 parity-forward range.
- Buy one same-day instrument-master lot of the signal-time ATM call/put in the following minute. No short leg.
- Net stop at 35% of initial premium; no profit target; exit at 15:00. Observed fills can exceed the stop loss.
- If no confirmation occurs by 13:29, the declared range-midpoint direction fallback enters at 13:30. Missing/invalid data never forces a trade.
- Quote-quality and displayed-size checks apply. Invalid observations remain explicitly ineligible.
- Four scenarios: quoted bid/ask, +1 adverse point per side, one extra minute entry delay, and both stresses together.
- Checksums freeze 14 loaded local Python dependencies. A dependency change stops evaluation pending review instead of silently mixing rule versions.
- Each record includes observation timestamp, configuration fingerprint and source-bar checksums. The first result for a date is immutable.

Runtime directory: `/home/ubuntu/confirmation-forward-v1`. Results: `/home/ubuntu/data/journal/confirmation-forward-v1/status.json`, `forward_journal.json` (created on first eligible observation), and separate `retrospective_checks.json`.

## Additional retrospective check: 26 October

This session was complete before this forward-validation installation and is excluded from its sample. Same frozen continuation/long rules, one lot of 65, expiry 27 October. Signal 09:46; ATM 22550 CE; quoted entry 09:47 and exit 15:00 IST. Quoted premium risk Rs 7,218.25 before charges.

| Scenario | Net after estimated charges, Rs |
|---|---:|
| Quoted bid/ask | +7,493.64 |
| Additional adverse slippage | +7,363.74 |
| Additional minute entry delay | +8,004.12 |
| Slippage and delay together | +7,874.22 |

All four passed data eligibility checks. Delay improved this particular entry price; the delay scenario is not guaranteed to be worse and does not establish execution safety. One additional positive session does not establish profitability.

## Review and limitations

Review after 20 eligible forward sessions as an initial checkpoint, not an automatic live-activation threshold. Assess coverage, fallback performance, net returns under combined stress, drawdown and profit concentration. No calendar-completeness guarantee is made: absent source sessions cannot be distinguished from holidays without a separate exchange-calendar audit; status exposes the latest available date.

Live readiness remains false. Explicit capital, daily account-loss and monthly drawdown limits are still needed for real-money sizing. Current infrastructure validates the frozen payoff on recorded quotes; live execution timing and broker fills need a separate validation stage.

To inspect operations: `systemctl status options-confirmation-forward.timer`, `journalctl -u options-confirmation-forward.service`, and the status JSON. To stop this research schedule only: `sudo systemctl disable --now options-confirmation-forward.timer`. Do not edit the manifest to bypass a dependency mismatch; investigate and version any intentional strategy change.
