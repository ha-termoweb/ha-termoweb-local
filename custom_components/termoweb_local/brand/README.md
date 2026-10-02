# Brand assets

Home Assistant 2026.3 and newer serve an integration's icons from a `brand/` directory inside the custom component, taking priority over the `brands.home-assistant.io` CDN, so these files are picked up with no extra configuration. `hacs.json` already requires 2026.9.0.

`icon.svg` is the source of truth. The PNGs are generated from it and should never be edited by hand.

- `icon.svg`: 256x256 viewBox, flat shapes only, no gradients, no external fonts.
- `icon.png`: 256x256 RGBA, the size Home Assistant asks for.
- `[email protected]`: 512x512 RGBA, the hDPI version.

The mark is a column radiator in warm amber beside three cool emission arcs, on a dark rounded tile. The tile supplies its own background, so one set of files reads correctly on both light and dark themes and no `dark_icon.png` variant is needed. The design is independent artwork and deliberately borrows nothing from any vendor's mark or palette.

No `logo.png` is shipped. Home Assistant falls back to `icon.png` when the logo is absent, and a landscape lockup would have to carry a wordmark this project cannot use.

## Regenerating the PNGs

Any SVG rasteriser works. With `cairosvg`:

```
cairosvg custom_components/termoweb_local/brand/icon.svg -o custom_components/termoweb_local/brand/icon.png -W 256 -H 256
cairosvg custom_components/termoweb_local/brand/icon.svg -o custom_components/termoweb_local/brand/[email protected] -W 512 -H 512
```

With `rsvg-convert`:

```
rsvg-convert -w 256 -h 256 custom_components/termoweb_local/brand/icon.svg -o custom_components/termoweb_local/brand/icon.png
rsvg-convert -w 512 -h 512 custom_components/termoweb_local/brand/icon.svg -o custom_components/termoweb_local/brand/[email protected]
```

## Submitting to home-assistant/brands

The `home-assistant/brands` repository is now the legacy path for custom integrations, but a submission is still possible. It needs a pull request adding `custom_integrations/termoweb_local/icon.png` and `custom_integrations/termoweb_local/[email protected]`, with the folder name matching the `domain` in `manifest.json`. The images must be PNG, transparent, trimmed to the subject, and ideally interlaced and losslessly compressed; the two files here satisfy every requirement except interlacing, which neither rasteriser emits. The repository also expects the integration to be publicly reachable, which `manifest.json`'s `documentation` and `issue_tracker` URLs now satisfy.
