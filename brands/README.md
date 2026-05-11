# Home Assistant brands export (Open-M2M)

This folder mirrors what you add under [home-assistant/brands](https://github.com/home-assistant/brands) at `custom_integrations/open_m2m/`. The integration **domain** in `manifest.json` must stay **`open_m2m`** so paths and URLs match ([brands README — custom integrations](https://github.com/home-assistant/brands/blob/master/README.md)).

## Requirements (summary)

- **PNG only**, optimized for web; transparency preferred.
- **`icon.png`**: square **256×256**; **`icon@2x.png`**: **512×512**.
- **`logo.png` / `logo@2x.png`**: landscape preferred; shortest side **128–256** (normal) and **256–512** (hDPI).
- **`dark_*`**: optional; if omitted, the non-dark asset is used as fallback.

Full rules: [Image specification](https://github.com/home-assistant/brands/blob/master/README.md#image-specification).

## Open PR to home-assistant/brands

1. Fork **https://github.com/home-assistant/brands** (if you have not already).
2. Clone your fork and create a branch, for example: `git checkout -b add-open-m2m-custom-integration-branding`.
3. Copy this repo’s folder **`brands/custom_integrations/open_m2m/`** into your fork at the same path: **`custom_integrations/open_m2m/`** (so you have `icon.png`, `logo.png`, and any optional files next to each other).
4. Commit and push the branch to your fork.
5. Open a pull request against `home-assistant/brands` **master**.

**Suggested PR title:** `Add Open-M2M custom integration branding`

**Before merging:** confirm [brands repository CI](https://github.com/home-assistant/brands/actions) passes on your PR (image checks and layout).

**Domain check:** folder name and integration domain must be **`open_m2m`** (see `custom_components/open_m2m/manifest.json`).

## Export notes (this copy)

- **`icon.png`** is taken from `custom_components/open_m2m/brand/light256x256.png` so the file meets the **256×256** icon rule. The file named `brand/icon.png` in this integration repo is **225×225** and does not meet the brands icon size requirement.
- **`icon@2x.png`** is **not** included: the current `brand/icon@2x.png` is **256×256**; brands requires **512×512** for the hDPI icon. Add a true 512×512 asset under `brand/` and copy it as `icon@2x.png` before opening the brands PR (or rely on brands’ fallback to `icon.png` until then).

After the PR is merged, icons are available from the brands CDN (cache delays apply per [brands README](https://github.com/home-assistant/brands/blob/master/README.md#caching)).
