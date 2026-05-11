# Open-M2M (custom integration)

Portal API for [Open-M2M](https://portal.open-m2m.com/). **Install / My HA:** [repository README](https://github.com/markstamnl/open-m2m-ha/blob/main/README.md).

UI names may shorten after updates; **`entity_id` / `unique_id` unchanged**. Rename in **Settings → Devices & services → Entities** if needed. Strings: `translation_key` + `translations/en.json`, `translations/nl.json` (`entity.sensor`).

## Entities

| Area | Device | Notes |
|------|--------|--------|
| **Account** | Hub *Open-M2M* | Balance, SIM/subscription counts, aggregate data remaining (best-effort), usage totals (`GetUsageTotals`, UTC months). |
| **SIM** | Per ICCID (`sub_<id>` if missing) | Name from portal **`description`** / `sim_description`; else **`Open-M2M` + ICCID tail**. Status, usage (kB), plan `data_included` (MB) when applicable. |
| **Subscription** | Merges to SIM when ICCID matches; else `sub_<subscription_id>` | Portal status, data used/allowance, bundles, renewals, timestamps, MSISDN, IP/hostname/RADIUS, product fields, IMEI lock (**diagnostic**, default off). |
| **Databundles** | SIM or subscription device | Up to `MAX_DATABUNDLES` (20)/refresh; linking via ICCID, `subscription_id`, volume groups. |
| **Services** | — | `open_m2m.suspend_sim` / `open_m2m.unsuspend_sim`: `iccid` required; `config_entry_id` if multiple entries. |

New SIMs/bundles after first poll: **Reload integration** until dynamic discovery exists.

## Subscription ↔ SIM

| | |
|--|--|
| Same HA device as SIM? | Yes when ICCID known (uppercase-normalized `DeviceInfo`). |
| Separate device? | `sub_<subscription_id>` when ICCID/MSISDN/IMSI cannot tie to a SIM. |
| Merged | `merged_onto_sim` on normalized rows / portal status sensor. |

## API (assumptions)

- **Success:** `APIcode == 1000` and/or success-like `APIstatus` (`api.is_api_success_payload`).
- **GetSubscriptions:** `Subscriptions` may be an array or one object with row-shaped keys; rows merged/deduped from wrappers (`data`, `result`).
- **GetSubscriptionInfo:** `SubscriptionInfo` / casing variants; optional `data`/`result`; product under `UpgradeOptions` → `ProductInfo`. `SIMinfo` merged for ICCID/MSISDN/description only (not PIN/PUK).
- **GetSIMs:** array, single object, or id→map; optional capped **`GetSIM`** for missing `description`.
- **GetProductInfo:** merged as `product_info_detail`; caps in `const.py` (`MAX_PRODUCT_INFO_FETCHES`).
- **GetUsageTotals:** body `apikey`, `year`, `month` (strings); optional `subscription_id` / `ICCID`. Coordinator: current + previous UTC month; subscription-scoped retries when account `CDR` empty (`MAX_USAGE_TOTALS_SUBSCRIPTION_TRIES`).
- **Polling:** `DEFAULT_SCAN_INTERVAL` (`const.py`, 900 s). Per cycle: `GetAccountInfo`, `GetSIMs`, `GetSubscriptions`; per subscription (concurrency 3): `GetDatabundles`, `GetVolumeGroups`; capped `GetSubscriptionInfo`, `GetProductInfo`, `GetSIM`, `GetUsageTotals` as above.
- **Units:** MB-like → bytes for `DATA_SIZE`; tenant-specific magnitudes may differ.

OpenAPI: [OpenM2M v1.0.7](https://app.swaggerhub.com/apis-docs/Open-M2M/OpenM2M/v1.0.7). Endpoints: `GetAccountInfo`, `GetSIMs`, `GetSIM`, `GetSubscriptions`, `GetDatabundles`, `GetVolumeGroups`, `GetSubscriptionInfo`, `GetProductInfo`, `GetUsageTotals`, `SuspendSIM`, `UnsuspendSIM`.

## Field mapping (short)

Coordinator keys → sensors/attributes: `subscription_id`, `ICCID`/`iccid`, `MSISDN`, `IMSI`, `ip`/`hostname`/`radius_status`, `status`, `start_date`/`expire_date`, `auto_renewal`, `sim_description`, `product_description`/`product_name`, `product_id`, `product_info_detail`, usage/limit/volume-group/DataProduct fields, `imei_lock`, databundle ids / `volumegroup`. **PIN/PUK** not stored from `SIMinfo`.

## Branding

`brand/icon.png`, `brand/logo.png` under this folder ([home-assistant/brands](https://github.com/home-assistant/brands/blob/master/README.md)). HA **2026.3+** serves `/api/brands/integration/open_m2m/…`. After image changes: **restart** (reload insufficient).

## Secrets

API key in config entry only; diagnostics should redact `api_key`/`apikey`. **SIM PIN/PUK** not on normalized paths or IMEI-lock sensor. Raw `Productinfo` on SIM entities may be sensitive.

## Local development

Copy this folder to `<config>/custom_components/open_m2m/`, restart, add integration from **Settings → Devices & services**.
