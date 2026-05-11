# Changelog

## [1.0.0] - 2026-05-11

First stable major release (semver from 0.1.x).

- Subscriptions: richer portal status attributes; consistent ICCID normalization for device merge with SIMs.
- New subscription sensors: start, MSISDN, IP/hostname/RADIUS, product description, monthly, DataProduct included, IMEI lock diagnostic (off by default).
- Databundles: per-subscription fetch, link/dedupe/cap (`MAX_DATABUNDLES`), entities on SIM or subscription device.
- Data used/allowance: clearer sourcing from volume groups, limits, products, DataProduct (MB → bytes).
- Docs: README updates for databundles, subscription↔SIM, field mapping.

Earlier `main` history includes balance/subscription parsing fixes and 2026.3+ brand assets (`a3951ee`).
