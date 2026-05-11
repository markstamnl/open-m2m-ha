# Changelog

## [1.1.0] - 2026-05-11

- API: new `GetSIM`, `GetProductInfo`, and `GetUsageTotals` endpoints wired in `api.py` / `const.py` with per-refresh caps (`MAX_SIM_DETAIL_ENRICHMENT_PER_REFRESH`, `MAX_PRODUCT_INFO_FETCHES`, `MAX_USAGE_TOTALS_SUBSCRIPTION_TRIES`).
- `GetSIMs` parsing: accept a JSON list, a single SIM object (keyed by `ICCID` / `MSISDN` / `description`), or an id→row map (ICCID injected from digit keys when missing); odd shapes now log at debug.
- `GetSIM` enrichment: per-refresh capped follow-up calls fill rows that still lack a non-empty `description` after normalization.
- `GetSubscriptions`: tolerate single-object payloads, merge rows from every payload level (top-level and wrappers such as `data`/`result`), and deduplicate by `subscription_id`.
- `GetSubscriptionInfo`: resolve `SubscriptionInfo` from dict / single-element list / flat body, including `data`/`result` wrappers; product info now also picked up from `UpgradeOptions → ProductInfo` casing variants.
- `GetProductInfo` merge: attach full row as `product_info_detail` on the subscription; expose `product_id`, pricing (`monthly`, `onetime`, `prorate_price`, `nextmonth_price`, `price`) in EUR and `*_fromdate` / `*_todate` as entity attributes on portal status and product-description sensors; only fill `product_name` / `product_description` / `data_included` when missing.
- `GetUsageTotals`: new account-level **Usage (this month)** / **Usage (last month)** sensors (`DATA_SIZE`, bytes; kB → bytes conversion) using current and previous **UTC** calendar month, with optional per-subscription retries when account-level `CDR` is empty.
- SIM device naming: device name now uses the portal SIM `description` / `sim_description` (from `GetSIMs`, `GetSIM`, or merged subscription rows); falls back to `Open-M2M <ICCID tail>`. Subscription, SIM-sensor, and merged-databundle device names share the same resolver.
- Translations: added EN/NL entity names for `usage_total_current_month` and `usage_total_previous_month`.
- Docs: README updated with API assumptions for `GetSubscriptions` / `GetUsageTotals`, per-cycle call breakdown, new endpoint list, and expanded field mapping (`product_id`, `product_info_detail`).

## [1.0.1] - 2026-05-11

- Brand: added `dark_icon.png`, `icon@2x.png`, `logo@2x.png` (from 256×256 sources); consolidated misplaced assets from repo `brand/` and root into `custom_components/open_m2m/brand/`; optional `brand/README.md` mapping source filenames.
- Translations: expanded English entity strings; added Dutch (`nl.json`) for config flow and entity names.
- Sensors: subscription/databundle-related updates aligned with translation keys (see `sensor.py` diff).

## [1.0.0] - 2026-05-11

First stable major release (semver from 0.1.x).

- Subscriptions: richer portal status attributes; consistent ICCID normalization for device merge with SIMs.
- New subscription sensors: start, MSISDN, IP/hostname/RADIUS, product description, monthly, DataProduct included, IMEI lock diagnostic (off by default).
- Databundles: per-subscription fetch, link/dedupe/cap (`MAX_DATABUNDLES`), entities on SIM or subscription device.
- Data used/allowance: clearer sourcing from volume groups, limits, products, DataProduct (MB → bytes).
- Docs: README updates for databundles, subscription↔SIM, field mapping.

Earlier `main` history includes balance/subscription parsing fixes and 2026.3+ brand assets (`a3951ee`).
