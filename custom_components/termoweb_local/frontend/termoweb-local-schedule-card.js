/*
 * termoweb-local-schedule-card: entry module.
 *
 * Loaded by the termoweb_local integration via add_extra_js_url (see
 * custom_components/termoweb_local/__init__.py, _async_register_frontend).
 * The card itself lives in schedule-card.js, split out with its editor
 * (schedule-card-editor.js), grid model (schedule-grid.js), styles
 * (schedule-styles.js) and presets (presets.js) as plain relative static
 * imports -- no build step, no bundler.
 *
 * Cache-busting: the StaticPathConfig serving this directory is registered
 * with cache_headers=False, so every sub-module is fetched fresh on a
 * dashboard reload; only this entry file's own URL carries the integration
 * version's "?v=" query string, to bust HA's frontend module cache when the
 * card's version bumps.
 */
import "./schedule-card.js";
