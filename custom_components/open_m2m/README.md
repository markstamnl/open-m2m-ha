# Open-M2M (Home Assistant custom integration)

Custom integration for the [Open-M2M](https://portal.open-m2m.com/) portal API.

## Branding

- `logo.png` and `icon.png` live next to `manifest.json`; Home Assistant uses them in the config flow and integration list without manifest changes.

## Current features

- **Config flow** — API key (stored as `api_key`), validated with `POST …/GetAccountInfo` and `apikey=<key>` (`application/x-www-form-urlencoded`).
- **Polling hub** — `DataUpdateCoordinator` refreshes about every **15 minutes** (`DEFAULT_SCAN_INTERVAL`), calling `GetAccountInfo` and `GetSIMs` (OpenAPI **v1.0.7** paths; not `GetSIMList`).
- **Sensors**
  - **Account** (device: *Open-M2M* / Account)
    - **Account balance** — `ClientInfo.balance` when present; otherwise state is *Unknown* with attributes `client_info` (if any) and `raw_account_keys` to help diagnose field names.
    - **SIM count** — number of SIM rows parsed from the `GetSIMs` payload.
  - **Per SIM** (one device per ICCID, or `sub_<subscription_id>` if ICCID is missing)
    - **Subscription status** — `subscription_status`, or `status` / `SIMstatus`, or a coarse value from `archived`.
    - **Data usage (kB)** — only created if the SIM object exposes a recognizable usage field (e.g. `usage_kb`, nested `usage` / `stats` maps). Otherwise no usage entity is added.
    - **Plan data included (MB)** — only when no usage field was found but `Productinfo.data_included` exists (per OpenAPI `Product`; unit treated as **MB** in this integration—confirm against your account if needed).

SIM entities are created from the **first successful** coordinator refresh when the sensor platform loads. New SIMs that appear only on a later poll may require reloading the integration entry until dynamic discovery is added.

## Install (dev)

Copy this folder to `<config>/custom_components/open_m2m/` in your Home
Assistant install, restart, then add **Open-M2M** from
*Settings → Devices & services → Add integration*.

## API assumptions (v1.0.7)

| Endpoint | Success markers | Main fields used |
|----------|-----------------|------------------|
| `GetAccountInfo` | `APIcode == 1000` and/or `APIstatus` success-like | `ClientInfo.balance`, `ClientInfo` for attributes |
| `GetSIMs` | Same | `SIMs` as **array** or **single object** (defensive parsing); SIM fields per [SwaggerHub Open-M2M/OpenM2M/v1.0.7](https://app.swaggerhub.com/apis-docs/Open-M2M/OpenM2M/v1.0.7) |

Legacy JSON keys such as `status` (without `API`) are still accepted as success where applicable (see `api.is_api_success_payload`).

## Secrets

- The API key is stored in `ConfigEntry.data[CONF_API_KEY]`. It is **not**
  written to logs; the config flow uses a password text selector.
- When adding diagnostics, run the entry data through
  `homeassistant.components.diagnostics.async_redact_data` with
  `{"api_key", "apikey"}`.
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
   - Add brand assets via a PR to `home-assistant/brands`.
   - One feature-complete PR is preferred over many small ones for new
     integrations.

## Useful links

- Open-M2M portal: <https://portal.open-m2m.com/>
- HA integration architecture: <https://developers.home-assistant.io/docs/creating_integration_file_structure>
- Quality scale: <https://developers.home-assistant.io/docs/core/integration-quality-scale/>
- Brands repo: <https://github.com/home-assistant/brands>
