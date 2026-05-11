# Open-M2M Home Assistant integration

This repository is the distribution home for the **Open-M2M** Home Assistant integration.

The integration source lives at `custom_components/open_m2m/`.

- **Branding** — `logo.png` / `icon.png` in that folder are picked up by Home Assistant next to `manifest.json`; the repo root may also keep `open-m2m-logo.png` as a source asset.

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
