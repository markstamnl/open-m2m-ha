# Changelog

## [1.0.0] - 2026-05-11

First stable-style major release (semver jump from 0.1.x).

- **Subscriptions:** Portal status and related sensors expose richer attributes (`merged_onto_sim`, identifiers, IP/hostname/RADIUS, product and data-limit fields, `dataproduct_summaries` when present). ICCIDs are normalized consistently so subscription rows merge onto the same Home Assistant device as the matching SIM when linked.
- **New subscription-scoped sensors:** Start time, MSISDN, IP/hostname/RADIUS status, product description, monthly plan flag, DataProduct data included, and an IMEI lock diagnostic sensor (disabled in the UI by default; avoids storing digit-only IMEI-like strings as lock state).
- **Databundles:** Fetches and normalizes `GetDatabundles` rows per subscription, links rows via explicit subscription id and/or volume group overlap (with deterministic fallbacks and debug logging when ambiguous), deduplicates, and caps exposed bundles (`MAX_DATABUNDLES` in `const.py`). Per-bundle status, start, expire, and monthly (EUR label) entities attach to the SIM device when ICCID is resolved, otherwise to the subscription device.
- **Data allowance / usage:** Clearer sourcing from volume groups, `limit_data`, product `data_included`, and first DataProduct `data_included` (MB to bytes) with matching sensor attributes.
- **Docs:** README expanded with databundle UX, subscription↔SIM device model, and API→coordinator→UI field mapping.

Prior history on `main` includes balance/subscription parsing fixes and brand assets/layout for Home Assistant 2026.3+ (`a3951ee`).
