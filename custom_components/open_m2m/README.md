# Open-M2M (Home Assistant custom integration)

Custom integration for the [Open-M2M](https://portal.open-m2m.com/) portal API.

## Branding

- **`brand/icon.png`** and **`brand/logo.png`** — Home Assistant **2026.3+** serves custom integration images from a **`brand/`** directory inside the integration package (`/api/brands/integration/<domain>/<image>`). The loader sets `has_branding` only when that folder exists. Filenames must be lowercase `icon.png` / `logo.png` (optional: `dark_icon.png`, `dark_logo.png`, `@2x` variants per [brands](https://github.com/home-assistant/brands/blob/master/README.md) conventions). Recommended sizes: square **icon** ~128×128 (yours may differ); **logo** often ~250×100 for wide headers—the UI scales PNGs.
- **Older Home Assistant** — the UI used the brands CDN only; local files beside `manifest.json` were never used. Either upgrade to **2026.3+** or open a PR to [home-assistant/brands](https://github.com/home-assistant/brands) for domain `open_m2m`.

### Logo or icon missing in the UI

1. **Full restart** — add or change images, then **restart Home Assistant** (reload YAML is not enough for loader / Brands cache).
2. **Paths on disk** — confirm files exist at **`/config/custom_components/open_m2m/brand/icon.png`** and **`.../brand/logo.png`** (Supervisor / Container: path is still under your config volume).
3. **HACS** — after download, the tree should match this repo: `custom_components/open_m2m/brand/`. If you copied only part of the folder, branding will be missing.
4. **Browser** — hard refresh or another browser/profile to rule out cached 404s from the brand API.
5. **Core version** — local bundled branding requires **2026.3** or newer; below that, expect a generic placeholder unless the domain is on the CDN.

## Current features

- **Config flow** — API key (stored as `api_key`), validated with `POST …/GetAccountInfo` and `apikey=<key>` (`application/x-www-form-urlencoded`).
- **Polling hub** — `DataUpdateCoordinator` refreshes on `DEFAULT_SCAN_INTERVAL` from `const.py` (**15 minutes = 900 seconds** by default), calling `GetAccountInfo`, `GetSIMs`, and subscription/bundle endpoints below (OpenAPI **v1.0.7** paths; not `GetSIMList`). To pull fresh portal data sooner, **reload the integration** (*Settings → Devices & services → Open-M2M → ⋮ → Reload*) or **restart Home Assistant** (same effect on the next coordinator run after startup).
- **Sensors**
  - **Account** (device: *Open-M2M* / Account)
    - **Account balance** — parsed from `ClientInfo` / `client_info` (and common alternate keys) when `balance` / `saldo` / `credit` are numbers **or** numeric strings (including **comma decimals**, e.g. `8,71`). Attributes include `client_info` when present, `account_balance_raw` when a raw value was parsed, and `raw_account_keys` when the balance is still *Unknown* to help diagnose field names.
    - **SIM count** — number of SIM rows parsed from the `GetSIMs` payload.
    - **Subscription count** — number of rows parsed from `GetSubscriptions`.
    - **Subscriptions data remaining (aggregate)** — sum of `(data_allowance_bytes − data_used_bytes)` over subscriptions where **both** values are known; otherwise *Unknown*. This is a best-effort hint, not a billing statement.
  - **Per SIM** (one device per ICCID, or `sub_<subscription_id>` if ICCID is missing)
    - **Subscription status** — `subscription_status`, or `status` / `SIMstatus`, or a coarse value from `archived`.
    - **Data usage (kB)** — only created if the SIM object exposes a recognizable usage field (e.g. `usage_kb`, nested `usage` / `stats` maps). Otherwise no usage entity is added.
    - **Plan data included (MB)** — only when no usage field was found but `Productinfo.data_included` exists (per OpenAPI `Product`; unit treated as **MB** in this integration—confirm against your account if needed).
  - **Per subscription (portal)** — one **device** per subscription (see **Subscription ↔ SIM in Home Assistant** below). Entity names use the ICCID or `sub_<id>` as the short label.
    - **Portal status** — `status` from `GetSubscriptions` / merged `GetSubscriptionInfo`. **Attributes** (non-exhaustive): `merged_onto_sim`, `subscription_id`, `iccid`, `msisdn`, `imsi`, `ip`, `hostname`, `radius_status`, `sim_description`, `product_description`, `product_name`, `monthly`, `limit_data_mb`, `product_data_included_mb`, `dataproduct_summaries` when present.
    - **Data used** — `SensorDeviceClass.DATA_SIZE`, bytes, aggregated from per-subscription `GetVolumeGroups` + `GetDatabundles` (see assumptions).
    - **Data allowance** — same device class; from summed `volume` on matched volume groups and/or `limit_data` / product `data_included` / first **DataProduct** `data_included` (MB→bytes). **Attributes**: `limit_data_mb`, `product_data_included_mb`, `product_data_included_bytes`, `dataproduct_data_included_mb`, `dataproduct_data_included_bytes`, `dataproduct_summaries`.
    - **Bundle** — comma-separated `product_description` values from databundles; attributes include `bundle_summaries` and `dataproduct_summaries`.
    - **Auto renew** — `on` / `off` from `auto_renewal` when present.
    - **Expire time** — `SensorDeviceClass.TIMESTAMP` when `expire_date` is a positive Unix timestamp (interpreted as UTC).
    - **Start time** — `SensorDeviceClass.TIMESTAMP` when `start_date` is a positive Unix timestamp (UTC). New `unique_id` suffix: `_portal_start`.
    - **MSISDN** — text; suffix `_portal_msisdn`.
    - **IP** / **hostname** / **RADIUS status** — text when present on the subscription row or merged info; suffixes `_portal_ip`, `_portal_hostname`, `_portal_radius_status`.
    - **Product description** — text from `product_description` / `product_name`; suffix `_portal_product_description`.
    - **Monthly plan** — `yes` / `no` when `monthly` is known; suffix `_portal_monthly`.
    - **DataProduct data included** — bytes from the first parsed **DataProduct** on databundle rows (does not replace volume-based allowance when that exists); suffix `_portal_dataproduct_data_included`.
    - **IMEI lock** — diagnostic sensor (`EntityCategory.DIAGNOSTIC`), **disabled in the UI by default** (`entity_registry_enabled_default=False`); state `locked` / `unlocked` from bool, or a short non-numeric label. Digit-only strings resembling an IMEI are **not** stored. Suffix `_diag_imei_lock`.
  - **Per databundle (linked rows)** — up to **`MAX_DATABUNDLES` (20 in `const.py`)** bundles per refresh after dedupe; see **Databundles and devices** below. Four entities per bundle on the **SIM** device when ICCID is resolved, else on the **subscription** device: `_databundle_status` (state = status; attrs include `product_description`, ids, `volumegroup_id`, `merged_onto_sim`), `_databundle_start`, `_databundle_expire` (timestamp), `_databundle_monthly` (numeric; unit shown as EUR, not a monetary device class).
- **Services** — `open_m2m.suspend_sim` and `open_m2m.unsuspend_sim` (`services.yaml`): `iccid` required; `config_entry_id` required if multiple Open-M2M entries are loaded. Each call triggers a coordinator refresh.

SIM and databundle entities are created from the **first successful** coordinator refresh when the sensor platform loads. New SIMs or bundles that appear only on a later poll may require reloading the integration entry until dynamic discovery is added.

### Databundles and devices (UX model)

- **Where they appear:** Each SIM device (ICCID identifier) shows the usual SIM sensors plus **portal subscription** sensors when the subscription row was linked to that ICCID. **Databundle** sensors are added on that **same SIM device** when `GetSubscriptions` / merge logic resolved an ICCID and the SIM exists in `GetSIMs`. If there is no ICCID, databundle sensors use the **subscription** device (`sub_<subscription_id>`) and still point **via** the Open-M2M account hub.
- **Linking (assumptions):** `GetDatabundles` is called **per** `subscription_id`. Each returned row is normalized (`databundle_id`, `product_description`, `status`, `start_date`, `expire_date`, `monthly`, `volumegroup` as id or nested object). **Primary link:** rows are attributed to the subscription whose API call returned them (`source_subscription_id`). **Override:** if the row includes a `subscription_id` (or `SubscriptionID` / …) that matches another subscription, that subscription wins. **Volume group:** if `volumegroup` matches a **volume group id** returned by `GetVolumeGroups` for one or more subscriptions, those subscriptions are **candidates**; if several match, the integration prefers the row’s own `subscription_id`, then the **source** subscription from the fetch, then the first stable candidate (with a debug log when ambiguous). **Assumption:** the example payload only shows `volumegroup`; the API may also return an explicit subscription id on a bundle — if it does, it is preferred when valid.
- **Entity ID pattern:** `{config_entry_id}_{slug}_databundle_{kind}` where `slug` is derived from `device_identifier` + `databundle_id`, or from `device_identifier` + `volumegroup_id` + `linked_subscription_id` when `databundle_id` is missing. `{kind}` is one of `status`, `start`, `expire`, `monthly`.
- **Limitations:** linking without ICCID uses the subscription device, not a SIM. Ambiguous shared volume groups may attach to a deterministic “first” candidate. More than **20** linked databundles per refresh are truncated. `monthly` is treated as a plain number with EUR **label** only; confirm billing currency in the portal.

## Subscription ↔ SIM in Home Assistant

| Question | Behavior in this integration |
|----------|------------------------------|
| Is a subscription “linked” to a SIM **in the UI**? | Subscription entities use `DeviceInfo(identifiers={(DOMAIN, device_identifier)})`. When an ICCID is known, `device_identifier` is that ICCID **normalized to uppercase**, matching the **per-SIM** device identifier (SIM sensors also normalize ICCID to uppercase). **Then** the subscription sensors and SIM sensors appear under the **same** HA device. |
| When are they **separate** devices? | If no ICCID can be resolved (and MSISDN/IMSI match also failed), `device_identifier` is `sub_<subscription_id>` — a **subscription-only** device with no shared identifier to the SIM device. |
| How do I see if a row matched a SIM ICCID from `GetSIMs`? | Each normalized subscription row includes **`merged_onto_sim`** (`true` when the ICCID exists on the SIM list). Also exposed on the **portal status** sensor attributes. |

Previously, mixed-case ICCIDs could split one physical SIM across two devices; SIM and subscription identifiers now use the same normalization rule as the coordinator’s ICCID set.

## Portal field mapping (API → coordinator → UI)

Sources: **`GetSubscriptions`** row, per-subscription **`GetDatabundles`** / **`GetVolumeGroups`**, optional **`GetSubscriptionInfo`** (merged with defensive key casings). MB values are converted to **bytes** where sensors use `SensorDeviceClass.DATA_SIZE`.

| API area (examples) | Coordinator keys | Home Assistant exposure |
|--------------------|-------------------|-------------------------|
| `subscription_id` / `SubscriptionID` | `subscription_id` | Attributes; entity labels |
| `ICCID` / `iccid`; `SIMinfo` | `iccid`, `device_identifier` | Device merge; MSISDN sensor; portal attrs |
| `MSISDN` / `msisdn`; `SIMinfo` | `msisdn` | MSISDN sensor; portal attrs (merge) |
| `IMSI` / `imsi` | `imsi` | Portal attrs |
| `ip`, `hostname`, `radius_status` (+ camelCase variants) | `ip`, `hostname`, `radius_status` | Dedicated sensors; portal attrs |
| `status` | `status` | Portal status entity state |
| `start_date` / `expire_date` (+ `StartDate` / `ExpireDate`) | `start_date`, `expire_date` (int epoch) | Start / expire **timestamp** sensors |
| `sim_description` | `sim_description` | Device name parts; portal attrs |
| `product_description` | `product_name`, `product_description` | Product description sensor; portal attrs |
| `auto_renewal` | `auto_renewal` (bool) | Auto renew sensor |
| `monthly` / `Monthly` (row or `SubscriptionInfo`) | `monthly` (bool) | Monthly sensor; portal attrs |
| `imeilock` / `imei_lock` / … | `imei_lock` (bool or short string) | Diagnostic IMEI lock sensor only (no full IMEI) |
| `limit_data` / `data_included` (subscription or product) | `limit_data_mb`, `data_allowance_bytes` | Data allowance sensor + attrs |
| `Productinfo` / `ProductInfo` / `product_info` | `product_name`, `product_description`, `product_data_included_mb`, `product_data_included_bytes` | Allowance attrs; product description sensor |
| Databundle row + nested **`DataProduct`** (`name`, `data_included`, `monthly`, `onetime`) | `dataproduct_summaries`, `dataproduct_data_included_mb`, `dataproduct_data_included_bytes` | DataProduct included sensor; bundle / allowance attrs |
| `SIMinfo` | ICCID, MSISDN, description only | **PIN and PUK are never copied** into coordinator state or subscription attributes |

## Install (dev)

Copy this folder to `<config>/custom_components/open_m2m/` in your Home
Assistant install, restart, then add **Open-M2M** from
*Settings → Devices & services → Add integration*.

## API assumptions (v1.0.7)

| Endpoint | Success markers | Main fields used |
|----------|-----------------|------------------|
| `GetAccountInfo` | `APIcode == 1000` and/or `APIstatus` success-like | `ClientInfo.balance`, `ClientInfo` for attributes |
| `GetSIMs` | Same | `SIMs` as **array** or **single object** (defensive parsing); SIM fields per [SwaggerHub Open-M2M/OpenM2M/v1.0.7](https://app.swaggerhub.com/apis-docs/Open-M2M/OpenM2M/v1.0.7) |
| `GetSubscriptions` | Same | `Subscriptions` and several alternate list keys / one-level wrappers (`data`, `result`, …); list, single row object, or **dict keyed by subscription id** (id copied into `subscription_id` when missing). Rows: `subscription_id` / `SubscriptionID`, `ICCID`, `MSISDN`, `product_description`, `status`, `start_date`, `expire_date`, `auto_renewal`, `sim_description` |
| `GetDatabundles` | Same | Form `subscription_id` (required). Parses `Databundles` / `databundles` and common wrappers (`data`, `result`, …). Rows: `databundle_id`, `product_description`, `status`, `start_date`, `expire_date`, `monthly` (numeric when not a boolean flag), `volumegroup` (scalar or object with id). Linked rows are exposed on `coordinator.data["databundles"]` (capped, deduped). |
| `GetVolumeGroups` | Same | Form `subscription_id` (optional in spec; **we always pass** the subscription id for accurate per-subscription volume). `VolumeGroups` → `volumegroup`, `volume`, `used`, `type` |
| `GetSubscriptionInfo` | Same | Form `subscription_id`. Optional: up to **10** subscriptions per refresh prioritized by missing **usage+allowance**, then missing **`monthly`**, then missing **`imei_lock`**; merges `SubscriptionInfo` (and `subscription_info` / `subscriptionInfo` wrappers), `Productinfo` / `ProductInfo`, `SIMinfo` / `sim_info` — **never** PIN/PUK |
| `SuspendSIM` / `UnsuspendSIM` | Same | Form `ICCID` (per spec casing) |

**Polling volume:** each coordinator cycle uses `GetAccountInfo` + `GetSIMs` + `GetSubscriptions`, then **per subscription** (bounded concurrency **3**): `GetDatabundles` and `GetVolumeGroups` in parallel, plus up to **10** optional `GetSubscriptionInfo` calls (also concurrency-limited) for subscriptions that still need usage/allowance **or** `monthly` **or** IMEI-lock fields. If `GetSubscriptions` fails, SIM/account sensors still update and subscription sensors stay empty until the next successful poll.

**Units:** OpenAPI examples use integer **MB**-like values for `VolumeGroup.volume` / `used` and for `SubscriptionInfo.limit_data`. This integration converts those numbers to **bytes** for `SensorDeviceClass.DATA_SIZE` sensors. If your tenant uses different units, expect incorrect magnitudes until mapped correctly.

**Matching:** subscription rows are linked to SIM devices by **ICCID** when both sides expose it (ICCID normalized to uppercase). If the subscription row has no ICCID but **MSISDN** or **IMSI** matches a SIM row, the ICCID from the SIM is borrowed for the device identifier.

Legacy JSON keys such as `status` (without `API`) are still accepted as success where applicable (see `api.is_api_success_payload`).

## Secrets

- The API key is stored in `ConfigEntry.data[CONF_API_KEY]`. It is **not**
  written to logs; the config flow uses a password text selector.
- When adding diagnostics, run the entry data through
  `homeassistant.components.diagnostics.async_redact_data` with
  `{"api_key", "apikey"}`.
- **SIM PIN / PUK** (and similar credentials) may appear under `SIMinfo` in portal JSON. This integration **does not** copy them into the normalized subscription dict, subscription sensor attributes, or the coordinator merge path. They are not shown on the **IMEI lock** diagnostic sensor (which is off by default).
- **Per-SIM** `OpenM2M SIM … subscription status` still forwards the raw `Productinfo` dict from `GetSIMs` in attributes when the portal returns it; treat that as **unredacted** if your tenant includes secrets there. A future `diagnostics.py` could redact those blobs.
- Never put the key in `configuration.yaml`; config-entry-only integrations
  are the modern HA pattern and a hard requirement for core inclusion.

## Path to official integration

1. **Now — custom component**: iterate fast here under `custom_components/`.
2. **Intermediate — HACS**: publish the same folder as a HACS custom repository
   (add a `hacs.json`, GitHub repo, releases). This gets real-world users and
   feedback without core's review latency.
3. **Official — single PR to `home-assistant/core`**:
   - Move the integration to `homeassistant/components/open_m2m/`.
   - Replace inline HTTP with a published PyPI client library (own repo, semver,
     tests) and list it in `manifest.json` → `requirements`.
   - Add `quality_scale: bronze` (then iterate to silver/gold) and meet the
     [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
     rules: full test coverage, strict typing, no I/O in `__init__`,
     `runtime_data` typed via `ConfigEntry[OpenM2MData]`, repair flows,
     diagnostics, etc.
   - Ship assets under `brand/` (2026.3+), and/or add a PR to `home-assistant/brands` for older HA releases.
   - One feature-complete PR is preferred over many small ones for new
     integrations.

## Useful links

- Open-M2M portal: <https://portal.open-m2m.com/>
- HA integration architecture: <https://developers.home-assistant.io/docs/creating_integration_file_structure>
- Quality scale: <https://developers.home-assistant.io/docs/core/integration-quality-scale/>
- Brands repo: <https://github.com/home-assistant/brands>
