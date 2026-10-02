/*
 * Preset codes, names and colours for the termoweb-local schedule card.
 * Shared between the card element and the grid model.
 */

export const PRESETS = [
  { code: 0, key: "cold", label: "Cold", color: "#3d7dc4" },
  { code: 1, key: "night", label: "Night", color: "#7c5cbf" },
  { code: 2, key: "day", label: "Day", color: "#e08a1e" },
];

export const PRESET_BY_CODE = new Map(PRESETS.map((p) => [p.code, p]));

export const UNSET_COLOR = "var(--disabled-text-color, #9e9e9e)";

export const PRESET_STEP_C = 0.5;

// Absolute floor/ceiling (setpoint floor and entity max, termoweb_local.network's
// MIN/MAX_SETPOINT_C). The heater 02 live failure (2026-09-13 00:xx local:
// anti-frost 10.0 rejected against eco's fixed 7.5-20.5C range while eco was
// 23.0) showed the panel's per-preset ranges are relative to the other two
// presets, half a degree past the neighbour, not absolute limits, so the real
// rule (also enforced server-side by network._check_preset_order) is
// ordering: MIN_PRESET_C <= anti-frost < eco < comfort <= MAX_PRESET_C.
export const MIN_PRESET_C = 7.0;
export const MAX_PRESET_C = 35.0;

// This preset's own editable bounds given the other two presets' current
// values: anti-frost up to eco minus a step, eco between anti-frost and
// comfort each a step away, comfort down to eco plus a step.
export function presetBounds(code, ptemp) {
  const antifrost = ptemp ? ptemp[0] : null;
  const eco = ptemp ? ptemp[1] : null;
  const comfort = ptemp ? ptemp[2] : null;
  if (code === 0) {
    return { min: MIN_PRESET_C, max: (eco ?? MAX_PRESET_C) - PRESET_STEP_C };
  }
  if (code === 1) {
    return {
      min: (antifrost ?? MIN_PRESET_C) + PRESET_STEP_C,
      max: (comfort ?? MAX_PRESET_C) - PRESET_STEP_C,
    };
  }
  if (code === 2) {
    return { min: (eco ?? MIN_PRESET_C) + PRESET_STEP_C, max: MAX_PRESET_C };
  }
  return { min: MIN_PRESET_C, max: MAX_PRESET_C };
}
