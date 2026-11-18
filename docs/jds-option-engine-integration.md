# JDS Option engine integration

The JDS Hub links to `/option-engine.html`. The module reuses calculation and
rendering hooks from the desk HTML, with identical feature scripts and research
JSON. Its JDS-specific module controller provides Monitor, Analyse and Reference
tool navigation. It does not load the standalone studio UI: no landing hero,
product directory, standalone footer, FAQ or external desk navigation.
It uses a JDS brand bar, Hub return link, typography and palette, and the existing
`jdtheme` preference. No trading rules changed.

Both editions read the existing public Realtime Database. The JDS page uses
the existing Firebase client gate and returns through `client.html` after
login. This is the existing client navigation gate, not new server-side
protection for the public research data.

## Updating

Run `python tools/build_jds_option_engine.py` after building NIFTY Desk.
It reads the JDS source at `C:/Users/Nisarg/Desktop/jds-wealth-website` and
stages a hosting release in `data/jds-option-integration`. Copy the generated
`hub.html`, `option-engine.html` and `option-engine-assets/` back into the JDS
source folder. Deploy the staged hosting configuration to project
`jds-wealth-management`. Do not deploy this configuration to `nifty-desk`.

The data feed is shared live; future frontend changes require regeneration
and deployment. Hosting stages web assets only, excluding ZIPs, PDFs, hidden
files and scripts. The original Hub is backed up in the staging directory.

## Verification

- Three existing dashboard integrity tests passed.
- Checked Hub link, auth gate inclusion, theme key, explicit database URL,
  expected feature IDs and byte-identical feature scripts/research JSON.
- Every generated inline JavaScript block passed Node syntax checking.
- Hosted preview redirects signed-out users to
  `client.html?next=option-engine.html`.
- Authenticated navigation remains to be verified with the user's session.
- Local visual test server was unreachable from the browser; its auth-free
  fixture is local only and excluded from the hosting release.
