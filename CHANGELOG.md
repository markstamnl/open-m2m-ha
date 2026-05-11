# Changelog

## [1.1.0] - 2026-05-11

- **GetSIMs / subscriptions:** tolerate list, single SIM object (`ICCID` / `MSISDN` / `description`), or id→row maps; merge and dedupe subscription rows across payload shapes (`data` / `result`, top-level).
- **GetSIM:** capped per-refresh follow-up calls to fill missing SIM `description` after normalization.
- **GetSubscriptionInfo:** resolve nested dict/list and `data` / `result` wrappers; product from `UpgradeOptions` → `ProductInfo` casing variants.
- **GetProductInfo:** merge full row as `product_info_detail`; expose `product_id`, pricing, and date-range attributes on portal/product sensors when missing from other sources.
- **GetUsageTotals:** account sensors for current and previous **UTC** calendar month (`DATA_SIZE`); optional per-subscription retries when account `CDR` is empty.
- **Naming:** SIM / subscription / databundle device names prefer portal `description` / `sim_description`, else `Open-M2M` + ICCID tail.
- **i18n:** EN/NL strings for usage-total entities.

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
