# Open-M2M (custom integration)

Portal API integration for [Open-M2M](https://portal.open-m2m.com/). **Install and My HA badges:** [repository README](https://github.com/markstamnl/open-m2m-ha/blob/main/README.md).

## Entities (overview)

| Area | Device / model | Notes |
|------|----------------|--------|
| **Account** | Hub device *Open-M2M* | Balance (parsed from `ClientInfo` / alternates), SIM count, subscription count, aggregate subscriptions data remaining (best-effort). |
| **SIM** | One device per ICCID (`sub_<id>` if ICCID missing) | Subscription status, data usage (kB) if present, plan data included (MB) when usage absent but product has `data_included`. |
| **Subscription (portal)** | Merges onto SIM device when ICCID matches (uppercase-normalized); else `sub_<subscription_id>` | Portal status, data used/allowance (`DATA_SIZE`), bundle text, auto renew, start/expire timestamps, MSISDN, IP, hostname, RADIUS status, product description, monthly, DataProduct included, IMEI lock (**diagnostic**, default off). |
| **Databundles** | Same SIM device if ICCID resolved; else subscription device | Up to `MAX_DATABUNDLES` (20) per refresh: status, start, expire, monthly (EUR label only). Linking: source subscription, explicit `subscription_id` on row, volume-group overlap (deterministic fallback + debug log if ambiguous). |
| **Services** | — | `open_m2m.suspend_sim` / `open_m2m.unsuspend_sim`: `iccid` required; `config_entry_id` if multiple entries. Each call refreshes the coordinator. |

New SIMs/bundles after first successful poll may need **Reload integration** until dynamic discovery exists.

## Subscription ↔ SIM

| Question | Behavior |
|----------|----------|
| Same HA device as SIM? | When ICCID is known, subscription entities use the same `DeviceInfo` identifier as the SIM device (ICCID uppercase). |
| Separate device? | If ICCID (and MSISDN/IMSI match) cannot be resolved: `sub_<subscription_id>`. |
| Merged onto SIM? | Attribute `merged_onto_sim` on normalized rows and portal status sensor. |

## Polling & API

- **Interval:** `DEFAULT_SCAN_INTERVAL` in `const.py` (900 s). **Reload integration** or restart for immediate refresh after config/asset changes.
- **Calls per cycle:** `GetAccountInfo`, `GetSIMs`, `GetSubscriptions`; per subscription (concurrency 3): `GetDatabundles`, `GetVolumeGroups`; up to 10 `GetSubscriptionInfo` for rows still missing usage/allowance, `monthly`, or IMEI lock.
- **Success:** `APIcode == 1000` and/or success-like `APIstatus` (see `api.is_api_success_payload` for legacy keys).
- **Units:** MB-like API values → bytes for `SensorDeviceClass.DATA_SIZE`. Wrong magnitudes possible if your tenant uses different units.
- **Matching:** Subscription ↔ SIM by ICCID; else MSISDN/IMSI match borrows ICCID from SIM row.

OpenAPI reference: [OpenM2M v1.0.7](https://app.swaggerhub.com/apis-docs/Open-M2M/OpenM2M/v1.0.7). Endpoints: `GetAccountInfo`, `GetSIMs`, `GetSubscriptions`, `GetDatabundles`, `GetVolumeGroups`, `GetSubscriptionInfo`, `SuspendSIM`, `UnsuspendSIM`.

## Field mapping (short)

Portal rows → coordinator keys → sensors/attributes: `subscription_id`, `ICCID`/`iccid`, `MSISDN`, `IMSI`, `ip`/`hostname`/`radius_status`, `status`, `start_date`/`expire_date`, `auto_renewal`, `sim_description`, `product_description`/`product_name`, `limit_data` / `data_included` / volume groups / DataProduct `data_included`, `imei_lock`, databundle ids and `volumegroup`. **PIN/PUK from `SIMinfo` are not** copied into coordinator state or subscription attributes.

## Branding

Place **`brand/icon.png`** and **`brand/logo.png`** (lowercase names; optional dark/`@2x` per [home-assistant/brands](https://github.com/home-assistant/brands/blob/master/README.md)) under this integration folder. **Home Assistant 2026.3+** serves them from `/api/brands/integration/open_m2m/…`. Older cores need the brands repo or an upgrade. After adding or changing images, **restart** Home Assistant (reload is not enough). Confirm paths under `<config>/custom_components/open_m2m/brand/`.

## Secrets

API key in config entry data only (password selector); not logged. Diagnostics should redact `api_key` / `apikey`. **SIM PIN/PUK** are not exposed via this integration’s normalized paths or the IMEI-lock sensor. Raw `Productinfo` on SIM entities may still be unredacted—treat as sensitive if your portal embeds secrets there.

## Local development

Copy this folder to `<config>/custom_components/open_m2m/`, restart, then add the integration from **Settings → Devices & services**.
