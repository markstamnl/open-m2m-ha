# Open-M2M

Home Assistant custom integration for the [Open-M2M](https://portal.open-m2m.com/) portal API (account, SIMs, subscriptions, databundles, suspend/unsuspend).

[![GitHub Release](https://img.shields.io/github/v/release/markstamnl/open-m2m-ha?sort=semver)](https://github.com/markstamnl/open-m2m-ha/releases)
[![License: MIT](https://img.shields.io/github/license/markstamnl/open-m2m-ha)](https://github.com/markstamnl/open-m2m-ha/blob/main/LICENSE)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=markstamnl&repository=open-m2m-ha&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=open_m2m)

## Installation

**HACS:** Add this repo (**Integrations**), install **Open-M2M**, then restart Home Assistant.

**Manual:** Copy `custom_components/open_m2m/` next to `configuration.yaml` and restart.

Source and behavior details: [`custom_components/open_m2m/`](custom_components/open_m2m/). Brand images live under `custom_components/open_m2m/brand/` (served on Home Assistant **2026.3+**). Dutch translations via `translations/nl.json`.

## Configuration

**Settings** → **Devices & services** → **Add integration** → **Open-M2M** (domain `open_m2m`) — API key in the config flow. Cloud polling hub (`iot_class` in `manifest.json`).

## Releases

After bumping `custom_components/open_m2m/manifest.json` and `CHANGELOG.md` on the commit you intend to ship, create and push a semver tag (must point at that released code):

```bash
git tag v1.0.2 && git push origin v1.0.2
```

Adjust the version as needed. The [Release workflow](.github/workflows/release.yml) runs on `v*` tag pushes and publishes a GitHub Release (auto-generated release notes from merged PRs and commits).

## Features

- **Account** — balance and account-oriented sensors.
- **SIMs** — per-SIM devices (ICCID), status/usage/plan when the API provides them; SIM count at account level.
- **Subscriptions** — per-subscription devices/sensors (status, data used/allowance, bundles, renewals, timestamps; attributes where available).
- **Databundles** — linked bundle sensors (capped per refresh; see integration README).
- **Services** — `open_m2m.suspend_sim` / `open_m2m.unsuspend_sim` ([`services.yaml`](custom_components/open_m2m/services.yaml)).

Entity list, API mapping, and assumptions: [`custom_components/open_m2m/README.md`](custom_components/open_m2m/README.md).

**Docs · issues · releases:** [github.com/markstamnl/open-m2m-ha](https://github.com/markstamnl/open-m2m-ha) · [Issues](https://github.com/markstamnl/open-m2m-ha/issues) · tag `vX.Y.Z` and match `version` in `custom_components/open_m2m/manifest.json` (no `v` prefix).
