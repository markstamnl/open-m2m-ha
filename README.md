# Open-M2M Home Assistant integration

This repository is the distribution home for the **Open-M2M** Home Assistant integration.

The integration source lives at `custom_components/open_m2m/`.

- **Branding** — put `logo.png` and `icon.png` under `custom_components/open_m2m/brand/` (not next to `manifest.json`). Home Assistant **2026.3** and newer loads them via the built-in Brands proxy; older versions only show a logo if the domain exists on the [central brands CDN](https://github.com/home-assistant/brands). The repo root keeps `open-m2m-logo.png` as a source asset when updating those files.

## HACS

1. In Home Assistant, open **HACS** → **Integrations** (or the HACS menu where custom repositories are managed).
2. Choose **Custom repositories** (or **Add custom repository**).
3. Repository URL: `https://github.com/markstamnl/open-m2m-ha`.
4. Category: **Integration**.
5. After it is added, open the repository in HACS and **Download** / **Install** the integration.
6. Install **Open-M2M** (domain `open_m2m`). **Restart Home Assistant** after install or update so the integration loads.

## Manual install

Copy or symlink `custom_components/open_m2m/` from this repo into your Home Assistant configuration directory as `custom_components/open_m2m/`, then restart Home Assistant.

## Releases

For store-like updates and clear versions: create a Git tag `vX.Y.Z` (for example `v0.1.0`) and set `version` in `custom_components/open_m2m/manifest.json` to the same `X.Y.Z` string (without the leading `v`).
